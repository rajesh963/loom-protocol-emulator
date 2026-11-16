; ============================================================================
; onewire.asm — LOOM 1-Wire / DS18B20-style temperature device
; ============================================================================
;
; PIN MAPPING (open-drain; DIR=1 pulls DQ low, DIR=0 releases to the pull-up)
;   DQ   P[7] = uio[7]   bidirectional, open-drain, external 4.7k pull-up
;
; THREAD: any single thread (standalone example; not used by demo_all).
;
; DEVICE BEHAVIOUR (standard-speed 1-Wire timing, 60 MHz)
;   * waits for the master's RESET pulse (DQ low >= 480 us; 25 x 20 us steps),
;   * answers with a PRESENCE pulse 30 us after the release, 120 us long,
;   * receives ROM command (expects 0xCC SKIP ROM) and a function command,
;   * 0x44 CONVERT T : no data, returns to idle (temperature is "converted"),
;   * 0xBE READ SCRATCHPAD : shifts out 9 bytes on the master's read slots:
;       0x90 0x01 0x4B 0x46 0x7F 0xFF 0x0C 0x10 0x00   (+25.0 C, factory bytes)
;     The last byte is a zero CRC placeholder: the host demo does not verify
;     the CRC (a real DS18B20 would compute CRC-8/MAXIM; that is ~30 extra
;     instructions of bit twiddling and is left as an exercise).
;
; TIMING (values are 12.4 fixed point; 1 us = 60 cycles)
;   RESET_STEP      20 us = 1200 cyc = 0x4B00   (25 steps = 500 us low)
;   PRESENCE_DELAY  30 us = 1800 = 0x7080
;   PRESENCE_HALF   60 us = 3600 = 0xE100       (two halves = 120 us low)
;   SAMPLE_DELAY    15 us =  900 = 0x3840       (when a write slot is sampled)
;   SLOT_HOLD       40 us = 2400 = 0x9600       (device drives a read slot)
;   RECOVERY         5 us =  300 = 0x12C0
;
;   All waits use the 12.4 fractional accumulator, so slot widths are exact
;   regardless of instruction issue; edge detection is WAITP (<= 4 cycles).
;
; SIZE: 94 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ DQ_PIN,        0x0080        ; P[7] = uio[7]
.equ WAIT_DQ_LOW,   (DQ_PIN << 8) | 0          ; 0x8000
.equ WAIT_DQ_HIGH,  (DQ_PIN << 8) | DQ_PIN     ; 0x8080
.equ RESET_STEP,    0x4B00
.equ PRESENCE_DELAY, 0x7080
.equ PRESENCE_HALF, 0xE100
.equ SAMPLE_DELAY,  0x3840
.equ SLOT_HOLD,     0x9600
.equ RECOVERY,      0x12C0

; ----------------------------------------------------------------------------
start:
        clr     DQ_PIN          ; pin value 0: only visible while driving low
        dclr    DQ_PIN          ; release the line (open-drain idle high)
        setm    0
        ; scratchpad/table in DMEM[0..8]: +25.0 C DS18B20 reading
        movi    r0, 0x90
        st      0, r0
        movi    r0, 0x01
        st      1, r0
        movi    r0, 0x4B
        st      2, r0
        movi    r0, 0x46
        st      3, r0
        movi    r0, 0x7F
        st      4, r0
        movi    r0, 0xFF
        st      5, r0
        movi    r0, 0x0C
        st      6, r0
        movi    r0, 0x10
        st      7, r0
        movi    r0, 0x00
        st      8, r0
main:
        ; ---------------- detect a RESET pulse (DQ low >= 480 us) ----------
        waitp   WAIT_DQ_LOW
        movi    r4, 25
reset_loop:
        waitd   RESET_STEP
        in      r0
        andi    r0, DQ_PIN
        jnz     not_reset       ; line released early = a normal slot, resync
        subi    r4, 1
        jnz     reset_loop
        ; ---------------- PRESENCE: 30 us after release, 120 us long ------
        waitp   WAIT_DQ_HIGH
        waitd   PRESENCE_DELAY
        dset    DQ_PIN
        waitd   PRESENCE_HALF
        waitd   PRESENCE_HALF
        dclr    DQ_PIN
        waitd   RECOVERY
        ; ---------------- ROM command (0xCC SKIP ROM) ---------------------
        csrr    r7, TPC
        movi    r0, sub_rx_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        ; ---------------- function command --------------------------------
        csrr    r7, TPC
        movi    r0, sub_rx_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        cmpi    r1, 0xBE        ; READ SCRATCHPAD?
        jnz     main            ; 0x44 CONVERT T (or anything else): done
        movi    r5, 0           ; table index
        movi    r4, 9
read_loop:
        ldi     r1, r5          ; next scratchpad byte
        csrr    r7, TPC
        movi    r0, sub_tx_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        addi    r5, 1
        subi    r4, 1
        jnz     read_loop
        jmp     main

not_reset:
        waitp   WAIT_DQ_HIGH    ; let the short pulse end, then look again
        jmp     main

; ============================================================================
; receive one byte from the master's write slots (sampled 15 us into the slot)
sub_rx_byte:
        movi    r1, 0
        movi    r3, 8
rxb_loop:
        waitp   WAIT_DQ_LOW
        waitd   SAMPLE_DELAY
        in      r2
        andi    r2, DQ_PIN      ; 0x0000 / 0x0080 (bit 7 of the byte)
        shr     r1, 1           ; LSB-first: new bit shifts down from bit 7
        or      r1, r2
        waitp   WAIT_DQ_HIGH
        waitd   RECOVERY
        subi    r3, 1
        jnz     rxb_loop
        csrw    TPC, r7

; ============================================================================
; transmit one byte into the master's read slots (device holds the slot 40 us)
sub_tx_byte:
        movi    r3, 8
txb_loop:
        mov     r2, r1          ; 1-Wire is LSB-first
        shr     r1, 1
        andi    r2, 1           ; Z = !bit0
        waitp   WAIT_DQ_LOW     ; master starts a read slot
        waitd   RECOVERY        ; ~5 us: present the bit quickly
        jnz     txb_one
        dset    DQ_PIN          ; 0 -> pull DQ low
        jmp     txb_hold
txb_one:
        dclr    DQ_PIN          ; 1 -> release (pull-up high)
txb_hold:
        waitd   SLOT_HOLD
        dclr    DQ_PIN          ; release after the slot
        waitd   RECOVERY
        subi    r3, 1
        jnz     txb_loop
        csrw    TPC, r7

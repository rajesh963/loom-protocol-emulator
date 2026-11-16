; ============================================================================
; usb_ls_tx.asm — LOOM USB 1.1 Low-Speed *device-mode transmitter* (stretch)
; ============================================================================
;
; PIN MAPPING (single-ended prototype: only D+ is driven/observed by the chip)
;   D+  P[0] = uio[0]       push-pull (DIR bit 0 = 1), NRZI coded; idle J = low
;   (On a real board this needs a USB connector/Pmod with D- tied to the idle
;    level (high for LS) and a 1.5 kOhm pull-up on D-; the chip monitors/drives
;    only the D+ half of the differential pair, which is exactly what the
;    golden-model decoder below models. External parts are allowed by the FAQ.)
;
; WHAT IT TRANSMITS (device -> host, one DATA0 packet per host command)
;   [idle J] SYNC (8 raw bits 00000001 LSB-first, NRZI KJKJKJKK)
;            PID (0xC3 = DATA0: nibble 0011 + complement 1100, LSB first)
;            data field: 4 payload bytes supplied by the host
;            CRC16 (USB: poly 0x8005 reflected, init 0xFFFF, final XOR 0xFFFF,
;                   transmitted 16 bits LSB first)
;            EOP (SE0: both lines low for 2 bit cells; on the single-ended
;                 model that is D+ forced low for 2 cell times)
;            idle J until the next command
;   * NRZI: a 0 data bit toggles D+, a 1 leaves it unchanged.
;   * Bit stuffing: after six consecutive 1 bits a 0 is inserted; the stuffed
;     zero is NRZI coded like any other bit. Stuffing applies after SYNC (SYNC
;     itself is never stuffed) and stops before EOP.
;   * CRC16 covers the data field only (PID excluded), matching USB 2.0 §8.3.5.
;
; HOST INTERFACE
;   Host pushes 4 payload bytes with TX_PUSH(0, byte) ... x4.  The thread polls
;   its RX FIFO (TFIFO rx_cnt >= 4, every 256 cycles while idle), pulls the 4
;   bytes into DMEM[1..4], computes the CRC16, builds the packet in DMEM[0..6]
;   (PID, payload, CRC lo/hi) and sends it. No host doorbell needed.
;
; TIMING — event scheduler, not instruction counting
;   USB LS is 1.5 Mbit/s.  At the shipped 33.333 MHz one bit is 22.2222 cycles;
;   `setp 0x0164` (356 in 12.4 fixed point = 22.25 cycles) gives **1.4981
;   Mbit/s (-0.125 %)**, inside the USB LS +/-0.25 % budget, and the fractional
;   carry keeps every bit boundary drift-free.  Every NRZI edge is one `emit`;
;   the CPU must only keep the 4-deep event queue fed.
;   Per-bit instruction budget: the data loop needs ~12-14 instructions/bit
;   (worst case ~18 when a stuff bit is inserted) against 22.25 cycles/bit at
;   IPC 1 -- the queue absorbs the jitter, so edges stay on the 22.25-cycle grid.
;
; CYCLE BUDGET
;   SYNC 8 + PID 8 + data 32 + CRC 16 = 64 bits + <= 8 stuff bits = <= 72 bits
;   packet = <= 72 * 22.25 = 1602 cycles (48 us), EOP 2 cells = 44.5 cycles,
;   idle gap 3333 cycles (100 us).  One packet per ~0.15 ms idle loop.
;
; SIZE: 105 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ BIT_DIV,    0x0164          ; 356/16 = 22.25 cycles/bit -> 1.4981 Mbit/s
.equ IDLE_DIV,   0xD055          ; 3333 cycles = 100 us idle between packets
.equ POLL_DIV,   0x1000          ; 256-cycle RX-FIFO poll while idle
.equ DP_PIN,     0x0001          ; P[0] = uio[0], driven (DIR bit 0)
.equ PID_DATA0,  0x00C3          ; DATA0 PID (nibble + complement)

; ----------------------------------------------------------------------------
start:
        dset    DP_PIN          ; uio[0] is an output (push-pull)
        clr     DP_PIN          ; idle J (D+ low, D- tied high externally)
        setm    DP_PIN          ; this thread's events drive P[0] only (value=level)
        setp    BIT_DIV         ; 22.25-cycle bit clock, drift-free 12.4
main:
        ; ---------------- wait for a 4-byte payload ----------------------
wait4:
        waitd   POLL_DIV
        csrr    r7, TFIFO
        andi    r7, 0x0F00      ; rx_cnt[3:0]
        cmpi    r7, 0x0400      ; fewer than 4 bytes -> keep waiting
        jc      wait4
        ; ---------------- build the packet in DMEM[0..6] -----------------
        movi    r1, PID_DATA0
        st      0, r1
        pull    r1
        st      1, r1
        pull    r1
        st      2, r1
        pull    r1
        st      3, r1
        pull    r1
        st      4, r1
        ; ---- CRC16/USB over the 4 data bytes (DMEM[1..4]) ----
        movi    r2, 0xFFFF
        movi    r1, 1
        movi    r6, 4
crc_byte:
        ldi     r3, r1
        xor     r2, r3
        movi    r3, 8
crc_bit:
        mov     r4, r2
        andi    r4, 1
        jz      crc_shift
        shr     r2, 1
        movi    r4, 0xA001
        xor     r2, r4
        jmp     crc_next
crc_shift:
        shr     r2, 1
crc_next:
        subi    r3, 1
        jnz     crc_bit
        addi    r1, 1
        subi    r6, 1
        jnz     crc_byte
        movi    r3, 0xFFFF
        xor     r2, r3          ; final XOR (CRC-16/USB)
        mov     r3, r2
        andi    r3, 0x00FF
        st      5, r3           ; CRC low byte
        shr     r2, 8
        st      6, r2           ; CRC high byte
        ; ---------------- SYNC: 0x80 LSB-first, never stuffed -------------
        movi    r4, 0x80
        movi    r6, 8
sync_loop:
        mov     r7, r4
        shr     r4, 1
        andi    r7, 1           ; Z = !bit
        jz      sync_zero
        mov     r7, r0          ; bit 1: no transition
        emit    r7
        jmp     sync_next
sync_zero:
        movi    r7, 1
        xor     r0, r7          ; bit 0: toggle (NRZI)
        mov     r7, r0
        emit    r7
sync_next:
        subi    r6, 1
        jnz     sync_loop
        ; ---------------- PID + data + CRC, with bit stuffing -------------
        movi    r5, 0           ; consecutive-ones counter
        movi    r1, 0           ; DMEM byte index
        movi    r6, 7           ; PID + 4 data + 2 CRC bytes
byte_loop:
        ldi     r4, r1
        addi    r1, 1
        movi    r3, 8
bit_loop:
        cmpi    r5, 6           ; six 1s seen -> insert a 0 first
        jnz     no_stuff
        movi    r7, 1
        xor     r0, r7
        mov     r7, r0
        emit    r7
        movi    r5, 0
no_stuff:
        mov     r7, r4
        shr     r4, 1
        andi    r7, 1
        jz      bit_zero
        mov     r7, r0          ; bit 1: no transition
        emit    r7
        addi    r5, 1
        jmp     bit_next
bit_zero:
        movi    r7, 1
        xor     r0, r7          ; bit 0: toggle
        movi    r5, 0
        mov     r7, r0
        emit    r7
bit_next:
        subi    r3, 1
        jnz     bit_loop
        subi    r6, 1
        jnz     byte_loop
        ; ---------------- EOP: D+ forced low for 2 bit cells --------------
        movi    r0, 0
        movi    r7, 0
        emit    r7
        emit    r7
        ; ---------------- idle J, then look for the next payload ----------
        waitd   IDLE_DIV
        jmp     main

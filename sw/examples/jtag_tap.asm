; ============================================================================
; jtag_tap.asm — LOOM as an IEEE 1149.1 JTAG TAP *controller* (bit-banged)
; ============================================================================
;
; PIN MAPPING (4-wire TAP, all on the bidirectional uio pins so TDO is readable)
;   TCK  P[0] = uio[0]   output (push-pull)
;   TMS  P[1] = uio[1]   output
;   TDI  P[2] = uio[2]   output
;   TDO  P[3] = uio[3]   INPUT (DIR bit 3 stays 0; sampled via `in`)
; External DUT: any IEEE 1149.1 TAP.  host/jtag.py models the far end (a DUT
; with a 4-bit IR, IDCODE = 0x14B5A5A5 in IR 0x2, BYPASS in IR 0xF) and
; independently decodes the pin trace with the TAP state machine.
;
; WHAT IT DOES (one TAP transaction per run)
;   1. Test-Logic-Reset: 5 x TCK with TMS=1 (the TAP's reset entry).
;   2. Run-Test/Idle:    1 x TCK with TMS=0.
;   3. Navigate: Select-DR -> Select-IR -> Capture-IR -> Shift-IR (TMS=1,1,0,0).
;   4. Shift-IR: 4 bits of the IDCODE instruction (IR 0x2) LSB-first, TMS=1 on
;      the last bit (Exit1-IR); TDO is sampled at every rising edge and stored
;      in DMEM (so a slow host cannot stretch TCK).
;   5. Update-IR: 1 x TCK with TMS=1.
;   6. Navigate: Select-DR -> Capture-DR -> Shift-DR (TMS=1,0,0).
;   7. Shift-DR: DR_BITS bits LSB-first (TDI=0, so this is a read), TDO
;      captured to DMEM; last bit TMS=1 (Exit1-DR).
;   8. Update-DR: 1 x TCK with TMS=1, then all 4+DR_BITS captured bits are
;      pushed to the host TX FIFO (one 0/1 byte per bit) and the thread halts.
;   Captured TDO: 4 IR-scan bits (expect Capture-IR status 0b0001) then
;   DR_BITS DR-scan bits (expect the low DR_BITS bits of IDCODE; DR_BITS=32
;   reads the whole 32-bit IDCODE).  The host decoder also sees every bit.
;
; TIMING
;   Bit-banged with the 12.4 `waitd` accumulator (no instruction counting):
;   per pulse TMS/TDI are set, TCK rises, TDO settles for TSU=4 cycles, is
;   sampled, TCK falls, then HALF=16 cycles low.  Measured on the golden model:
;   TCK period 31..39 cycles (mean 34.8 -> 959 kHz), TCK HIGH phase exactly
;   6 cycles on every pulse, low phase padded by the fixed instruction issue
;   path (+-4 cycles = +-127 ns), so the DUT always sees a clean setup/hold
;   window; TDO changes on the falling edge there.  There is no JTAG maximum
;   TCK rate, so the low-phase padding is legal.  Raise HALF (0x0200 -> 512
;   cycles) for a slower cable.
;
; SIZE: 115 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ CLOCK_HZ,  33333333
.equ TCK_PIN,   0x0001           ; P[0] = uio[0]
.equ TMS_PIN,   0x0002           ; P[1] = uio[1]
.equ TDI_PIN,   0x0004           ; P[2] = uio[2]
.equ TDO_PIN,   0x0008           ; P[3] = uio[3], never set in DIR
.equ TSU,       0x0040           ; 4 cycles: TDO settle after TCK rises
.equ HALF,      0x0100           ; 16 cycles: TCK low phase
.equ IR_IDCODE, 0x0002           ; 4-bit IDCODE instruction
.equ DR_BITS,   8                ; DR scan length (32 reads the full IDCODE)

; ----------------------------------------------------------------------------
start:
        clr     0x000F          ; static low: TCK/TMS/TDI, no output enable yet
        dset    0x0007          ; TCK/TMS/TDI are outputs; TDO (bit3) stays input
        setm    0               ; this program never uses the event scheduler

        ; ---- Test-Logic-Reset: 5 x TCK with TMS=1 ----------------------
        movi    r3, TMS_PIN
        movi    r2, 5
tlr_loop:
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        subi    r2, 1
        jnz     tlr_loop

        ; ---- Run-Test/Idle (TMS=0) -------------------------------------
        movi    r3, 0
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0

        ; ---- Select-DR, Select-IR (TMS=1 x2) ---------------------------
        movi    r3, TMS_PIN
        movi    r2, 2
seldr_loop:
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        subi    r2, 1
        jnz     seldr_loop

        ; ---- Capture-IR, Shift-IR (TMS=0 x2) ---------------------------
        movi    r3, 0
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0

        ; ---- Shift-IR: 4 bits of IDCODE, LSB first ---------------------
        movi    r4, 0           ; DMEM capture index (cleared after Update-DR)
        movi    r1, IR_IDCODE
        movi    r2, 4
        csrr    r6, TPC
        movi    r0, sub_shift - . + 1
        add     r0, r6
        addi    r6, 5
        csrw    TPC, r0

        ; ---- Update-IR (shift left r3 = TMS_PIN on the last bit) -------
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0

        ; ---- Select-DR, Capture-DR, Shift-DR ---------------------------
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0        ; ; Select-DR   (r3 still TMS_PIN)
        movi    r3, 0
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0        ; ; Capture-DR
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0        ; ; Shift-DR

        ; ---- Shift-DR: DR_BITS bits, TDI=0 (read) ----------------------
        movi    r1, 0
        movi    r2, DR_BITS
        csrr    r6, TPC
        movi    r0, sub_shift - . + 1
        add     r0, r6
        addi    r6, 5
        csrw    TPC, r0

        ; ---- Update-DR --------------------------------------------------
        csrr    r7, TPC
        movi    r0, sub_pulse - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0

        ; ---- TAP idle: hand the captured TDO bits to the host -----------
        movi    r4, 0
        movi    r2, 4 + DR_BITS
tx_loop:
        ldi     r5, r4
        push    r5
        addi    r4, 1
        subi    r2, 1
        jnz     tx_loop
        halt

; ============================================================================
; sub_pulse — one TCK pulse with TMS/TDI = r3, TDO captured into r5
;   in : r3 = pin value (TCK bit must be 0), r7 = return address (call macro)
;   out: r5 = TDO level (0 or TDO_PIN)
; ============================================================================
sub_pulse:
        csrw    POUT, r3        ; TMS/TDI set while TCK is low
        set     TCK_PIN         ; TCK rising edge (the TAP samples TMS/TDI here)
        waitd   TSU             ; let TDO settle; sample at the eye centre
        in      r5
        clr     TCK_PIN         ; TCK falling edge: DUT updates TDO
        andi    r5, TDO_PIN
        waitd   HALF            ; TCK low phase
        csrw    TPC, r7

; ============================================================================
; sub_shift — shift r2 bits of r1 (LSB first) through TDI/TMS, capture TDO
;   in : r1 = data, r2 = bit count, r6 = return address (call macro)
;   out: r3 = TMS_PIN (the Exit1 pulse is the last bit), captured TDO bits are
;        pushed to the host TX FIFO as one 0/1 byte per bit
; ============================================================================
sub_shift:
ss_loop:
        mov     r3, r1
        andi    r3, 1           ; TDI bit
        shl     r3, 2           ; -> TDI_PIN (P[2]) of the pin value
        cmpi    r2, 1
        jnz     ss_notlast
        ori     r3, TMS_PIN     ; last shift bit also exits to Exit1-xx
ss_notlast:
        csrw    POUT, r3
        set     TCK_PIN
        waitd   TSU
        in      r5
        clr     TCK_PIN         ; falling edge: keep the high phase uniform
        andi    r5, TDO_PIN
        shr     r5, 3           ; captured TDO bit -> 0/1
        sti     r5, r4          ; DMEM[r4] = bit (no FIFO back-pressure on TCK)
        addi    r4, 1
        waitd   HALF
        shr     r1, 1
        subi    r2, 1
        jnz     ss_loop
        csrw    TPC, r6

; ============================================================================
; CALL CONVENTION
;   LOOM has no return stack; `csrw TPC, rN` is a computed jump (the CSR write
;   redirects the fetch).  Each call below is the position-independent 5-word
;   sequence: read the runtime PC (TPC), add the assembly-time relative offset
;   of the subroutine, set the return register to the instruction after the
;   call, and redirect the fetch.  sub_pulse returns via r7, sub_shift via r6.
; ============================================================================

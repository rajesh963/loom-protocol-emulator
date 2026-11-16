; ============================================================================
; i2c_master.asm — LOOM I2C controller: START/STOP/repeated-START, 7-bit addr,
;                 ACK/NAK, clock-stretching tolerant.  100 kHz or 400 kHz.
; ============================================================================
;
; PIN MAPPING (open-drain: DIR=1 pulls the line low, DIR=0 releases it; the
; external pull-ups/other devices drive the line high)
;   SCL  P[4] = uio[4]   output-enable only, value always 0        (DIR bit 4)
;   SDA  P[5] = uio[5]   output-enable only, value always 0; reads  (DIR bit 5)
;   Both lines are read back through `pins_in[4]`/`pins_in[5]`.
;
; THREAD: single thread (demo_all uses t3).
;
; TIMING
;   HALF_DIV = round(CLOCK_HZ*16/(2*I2C_HZ)) is the SCL low and high phase,
;   generated with the 12.4 fractional WAITD accumulator.  100 kHz @ 33.33 MHz:
;   166.6 cycles per phase (0x0A6A); 400 kHz: 41.6 cycles (0x029A).
;   Every time SCL is released the firmware WAITPs for the line to actually go
;   high, so a target that stretches the clock simply extends the low phase --
;   no timeout, no corruption.  SDA/SCL are never driven high (open-drain).
;   Rise-time is external (pull-up); the protocol only guarantees the timing
;   from the 30-70 % points, which is exactly what the model/VIP checks.
;
; SUBROUTINES
;   LOOM has no return stack, but `csrw TPC, rN` is a computed jump (the CSR
;   write redirects the fetch).  The tiny call macro below is position
;   independent: it reads the *runtime* PC and adds an assembly-time relative
;   offset, so the image can be loaded at any PMEM base.
;       CALL sub :  csrr r7,TPC ; movi r0, sub-.-1 ; add r0,r7 ; addi r7,5 ;
;                   csrw TPC,r0
;       return   :  csrw TPC,r7
;   r7 is the return-address register (no nesting is needed here), r0 is the
;   target scratch, r1 the argument/return, r2 the ACK sample, r3 a loop
;   counter.  Fixed opcodes only -- no self-modifying code.
;
; HOST INTERFACE (one 4-byte job; no doorbell, the thread polls its RX FIFO)
;   host -> TX_PUSH(t3, cmd) TX_PUSH(t3, dev) TX_PUSH(t3, reg) TX_PUSH(t3, data)
;   cmd bit0 = 0: write   START, dev+W, reg, data, STOP        (3 bytes on bus)
;   cmd bit0 = 1: read    START, dev+W, reg, Sr, dev+R, byte, NAK, STOP
;   dev = (7-bit address)<<1 | R/W.
;   A read job PUSHes the received byte to the thread TX FIFO (host RX_POP(t3)).
;
; CYCLE BUDGET (100 kHz, HALF = 166.6 cycles)
;   START ~ 4.5 phases = 1350 cyc, each byte = 9 phases = 2700 cyc,
;   STOP ~ 4 phases = 667 cyc.  A 3-byte write is ~10.4 kcycles (313 us on the
;   wire); the CPU is idle inside WAITD >95 % of the time, so this thread can
;   share the chip with the other three example programs.
;   At 400 kHz the same program runs 4x faster (HALF_DIV scales); measured on
;   the model, the CPU still keeps up: ~40 instructions per 42-cycle phase at
;   worst, and the WAITPs absorb scheduler jitter.
;
; SIZE: ~120 instructions (< 128, fits the FF fallback build; the call macro
; keeps the 4 bus byte-loops from being inlined 4 times).  NOTE: this example
; needs the RTL fix that makes a thread `CSRW TPC` redirect the fetch cleanly
; (model already does; RTL has a stale-instruction-buffer bug reported by the
; toolchain owner).  It is the only example that uses computed jumps.
;
; ASSEMBLE: same command as the others; image is position independent.
; ============================================================================

.equ CLOCK_HZ,   33333333
.equ I2C_HZ,     100000           ; set 400000 for fast mode

.equ HALF_DIV,   (CLOCK_HZ * 16) / (2 * I2C_HZ)     ; 100k: 0x0A6A, 400k: 0x029A
.equ QUARTER_DIV, HALF_DIV / 2
.equ SCL_PIN,    0x0010           ; P[4] = uio[4]
.equ SDA_PIN,    0x0020           ; P[5] = uio[5]
.equ WAIT_SCL_HI,  (SCL_PIN << 8) | SCL_PIN          ; 0x1010
.equ IDLE_DIV,   0x1000           ; 256 cycles between idle FIFO polls
; dev/reg/data are pulled into r5/r4/r6; r0/r1/r2/r3/r7 are subroutine scratch

; ----------------------------------------------------------------------------
start:
        clr     SCL_PIN | SDA_PIN   ; pin value 0 (only seen while driving low)
        dclr    SCL_PIN | SDA_PIN   ; release both lines (open-drain idle)
        setm    0                   ; no event-driven pins
main:
        waitd   IDLE_DIV            ; idle: ~4 % of issue slots
        csrr    r0, TFIFO
        andi    r0, 0x0F00          ; any job bytes queued?
        jz      main
        pull    r6                  ; r6 = cmd
        pull    r5                  ; r5 = dev address byte
        pull    r4                  ; r4 = register pointer
        ; ---- START ----
        csrr    r7, TPC
        movi    r0, sub_start - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        ; ---- dev + W ----
        mov     r1, r5
        csrr    r7, TPC
        movi    r0, sub_send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        ; ---- register pointer ----
        mov     r1, r4
        csrr    r7, TPC
        movi    r0, sub_send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        ; ---- write or read? ----
        andi    r6, 1
        jnz     do_read
        pull    r1                  ; write: data byte
        csrr    r7, TPC
        movi    r0, sub_send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        jmp     do_stop
do_read:
        ; repeated START, dev | R, read one byte, NAK, STOP
        csrr    r7, TPC
        movi    r0, sub_start - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        mov     r1, r5
        ori     r1, 1               ; R/W bit = 1
        csrr    r7, TPC
        movi    r0, sub_send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        csrr    r7, TPC
        movi    r0, sub_read_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        push    r1                  ; received byte -> host
do_stop:
        csrr    r7, TPC
        movi    r0, sub_stop - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        jmp     main

; ============================================================================
; subroutines (each ends with `csrw TPC, r7` = return)
; ============================================================================
sub_start:
        dclr    SDA_PIN             ; SDA high
        dclr    SCL_PIN             ; SCL high
        waitp   WAIT_SCL_HI
        waitd   HALF_DIV            ; tSU;STA
        dset    SDA_PIN             ; SDA falls while SCL high = START
        waitd   HALF_DIV            ; tHD;STA
        dset    SCL_PIN             ; SCL low (first data bit's WAITD is the low phase)
        csrw    TPC, r7

sub_stop:
        dset    SDA_PIN             ; SDA low
        waitd   HALF_DIV
        dclr    SCL_PIN             ; SCL high
        waitp   WAIT_SCL_HI
        waitd   HALF_DIV            ; tSU;STO
        dclr    SDA_PIN             ; SDA rises while SCL high = STOP
        waitd   HALF_DIV            ; bus free time
        csrw    TPC, r7

; send the byte in r1, MSB first; returns r2 = 0 on ACK (SDA low on 9th clock)
sub_send_byte:
        shl     r1, 8               ; data bits in 15..8; ADD shifts them out
        movi    r3, 8
sb_loop:
        add     r1, r1              ; C = next bit
        jnc     sb_zero
        dclr    SDA_PIN             ; 1 -> release (pull-up high)
        jmp     sb_sda
sb_zero:
        dset    SDA_PIN             ; 0 -> pull low
sb_sda:
        waitd   HALF_DIV            ; data setup while SCL low
        dclr    SCL_PIN             ; release SCL (target may stretch)
        waitp   WAIT_SCL_HI
        waitd   HALF_DIV            ; SCL high phase
        dset    SCL_PIN             ; SCL low (the next bit's WAITD is the low phase)
        subi    r3, 1
        jnz     sb_loop
        ; ACK clock: release SDA, clock 9th pulse, sample SDA at mid-high
        dclr    SDA_PIN
        waitd   HALF_DIV            ; SCL low phase
        dclr    SCL_PIN
        waitp   WAIT_SCL_HI
        waitd   QUARTER_DIV         ; sample in the middle of the high phase
        in      r2
        andi    r2, SDA_PIN         ; 0x0020 = NAK, 0 = ACK
        waitd   QUARTER_DIV
        dset    SCL_PIN             ; SCL low again
        waitd   HALF_DIV
        csrw    TPC, r7

; read a byte into r1, MSB first, then send NAK (master does not ACK)
sub_read_byte:
        movi    r1, 0
        movi    r3, 8
rb_loop:
        dclr    SDA_PIN             ; release SDA so the target can drive it
        waitd   HALF_DIV            ; SCL low: target puts the next bit on SDA
        dclr    SCL_PIN
        waitp   WAIT_SCL_HI
        waitd   QUARTER_DIV         ; sample mid-high
        in      r2
        andi    r2, SDA_PIN
        shr     r2, 5               ; 0x0020 -> bit 0
        shl     r1, 1
        or      r1, r2
        waitd   QUARTER_DIV
        dset    SCL_PIN             ; SCL low (next bit's WAITD is the low phase)
        subi    r3, 1
        jnz     rb_loop
        ; 9th clock: NAK (keep SDA released)
        dclr    SDA_PIN
        waitd   HALF_DIV
        dclr    SCL_PIN
        waitp   WAIT_SCL_HI
        waitd   HALF_DIV
        dset    SCL_PIN
        waitd   HALF_DIV
        csrw    TPC, r7

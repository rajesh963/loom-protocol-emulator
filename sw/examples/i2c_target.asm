; ============================================================================
; i2c_target.asm — LOOM I2C target (slave) with a DMEM register file, 100 kHz
; ============================================================================
;
; PIN MAPPING (open-drain; DIR=1 pulls the line low, DIR=0 releases to the
; external pull-up)
;   SCL  P[6] = uio[6]   output-enable only (target can stretch the clock)
;   SDA  P[7] = uio[7]   output-enable only; both lines read via pins_in[6:7]
;
; THREAD: single thread.  Used standalone / in the VIP tests; demo_all runs the
; I2C *master* example instead.
;
; PROTOCOL (standard register-target behaviour)
;   * 7-bit address MY_ADDR (default 0x50); ACK on match, NAK + resync otherwise.
;   * Write:  START addr+W ACK [reg] ACK [data] ACK ... STOP
;   * Read:   START addr+W ACK [reg] ACK Sr addr+R ACK [data] ACK/NAK ... STOP
;   * The register file is DMEM words 0..7 (8-bit); the first write byte is the
;     register pointer, following bytes auto-increment it modulo 8.
;   * Reads return DMEM[reg] and auto-increment.  The host can inspect the file
;     with READ_MEM / host/dmem readback.
;
; HOW IT REACTS — all timing is WAITP polling, so the CPU never owns the clock:
;   waitp 0xC040 = SCL high & SDA low    -> START / repeated START
;   waitp 0x4040 = SCL high              -> sample a data bit
;   waitp 0x4000 = SCL low               -> next bit / drive next bit
;   waitp 0xC0C0 = SCL high & SDA high   -> STOP / bus idle
;   A byte boundary is resolved by sub_next_byte: sample SDA at the rising edge
;   and poll while SCL is high - a normal data bit is stable, a START/STOP makes
;   SDA move.  That is how the target tells a repeated START from a data byte.
;   Detection latency is 2-4 cycles against a 300-cycle half period at 100 kHz.
;
; SUBROUTINES: position-independent CALL/RETURN via `csrw TPC` (computed jump),
;   same macro as i2c_master.asm.  r6 = DMEM register pointer, r4 = scratch,
;   r1/r2/r3 subroutine scratch, r7 = link register, r0 = call scratch.
;
; SIZE: ~124 instructions (< 128, fits the FF fallback build).
; ============================================================================

.equ MY_ADDR,      0x50           ; 7-bit target address
.equ SCL_PIN,      0x0040         ; P[6] = uio[6]
.equ SDA_PIN,      0x0080         ; P[7] = uio[7]
.equ WAIT_START,   (SCL_PIN | SDA_PIN) << 8 | SCL_PIN   ; 0xC040
.equ WAIT_SCL_HI,  (SCL_PIN << 8) | SCL_PIN             ; 0x4040
.equ WAIT_SCL_LO,  (SCL_PIN << 8) | 0                   ; 0x4000
.equ WAIT_STOP,    (SCL_PIN | SDA_PIN) << 8 | (SCL_PIN | SDA_PIN)  ; 0xC0C0

; ----------------------------------------------------------------------------
start:
        clr     SCL_PIN | SDA_PIN   ; pin values 0: only visible while driving
        dclr    SCL_PIN | SDA_PIN   ; release both lines
        setm    0
        movi    r6, 0               ; DMEM register pointer
main:
        waitp   WAIT_START          ; START: SDA falls while SCL is high
        waitp   WAIT_SCL_LO
        csrr    r7, TPC
        movi    r0, sub_rx_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        mov     r4, r1              ; r4 = address byte (addr<<1 | R/W)
        mov     r2, r1
        shr     r2, 1
        cmpi    r2, MY_ADDR
        jnz     nak_resync
        ; ---- ACK the address ----
        dset    SDA_PIN
        waitp   WAIT_SCL_HI
        waitp   WAIT_SCL_LO
        dclr    SDA_PIN
        andi    r4, 1
        jnz     read_path
        ; ================= WRITE =================
        csrr    r7, TPC
        movi    r0, sub_rx_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        mov     r6, r1
        andi    r6, 7               ; first byte = register pointer
        dset    SDA_PIN             ; ACK
        waitp   WAIT_SCL_HI
        waitp   WAIT_SCL_LO
        dclr    SDA_PIN
write_next:
        csrr    r7, TPC
        movi    r0, sub_next_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        cmpi    r5, 0               ; 0 = data byte, 2 = STOP, 3 = Sr
        jnz     main
        sti     r1, r6              ; DMEM[reg] = data
        addi    r6, 1
        andi    r6, 7
        dset    SDA_PIN             ; ACK
        waitp   WAIT_SCL_HI
        waitp   WAIT_SCL_LO
        dclr    SDA_PIN
        jmp     write_next

        ; ================= READ =================
read_path:
        ldi     r1, r6              ; r1 = DMEM[reg]
        csrr    r7, TPC
        movi    r0, sub_tx_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        ; master ACK/NAK clock: 9th pulse, SDA released by sub_tx_byte
        dclr    SDA_PIN
        waitp   WAIT_SCL_HI
        in      r2
        andi    r2, SDA_PIN         ; 0 = ACK -> send the next byte
        waitp   WAIT_SCL_LO
        jnz     read_done
        addi    r6, 1
        andi    r6, 7
        jmp     read_path
read_done:
        waitp   WAIT_STOP           ; NAK -> wait for the STOP
        jmp     main

nak_resync:
        dclr    SDA_PIN             ; NAK (leave SDA released)
        ; a data "1" also looks like (SCL=1,SDA=1) for one high phase, so
        ; re-check after 320 cycles (> the 300-cycle high phase at 100 kHz)
        waitp   WAIT_STOP
        waitd   0x1400
        in      r2
        andi    r2, SCL_PIN | SDA_PIN
        cmpi    r2, SCL_PIN | SDA_PIN
        jnz     nak_resync
        jmp     main

; ============================================================================
; subroutines
; ============================================================================
; receive 8 bits MSB-first into r1; entry/exit with SCL low
sub_rx_byte:
        movi    r1, 0
        movi    r3, 8
rxb_loop:
        waitp   WAIT_SCL_HI
        in      r2
        andi    r2, SDA_PIN
        shr     r2, 7
        shl     r1, 1
        or      r1, r2
        waitp   WAIT_SCL_LO
        subi    r3, 1
        jnz     rxb_loop
        csrw    TPC, r7

; Resolve one byte boundary after an ACK.  Returns r5 = 0 with r1 = data byte,
; or r5 = 2 (STOP seen) / r5 = 3 (repeated START seen).
sub_next_byte:
        waitp   WAIT_SCL_HI
        in      r4
        andi    r4, SDA_PIN         ; first-bit sample, in mask form
nb_poll:
        ; SCL first: the moment SCL is low this is a normal data bit and the
        ; master is allowed to change SDA (setup/hold time can be ~0 cycles on
        ; a fast master).  Only an SDA move while SCL is STILL high is a
        ; START/repeated START/STOP.
        in      r2
        andi    r2, SCL_PIN
        jz      nb_data             ; SCL fell -> this was a normal data bit
        in      r2
        andi    r2, SDA_PIN
        cmp     r2, r4
        jnz     nb_event            ; SDA moved while SCL high -> START/STOP
        waitd   0x00A0              ; 10 cycles between polls
        jmp     nb_poll
nb_event:
        andi    r2, SDA_PIN         ; SDA now low -> repeated START, high -> STOP
        jz      nb_start
        movi    r5, 2               ; STOP
        csrw    TPC, r7
nb_start:
        movi    r5, 3               ; repeated START
        csrw    TPC, r7
nb_data:
        shr     r4, 7               ; first data bit -> bit 0
        mov     r1, r4
        movi    r3, 7
        movi    r5, 0
        jmp     rxb_loop            ; receive the remaining 7 bits, then return

; send the byte in r1 MSB-first; entry/exit with SCL low, releases SDA
sub_tx_byte:
        shl     r1, 8               ; MSB of the byte -> bit 15 for ADD/C
        movi    r3, 8
txb_loop:
        add     r1, r1              ; C = next data bit
        jnc     txb_zero
        dclr    SDA_PIN             ; 1 -> release
        jmp     txb_sda
txb_zero:
        dset    SDA_PIN             ; 0 -> pull low
txb_sda:
        waitp   WAIT_SCL_HI
        waitp   WAIT_SCL_LO
        subi    r3, 1
        jnz     txb_loop
        dclr    SDA_PIN             ; release for the master's ACK/NAK clock
        csrw    TPC, r7

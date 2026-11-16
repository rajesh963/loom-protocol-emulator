; ============================================================================
; ps2_target.asm — LOOM PS/2 device (keyboard-style) transmitter
; ============================================================================
;
; PIN MAPPING (device -> host; the example drives both lines)
;   CLK   P[13] = uo_out[5]   open-collector in real PS/2; push-pull is fine
;   DATA  P[14] = uo_out[6]   when no host clock-inhibit is used
;
; THREAD: any single thread (standalone example; not used by demo_all).
;
; PROTOCOL
;   Device->host frame, one bit per clock pulse, 11 bits:
;     start(0), data0..data7 (LSB first), odd parity, stop(1)
;   Clock idle high.  For each bit the device places DATA, waits tSETUP,
;   pulls CLK low for ~35 us, releases it high for ~35 us.  The host samples
;   DATA on the falling edge.  Frames are separated by a >50 us idle gap.
;
; TIMING (33.33 MHz, ~14.3 kHz clock -> 70 us/bit)
;   HALF  = 35 us = 1166.7 cycles = 0x48EB (12.4 fixed point)
;   SETUP =  5 us =  166.7 cycles = 0x0A6B
;   GAP   = 50 us = 1666.7 cycles = 0x682B
;   The per-bit WAITDs use the 12.4 accumulator, so a frame is 11*4200 cycles
;   +/- <1/16 cycle and independent of the instruction schedule.
;
; HOST INTERFACE
;   Host pushes one byte (the scancode to send) with TX_PUSH; the thread polls
;   its RX FIFO every 256 cycles, sends the 11-bit frame, then idles.  Multiple
;   queued bytes are sent back-to-back with the inter-frame gap.
;
; SIZE: 70 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ HALF_DIV,  0x48EB          ; 1166.7 cycles = 35 us
.equ SETUP_DIV, 0x0A6B          ; 166.7 cycles = 5 us
.equ GAP_DIV,   0x682B          ; 1666.7 cycles = 50 us
.equ IDLE_DIV,  0x1000          ; 256-cycle RX-FIFO poll
.equ CLK_PIN,   0x2000          ; P[13] = uo_out[5]
.equ DATA_PIN,  0x4000          ; P[14] = uo_out[6]

; ----------------------------------------------------------------------------
start:
        set     CLK_PIN         ; idle: clock high, data high
        set     DATA_PIN
        setm    0               ; no event-driven pins
main:
        waitd   IDLE_DIV
        csrr    r0, TFIFO
        andi    r0, 0x0F00      ; scancode queued?
        jz      main
        pull    r1              ; r1 = data byte
        movi    r5, 0           ; parity accumulator (XOR of data bits)
        movi    r2, 0           ; start bit = 0
        csrr    r7, TPC
        movi    r0, sub_bit - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        movi    r4, 8
data_loop:
        mov     r2, r1
        andi    r2, 1           ; LSB first
        xor     r5, r2          ; parity accumulator
        csrr    r7, TPC
        movi    r0, sub_bit - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        shr     r1, 1
        subi    r4, 1
        jnz     data_loop
        andi    r5, 1           ; odd parity: bit = 1 when data popcount is even
        jz      par_one
        movi    r2, 0
        jmp     par_send
par_one:
        movi    r2, 1
par_send:
        csrr    r7, TPC
        movi    r0, sub_bit - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        movi    r2, 1           ; stop bit = 1
        csrr    r7, TPC
        movi    r0, sub_bit - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        set     DATA_PIN        ; idle lines high
        set     CLK_PIN
        waitd   GAP_DIV         ; inter-frame gap
        jmp     main

; send the bit in r2: place DATA, tSETUP, CLK low 35 us, CLK high 35 us
sub_bit:
        cmpi    r2, 0
        jz      bit_zero
        set     DATA_PIN
        jmp     bit_clk
bit_zero:
        clr     DATA_PIN
bit_clk:
        waitd   SETUP_DIV
        clr     CLK_PIN
        waitd   HALF_DIV
        set     CLK_PIN
        waitd   HALF_DIV
        csrw    TPC, r7

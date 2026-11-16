; ============================================================================
; ws2812.asm — LOOM WS2812/WS2812B driver, 800 kHz, event-scheduler timed
; ============================================================================
;
; PIN MAPPING
;   DATA  P[12] = uo_out[4]   output, push-pull, idle low (matches info.yaml)
;
; THREAD: any single thread (not used by demo_all; standalone LED demo).
;
; TIMING (60 MHz, bit period 1.25 us = 75 cycles)
;   T0H = 0.40 us =  24 cycles = 0x0180    T0L = 0.85 us = 51 cycles = 0x0330
;   T1H = 0.80 us =  48 cycles = 0x0300    T1L = 0.45 us = 27 cycles = 0x01B0
;   RESET latch: > 50 us low -> 3000 cycles (0xBB80 in 12.4)
;
;   Every pulse is one `emit` and every pulse width is set by `setp` right
;   before it, so the hardware event scheduler - not instruction timing -
;   defines the waveform.  Emitting an event costs 2 instructions; the CPU has
;   75 cycles per bit, so the queue back-pressure never starves the stream.
;   The `emit` chain is NEXTT-relative, so high+low always sums to exactly the
;   period register (drift-free; reset gap restored by the clamp).
;
; HOST INTERFACE
;   Host pushes 3 bytes per LED in GRB order (G, R, B) with TX_PUSH; the thread
;   polls its RX FIFO every 256 cycles, pops G,R,B and shifts 24 bits MSB-first
;   (WS2812 wants G7..G0, R7..R0, B7..B0), then holds DATA low for the latch.
;   Push whole LEDs (multiples of 3 bytes) - a partial LED will block on PULL.
;
; CYCLE BUDGET
;   per LED  = 24 bits * 75 cycles + 3000 reset = 4800 cycles (80 us)
;   per bit  = ~8 instructions < 75 cycles -> the CPU keeps up with 800 kHz
;   maximum sustained LED rate ~ 12.5 k LEDs/s (limited by the reset gap)
;
; SIZE: 55 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ T0H,       0x0180          ; 24 cycles high
.equ T0L,       0x0330          ; 51 cycles low
.equ T1H,       0x0300          ; 48 cycles high
.equ T1L,       0x01B0          ; 27 cycles low
.equ RESET_DIV, 0xBB80          ; 3000 cycles = 50 us latch
.equ IDLE_DIV,  0x1000          ; 256-cycle RX-FIFO poll
.equ DATA_PIN,  0x1000          ; P[12] = uo_out[4]

; ----------------------------------------------------------------------------
start:
        clr     DATA_PIN
        setm    DATA_PIN
        movi    r2, DATA_PIN    ; "1" cell value
        movi    r3, 0           ; "0" cell value
main:
        waitd   IDLE_DIV
        csrr    r0, TFIFO
        andi    r0, 0x0F00      ; any LED bytes queued?
        jz      main
        pull    r5              ; G
        mov     r1, r5
        csrr    r7, TPC
        movi    r0, sub_send8 - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        pull    r5              ; R
        mov     r1, r5
        csrr    r7, TPC
        movi    r0, sub_send8 - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        pull    r5              ; B
        mov     r1, r5
        csrr    r7, TPC
        movi    r0, sub_send8 - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        waitd   RESET_DIV       ; latch: DATA stays low
        jmp     main

; send the byte in r1 MSB-first as 8 WS2812 bits; r2/r3 = pin vector values
sub_send8:
        shl     r1, 8           ; MSB -> bit 15 for the ADD/C trick
        movi    r4, 8
ws_loop:
        add     r1, r1          ; C = next data bit
        jc      ws_one
        setp    T0L             ; LOW-cell duration before the HIGH event
        emit    r2              ; high pulse (T0H, set by the next emit)
        setp    T0H
        emit    r3              ; low pulse (T0L)
        jmp     ws_next
ws_one:
        setp    T1L
        emit    r2
        setp    T1H
        emit    r3
ws_next:
        subi    r4, 1
        jnz     ws_loop
        csrw    TPC, r7

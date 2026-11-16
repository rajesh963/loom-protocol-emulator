; WS2812 / SK6812 stream at 800 kHz (41.67 cycles/bit @ 33.33 MHz).
; LED data on P[12] = WS2812_DATA = uo[4] (0x1000). T0H = 24, T1H = 48 cycles.
.equ LED,   0x1000
.equ SPIN,  0x01FC            ; waitd 31.75 cycles; loop body = 10 cycles -> 41.75
.equ RESET, 0x682B            ; 1666.7 cycles = 50 us low

pixel:
    setm   LED
    li     r5, LED            ; constant "high" value for the rise edge
    li     r3, 0              ; constant "low" value for the fall edge
    pull   r0                 ; one byte from the host = one colour component
    li     r1, 8
bitloop:
    mov    r2, r0
    andi   r2, 0x80           ; MSB first
    jz     bit0
    emitd  r5, 0              ; rise (fires next cycle)
    emitd  r3, 27             ; fall after 27 cycles = a '1'
    jmp    next
bit0:
    emitd  r5, 0
    emitd  r3, 13             ; fall after 13 cycles = a '0'
    nop                       ; pad so both branches take the same time
next:
    waitd  SPIN               ; 31.75 + 10 = 41.75 cycles per bit
    shl    r0, 1
    subi   r1, 1
    jnz    bitloop
    jmp    pixel

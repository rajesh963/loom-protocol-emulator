; WS2812 / SK6812 stream at 800 kHz (75 cycles/bit @ 60 MHz).
; LED data on P[14] = WS2812_DATA = uo[6] (0x4000). T0H = 24, T1H = 48 cycles.
.equ LED,   0x4000
.equ SPIN,  0x0410            ; waitd 65 cycles; loop body = 10 cycles -> 75
.equ RESET, 0xF000            ; 3840 cycles = 64 us low

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
    emitd  r3, 48             ; fall after 48 cycles = a '1'
    jmp    next
bit0:
    emitd  r5, 0
    emitd  r3, 24             ; fall after 24 cycles = a '0'
    nop                       ; pad so both branches take the same time
next:
    waitd  SPIN               ; 65 + 10 = 75 cycles per bit
    shl    r0, 1
    subi   r1, 1
    jnz    bitloop
    jmp    pixel

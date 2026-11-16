; PS/2 host (device-to-host), ~11 kHz clock from the keyboard.
; PS2_CLK = P[2] = uio[2] (0x04), PS2_DAT = P[3] = uio[3] (0x08)
ps2:
    dir    0x00              ; both lines are inputs (external pull-ups)
    li     r0, 0
    li     r1, 8
bitloop:
    shr    r0, 1             ; make room for the bit at LSB-first position 7
    waitp  0x0404            ; CLK high
    waitp  0x0400            ; CLK low: falling edge, data is valid
    in     r2
    andi   r2, 0x0008        ; DAT
    jz     no_bit
    ori    r0, 0x0080
no_bit:
    subi   r1, 1
    jnz    bitloop
    waitp  0x0404            ; parity bit
    waitp  0x0400
    waitp  0x0404            ; stop bit
    waitp  0x0400
    push   r0                ; scan code to the host
    jmp    ps2

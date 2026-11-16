; UART receiver, 8N1, 115200 baud @ 60 MHz. RX on P[11] = ui[3].
.equ BITPER,  0x208D         ; 520.8125 cycles
.equ MIDBIT,  0x30D4         ; 1.5 bits = 781.25 cycles
.equ RX,      0x0800         ; P[11] in the pins_in vector

rx:
    setm   0                 ; input thread: events must not drive
hunt:
    in     r2                ; poll for the start edge (3-cycle loop)
    andi   r2, RX
    jnz    hunt              ; loop while P[11] is high
    waitd  MIDBIT            ; move to the centre of bit 0
    li     r0, 0
    li     r1, 8
bitloop:
    shr    r0, 1
    in     r2                ; sample pins_in at this exact cycle
    andi   r2, RX
    jz     no_bit
    ori    r0, 0x80
no_bit:
    waitd  BITPER
    subi   r1, 1
    jnz    bitloop
    in     r2                ; stop bit, 9.5 bits after the start edge
    andi   r2, RX
    jz     rx
    push   r0                ; hand the byte to the host
    jmp    rx

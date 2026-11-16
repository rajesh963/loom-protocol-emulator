; UART transmitter, 8N1, 115200 baud @ 60 MHz. TX on P[10] = uo[2].
.equ BITPER, 0x208D          ; 520.8125 cycles/bit (12.4 fixed) -> 115205 Bd
.equ TX,     0x0400          ; P[10] in the pins_out vector

start:
    setm   TX                ; this thread only ever drives the TX pin
    setp   BITPER            ; every emit() is one bit after the previous one
send:
    pull   r0                ; block until the host pushes a byte
    shl    r0, 1             ; frame: b0 = start(0), b1..b8 = data LSB first
    ori    r0, 0x0200        ; b9 = stop(1)
    li     r1, 10            ; 10 bit cells
bitloop:
    mov    r2, r0
    andi   r2, 1             ; Z = 1 when the bit is 0
    jz     drive
    li     r2, TX            ; bit is 1 -> line high
drive:
    emit   r2                ; schedule at NEXTT + PERIOD, arm the next slot
    shr    r0, 1
    subi   r1, 1
    jnz    bitloop
    jmp    send

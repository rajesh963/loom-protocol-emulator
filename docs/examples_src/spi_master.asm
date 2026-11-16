; SPI controller, mode 0, transmit only, 1 MHz SCK @ 60 MHz.
; SCK=P[10]=uo[2], MOSI=P[11]=uo[3], CS_N=P[12]=uo[4]
.equ SCK,  0x0400
.equ MOSI, 0x0800
.equ CSN,  0x1000
.equ HALF, 0x01E0            ; 30 cycles = half of a 1 MHz SCK period

spi:
    setm  SCK|MOSI|CSN       ; own all three wires
    setp  HALF               ; one event per half SCK period
    li    r2, CSN            ; CS asserted, SCK low, MOSI low
    emit  r2
send:
    pull  r0                 ; next byte from the host
    li    r1, 8
bitloop:
    mov   r3, r0
    andi  r3, 0x80           ; MSB first
    shl   r3, 4              ; -> MOSI bit (0x0800)
    ori   r3, CSN            ; CS held low, SCK low: data setup
    emit  r3
    ori   r3, SCK            ; rising edge: the target samples MOSI
    emit  r3
    shl   r0, 1
    subi  r1, 1
    jnz   bitloop
    li    r3, CSN            ; SCK low after the last bit
    emit  r3
    li    r3, 0              ; release CS
    emit  r3
    jmp   send

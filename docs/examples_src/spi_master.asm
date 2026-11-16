; SPI controller, mode 0, transmit only, 1 MHz SCK @ 33.33 MHz (canonical info.yaml map).
; SCK=P[5]=uio[5], MOSI=P[6]=uio[6], CS_N=P[11]=uo[3]  (MISO would be P[7]=uio[7], input)
.equ SCK,  0x0020            ; P[5]  = uio[5]
.equ MOSI, 0x0040            ; P[6]  = uio[6]
.equ CSN,  0x0800            ; P[11] = uo[3]
.equ DIRB, 0x0060            ; DIR bits 5:6 -> SCK/MOSI are outputs
.equ HALF, 0x010A            ; 16.625 cycles = half of a ~1 MHz SCK period

spi:
    dir   DIRB               ; uio pins need DIR=1 to drive
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
    shr   r3, 1              ; bit 7 -> bit 6 = MOSI
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

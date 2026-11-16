; SPI target, mode 0, on the spare bidirectional pins.
.equ SCK_T,  0x0020          ; P[5]  = uio[5]
.equ MOSI_T, 0x0040          ; P[6]  = uio[6]
.equ CS_T,   0x1000          ; P[12] = ui[4] SPI_CS_IN
.equ MISO_T, 0x2000          ; P[13] = uo[5] SPARE_OUT

target:
    dir    0x00              ; SCK_T/MOSI_T/CS_T are inputs; all uio released
    setm   MISO_T            ; our only output
    setem  CS_T|SCK_T        ; EMASK: watch the chip select and the clock
    setec  SCK_T             ; ECOND: fire when CS is low and SCK is high
    pull   r0                ; byte to return, supplied by the host
    li     r1, 8
bit:
    mov    r3, r0
    andi   r3, 0x80
    jz     zero
    li     r2, MISO_T
    jmp    armed
zero:
    li     r2, 0
armed:
    waitp  0x2000            ; let SCK return low before re-arming (mask=0x20 -> P[5])
    emite  r2                ; drive MISO on the next (CS low & SCK high)
poll:
    csrr   r4, TEQ
    cmpi   r4, 0
    jnz    poll              ; wait until the event has fired
    shl    r0, 1
    subi   r1, 1
    jnz    bit
    push   r0                ; byte received on MOSI would be read with `in` similarly
    jmp    target

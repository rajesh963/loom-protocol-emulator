; I2C controller, 100 kHz, open drain on P[0]=I2C_SCL, P[1]=I2C_SDA (uio[0:1]).
; DIR bit = 1 pulls the line low, DIR bit = 0 releases it to the external pull-up.
.equ SCL,     0x0001
.equ SDA,     0x0002
.equ QUARTER, 0x0535         ; 83.3 cycles = 2.5 us (100 kHz; 0x014D for 400 kHz)

i2c:
    dset  SCL|SDA            ; enable both drivers
    li    r5, 0
    csrw  POUT, r5           ; both drive 0 when enabled
    dclr  SCL|SDA            ; release: both lines idle high
    waitd QUARTER
    dset  SDA                ; START: SDA falls while SCL is high
    waitd QUARTER
    dset  SCL
    waitd QUARTER
    li    r1, 8
sbit:
    mov   r2, r0
    andi  r2, 0x80
    jz    slow
    dclr  SDA                ; data 1: release SDA
    jmp   sclk
slow:
    dset  SDA                ; data 0: pull SDA low
sclk:
    dclr  SCL                ; SCL high: the target samples SDA
    waitd QUARTER
    dset  SCL                ; SCL low
    waitd QUARTER
    shl   r0, 1
    subi  r1, 1
    jnz   sbit
    dclr  SDA                ; ACK slot: release SDA
    dclr  SCL                ; SCL high
    waitd QUARTER
    in    r4                 ; sample SDA: 0 = ACK, 1 = NAK
    andi  r4, SDA
    dset  SCL
    waitd QUARTER
    dset  SDA                ; STOP: SDA rises while SCL is high
    dclr  SCL
    waitd QUARTER
    dclr  SDA
    waitd QUARTER
    jmp   i2c

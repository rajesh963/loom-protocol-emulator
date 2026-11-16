; 1-Wire reset/presence on P[4] = ONEWIRE = uio[4] (0x10), open drain.
.equ DQ,   0x0010
.equ W60,  0xE100            ; 3600 cycles = 60 us
.equ W30,  0x7080            ; 1800 cycles = 30 us
.equ W6,   0x1680            ;  360 cycles =  6 us
.equ W9,   0x21C0            ;  540 cycles =  9 us

ow_reset:
    dset   DQ                ; enable the driver
    clr    DQ                ; pull DQ low
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60               ; 480 us reset pulse
    dclr   DQ                ; release: pull-up restores idle high
    waitd  W30               ; sample the presence pulse at ~30 us
    in     r2
    andi   r2, DQ
    jz     present           ; 0 = a device answered
    jmp    ow_reset

; one read time slot: low 6 us, release, sample at 9 us
present:
    dset   DQ
    clr    DQ
    waitd  W6
    dclr   DQ
    waitd  W9
    in     r3
    andi   r3, DQ            ; r3 = 0 for a 0 bit, 0x10 for a 1 bit
    waitd  W60               ; recovery
    jmp    ow_reset

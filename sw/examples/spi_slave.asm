; ============================================================================
; spi_slave.asm — LOOM SPI target (slave), mode 0, 8-bit full duplex frames
; ============================================================================
;
; PIN MAPPING (target side; uio pins are used so WAITP can poll them — WAITP's
; mask is an 8-bit field and only covers P[7:0])
;   SCK   P[4] = uio[4]   input  (DIR bit 4 stays 0)
;   MOSI  P[5] = uio[5]   input  (DIR bit 5 stays 0)
;   CS_N  P[6] = uio[6]   input  (DIR bit 6 stays 0), active low
;   MISO  P[0] = uio[0]   output (DIR bit 0 = 1)
;
; THREAD: any single thread (demo_all does not run this example concurrently
; with spi_master.asm because the demo uses spi_master instead).
;
; PROTOCOL (mode 0 / CPOL=0 / CPHA=0)
;   Master pulls CS low, then clocks 8 bits MSB-first: MOSI changes on the
;   falling edge and is sampled here on the rising edge; MISO is presented
;   while SCK is low and sampled by the master on the next rising edge.
;   One CS frame carries exactly one byte, both directions.
;   Response rule: MISO byte of a frame = previous frame's MOSI byte + 1
;   (a simple deterministic register-read fixture; initial value 0).
;
; HOW IT REACTS (no CPU involvement in the clock at all)
;   The firmware polls the pins with WAITP:
;       WAITP imm16  fires when (pins_in & imm[15:8]) == imm[7:0]
;   so `waitp 0x4000` = "CS_N low", `waitp 0x1010` = "SCK high" (rising edge),
;   `waitp 0x1000` = "SCK low".  Detection latency is <= 2-4 cycles (the
;   scheduler retries a non-matching WAITP every other cycle), then the data
;   is sampled with IN on the next issue.  One bit costs ~8-12 instructions.
;
; MAX SLAVE CLOCK (measured on the golden model; see model_selftest.py)
;   CS->first MISO setup : <= ~10 cycles  (WAITP detect + 2 instr)
;   per SCK half-period   : detection <= 4 + sample/shift ~8 = ~12 cycles
;   -> a half period of 16 cycles (2 MHz @ 60 MHz) is the practical edge;
;      tested clean at 1 MHz (30-cycle half period), where the CPU uses ~1/3
;      of the issue slots.  Beyond that the poll latency eats the setup margin.
;   A pure event-driven version (edge-triggered EMITE) could sample without
;   polling but cannot feed the CPU a data byte, so polling is the right tool.
;
; SIZE: 32 instructions (< 128, fits the FF fallback build).
; ============================================================================

.equ MISO_PIN,  0x0001          ; P[0] = uio[0] output
.equ SCK_PIN,   0x0010          ; P[4] = uio[4] input
.equ MOSI_PIN,  0x0020          ; P[5] = uio[5] input
.equ CS_PIN,    0x0040          ; P[6] = uio[6] input
.equ WAIT_CS_LOW,  (CS_PIN << 8) | 0        ; 0x4000
.equ WAIT_SCK_HI,  (SCK_PIN << 8) | SCK_PIN ; 0x1010
.equ WAIT_SCK_LO,  (SCK_PIN << 8) | 0       ; 0x1000
.equ WAIT_CS_HI,   (CS_PIN << 8) | CS_PIN   ; 0x4040

; ----------------------------------------------------------------------------
start:
        dset    MISO_PIN        ; MISO is an output; SCK/MOSI/CS stay inputs
        clr     MISO_PIN
        setm    0
        movi    r7, 0           ; previous received byte (response = r7 + 1)
main:
        waitp   WAIT_CS_LOW     ; wait for the master to select us
        mov     r1, r7          ; response byte for this frame = previous + 1
        addi    r1, 1
        shl     r1, 8           ; MSB of the byte -> bit 15 for the ADD/C trick
        clr     MISO_PIN        ; present bit 7 while SCK is low
        add     r1, r1          ; C = bit 7, r1 <<= 1
        jnc     miso_lo
        set     MISO_PIN
miso_lo:
        movi    r4, 8           ; 8 bits
        movi    r5, 0           ; received byte
bit_loop:
        waitp   WAIT_SCK_HI     ; rising edge: sample MOSI
        in      r3
        andi    r3, MOSI_PIN    ; 0x0000 / 0x0020
        shr     r3, 5           ; -> bit 0
        shl     r5, 1
        or      r5, r3
        waitp   WAIT_SCK_LO     ; falling edge: present the next MISO bit
        clr     MISO_PIN
        add     r1, r1          ; C = next data bit
        jnc     next_bit
        set     MISO_PIN
next_bit:
        subi    r4, 1
        jnz     bit_loop
        ; frame done: wait for CS high, deliver the byte, remember it
        waitp   WAIT_CS_HI
        push    r5              ; received byte -> host
        mov     r7, r5          ; next frame's response = this byte + 1
        jmp     main

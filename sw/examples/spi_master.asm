; ============================================================================
; spi_master.asm — LOOM SPI controller, full duplex, modes 0-3, 8-bit bytes
; ============================================================================
;
; PIN MAPPING (default LOOM pinout; all four on the bidirectional uio pins so
; an external SPI device can be wired directly, no level shifting)
;   SCK  P[0] = uio[0]   output  (DIR bit 0)
;   MOSI P[1] = uio[1]   output  (DIR bit 1)
;   MISO P[2] = uio[2]   input   (DIR bit 2 stays 0)
;   CS_N P[3] = uio[3]   output  (DIR bit 3), active low, idle high
;
; THREAD: single thread (demo_all uses t2).  CS/MOSI/SCK are driven with
; SET/CLR on single bits, never `OUT`, so the other threads' pins are safe.
;
; MODES 0-3 — selected at assembly time by .equ MODE (CPOL = MODE/2,
; CPHA = MODE%2).  SCK edges are produced with TOG, so both polarities share
; one code path; the only mode-dependent code is *where* MISO is sampled:
;   CPHA=0: sample in the middle of the phase BEFORE the leading edge
;           (slave changes MISO on the trailing edge -> stable before leading)
;   CPHA=1: sample in the middle of the phase AFTER the leading edge
;           (slave changes MISO on the leading edge)
; MOSI is always placed one half-period before the leading edge (CPHA=0) or
; at the leading edge (CPHA=1), which is correct for both: the master changes
; MOSI on the leading edge, so placing it early is harmless for CPHA=0 and
; exactly right for CPHA=1.
;
; CLOCK RATE — HONEST CYCLE BUDGET
;   SPI is bit-banged: per bit the loop performs 4 WAITD quarter-cells plus a
;   fixed body of instructions.  With one runnable thread every instruction
;   issues in 1 cycle, so
;       SCK period = 4*QUARTER_CYCLES + N_instr  (N_instr ~= 30 for mode 0)
;   Set QUARTER_CYCLES = (target_period - N_instr)/4.  Measured on the model
;   at 33.33 MHz (mode 0, single thread; SCK period includes ~16 instr cycles):
;       QUARTER_CYCLES = 0/1 -> 20.4 cyc/bit = 1.63 MHz (ceiling, tight MISO)
;       QUARTER_CYCLES = 2   -> 24.4 cyc/bit = 1.37 MHz
;       QUARTER_CYCLES = 4   -> 32.4 cyc/bit = 1.03 MHz (shipped default)
;       QUARTER_CYCLES = 8   -> 48.4 cyc/bit = 0.69 MHz (max margin)
;   With all four threads runnable the scheduler gives this thread ~1 issue in
;   2-4 cycles, so the body alone costs 60-120 cycles: SCK drops to ~0.4-0.7 MHz
;   and the duty cycle jitters with the scheduler (not with the data).
;   => bit-banged full-duplex SPI is a ~0.3-1.1 MHz protocol on LOOM at 33.33 MHz.
;   The 10 MHz
;   figure quoted for the pin unit (half period = 3 cycles) is reachable only
;   by the event scheduler / wave player with precomputed patterns, i.e. for
;   MOSI-only streams, and is documented as a stretch goal, not claimed here.
;   The CPU still keeps up with the wire by 30-60 instructions per 60-cycle bit;
;   it is the *issue rate*, not the event queue, that sets the SPI clock.
;
; HOST INTERFACE
;   Host queues one byte per transaction with TX_PUSH(t2, byte).  The firmware
;   polls its RX FIFO every 256 cycles while idle, then asserts CS, shifts the
;   byte out MSB-first while shifting MISO in, deasserts CS, and PUSHes the
;   received byte to its TX FIFO (host reads it with RX_POP(t2)).  A byte
;   costs 8 SCK periods and one CS frame; the drain loop empties the FIFO.
;
; SIZE: 50 instructions (< 128, fits the FF fallback build).
;
; ASSEMBLE + PLACE: image is relocatable (PC-relative jumps only); the host
; loads it at any PMEM base and sets the thread PC there.
; ============================================================================

.equ CLOCK_HZ,        33333333

; ---- select the SPI mode here (0,1,2,3) ------------------------------------
.equ MODE,            0
.equ CPOL,            MODE / 2
.equ CPHA,            MODE % 2

; ---- select the clock: SCK period = 4*QUARTER_CYCLES + ~30 instruction cycles
.equ QUARTER_CYCLES,  4                ; 4 -> ~32 cycles/bit = ~1.03 MHz @ 33.33 MHz
.equ QUARTER_DIV,     QUARTER_CYCLES * 16
.equ HALF_DIV,        2 * QUARTER_CYCLES * 16

; ---- pins -----------------------------------------------------------------
.equ SCK_PIN,         0x0001          ; P[0]  = uio[0]
.equ MOSI_PIN,        0x0002          ; P[1]  = uio[1]
.equ MISO_PIN,        0x0004          ; P[2]  = uio[2] (input)
.equ CS_PIN,          0x0008          ; P[3]  = uio[3]
.equ OUT_DIR,         SCK_PIN | MOSI_PIN | CS_PIN       ; 0x000B
.equ IDLE_DIV,        0x1000          ; 256 cycles between idle FIFO polls

; ----------------------------------------------------------------------------
start:
        dset    OUT_DIR         ; SCK/MOSI/CS are outputs; MISO stays an input
        clr     SCK_PIN         ; SCK = 0 ...
        set     (SCK_PIN * CPOL)  ; ... and 1 if CPOL=1 (idle level)
        set     CS_PIN          ; CS_N idle high
        clr     MOSI_PIN
        setm    0               ; this thread drives no event pins
        movi    r7, CPHA        ; runtime CPHA flag for the sample positions
        movi    r5, MISO_PIN
main:
        waitd   IDLE_DIV        ; idle: ~4 % of issue slots (EVWAIT costs 50 %)
drain:
        csrr    r0, TFIFO
        andi    r0, 0x0F00      ; rx_cnt[3:0] != 0 ?
        jz      main
        pull    r1              ; byte to send
        shl     r1, 8           ; MSB in bit 15; ADD r1,r1 shifts it out via C
        movi    r3, 0           ; MISO result accumulator
        movi    r4, 8           ; 8 bits
        clr     CS_PIN          ; assert CS_N
        waitd   HALF_DIV        ; CS setup + let the target drive MISO bit 7
bit_loop:
        ; ---- place MOSI (MSB of r1); ADD shifts it out and sets C ----
        add     r1, r1          ; r1 <<= 1, C = old bit 15 = the next data bit
        jnc     mosi_low
        set     MOSI_PIN
        jmp     mosi_done
mosi_low:
        clr     MOSI_PIN
mosi_done:
        ; ---- sample point 1 (used when CPHA=0) ----
        cmpi    r7, 0
        jnz     phase1
        in      r6
        and     r6, r5
        shr     r6, 2           ; P[2] -> bit 0
        or      r3, r6
        shl     r3, 1
phase1:
        waitd   QUARTER_DIV
        tog     SCK_PIN         ; leading edge
        waitd   QUARTER_DIV
        ; ---- sample point 2 (used when CPHA=1) ----
        cmpi    r7, 0
        jz      phase2
        in      r6
        and     r6, r5
        shr     r6, 2
        or      r3, r6
        shl     r3, 1
phase2:
        waitd   QUARTER_DIV
        tog     SCK_PIN         ; trailing edge -> SCK back to idle level
        waitd   QUARTER_DIV
        subi    r4, 1
        jnz     bit_loop
        shr     r3, 1           ; undo the last result shift
        set     CS_PIN          ; deassert CS_N
        push    r3              ; received byte -> host
        jmp     drain

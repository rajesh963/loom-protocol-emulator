; ============================================================================
; uart_rx.asm — LOOM UART receiver, 8N1, configurable baud, 33.33 MHz
; ============================================================================
;
; PIN MAPPING
;   RX  P[11] = ui_in[3]    input, idle high; ui pins cannot be driven.
;
; THREAD: any single thread (demo_all uses t1).
;
; HOW THE BYTE IS RECEIVED
;   There is no absolute-time instruction form that takes a register (WAITU /
;   INAT take an immediate), so the receiver is *relative-timed*: it advances
;   its bit clock with WAITD, whose 12.4 fractional accumulator makes N cells
;   exactly N*BAUD_CELL cycles.  Only round-robin issue delay adds jitter
;   (0..3 cycles per instruction) - below 1 % of a bit and far inside the
;   mid-bit sampling margin (see table).
;
;   Slow baud rates hit the same 16-bit WAITD limit as the TX program, so a bit
;   is CELLS * BAUD_CELL with CELLS = ceil(BAUD_DIV/65535); the mid-bit sample
;   is HALF_CELL_DIV = BAUD_DIV/(2*HALF_CELLS) with HALF_CELLS = ceil(CELLS/2).
;
;   start detection : gentle poll (one 16-cycle WAITD + IN per attempt) so an
;                     idle receiver costs ~1/4 of the issue slots of a busy
;                     thread instead of 1/2 (a retrying WAITP polls every 2).
;   sampling        : mid-bit for start (glitch reject), then mid-bit for each
;                     data cell; stop bit is checked (framing error -> drop).
;   Jitter budget   : poll latency <= ~22 cycles after the falling edge, then
;                     HALF to mid-start.  Data cell i is sampled at
;                     half + i*bit + jitter(i) and the cell window is
;                     [i*bit, i*bit+bit], so the margin stays > 150 cycles
;                     even at 115200 with all four threads runnable.
;
; HOST INTERFACE
;   Every decoded byte is PUSHed to thread t1's TX FIFO; host reads with
;   RX_POP(t1).  No doorbell is needed (the wire is the input).
;
; CYCLE BUDGET
;   per byte on the wire      : 10 cells                        = 2894 cyc
;   CPU per byte              : ~10 (detect) + 8*(2+CELLS+6) word-issues
;   idle CPU cost             : 1 poll / 32 cycles = ~3 % of issue slots
;   max sustained rate        : 115200 baud full duplex (~11520 B/s);
;                               host TX FIFO depth 8 = 0.67 ms of elasticity
;
; SIZE: 34 instructions (< 128, fits the FF fallback build).
; Image is position independent (PC-relative jumps only).
; ============================================================================

.equ CLOCK_HZ,  33333333
.equ BAUD,      115200
.equ BAUD_DIV,  (CLOCK_HZ * 16 + BAUD / 2) / BAUD      ; 115200 -> 0x1216
.equ CELLS,     (BAUD_DIV + 0xFFFF - 1) / 0xFFFF        ; cells per bit
.equ BAUD_CELL, (BAUD_DIV + CELLS / 2) / CELLS
.equ HALF_CELLS, (CELLS + 1) / 2                        ; cells in a half bit
.equ HALF_CELL_DIV, (BAUD_DIV + HALF_CELLS) / (2 * HALF_CELLS)

.equ RX_PIN,    0x0800          ; P[11] = ui_in[3]
.equ POLL_DIV,  0x0200          ; 32.0 cycles between idle polls

; ----------------------------------------------------------------------------
start:
        setm    0               ; this thread emits no events; never touch pins
        movi    r6, RX_PIN
poll:
        waitd   POLL_DIV        ; gentle poll (do not starve other threads)
        in      r0
        and     r0, r6          ; RX high -> keep waiting
        jnz     poll
        ; falling edge seen within the last POLL_DIV cycles -> go to mid-start
        waitd   HALF_CELL_DIV
        in      r0
        and     r0, r6
        jnz     poll            ; false start (line already back high) -> resync
        movi    r5, 0           ; r5 = assembled byte
        movi    r4, 8           ; 8 data cells
rx_bit:
        movi    r3, CELLS       ; advance one whole bit
cell_wait:
        waitd   BAUD_CELL
        subi    r3, 1
        jnz     cell_wait
        in      r3
        and     r3, r6          ; keep RX bit only: 0x0000 / 0x0800
        shr     r3, 4           ; -> 0x0000 / 0x0080 (the new MSB)
        shr     r5, 1           ; first-received bit ends at bit 0
        or      r5, r3
        subi    r4, 1
        jnz     rx_bit
        movi    r3, CELLS       ; stop cell
stop_wait:
        waitd   BAUD_CELL
        subi    r3, 1
        jnz     stop_wait
        in      r0
        and     r0, r6
        jz      poll            ; framing error (stop not high) -> drop byte
        push    r5              ; deliver to host (thread TX FIFO)
        jmp     poll

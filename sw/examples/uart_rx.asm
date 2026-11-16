; ============================================================================
; uart_rx.asm — LOOM UART receiver, 8N1, 115200 baud @ 60 MHz
; ============================================================================
;
; PIN MAPPING
;   RX  P[11] = ui_in[3]    input, idle high; ui pins cannot be driven.
;
; THREAD: any single thread (demo_all uses t1).
;
; HOW THE BYTE IS RECEIVED
;   There is no absolute-time instruction form (WAITU/INAT take an immediate),
;   so the receiver is *relative-timed*: it uses the 12.4 fractional WAITD
;   accumulator to advance exactly 520 + 13/16 cycles per data cell.  The
;   fractional carry keeps the cell clock drift-free; only the round-robin
;   issue delay of a stalled thread (0..3 cycles/instruction) adds jitter,
;   which is < 0.6 % of a bit and does NOT accumulate past mid-bit (worst-case
;   frame drift < 90 cycles vs the 260-cycle sampling margin, see table).
;
;   start detection : gentle poll (one 16-cycle WAITD + IN per attempt) so an
;                     idle receiver costs ~1/4 of the issue slots of a busy
;                     thread instead of 1/2 (a retrying WAITP polls every 2).
;   sampling        : mid-bit for start, then mid-bit for each data cell;
;                     start bit is re-validated (glitch reject); stop bit is
;                     checked (framing error -> byte dropped).
;
;   Poll latency:  <= ~22 cycles after the falling edge; then HALF_DIV=260.4
;   to mid-start.  Data cell i is sampled at 260 + i*520.8 + jitter(i), and
;   the cell window is [520*i, 520*i+520] -> margin > 170 cycles for i<=8.
;
; HOST INTERFACE
;   Every decoded byte is PUSHed to thread t1's TX FIFO, so the host reads it
;   with RX_POP(t1).  No doorbell is needed (the wire is the input).
;
; CYCLE BUDGET
;   per byte on the wire      : 10 cells                       = 5208 cyc
;   CPU per byte              : 2 (detect) + 8*8 + ~12 = 78 word-issues
;   idle CPU cost             : 1 poll / 16 cycles = ~4-5 % of issue slots
;   max sustained rate        : 115200 baud full duplex (1 byte / 5208 cyc)
;                               ~ 11520 bytes/s theoretical, host FIFO depth
;                               8 gives 0.7 ms of elastic buffering.
;
; SIZE: 30 instructions (< 128, fits the FF fallback build).
;
; ASSEMBLY (same command as uart_tx.asm; image is position independent)
; ============================================================================

.equ CLOCK_HZ,  60000000
.equ BAUD,      115200
.equ BAUD_DIV,  (CLOCK_HZ * 16 + BAUD / 2) / BAUD      ; 0x208D
.equ HALF_DIV,  (BAUD_DIV + 1) / 2                    ; 0x1047 = 260.4375 cyc

.equ RX_PIN,    0x0800          ; P[11] = ui_in[3]
.equ POLL_DIV,  0x0100          ; 16.0 cycles between idle polls

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
        waitd   HALF_DIV
        in      r0
        and     r0, r6
        jnz     poll            ; false start (line already back high) -> resync
        movi    r5, 0           ; r5 = assembled byte
        movi    r4, 8           ; 8 data cells
rx_bit:
        waitd   BAUD_DIV
        in      r3
        and     r3, r6          ; keep RX bit only
        shr     r3, 11          ; P[11] -> bit 0
        or      r5, r3
        shl     r5, 1           ; open a slot for the next bit
        subi    r4, 1
        jnz     rx_bit
        shr     r5, 1           ; undo the last slot
        waitd   BAUD_DIV        ; stop cell
        in      r0
        and     r0, r6
        jz      poll            ; framing error (stop not high) -> drop byte
        push    r5              ; deliver to host (thread TX FIFO)
        jmp     poll

; ============================================================================
; uart_tx.asm — LOOM UART transmitter, 8N1, configurable baud, drift-free cells
; ============================================================================
;
; PIN MAPPING (default LOOM pinout)
;   TX  P[10] = uo_out[2]   output, idle high, 1 = mark
;       (ui/uo pins need no DIR setup; uo_out is always driven)
;
; THREAD: any single thread (demo_all uses t0).  Uses no uio pins.
;
; TIMING AND THE 16-BIT PERIOD LIMIT
;   One bit = CLOCK_HZ / BAUD cycles.  The hardware period register is 12.4
;   fixed point, so one cell is at most 0xFFFF/16 = 4095.94 cycles.  Fast baud
;   rates fit in one cell (115200 -> BAUD_DIV 0x208D = 520.81 cycles); for slow
;   rates the bit is built from CELLS identical event cells:
;
;       CELLS     = ceil(BAUD_DIV / 65535)
;       BAUD_CELL = round(BAUD_DIV / CELLS)
;       bit       = CELLS * BAUD_CELL   (exact for 4800/9600/19200/38400)
;
;   Every frame cell is an event-scheduler entry; the fractional carry in
;   `setp` makes the cell stream drift-free regardless of instruction timing,
;   scheduler pressure, FIFO stalls or other threads.
;
;   115200 @ 60 MHz: BAUD_DIV = 8333 = 0x208D -> 520.8125 cycles/bit
;                    (boundary error: -40 ppm, crystal-class)
;   9600   @ 60 MHz: BAUD_DIV = 100000 -> CELLS=2 x 50000 (6250 cycles/bit)
;
; HOST INTERFACE (byte stream, see host/demos/demo_all.py)
;   Host pushes data bytes with TX_PUSH(t0, byte); no doorbell is needed.
;   The thread polls its RX-FIFO occupancy (TFIFO rx_cnt[11:8]) once every 256
;   cycles while idle, then drains the whole FIFO before sleeping again.
;   No reply is queued in the thread TX FIFO; the wire is the output.
;
; FRAME: start(0) d0..d7 stop(1), LSB first.  Back-to-back frames are legal:
;   the next start is scheduled one bit period after the stop cell.  After an
;   idle gap the behind-schedule clamp restarts the chain at TMR+1 (spec §3.1).
;
; CYCLE BUDGET (one byte, 4-thread worst case)
;   cell emit rate           : 1 event / BAUD_CELL cycles (queue depth 4 ->
;                              the thread stalls on EMIT, which is correct)
;   per byte on the wire     : 10 bits                        = 5208 cyc
;   CPU instructions / byte  : ~50 word-issues (CELLS=1); a small fraction of
;                              the 5208-cycle byte time, so this thread can
;                              share the chip with the other examples.
;   host->wire latency       : doorbell->start edge <= 3 cycles after `pull`
;
; SIZE: 30 instructions (< 128, fits the FF fallback build).
; Image is relocatable (PC-relative jumps only): load at any PMEM base, set_pc.
; ============================================================================

.equ CLOCK_HZ,  60000000
.equ BAUD,      115200          ; any rate; CELLS grows for slow rates

; 12.4 fixed-point bit period, rounded to the nearest 1/16 cycle.
.equ BAUD_DIV,  (CLOCK_HZ * 16 + BAUD / 2) / BAUD
; number of event cells per bit: 1 unless the 12.4 value would overflow 16 bits
.equ CELLS,     (BAUD_DIV + 0xFFFF - 1) / 0xFFFF
.equ BAUD_CELL, (BAUD_DIV + CELLS / 2) / CELLS

.equ TX_PIN,    0x0400          ; P[10] = uo_out[2]
.equ IDLE_DIV,  0x1000          ; 256 cycles between idle FIFO polls

; ----------------------------------------------------------------------------
start:
        set     TX_PIN          ; idle line = mark (1), OR-only: other pins safe
        setm    TX_PIN          ; this thread's events drive P[10] only
        setp    BAUD_CELL       ; hardware cell clock, 12.4 fixed point
main:
        waitd   IDLE_DIV        ; sleep 256 cycles between polls: an idle thread
                                ; costs ~4 % of the issue slots (an EVWAIT retry
                                ; costs 50 %, which would starve other protocols)
        csrr    r0, TFIFO
        andi    r0, 0x0F00      ; rx_cnt[3:0] != 0 ?
        jz      main
drain:
        csrr    r0, TFIFO
        andi    r0, 0x0F00      ; rx_cnt[3:0] != 0 ?
        jz      main
        pull    r1              ; r1 = data byte (host TX_PUSH)
        ; ---- cell 0: start bit = 0 ----
        movi    r2, 0
        movi    r3, CELLS
start_cells:
        emit    r2
        subi    r3, 1
        jnz     start_cells
        ; ---- 8 data cells, LSB first ----
        movi    r4, 8
        movi    r5, TX_PIN
bit_loop:
        mov     r2, r1
        andi    r2, 1           ; isolate LSB
        shl     r2, 10          ; 0/1 -> 0x0000/0x0400 pin vector value
        movi    r3, CELLS
bit_cells:
        emit    r2              ; one data cell (value masked by TMASK)
        subi    r3, 1
        jnz     bit_cells
        shr     r1, 1
        subi    r4, 1
        jnz     bit_loop
        ; ---- stop bit = 1 ----
        movi    r2, TX_PIN
        movi    r3, CELLS
stop_cells:
        emit    r2
        subi    r3, 1
        jnz     stop_cells
        jmp     drain

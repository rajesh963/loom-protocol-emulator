; ============================================================================
; uart_loopback.asm — LOOM UART echo / wire bridge, 8N1, 115200 baud @ 33.33 MHz
; ============================================================================
;
; PIN MAPPING
;   RX  P[11] = ui_in[3]   input,  idle high
;   TX  P[10] = uo_out[2]  output, idle high (1 = mark)
;
; THREAD: one thread.  This is the "loopback" member of the UART example set:
; it receives a byte on RX and re-transmits it on TX (and also pushes it to the
; host TX FIFO so a host can read the echo with RX_POP).
;
; HALF-DUPLEX NOTE (honest limitation)
;   One thread cannot track a receive bit-clock and an independent transmit
;   bit-clock at the same time with plain WAITD chains.  For true full duplex,
;   run `uart_tx.asm` on one thread and `uart_rx.asm` on another: the event
;   scheduler keeps the TX bit clock exact while the RX thread polls/waits.
;   `host/demos/demo_all.py` does exactly that (t0 = tx, t1 = rx) and moves
;   bytes in both directions at once.  This program is the single-thread echo
;   used when a loopback jumper (uo[2] -> ui[3]) or a host UART is attached.
;
; TIMING
;   RX: gentle 16-cycle poll for the start edge, WAITD mid-bit sampling
;       (same arithmetic as uart_rx.asm, drift < 1/16 cycle per cell from the
;       12.4 fractional accumulator).
;   TX: 10 event-scheduler cells at BAUD_DIV = 0x1216 (289.375 cycles), so
;       the echo's bit period is exact and independent of instruction timing.
;   Echo turnaround: start bit of the echo is placed one bit period after the
;       stop cell was enqueued, i.e. ~1 bit after the received char completes.
;   THROUGHPUT: this is a half-duplex echo - one character time to receive, then
;       one character time to transmit -> ~50 % of the raw line rate.  Send
;       bytes with an inter-byte gap of >= 2 character times (the harness does
;       2*10*521 cycles) or the next start bit arrives while the echo's event
;       chain is still draining.  For true full duplex use uart_tx.asm +
;       uart_rx.asm on two threads (demo_all) - each direction gets 100 %.
;
; SIZE: 58 instructions (< 128, fits the FF fallback build).
; ============================================================================

.equ CLOCK_HZ,  33333333
.equ BAUD,      115200
.equ BAUD_DIV,  (CLOCK_HZ * 16 + BAUD / 2) / BAUD      ; 0x1216
.equ HALF_DIV,  (BAUD_DIV + 1) / 2                    ; 0x090B
.equ POLL_DIV,  0x0100                                ; 16.0 cycles

.equ RX_PIN,    0x0800          ; P[11] = ui_in[3]
.equ TX_PIN,    0x0400          ; P[10] = uo_out[2]

; ----------------------------------------------------------------------------
start:
        set     TX_PIN          ; idle line = mark
        setm    TX_PIN          ; the event scheduler owns P[10] only
        setp    BAUD_DIV
        movi    r6, RX_PIN
poll:
        waitd   POLL_DIV
        in      r0
        and     r0, r6
        jnz     poll
        waitd   HALF_DIV        ; mid-start
        in      r0
        and     r0, r6
        jnz     poll            ; false start
        movi    r5, 0
        movi    r4, 8
rx_bit:
        waitd   BAUD_DIV
        in      r3
        and     r3, r6
        shr     r3, 4           ; RX bit -> 0x0080 (the new MSB)
        shr     r5, 1
        or      r5, r3
        subi    r4, 1
        jnz     rx_bit
        waitd   BAUD_DIV        ; stop cell
        in      r0
        and     r0, r6
        jz      poll            ; framing error -> drop
        push    r5              ; host can observe the echo via RX_POP
        ; ---- echo: transmit the received byte back on TX ----
        movi    r0, 0
        emit    r0              ; start bit
        movi    r4, 8
        mov     r1, r5
tx_bit:
        mov     r2, r1
        andi    r2, 1
        shl     r2, 10          ; 0/1 -> 0x0000/0x0400
        emit    r2
        shr     r1, 1
        subi    r4, 1
        jnz     tx_bit
        movi    r2, TX_PIN
        emit    r2              ; stop bit = 1
        jmp     poll

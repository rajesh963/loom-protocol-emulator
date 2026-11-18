; ============================================================================
; midi_tx.asm — LOOM as a MIDI 1.0 transmitter (31250 baud 8N1)
; ============================================================================
;
; PIN MAPPING (a real board needs a MIDI current-loop / DIN-5 output stage;
; the pin is the UART TX signal: idle high, start bit low, 8N1, 31250 baud)
;   MIDI_TX  P[10] = uo[2]   output, 31250 baud 8N1
;
; WHAT IT TRANSMITS
;   The host pushes raw MIDI bytes into the thread's RX FIFO; the firmware
;   serialises them back-to-back as 8N1 frames: start(0), 8 data bits LSB
;   first, stop(1).  A host job is typically a whole short message
;   (e.g. Note On 0x90 0x3C 0x64), which the far end decodes with the
;   independent `host/midi.py` receiver: start/stop framing, status/data byte
;   classification and message assembly (Note On/Off, controllers, ...).
;   Bytes are chained on the event grid, so consecutive frames are
;   back-to-back with exactly one stop bit between them.
;
; TIMING — the event scheduler, not instruction counting
;   MIDI is 31250 baud = 32 us/bit.  At the shipped 33.33 MHz one bit is
;   1066.6667 cycles; `setp 0x42AB` (17067 in 12.4 fixed point =
;   1066.6875 cycles) gives 31250.6 baud (+0.002 %), and the 12.4 carry keeps
;   a 10-bit frame (10666.875 cycles) drift-free.  Each bit cell is one
;   `emit`; per-bit work is ~9 instructions against 1066 cycles, so the CPU
;   is idle >99 % of the time and the timing is entirely the scheduler's.
;
; HOST INTERFACE
;   Push bytes with TX_PUSH(thread, byte); no doorbell is needed (the thread
;   blocks on `pull`).  The TX FIFO is drained one byte per 10-bit frame.
;
; SIZE: 37 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ CLOCK_HZ,  33333333
.equ MIDI_BAUD, 31250
.equ BITPER,    (CLOCK_HZ * 16 + MIDI_BAUD / 2) / MIDI_BAUD  ; 17067 = 0x42AB
.equ TX_PIN,    0x0400          ; P[10] = uo[2]

; ----------------------------------------------------------------------------
start:
        setm    TX_PIN          ; this thread only drives the MIDI TX pin
        setp    BITPER          ; 1066.6875 cycles/bit -> 31250.6 baud
        set     TX_PIN          ; 8N1 idle is high
main:
        pull    r0              ; block until the host pushes a byte
        shl     r0, 1           ; b0 = start bit (0), b1..b8 = data LSB first
        ori     r0, 0x0200      ; b9 = stop bit (1)
        movi    r1, 10          ; 10 bit cells per 8N1 frame
bit_loop:
        mov     r2, r0
        andi    r2, 1           ; Z = 1 when the bit is 0
        jz      drive
        movi    r2, TX_PIN
drive:
        emit    r2              ; one bit cell: NEXTT + BITPER
        shr     r0, 1
        subi    r1, 1
        jnz     bit_loop
        jmp     main

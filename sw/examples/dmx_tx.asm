; ============================================================================
; dmx_tx.asm — LOOM as a DMX512 transmitter (250 kbaud, 512-slot frame)
; ============================================================================
;
; PIN MAPPING (a real board needs an RS-485 transceiver; external parts are
; allowed by the competition FAQ)
;   DMX  P[10] = uo[2]   output: UART-style 8N2 at 250 kbaud, break = low,
;                        mark-after-break = high.  One differential pair.
;
; WHAT IT TRANSMITS (one full DMX512 frame, single shot)
;   1. BREAK: line held low for ~120 us (spec: >= 88 us).
;   2. MAB (mark after break): line held high for ~12 us (spec: >= 8 us).
;   3. Start-code slot: 0x00, on the wire as 11 bit cells
;      (start bit 0, 8 data bits LSB first, two stop bits 1).
;   4. SLOTS data slots, one 11-bit cell group each, byte pulled from the host
;      RX FIFO (0x00..0xFF).  Spec default SLOTS = 512 (a full universe).
;   The thread halts after the last stop bit; the line idles high.
;
; HOST INTERFACE
;   The host pushes the SLOTS data bytes into the thread's RX FIFO (one byte
;   per slot, `host/dmx.py`'s app helper does the flow control).  The firmware
;   pulls one byte per slot; because a slot is 11 x 133.3 = 1466 cycles, a
;   33 MHz host has >1.4 kcycles per byte to keep the 8-byte FIFO ahead.
;
; TIMING — the event scheduler, not instruction counting
;   250 kbaud at 33.33 MHz = 133.3333 cycles/bit; `setp 0x0855` (2133 in 12.4
;   fixed point = 133.3125 cycles) gives **250.03 kbaud** (+0.015 %, well
;   inside the DMX +/- 1 % window).  Every bit cell is one `emit`; the 12.4
;   carry keeps an 11-bit slot (1466.4 cycles) drift-free across 513 slots.
;   The first slot's start bit is scheduled by the behind-schedule clamp of
;   `emit` (target 0 + PERIOD is in the past after the break/MAB waits), which
;   is exactly what starts the chain one cycle after the MAB.
;   Per-bit work is ~9 instructions against 133.3 cycles: no issue-rate risk.
;
; CYCLE BUDGET
;   break 4000 + MAB 400 + 513 slots * 1466.4 = ~756 kcycles = 22.7 ms.
;
; SIZE: 37 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ CLOCK_HZ,   33333333
.equ DMX_BAUD,   250000
.equ BITPER,     (CLOCK_HZ * 16 + DMX_BAUD / 2) / DMX_BAUD  ; 2133 = 0x0855
.equ TX_PIN,     0x0400          ; P[10] = uo[2]
.equ BREAK_DIV,  0xFA00          ; 4000 cycles = 120 us low (>= 88 us)
.equ MAB_DIV,    0x1900          ; 400 cycles = 12 us high (>= 8 us)
.equ SLOTS,      512             ; DMX512 universe size

; ----------------------------------------------------------------------------
start:
        setm    TX_PIN
        setp    BITPER          ; 133.3125 cycles/bit -> 250.03 kbaud
        clr     TX_PIN          ; BREAK: line low
        waitd   BREAK_DIV
        set     TX_PIN          ; MAB: line high
        waitd   MAB_DIV
        ; ---- start-code slot = 0x00 ------------------------------------
        movi    r4, 0
        csrr    r7, TPC
        movi    r0, sub_slot - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        ; ---- SLOTS data slots, one host byte per slot -------------------
        movi    r3, SLOTS
slot_loop:
        pull    r4              ; block until the host supplies the next byte
        csrr    r7, TPC
        movi    r0, sub_slot - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        subi    r3, 1
        jnz     slot_loop
        halt

; ============================================================================
; sub_slot — send r4 as one 11-bit DMX slot cell group (start, 8 data, 2 stop)
;   in : r4 = byte, r7 = return address
; ============================================================================
sub_slot:
        mov     r0, r4
        shl     r0, 1           ; data in b1..b8 (LSB first), start bit b0 = 0
        ori     r0, 0x0600      ; two stop bits (b9, b10)
        movi    r1, 11
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
        csrw    TPC, r7

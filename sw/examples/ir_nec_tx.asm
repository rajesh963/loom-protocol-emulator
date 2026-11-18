; ============================================================================
; ir_nec_tx.asm — LOOM as a NEC infrared remote-control transmitter (wave*
;                 player style: the event scheduler synthesises the 38 kHz
;                 carrier and the pulse-distance envelope)
; ============================================================================
;
; PIN MAPPING (a real board needs an IR LED + driver transistor on the pin)
;   IR  P[10] = uo[2]   carrier-gated output: high during a mark (LED on),
;                       low during a space (LED off)
;
; WHAT IT TRANSMITS (one NEC frame, single shot, then idle low)
;   9 ms carrier MARK  (the NEC "lead" / AGC burst)
;   4.5 ms SPACE
;   32 bits (LSB first per byte):
;       [addr, ~addr, cmd, ~cmd] = [0x04, 0xFB, 0x08, 0xF7] by default
;       each bit: 562.5 us carrier MARK + space
;                 562.5 us for a 0, 1687.5 us for a 1  (pulse-distance coding)
;   562.5 us final carrier MARK (end-of-frame mark), then idle low.
;   The 38 kHz carrier is synthesised by alternating high/low `emit`s one
;   half-period apart: `setp 0x1B6A` = 7018/16 = 438.625 cycles per half
;   period -> 38.00 kHz (-0.01 %; IR receivers accept +-10 %).  Marks are runs of
;   high/low half cells; spaces are runs of low events whose periods are split
;   into <= 4095.9-cycle chunks (the 12.4 period register limit), so the
;   envelope is also generated entirely by the scheduler, never by
;   instruction counting.
;
; HOST INTERFACE
;   The host preloads DMEM[0] and DMEM[1] with the 32 payload bits packed 16
;   per word, earliest bit in bit 0 (address byte first, LSB first, then
;   ~address, command, ~command).  host/ir_nec.py builds those words from
;   (address, command) and independently decodes the mark/space envelope and
;   the 38 kHz carrier from the pin trace.
;
; TIMING (33.33 MHz design point)
;   1 us = 33.333 cycles; carrier half period 438.625 cycles (38.00 kHz).
;   mark/space counts are computed from those constants:
;     mark 562.5 us = 22 full carrier periods -> 43*438.625 = 18860.9 cyc
;                     (565.8 us, +0.6 % inside the NEC marking window)
;     lead 9 ms     = 342 periods -> 683*438.625 = 299580.9 cyc (8.987 ms)
;     0-bit space 18311.375 cyc + 438.625 startup = 18750 cyc = 562.5 us
;     1-bit space 55811.375 cyc + 438.625 startup = 56250 cyc = 1687.5 us
;     lead space  149561.375 cyc + 438.625 = 150000 cyc = 4.5 ms
;   Event-chain sequencing makes every mark and space exactly the scheduled
;   number of cycles; per-half-cell work is 4 instructions against 877.25
;   cycles, so the CPU is idle >99 % of the carrier and always keeps the
;   4-deep queue fed.
;
; CYCLE BUDGET
;   ~2.25 Mcycles = 67.5 ms per frame (the NEC frame length itself, not the
;   CPU); the thread is idle >85 % of it.
;
; SIZE: 54 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ CLOCK_HZ,   33333333
.equ IR_PIN,     0x0400          ; P[10] = uo[2]
.equ HALF,       0x1B6A          ; 7018/16 = 438.625 cycles -> 38.00 kHz carrier
.equ MARK_CNT,   22              ; 22 carrier periods = 565.8 us mark (+0.6 %)
.equ LEAD_CNT,   342             ; 342 periods = 8.987 ms lead mark (-0.14 %)
.equ CHUNK,      0xFFFF          ; 4095.9375-cycle max event period
.equ SP0_REM,    0x787A          ; 1927.625 cycles: 0-bit space remainder
.equ SP1_REM,    0xA043          ; 2564.1875 cycles: 1-bit space remainder
.equ LEAD_REM,   0x83BA          ; 2107.625 cycles: 4.5 ms lead-space remainder

; ----------------------------------------------------------------------------
start:
        setm    IR_PIN
        clr     IR_PIN          ; idle: LED off
        movi    r1, IR_PIN      ; carrier high value
        movi    r6, 0           ; carrier low value
        ld      r0, 0           ; r0 = bits 0..15 (address, ~address)
        movi    r3, 1           ; DMEM word pointer for the second half
        movi    r2, 16          ; bits left in r0
        movi    r7, 32          ; 32 payload bits
        ; ---- 9 ms lead mark -------------------------------------------
        setp    HALF
        movi    r4, LEAD_CNT
lead_mark:
        emit    r1              ; carrier high half cell
        emit    r6              ; carrier low half cell
        subi    r4, 1
        jnz     lead_mark
        ; ---- 4.5 ms lead space ----------------------------------------
        setp    CHUNK
        rpt     35
        emit    r6
        setp    LEAD_REM
        emit    r6
        ; ---- 32 pulse-distance data bits ------------------------------
bit_loop:
        setp    HALF
        movi    r4, MARK_CNT
bit_mark:
        emit    r1
        emit    r6
        subi    r4, 1
        jnz     bit_mark
        mov     r5, r0
        andi    r5, 1           ; Z = 1 for a 0 bit
        jz      space0
        ; ---- 1 bit: 1687.5 us space -----------------------------------
        setp    CHUNK
        rpt     12
        emit    r6
        setp    SP1_REM
        emit    r6
        jmp     bit_next
        ; ---- 0 bit: 562.5 us space ------------------------------------
space0:
        setp    CHUNK
        rpt     3
        emit    r6
        setp    SP0_REM
        emit    r6
bit_next:
        shr     r0, 1
        subi    r2, 1
        jnz     have_bits
        ldi     r0, r3          ; load the second 16-bit payload word
        addi    r3, 1
        movi    r2, 16
have_bits:
        subi    r7, 1
        jnz     bit_loop
        ; ---- final 562.5 us mark --------------------------------------
        setp    HALF
        movi    r4, MARK_CNT
final_mark:
        emit    r1
        emit    r6
        subi    r4, 1
        jnz     final_mark
        halt

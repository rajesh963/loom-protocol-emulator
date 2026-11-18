; ============================================================================
; cec_tx.asm — LOOM as an HDMI-CEC initiator (600 us/bit, one short frame)
; ============================================================================
;
; PIN MAPPING (CEC is a single-wire, open-drain 3.3 V bus; a real board needs
; the standard CEC pull-up and the HDMI connector — external parts allowed)
;   CEC  P[0] = uio[0]   OPEN-DRAIN: DIR=1 pulls the line low, DIR=0 releases it
;                        to the external pull-up (the chip only ever drives 0).
;
; WHAT IT TRANSMITS (one CEC frame, single shot)
;   START bit:   3.7 ms low, 0.8 ms high
;   header block: 8 data bits (initiator<<4 | destination, LSB first)
;                 + EOM bit + ACK bit
;   opcode block: 8 data bits (opcode, LSB first) + EOM bit + ACK bit
;   Each data bit: 0 = 1.5 ms low + 0.9 ms high, 1 = 0.6 ms low + 1.8 ms high
;   (600 us per bit cell: 2.4 ms per data bit, start bit 4.5 ms).
;   Default frame: initiator 0x4 (Playback 1) -> destination 0x0 (TV),
;   opcode 0x04 (Image View On); header EOM=0 (more blocks follow), opcode
;   EOM=1 (last block); both ACK slots are left NACK (1) — this is an
;   initiator-only demonstration, no follower is modelled, so the ACK slots
;   are recessive.  Total = 1 + 10 + 10 = 21 bit cells, ~52.5 ms.
;
; TIMING — 12.4 `waitd` phase generator, no instruction counting
;   At the shipped 33.33 MHz: 1 ms = 33333.33 cycles.  The phases are split
;   into 4000-cycle chunks (0xFA00) plus one remainder:
;     start: 3.7 ms = 30*4000 + 3333 = 123333 cyc; 0.8 ms = 6*4000 + 2667
;     data 0: 1.5 ms = 12*4000 + 2000;          0.9 ms = 7*4000 + 2000
;     data 1: 0.6 ms = 5*4000;                  1.8 ms = 15*4000
;   Every phase is exact to the cycle (the chunk loop adds 2 cycles/4000,
;   i.e. <0.06 %).  Open-drain driving: `dset PIN` pulls low, `dclr PIN`
;   releases the line; the external pull-up makes it high.
;
; SIZE: 91 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ CLOCK_HZ,  33333333
.equ CEC_PIN,   0x0001           ; P[0] = uio[0], open-drain
.equ CHUNK,     0xFA00           ; 4000 cycles per waitd call
.equ REM_START_LOW, 0xD050       ; 3333 cycles (3.7 ms remainder)
.equ REM_START_HIGH, 0xA6B0      ; 2667 cycles (0.8 ms remainder)
.equ REM_SHORT, 0x7D00           ; 2000 cycles (0.6/0.9 ms short remainder)
.equ HEADER,    0x40             ; initiator 0x4 << 4 | destination 0x0
.equ OPCODE,    0x04             ; Image View On

; ----------------------------------------------------------------------------
start:
        setm    0
        clr     CEC_PIN         ; open-drain value is always 0 (driven low)
        dclr    CEC_PIN         ; bus idle: released, external pull-up = high
        ; ---- START bit: 3.7 ms low, 0.8 ms high ------------------------
        dset    CEC_PIN
        movi    r2, 30
st_lo:
        waitd   CHUNK
        subi    r2, 1
        jnz     st_lo
        waitd   REM_START_LOW
        dclr    CEC_PIN
        movi    r2, 6
st_hi:
        waitd   CHUNK
        subi    r2, 1
        jnz     st_hi
        waitd   REM_START_HIGH
        ; ---- header block (8 data + EOM + ACK) -------------------------
        movi    r4, HEADER
        csrr    r7, TPC
        movi    r1, sub_byte - . + 1
        add     r1, r7
        addi    r7, 5
        csrw    TPC, r1
        movi    r0, 0           ; header EOM = 0 (more blocks follow)
        csrr    r6, TPC
        movi    r1, sub_bit - . + 1
        add     r1, r6
        addi    r6, 5
        csrw    TPC, r1
        movi    r0, 1           ; header ACK slot: NACK (recessive)
        csrr    r6, TPC
        movi    r1, sub_bit - . + 1
        add     r1, r6
        addi    r6, 5
        csrw    TPC, r1
        ; ---- opcode block (8 data + EOM + ACK) -------------------------
        movi    r4, OPCODE
        csrr    r7, TPC
        movi    r1, sub_byte - . + 1
        add     r1, r7
        addi    r7, 5
        csrw    TPC, r1
        movi    r0, 1           ; opcode EOM = 1 (last block)
        csrr    r6, TPC
        movi    r1, sub_bit - . + 1
        add     r1, r6
        addi    r6, 5
        csrw    TPC, r1
        movi    r0, 1           ; opcode ACK slot: NACK
        csrr    r6, TPC
        movi    r1, sub_bit - . + 1
        add     r1, r6
        addi    r6, 5
        csrw    TPC, r1
        halt

; ============================================================================
; sub_byte — send r4 as 8 CEC data bits, LSB first (r6 = sub_bit return,
;            r7 = caller return)
; ============================================================================
sub_byte:
        movi    r5, 8
sb_loop:
        mov     r0, r4
        andi    r0, 1
        csrr    r6, TPC
        movi    r1, sub_bit - . + 1
        add     r1, r6
        addi    r6, 5
        csrw    TPC, r1
        shr     r4, 1
        subi    r5, 1
        jnz     sb_loop
        csrw    TPC, r7

; ============================================================================
; sub_bit — send one CEC data bit (r0 = 0/1); open-drain low phase then
;           released high phase; returns via r6
; ============================================================================
sub_bit:
        dset    CEC_PIN         ; pull the bus low
        cmpi    r0, 0
        jz      sb0_low
        movi    r2, 5           ; logical 1: 0.6 ms low
sb1_l:
        waitd   CHUNK
        subi    r2, 1
        jnz     sb1_l
        jmp     sb_high
sb0_low:
        movi    r2, 12          ; logical 0: 1.5 ms low
sb0_l:
        waitd   CHUNK
        subi    r2, 1
        jnz     sb0_l
        waitd   REM_SHORT
sb_high:
        dclr    CEC_PIN         ; release: external pull-up raises the bus
        cmpi    r0, 0
        jz      sb0_high
        movi    r2, 15          ; logical 1: 1.8 ms high
sb1_h:
        waitd   CHUNK
        subi    r2, 1
        jnz     sb1_h
        jmp     sb_done
sb0_high:
        movi    r2, 7           ; logical 0: 0.9 ms high
sb0_h:
        waitd   CHUNK
        subi    r2, 1
        jnz     sb0_h
        waitd   REM_SHORT
sb_done:
        csrw    TPC, r6

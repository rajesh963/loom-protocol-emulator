; ============================================================================
; can_tx.asm — LOOM as a CAN 2.0A (standard 11-bit ID) *transmitter*
; ============================================================================
;
; PIN MAPPING (a real board needs a CAN transceiver; external parts are allowed)
;   TXD  P[10] = uo[2]   output: recessive = 1 (high), dominant = 0
;   RXD  P[11] = ui[3]   input:  the transceiver's receive output; the chip
;                        polls it after the frame to confirm the ACK slot
; The transceiver turns TXD into CANH/CANL and RXD back into the bus level;
; host/can.py models the far end at the pin level (dominates RXD in the ACK slot).
;
; WHAT IT TRANSMITS (one classical CAN standard data frame, single shot)
;   SOF (dominant) | ID[10:0] (MSB first) | RTR=0 | IDE=0 | r0=0 | DLC[3:0] |
;   data (8*DLC bits, MSB first) | CRC-15 (MSB first) | CRC delimiter |
;   ACK slot (recessive) | ACK delimiter | EOF 7 recessive | IFS 3 recessive
;   * CRC-15: generator 0x4599 (x^15+x^14+x^10+x^8+x^7+x^4+x^3+1), register
;     init 0, computed bit-by-bit over SOF..last data bit (ISO 11898-1
;     algorithm; check value over "123456789" is 0x059E).
;   * Dynamic bit stuffing: after five consecutive identical bits a
;     complemented stuff bit is inserted; applied to SOF..end of CRC sequence,
;     never to the CRC delimiter/ACK/EOF.  The stuff bit is the complement of
;     the five-bit run (unconditional, so the receiver can destuff
;     deterministically).
;   * The CRC sequence is transmitted MSB first and is *also* stuffed; the
;     remaining-bit counter reaches 15 exactly at the last CRC bit, and the
;     stuffed region ends there.
;   * ACK: the transmitter sends the ACK slot recessive and then polls RXD for
;     up to ACK_POLL loop iterations (~1000 cycles = 15 bit times) -- a
;     receiver drives the slot dominant.  The result (1 = ACK seen, 0 = none)
;     is pushed to the host TX FIFO after the frame.
;
; HOST INTERFACE
;   The host writes the frame's arbitration/control/data *bit stream* into the
;   TX mailbox before RUN, exactly as a CAN controller's TX buffer holds the
;   frame: DMEM[0] = number of SOF..data bits N (19 + 8*DLC), DMEM[1..] = the
;   bits packed 16 per word, earliest bit in bit 0.  host/can.py's
;   `build_frame_bits`/`pack_bit_words` do the packing, so the host supplies
;   (ID, DLC, data) and the firmware does the link layer: CRC-15, stuffing,
;   framing, ACK sampling and bit timing.  The thread then halts (single shot).
;
; TIMING — event scheduler, not instruction counting
;   500 kbit/s at 33.33 MHz = 66.6667 cycles/bit; `setp 0x042B` (1067 in 12.4
;   fixed point = 66.6875 cycles) gives 499.85 kbit/s (-0.03 %), inside the
;   CAN oscillator tolerance.  Every bit (and every stuff bit) is one `emit`,
;   so all edges sit on the 66.6875-cycle grid; the 12.4 carry keeps 100+ bits
;   drift-free.  Per-bit work is ~20-40 instructions against 66.7 cycles, so
;   the 4-deep event queue is always fed before the wire drains it.
;
; CYCLE BUDGET
;   8-byte frame: 19+64 = 83 frame bits + 15 CRC bits + <=16 stuff bits = <=114
;   wire bits = <=7.6 kcycles (228 us), then the ACK poll <=1 kcycle, and the
;   trailing recessive bits are already on the wire.  One frame per run.
;
; SIZE: 79 instructions (< 128, fits the FF fallback build).  Relocatable.
; ============================================================================

.equ CLOCK_HZ,  33333333
.equ CAN_BAUD,  500000
.equ BITPER,    (CLOCK_HZ * 16 + CAN_BAUD / 2) / CAN_BAUD   ; 1067 = 0x042B
.equ TX_PIN,    0x0400          ; P[10] = uo[2], recessive = 1
.equ RXD_PIN,   0x0800          ; P[11] = ui[3]
.equ ACK_POLL,  0x00C8          ; 200 poll iterations (~1000 cycles)
.equ LAST_ADDR, 40              ; DMEM word holding the previous wire bit

; ----------------------------------------------------------------------------
start:
        setm    TX_PIN          ; this thread only drives TXD
        setp    BITPER          ; 66.6875-cycle CAN bit -> 499.85 kbit/s
        set     TX_PIN          ; idle recessive
        ld      r6, 0           ; r6 = N (SOF..last data bit)
        addi    r6, 15          ; + 15 CRC-sequence bits
        movi    r3, 1           ; DMEM read pointer (bit words start at 1)
        ldi     r1, r3          ; r1 = current bit word, bit 0 first
        addi    r3, 1
        movi    r2, 16          ; bits left in r1
        movi    r4, 0           ; CRC-15 register
        movi    r5, 0           ; run length of identical bits
        movi    r0, 2           ; impossible "last bit" before the SOF
        st      LAST_ADDR, r0

; ---- one loop iteration = one bit cell: frame bit or CRC bit ---------------
loop:
        cmpi    r6, 16
        jc      crc_bit         ; <=15 bits left -> CRC sequence
        ; ---- next frame bit from the DMEM bit stream -------------------
        mov     r7, r1
        andi    r7, 1
        shr     r1, 1
        subi    r2, 1
        jnz     fb_ok
        ldi     r1, r3          ; load the next bit word
        addi    r3, 1
        movi    r2, 16
fb_ok:
        ; ---- CRC-15 update: feedback = bit XOR crc[14] ------------------
        mov     r0, r4
        shr     r0, 14
        xor     r0, r7
        jnz     crc_poly
        shl     r4, 1
        andi    r4, 0x7FFF
        jmp     tx_bit
crc_poly:
        shl     r4, 1
        andi    r4, 0x7FFF
        xori    r4, 0x4599
        jmp     tx_bit
crc_bit:
        ; ---- next CRC bit = CRC register MSB, then shift ----------------
        mov     r7, r4
        shr     r7, 14
        shl     r4, 1
        andi    r4, 0x7FFF

; ---- transmit r7 with bit stuffing ----------------------------------------
tx_bit:
        ld      r0, LAST_ADDR   ; r0 = previous wire bit
        cmpi    r5, 5
        jnz     tx_no_stuff
        xori    r0, 1           ; stuff bit = complement of the five-bit run
        shl     r0, 10
        emit    r0              ; one extra bit cell
        movi    r5, 0
        ld      r0, LAST_ADDR
tx_no_stuff:
        ; run counter: 0 or different bit -> 1, same bit -> +1
        cmpi    r5, 0
        jz      tx_run1
        cmp     r7, r0
        jnz     tx_run1
        addi    r5, 1
        jmp     tx_send
tx_run1:
        movi    r5, 1
tx_send:
        st      LAST_ADDR, r7
        mov     r0, r7
        shl     r0, 10          ; bit -> P[10]
        emit    r0
        subi    r6, 1
        jnz     loop

; ---- trailer: CRC delimiter, ACK slot, poll RXD, ACK delimiter, EOF/IFS ----
        movi    r0, TX_PIN
        emit    r0              ; CRC delimiter (recessive)
        emit    r0              ; ACK slot (transmitter sends recessive)
        movi    r3, ACK_POLL
ack_poll:
        in      r0
        andi    r0, RXD_PIN     ; 0 = dominant = a receiver ACKed the frame
        jz      ack_seen
        subi    r3, 1
        jnz     ack_poll
        movi    r2, 0
        jmp     ack_report
ack_seen:
        movi    r2, 1
ack_report:
        push    r2              ; 1 = ACK seen -> host; 0 = no ACK
        movi    r0, TX_PIN
        emit    r0              ; ACK delimiter
        rpt     6
        emit    r0              ; 7 x EOF (recessive, not stuffed)
        rpt     2
        emit    r0              ; 3 x IFS / intermission
        halt

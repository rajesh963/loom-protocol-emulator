; ============================================================================
; eth_tx.asm — LOOM Ethernet (802.3) Manchester frame transmitter
; ============================================================================
;
; WHAT IT IS
;   Stage 1 of the Ethernet stretch goal: a full 64-byte minimum Ethernet frame
;   transmitted with IEEE 802.3 Manchester line coding and a CRC-32 FCS that the
;   firmware computes itself.  Not 10BASE-T-compliant: 10BASE-T needs 20 Mbaud
;   (1.6667-cycle cells at 33.33 MHz) with ns-class TX jitter, while our cell
;   period here is 8 cycles and pin edges are cycle-quantised (see
;   sw/examples/EPROTOCOL? -> the README sections and host/eth.py CLI for the
;   honest ceiling numbers).
;
; PIN
;   TXD  P[0] = uio[0], push-pull (DIR bit 0 = 1), Manchester-coded.
;   (Separate standalone example; the USB examples also use uio[0].)
;
; FRAME (625 ns/bit at CELL_DIV=8: bit=16 cyc -> 2.083 Mbit/s, 4.167 Mbaud)
;   idle (0) -> 7 x 0xAA preamble -> 0xD5 SFD -> 64 frame bytes -> IPG -> idle
;   Frame bytes (DMEM[0..31], byte-packed little-endian):
;       dst(6) + src(6) + ethertype(2) + payload(46) + FCS(4) = 64 bytes
;   The host preloads DMEM[0..29] (60 bytes); the firmware computes
;   CRC-32/802.3 over those 60 bytes and overwrites DMEM[30..31] with the FCS
;   (little-endian), then streams all 64 bytes.
;   Manchester: 0 -> high,low (cells 1,0); 1 -> low,high (cells 0,1);
;   bytes/bits LSB-first.
;
; TIMING / DMEM
;   setp 0x0080 = 8 cycles per Manchester cell = 16 cycles/bit = 4.167 Mbaud,
;   2.083 Mbit/s; every cell is one `emit`, so all edges are on the exact
;   event grid (no instruction-timing jitter).  Transmit loop budget: ~8
;   instructions per bit against 16 cycles/bit.
;   Wire time: (56 + 8 + 512) bits x 16 = 9216 cycles = 276 us per frame;
;   IPG = 1536 cycles (96 bit times); DMEM footprint: **32/32 words** (the full
;   64-byte frame buffer); state lives in registers only.
;
; SIZE: fits the 128-word FF fallback build.
; ============================================================================

.equ CELL_DIV,   0x0080          ; 8 cycles per Manchester cell
.equ TXD_PIN,    0x0001          ; P[0] = uio[0]
.equ FCS_LO,     0x8320          ; 0xEDB88320 split for 16-bit registers
.equ FCS_HI,     0xEDB8
.equ IPG_DIV,    0x6000          ; 1536 cycles = 96 bit times inter-packet gap

; ----------------------------------------------------------------------------
start:
        setm    TXD_PIN          ; this thread's events drive P[0]
        dset    TXD_PIN          ; output enable
        clr     TXD_PIN          ; idle low
        setp    CELL_DIV         ; 8-cycle Manchester cell clock
        ; ================= CRC-32/802.3 over DMEM[0..29] =================
crc_start:
        movi    r4, 0xFFFF       ; CRC low 16 bits
        movi    r5, 0xFFFF       ; CRC high 16 bits
        movi    r6, 0            ; DMEM word address
        movi    r1, 30           ; 30 words = 60 bytes
crc_word:
        ldi     r7, r6
        addi    r6, 1
        movi    r0, 2            ; two bytes per word
crc_byte:
        mov     r2, r7
        andi    r2, 0x00FF       ; low byte first (little endian)
        xor     r4, r2
        movi    r3, 8            ; 8 CRC bit steps
crc_bit:
        mov     r2, r4
        andi    r2, 1
        jz      crc_shift
        ; ---- odd: shift the 32-bit CRC right, then XOR the reflected poly --
        mov     r2, r5
        andi    r2, 1
        shl     r2, 15
        shr     r4, 1
        or      r4, r2
        shr     r5, 1
        movi    r2, FCS_LO
        xor     r4, r2
        movi    r2, FCS_HI
        xor     r5, r2
        jmp     crc_next
crc_shift:
        mov     r2, r5
        andi    r2, 1
        shl     r2, 15
        shr     r4, 1
        or      r4, r2
        shr     r5, 1
crc_next:
        subi    r3, 1
        jnz     crc_bit
        shr     r7, 8
        subi    r0, 1
        jnz     crc_byte
        subi    r1, 1
        jnz     crc_word
        ; ---- final XOR, then store the FCS into DMEM[30..31] ------------
        movi    r2, 0xFFFF
        xor     r4, r2
        xor     r5, r2
        movi    r6, 30
        sti     r4, r6           ; bytes 60..61 (FCS little-endian)
        addi    r6, 1
        sti     r5, r6           ; bytes 62..63
        ; ================= 7 x 0xAA preamble + 0xD5 SFD ==================
        movi    r6, 7
pre_loop:
        movi    r1, 0x00AA
        csrr    r7, TPC
        movi    r0, send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        subi    r6, 1
        jnz     pre_loop
        movi    r1, 0x00D5       ; SFD
        csrr    r7, TPC
        movi    r0, send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        ; ================= 64 frame bytes from DMEM (Manchester) =========
        movi    r6, 0
tx_word:
        ldi     r2, r6
        mov     r1, r2
        andi    r1, 0x00FF       ; byte 2i
        csrr    r7, TPC
        movi    r0, send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        shr     r2, 8
        mov     r1, r2
        andi    r1, 0x00FF       ; byte 2i+1
        csrr    r7, TPC
        movi    r0, send_byte - . + 1
        add     r0, r7
        addi    r7, 5
        csrw    TPC, r0
        addi    r6, 1
        cmpi    r6, 32
        jnz     tx_word
        ; ================= IPG, then next frame =========================
        waitd   IPG_DIV
        jmp     crc_start

; ----------------------------------------------------------------------------
; send_byte: transmit r1 (8 bits, LSB first) as Manchester cells.
;   First cell of a 0 bit = 1, of a 1 bit = 0; second cell = the bit itself.
; ----------------------------------------------------------------------------
send_byte:
        movi    r3, 8
sb_loop:
        mov     r4, r1
        andi    r4, 1            ; data bit
        shr     r1, 1
        mov     r5, r4
        xori    r5, 1            ; first half-cell = 1-bit
        emit    r5               ; cell 2k   (target = NEXTT + 8)
        emit    r4               ; cell 2k+1 (target = NEXTT + 8)
        subi    r3, 1
        jnz     sb_loop
        csrw    TPC, r7          ; return

; ============================================================================
; usb_ls_rx.asm — LOOM USB 1.1 Low-Speed *device receiver* (stretch goal)
; ============================================================================
;
; STATUS: **device RX decode + ACK response verified on the golden model.**
; The thread samples the differential line (D+ / D-) on the exact 22.25-cycle
; LS bit grid, NRZI-decodes, validates SYNC, bit-unstuffs, checks PID +
; complement, CRC-5 (token) / CRC-16 (data) and the EOP (SE0 for two bit
; cells), publishes the decoded packet to the host, and ACKs a valid DATA0.
; NOT implemented in this pass (honest boundary, see README): the enumeration
; state machine (GET_DESCRIPTOR response / SET_ADDRESS).  DMEM analysis says a
; 16-18-byte device descriptor fits by reusing the dead sample region
; DMEM[0..8] at response time; a config descriptor does not fit in 32 words.
;
; TIMING HYGIENE: the per-bit loop has ZERO issue slack -- 15 instructions plus
; a 7.25-cycle WAITD make exactly 22.25 cycles.  The program must therefore run
; undisturbed: one thread only, no other thread on these pins/FIFOs, and no
; host CSR/DMEM retry during a packet (the model treats host accesses as
; atomic between cycles; on RTL a stolen cycle shifts the sample grid by one
; cycle -- tolerated by the 6..12-cycle alignment window, but repeated stalls
; break it).  So: "RX is single-thread and host-quiet while a packet is being
; sampled."
;
; RESPONSE LATENCY (honest): the ACK is symbol-correct (SYNC + PID 0xD2 + EOP,
; 18 cells at 22.25 cycles) but arrives far outside the USB response window
; because sampling (104-bit fixed window, 2314 cycles) and untimed
; post-processing are not pipelined.  USB LS allows 7.5 bit times (167 cycles).
;
; CEILING QUANTIFICATION (measured on the golden model with PC breakpoints,
; DATA0 8-byte packet, EOP at sample 96; 22.25 cycles/bit):
;   sampler per bit ............ 16 instructions + WAITD 7.25 = 22.25 cyc
;                                (72 % of the bit budget; zero issue slack)
;   host EOP -> sample end ..... 198 cyc  ( 8.9 bit times; fixed 104-sample
;                                window covers 8 bits past EOP)
;   NRZI+unstuff+bytes (96 st) . 2505 cyc (112.6 bit times)
;   PID + CRC-16 over 8 bytes .. 564 cyc  ( 25.3 bit times)
;   EOP double-SE0 scan (104 st) 1155 cyc ( 51.9 bit times)
;   publish + ACK setup/emit ... 23 cyc
;   ----------------------------------------------------------------
;   total ACK latency .......... 4445 cyc (199.8 bit times) vs 167 (7.5 bits)
;
; WHY THIS IS AN ARCHITECTURAL CEILING, not a tuning problem:
;   * The packet is decoded *after* capture, so even a perfectly optimised
;     post-processing pass costs hundreds of cycles (>=4.5 bit times) -- you
;     cannot validate CRC-16 and start the ACK within 7.5 bit times.
;   * Wire-speed ACK requires decoding DURING reception.  Per bit that needs
;     the sampler (16 instr) PLUS inline NRZI+unstuff+accumulate (~10 instr)
;     PLUS CRC-16 (~8 instr/bit) = ~34 instructions/bit > 22.25, i.e. ~1.5x
;     over IPC, and two threads cannot help (they share the single IPC).
;   * Even with an idealised 8-instruction sampler the total is ~26 > 22.25
;     (~17 % over), so an inline single-thread LS RX is not achievable.
;   => Revision-B option (NOT built here): a small hardware LS receiver FSM
;      (edge detect + DPLL bit sampler + NRZI/stuff/CRC-16 + EOP, byte FIFO /
;      interrupt to firmware) would let the firmware ACK within a few bit
;      times.  Firmware-only wire-speed LS RX is not physically feasible on
;      this core; this document is the measured proof.
;
; PIN MAPPING (device mode: both lines are inputs while receiving)
;   D+  P[0] = uio[0]     D-  P[1] = uio[1]
;   Line state value = D+ | (D- << 1):  K = 1, J = 2 (idle), SE0 = 0, SE1 = 3.
;   DIR bits 0:1 are clear while sampling (high-Z, the host drives); the
;   response code sets them when the device drives the pair.
;
; RECEIVE PATH
;   1. poll D+ for the J->K transition that starts SYNC (3-cycle poll).
;   2. one inline sample right after it, then 103 samples every 22.25 cycles:
;        loop = WAITD(7.25) + 15 fixed instructions = 22.25 cycles exactly
;      (branchy work is padded with NOPs, so every sample instant sits on an
;      exact 22.25-cycle grid; the first sample is aligned near the first bit
;      centre).  104 samples x 2 bits = 13 DMEM words: DMEM[0..12], first
;      sample in bits 1:0 of word 0, 8 samples per word.
;   3. post-process (untimed; the packet is already captured):
;      SYNC state pattern -> NRZI bits -> bit-unstuff -> bytes in DMEM[13..23]
;      -> PID + complement -> token CRC-5 / data CRC-16 -> EOP check (states
;      N,N+1 = SE0 and N+2,N+3 = idle J) -> summary pushed to the host TX FIFO:
;      [status, pid, addr|endp<<7, crc_ok, eop_ok].
;
; DMEM MAP (32 words = 64 bytes, shipped build)
;   [0..12]  104 sampled line states (2 bits each, 8 per word)
;   [13]     PID, [14..21] 8 data bytes (DATA0) or token fields
;   [22..23] received CRC-16 (DATA0)
;   [24] status (1=token, 2=data, 3=handshake)
;   [25] pid     [26] addr|endp<<7    [27] crc_ok   [28] eop_ok
;   [29] sync_ok  [30] payload length  [31] scratch (response state)
;
; TIMING / CODE BUDGET
;   22.25 cycles/bit (setp 0x0164); the sampler spends 16 instructions/bit at
;   IPC 1 (72 % of the 22.25-cycle budget); post-processing is untimed.
;   RX decode is code-size-bound, not timing-bound (README).
;
; SIZE: 357 instructions -> **SRAM-1024 build only**; the 128-word FF fallback
;   cannot hold it (documented in the README / report).
; ============================================================================

.equ BIT_DIV,     0x0164         ; 22.25 cycles/bit (356/16) = 1.4981 Mbit/s
.equ SAMPLE_DIV,  0x0074         ; 7.25 cycles; +15 instructions = 22.25
.equ NSAMP,       104            ; <= 8 SYNC + 96 worst-case stuffed data bits
.equ BYTE_BASE,   13             ; decoded bytes land here
.equ SYNC_WORD,   0x5999         ; sampled SYNC states K J K J K J K K
.equ TMASK,       0x0003         ; response drives D+ and D-

; ----------------------------------------------------------------------------
start:
        setm    TMASK            ; response events drive both USB lines
        dclr    TMASK            ; high-Z while receiving (host drives)
        setp    BIT_DIV          ; LS bit clock for sampler / responses

main:
poll:
        in      r1
        andi    r1, 1            ; D+ (idle J = 0)
        jz      poll
        ; ---- fixed-offset grid alignment -----------------------------
        ; The edge is detected with up to a 3-cycle poll latency; this 9-cycle
        ; delay centres the first sample in SYNC bit 0.  The valid window is
        ; 6..12 cycles (measured): inside it the 104-bit window decodes at
        ; nominal, +0.25 % and -0.25 % host clock drift.
        waitd   0x0090
        movi    r0, NSAMP - 1
        movi    r3, 2            ; previous line state = J
        movi    r4, 0            ; packed sample word
        movi    r5, 8            ; samples left in this word
        movi    r6, 0            ; sample-word DMEM address
        in      r1
        andi    r1, 3
        mov     r2, r1
        shl     r2, 14
        shr     r4, 2
        or      r4, r2
        subi    r5, 1
        jnz     first_no_store
        sti     r4, r6
        addi    r6, 1
        movi    r4, 0
        movi    r5, 8
first_no_store:
sample:
        waitd   SAMPLE_DIV       ; 7.25 cycles + 15 instructions = 22.25
        in      r1
        andi    r1, 3
        mov     r2, r1
        shl     r2, 14
        shr     r4, 2
        or      r4, r2
        subi    r5, 1
        jnz     no_store
        sti     r4, r6
        addi    r6, 1
        movi    r4, 0
        movi    r5, 8
        jmp     stored
no_store:
        nop
        nop
        nop
        nop
        nop
stored:
        subi    r0, 1
        jnz     sample
        cmpi    r5, 8
        jz      all_stored
        sti     r4, r6           ; final partial sample word
all_stored:
        ; ================= SYNC check ==================================
        movi    r0, 0
        ldi     r1, r0
        movi    r2, SYNC_WORD
        movi    r3, 1
        cmp     r1, r2
        jz      sync_ok1
        movi    r3, 0
sync_ok1:
        movi    r0, 29
        sti     r3, r0           ; DMEM[29] = sync_ok
        ; ================= NRZI + unstuff -> bytes =====================
        ; Skip the 8 SYNC states (word 0); start from word 1 with prev = K.
        movi    r0, 1            ; sample-word address
        ldi     r1, r0
        movi    r2, 8            ; states left in word
        movi    r3, 0            ; (bits_in_acc << 8) | byte_ptr
        movi    r4, 1            ; previous state = last SYNC state K
        movi    r5, 0            ; consecutive-ones counter
        movi    r6, 0            ; byte accumulator (LSB first)
dec_loop:
        mov     r7, r1
        andi    r7, 3            ; line state
        shr     r1, 2
        subi    r2, 1            ; one state consumed
        ; NRZI bit: 1 when the line held, 0 when it changed
        ; (branch on the CMP flags *before* the flag-setting MOV)
        cmp     r7, r4
        jz      dec_held
        mov     r4, r7
        movi    r7, 0
        jmp     dec_bit
dec_held:
        mov     r4, r7
        movi    r7, 1
dec_bit:
        ; bit stuffing: after six 1s the next bit is the stuffed 0
        cmpi    r5, 6
        jnz     dec_data
        movi    r5, 0
        jmp     dec_next
dec_data:
        cmpi    r7, 0
        jz      dec_zero_run
        addi    r5, 1
        jmp     dec_acc
dec_zero_run:
        movi    r5, 0
dec_acc:
        shr     r6, 1
        cmpi    r7, 0
        jz      dec_no_high
        ori     r6, 0x0080
dec_no_high:
        addi    r3, 0x0100       ; one more bit in the accumulator
        mov     r7, r3
        shr     r7, 8
        cmpi    r7, 8
        jnz     dec_next
        mov     r7, r3           ; ---- one complete byte ----
        andi    r7, 0x00FF
        addi    r7, BYTE_BASE
        sti     r6, r7
        movi    r6, 0
        mov     r7, r3
        andi    r7, 0x00FF
        addi    r7, 1
        mov     r3, r7
dec_next:
        cmpi    r2, 0
        jnz     dec_loop
        addi    r0, 1
        cmpi    r0, 13
        jz      dec_done
        ldi     r1, r0
        movi    r2, 8
        jmp     dec_loop
dec_done:
        ; store the decoded byte count
        mov     r1, r3
        andi    r1, 0x00FF
        movi    r0, 30
        sti     r1, r0           ; DMEM[30] = payload/field byte count
        ; ================= PID + complement =============================
        movi    r0, 13
        ldi     r6, r0           ; PID
        movi    r0, 25
        sti     r6, r0
        mov     r1, r6
        andi    r1, 0x000F
        movi    r2, 0x000F
        xor     r1, r2           ; ~nibble
        shl     r1, 4
        mov     r2, r6
        andi    r2, 0x00F0
        movi    r3, 1
        cmp     r2, r1
        jz      pid_ok1
        movi    r3, 0
pid_ok1:
        ; branch on the PID nibble
        mov     r1, r6
        andi    r1, 0x000F
        cmpi    r1, 3            ; DATA0 / DATA1
        jz      data_path
        cmpi    r1, 2            ; ACK handshake
        jz      ack_path
        ; ------------------ token (SETUP / OUT / IN) -------------------
token_path:
        movi    r0, 14
        ldi     r3, r0           ; byte 1
        movi    r0, 15
        ldi     r4, r0           ; byte 2
        mov     r5, r3
        andi    r5, 0x007F       ; ADDR (7 bits)
        mov     r6, r3
        shr     r6, 7
        mov     r1, r4
        andi    r1, 0x0007
        shl     r1, 1
        or      r6, r1           ; ENDP (4 bits)
        mov     r1, r4
        shr     r1, 3            ; received CRC-5 (5 bits)
        mov     r2, r6
        shl     r2, 7
        or      r2, r5           ; 11-bit ADDR+ENDP, LSB first
        movi    r7, 0x001F       ; CRC-5/USB
        movi    r0, 11
crc5_loop:
        mov     r3, r2
        andi    r3, 1
        shr     r2, 1
        mov     r4, r7
        andi    r4, 1
        xor     r4, r3
        shr     r7, 1
        cmpi    r4, 0
        jz      crc5_nopoly
        movi    r4, 0x0014
        xor     r7, r4
crc5_nopoly:
        subi    r0, 1
        jnz     crc5_loop
        movi    r4, 0x001F
        xor     r7, r4           ; final XOR-0x1F (CRC-5/USB)
        movi    r3, 1
        cmp     r7, r1
        jz      tok_crc_ok
        movi    r3, 0
tok_crc_ok:
        movi    r0, 24
        movi    r1, 1
        sti     r1, r0           ; status = token
        movi    r0, 26
        mov     r1, r6
        shl     r1, 7
        or      r1, r5
        sti     r1, r0           ; addr|endp<<7
        movi    r0, 27
        sti     r3, r0           ; crc_ok
        jmp     finish
        ; ------------------ DATA0 / DATA1 ------------------------------
data_path:
        movi    r0, 24
        movi    r1, 2
        sti     r1, r0           ; status = data
        movi    r0, 30
        movi    r1, 8
        sti     r1, r0           ; fixed 8-byte payload (SETUP data stage)
        ; ---- CRC-16/USB over the 8 data bytes (DMEM[14..21]) ----
        ; reflected poly 0x8005 -> 0xA001, init 0xFFFF, final XOR 0xFFFF
        movi    r4, 0xFFFF
        movi    r6, 14
        movi    r0, 8
data_byte:
        ldi     r1, r6
        addi    r6, 1
        xor     r4, r1
        movi    r2, 8
data_bit:
        mov     r1, r4
        andi    r1, 1
        jz      data_shift
        shr     r4, 1
        movi    r1, 0xA001
        xor     r4, r1
        jmp     data_next
data_shift:
        shr     r4, 1
data_next:
        subi    r2, 1
        jnz     data_bit
        subi    r0, 1
        jnz     data_byte
        movi    r1, 0xFFFF
        xor     r4, r1
        ; received CRC-16 = DMEM[22] | DMEM[23]<<8
        movi    r0, 22
        ldi     r1, r0
        movi    r0, 23
        ldi     r2, r0
        shl     r2, 8
        or      r1, r2
        movi    r3, 1
        cmp     r4, r1
        jz      data_crc_ok
        movi    r3, 0
data_crc_ok:
        movi    r0, 27
        sti     r3, r0
        jmp     finish
        ; ------------------ ACK handshake ------------------------------
ack_path:
        movi    r0, 24
        movi    r1, 3
        sti     r1, r0
        jmp     finish
        ; ================= EOP check + publish =========================
        ; Find the first pair of consecutive SE0 samples: that is the EOP,
        ; then require the next two samples to be idle J (2).  Both the EOP
        ; index and the result are published (index also gives the number of
        ; encoded bits on the wire, i.e. the stuffing evidence).
finish:
        movi    r0, 0            ; sample-word address
        ldi     r1, r0
        movi    r2, 8            ; samples left in word
        movi    r3, 0            ; sample index k
        movi    r4, 3            ; previous state (sentinel SE1)
        movi    r6, 0xFFFF       ; EOP pair start (0xFFFF = not found)
eop_scan:
        mov     r5, r1
        andi    r5, 3            ; state[k]
        shr     r1, 2
        subi    r2, 1
        cmpi    r4, 0            ; previous state SE0?
        jnz     eop_next
        cmpi    r5, 0            ; current state SE0?
        jnz     eop_next
        mov     r6, r3
        subi    r6, 1            ; pair start = k-1
        jmp     eop_found
eop_next:
        mov     r4, r5
        addi    r3, 1
        cmpi    r2, 0
        jnz     eop_scan
        addi    r0, 1
        cmpi    r0, 13
        jz      eop_notfound
        ldi     r1, r0
        movi    r2, 8
        jmp     eop_scan
eop_notfound:
        movi    r0, 28
        movi    r3, 0
        sti     r3, r0           ; eop_ok = 0
        movi    r0, 31
        sti     r6, r0           ; DMEM[31] = 0xFFFF
        jmp     publish
eop_found:
        movi    r0, 31
        sti     r6, r0           ; DMEM[31] = EOP cell-1 sample index
        movi    r0, 28
        movi    r3, 1
        sti     r3, r0           ; eop_ok = 1 until proven otherwise
        ; verify states pair_start+2 and +3 are idle J (2)
        movi    r7, 2            ; j = 2, 3
eop_chk:
        mov     r3, r6
        add     r3, r7           ; k = pair_start + j
        mov     r4, r3
        shr     r4, 3
        ldi     r5, r4           ; sample word
        mov     r4, r3
        andi    r4, 7
        shl     r4, 1            ; shift = (k&7)*2
eop_shift:
        cmpi    r4, 0
        jz      eop_shift_done
        shr     r5, 1
        subi    r4, 1
        jmp     eop_shift
eop_shift_done:
        andi    r5, 3            ; state[k]
        movi    r3, 2
        movi    r4, 1
        cmp     r5, r3
        jz      eop_ok2
        movi    r4, 0
eop_ok2:
        cmpi    r4, 0
        jnz     eop_chk_next
        movi    r0, 28
        movi    r3, 0
        sti     r3, r0           ; eop_ok = 0
eop_chk_next:
        addi    r7, 1
        cmpi    r7, 4
        jnz     eop_chk
publish:
        ; ---------------- publish to the host TX FIFO ------------------
        movi    r0, 24
        ldi     r1, r0
        push    r1
        movi    r0, 25
        ldi     r1, r0
        push    r1
        movi    r0, 26
        ldi     r1, r0
        push    r1
        movi    r0, 27
        ldi     r1, r0
        push    r1
        movi    r0, 28
        ldi     r1, r0
        push    r1
        ; ================= ACK response after a valid DATA0 ============
        ; (checks the publish results directly: status == data, CRC ok)
        movi    r1, 24
        ldi     r0, r1
        cmpi    r0, 2
        jnz     no_response
        movi    r1, 27
        ldi     r0, r1
        cmpi    r0, 1
        jnz     no_response
        dset    TMASK            ; drive the differential pair
        ; ---- SYNC: 7 toggling cells then a held 1 (K J K J K J K K) ----
        movi    r0, 1            ; line state K
        movi    r1, 7
ack_sync:
        mov     r5, r0
        emit    r5               ; one cell per 22.25-cycle period
        xori     r0, 3            ; K <-> J toggle (NRZI data 0)
        subi    r1, 1
        jnz     ack_sync
        xori     r0, 3
        mov     r5, r0
        emit    r5               ; 8th SYNC cell is data 1 (hold)
        ; ---- PID 0xD2 with NRZI + bit stuffing ------------------------
        movi    r1, 0x00D2
        movi    r2, 8
        movi    r3, 0            ; consecutive-ones counter
ack_bit:
        cmpi    r3, 6
        jnz     ack_nostuff
        xori     r0, 3            ; stuffed 0: toggle
        mov     r5, r0
        emit    r5
        movi    r3, 0
ack_nostuff:
        mov     r4, r1
        andi    r4, 1
        shr     r1, 1
        cmpi    r4, 0
        jnz     ack_one
        xori     r0, 3            ; data 0: toggle
        movi    r3, 0
        jmp     ack_emit
ack_one:
        addi    r3, 1
ack_emit:
        mov     r5, r0
        emit    r5
        subi    r2, 1
        jnz     ack_bit
        ; ---- EOP: SE0 (both lines low) for two bit cells --------------
        movi    r0, 0
        mov     r5, r0
        emit    r5
        emit    r5
        ; the CPU runs ahead of the event queue, so let the EOP events fire
        ; before releasing the pair back to the host (5 bit times covers the
        ; 4-deep queue plus the two EOP cells)
        waitd   0x06F4           ; 111.25 cycles
        dclr    TMASK            ; release the pair back to the host
no_response:
        jmp     main

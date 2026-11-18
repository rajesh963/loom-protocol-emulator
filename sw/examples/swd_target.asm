; ============================================================================
; swd_target.asm — LOOM as an ARM Serial Wire Debug (SWD) *target* (DP+AP)
; ============================================================================
;
; PIN MAPPING (the debug probe drives both lines; SWDIO is bidirectional)
;   SWCLK  P[0] = uio[0]   INPUT  (probe-driven clock; DIR bit 0 stays 0)
;   SWDIO  P[1] = uio[1]   BIDIR  (DIR bit 1 = 1 drives, 0 releases to the probe;
;                                released reads high through the external pull-up)
; A real board needs the standard SWD pull-up on SWDIO (external parts are
; allowed); no other parts.  host/swd.py is the independent probe model.
;
; WHAT IT EMULATES — a minimal ADIv5 debug port + access port
;   Target samples SWDIO on SWCLK rising edges, drives on falling edges.
;   Request packet: start(1) APnDP RnW A2 A3 parity stop(0) park(1), LSB
;   first; even parity over APnDP,RnW,A2,A3 and over the 32 data bits.
;   Turnaround: one clock after the request, then the target drives the 3-bit
;   ACK (OK=0b001 LSB first = release,drive-low,drive-low; FAULT=0b100) on
;   falling edges; the probe samples on rising edges.
;     - Read: 32 data bits + parity driven by the target, then a turnaround
;       clock; SWDIO released.
;     - Write: 32 data bits + parity driven by the probe and sampled by the
;       target on rising edges; the target stores the value.
;   Supported register subset (word address A[3:2]):
;     DP  0x0 read  = IDCODE 0x0BB11477 (4 KB RAM, ARM JTAG-DP)
;     DP  0x1 read  = CTRL/STAT = 0xF0000000 (both power-up ACKs set)
;     DP  0x2 write = SELECT, stored (DMEM[8..9])
;     DP  0x3 read  = RDBUFF, returns the last AP write (DMEM[10..11])
;     AP  0x1 write/read = TAR (DMEM[14..15])
;     AP  0x3 write/read = DRW (DMEM[16..17]); a DRW write also updates RDBUFF
;   FAULT is returned when the request parity/stop/park bits are wrong (no
;   data phase); a write with bad data parity is dropped (not stored).
;   Honest boundary: no SWD-to-JTAG switching, no WAIT/retry, no AP bank/DPBANKSEL
;   (only the two AP registers above), no real core/memory behind DRW.
;
; TIMING — probe clock, polling firmware
;   The firmware is clocked *by the probe* (SWCLK is an input): every bit is a
;   pair of `waitp` edge waits (<=4-cycle latency each) plus ~10-15
;   instructions, so the supported SWCLK is roughly 33.33 MHz / (2 * ~28) ~=
;   600 kHz at the shipped design point; host/swd.py measures the actual
;   ceiling by sweeping the probe half period.  There is no chip-side clock
;   generation: the wire timing is the probe's.
;
; SIZE: 210 instructions -> SRAM-1024 build only (like usb_ls_rx.asm); the
;   128-word FF fallback cannot hold it.  Relocatable, single-thread.
; ============================================================================

.equ SWCLK,     0x0001          ; P[0] = uio[0], input
.equ SWDIO,     0x0002          ; P[1] = uio[1], bidirectional
.equ CLK_HI,    0x0101          ; waitp: (P[0] & 1) == 1
.equ CLK_LO,    0x0100          ; waitp: (P[0] & 1) == 0
.equ ID_L,      0x1477          ; IDCODE 0x0BB11477
.equ ID_H,      0x0BB1
.equ CTRL_H,    0xF000          ; CTRL/STAT = 0xF0000000

; ----------------------------------------------------------------------------
start:
        setm    0
        clr     0x0003          ; both lines low, SWDIO released
        dclr    SWDIO
        movi    r0, 0
        st      8, r0          ; DP SELECT
        st      9, r0
        st      10, r0         ; DP RDBUFF
        st      11, r0
        st      14, r0         ; AP TAR
        st      15, r0
        st      16, r0         ; AP DRW
        st      17, r0

; ---- wait for a request start bit (rising edge with SWDIO high) ------------
main:
wait_start:
        waitp   CLK_LO
        waitp   CLK_HI
        in      r1
        andi    r1, SWDIO
        jz      wait_start
        movi    r1, 1           ; reversed packet accumulator: start first
        movi    r4, 7
req_loop:
        waitp   CLK_LO          ; falling: probe sets the next request bit
        waitp   CLK_HI          ; rising: sample it
        in      r0
        andi    r0, SWDIO
        shr     r0, 1           ; SWDIO bit -> 0/1
        shl     r1, 1
        or      r1, r0
        subi    r4, 1
        jnz     req_loop

; ---- decode the request (bits: start7 APnDP6 RnW5 A2-4 A3-3 parity2 stop1 park0)
        mov     r0, r1
        shr     r0, 6
        andi    r0, 1           ; r0 = APnDP
        mov     r5, r1
        shr     r5, 5
        andi    r5, 1           ; r5 = RnW
        mov     r6, r1
        shr     r6, 4
        andi    r6, 1           ; r6 = A2
        mov     r4, r1
        shr     r4, 3
        andi    r4, 1           ; A3
        shl     r4, 1
        or      r6, r4          ; r6 = A[3:2] word address
        ; stop bit = 0 and park bit = 1 -> (request & 3) == 1
        mov     r4, r1
        andi    r4, 3
        cmpi    r4, 1
        jnz     req_bad
        ; even parity over APnDP, RnW, A2, A3 == request parity bit
        mov     r4, r1
        shr     r4, 3
        andi    r4, 0x0F
        mov     r7, r4
        shr     r7, 2
        xor     r4, r7
        mov     r7, r4
        shr     r7, 1
        xor     r4, r7
        andi    r4, 1           ; computed parity
        mov     r7, r1
        shr     r7, 2
        andi    r7, 1           ; received parity
        cmp     r4, r7
        jnz     req_bad
        movi    r1, 0           ; r1 = fault flag = 0
        jmp     ack
req_bad:
        movi    r1, 1
        jmp     ack

; ---- dispatch (runs inside the ACK phase, between ACK1 and ACK2) -----------
;      preload read data (r2/r3) or the write-store pointer (r7)
dispatch:
        cmpi    r5, 0
        jz      wr_dispatch
        ; ---------------- read transaction: r2/r3 = data -----------------
        cmpi    r0, 0
        jnz     rd_ap
        cmpi    r6, 0
        jz      rd_idcode
        cmpi    r6, 1
        jz      rd_ctrl
        cmpi    r6, 3
        jz      rd_rdbuff
        movi    r2, 0
        movi    r3, 0
        jmp     ack2
rd_idcode:
        movi    r2, ID_L
        movi    r3, ID_H
        jmp     ack2
rd_ctrl:
        movi    r2, 0
        movi    r3, CTRL_H
        jmp     ack2
rd_rdbuff:
        ld      r2, 10
        ld      r3, 11
        jmp     ack2
rd_ap:
        cmpi    r6, 1
        jz      rd_tar
        cmpi    r6, 3
        jz      rd_drw
        movi    r2, 0
        movi    r3, 0
        jmp     ack2
rd_tar:
        ld      r2, 14
        ld      r3, 15
        jmp     ack2
rd_drw:
        ld      r2, 16
        ld      r3, 17
        jmp     ack2
        ; ---------------- write transaction: r7 = DMEM target (0 = drop) ---
wr_dispatch:
        cmpi    r0, 0
        jnz     wr_ap
        cmpi    r6, 2
        jnz     wr_ignore
        movi    r7, 8          ; DP SELECT
        jmp     ack2
wr_ap:
        cmpi    r6, 1
        jz      wr_tar
        cmpi    r6, 3
        jz      wr_drw
wr_ignore:
        movi    r7, 0
        jmp     ack2
wr_tar:
        movi    r7, 14
        jmp     ack2
wr_drw:
        movi    r7, 16
        jmp     ack2

; ---- ACK phase: turnaround clock then 3 ACK bits (driven on falling) ------
ack:
        waitp   CLK_LO          ; F8 (host releases SWDIO)
        waitp   CLK_HI          ; R9: turnaround clock
        waitp   CLK_LO          ; F9: drive ACK bit 0
        cmpi    r1, 0
        jnz     ack_fault
        dclr    SWDIO           ; OK bit0 = 1 -> release
        waitp   CLK_HI          ; R10: probe samples ACK0
        waitp   CLK_LO          ; F10
        clr     SWDIO
        dset    SWDIO           ; OK bit1 = 0 -> drive low
        jmp     dispatch        ; data preload / store pointer, before ACK2
ack2:
        waitp   CLK_HI          ; R11
        waitp   CLK_LO          ; F11: OK bit2 = 0 (still driven low)
        cmpi    r5, 0
        jz      wr_data
        jmp     rd_data
ack_fault:
        clr     SWDIO
        dset    SWDIO           ; FAULT bit0 = 0 -> drive low
        waitp   CLK_HI
        waitp   CLK_LO          ; FAULT bit1 = 0 (still low)
        waitp   CLK_HI
        waitp   CLK_LO          ; F11: FAULT bit2 = 1 -> release
        dclr    SWDIO
        waitp   CLK_HI          ; R12: probe samples the FAULT ACK
        jmp     main

; ---- read data phase: 32 data bits + parity driven on falling edges -------
rd_data:
        movi    r4, 32
        movi    r5, 0           ; parity accumulator
rd_loop:
        waitp   CLK_HI          ; R(12+k): probe samples the previous bit
        waitp   CLK_LO          ; F(12+k): drive bit k
        mov     r0, r2
        andi    r0, 1
        xor     r5, r0
        shl     r0, 1           ; -> SWDIO pin (P[1])
        csrw    POUT, r0
        mov     r1, r3          ; 32-bit shift right r2:r3
        andi    r1, 1
        shl     r1, 15
        shr     r2, 1
        or      r2, r1
        shr     r3, 1
        subi    r4, 1
        jnz     rd_loop
        waitp   CLK_HI          ; R44
        waitp   CLK_LO          ; F44: parity bit
        mov     r0, r5
        andi    r0, 1
        shl     r0, 1
        csrw    POUT, r0
        waitp   CLK_HI          ; R45: probe samples parity
        waitp   CLK_LO          ; F45: release for the turnaround clock
        dclr    SWDIO
        waitp   CLK_HI          ; R46: the turnaround clock itself
        waitp   CLK_LO          ; F46: probe drives idle again
        jmp     main

; ---- write data phase: 32 data bits + parity sampled on rising edges ------
wr_data:
        waitp   CLK_HI          ; R12: probe samples ACK2
        waitp   CLK_LO          ; F12: release SWDIO, probe drives data bit 0
        dclr    SWDIO
        movi    r2, 0
        movi    r3, 0
        movi    r4, 32
        movi    r5, 0           ; parity accumulator
wr_loop:
        waitp   CLK_HI          ; R(13+k): sample data bit k
        in      r0
        andi    r0, SWDIO
        shr     r0, 1
        xor     r5, r0
        mov     r1, r3          ; 32-bit shift left-in at bit 31
        andi    r1, 1
        shl     r1, 15
        shr     r2, 1
        or      r2, r1
        shr     r3, 1
        mov     r1, r0
        shl     r1, 15
        or      r3, r1
        waitp   CLK_LO          ; F(13+k): probe sets the next data bit
        subi    r4, 1
        jnz     wr_loop
        waitp   CLK_HI          ; R45: parity bit
        in      r0
        andi    r0, SWDIO
        shr     r0, 1
        cmp     r5, r0          ; even parity: XOR(data) == parity bit
        jnz     wr_drop
        cmpi    r7, 0
        jz      wr_done
        sti     r2, r7          ; store the 32-bit value into the AP/DP word
        addi    r7, 1
        sti     r3, r7
        movi    r1, 10          ; mirror the last AP write into DP RDBUFF
        sti     r2, r1
        addi    r1, 1
        sti     r3, r1
wr_drop:
wr_done:
        jmp     main

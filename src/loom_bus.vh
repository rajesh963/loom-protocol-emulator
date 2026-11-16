`ifndef LOOM_BUS_VH
`define LOOM_BUS_VH
// ============================================================================
// LOOM shared definitions — FROZEN.  Do not change without Lead approval
// (changes here invalidate the assembler, ISA model, RTL and tests at once).
// ============================================================================

// ---- build-time parameters ----
`define NUM_THREADS     4
`define EQ_DEPTH        4      // events per thread
`define FIFO_DEPTH      8      // bytes per thread TX / RX FIFO
`define FIFO_AW         3
`define DMEM_WORDS      64
`define DMEM_AW         6
`define PMEM_AW         10     // 1024 words max (FF fallback build uses fewer)
`define INSN_W          32

// ---- instruction encoding ----
`define OPC_NOP     7'h00
`define OPC_HALT    7'h01
`define OPC_MOVI    7'h02
`define OPC_MOV     7'h03
`define OPC_ADD     7'h04
`define OPC_ADDI    7'h05
`define OPC_SUB     7'h06
`define OPC_SUBI    7'h07
`define OPC_AND     7'h08
`define OPC_ANDI    7'h09
`define OPC_OR      7'h0A
`define OPC_ORI     7'h0B
`define OPC_XOR     7'h0C
`define OPC_XORI    7'h0D
`define OPC_INV     7'h0E
`define OPC_SHL     7'h0F
`define OPC_SHR     7'h10
`define OPC_ROL     7'h11
`define OPC_ROR     7'h12
`define OPC_CMP     7'h13
`define OPC_CMPI    7'h14
`define OPC_CSRW    7'h15
`define OPC_CSRR    7'h16
`define OPC_OUT     7'h18
`define OPC_SET     7'h19
`define OPC_CLR     7'h1A
`define OPC_TOG     7'h1B
`define OPC_IN      7'h1C
`define OPC_DIR     7'h1D
`define OPC_DSET    7'h1E
`define OPC_DCLR    7'h1F
`define OPC_EMIT    7'h20
`define OPC_EMITA   7'h21
`define OPC_EMITD   7'h22
`define OPC_EMITE   7'h23
`define OPC_SETP    7'h24
`define OPC_SETM    7'h25
`define OPC_SETEM   7'h26
`define OPC_SETEC   7'h27
`define OPC_INAT    7'h28
`define OPC_WAITU   7'h29
`define OPC_WAITD   7'h2A
`define OPC_WAITP   7'h2B
`define OPC_LD      7'h30
`define OPC_ST      7'h31
`define OPC_LDI     7'h32
`define OPC_STI     7'h33
`define OPC_PUSH    7'h38
`define OPC_PULL    7'h39
`define OPC_PUSHI   7'h3A
`define OPC_JMP     7'h40
`define OPC_JZ      7'h41
`define OPC_JNZ     7'h42
`define OPC_JC      7'h43
`define OPC_JNC     7'h44
`define OPC_RPT     7'h48
`define OPC_EVSET   7'h49
`define OPC_EVWAIT  7'h4A
`define OPC_IRQ     7'h4B

// ---- CSR map: per-thread bank, +0x00..+0x0F (host addresses thread t at 0x40+16t+n) ----
`define CSR_TPERIOD   8'h00
`define CSR_TMASK     8'h01
`define CSR_TSTATUS   8'h02
`define CSR_TFIFO     8'h03
`define CSR_TNEXTT    8'h04
`define CSR_TEQ       8'h05
`define CSR_TEMASK    8'h06
`define CSR_TECOND    8'h07
`define CSR_TPC       8'h08

// ---- CSR map: global ----
`define CSR_POUT      8'h10
`define CSR_PDIR      8'h11
`define CSR_TMR       8'h12
`define CSR_EVT       8'h13
`define CSR_IRQ       8'h14
`define CSR_WCTRL     8'h15   // {13'b0, done_rst, oneshot, loop, start}
`define CSR_WADDR     8'h16   // DMEM word address of pattern buffer
`define CSR_WLEN      8'h17   // 1..64 patterns
`define CSR_WPER      8'h18   // symbol period, 12.4 fixed point
`define CSR_WSTATUS   8'h19   // {14'b0, busy, done}
`define CSR_CAPCTRL   8'h1A   // stretch
`define CSR_CAPSTAT   8'h1B   // stretch
`define CSR_GSTATUS   8'h1C   // {4'b0, thread_run_mask, 8'b0, irq}
`define CSR_SOFTRST   8'h20   // host-only: write 1 = soft reset

// ---- host command opcodes (SPI byte stream) ----
`define HCMD_SET_ADDR   8'h01
`define HCMD_WRITE_MEM  8'h02
`define HCMD_READ_MEM   8'h03
`define HCMD_WRITE_CSR  8'h04
`define HCMD_READ_CSR   8'h05
`define HCMD_TX_PUSH    8'h06   // host -> thread RX FIFO
`define HCMD_RX_POP     8'h07   // thread TX FIFO -> host
`define HCMD_SET_PC     8'h08
`define HCMD_RUN        8'h09
`define HCMD_STOP       8'h0A
`define HCMD_STATUS     8'h0B
`define HCMD_ACK_IRQ    8'h0C
`define HCMD_PING       8'h0D
`define HCMD_RESP_OK    8'h80
`define HCMD_RESP_BUSY  8'h81

`endif // LOOM_BUS_VH

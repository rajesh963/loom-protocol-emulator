// ============================================================================
// loom_core.v — LOOM core: 4-thread scheduler + datapath + event scheduler + CSRs
//
// Reference: spec/ARCHITECTURE.md §3 (execution model) and §4 (ISA).
// Owner: Lead.  sw/loom_model.py is the golden model and must match this cycle
// for cycle; any change here requires updating the spec + model + tests.
//
// Frozen timing model:
//   * one instruction issues per cycle, work-conserving round-robin over
//     threads 0..3; a thread issues when en & ~halt & stall==0 & insn ready
//   * PMEM is 1-cycle synchronous: the cycle a thread issues, the fetch of its
//     NEXT instruction starts (bypass path), so a single active thread sustains
//     ~1 IPC and four active threads sustain ~1 IPC aggregate
//   * an instruction that cannot execute (FIFO / host port / queue full) is
//     retried: stall<=1, PC not advanced, no side effects
//   * pins update from (a) OUT/SET/CLR/TOG/DIR ops, (b) fired scheduled events
//     (t0 has highest priority), (c) wave player (overrides everything).
//     Scheduled edges never depend on instruction issue jitter.
// ============================================================================
`default_nettype none
`include "loom_bus.vh"

module loom_core #(
    parameter NUM_THREADS = 4,
    parameter EQ_DEPTH    = 4,
    parameter FIFO_DEPTH  = 8,
    parameter DMEM_WORDS  = 64
) (
    input  wire        clk,
    input  wire        rst_n,
    // instruction memory (1-cycle synchronous read)
    output wire [9:0]  imem_addr,
    input  wire [31:0] imem_data,
    // data memory (combinational read, synchronous write)
    output reg  [5:0]  dmem_addr,
    output wire        dmem_we,
    output wire [15:0] dmem_wdata,
    input  wire [15:0] dmem_rdata,
    input  wire        dmem_denied,
    // pins
    output wire [15:0] pins_out,
    output wire [7:0]  pins_dir,
    input  wire [15:0] pins_in,
    // wave player
    output reg         wave_start,
    output reg         wave_loop,
    output reg         wave_oneshot,
    output reg  [5:0]  wave_addr,
    output reg  [6:0]  wave_len,
    output reg  [15:0] wave_period,
    input  wire        wave_busy,
    input  wire        wave_done,
    input  wire        wave_active,
    input  wire [15:0] wave_pins,
    // capture (stretch)
    output reg  [15:0] cap_ctrl,
    input  wire [15:0] cap_stat,
    // host CSR port
    input  wire        host_csrw_en,
    input  wire [7:0]  host_csrw_addr,
    input  wire [15:0] host_csrw_data,
    input  wire        host_csrr_en,
    input  wire [7:0]  host_csrr_addr,
    output wire [15:0] host_csrr_data,
    // host <-> byte FIFOs
    input  wire        host_rx_push,
    input  wire [1:0]  host_rx_tid,
    input  wire [7:0]  host_rx_data,
    output wire [3:0]  rx_full,
    input  wire        host_tx_pop,
    input  wire [1:0]  host_tx_tid,
    output wire [7:0]  host_tx_data,
    output wire [3:0]  tx_empty,
    // interrupt / status
    output reg  [15:0] irq_code,
    output wire [3:0]  thread_run_mask
);

  localparam FIFO_FULL = FIFO_DEPTH;   // bytes
  // pins 9:8 are the host link (MISO/IRQ): writable internally but never bonded
  localparam [15:0] PINS_OUT_DRIVABLE = 16'hFCFF;
  localparam EQ_TOT    = 16;           // NUM_THREADS * EQ_DEPTH

  // ==========================================================================
  // Global state
  // ==========================================================================
  reg [15:0] tmr;
  reg [15:0] pout;
  reg [7:0]  pdir;
  reg [7:0]  evt;
  reg [2:0]  wave_ctl_q;
  reg [15:0] cap_ctrl_q;

  // ==========================================================================
  // Per-thread state
  // ==========================================================================
  reg [9:0]  pc       [0:3];
  reg [15:0] regf     [0:31];
  reg        fz       [0:3];
  reg        fc       [0:3];
  reg        t_en     [0:3];
  reg        t_halt   [0:3];
  reg [15:0] stall    [0:3];
  reg [15:0] period   [0:3];
  reg [3:0]  pfrac    [0:3];
  reg [3:0]  wfrac    [0:3];   // waitd fractional accumulator
  reg [15:0] tmask    [0:3];
  reg [15:0] emask    [0:3];
  reg [15:0] econd    [0:3];
  reg [15:0] nextt    [0:3];
  reg [31:0] insn     [0:3];
  reg        i_rdy    [0:3];
  reg [7:0]  rep_left [0:3];
  reg        rep_arm  [0:3];
  reg        inat_arm [0:3];
  reg [15:0] inat_tgt [0:3];
  reg [1:0]  inat_rd  [0:3];

  // ==========================================================================
  // Event queues (per-thread FIFO, depth EQ_DEPTH)
  // ==========================================================================
  reg [15:0] eq_tgt  [0:EQ_TOT-1];
  reg [15:0] eq_val  [0:EQ_TOT-1];
  reg        eq_edge [0:EQ_TOT-1];
  reg        eq_val_i[0:EQ_TOT-1];
  reg [2:0]  eq_head [0:3];
  reg [2:0]  eq_cnt  [0:3];

  // ==========================================================================
  // Byte FIFOs (RX = host->thread, TX = thread->host), 4 x 8 bytes each
  // ==========================================================================
  reg [7:0] rx_mem [0:31];
  reg [2:0] rx_head[0:3], rx_tail[0:3];
  reg [3:0] rx_cnt [0:3];
  reg [7:0] tx_mem [0:31];
  reg [2:0] tx_head[0:3], tx_tail[0:3];
  reg [3:0] tx_cnt [0:3];

  // ==========================================================================
  // Scheduler
  // ==========================================================================
  reg [1:0] issue_ptr, fetch_ptr;
  reg [1:0] fetch_tid;
  reg       fetch_pend;
  reg [1:0] sel;
  reg       do_issue;
  integer   i, t;
  reg [1:0] cand;
  reg       found;

  integer ii;
  reg [1:0] cand_i;
  reg       found_i;
  always @* begin
    do_issue = 1'b0;
    sel      = 2'd0;
    found_i  = 1'b0;
    for (ii = 0; ii < NUM_THREADS; ii = ii + 1) begin
      cand_i = issue_ptr + ii[1:0];
      if (!found_i && t_en[cand_i] && !t_halt[cand_i] && (stall[cand_i] == 16'd0) &&
          (i_rdy[cand_i] || (fetch_pend && (fetch_tid == cand_i)))) begin
        found_i  = 1'b1;
        do_issue = 1'b1;
        sel      = cand_i;
      end
    end
  end

  wire [31:0] insn_i = (fetch_pend && (fetch_tid == sel) && !i_rdy[sel])
                       ? imem_data : insn[sel];
  wire [6:0]  opc   = insn_i[31:25];
  wire [2:0]  rd    = insn_i[24:22];
  wire [2:0]  rs    = insn_i[21:19];
  wire [15:0] imm   = insn_i[15:0];
  wire [7:0]  rdi   = {sel, rd};
  wire [7:0]  rsi   = {sel, rs};
  wire [15:0] src_a = regf[rdi];
  wire [15:0] src_b = regf[rsi];
  wire signed [15:0] sei = imm;

  // ---------------- execution control signals -------------------------------
  reg        stall_issue;
  reg [15:0] stall_set;
  reg [15:0] wb_data;
  reg        wb_en;
  reg        flag_en;
  reg        alu_z, alu_c;
  reg        npc_en;
  reg [5:0]  dmem_addr_i;
  reg        dmem_we_i;
  reg [15:0] dmem_wdata_i;
  reg        pin_op;
  reg [15:0] pin_val;
  reg        dir_op;
  reg [7:0]  dir_val;
  reg        csr_wr;
  reg [7:0]  csr_wr_addr;
  reg [15:0] csr_wr_val;
  reg        csr_rd;
  reg [7:0]  csr_rd_addr;
  reg        fifo_push, fifo_pull;
  reg        irq_op;
  reg [15:0] irq_val;
  reg        evset_op, evwait_op;
  reg [7:0]  evflag;
  reg        inat_do;
  reg [15:0] inat_delta;
  reg        ev_enq_t, ev_enq_edge;
  reg [15:0] ev_enq_val, ev_enq_time;
  reg [15:0] pc_rel;
  reg [9:0]  pc_nxt;
  reg        jump_taken;

  // arithmetic helpers
  wire [16:0] add_r  = {1'b0, src_a} + {1'b0, src_b};
  wire [16:0] sub_r  = {1'b0, src_a} - {1'b0, src_b};
  wire [16:0] addi_r = {1'b0, src_a} + {1'b0, imm};
  wire [16:0] subi_r = {1'b0, src_a} - {1'b0, imm};
  wire [15:0] shl_r  = src_a << imm[3:0];
  wire [15:0] shr_r  = src_a >> imm[3:0];
  wire [15:0] rol_r  = (imm[3:0] == 4'd0) ? src_a
                     : ((src_a << imm[3:0]) | (src_a >> (5'd16 - imm[3:0])));
  wire [15:0] ror_r  = (imm[3:0] == 4'd0) ? src_a
                     : ((src_a >> imm[3:0]) | (src_a << (5'd16 - imm[3:0])));

  // fractional WAITD
  wire [4:0]  wd_frac  = {1'b0, wfrac[sel]} + {1'b0, imm[3:0]};
  wire        wd_carry = wd_frac[4];
  wire [15:0] wd_total = imm[15:4] + {15'd0, wd_carry};

  // event target computation
  wire [4:0]  per_frac  = {1'b0, pfrac[sel]} + {1'b0, period[sel][3:0]};
  wire        per_carry = per_frac[4];
  wire [15:0] tgt_per   = nextt[sel] + {12'd0, period[sel][15:4]} + {15'd0, per_carry};
  wire [15:0] tgt_abs_r = imm;
  wire [15:0] tgt_rel_r = tmr + imm;
  wire [15:0] d_per     = tgt_per - tmr;
  wire [15:0] d_abs     = tgt_abs_r - tmr;
  wire [15:0] tgt_per_c = (d_per >= 16'h8000) ? (tmr + 16'd1) : tgt_per;
  wire [15:0] tgt_abs_c = (d_abs >= 16'h8000) ? (tmr + 16'd1) : tgt_abs_r;
  wire [15:0] tgt_rel_c = (imm == 16'd0) ? (tmr + 16'd1) : tgt_rel_r;

  wire [15:0] pin_set = pout |  imm;
  wire [15:0] pin_clr = pout & ~imm;
  wire [15:0] pin_tog = pout ^  imm;

  // does this thread issue next cycle? (used by fetch selection)
  reg  reissue_now;
  reg  hv [0:3];
  always @* begin
    reissue_now = do_issue && !stall_issue && (opc != `OPC_RPT) &&
                  rep_arm[sel] && (rep_left[sel] != 8'd0);
  end
  integer ih;
  always @* begin
    for (ih = 0; ih < NUM_THREADS; ih = ih + 1) begin
      if (do_issue && (sel == ih[1:0]) && !stall_issue)
        hv[ih] = reissue_now;
      else
        hv[ih] = i_rdy[ih] || (fetch_pend && (fetch_tid == ih[1:0]));
    end
  end

  // ==========================================================================
  // Decode / execute
  // ==========================================================================
  always @* begin
    stall_issue = 1'b0;
    stall_set   = 16'd0;
    wb_data     = 16'd0;
    wb_en       = 1'b0;
    flag_en     = 1'b0;
    alu_z       = 1'b0;
    alu_c       = 1'b0;
    npc_en      = 1'b1;
    dmem_addr_i = 6'd0;
    dmem_we_i   = 1'b0;
    dmem_wdata_i= 16'd0;
    pin_op      = 1'b0;
    pin_val     = 16'd0;
    dir_op      = 1'b0;
    dir_val     = 8'd0;
    csr_wr      = 1'b0;
    csr_wr_addr = 8'd0;
    csr_wr_val  = 16'd0;
    csr_rd      = 1'b0;
    csr_rd_addr = 8'd0;
    fifo_push   = 1'b0;
    fifo_pull   = 1'b0;
    irq_op      = 1'b0;
    irq_val     = 16'd0;
    evset_op    = 1'b0;
    evwait_op   = 1'b0;
    evflag      = 8'd0;
    inat_do     = 1'b0;
    inat_delta  = 16'd0;
    ev_enq_t    = 1'b0;
    ev_enq_edge = 1'b0;
    ev_enq_val  = 16'd0;
    ev_enq_time = 16'd0;
    pc_rel      = {6'd0, sei[9:0]};
    pc_nxt      = pc[sel];
    jump_taken  = 1'b0;

    if (do_issue) begin
      case (opc)
        `OPC_NOP: ;
        `OPC_HALT: npc_en = 1'b0;
        `OPC_MOVI: begin wb_en=1'b1; wb_data=imm; flag_en=1'b1; alu_z=(imm==16'd0); end
        `OPC_MOV:  begin wb_en=1'b1; wb_data=src_b; flag_en=1'b1; alu_z=(src_b==16'd0); end
        `OPC_ADD:  begin wb_en=1'b1; wb_data=add_r[15:0]; flag_en=1'b1; alu_z=(add_r[15:0]==16'd0); alu_c=add_r[16]; end
        `OPC_ADDI: begin wb_en=1'b1; wb_data=addi_r[15:0]; flag_en=1'b1; alu_z=(addi_r[15:0]==16'd0); alu_c=addi_r[16]; end
        `OPC_SUB:  begin wb_en=1'b1; wb_data=sub_r[15:0]; flag_en=1'b1; alu_z=(sub_r[15:0]==16'd0); alu_c=sub_r[16]; end
        `OPC_SUBI: begin wb_en=1'b1; wb_data=subi_r[15:0]; flag_en=1'b1; alu_z=(subi_r[15:0]==16'd0); alu_c=subi_r[16]; end
        `OPC_AND:  begin wb_en=1'b1; wb_data=src_a & src_b; flag_en=1'b1; alu_z=((src_a&src_b)==16'd0); end
        `OPC_ANDI: begin wb_en=1'b1; wb_data=src_a & imm;   flag_en=1'b1; alu_z=((src_a&imm)==16'd0); end
        `OPC_OR:   begin wb_en=1'b1; wb_data=src_a | src_b; flag_en=1'b1; alu_z=((src_a|src_b)==16'd0); end
        `OPC_ORI:  begin wb_en=1'b1; wb_data=src_a | imm;   flag_en=1'b1; alu_z=((src_a|imm)==16'd0); end
        `OPC_XOR:  begin wb_en=1'b1; wb_data=src_a ^ src_b; flag_en=1'b1; alu_z=((src_a^src_b)==16'd0); end
        `OPC_XORI: begin wb_en=1'b1; wb_data=src_a ^ imm;   flag_en=1'b1; alu_z=((src_a^imm)==16'd0); end
        `OPC_INV:  begin wb_en=1'b1; wb_data=~src_a;        flag_en=1'b1; alu_z=((~src_a)==16'd0); end
        `OPC_SHL:  begin wb_en=1'b1; wb_data=shl_r; flag_en=1'b1; alu_z=(shl_r==16'd0); end
        `OPC_SHR:  begin wb_en=1'b1; wb_data=shr_r; flag_en=1'b1; alu_z=(shr_r==16'd0); end
        `OPC_ROL:  begin wb_en=1'b1; wb_data=rol_r; end
        `OPC_ROR:  begin wb_en=1'b1; wb_data=ror_r; end
        `OPC_CMP:  begin flag_en=1'b1; alu_z=(sub_r[15:0]==16'd0); alu_c=sub_r[16]; end
        `OPC_CMPI: begin flag_en=1'b1; alu_z=(subi_r[15:0]==16'd0); alu_c=subi_r[16]; end
        `OPC_CSRW: begin csr_wr=1'b1; csr_wr_addr=imm[7:0]; csr_wr_val=src_b; end
        `OPC_CSRR: begin csr_rd=1'b1; csr_rd_addr=imm[7:0]; wb_en=1'b1; wb_data=csr_rdata; end
        `OPC_OUT:  begin pin_op=1'b1; pin_val=imm; end
        `OPC_SET:  begin pin_op=1'b1; pin_val=pin_set; end
        `OPC_CLR:  begin pin_op=1'b1; pin_val=pin_clr; end
        `OPC_TOG:  begin pin_op=1'b1; pin_val=pin_tog; end
        `OPC_IN:   begin wb_en=1'b1; wb_data=pins_in; end
        `OPC_DIR:  begin dir_op=1'b1; dir_val=imm[7:0]; end
        `OPC_DSET: begin dir_op=1'b1; dir_val=pdir | imm[7:0]; end
        `OPC_DCLR: begin dir_op=1'b1; dir_val=pdir & ~imm[7:0]; end
        `OPC_EMIT: begin
          if (eq_cnt[sel] >= EQ_DEPTH[2:0]) stall_issue = 1'b1;
          else begin ev_enq_t=1'b1; ev_enq_val=src_a; ev_enq_time=tgt_per_c; end
        end
        `OPC_EMITA: begin
          if (eq_cnt[sel] >= EQ_DEPTH[2:0]) stall_issue = 1'b1;
          else begin ev_enq_t=1'b1; ev_enq_val=src_a; ev_enq_time=tgt_abs_c; end
        end
        `OPC_EMITD: begin
          if (eq_cnt[sel] >= EQ_DEPTH[2:0]) stall_issue = 1'b1;
          else begin ev_enq_t=1'b1; ev_enq_val=src_a; ev_enq_time=tgt_rel_c; end
        end
        `OPC_EMITE: begin
          if (eq_cnt[sel] >= EQ_DEPTH[2:0]) stall_issue = 1'b1;
          else begin ev_enq_edge=1'b1; ev_enq_val=src_a; end
        end
        `OPC_SETP:  begin csr_wr=1'b1; csr_wr_addr=`CSR_TPERIOD; csr_wr_val=imm; end
        `OPC_SETM:  begin csr_wr=1'b1; csr_wr_addr=`CSR_TMASK;   csr_wr_val=imm; end
        `OPC_SETEM: begin csr_wr=1'b1; csr_wr_addr=`CSR_TEMASK;  csr_wr_val=imm; end
        `OPC_SETEC: begin csr_wr=1'b1; csr_wr_addr=`CSR_TECOND;  csr_wr_val=imm; end
        `OPC_INAT: begin
          inat_delta = imm - tmr;
          if (inat_delta == 16'd0) begin wb_en=1'b1; wb_data=pins_in; end
          else begin inat_do=1'b1; stall_set=inat_delta; end
        end
        `OPC_WAITU: begin
          if (imm == tmr) stall_set = 16'd0;
          else stall_set = (imm - tmr) - 16'd1;
        end
        `OPC_WAITD: begin
          if (wd_total == 16'd0) stall_set = 16'd0;
          else stall_set = wd_total - 16'd1;
        end
        `OPC_WAITP: begin
          if ((pins_in & imm[15:8]) != {8'd0, imm[7:0]}) stall_issue = 1'b1;
        end
        `OPC_LD: begin
          if (dmem_denied) stall_issue = 1'b1;
          else begin dmem_addr_i=imm[5:0]; wb_en=1'b1; wb_data=dmem_rdata; end
        end
        `OPC_ST: begin
          if (dmem_denied) stall_issue = 1'b1;
          else begin dmem_addr_i=imm[5:0]; dmem_we_i=1'b1; dmem_wdata_i=src_b; end
        end
        `OPC_LDI: begin
          if (dmem_denied) stall_issue = 1'b1;
          else begin dmem_addr_i=src_b[5:0]; wb_en=1'b1; wb_data=dmem_rdata; end
        end
        `OPC_STI: begin
          if (dmem_denied) stall_issue = 1'b1;
          else begin dmem_addr_i=src_a[5:0]; dmem_we_i=1'b1; dmem_wdata_i=src_b; end
        end
        `OPC_PUSH: begin
          if (tx_cnt[sel] == FIFO_FULL[3:0]) stall_issue = 1'b1;
          else fifo_push = 1'b1;
        end
        `OPC_PUSHI: begin
          if (tx_cnt[sel] == FIFO_FULL[3:0]) stall_issue = 1'b1;
          else fifo_push = 1'b1;
        end
        `OPC_PULL: begin
          if (rx_cnt[sel] == 4'd0) stall_issue = 1'b1;
          else begin fifo_pull=1'b1; wb_en=1'b1; wb_data={8'd0, rx_mem[{sel, rx_head[sel]}]}; end
        end
        `OPC_JMP: begin jump_taken=1'b1; end
        `OPC_JZ:  begin if (fz[sel])  jump_taken=1'b1; end
        `OPC_JNZ: begin if (!fz[sel]) jump_taken=1'b1; end
        `OPC_JC:  begin if (fc[sel])  jump_taken=1'b1; end
        `OPC_JNC: begin if (!fc[sel]) jump_taken=1'b1; end
        `OPC_RPT: ;
        `OPC_EVSET:  begin evset_op=1'b1; evflag=imm[7:0]; end
        `OPC_EVWAIT: begin
          if (evt[imm[2:0]]) begin evwait_op=1'b1; evflag=imm[7:0]; end
          else stall_issue = 1'b1;
        end
        `OPC_IRQ: begin irq_op=1'b1; irq_val={8'd0, imm[7:0]}; end
        default: ;
      endcase

      // host owns the CSR write port: retry
      if (csr_wr && host_csrw_en) stall_issue = 1'b1;

      // next PC (combinational): used both for writeback and for the fetch address
      pc_nxt = pc[sel];
      if ((opc != `OPC_RPT) && rep_arm[sel] && (rep_left[sel] != 8'd0))
        pc_nxt = pc[sel];                                    // re-issue: PC frozen
      else if (jump_taken)
        pc_nxt = pc[sel] + pc_rel[9:0];
      else if (npc_en)
        pc_nxt = pc[sel] + 10'd1;
    end
  end

  // ==========================================================================
  // CSR read mux
  // ==========================================================================
  reg [15:0] csr_rdata;
  reg [15:0] host_csrr_rdata;
  reg        csr_hbank;
  reg [1:0]  csr_ht;
  reg [7:0]  csr_ha;
  always @* begin
    csr_hbank = 1'b0;
    csr_ht    = 2'd0;
    csr_ha    = 8'd0;
    if ((host_csrr_addr >= 8'h40) && (host_csrr_addr < 8'h80)) begin
      csr_hbank = 1'b1;
      csr_ht    = host_csrr_addr[5:4];
      csr_ha    = {4'd0, host_csrr_addr[3:0]};
    end
  end

  // Two independent read ports: thread instructions read their OWN bank from
  // `csr_rd_addr` (0-latency); the host reads `host_csrr_addr` (thread banks at
  // 0x40+16t+n, globals at 0x10..0x1F).  These used to share one mux, which made
  // every host global-CSR read return whatever the last thread CSRR addressed
  // (VER-003, caught by the cocotb host suite).
  function [15:0] bank_read;
    input [1:0] t;
    input [7:0] a;
    begin
      case (a)
        `CSR_TPERIOD: bank_read = period[t];
        `CSR_TMASK:   bank_read = tmask[t];
        `CSR_TSTATUS: bank_read = {14'd0, t_halt[t], t_en[t]};
        `CSR_TFIFO:   bank_read = {4'd0, rx_cnt[t], tx_cnt[t], 2'd0,
                                   (tx_cnt[t]==FIFO_FULL[3:0]), (rx_cnt[t]==4'd0)};
        `CSR_TNEXTT:  bank_read = nextt[t];
        `CSR_TEQ:     bank_read = {13'd0, eq_cnt[t]};
        `CSR_TEMASK:  bank_read = emask[t];
        `CSR_TECOND:  bank_read = econd[t];
        `CSR_TPC:     bank_read = {6'd0, pc[t]};
        default:      bank_read = 16'd0;
      endcase
    end
  endfunction

  function [15:0] global_read;
    input [7:0] a;
    begin
      case (a)
        `CSR_POUT:    global_read = pout;
        `CSR_PDIR:    global_read = {8'd0, pdir};
        `CSR_TMR:     global_read = tmr;
        `CSR_EVT:     global_read = {8'd0, evt};
        `CSR_IRQ:     global_read = irq_code;
        `CSR_WCTRL:   global_read = {13'd0, wave_ctl_q};
        `CSR_WADDR:   global_read = {10'd0, wave_addr};
        `CSR_WLEN:    global_read = {9'd0, wave_len};
        `CSR_WPER:    global_read = wave_period;
        `CSR_WSTATUS: global_read = {14'd0, wave_busy, wave_done};
        `CSR_CAPCTRL: global_read = cap_ctrl;
        `CSR_CAPSTAT: global_read = cap_stat;
        `CSR_GSTATUS: global_read = {7'd0, t_en[3], t_en[2], t_en[1], t_en[0],
                                     4'd0, (irq_code != 16'd0)};
        default:      global_read = 16'd0;
      endcase
    end
  endfunction

  // thread-instruction view: own bank below 0x10, globals above
  always @* begin
    if (csr_rd_addr < 8'h10) csr_rdata = bank_read(sel, csr_rd_addr);
    else                     csr_rdata = global_read(csr_rd_addr);
  end

  // host view: thread banks 0x40..0x7F, globals 0x10..0x1F, thread-local 0x00..0x0F reads 0
  always @* begin
    if (csr_hbank)                   host_csrr_rdata = bank_read(csr_ht, csr_ha);
    else if (host_csrr_addr < 8'h10) host_csrr_rdata = 16'd0;
    else                             host_csrr_rdata = global_read(host_csrr_addr);
  end
  assign host_csrr_data = host_csrr_rdata;

  // ==========================================================================
  // Event firing (every cycle, independent of instruction issue)
  // ==========================================================================
  reg        fire [0:3];
  reg [15:0] fire_v[0:3];
  reg [3:0]  eq_h  [0:3];

  integer ie;
  always @* begin
    for (ie = 0; ie < NUM_THREADS; ie = ie + 1) begin
      eq_h[ie]   = (ie[1:0] * EQ_DEPTH[3:0]) + {1'b0, eq_head[ie]};
      fire[ie]   = 1'b0;
      fire_v[ie] = 16'd0;
      if (eq_val_i[eq_h[ie]]) begin
        if (eq_edge[eq_h[ie]]) begin
          if ((pins_in & emask[ie]) == (econd[ie] & emask[ie])) begin
            fire[ie]   = 1'b1;
            fire_v[ie] = eq_val[eq_h[ie]];
          end
        end else begin
          // fire when due, or when overdue (target is in the past under modular
          // 16-bit time). This guarantees an armed event is never dropped when
          // two entries share a target; scheduling lookahead must stay < 32768.
          if ((eq_tgt[eq_h[ie]] == tmr) ||
              ((eq_tgt[eq_h[ie]] - tmr) >= 16'h8000)) begin
            fire[ie]   = 1'b1;
            fire_v[ie] = eq_val[eq_h[ie]];
          end
        end
      end
    end
  end

  // ==========================================================================
  // Pin merge
  // ==========================================================================
  reg [15:0] npout;
  always @* begin
    npout = pout;
    if (do_issue && !stall_issue && pin_op) npout = pin_val;
    if (do_issue && !stall_issue && csr_wr && (csr_wr_addr == `CSR_POUT))
      npout = csr_wr_val;
    if (host_csrw_en && (host_csrw_addr == `CSR_POUT)) npout = host_csrw_data;
    if (fire[3]) npout = (npout & ~tmask[3]) | (fire_v[3] & tmask[3]);
    if (fire[2]) npout = (npout & ~tmask[2]) | (fire_v[2] & tmask[2]);
    if (fire[1]) npout = (npout & ~tmask[1]) | (fire_v[1] & tmask[1]);
    if (fire[0]) npout = (npout & ~tmask[0]) | (fire_v[0] & tmask[0]);
    // host-link bits are stored as 0 so CSR POUT reads are architecturally clean
    npout = npout & PINS_OUT_DRIVABLE;
  end

  assign pins_out = wave_active ? wave_pins : npout;
  assign pins_dir = pdir;

  // ==========================================================================
  // Sequential
  // ==========================================================================
  reg [3:0] eq_wr;
  reg [3:0] eq_tail;
  reg [3:0] pc_after_r;
  reg [3:0] fetch_idx;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tmr          <= 16'd0;
      pout         <= 16'd0;
      pdir         <= 8'd0;
      evt          <= 8'd0;
      irq_code     <= 16'd0;
      wave_ctl_q   <= 3'd0;
      wave_start   <= 1'b0;
      wave_loop    <= 1'b0;
      wave_oneshot <= 1'b0;
      wave_addr    <= 6'd0;
      wave_len     <= 7'd1;
      wave_period  <= 16'd16;
      cap_ctrl     <= 16'd0;
      cap_ctrl_q   <= 16'd0;
      issue_ptr    <= 2'd0;
      fetch_ptr    <= 2'd0;
      fetch_tid    <= 2'd0;
      fetch_pend   <= 1'b0;
      dmem_addr    <= 6'd0;
      for (t = 0; t < 4; t = t + 1) begin
        pc[t]       <= 10'd0;
        fz[t]       <= 1'b0;
        fc[t]       <= 1'b0;
        t_en[t]     <= 1'b0;
        t_halt[t]   <= 1'b1;
        stall[t]    <= 16'd0;
        period[t]   <= 16'd16;
        pfrac[t]    <= 4'd0;
        wfrac[t]    <= 4'd0;
        tmask[t]    <= 16'hFFFF;
        emask[t]    <= 16'd0;
        econd[t]    <= 16'd0;
        nextt[t]    <= 16'd0;
        insn[t]     <= 32'h0000_0001;
        i_rdy[t]    <= 1'b0;
        rep_left[t] <= 8'd0;
        rep_arm[t]  <= 1'b0;
        inat_arm[t] <= 1'b0;
        inat_tgt[t] <= 16'd0;
        inat_rd[t]  <= 2'd0;
        eq_head[t]  <= 3'd0;
        eq_cnt[t]   <= 3'd0;
        rx_head[t]  <= 3'd0;
        rx_tail[t]  <= 3'd0;
        rx_cnt[t]   <= 4'd0;
        tx_head[t]  <= 3'd0;
        tx_tail[t]  <= 3'd0;
        tx_cnt[t]   <= 4'd0;
      end
      for (i = 0; i < 32; i = i + 1) begin
        regf[i]   <= 16'd0;
        rx_mem[i] <= 8'd0;
        tx_mem[i] <= 8'd0;
      end
      for (i = 0; i < EQ_TOT; i = i + 1) begin
        eq_tgt[i]   <= 16'd0;
        eq_val[i]   <= 16'd0;
        eq_edge[i]  <= 1'b0;
        eq_val_i[i] <= 1'b0;
      end
    end else begin
      wave_start <= 1'b0;
      tmr        <= tmr + 16'd1;

      // ---------------- capture returned instruction -----------------------
      if (fetch_pend) begin
        insn[fetch_tid] <= imem_data;
        if (!(do_issue && !stall_issue && (sel == fetch_tid)))
          i_rdy[fetch_tid] <= 1'b1;
      end

      // ---------------- issue ---------------------------------------------
      if (do_issue) begin
        issue_ptr <= sel + 2'd1;
        if (!stall_issue) begin
          if (wb_en) regf[rdi] <= wb_data;
          if (flag_en) begin
            fz[sel] <= alu_z;
            fc[sel] <= alu_c;
          end
          i_rdy[sel] <= 1'b0;

          // PC update
          pc[sel] <= pc_nxt;

          if (stall_set != 16'd0) stall[sel] <= stall_set;

          // RPT
          if (opc == `OPC_RPT) begin
            rep_left[sel] <= imm[7:0];
            rep_arm[sel]  <= 1'b1;
          end else if (rep_arm[sel]) begin
            if (rep_left[sel] != 8'd0) begin
              rep_left[sel] <= rep_left[sel] - 8'd1;
              i_rdy[sel]    <= 1'b1;
            end else begin
              rep_arm[sel]  <= 1'b0;
            end
          end

          // WAITD fractional accumulate
          if (opc == `OPC_WAITD) wfrac[sel] <= wd_frac[3:0];

          // INAT arm + register index
          if (inat_do) begin
            inat_arm[sel] <= 1'b1;
            inat_tgt[sel] <= tmr + inat_delta;
            inat_rd[sel]  <= rd[1:0];
          end

          // periodic enqueue: advance NEXTT + fractional accumulator
          if (ev_enq_t || ev_enq_edge) begin
            if (ev_enq_t) begin
              nextt[sel] <= ev_enq_time;
              pfrac[sel] <= per_frac[3:0];
            end
            eq_tail = (eq_head[sel] + eq_cnt[sel]) & 3'd3;
            eq_wr   = {sel, eq_tail[1:0]};
            eq_tgt[eq_wr]   <= ev_enq_time;
            eq_val[eq_wr]   <= ev_enq_val;
            eq_edge[eq_wr]  <= ev_enq_edge;
            eq_val_i[eq_wr] <= 1'b1;
          end

          // FIFO push/pull
          if (fifo_push) begin
            tx_mem[{sel, tx_tail[sel]}] <= (opc == `OPC_PUSHI) ? imm[7:0] : src_a[7:0];
            tx_tail[sel] <= tx_tail[sel] + 3'd1;
            tx_cnt[sel]  <= tx_cnt[sel] + 4'd1;
          end
          if (fifo_pull) begin
            rx_head[sel] <= rx_head[sel] + 3'd1;
            rx_cnt[sel]  <= rx_cnt[sel] - 4'd1;
          end

          if (evset_op)  evt[evflag[2:0]] <= 1'b1;
          if (evwait_op) evt[evflag[2:0]] <= 1'b0;
          if (irq_op)    irq_code <= irq_val;

          if (opc == `OPC_HALT) t_halt[sel] <= 1'b1;
        end else begin
          // cannot execute: keep instruction, retry after one cycle
          i_rdy[sel] <= 1'b1;
          stall[sel] <= 16'd1;
        end
      end

      // ---------------- event queue pop / occupancy ------------------------
      // A thread may enqueue and fire in the same cycle (its own pop frees the
      // slot); occupancy must therefore be updated jointly, not twice.
      for (t = 0; t < 4; t = t + 1) begin
        if (fire[t]) begin
          eq_val_i[eq_h[t]] <= 1'b0;
          eq_head[t]        <= (eq_head[t] + 3'd1) & 3'd3;
        end
        if (do_issue && !stall_issue && (sel == t[1:0]) && (ev_enq_t || ev_enq_edge)) begin
          eq_cnt[t] <= fire[t] ? eq_cnt[t] : (eq_cnt[t] + 3'd1);
        end else if (fire[t]) begin
          eq_cnt[t] <= eq_cnt[t] - 3'd1;
        end
      end

      // ---------------- INAT timed sample ----------------------------------
      for (t = 0; t < 4; t = t + 1) begin
        if (inat_arm[t] && (tmr == inat_tgt[t])) begin
          regf[{t[1:0], inat_rd[t]}] <= pins_in;
          inat_arm[t] <= 1'b0;
        end
      end

      // ---------------- stall countdown ------------------------------------
      for (t = 0; t < 4; t = t + 1) begin
        if ((stall[t] != 16'd0) && !(do_issue && (sel == t[1:0]) && stall_issue))
          stall[t] <= stall[t] - 16'd1;
      end

      // ---------------- fetch ----------------------------------------------
      if (do_fetch) begin
        fetch_tid  <= fsel;
        fetch_pend <= 1'b1;
        fetch_ptr  <= fsel + 2'd1;
      end else begin
        fetch_pend <= 1'b0;
      end

      // ---------------- pins / direction -----------------------------------
      pout <= npout;
      if (do_issue && !stall_issue && dir_op) pdir <= dir_val;
      if (do_issue && !stall_issue && csr_wr && (csr_wr_addr == `CSR_PDIR))
        pdir <= csr_wr_val[7:0];
      if (host_csrw_en && (host_csrw_addr == `CSR_PDIR))
        pdir <= host_csrw_data[7:0];

      // ---------------- host CSR writes ------------------------------------
      if (host_csrw_en) begin
        if ((host_csrw_addr >= 8'h40) && (host_csrw_addr < 8'h80)) begin
          case (host_csrw_addr[3:0])
            `CSR_TPERIOD: period[host_csrw_addr[5:4]] <= host_csrw_data;
            `CSR_TMASK:   tmask [host_csrw_addr[5:4]] <= host_csrw_data;
            `CSR_TEMASK:  emask [host_csrw_addr[5:4]] <= host_csrw_data;
            `CSR_TECOND:  econd [host_csrw_addr[5:4]] <= host_csrw_data;
            `CSR_TPC:     pc    [host_csrw_addr[5:4]] <= host_csrw_data[9:0];
            default: ;
          endcase
        end else begin
          case (host_csrw_addr)
            `CSR_EVT:     evt <= host_csrw_data[7:0];
            `CSR_IRQ:     irq_code <= host_csrw_data;
            `CSR_WCTRL:   begin
              wave_ctl_q   <= host_csrw_data[2:0];
              wave_loop    <= host_csrw_data[1];
              wave_oneshot <= host_csrw_data[2];
              if (host_csrw_data[0]) wave_start <= 1'b1;
            end
            `CSR_WADDR:   wave_addr   <= host_csrw_data[5:0];
            `CSR_WLEN:    wave_len    <= host_csrw_data[6:0];
            `CSR_WPER:    wave_period <= host_csrw_data;
            `CSR_CAPCTRL: cap_ctrl    <= host_csrw_data;
            8'h30: begin  // RUNMASK
              for (t = 0; t < 4; t = t + 1) begin
                if (host_csrw_data[t]) begin
                  t_en[t]   <= 1'b1;
                  t_halt[t] <= 1'b0;
                  stall[t]  <= 16'd0;
                  i_rdy[t]  <= 1'b0;
                  nextt[t]  <= tmr;
                  inat_arm[t] <= 1'b0;
                end
              end
            end
            8'h31: begin  // STOPMASK
              for (t = 0; t < 4; t = t + 1)
                if (host_csrw_data[t]) t_en[t] <= 1'b0;
            end
            8'h20: begin  // soft reset
              for (t = 0; t < 4; t = t + 1) begin
                pc[t]      <= 10'd0;
                fz[t]      <= 1'b0;
                fc[t]      <= 1'b0;
                t_en[t]    <= 1'b0;
                t_halt[t]  <= 1'b1;
                stall[t]   <= 16'd0;
                period[t]  <= 16'd16;
                pfrac[t]   <= 4'd0;
                wfrac[t]   <= 4'd0;
                tmask[t]   <= 16'hFFFF;
                emask[t]   <= 16'd0;
                econd[t]   <= 16'd0;
                nextt[t]   <= 16'd0;
                i_rdy[t]   <= 1'b0;
                rep_left[t]<= 8'd0;
                rep_arm[t] <= 1'b0;
                inat_arm[t]<= 1'b0;
                eq_head[t] <= 3'd0;
                eq_cnt[t]  <= 3'd0;
                rx_head[t] <= 3'd0; rx_tail[t] <= 3'd0; rx_cnt[t] <= 4'd0;
                tx_head[t] <= 3'd0; tx_tail[t] <= 3'd0; tx_cnt[t] <= 4'd0;
              end
              pout <= 16'd0; pdir <= 8'd0; evt <= 8'd0; irq_code <= 16'd0;
              for (i = 0; i < EQ_TOT; i = i + 1) eq_val_i[i] <= 1'b0;
            end
            default: ;
          endcase
        end
      end

      // ---------------- instruction CSR writes -----------------------------
      if (do_issue && !stall_issue && csr_wr) begin
        if (csr_wr_addr < 8'h10) begin
          case (csr_wr_addr)
            `CSR_TPERIOD: period[sel] <= csr_wr_val;
            `CSR_TMASK:   tmask [sel] <= csr_wr_val;
            `CSR_TEMASK:  emask [sel] <= csr_wr_val;
            `CSR_TECOND:  econd [sel] <= csr_wr_val;
            `CSR_TPC:     pc    [sel] <= csr_wr_val[9:0];
            default: ;
          endcase
        end else begin
          case (csr_wr_addr)
            `CSR_EVT:    evt <= csr_wr_val[7:0];
            `CSR_IRQ:    irq_code <= csr_wr_val;
            `CSR_WCTRL:  begin
              wave_ctl_q   <= csr_wr_val[2:0];
              wave_loop    <= csr_wr_val[1];
              wave_oneshot <= csr_wr_val[2];
              if (csr_wr_val[0]) wave_start <= 1'b1;
            end
            `CSR_WADDR:  wave_addr   <= csr_wr_val[5:0];
            `CSR_WLEN:   wave_len    <= csr_wr_val[6:0];
            `CSR_WPER:   wave_period <= csr_wr_val;
            `CSR_CAPCTRL:cap_ctrl    <= csr_wr_val;
            default: ;
          endcase
        end
      end

      // ---------------- host FIFO ports ------------------------------------
      if (host_rx_push && (rx_cnt[host_rx_tid] != FIFO_FULL[3:0])) begin
        rx_mem[{host_rx_tid, rx_tail[host_rx_tid]}] <= host_rx_data;
        rx_tail[host_rx_tid] <= rx_tail[host_rx_tid] + 3'd1;
        rx_cnt[host_rx_tid]  <= rx_cnt[host_rx_tid] + 4'd1;
      end
      if (host_tx_pop && (tx_cnt[host_tx_tid] != 4'd0)) begin
        tx_head[host_tx_tid] <= tx_head[host_tx_tid] + 3'd1;
        tx_cnt[host_tx_tid]  <= tx_cnt[host_tx_tid] - 4'd1;
      end
    end
  end

  // ==========================================================================
  // Fetch sideband
  // ==========================================================================
  wire [9:0] pc_after_w = tpc_jump                  ? csr_wr_val[9:0] :
                          (do_issue && !stall_issue) ? pc_nxt : pc[sel];
  wire [9:0] fetch_addr_w = (do_fetch) ? ((do_issue && (sel == fsel)) ? pc_after_w
                                                                     : pc[fsel])
                                       : 10'd0;
  reg [1:0] fsel;
  reg       do_fetch;

  integer ifc;
  reg [1:0] cand_f;
  reg       found_f;
  always @* begin
    do_fetch = 1'b0;
    fsel     = 2'd0;
    found_f  = 1'b0;
    for (ifc = 0; ifc < NUM_THREADS; ifc = ifc + 1) begin
      cand_f = fetch_ptr + ifc[1:0];
      if (!found_f && t_en[cand_f] && !t_halt[cand_f] && !hv[cand_f] &&
          !((fetch_pend && (fetch_tid == cand_f)) &&
            !(do_issue && !stall_issue && (sel == cand_f)))) begin
        found_f  = 1'b1;
        do_fetch = 1'b1;
        fsel     = cand_f;
      end
    end
  end

  // move fetch state into the sequential block via these wires
  wire [9:0] fetch_addr = fetch_addr_w;

  // A CSRW to CSR_TPC is a computed jump: the fetch started in the same cycle
  // must target the NEW PC, not pc+1, otherwise the buffer captures the wrong
  // instruction (toolchain co-sim found this).
  wire tpc_jump = do_issue && !stall_issue && csr_wr &&
                  (csr_wr_addr == `CSR_TPC);

  // PMEM is a 1-cycle synchronous memory: the address must be presented during
  // the SAME cycle as the fetch decision, so drive it combinationally.
  reg  [9:0] imem_hold;
  assign imem_addr = do_fetch ? fetch_addr : imem_hold;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) imem_hold <= 10'd0;
    else if (do_fetch) imem_hold <= fetch_addr;

  // ==========================================================================
  // Outputs
  // ==========================================================================
  assign dmem_we    = do_issue && !stall_issue && dmem_we_i;
  assign dmem_wdata = dmem_wdata_i;

  assign rx_full = { (rx_cnt[3] == FIFO_FULL[3:0]), (rx_cnt[2] == FIFO_FULL[3:0]),
                     (rx_cnt[1] == FIFO_FULL[3:0]), (rx_cnt[0] == FIFO_FULL[3:0]) };
  assign tx_empty = { (tx_cnt[3] == 4'd0), (tx_cnt[2] == 4'd0),
                      (tx_cnt[1] == 4'd0), (tx_cnt[0] == 4'd0) };
  assign host_tx_data = tx_mem[{host_tx_tid, tx_head[host_tx_tid]}];
  assign thread_run_mask = {t_en[3], t_en[2], t_en[1], t_en[0]};

endmodule
`default_nettype wire

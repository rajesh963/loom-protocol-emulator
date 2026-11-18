// ============================================================================
//  formal/h_sched.v — bounded formal proof of event-scheduler exactness
//
//  Instrumentation: yosys does not resolve hierarchical references into a
//  submodule's internal wire/regs (it would silently create undriven wires), so
//  run_formal.py generates formal/build/loom_core_probe.v — a copy of
//  rtl/loom_core.v with only read-only observation output ports added (all
//  logic identical; only `assign dbg_* = …` statements are appended).  This
//  harness instantiates that wrapper and observes the scheduler directly.
//
//  Claim (BMC from generated reset, arbitrary external stimulus):
//    "An accepted timed event fires *exactly* in the cycle where TMR equals its
//     recorded due time - never one cycle late, never one cycle early - and an
//     accepted edge-triggered event fires exactly in the cycle where the pin
//     condition holds; the due time recorded at enqueue is exact (periodic
//     emit incl. the 12.4 PFRAC carry, absolute emita, relative emitd); the
//     fired value reaches pins_out in that same cycle when it is the only pin
//     cause."
//
//  Cycle-local invariants (checked every post-reset step, hence valid for every
//  program/stimulus explored within the bound):
//    P1 due => fire       : head timed event, head_tgt == TMR => fire[ie]
//    P2 future => no fire : head timed event, lookahead in (0,0x8000)
//                           => !fire[ie]
//    P3 edge exact        : head edge event fires iff masked pins_in == TECOND
//    P4 fired value       : fire[ie] => fire_v[ie] == head value
//    P5 enqueue due time  : accepted emit/emita/emitd records exactly the
//                           documented due time (incl. PFRAC carry / clamp)
//    P6 same-cycle apply  : when thread 0's event is the only pin cause,
//                           pins_out == merge(pout, fire_v[0], tmask[0]) and
//                           pins_out == npout
//
//  Assumptions / honest scope (not claimed):
//    * firmware must enqueue a timed event with lookahead < 0x8000 and before
//      its due cycle (a late enqueue is recovered as "overdue" by design;
//      P1/P2 cover the due and future cycles, not the recovery path);
//    * the queue must have room: a full queue stalls the emitting instruction
//      (no event is dropped) - the enqueue properties' antecedent excludes
//      stall_issue;
//    * pins_in/DMEM/imem/wave/host ports are (* anyseq *); P6 excludes
//      competing pin writers (instruction pin op / CSR POUT / host POUT /
//      wave / another thread's fire) from its antecedent, while P1-P5 hold
//      under all stimulus;
//    * bounded depth k per formal/results.txt (cycle-local invariants prove
//      each cycle's behaviour; an unbounded induction is future work).
// ============================================================================
`default_nettype none
`include "loom_bus.vh"
module h_sched(input wire clk);

  reg [4:0] rst_cnt = 0;
  wire rst_n = (rst_cnt == 5'd6);
  always @(posedge clk) if (rst_cnt != 5'd6) rst_cnt <= rst_cnt + 5'd1;

  // Scheduler-exercise instruction stream: one thread runs EMIT / EMITD /
  // EMITA / EMITE in rotation so BMC actually reaches enqueue/fire states
  // (declared as a non-vacuity stimulus assumption for this bounded proof).
  reg [31:0] imem_data = {`OPC_NOP, 25'd0};
  reg [7:0]  prog_cyc = 8'd0;
  always @(posedge clk) begin
    prog_cyc <= prog_cyc + 8'd1;
    case (prog_cyc[2:1])
      2'd0: imem_data <= {`OPC_EMIT,  3'd0, 3'd0, 3'd0, 16'd0};  // emit  r0
      2'd1: imem_data <= {`OPC_EMITD, 3'd0, 3'd0, 3'd0, 16'd2};  // emitd r0,2
      2'd2: imem_data <= {`OPC_EMITA, 3'd0, 3'd0, 3'd0, 16'd8};  // emita r0,8
      default: imem_data <= {`OPC_EMITE, 3'd0, 3'd0, 3'd0, 16'd0};// emite r0
    endcase
  end

  (* anyseq *) wire [15:0] dmem_rdata;
  (* anyseq *) wire [15:0] pins_in;
  // The scheduler proof runs one thread: the harness itself issues exactly one
  // host RUN (CSR 0x30 = mask 0x1) after reset, then leaves the core to fetch
  // an arbitrary instruction stream.  Unused host/wave inputs are tied low so
  // the only scheduler stimulus is instructions + pins_in.
  wire        wave_busy = 1'b0, wave_done = 1'b0, wave_active = 1'b0;
  wire [15:0] wave_pins = 16'd0, cap_stat = 16'd0;
  wire        host_csrr_en = 1'b0;
  wire [7:0]  host_csrr_addr = 8'd0;
  wire        host_rx_push = 1'b0, host_tx_pop = 1'b0;
  wire [1:0]  host_rx_tid = 2'd0, host_tx_tid = 2'd0;
  wire [7:0]  host_rx_data = 8'd0;
  reg         host_csrw_en = 1'b0;
  reg  [7:0]  host_csrw_addr = 8'd0;
  reg  [15:0] host_csrw_data = 16'd0;

  wire [9:0]  imem_addr;
  wire [5:0]  dmem_addr;
  wire        dmem_we;
  wire [15:0] dmem_wdata;
  wire [15:0] pins_out;
  wire [7:0]  pins_dir;
  wire        wave_start, wave_loop, wave_oneshot;
  wire [5:0]  wave_addr;
  wire [6:0]  wave_len;
  wire [15:0] wave_period;
  wire [15:0] cap_ctrl;
  wire [15:0] host_csrr_data;
  wire [3:0]  rx_full, tx_empty;
  wire [15:0] irq_code;
  wire [3:0]  thread_run_mask;

  // observation ports of the generated formal wrapper
  wire [15:0] dbg_tmr, dbg_pout, dbg_npout, dbg_imm, dbg_src_a;
  wire [15:0] dbg_ev_enq_val, dbg_ev_enq_time, dbg_csr_wr_val;
  wire [3:0]  dbg_fire;
  wire [63:0] dbg_fire_v, dbg_period, dbg_nextt, dbg_tmask, dbg_emask, dbg_econd;
  wire [2:0]  dbg_sel;
  wire [6:0]  dbg_opc;
  wire [7:0]  dbg_csr_wr_addr;
  wire        dbg_do_issue, dbg_stall_issue, dbg_pin_op, dbg_csr_wr;
  wire        dbg_ev_enq_t, dbg_ev_enq_edge;
  wire [11:0] dbg_eq_head, dbg_eq_cnt;
  wire [255:0] dbg_eq_tgt, dbg_eq_val;
  wire [15:0] dbg_eq_edge, dbg_eq_vali;
  wire [15:0] dbg_pfrac;

  loom_core_probe #(.NUM_THREADS(4), .EQ_DEPTH(4), .FIFO_DEPTH(8), .DMEM_WORDS(64)) dut (
      .clk(clk), .rst_n(rst_n),
      .imem_addr(imem_addr), .imem_data(imem_data),
      .dmem_addr(dmem_addr), .dmem_we(dmem_we), .dmem_wdata(dmem_wdata),
      .dmem_rdata(dmem_rdata), .dmem_denied(1'b0),
      .pins_out(pins_out), .pins_dir(pins_dir), .pins_in(pins_in),
      .wave_start(wave_start), .wave_loop(wave_loop), .wave_oneshot(wave_oneshot),
      .wave_addr(wave_addr), .wave_len(wave_len), .wave_period(wave_period),
      .wave_busy(wave_busy), .wave_done(wave_done),
      .wave_active(wave_active), .wave_pins(wave_pins),
      .cap_ctrl(cap_ctrl), .cap_stat(cap_stat),
      .host_csrw_en(host_csrw_en), .host_csrw_addr(host_csrw_addr),
      .host_csrw_data(host_csrw_data),
      .host_csrr_en(host_csrr_en), .host_csrr_addr(host_csrr_addr),
      .host_csrr_data(host_csrr_data),
      .host_rx_push(host_rx_push), .host_rx_tid(host_rx_tid),
      .host_rx_data(host_rx_data), .rx_full(rx_full),
      .host_tx_pop(host_tx_pop), .host_tx_tid(host_tx_tid),
      .host_tx_data(), .tx_empty(tx_empty),
      .irq_code(irq_code), .thread_run_mask(thread_run_mask),
      .dbg_tmr(dbg_tmr), .dbg_fire(dbg_fire), .dbg_fire_v(dbg_fire_v),
      .dbg_pout(dbg_pout), .dbg_npout(dbg_npout), .dbg_sel(dbg_sel),
      .dbg_opc(dbg_opc), .dbg_imm(dbg_imm), .dbg_src_a(dbg_src_a),
      .dbg_do_issue(dbg_do_issue), .dbg_stall_issue(dbg_stall_issue),
      .dbg_pin_op(dbg_pin_op), .dbg_csr_wr(dbg_csr_wr),
      .dbg_csr_wr_addr(dbg_csr_wr_addr), .dbg_csr_wr_val(dbg_csr_wr_val),
      .dbg_ev_enq_t(dbg_ev_enq_t), .dbg_ev_enq_edge(dbg_ev_enq_edge),
      .dbg_ev_enq_val(dbg_ev_enq_val), .dbg_ev_enq_time(dbg_ev_enq_time),
      .dbg_eq_head(dbg_eq_head), .dbg_eq_cnt(dbg_eq_cnt),
      .dbg_eq_tgt(dbg_eq_tgt), .dbg_eq_val(dbg_eq_val),
      .dbg_eq_edge(dbg_eq_edge), .dbg_eq_vali(dbg_eq_vali),
      .dbg_period(dbg_period), .dbg_nextt(dbg_nextt), .dbg_pfrac(dbg_pfrac),
      .dbg_tmask(dbg_tmask), .dbg_emask(dbg_emask), .dbg_econd(dbg_econd));

  reg post_reset = 1'b0;
  always @(posedge clk) post_reset <= rst_n;

  // one host RUN (thread 0 only) immediately after reset
  reg run_sent = 1'b0;
  always @(posedge clk) begin
    host_csrw_en   <= 1'b0;
    if (!rst_n) begin
      run_sent       <= 1'b0;
      host_csrw_addr <= 8'd0;
      host_csrw_data <= 16'd0;
    end else if (post_reset && !run_sent) begin
      run_sent       <= 1'b1;
      host_csrw_en   <= 1'b1;
      host_csrw_addr <= 8'h30;
      host_csrw_data <= 16'h0001;
    end
  end


  // ---- thread 0: head selection from the observation ports --------------
  wire [2:0] sh0 = dbg_eq_head[0 +: 3];
  wire       hv0 = (sh0 == 3'd0) ? dbg_eq_vali[0] :
                     (sh0 == 3'd1) ? dbg_eq_vali[1] :
                     (sh0 == 3'd2) ? dbg_eq_vali[2] : dbg_eq_vali[3];
  wire       he0 = (sh0 == 3'd0) ? dbg_eq_edge[0] :
                     (sh0 == 3'd1) ? dbg_eq_edge[1] :
                     (sh0 == 3'd2) ? dbg_eq_edge[2] : dbg_eq_edge[3];
  wire [15:0] ht0 = (sh0 == 3'd0) ? dbg_eq_tgt[0 +: 16] :
                      (sh0 == 3'd1) ? dbg_eq_tgt[16 +: 16] :
                      (sh0 == 3'd2) ? dbg_eq_tgt[32 +: 16] : dbg_eq_tgt[48 +: 16];
  wire [15:0] hval0 = (sh0 == 3'd0) ? dbg_eq_val[0 +: 16] :
                        (sh0 == 3'd1) ? dbg_eq_val[16 +: 16] :
                        (sh0 == 3'd2) ? dbg_eq_val[32 +: 16] : dbg_eq_val[48 +: 16];
  wire [15:0] emask0 = dbg_emask[0 +: 16];
  wire [15:0] econd0 = dbg_econd[0 +: 16];
  wire [15:0] fv0 = dbg_fire_v[0 +: 16];
  always @(posedge clk) if (post_reset) begin
    if (hv0) begin
      if (he0) begin
        // P3: edge event fires iff the masked pin sample matches TECOND
        if ((pins_in & emask0) == (econd0 & emask0)) begin
          assert (dbg_fire[0]);                      // fires this cycle
          assert (fv0 == hval0);                   // P4
        end else begin
          assert (!dbg_fire[0]);                     // no early/late edge fire
        end
      end else begin
        // P1: due this cycle => must fire this cycle (never late)
        if (ht0 == dbg_tmr) begin
          assert (dbg_fire[0]);
          assert (fv0 == hval0);                   // P4
        end
        // P2: due later (lookahead in 0x0001..0x7FFF) => must not fire early
        if ((ht0 != dbg_tmr) && ((ht0 - dbg_tmr) < 16'h8000)) begin
          assert (!dbg_fire[0]);
        end
      end
    end
  end

  // ---- thread 1: head selection from the observation ports --------------
  wire [2:0] sh1 = dbg_eq_head[3 +: 3];
  wire       hv1 = (sh1 == 3'd0) ? dbg_eq_vali[4] :
                     (sh1 == 3'd1) ? dbg_eq_vali[5] :
                     (sh1 == 3'd2) ? dbg_eq_vali[6] : dbg_eq_vali[7];
  wire       he1 = (sh1 == 3'd0) ? dbg_eq_edge[4] :
                     (sh1 == 3'd1) ? dbg_eq_edge[5] :
                     (sh1 == 3'd2) ? dbg_eq_edge[6] : dbg_eq_edge[7];
  wire [15:0] ht1 = (sh1 == 3'd0) ? dbg_eq_tgt[64 +: 16] :
                      (sh1 == 3'd1) ? dbg_eq_tgt[80 +: 16] :
                      (sh1 == 3'd2) ? dbg_eq_tgt[96 +: 16] : dbg_eq_tgt[112 +: 16];
  wire [15:0] hval1 = (sh1 == 3'd0) ? dbg_eq_val[64 +: 16] :
                        (sh1 == 3'd1) ? dbg_eq_val[80 +: 16] :
                        (sh1 == 3'd2) ? dbg_eq_val[96 +: 16] : dbg_eq_val[112 +: 16];
  wire [15:0] emask1 = dbg_emask[16 +: 16];
  wire [15:0] econd1 = dbg_econd[16 +: 16];
  wire [15:0] fv1 = dbg_fire_v[16 +: 16];
  always @(posedge clk) if (post_reset) begin
    if (hv1) begin
      if (he1) begin
        // P3: edge event fires iff the masked pin sample matches TECOND
        if ((pins_in & emask1) == (econd1 & emask1)) begin
          assert (dbg_fire[1]);                      // fires this cycle
          assert (fv1 == hval1);                   // P4
        end else begin
          assert (!dbg_fire[1]);                     // no early/late edge fire
        end
      end else begin
        // P1: due this cycle => must fire this cycle (never late)
        if (ht1 == dbg_tmr) begin
          assert (dbg_fire[1]);
          assert (fv1 == hval1);                   // P4
        end
        // P2: due later (lookahead in 0x0001..0x7FFF) => must not fire early
        if ((ht1 != dbg_tmr) && ((ht1 - dbg_tmr) < 16'h8000)) begin
          assert (!dbg_fire[1]);
        end
      end
    end
  end

  // ---- thread 2: head selection from the observation ports --------------
  wire [2:0] sh2 = dbg_eq_head[6 +: 3];
  wire       hv2 = (sh2 == 3'd0) ? dbg_eq_vali[8] :
                     (sh2 == 3'd1) ? dbg_eq_vali[9] :
                     (sh2 == 3'd2) ? dbg_eq_vali[10] : dbg_eq_vali[11];
  wire       he2 = (sh2 == 3'd0) ? dbg_eq_edge[8] :
                     (sh2 == 3'd1) ? dbg_eq_edge[9] :
                     (sh2 == 3'd2) ? dbg_eq_edge[10] : dbg_eq_edge[11];
  wire [15:0] ht2 = (sh2 == 3'd0) ? dbg_eq_tgt[128 +: 16] :
                      (sh2 == 3'd1) ? dbg_eq_tgt[144 +: 16] :
                      (sh2 == 3'd2) ? dbg_eq_tgt[160 +: 16] : dbg_eq_tgt[176 +: 16];
  wire [15:0] hval2 = (sh2 == 3'd0) ? dbg_eq_val[128 +: 16] :
                        (sh2 == 3'd1) ? dbg_eq_val[144 +: 16] :
                        (sh2 == 3'd2) ? dbg_eq_val[160 +: 16] : dbg_eq_val[176 +: 16];
  wire [15:0] emask2 = dbg_emask[32 +: 16];
  wire [15:0] econd2 = dbg_econd[32 +: 16];
  wire [15:0] fv2 = dbg_fire_v[32 +: 16];
  always @(posedge clk) if (post_reset) begin
    if (hv2) begin
      if (he2) begin
        // P3: edge event fires iff the masked pin sample matches TECOND
        if ((pins_in & emask2) == (econd2 & emask2)) begin
          assert (dbg_fire[2]);                      // fires this cycle
          assert (fv2 == hval2);                   // P4
        end else begin
          assert (!dbg_fire[2]);                     // no early/late edge fire
        end
      end else begin
        // P1: due this cycle => must fire this cycle (never late)
        if (ht2 == dbg_tmr) begin
          assert (dbg_fire[2]);
          assert (fv2 == hval2);                   // P4
        end
        // P2: due later (lookahead in 0x0001..0x7FFF) => must not fire early
        if ((ht2 != dbg_tmr) && ((ht2 - dbg_tmr) < 16'h8000)) begin
          assert (!dbg_fire[2]);
        end
      end
    end
  end

  // ---- thread 3: head selection from the observation ports --------------
  wire [2:0] sh3 = dbg_eq_head[9 +: 3];
  wire       hv3 = (sh3 == 3'd0) ? dbg_eq_vali[12] :
                     (sh3 == 3'd1) ? dbg_eq_vali[13] :
                     (sh3 == 3'd2) ? dbg_eq_vali[14] : dbg_eq_vali[15];
  wire       he3 = (sh3 == 3'd0) ? dbg_eq_edge[12] :
                     (sh3 == 3'd1) ? dbg_eq_edge[13] :
                     (sh3 == 3'd2) ? dbg_eq_edge[14] : dbg_eq_edge[15];
  wire [15:0] ht3 = (sh3 == 3'd0) ? dbg_eq_tgt[192 +: 16] :
                      (sh3 == 3'd1) ? dbg_eq_tgt[208 +: 16] :
                      (sh3 == 3'd2) ? dbg_eq_tgt[224 +: 16] : dbg_eq_tgt[240 +: 16];
  wire [15:0] hval3 = (sh3 == 3'd0) ? dbg_eq_val[192 +: 16] :
                        (sh3 == 3'd1) ? dbg_eq_val[208 +: 16] :
                        (sh3 == 3'd2) ? dbg_eq_val[224 +: 16] : dbg_eq_val[240 +: 16];
  wire [15:0] emask3 = dbg_emask[48 +: 16];
  wire [15:0] econd3 = dbg_econd[48 +: 16];
  wire [15:0] fv3 = dbg_fire_v[48 +: 16];
  always @(posedge clk) if (post_reset) begin
    if (hv3) begin
      if (he3) begin
        // P3: edge event fires iff the masked pin sample matches TECOND
        if ((pins_in & emask3) == (econd3 & emask3)) begin
          assert (dbg_fire[3]);                      // fires this cycle
          assert (fv3 == hval3);                   // P4
        end else begin
          assert (!dbg_fire[3]);                     // no early/late edge fire
        end
      end else begin
        // P1: due this cycle => must fire this cycle (never late)
        if (ht3 == dbg_tmr) begin
          assert (dbg_fire[3]);
          assert (fv3 == hval3);                   // P4
        end
        // P2: due later (lookahead in 0x0001..0x7FFF) => must not fire early
        if ((ht3 != dbg_tmr) && ((ht3 - dbg_tmr) < 16'h8000)) begin
          assert (!dbg_fire[3]);
        end
      end
    end
  end

  // ---- P5: accepted enqueue records exactly the documented due time -------
  // constant-index muxes over the observed issue slot
  wire [15:0] period_s = (dbg_sel == 2'd0) ? dbg_period[0*16 +: 16] :
                         (dbg_sel == 2'd1) ? dbg_period[1*16 +: 16] :
                         (dbg_sel == 2'd2) ? dbg_period[2*16 +: 16] :
                                             dbg_period[3*16 +: 16];
  wire [15:0] nextt_s  = (dbg_sel == 2'd0) ? dbg_nextt[0*16 +: 16] :
                         (dbg_sel == 2'd1) ? dbg_nextt[1*16 +: 16] :
                         (dbg_sel == 2'd2) ? dbg_nextt[2*16 +: 16] :
                                             dbg_nextt[3*16 +: 16];
  wire [3:0]  pfrac_s  = (dbg_sel == 2'd0) ? dbg_pfrac[0*4 +: 4] :
                         (dbg_sel == 2'd1) ? dbg_pfrac[1*4 +: 4] :
                         (dbg_sel == 2'd2) ? dbg_pfrac[2*4 +: 4] :
                                             dbg_pfrac[3*4 +: 4];
  wire [4:0]  per_frac = {1'b0, pfrac_s} + {1'b0, period_s[3:0]};
  wire [15:0] emit_raw = nextt_s + {12'd0, period_s[15:4]} + {15'd0, per_frac[4]};
  wire [15:0] emit_d   = emit_raw - dbg_tmr;
  wire [15:0] emit_exp = (emit_d >= 16'h8000) ? (dbg_tmr + 16'd1) : emit_raw;
  wire [15:0] abs_d    = dbg_imm - dbg_tmr;
  wire [15:0] abs_exp  = (abs_d >= 16'h8000) ? (dbg_tmr + 16'd1) : dbg_imm;
  wire [15:0] rel_exp  = (dbg_imm == 16'd0) ? (dbg_tmr + 16'd1)
                                             : (dbg_tmr + dbg_imm);
  always @(posedge clk) if (post_reset) begin
    if (dbg_do_issue && !dbg_stall_issue && dbg_ev_enq_t) begin
      assert (dbg_ev_enq_val == dbg_src_a);            // value path
      case (dbg_opc)
        `OPC_EMIT:  assert (dbg_ev_enq_time == emit_exp);
        `OPC_EMITA: assert (dbg_ev_enq_time == abs_exp);
        `OPC_EMITD: assert (dbg_ev_enq_time == rel_exp);
        default:    assert (1'b0);                     // ev_enq_t only from these
      endcase
      // the recorded due time is never more than half the time space ahead
      assert ((dbg_ev_enq_time == dbg_tmr) ||
              ((dbg_ev_enq_time - dbg_tmr) < 16'h8000));
    end
    if (dbg_do_issue && !dbg_stall_issue && dbg_ev_enq_edge)
      assert (!dbg_ev_enq_t);                          // edge path is separate
  end

  // ---- P6: the fired value reaches pins_out in the same cycle -------------
  wire pin_compete =
        dbg_fire[1] | dbg_fire[2] | dbg_fire[3]
      | (dbg_do_issue && !dbg_stall_issue && dbg_pin_op)
      | (dbg_do_issue && !dbg_stall_issue && dbg_csr_wr &&
         (dbg_csr_wr_addr == `CSR_POUT))
      | (host_csrw_en && (host_csrw_addr == `CSR_POUT))
      | wave_active;
  wire [15:0] tm0 = dbg_tmask[0*16 +: 16];
  wire [15:0] fv0 = dbg_fire_v[0*16 +: 16];
  wire [15:0] p6_expected = ((dbg_pout & ~tm0) | (fv0 & tm0)) & 16'hFCFF;
  always @(posedge clk) if (post_reset) begin
    if (!wave_active)
      assert (pins_out == dbg_npout);
    if (dbg_fire[0] && !pin_compete)
      assert (pins_out == p6_expected);
  end

  // ---- non-vacuity covers (reached => the invariants saw real activity) ----
  always @(posedge clk) if (post_reset) begin
    cover (dbg_do_issue);
    cover (dbg_do_issue && !dbg_stall_issue && dbg_ev_enq_t);
    cover (dbg_do_issue && !dbg_stall_issue && dbg_ev_enq_edge);
    cover (dbg_fire[0]);
    cover (hv0 && !he0 && (ht0 == dbg_tmr));
  end

endmodule

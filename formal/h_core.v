// ============================================================================
//  formal/h_core.v — bounded formal harness for rtl/loom_core.v
//
//  Proves (BMC from a generated reset):
//    * reset completeness: TMR=0, pout=0, pdir=0, all threads disabled+halted,
//      event queues and FIFO counters empty (spec §7),
//    * FIFO safety: TX/RX occupancy never exceeds 8 (no silent overflow /
//      underflow count corruption),
//    * event-queue safety: depth never exceeds 4, head pointer stays in range,
//    * pout only changes through a defined cause: a fired event, an instruction
//      pin op / CSR POUT write, a host CSR POUT write, or the wave player.
//
//  Everything else is assumed by construction: imem data, DMEM read data, pin
//  inputs and the host CSR/FIFO ports are (* anyseq *) -- the proofs are
//  therefore valid for *all* external stimulus.
// ============================================================================
`default_nettype none
`include "loom_bus.vh"
module h_core(input wire clk);

  reg [4:0] rst_cnt = 0;
  wire rst_n = (rst_cnt == 5'd6);
  always @(posedge clk) if (rst_cnt != 5'd6) rst_cnt <= rst_cnt + 5'd1;

  (* anyseq *) wire [31:0] imem_data;
  (* anyseq *) wire [15:0] dmem_rdata;
  (* anyseq *) wire [15:0] pins_in;
  (* anyseq *) wire        wave_busy, wave_done, wave_active;
  (* anyseq *) wire [15:0] wave_pins;
  (* anyseq *) wire [15:0] cap_stat;
  (* anyseq *) wire        host_csrw_en;
  (* anyseq *) wire [7:0]  host_csrw_addr;
  (* anyseq *) wire [15:0] host_csrw_data;
  (* anyseq *) wire        host_csrr_en;
  (* anyseq *) wire [7:0]  host_csrr_addr;
  (* anyseq *) wire        host_rx_push;
  (* anyseq *) wire [1:0]  host_rx_tid;
  (* anyseq *) wire [7:0]  host_rx_data;
  (* anyseq *) wire        host_tx_pop;
  (* anyseq *) wire [1:0]  host_tx_tid;

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

  loom_core #(.NUM_THREADS(4), .EQ_DEPTH(4), .FIFO_DEPTH(8), .DMEM_WORDS(64)) dut (
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
      .irq_code(irq_code), .thread_run_mask(thread_run_mask));

  reg post_reset = 1'b0;
  always @(posedge clk) post_reset <= rst_n;

  reg [15:0] prev_pout;
  always @(posedge clk) prev_pout <= pins_out;

  // ---- reset completeness (first cycle after release) --------------------
  reg first_post = 1'b1;
  always @(posedge clk) begin
    if (!rst_n) begin
      first_post <= 1'b1;
    end else if (first_post) begin
      assert (dut.tmr == 16'd0);
      assert (pins_out == 16'd0);
      assert (pins_dir == 8'd0);
      assert (dut.t_en  == 4'b0000);
      assert (dut.t_halt == 4'b1111);
      assert (dut.eq_cnt[0] == 0 && dut.eq_cnt[1] == 0 &&
              dut.eq_cnt[2] == 0 && dut.eq_cnt[3] == 0);
      assert (dut.tx_cnt[0] == 0 && dut.tx_cnt[1] == 0 &&
              dut.tx_cnt[2] == 0 && dut.tx_cnt[3] == 0);
      assert (dut.rx_cnt[0] == 0 && dut.rx_cnt[1] == 0 &&
              dut.rx_cnt[2] == 0 && dut.rx_cnt[3] == 0);
      first_post <= 1'b0;
    end
  end

  // ---- always-on safety properties ---------------------------------------
  wire pin_cause = dut.fire[0] | dut.fire[1] | dut.fire[2] | dut.fire[3]
                 | (dut.do_issue & ~dut.stall_issue & dut.pin_op)
                 | (dut.do_issue & ~dut.stall_issue & dut.csr_wr &
                    (dut.csr_wr_addr == `CSR_POUT))
                 | (host_csrw_en & (host_csrw_addr == `CSR_POUT))
                 | wave_active;

  always @(posedge clk) if (post_reset) begin
    // event queue: depth and head/tail integrity
    assert (dut.eq_cnt[0] <= `EQ_DEPTH);
    assert (dut.eq_cnt[1] <= `EQ_DEPTH);
    assert (dut.eq_cnt[2] <= `EQ_DEPTH);
    assert (dut.eq_cnt[3] <= `EQ_DEPTH);
    assert (dut.eq_head[0] < `EQ_DEPTH);
    assert (dut.eq_head[1] < `EQ_DEPTH);
    assert (dut.eq_head[2] < `EQ_DEPTH);
    assert (dut.eq_head[3] < `EQ_DEPTH);
    // byte FIFOs: counts never exceed the physical depth
    assert (dut.tx_cnt[0] <= `FIFO_DEPTH);
    assert (dut.tx_cnt[1] <= `FIFO_DEPTH);
    assert (dut.tx_cnt[2] <= `FIFO_DEPTH);
    assert (dut.tx_cnt[3] <= `FIFO_DEPTH);
    assert (dut.rx_cnt[0] <= `FIFO_DEPTH);
    assert (dut.rx_cnt[1] <= `FIFO_DEPTH);
    assert (dut.rx_cnt[2] <= `FIFO_DEPTH);
    assert (dut.rx_cnt[3] <= `FIFO_DEPTH);
    // pins_out only changes through a defined cause
    assert ((pins_out == prev_pout) || pin_cause);
    // DMEM port address is always a legal DMEM word address
    assert (dmem_addr < `DMEM_WORDS);
  end

endmodule
`default_nettype wire

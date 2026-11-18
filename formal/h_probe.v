`default_nettype none
`include "loom_bus.vh"
module h_probe(input wire clk);
  reg [4:0] rst_cnt = 0;
  wire rst_n = (rst_cnt == 5'd6);
  always @(posedge clk) if (rst_cnt != 5'd6) rst_cnt <= rst_cnt + 5'd1;
  (* anyseq *) wire [31:0] imem_data; (* anyseq *) wire [15:0] dmem_rdata;
  (* anyseq *) wire [15:0] pins_in; (* anyseq *) wire wave_busy, wave_done, wave_active;
  (* anyseq *) wire [15:0] wave_pins, cap_stat;
  (* anyseq *) wire host_csrw_en, host_csrr_en, host_rx_push, host_tx_pop;
  (* anyseq *) wire [7:0] host_csrw_addr, host_csrr_addr, host_rx_data;
  (* anyseq *) wire [15:0] host_csrw_data; (* anyseq *) wire [1:0] host_rx_tid, host_tx_tid;
  wire [9:0] imem_addr; wire [5:0] dmem_addr; wire dmem_we; wire [15:0] dmem_wdata;
  wire [15:0] pins_out; wire [7:0] pins_dir; wire wave_start, wave_loop, wave_oneshot;
  wire [5:0] wave_addr; wire [6:0] wave_len; wire [15:0] wave_period;
  wire [15:0] cap_ctrl, host_csrr_data; wire [3:0] rx_full, tx_empty;
  wire [15:0] irq_code; wire [3:0] thread_run_mask;
  loom_core #(.NUM_THREADS(4), .EQ_DEPTH(4), .FIFO_DEPTH(8), .DMEM_WORDS(64)) dut (
      .clk(clk), .rst_n(rst_n), .imem_addr(imem_addr), .imem_data(imem_data),
      .dmem_addr(dmem_addr), .dmem_we(dmem_we), .dmem_wdata(dmem_wdata),
      .dmem_rdata(dmem_rdata), .dmem_denied(1'b0), .pins_out(pins_out),
      .pins_dir(pins_dir), .pins_in(pins_in), .wave_start(wave_start),
      .wave_loop(wave_loop), .wave_oneshot(wave_oneshot), .wave_addr(wave_addr),
      .wave_len(wave_len), .wave_period(wave_period), .wave_busy(wave_busy),
      .wave_done(wave_done), .wave_active(wave_active), .wave_pins(wave_pins),
      .cap_ctrl(cap_ctrl), .cap_stat(cap_stat), .host_csrw_en(host_csrw_en),
      .host_csrw_addr(host_csrw_addr), .host_csrw_data(host_csrw_data),
      .host_csrr_en(host_csrr_en), .host_csrr_addr(host_csrr_addr),
      .host_csrr_data(host_csrr_data), .host_rx_push(host_rx_push),
      .host_rx_tid(host_rx_tid), .host_rx_data(host_rx_data), .rx_full(rx_full),
      .host_tx_pop(host_tx_pop), .host_tx_tid(host_tx_tid),
      .host_tx_data(), .tx_empty(tx_empty), .irq_code(irq_code),
      .thread_run_mask(thread_run_mask));
  always @(posedge clk) begin
    assert (dut.pout == 16'h0 || 1'b1);
    assert (dut.npout == 16'h0 || 1'b1);
    assert (dut.fire[0] == 1'b0 || 1'b1);
    assert (dut.fire_v[0] == 16'h0 || 1'b1);
    assert (dut.src_a == 16'h0 || 1'b1);
    assert (dut.opc == 7'h0 || 1'b1);
    assert (dut.sel == 2'h0 || 1'b1);
    assert (dut.ev_enq_time == 16'h0 || 1'b1);
    assert (dut.eq_cnt[0] == 3'h0 || 1'b1);
    assert (dut.do_issue == 1'b0 || 1'b1);
    assert (dut.stall_issue == 1'b0 || 1'b1);
    assert (dut.pin_op == 1'b0 || 1'b1);
    assert (dut.pin_val == 16'h0 || 1'b1);
    assert (dut.csr_wr == 1'b0 || 1'b1);
    assert (dut.csr_wr_addr == 8'h0 || 1'b1);
    assert (dut.csr_wr_val == 16'h0 || 1'b1);
    assert (dut.ev_enq_t == 1'b0 || 1'b1);
    assert (dut.ev_enq_edge == 1'b0 || 1'b1);
    assert (dut.ev_enq_val == 16'h0 || 1'b1);
    assert (dut.imm == 16'h0 || 1'b1);
  end
endmodule

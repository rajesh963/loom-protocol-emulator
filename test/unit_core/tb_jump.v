`default_nettype none
`timescale 1ns / 1ps
// Regression: `csrw TPC` is a computed jump. The instruction at the new PC must
// be executed on the very next issue; the fall-through instruction must not be.
module tb_jump;
  reg clk = 0; always #5 clk = ~clk;
  reg rst_n = 0;
  wire [9:0] imem_addr; reg [31:0] imem_data; reg [31:0] imem [0:1023];
  always @(posedge clk) imem_data <= imem[imem_addr];
  wire [5:0] dmem_addr; wire dmem_we; wire [15:0] dmem_wdata;
  reg [15:0] dmem_rdata = 0; reg dmem_denied = 0;
  wire [15:0] pins_out; wire [7:0] pins_dir; reg [15:0] pins_in = 0;
  wire ws, wl, wo; wire [5:0] wa; wire [6:0] wlen; wire [15:0] wper;
  reg wbusy = 0, wdone = 0, wact = 0; reg [15:0] wp = 0;
  wire [15:0] cap_ctrl; reg [15:0] cap_stat = 0;
  reg hcw = 0, hcr = 0; reg [7:0] hcwa = 0, hcra = 0; reg [15:0] hcwd = 0;
  wire [15:0] hcrd;
  reg hrxp = 0, htxp = 0; reg [1:0] hrxt = 0, htxt = 0; reg [7:0] hrxd = 0;
  wire [3:0] rxf, txe; wire [7:0] htxd; wire [15:0] irq; wire [3:0] trm;

  loom_core dut (.clk(clk), .rst_n(rst_n), .imem_addr(imem_addr), .imem_data(imem_data),
    .dmem_addr(dmem_addr), .dmem_we(dmem_we), .dmem_wdata(dmem_wdata),
    .dmem_rdata(dmem_rdata), .dmem_denied(dmem_denied),
    .pins_out(pins_out), .pins_dir(pins_dir), .pins_in(pins_in),
    .wave_start(ws), .wave_loop(wl), .wave_oneshot(wo), .wave_addr(wa),
    .wave_len(wlen), .wave_period(wper), .wave_busy(wbusy), .wave_done(wdone),
    .wave_active(wact), .wave_pins(wp), .cap_ctrl(cap_ctrl), .cap_stat(cap_stat),
    .host_csrw_en(hcw), .host_csrw_addr(hcwa), .host_csrw_data(hcwd),
    .host_csrr_en(hcr), .host_csrr_addr(hcra), .host_csrr_data(hcrd),
    .host_rx_push(hrxp), .host_rx_tid(hrxt), .host_rx_data(hrxd), .rx_full(rxf),
    .host_tx_pop(htxp), .host_tx_tid(htxt), .host_tx_data(htxd), .tx_empty(txe),
    .irq_code(irq), .thread_run_mask(trm));

  task csr_write(input [7:0] a, input [15:0] d);
    begin
      @(negedge clk); hcw = 1; hcwa = a; hcwd = d;
      @(negedge clk); hcw = 0;
    end
  endtask

  integer errors = 0;
  initial begin
    $readmemh("jump.hex", imem);
    repeat (5) @(posedge clk);
    rst_n = 1;
    repeat (3) @(posedge clk);
    csr_write(8'h30, 16'h0001);          // RUN thread 0 (PC 0)
    repeat (20) @(posedge clk);
    if (pins_out !== 16'h0400) begin
      errors = errors + 1;
      $display("ERROR: computed jump did not execute target block (pins=%h want 0400)", pins_out);
    end
    if (dut.t_halt[0] !== 1'b1) begin
      errors = errors + 1;
      $display("ERROR: thread did not halt at addr 8");
    end
    if (errors == 0) $display("JUMP_TB: PASS");
    else begin $display("JUMP_TB: FAIL"); $fatal(1); end
    $finish;
  end
  initial begin #100000; $display("JUMP_TB: FAIL (timeout)"); $fatal(1); end
endmodule
`default_nettype wire

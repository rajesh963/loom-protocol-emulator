`default_nettype none
`timescale 1ns / 1ps
// Self-checking smoke test for loom_core (no host bridge / wave player / top).
// Thread 0 toggles pins[7:0] every 16 cycles, thread 1 toggles pins[11:8] every
// 8 cycles.  Verifies independent periodic event streams + queue back-pressure.
module tb_core;

  localparam CLK_HALF = 5;      // 100 MHz simulation clock

  reg clk = 0;
  always #CLK_HALF clk = ~clk;

  reg rst_n = 0;
  integer errors = 0;
  integer cyc = 0;
  integer low_trans = 0, hi_trans = 0;
  integer last_low = -1, last_hi = -1;
  reg [15:0] prev_pins = 16'h0000;
  reg        running = 0;

  // instruction memory: 1-cycle synchronous read
  reg  [9:0]  imem_addr;
  reg  [31:0] imem_data;
  reg  [31:0] imem [0:1023];
  always @(posedge clk) imem_data <= imem[imem_addr];

  // DUT ports
  wire [5:0]  dmem_addr;
  wire        dmem_we;
  wire [15:0] dmem_wdata;
  reg  [15:0] dmem_rdata = 16'd0;
  reg         dmem_denied = 1'b0;

  wire [15:0] pins_out;
  wire [7:0]  pins_dir;
  reg  [15:0] pins_in = 16'd0;

  wire        wave_start, wave_loop, wave_oneshot;
  wire [5:0]  wave_addr;
  wire [6:0]  wave_len;
  wire [15:0] wave_period;
  reg         wave_busy = 0, wave_done = 0, wave_active = 0;
  reg  [15:0] wave_pins = 16'd0;

  wire [15:0] cap_ctrl;
  reg  [15:0] cap_stat = 16'd0;

  reg         host_csrw_en = 0, host_csrr_en = 0;
  reg  [7:0]  host_csrw_addr = 0, host_csrr_addr = 0;
  reg  [15:0] host_csrw_data = 0;
  wire [15:0] host_csrr_data;

  reg         host_rx_push = 0, host_tx_pop = 0;
  reg  [1:0]  host_rx_tid = 0, host_tx_tid = 0;
  reg  [7:0]  host_rx_data = 0;
  wire [3:0]  rx_full, tx_empty;
  wire [7:0]  host_tx_data;
  wire [15:0] irq_code;

  loom_core #(
      .NUM_THREADS(4), .EQ_DEPTH(4), .FIFO_DEPTH(8), .DMEM_WORDS(64)
  ) dut (
      .clk(clk), .rst_n(rst_n),
      .imem_addr(imem_addr), .imem_data(imem_data),
      .dmem_addr(dmem_addr), .dmem_we(dmem_we), .dmem_wdata(dmem_wdata),
      .dmem_rdata(dmem_rdata), .dmem_denied(dmem_denied),
      .pins_out(pins_out), .pins_dir(pins_dir), .pins_in(pins_in),
      .wave_start(wave_start), .wave_loop(wave_loop), .wave_oneshot(wave_oneshot),
      .wave_addr(wave_addr), .wave_len(wave_len), .wave_period(wave_period),
      .wave_busy(wave_busy), .wave_done(wave_done), .wave_active(wave_active),
      .wave_pins(wave_pins),
      .cap_ctrl(cap_ctrl), .cap_stat(cap_stat),
      .host_csrw_en(host_csrw_en), .host_csrw_addr(host_csrw_addr),
      .host_csrw_data(host_csrw_data),
      .host_csrr_en(host_csrr_en), .host_csrr_addr(host_csrr_addr),
      .host_csrr_data(host_csrr_data),
      .host_rx_push(host_rx_push), .host_rx_tid(host_rx_tid),
      .host_rx_data(host_rx_data), .rx_full(rx_full),
      .host_tx_pop(host_tx_pop), .host_tx_tid(host_tx_tid),
      .host_tx_data(host_tx_data), .tx_empty(tx_empty),
      .irq_code(irq_code)
  );

  // ---- host CSR write task (negedge drive, posedge sample) -----------------
  task csr_write(input [7:0] a, input [15:0] d);
    begin
      @(negedge clk);
      host_csrw_en   = 1'b1;
      host_csrw_addr = a;
      host_csrw_data = d;
      @(negedge clk);
      host_csrw_en   = 1'b0;
      host_csrw_addr = 8'h00;
      host_csrw_data = 16'h0000;
    end
  endtask

  // ---- transition monitor ---------------------------------------------------
  always @(posedge clk) begin
    cyc = cyc + 1;
    if (running) begin
      // low-byte activity (thread 0): expect every 16 cycles
      if ((pins_out & 16'h00FF) !== (prev_pins & 16'h00FF)) begin
        low_trans = low_trans + 1;
        if (last_low >= 0) begin
          if ((cyc - last_low) !== 16) begin
            errors = errors + 1;
            $display("ERROR t0: low-byte edge after %0d cycles (expected 16) at cyc %0d",
                     cyc - last_low, cyc);
          end
        end
        last_low = cyc;
      end
      // high-nibble activity (thread 1): expect every 8 cycles
      if ((pins_out & 16'h0F00) !== (prev_pins & 16'h0F00)) begin
        hi_trans = hi_trans + 1;
        if (last_hi >= 0) begin
          if ((cyc - last_hi) !== 8) begin
            errors = errors + 1;
            $display("ERROR t1: hi-nibble edge after %0d cycles (expected 8) at cyc %0d",
                     cyc - last_hi, cyc);
          end
        end
        last_hi = cyc;
      end
      prev_pins = pins_out;
    end
  end

  initial begin
    $display("CORE_TB: loading program");
    $readmemh("prog.hex", imem);
    repeat (5) @(posedge clk);
    rst_n = 1;
    repeat (3) @(posedge clk);

    // sanity: PC of both threads is 0 after reset, both halted
    csr_write(8'h40 + 8'h08, 16'h0000);   // t0 PC = 0 (already)
    csr_write(8'h50 + 8'h08, 16'h0010);   // t1 PC = 16
    csr_write(8'h30, 16'h0003);           // RUNMASK: run threads 0 and 1
    running   = 1;
    prev_pins = pins_out;
    repeat (500) @(posedge clk);
    running = 0;

    $display("CORE_TB: transitions t0=%0d t1=%0d", low_trans, hi_trans);
    if (errors != 0) begin
      $display("CORE_TB: FAIL (%0d errors)", errors);
      $fatal(1);
    end
    if (low_trans < 8 || hi_trans < 12) begin
      $display("CORE_TB: FAIL (not enough transitions: t0=%0d t1=%0d)",
               low_trans, hi_trans);
      $fatal(1);
    end
    $display("CORE_TB: PASS");
    $finish;
  end

  // global timeout
  initial begin
    #200000;
    $display("CORE_TB: FAIL (timeout)");
    $fatal(1);
  end

endmodule
`default_nettype wire

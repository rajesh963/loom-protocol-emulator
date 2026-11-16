// ============================================================================
//  test/unit_wave/tb_wave.v - self-checking testbench for rtl/loom_wave.v
//  Simulator: Icarus Verilog 12 (iverilog -g2012).  Run via ./run.sh
//
//  Checks:
//    (a)  first-pattern latency and symbol period for cfg_period = 1.0 cycle
//    (a2) first-pattern latency for cfg_period = 2.0 cycles
//    (b)  fractional (12.4) period accuracy: 1000 symbols at 0x001A (1.625)
//    (b2) fractional accuracy: 100 symbols at 0x0018 (1.5)
//    (c)  loop wrap-around, busy stays asserted
//    (d)  one-shot completion (done/busy/wave_valid/pins) and done latching
//    (e)  cfg_start while busy restarts at the next symbol boundary,
//         no truncated / glitched pattern
//    (f)  asynchronous reset while running
//    (g)  sub-cycle period (0x0005) -> one pattern per cycle
// ============================================================================

`timescale 1ns/1ps

module tb_wave;

  // ------------------------------------------------------------------ clock
  reg clk;
  initial clk = 1'b0;
  always #5 clk = ~clk;              // 100 MHz sim clock

  // ------------------------------------------------------------------ DUT
  reg         rst_n;

  reg         cfg_start;
  reg         cfg_loop;
  reg         cfg_oneshot;
  reg  [5:0]  cfg_addr;
  reg  [6:0]  cfg_len;
  reg  [15:0] cfg_period;

  wire        busy;
  wire        done;
  wire [5:0]  dmem_addr;
  wire [15:0] wave_pins;
  wire        wave_valid;

  // DMEM stub: 64 x 16 FF array with combinational read
  reg [15:0] dmem [0:63];
  wire [15:0] dmem_data = dmem[dmem_addr];

  loom_wave dut (
    .clk        (clk),
    .rst_n      (rst_n),
    .cfg_start  (cfg_start),
    .cfg_loop   (cfg_loop),
    .cfg_oneshot(cfg_oneshot),
    .cfg_addr   (cfg_addr),
    .cfg_len    (cfg_len),
    .cfg_period (cfg_period),
    .busy       (busy),
    .done       (done),
    .dmem_addr  (dmem_addr),
    .dmem_data  (dmem_data),
    .wave_pins  (wave_pins),
    .wave_valid (wave_valid)
  );

  // ------------------------------------------------------------- checking
  integer errors;
  integer checks;

  `define CHK(cond, msg) \
    begin checks = checks + 1; \
      if (!(cond)) begin errors = errors + 1; $display("FAIL @%0t: %s", $time, msg); end \
    end

  // pattern word for offset n in the DMEM stub
  function [15:0] pat;
    input integer n;
    begin pat = 16'hA000 + n[15:0]; end
  endfunction

  // ------------------------------------------------------------- helpers
  // Load the whole DMEM stub with a known, easily recognisable pattern.
  task load_dmem;
    integer k;
    begin
      for (k = 0; k < 64; k = k + 1) dmem[k] = pat(k);
    end
  endtask

  task do_reset;
    begin
      rst_n     = 1'b0;
      cfg_start = 1'b0;
      cfg_loop  = 1'b0;
      cfg_oneshot = 1'b0;
      cfg_addr  = 6'd0;
      cfg_len   = 7'd1;
      cfg_period= 16'h0010;
      repeat (3) @(negedge clk);
      rst_n = 1'b1;
      @(negedge clk);
    end
  endtask

  // Set the configuration inputs.  Returns on a negedge; the following
  // start_pulse() call puts cfg_start one cycle later (so cfg_addr is stable
  // for one full cycle before the start pulse, as the module requires).
  task cfg_setup;
    input [5:0]  a;
    input [6:0]  l;
    input [15:0] p;
    input        lp;
    input        os;
    begin
      @(negedge clk);
      cfg_addr   = a;
      cfg_len    = l;
      cfg_period = p;
      cfg_loop   = lp;
      cfg_oneshot= os;
    end
  endtask

  task start_pulse;
    begin
      @(negedge clk);
      cfg_start = 1'b1;
      @(negedge clk);
      cfg_start = 1'b0;
    end
  endtask

  // wait n negedges
  task wait_cyc;
    input integer n;
    begin
      repeat (n) @(negedge clk);
    end
  endtask

  // ------------------------------------------------------------- monitors
  // wave_pins must be 0 whenever the player does not own the pins
  always @(negedge clk) begin
    if ((rst_n === 1'b1) && (wave_valid === 1'b0) && (wave_pins !== 16'd0)) begin
      errors = errors + 1;
      $display("FAIL @%0t: wave_pins=%h while wave_valid=0", $time, wave_pins);
    end
    if ((rst_n === 1'b1) && (busy === 1'b0) && (wave_valid === 1'b1)) begin
      errors = errors + 1;
      $display("FAIL @%0t: wave_valid=1 while not busy", $time);
    end
  end

  // ------------------------------------------------------------- tests
  integer i;
  integer elapsed;
  reg [15:0] prev;

  initial begin
    errors = 0;
    checks = 0;
    rst_n  = 1'b0;
    cfg_start = 1'b0; cfg_loop = 1'b0; cfg_oneshot = 1'b0;
    cfg_addr = 6'd0; cfg_len = 7'd1; cfg_period = 16'h0010;
    load_dmem;
    wait_cyc(1);

    // ------------------------------------------------------------ (0) reset
    do_reset;
    `CHK(busy === 0,      "(0) reset: busy=0");
    `CHK(done === 0,      "(0) reset: done=0");
    `CHK(wave_valid === 0,"(0) reset: wave_valid=0");
    `CHK(wave_pins === 16'd0, "(0) reset: wave_pins=0");
    `CHK(dmem_addr === 6'd0,  "(0) reset: dmem_addr=0");

    // ------------------------------------------------- (a) period = 1.0 cycle
    // first pattern must be on the pins exactly 1 cycle after the start pulse
    cfg_setup(6'd0, 7'd4, 16'h0010, 1'b1, 1'b0);
    @(negedge clk);
    `CHK(busy === 0, "(a) idle before start");
    `CHK(wave_valid === 0 && wave_pins === 16'd0, "(a) pins released before start");
    cfg_start = 1'b1;
    @(negedge clk);                       // exactly one cycle after the pulse
    cfg_start = 1'b0;
    `CHK(busy === 1, "(a) busy set");
    `CHK(done === 0, "(a) done cleared");
    `CHK(wave_valid === 1, "(a) wave_valid exactly 1 cycle after start");
    `CHK(wave_pins === pat(0), "(a) first pattern value");
    `CHK(dmem_addr === 6'd1, "(a) DMEM address pipelined one symbol ahead");
    @(negedge clk);
    `CHK(wave_valid === 1 && wave_pins === pat(1), "(a) symbol 1 after 1 cycle");
    @(negedge clk);
    `CHK(wave_valid === 1 && wave_pins === pat(2), "(a) symbol 2 after 1 cycle");
    @(negedge clk);
    `CHK(wave_valid === 1 && wave_pins === pat(3), "(a) symbol 3 after 1 cycle");
    @(negedge clk);
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(a) loop wraps to pattern 0");
    `CHK(busy === 1 && done === 0, "(a) still running (loop)");

    // ------------------------------------------------ (a2) period = 2.0 cycles
    do_reset;
    cfg_setup(6'd0, 7'd4, 16'h0020, 1'b1, 1'b0);
    start_pulse;                          // returns 1 cycle after the pulse
    `CHK(wave_valid === 0, "(a2) no pattern 1 cycle after start when period=2.0");
    `CHK(wave_pins === 16'd0, "(a2) pins still low");
    @(negedge clk);
    `CHK(wave_valid === 1, "(a2) pattern appears 2 cycles after start");
    `CHK(wave_pins === pat(0), "(a2) first pattern value");
    @(negedge clk);
    `CHK(wave_pins === pat(0), "(a2) symbol 0 lasts 2 cycles");
    @(negedge clk);
    `CHK(wave_pins === pat(1), "(a2) symbol 1 at start+4");
    @(negedge clk);
    `CHK(wave_pins === pat(1), "(a2) symbol 1 lasts 2 cycles");
    @(negedge clk);
    `CHK(wave_pins === pat(2), "(a2) symbol 2 at start+6");

    // --------------------------- (b) fractional period 0x001A = 1.625 cycles
    do_reset;
    cfg_setup(6'd0, 7'd2, 16'h001A, 1'b1, 1'b0);
    start_pulse;                          // 1 cycle after start: first pattern
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(b) first pattern");
    prev    = wave_pins;
    elapsed = 0;
    for (i = 0; i < 1000; i = i + 1) begin
      @(negedge clk);
      elapsed = elapsed + 1;
      while (wave_pins === prev) begin
        @(negedge clk);
        elapsed = elapsed + 1;
      end
      prev = wave_pins;
    end
    $display("  (b) 1000 symbols at period 1.625 -> %0d cycles (exact 1625)", elapsed);
    `CHK(elapsed >= 1624 && elapsed <= 1626,
         "(b) 1000-symbol elapsed time within 1 cycle of 1625");
    `CHK(busy === 1 && wave_valid === 1 && done === 0, "(b) still running");

    // --------------------------- (b2) fractional period 0x0018 = 1.5 cycles
    do_reset;
    cfg_setup(6'd0, 7'd2, 16'h0018, 1'b1, 1'b0);
    start_pulse;
    prev    = wave_pins;
    elapsed = 0;
    for (i = 0; i < 100; i = i + 1) begin
      @(negedge clk);
      elapsed = elapsed + 1;
      while (wave_pins === prev) begin
        @(negedge clk);
        elapsed = elapsed + 1;
      end
      prev = wave_pins;
    end
    $display("  (b2) 100 symbols at period 1.5 -> %0d cycles (exact 150)", elapsed);
    `CHK(elapsed >= 149 && elapsed <= 151,
         "(b2) 100-symbol elapsed time within 1 cycle of 150");

    // ------------------------------------------------------ (c) loop wrap
    do_reset;
    cfg_setup(6'd0, 7'd3, 16'h0010, 1'b1, 1'b0);
    start_pulse;
    for (i = 0; i < 7; i = i + 1) begin
      `CHK(wave_valid === 1, "(c) valid while looping");
      `CHK(wave_pins === pat(i % 3), "(c) loop sequence wraps every 3 symbols");
      @(negedge clk);
    end
    `CHK(busy === 1 && done === 0, "(c) loop keeps busy, done stays 0");

    // ------------------------------------------------------ (d) one-shot
    do_reset;
    cfg_setup(6'd0, 7'd3, 16'h0010, 1'b0, 1'b1);
    start_pulse;
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(d) one-shot first pattern");
    @(negedge clk);
    `CHK(wave_pins === pat(1), "(d) one-shot second pattern");
    @(negedge clk);
    `CHK(wave_pins === pat(2), "(d) one-shot third pattern");
    @(negedge clk);                       // last pattern played a full period
    `CHK(busy === 0, "(d) busy deasserted after last pattern");
    `CHK(done === 1, "(d) done set after last pattern");
    `CHK(wave_valid === 0, "(d) wave_valid cleared");
    `CHK(wave_pins === 16'd0, "(d) wave_pins forced to 0");
    wait_cyc(5);
    `CHK(done === 1 && busy === 0, "(d) done is a level until the next start");
    // a new start clears done
    cfg_setup(6'd0, 7'd2, 16'h0010, 1'b0, 1'b1);
    start_pulse;
    `CHK(done === 0 && busy === 1, "(d) cfg_start clears done");
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(d) restart plays pattern 0");

    // ------------------------------ (e) restart while busy, clean boundary
    do_reset;
    // config 1: buffer at word 0, 4 patterns, loop, period 4 cycles
    cfg_setup(6'd0, 7'd4, 16'h0040, 1'b1, 1'b0);
    start_pulse;                          // 1 cycle after start
    `CHK(wave_valid === 0, "(e) waiting for first pattern (period=4.0)");
    wait_cyc(1);
    `CHK(wave_valid === 0, "(e) still waiting at start+2");
    wait_cyc(1);
    `CHK(wave_valid === 0, "(e) still waiting at start+3");
    wait_cyc(1);
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(e) first pattern at start+4");
    // now 1 cycle into symbol 0 (which lasts 4 cycles): 3 cycles to go
    wait_cyc(1);
    cfg_addr   = 6'd8;
    cfg_len    = 7'd2;
    cfg_period = 16'h0010;
    cfg_loop   = 1'b1;
    cfg_oneshot= 1'b0;
    wait_cyc(1);
    cfg_start = 1'b1;                     // request restart mid-symbol
    @(negedge clk);
    cfg_start = 1'b0;
    `CHK(wave_pins === pat(0), "(e) running symbol not truncated by restart");
    `CHK(wave_valid === 1,     "(e) wave_valid continuous across restart");
    @(negedge clk);                       // next symbol boundary
    `CHK(wave_pins === pat(8), "(e) new buffer pattern 0 at next boundary");
    `CHK(busy === 1 && done === 0, "(e) busy/done across restart");
    @(negedge clk);
    `CHK(wave_pins === pat(9), "(e) new buffer continues (pattern 1)");
    @(negedge clk);
    `CHK(wave_pins === pat(8), "(e) new 2-pattern buffer wraps to pattern 0");

    // ------------------------------------------------------ (f) reset
    rst_n = 1'b0;                          // async reset in the middle of a run
    @(negedge clk);
    `CHK(busy === 0, "(f) async reset: busy=0");
    `CHK(done === 0, "(f) async reset: done=0");
    `CHK(wave_valid === 0, "(f) async reset: wave_valid=0");
    `CHK(wave_pins === 16'd0, "(f) async reset: wave_pins=0");
    @(negedge clk);
    rst_n = 1'b1;
    wait_cyc(2);
    `CHK(busy === 0 && done === 0 && wave_valid === 0 && wave_pins === 16'd0,
         "(f) stays idle after reset release");
    // and it can be started again after reset
    cfg_setup(6'd0, 7'd2, 16'h0010, 1'b1, 1'b0);
    start_pulse;
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(f) start after reset works");

    // ----------------------------------- (g) sub-cycle period = 0x0005
    do_reset;
    cfg_setup(6'd0, 7'd4, 16'h0005, 1'b1, 1'b0);
    start_pulse;
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(g) period<1: pattern immediately");
    @(negedge clk);
    `CHK(wave_pins === pat(1), "(g) one pattern per cycle");
    @(negedge clk);
    `CHK(wave_pins === pat(2), "(g) one pattern per cycle");
    @(negedge clk);
    `CHK(wave_pins === pat(3), "(g) one pattern per cycle");

    // -------- (h) cfg_start exactly on the final boundary of a one-shot
    // The pass must not stop and no symbol may be truncated: the old buffer
    // wraps one extra symbol (documented) and the new configuration starts at
    // the following boundary.  done must never be asserted.
    do_reset;
    cfg_setup(6'd0, 7'd2, 16'h0010, 1'b0, 1'b1);
    start_pulse;                          // T+1: pat(0)
    @(negedge clk);                       // T+2: pat(1) = last symbol
    `CHK(wave_pins === pat(1), "(h) last one-shot symbol");
    cfg_addr   = 6'd8;
    cfg_len    = 7'd2;
    cfg_period = 16'h0010;
    cfg_loop   = 1'b1;
    cfg_oneshot= 1'b0;
    cfg_start  = 1'b1;                    // lands on the pass boundary
    @(negedge clk);
    cfg_start = 1'b0;
    `CHK(busy === 1 && done === 0, "(h) no stop on a start request at the boundary");
    `CHK(wave_valid === 1 && wave_pins === pat(0), "(h) old buffer wraps one symbol");
    @(negedge clk);
    `CHK(wave_pins === pat(8), "(h) new configuration starts next boundary");
    @(negedge clk);
    `CHK(wave_pins === pat(9), "(h) new configuration continues");
    `CHK(busy === 1 && done === 0, "(h) still running");

    // ------------- (i) restart while running with period = 1.0 (tick every
    // cycle): the old symbol completes, then the new buffer starts
    do_reset;
    cfg_setup(6'd0, 7'd4, 16'h0010, 1'b1, 1'b0);
    start_pulse;                          // T+1: pat(0)
    @(negedge clk);                       // T+2: pat(1)
    `CHK(wave_pins === pat(1), "(i) old buffer running");
    cfg_addr   = 6'd16;
    cfg_len    = 7'd2;
    cfg_period = 16'h0010;
    cfg_loop   = 1'b1;
    cfg_oneshot= 1'b0;
    cfg_start  = 1'b1;
    @(negedge clk);
    cfg_start = 1'b0;                     // T+3: old symbol advanced, not glitched
    `CHK(wave_valid === 1, "(i) wave_valid continuous");
    `CHK(wave_pins === pat(2), "(i) old buffer advances one more symbol");
    @(negedge clk);                       // T+4: new buffer
    `CHK(wave_pins === pat(16), "(i) new buffer pattern 0");
    @(negedge clk);
    `CHK(wave_pins === pat(17), "(i) new buffer pattern 1");
    @(negedge clk);
    `CHK(wave_pins === pat(16), "(i) new 2-pattern buffer wraps");

    // ------------------------------------- (j) len=1 loop: same pattern
    do_reset;
    cfg_setup(6'd5, 7'd1, 16'h0010, 1'b1, 1'b0);
    start_pulse;
    `CHK(wave_pins === pat(5), "(j) len=1 pattern");
    @(negedge clk);
    `CHK(wave_pins === pat(5) && wave_valid === 1, "(j) len=1 repeats");
    @(negedge clk);
    `CHK(wave_pins === pat(5) && busy === 1, "(j) len=1 repeats");
    @(negedge clk);
    `CHK(wave_pins === pat(5), "(j) len=1 repeats");

    // ------------------------- (k) cfg_len=0 is clamped to a 1-pattern pass
    do_reset;
    cfg_setup(6'd3, 7'd0, 16'h0010, 1'b0, 1'b1);
    start_pulse;
    `CHK(wave_valid === 1 && wave_pins === pat(3), "(k) len=0 -> 1 pattern");
    @(negedge clk);
    `CHK(busy === 0 && done === 1 && wave_pins === 16'd0,
         "(k) len=0 one-shot completes");

    // ------------- (l) buffer address wraps at the top of the 64-word DMEM
    do_reset;
    cfg_setup(6'd63, 7'd2, 16'h0010, 1'b1, 1'b0);
    start_pulse;
    `CHK(wave_pins === pat(63), "(l) pattern at word 63");
    @(negedge clk);
    `CHK(wave_pins === pat(0), "(l) wraps to word 0");
    @(negedge clk);
    `CHK(wave_pins === pat(63), "(l) wraps back to word 63");
    @(negedge clk);
    `CHK(wave_pins === pat(0), "(l) wraps to word 0");

    // ------------------------------------------------------------ verdict
    if (errors == 0) begin
      $display("WAVE_TB: PASS (%0d checks)", checks);
      $finish;
    end else begin
      $display("WAVE_TB: FAIL (%0d errors in %0d checks)", errors, checks);
      $fatal(1, "loom_wave testbench failed");
    end
  end

  // watchdog
  initial begin
    #2000000;
    $display("WAVE_TB: FAIL (timeout)");
    $fatal(1, "loom_wave testbench timeout");
  end

endmodule

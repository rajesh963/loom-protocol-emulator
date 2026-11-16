// ============================================================================
//  test/unit_wave/tb_capture.v - self-checking testbench for loom_capture.v
//  Simulator: Icarus Verilog 12 (iverilog -g2012).  Run via ./run.sh
//
//  Checks:
//    (a) trigger filtering, entry packing, count/address ordering
//    (b) cfg_div (record every (cfg_div+1)-th matching cycle)
//    (c) full buffer: 64 entries, cap_full, stop (no overwrite), cap_count
//    (d) cfg_clear disarms and empties; no writes while disarmed
//    (e) masked trigger compare ignores value bits outside cfg_mask
//    (f) timestamp packing across a tmr[7:0] wrap
//    (g) re-arm clears the pointer
//    (h) level-style cfg_en (as a held CSR bit) arms once, does not re-arm
//    (i) level-style cfg_clear disarms/clears while held
// ============================================================================

`timescale 1ns/1ps

module tb_capture;

  // ------------------------------------------------------------------ clock
  reg clk;
  initial clk = 1'b0;
  always #5 clk = ~clk;

  // ------------------------------------------------------------------ DUT
  reg         rst_n;
  reg         cfg_en;
  reg         cfg_clear;
  reg  [15:0] cfg_mask;
  reg  [15:0] cfg_value;
  reg  [3:0]  cfg_div;
  reg  [15:0] pins_in;
  reg  [15:0] tmr_seed;

  wire        cap_we;
  wire [5:0]  cap_addr;
  wire [15:0] cap_wdata;
  wire [5:0]  cap_count;
  wire        cap_full;
  wire        cap_active;
  reg  [15:0] tmr;

  // free-running TMR model (loaded by reset from tmr_seed)
  always @(posedge clk or negedge rst_n)
    if (!rst_n) tmr <= tmr_seed;
    else        tmr <= tmr + 16'd1;

  loom_capture dut (
    .clk       (clk),
    .rst_n     (rst_n),
    .cfg_en    (cfg_en),
    .cfg_clear (cfg_clear),
    .cfg_mask  (cfg_mask),
    .cfg_value (cfg_value),
    .cfg_div   (cfg_div),
    .pins_in   (pins_in),
    .tmr       (tmr),
    .cap_we    (cap_we),
    .cap_addr  (cap_addr),
    .cap_wdata (cap_wdata),
    .cap_count (cap_count),
    .cap_full  (cap_full),
    .cap_active(cap_active)
  );

  // ------------------------------------------- DMEM stub: 64 x 16, sync write
  reg [15:0] capmem  [0:63];
  reg [15:0] wrlog   [0:255];   // capture writes, in order (data)
  reg [5:0]  addrlog [0:255];   // capture writes, in order (address)
  integer    nwrites;

  always @(posedge clk) begin
    if (cap_we) begin
      capmem[cap_addr]   <= cap_wdata;
      wrlog[nwrites]     <= cap_wdata;
      addrlog[nwrites]   <= cap_addr;
      nwrites            <= nwrites + 1;
    end
  end

  // ------------------------------------------------------------- checking
  integer errors;
  integer checks;

  `define CHK(cond, msg) \
    begin checks = checks + 1; \
      if (!(cond)) begin errors = errors + 1; $display("FAIL @%0t: %s", $time, msg); end \
    end

  // expected entries
  reg [15:0] exp_data [0:255];

  // ------------------------------------------------------------- helpers
  task do_reset;
    input [15:0] seed;
    integer k;
    begin
      rst_n     = 1'b0;
      cfg_en    = 1'b0;
      cfg_clear = 1'b0;
      cfg_mask  = 16'hFFFF;
      cfg_value = 16'h0000;
      cfg_div   = 4'd0;
      pins_in   = 16'h0000;
      tmr_seed  = seed;
      nwrites   = 0;
      for (k = 0; k < 64; k = k + 1) capmem[k] = 16'hDEAD;
      for (k = 0; k < 256; k = k + 1) begin
        wrlog[k]   = 16'hDEAD;
        addrlog[k] = 6'h3F;
      end
      repeat (3) @(negedge clk);
      rst_n = 1'b1;
      @(negedge clk);
    end
  endtask

  task arm_capture;
    begin
      @(negedge clk);
      cfg_en = 1'b1;
      @(negedge clk);
      cfg_en = 1'b0;
    end
  endtask

  task clear_capture;
    begin
      @(negedge clk);
      cfg_clear = 1'b1;
      @(negedge clk);
      cfg_clear = 1'b0;
    end
  endtask

  task wait_cyc;
    input integer n;
    begin
      repeat (n) @(negedge clk);
    end
  endtask

  // ------------------------------------------------------------- tests
  integer i;
  integer nexp;
  integer mcount;
  integer nw0;
  reg [7:0] nib;
  reg       wrap_seen;

  initial begin
    errors = 0;
    checks = 0;
    rst_n  = 1'b0;
    cfg_en = 1'b0; cfg_clear = 1'b0;
    cfg_mask = 16'hFFFF; cfg_value = 16'h0000; cfg_div = 4'd0;
    pins_in = 16'h0000; tmr_seed = 16'h0000;
    nwrites = 0;
    wait_cyc(1);

    // ---------------------------------------------------------- (0) reset
    do_reset(16'h0000);
    `CHK(cap_active === 0, "(0) reset: disarmed");
    `CHK(cap_full === 0,   "(0) reset: not full");
    `CHK(cap_count === 0,  "(0) reset: count 0");
    `CHK(cap_we === 0,     "(0) reset: no write");

    // ------------------------------- (a) trigger filtering and entry packing
    cfg_mask  = 16'h000F;
    cfg_value = 16'h000A;
    cfg_div   = 4'd0;
    arm_capture;
    `CHK(cap_active === 1, "(a) armed");
    `CHK(cap_full === 0,   "(a) not full");
    nexp = 0;
    for (i = 0; i < 10; i = i + 1) begin
      case (i)
        0, 1, 2, 5, 8, 9: nib = 8'h0A;
        default:          nib = 8'h0B;
      endcase
      pins_in = {8'h5A, 4'h0, nib};
      if (((pins_in & cfg_mask) == (cfg_value & cfg_mask))) begin
        exp_data[nexp] = {pins_in[7:0], tmr[7:0]};
        nexp = nexp + 1;
      end
      @(negedge clk);
    end
    pins_in = 16'h0000;                // stop matching (trigger filter test)
    wait_cyc(3);                       // let the registered write port drain
    $display("  (a) %0d of 10 cycles matched the trigger", nexp);
    `CHK(nexp === 6, "(a) expected 6 trigger matches");
    `CHK(nwrites === nexp, "(a) one entry per matching cycle");
    `CHK(cap_count === nexp, "(a) cap_count == entries recorded");
    `CHK(cap_active === 1 && cap_full === 0, "(a) status while armed");
    for (i = 0; i < nexp; i = i + 1) begin
      `CHK(wrlog[i] === exp_data[i], "(a) entry packing {pins[7:0],tmr[7:0]}");
      `CHK(addrlog[i] === i[5:0],    "(a) entries written in address order");
      `CHK(capmem[i] === exp_data[i],"(a) DMEM write port data");
    end
    // the packed pin byte must be the low pin byte, not the high one
    `CHK(exp_data[0][15:8] === 8'h0A, "(a) pin snapshot is pins_in[7:0]");

    // ------------------------------------------------ (b) cfg_div divider
    clear_capture;
    `CHK(cap_active === 0 && cap_count === 0 && cap_full === 0, "(b) clear works");
    cfg_mask  = 16'h00FF;
    cfg_value = 16'h00A5;
    cfg_div   = 4'd3;                  // record every 4th matching cycle
    nw0 = nwrites;
    nexp = 0;
    mcount = 0;
    arm_capture;
    for (i = 0; i < 16; i = i + 1) begin
      pins_in = {8'h11, 8'hA5};        // always matches
      mcount = mcount + 1;
      if ((mcount % 4) == 0) begin
        exp_data[nexp] = {pins_in[7:0], tmr[7:0]};
        nexp = nexp + 1;
      end
      @(negedge clk);
    end
    pins_in = 16'h0000;                // stop matching
    wait_cyc(3);
    `CHK(nexp === 4, "(b) 16 matches at cfg_div=3 -> 4 entries");
    `CHK((nwrites - nw0) === nexp, "(b) divider limits the write rate");
    for (i = 0; i < nexp; i = i + 1) begin
      `CHK(wrlog[nw0 + i] === exp_data[i], "(b) divider keeps every 4th match");
    end

    // ------------------------------------------------- (c) full buffer
    clear_capture;
    cfg_mask  = 16'hFFFF;
    cfg_value = 16'hC35A;
    cfg_div   = 4'd0;
    nw0 = nwrites;
    mcount = 0;
    arm_capture;
    for (i = 0; i < 70; i = i + 1) begin   // 70 matching cycles, 64 fit
      pins_in = 16'hC35A;
      if (mcount < 64) exp_data[mcount] = {pins_in[7:0], tmr[7:0]};
      mcount = mcount + 1;
      @(negedge clk);
    end
    wait_cyc(3);
    `CHK((nwrites - nw0) === 64, "(c) exactly 64 entries written");
    `CHK(cap_full === 1,   "(c) cap_full set");
    `CHK(cap_active === 0, "(c) capture disarmed when full");
    `CHK(cap_count === 63, "(c) cap_count saturates at 63 when full");
    for (i = 0; i < 64; i = i + 1) begin
      `CHK(addrlog[nw0 + i] === i[5:0], "(c) addresses 0..63 in order");
      `CHK(capmem[i] === exp_data[i],   "(c) buffer contents (no overwrite)");
    end

    // ------------------------------- (d) no writes once cleared/disarmed
    clear_capture;
    `CHK(cap_count === 0, "(d) clear resets count");
    `CHK(cap_full === 0,  "(d) clear resets full");
    `CHK(cap_active === 0,"(d) clear disarms");
    nw0 = nwrites;
    for (i = 0; i < 5; i = i + 1) begin
      pins_in = 16'hC35A;              // would match if still armed
      @(negedge clk);
    end
    wait_cyc(2);
    `CHK(nwrites === nw0, "(d) no capture while disarmed");

    // --------------------- (e) masked compare ignores value bits outside mask
    cfg_mask  = 16'h00F0;
    cfg_value = 16'h00A5;              // effective expected value = 0xA0
    cfg_div   = 4'd0;
    nw0 = nwrites;
    nexp = 0;
    arm_capture;
    for (i = 0; i < 8; i = i + 1) begin
      case (i)
        0: pins_in = {8'h3C, 8'hA0};   // match
        1: pins_in = {8'h3C, 8'h55};   // no match
        2: pins_in = {8'h3C, 8'hAF};   // match (low nibble irrelevant)
        3: pins_in = {8'h3C, 8'hB0};   // no match
        4: pins_in = {8'h3C, 8'hA1};   // match
        5: pins_in = {8'h3C, 8'h00};   // no match
        6: pins_in = {8'h3C, 8'hA0};   // match
        7: pins_in = {8'h3C, 8'hFF};   // no match
      endcase
      if (((pins_in & cfg_mask) == (cfg_value & cfg_mask))) begin
        exp_data[nexp] = {pins_in[7:0], tmr[7:0]};
        nexp = nexp + 1;
      end
      @(negedge clk);
    end
    pins_in = 16'h0000;                // stop matching
    wait_cyc(3);
    `CHK(nexp === 4, "(e) masked compare: 4 of 8 cycles match");
    `CHK((nwrites - nw0) === nexp, "(e) masked compare entry count");
    for (i = 0; i < nexp; i = i + 1) begin
      `CHK(wrlog[nw0 + i] === exp_data[i], "(e) masked compare entry data");
    end

    // ------------------------------------- (f) timestamp wrap at tmr[7:0]
    do_reset(16'hFFF8);               // tmr starts near its 8-bit wrap
    cfg_mask  = 16'h0000;             // mask 0 -> match every cycle
    cfg_value = 16'h0000;
    cfg_div   = 4'd0;
    arm_capture;
    `CHK(cap_active === 1, "(f) armed with mask 0");
    nw0 = nwrites;
    for (i = 0; i < 10; i = i + 1) begin
      pins_in = 16'hC35A;
      exp_data[i] = {pins_in[7:0], tmr[7:0]};
      @(negedge clk);
    end
    cfg_mask = 16'hFFFF;               // stop matching before draining
    cfg_value = 16'h0000;
    wait_cyc(3);
    `CHK((nwrites - nw0) === 10, "(f) mask 0 records every cycle");
    wrap_seen = 1'b0;
    for (i = 0; i < 9; i = i + 1) begin
      `CHK(wrlog[nw0 + i] === exp_data[i], "(f) timestamp low byte of tmr");
      if ((exp_data[i][7:0] === 8'hFF) && (exp_data[i+1][7:0] === 8'h00))
        wrap_seen = 1'b1;
    end
    `CHK(wrlog[nw0 + 9] === exp_data[9], "(f) timestamp low byte of tmr");
    `CHK(wrap_seen === 1'b1, "(f) test really crossed the 8-bit timestamp wrap");

    // ------------------------------------------ (g) re-arm clears pointer
    cfg_mask  = 16'hFFFF;             // not matching, so pre-arm cycles record
    cfg_value = 16'h0000;             // nothing
    nw0 = nwrites;
    arm_capture;                      // re-arm while already armed
    `CHK(cap_count === 0, "(g) re-arm clears the write pointer");
    `CHK(cap_full === 0,  "(g) re-arm clears full");
    `CHK(cap_active === 1,"(g) re-arm keeps capture active");
    cfg_mask  = 16'h0000;             // now match every cycle
    cfg_value = 16'h0000;
    for (i = 0; i < 3; i = i + 1) begin
      pins_in = 16'hC35A;
      @(negedge clk);
    end
    cfg_mask = 16'hFFFF;               // stop matching before draining
    cfg_value = 16'h0000;
    wait_cyc(2);
    `CHK((nwrites - nw0) === 3, "(g) capture runs again from entry 0");
    `CHK(addrlog[nw0] === 6'd0 && addrlog[nw0+1] === 6'd1 && addrlog[nw0+2] === 6'd2,
         "(g) addresses restart at 0");

    // ------- (h) level-style cfg_en (a held CSR bit, as the top drives it)
    // A held enable must arm once (rising edge) and then record normally; the
    // old pulse-only behaviour would re-arm every cycle and record nothing.
    clear_capture;
    cfg_mask  = 16'h00FF;
    cfg_value = 16'h0033;
    nw0 = nwrites;
    @(negedge clk);
    cfg_en = 1'b1;                     // 0 -> 1: arms
    wait_cyc(1);
    `CHK(cap_active === 1, "(h) level cfg_en arms on its rising edge");
    nexp = 0;
    for (i = 0; i < 6; i = i + 1) begin
      pins_in = 16'h0033;              // always matches
      exp_data[nexp] = {pins_in[7:0], tmr[7:0]};
      nexp = nexp + 1;
      @(negedge clk);
    end
    `CHK(cap_active === 1, "(h) holding cfg_en high does not re-arm");
    pins_in = 16'h0000;                // stop matching
    wait_cyc(2);
    `CHK((nwrites - nw0) === nexp,
         "(h) a held cfg_en records every match (arms once)");
    for (i = 0; i < nexp; i = i + 1) begin
      `CHK(wrlog[nw0 + i] === exp_data[i], "(h) level-enable entry data");
    end
    // dropping and re-raising cfg_en arms again from entry 0
    cfg_en = 1'b0;
    wait_cyc(1);
    @(negedge clk);
    cfg_en = 1'b1;
    wait_cyc(1);
    `CHK(cap_count === 0 && cap_active === 1, "(h) rising edge re-arms from 0");
    cfg_en = 1'b0;
    wait_cyc(1);

    // ---------------- (i) level-style cfg_clear (held CSR bit)
    cfg_clear = 1'b1;
    wait_cyc(1);
    `CHK(cap_active === 0 && cap_count === 0, "(i) level cfg_clear disarms/clears");
    nw0 = nwrites;
    for (i = 0; i < 3; i = i + 1) begin
      pins_in = 16'h0033;              // would match if armed
      @(negedge clk);
    end
    `CHK(nwrites === nw0 && cap_count === 0, "(i) held clear blocks recording");
    cfg_clear = 1'b0;
    wait_cyc(1);
    @(negedge clk);
    cfg_en = 1'b1;                     // re-arm after clear
    wait_cyc(1);
    `CHK(cap_active === 1, "(i) re-arm after clear works");
    cfg_en = 1'b0;
    wait_cyc(1);

    // ------------------------------------------------------------ verdict
    if (errors == 0) begin
      $display("CAPTURE_TB: PASS (%0d checks)", checks);
      $finish;
    end else begin
      $display("CAPTURE_TB: FAIL (%0d errors in %0d checks)", errors, checks);
      $fatal(1, "loom_capture testbench failed");
    end
  end

  // watchdog
  initial begin
    #1000000;
    $display("CAPTURE_TB: FAIL (timeout)");
    $fatal(1, "loom_capture testbench timeout");
  end

endmodule

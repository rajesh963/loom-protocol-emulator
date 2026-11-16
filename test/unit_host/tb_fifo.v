// ============================================================================
//  tb_fifo.v -- self-checking testbench for rtl/loom_fifo.v
//  Covers: reset state, full/empty/count, fall-through read, no-op on bad
//          rd/wr, pointer wrap-around, simultaneous push+pop,
//          DEPTH=8 (AW=3) and DEPTH=2 (AW=1) configurations.
//  Prints "FIFO_TB: PASS" on success; $fatal (non-zero exit) on failure.
// ============================================================================
`timescale 1ns/1ps
`default_nettype none

module tb_fifo;

  integer errors;
  reg     clk;
  reg     rst_n;
  integer i;

  always #5 clk = ~clk;      // 100 MHz

  // ---------------- DUT 1: DEPTH=8 ----------------
  reg        wr8, rd8;
  reg  [7:0] wd8;
  wire [7:0] rd8_data;
  wire       full8, empty8;
  wire [3:0] cnt8;

  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u8 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(wr8), .wr_data(wd8), .full(full8),
    .rd_en(rd8), .rd_data(rd8_data), .empty(empty8),
    .count(cnt8)
  );

  // ---------------- DUT 2: DEPTH=2 ----------------
  reg        wr2, rd2;
  reg  [7:0] wd2;
  wire [7:0] rd2_data;
  wire       full2, empty2;
  wire [1:0] cnt2;

  loom_fifo #(.DEPTH(2), .AW(1), .WIDTH(8)) u2 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(wr2), .wr_data(wd2), .full(full2),
    .rd_en(rd2), .rd_data(rd2_data), .empty(empty2),
    .count(cnt2)
  );

  // ---------------- checking helpers ----------------
  task chk_int(input [255:0] msg, input [31:0] got, input [31:0] exp);
    begin
      if (got !== exp) begin
        errors = errors + 1;
        $display("FAIL: %0s : got 0x%0h expected 0x%0h", msg, got, exp);
      end
    end
  endtask

  // push one byte into DUT1 (drive on negedge, sampled on the following posedge)
  task push8(input [7:0] d);
    begin
      @(negedge clk); wr8 = 1'b1; wd8 = d;
      @(negedge clk); wr8 = 1'b0; wd8 = 8'h00;
      #1;
    end
  endtask

  // pop one byte from DUT1
  task pop8;
    begin
      @(negedge clk); rd8 = 1'b1;
      @(negedge clk); rd8 = 1'b0;
      #1;
    end
  endtask

  task push2(input [7:0] d);
    begin
      @(negedge clk); wr2 = 1'b1; wd2 = d;
      @(negedge clk); wr2 = 1'b0; wd2 = 8'h00;
      #1;
    end
  endtask

  task pop2;
    begin
      @(negedge clk); rd2 = 1'b1;
      @(negedge clk); rd2 = 1'b0;
      #1;
    end
  endtask

  // ---------------- stimulus ----------------
  initial begin
    errors = 0;
    clk    = 1'b0;
    rst_n  = 1'b0;
    wr8 = 1'b0; rd8 = 1'b0; wd8 = 8'h00;
    wr2 = 1'b0; rd2 = 1'b0; wd2 = 8'h00;
    repeat (3) @(posedge clk);
    #1;
    rst_n = 1'b1;
    repeat (2) @(posedge clk);
    #1;

    // ================= DEPTH=8 =================
    chk_int("d8 empty after reset", {31'd0, empty8}, 32'd1);
    chk_int("d8 full after reset",  {31'd0, full8},  32'd0);
    chk_int("d8 count after reset", {28'd0, cnt8},   32'd0);

    // bad read on empty: no-op
    @(negedge clk); rd8 = 1'b1;
    @(negedge clk); rd8 = 1'b0; #1;
    chk_int("d8 count after bad rd", {28'd0, cnt8}, 32'd0);
    chk_int("d8 empty after bad rd", {31'd0, empty8}, 32'd1);

    // push 3
    push8(8'hA1); push8(8'hB2); push8(8'hC3);
    chk_int("d8 count=3",     {28'd0, cnt8}, 32'd3);
    chk_int("d8 count reg 3", {28'd0, cnt8}, 32'd3);
    chk_int("d8 not empty",   {31'd0, empty8}, 32'd0);
    chk_int("d8 not full",    {31'd0, full8},  32'd0);
    chk_int("d8 fallthrough head A1", {24'd0, rd8_data}, 32'hA1);

    // pop one: head must become B2 (fall-through, no output register)
    pop8;
    chk_int("d8 count=2",     {28'd0, cnt8}, 32'd2);
    chk_int("d8 head after pop B2", {24'd0, rd8_data}, 32'hB2);

    // fill to full (6 more)
    push8(8'hD4); push8(8'hE5); push8(8'hF6);
    push8(8'h17); push8(8'h28); push8(8'h39);
    chk_int("d8 count=8",     {28'd0, cnt8}, 32'd8);
    chk_int("d8 full",        {31'd0, full8}, 32'd1);

    // bad write while full: no-op
    push8(8'h99);
    chk_int("d8 count after bad wr", {28'd0, cnt8}, 32'd8);
    chk_int("d8 head unchanged",     {24'd0, rd8_data}, 32'hB2);

    // drain: B2 C3 D4 E5 F6 17 28 39
    chk_int("d8 drain 0", {24'd0, rd8_data}, 32'hB2); pop8;
    chk_int("d8 drain 1", {24'd0, rd8_data}, 32'hC3); pop8;
    chk_int("d8 drain 2", {24'd0, rd8_data}, 32'hD4); pop8;
    chk_int("d8 drain 3", {24'd0, rd8_data}, 32'hE5); pop8;
    chk_int("d8 drain 4", {24'd0, rd8_data}, 32'hF6); pop8;
    chk_int("d8 drain 5", {24'd0, rd8_data}, 32'h17); pop8;
    chk_int("d8 drain 6", {24'd0, rd8_data}, 32'h28); pop8;
    chk_int("d8 drain 7", {24'd0, rd8_data}, 32'h39); pop8;
    chk_int("d8 empty after drain", {31'd0, empty8}, 32'd1);
    chk_int("d8 count after drain", {28'd0, cnt8},   32'd0);

    // pointer wrap: 20 push/pop pairs (forces wr_ptr/rd_ptr to wrap the array)
    for (i = 0; i < 20; i = i + 1) begin
      push8(i[7:0]);
      chk_int("d8 wrap count 1", {28'd0, cnt8}, 32'd1);
      chk_int("d8 wrap head",    {24'd0, rd8_data}, {24'd0, i[7:0]});
      pop8;
      chk_int("d8 wrap count 0", {28'd0, cnt8}, 32'd0);
    end

    // simultaneous push + pop while half full: order preserved, count stable
    push8(8'h10); push8(8'h11);
    @(negedge clk); wr8 = 1'b1; rd8 = 1'b1; wd8 = 8'h12;
    @(negedge clk); wr8 = 1'b0; rd8 = 1'b0; wd8 = 8'h00; #1;
    chk_int("d8 sim count", {28'd0, cnt8}, 32'd2);
    chk_int("d8 sim head 0x11", {24'd0, rd8_data}, 32'h11);
    pop8;
    chk_int("d8 sim head 0x12", {24'd0, rd8_data}, 32'h12);
    pop8;
    chk_int("d8 sim empty", {31'd0, empty8}, 32'd1);

    // ================= DEPTH=2 =================
    chk_int("d2 empty after reset", {31'd0, empty2}, 32'd1);
    chk_int("d2 count after reset", {30'd0, cnt2},   32'd0);
    chk_int("d2 full after reset",  {31'd0, full2},  32'd0);

    push2(8'h5A); push2(8'hA5);
    chk_int("d2 count=2", {30'd0, cnt2}, 32'd2);
    chk_int("d2 full",    {31'd0, full2}, 32'd1);
    chk_int("d2 head 5A", {24'd0, rd2_data}, 32'h5A);

    push2(8'hFF);                       // dropped: full
    chk_int("d2 count after bad wr", {30'd0, cnt2}, 32'd2);
    chk_int("d2 head still 5A",      {24'd0, rd2_data}, 32'h5A);

    pop2;  chk_int("d2 head A5", {24'd0, rd2_data}, 32'hA5);
    push2(8'h11);                        // wraps wr_ptr
    pop2;  chk_int("d2 head 11", {24'd0, rd2_data}, 32'h11);
    push2(8'h22);
    pop2;  chk_int("d2 head 22", {24'd0, rd2_data}, 32'h22);
    pop2;  chk_int("d2 count=0", {30'd0, cnt2}, 32'd0);
    chk_int("d2 empty", {31'd0, empty2}, 32'd1);
    pop2;                                // no-op
    chk_int("d2 empty after bad rd", {31'd0, empty2}, 32'd1);
    chk_int("d2 count after bad rd", {30'd0, cnt2},   32'd0);

    // wrap many times
    for (i = 0; i < 10; i = i + 1) begin
      push2(i[7:0]);
      chk_int("d2 wrap head", {24'd0, rd2_data}, {24'd0, i[7:0]});
      pop2;
      chk_int("d2 wrap empty", {31'd0, empty2}, 32'd1);
    end

    repeat (2) @(posedge clk);
    if (errors == 0) begin
      $display("FIFO_TB: PASS");
      $finish;
    end else begin
      $display("FIFO_TB: FAIL (%0d errors)", errors);
      $fatal(1);
    end
  end

endmodule

`default_nettype wire

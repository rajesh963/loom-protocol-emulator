`default_nettype none
`timescale 1ns / 1ps
// End-to-end: bit-bang the SPI host link, load a program into PMEM, start a
// thread, and check that the hardware event scheduler produces an exact
// 16-cycle square wave on uo_out[2] (protocol pin P[10]).
module tb_top;
  reg clk = 0;
  always #5 clk = ~clk;              // 100 MHz system clock
  reg rst_n = 0, ena = 1;
  reg [7:0] ui_in = 0, uio_in = 0;
  wire [7:0] uo_out, uio_out, uio_oe;

  tt_um_rajesh963_loom dut (
      .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in),
      .uio_out(uio_out), .uio_oe(uio_oe), .ena(ena), .clk(clk), .rst_n(rst_n));

  reg [31:0] prog [0:1023];
  reg [7:0] r1, r2, r3;
  reg [31:0] rdword;
  integer errors = 0, cyc = 0, last_edge = -1, nedge = 0;
  reg prev_pin = 0;
  reg running = 0;
  integer b;

  // ---- SPI mode 0 byte transfer (10 MHz SCK, chip clock 100 MHz) -----------
  task spi(input [7:0] tx, output [7:0] rx);
    reg [7:0] r;
    begin
      r = 8'h00;
      for (b = 7; b >= 0; b = b - 1) begin
        ui_in[2] = tx[b];
        #50 ui_in[1] = 1'b1;
        #5 r[b] = uo_out[0];
        #45 ui_in[1] = 1'b0;
        #50;
      end
      rx = r;
    end
  endtask

  task cs_begin; begin @(negedge clk); ui_in[0] = 1'b0; #120; end endtask
  task cs_end;   begin #120; ui_in[0] = 1'b1; ui_in[1] = 1'b0; @(negedge clk); #200; end endtask

  task wr_mem(input [9:0] a, input [31:0] d);
    begin
      cs_begin;
      spi(8'h01, r1);
      spi(a[7:0], r1); spi({6'd0, a[9:8]}, r1); spi(8'h00, r1);
      spi(8'h02, r1);
      spi(d[7:0], r1); spi(d[15:8], r1); spi(d[23:16], r1); spi(d[31:24], r1);
      cs_end;
    end
  endtask

  task rd_mem(input [9:0] a, output [31:0] d);
    begin
      cs_begin;
      spi(8'h01, r1);
      spi(a[7:0], r1); spi({6'd0, a[9:8]}, r1); spi(8'h00, r1);
      spi(8'h03, r1);
      spi(8'h00, r2); d[7:0]   = r2;
      spi(8'h00, r2); d[15:8]  = r2;
      spi(8'h00, r2); d[23:16] = r2;
      spi(8'h00, r2); d[31:24] = r2;
      cs_end;
    end
  endtask

  task rd_csr(input [7:0] a, output [15:0] d);
    begin
      cs_begin;
      spi(8'h05, r1); spi(a, r1);
      spi(8'h00, r2); d[7:0]  = r2;
      spi(8'h00, r2); d[15:8] = r2;
      cs_end;
    end
  endtask

  task wr_csr(input [7:0] a, input [15:0] d);
    begin
      cs_begin;
      spi(8'h04, r1); spi(a, r1); spi(d[7:0], r1); spi(d[15:8], r1);
      cs_end;
    end
  endtask

  // transition monitor for uo_out[2]
  always @(posedge clk) begin
    cyc = cyc + 1;
    if (running) begin
      if (uo_out[2] !== prev_pin) begin
        nedge = nedge + 1;
        if (last_edge >= 0) begin
          if ((cyc - last_edge) !== 16) begin
            errors = errors + 1;
            $display("ERROR: uo_out[2] edge after %0d cycles (expected 16) at cyc %0d",
                     cyc - last_edge, cyc);
          end
        end
        last_edge = cyc;
        prev_pin  = uo_out[2];
      end
    end
  end

  integer i;
  initial begin
    $readmemh("itest.hex", prog);
    repeat (10) @(posedge clk);
    rst_n = 1;
    repeat (10) @(posedge clk);

    // ---- PING ------------------------------------------------------------
    cs_begin;
    spi(8'h0D, r1); spi(8'h5A, r2); spi(8'h00, r3);
    cs_end;
    if (r2 !== 8'hA5) begin errors = errors + 1; $display("ERROR: PING returned %h", r2); end
    else $display("PING ok (0x%h)", r2);

    // ---- global CSR reads (VER-003 regression) ---------------------------
    rd_csr(8'h10, rdword[15:0]);          // POUT after reset = 0
    if (rdword[15:0] !== 16'h0000) begin
      errors = errors + 1; $display("ERROR: host POUT read %h (want 0000)", rdword[15:0]);
    end else $display("host global CSR read ok: POUT=0000");
    rd_csr(8'h12, rdword[15:0]);          // TMR must be nonzero after reset
    if (rdword[15:0] === 16'h0000) begin
      errors = errors + 1; $display("ERROR: host TMR read still 0000");
    end else $display("host TMR read ok: TMR=%h", rdword[15:0]);
    rd_csr(8'h11, rdword[15:0]);          // PDIR after reset = 0
    if (rdword[15:0] !== 16'h0000) begin
      errors = errors + 1; $display("ERROR: host PDIR read %h (want 0000)", rdword[15:0]);
    end

    // ---- program load ----------------------------------------------------
    for (i = 0; i < 7; i = i + 1) wr_mem(i[9:0], prog[i]);
    for (i = 0; i < 7; i = i + 1) begin
      rd_mem(i[9:0], rdword);
      $display("readback[%0d] = %h (want %h)", i, rdword, prog[i]);
      if (rdword !== prog[i]) begin
        errors = errors + 1;
        $display("ERROR: program readback %h != %h", rdword, prog[i]);
      end
    end

    // ---- start thread 0 --------------------------------------------------
    cs_begin; spi(8'h08, r1); spi(8'h00, r1); spi(8'h00, r1); spi(8'h00, r1); cs_end; // SET_PC t0=0
    cs_begin; spi(8'h09, r1); spi(8'h01, r1); cs_end;                                   // RUN mask=1
    running   = 1;
    prev_pin  = uo_out[2];
    repeat (400) @(posedge clk);
    running = 0;

    $display("edges=%0d errors=%0d", nedge, errors);
    if (errors != 0) begin $display("ITEST: FAIL"); $fatal(1); end
    if (nedge < 10)  begin $display("ITEST: FAIL (no activity)"); $fatal(1); end
    $display("ITEST: PASS");
    $finish;
  end

  initial begin #2000000; $display("ITEST: FAIL (timeout)"); $fatal(1); end
endmodule
`default_nettype wire

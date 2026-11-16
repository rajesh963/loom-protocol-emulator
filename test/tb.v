`default_nettype none
`timescale 1ns / 1ps
// ============================================================================
//  test/tb.v -- cocotb testbench top for the LOOM protocol loom
//
//  Instantiates the Tiny Tapeout top and adds a *pad model* so that the
//  bidirectional uio[7:0] pins behave like real open-drain / pull-up pads:
//
//      pad_uio[i] = (chip drives i ? chip_level : 1'b1)      // chip side
//                 & (external drives i ? ext_level : 1'b1)   // board side
//      uio_in = pad_uio
//
//  A line is only low if *somebody* pulls it low; that is exactly how I2C /
//  1-Wire / PS/2 open-drain buses work, and it makes the DUT's readback of its
//  own driven pins correct.  `pad_model=0` disables the loopback (uio_in = the
//  raw external value) for tests that just want to inject a pin vector.
//
//  The dedicated input pins ui_in[7:3] (protocol P[15:11]) are plain regs.
//  ui_in[2:0] are the host SPI pins (H_MOSI, H_SCK, H_CS_N) and are driven by
//  the cocotb LoomHost.
//
//  Waveforms: run with `make WAVES=1` / `+dump` to get tb.fst.
// ============================================================================
module tb ();

  // ---- clock / reset (cloth driven by cocotb, reset also by cocotb) -------
  reg clk   = 1'b0;
  reg rst_n = 1'b0;
  reg ena   = 1'b1;

  // ---- host SPI pins: ui_in[2:0] = MOSI, SCK, CS_N -------------------------
  // ui_in[7:3] = protocol inputs P[15:11] (driven by cocotb)
  reg [7:0] ui_in = 8'h05;      // CS_N high, SCK low, MOSI low, P[15:11]=0

  // ---- bidirectional uio[7:0] pad model -----------------------------------
  reg  [7:0] uio_ext    = 8'hFF;  // external level per pin (1 = high/pull-up)
  reg  [7:0] uio_ext_oe = 8'h00;  // 1 = board actively drives, 0 = pull-up/HiZ
  reg        pad_model  = 1'b1;   // 1 = open-drain wire-AND loopback
  wire [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  wire [7:0] chip_uio_level = (uio_out & uio_oe) | ~uio_oe;
  wire [7:0] ext_uio_level  = uio_ext | ~uio_ext_oe;

  // uio_in: real pad loopback in pad_model mode, plain injected vector else
  assign uio_in = pad_model ? (chip_uio_level & ext_uio_level) : uio_ext;

  // ---- DUT ----------------------------------------------------------------
  tt_um_rajesh963_loom dut (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

  // ---- optional wave dump -------------------------------------------------
  initial begin
    if ($test$plusargs("dump")) begin
      $dumpfile("tb.fst");
      $dumpvars(0, tb);
    end
  end

  // ---- transaction pin trace (for debugging a failing run) ----------------
  // Written by cocotb?  No: keep the testbench purely structural; all checks
  // live in test/*.py.  A few handy named wires for cocotb handles:
  wire [15:0] pins_out16 = {uo_out[7:2], 2'b00, uio_out};  // [9:8] = host bits
  wire [15:0] pins_in16  = {ui_in, uio_in};

endmodule
`default_nettype wire

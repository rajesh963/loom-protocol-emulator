// ============================================================================
//  loom_pmem_sram.v — LOOM program memory: IHP single-port SRAM macro wrapper
// ============================================================================
//  Owner: wavecapt (SRAM integration).  Verilog-2005, synthesizable.
//
//  Selectable macro (build define, see tools/select_build.sh):
//    (default)          RM_IHPSG13_1P_1024x32_c2_bm_bist : 1024 x 32,
//                       416.64 x 336.46 um (140 183 um^2), address [9:0]
//    -DLOOM_PMEM_512    RM_IHPSG13_1P_512x32_c2_bm_bist  :  512 x 32,
//                       416.64 x 191.34 um ( 79 720 um^2), address [8:0]
//  Both are one single-port SRAM: one access (read or write) per clock,
//  controls and address sampled on the rising edge, read data registered
//  (A_DOUT valid in the cycle after the access).
//
//  512-WORD BUILD CONSTRAINT (documented for firmware/docs):
//    IMEM has 4 program slots at word addresses 0x000 / 0x080 / 0x100 / 0x180
//    (128 words each).  0x180 + 125 = 509, so 512 words fit those four
//    programs exactly; nothing may be placed above word 509.  PC/inat
//    addresses >= 512 read HALT (32'h0200_0000), exactly like the FF-array
//    PMEM with PMEM_WORDS=512, so a runaway PC still stops harmlessly.
//
//  INTERFACE (identical cycle behaviour to the old FF-array PMEM):
//    * instruction fetch: imem_addr in cycle N -> imem_data valid in N+1;
//    * host write page:   host_we/host_addr/host_wdata, one word per cycle,
//                         committed at the end of that cycle;
//    * host read page:    host_re/host_addr in cycle N -> host_rdata and
//                         host_rvalid in cycle N+1 (1-cycle latency).
//
//  ARBITRATION (spec/ARCHITECTURE.md §5: the host owns PMEM while threads
//  are halted):   host write > host read > instruction fetch.
//  A fetch that loses the port is replaced by HALT so the core never sees
//  host data as an instruction; an out-of-range host access is ignored and a
//  read returns HALT.  On silicon the macro has no reset: the host must load
//  the program before use (the host protocol already requires this).
//
//  MACRO NOTES (IHP datasheet):
//    A_DLY must be tied to 1'b1; A_MEN enables every operation; A_REN reads,
//    A_WEN writes (both = write-through); A_BM is the per-bit write mask
//    (1 = write that bit; LOOM uses all ones); the A_BIST_* test port is
//    unused and tied to 0.
//
//  SYNTHESIS / SIMULATION
//    Synthesised with the macro as a black box: the (* blackbox *)
//    declaration lives in src/sram_bb.v (the TT flow reads src/), and the
//    physical views come from MACROS in src/config.json.  For simulation
//    rtl/loom_sram_model.v defines the selected macro behaviourally when
//    LOOM_SIM is set, so this wrapper's arbitration logic is the SAME RTL in
//    simulation and in silicon.  The macro choice is a `ifdef rather than a
//    generate-if parameter ON PURPOSE: a generate scope would rename the
//    flattened macro instance to u_mem.u_pmem.g_xxx.sram, which would have to
//    be mirrored in MACROS/PDN_MACRO_CONNECTIONS.
//
//  HIERARCHY / MACRO PLACEMENT
//    loom_mem instantiates this wrapper as `u_pmem` and the macro below is
//    named `sram`, so after synthesis flattening the macro's instance name is
//        u_mem.u_pmem.sram
//    That is the key used by the `MACROS` and `PDN_MACRO_CONNECTIONS` blocks
//    in src/config.json.
// ============================================================================

`default_nettype none

// This file is only compiled in the SRAM program-memory build: -DLOOM_PMEM_SRAM
// (see tools/select_build.sh / src/config.json VERILOG_DEFINES).  Keeping the
// wrapper inside the guard means the default FF build does not need the macro
// black box to lint, and non-SRAM flows never see the macro instance.
`ifdef LOOM_PMEM_SRAM

module loom_pmem_sram (
    input  wire        clk,
    input  wire        rst_n,
    // instruction fetch: address in cycle N -> imem_data in cycle N+1
    input  wire [9:0]  imem_addr,
    output wire [31:0] imem_data,
    // host write page (word committed at the end of the cycle)
    input  wire        host_we,
    input  wire [9:0]  host_addr,
    input  wire [31:0] host_wdata,
    // host read page: host_re in cycle N -> host_rdata/host_rvalid in N+1
    input  wire        host_re,
    output wire [31:0] host_rdata,
    output wire        host_rvalid
);

  localparam [31:0] HALT_WORD = 32'h0200_0000;

`ifdef LOOM_PMEM_512
  localparam [11:0] WORDS = 12'd512;
`else
  localparam [11:0] WORDS = 12'd1024;
`endif

  // -------------------------------------------------------------------------
  // Single-port arbitration: host write > host read > instruction fetch.
  // Out-of-range host accesses are dropped (read returns HALT) and do not
  // steal the port, matching the FF-array PMEM behaviour above PMEM_WORDS.
  // -------------------------------------------------------------------------
  wire in_range = (host_addr < WORDS);        // constant 1 in the 1024 build
  wire host_wr  = host_we && in_range;
  wire host_rd  = host_re && !host_we && in_range;
  wire fetch    = !host_wr && !host_rd;
  wire fetch_oob = (imem_addr >= WORDS);      // constant 0 in the 1024 build

  wire [9:0]  a_addr = (host_wr || host_rd) ? host_addr : imem_addr;
  wire        a_wen  = host_wr;
  wire        a_ren  = !host_wr;              // instruction fetch or host read

  // one-cycle-delayed access qualifiers (the macro output is registered)
  reg fetch_q;
  reg fetch_oob_q;
  reg host_rd_q;
  reg host_oob_q;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      fetch_q    <= 1'b0;
      fetch_oob_q<= 1'b0;
      host_rd_q  <= 1'b0;
      host_oob_q <= 1'b0;
    end else begin
      fetch_q     <= fetch;
      fetch_oob_q <= fetch && fetch_oob;
      host_rd_q   <= host_re;
      host_oob_q  <= host_re && !in_range;
    end
  end

  wire [31:0] a_dout;

  // DOUT during cycle N+1 belongs to the access issued in cycle N: give it to
  // the fetch port only if that access was an in-range instruction fetch.
  assign imem_data   = (fetch_q && !fetch_oob_q) ? a_dout : HALT_WORD;
  assign host_rdata  = host_oob_q ? HALT_WORD : a_dout;
  assign host_rvalid = host_rd_q;

  // -------------------------------------------------------------------------
  // The macro.  Instance name `sram` is the key used in src/config.json.
  // The `ifdef keeps the flattened instance path stable (u_mem.u_pmem.sram).
  // -------------------------------------------------------------------------
`ifdef LOOM_PMEM_512
  RM_IHPSG13_1P_512x32_c2_bm_bist sram (
      .A_CLK      (clk),
      .A_MEN      (1'b1),             // A_MEN enables every operation
      .A_WEN      (a_wen),
      .A_REN      (a_ren),
      .A_ADDR     (a_addr[8:0]),
      .A_DIN      (host_wdata),
      .A_DLY      (1'b1),             // datasheet: must be tied high
      .A_DOUT     (a_dout),
      .A_BM       (32'hFFFF_FFFF),    // full-word writes
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (32'd0),
      .A_BIST_BM  (32'd0)
  );
`else
  RM_IHPSG13_1P_1024x32_c2_bm_bist sram (
      .A_CLK      (clk),
      .A_MEN      (1'b1),             // A_MEN enables every operation
      .A_WEN      (a_wen),
      .A_REN      (a_ren),
      .A_ADDR     (a_addr[9:0]),
      .A_DIN      (host_wdata),
      .A_DLY      (1'b1),             // datasheet: must be tied high
      .A_DOUT     (a_dout),
      .A_BM       (32'hFFFF_FFFF),    // full-word writes
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(10'd0),
      .A_BIST_DIN (32'd0),
      .A_BIST_BM  (32'd0)
  );
`endif

endmodule

`endif // LOOM_PMEM_SRAM

`default_nettype wire

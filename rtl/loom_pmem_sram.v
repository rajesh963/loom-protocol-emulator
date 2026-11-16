// ============================================================================
//  loom_pmem_sram.v — LOOM program memory: IHP RM_IHPSG13_1P_1024x32 SRAM
//                     wrapper (single read/write port, host + fetch access)
// ============================================================================
//  Owner: wavecapt (SRAM integration).  Verilog-2005, synthesizable.
//
//  The macro is one single-port 1024x32 SRAM: one access (read or write) per
//  clock, address and controls sampled on the rising edge, read data
//  registered (A_DOUT valid in the cycle after the access).  This wrapper
//  presents the two clients the LOOM memory subsystem needs:
//
//    * instruction fetch: imem_addr in cycle N -> imem_data valid in cycle
//      N+1 (exactly the semantics of the old 1024x32 FF-array PMEM, so the
//      core needs no change).  The macro's registered DOUT is used directly:
//      A_DOUT during cycle N+1 is the word addressed in cycle N.
//    * host write port:  host_we + host_addr + host_wdata, one word write
//      per cycle, executed at the end of that cycle (registered inputs).
//    * host read port:   host_re + host_addr in cycle N -> host_rdata and
//      host_rvalid in cycle N+1 (same 1-cycle latency as the FF build).
//
//  ARBITRATION (see spec/ARCHITECTURE.md §5: DMEM has priority semantics,
//  PMEM is host-owned while threads are halted):
//      host write  >  host read  >  instruction fetch
//  The host may only program/read the program memory while the threads are
//  halted (host protocol contract); a fetch that loses the port is replaced
//  by the HALT word (32'h0200_0000) so the core never sees host data as an
//  instruction.  On real silicon the macro has no reset: the host must load
//  the program before use, as the host protocol already requires.
//
//  MACRO NOTES (from the IHP datasheet, RM_IHPSG13_1P_1024x32_c2_bm_bist):
//    A_DLY     must be tied to 1'b1 (liberty characterised with it high),
//    A_MEN=1   enables ANY operation, A_REN=1 reads, A_WEN=1 writes
//              (both = write-through),
//    A_BM      per-bit write mask, 1 = write that bit (we use all ones),
//    A_BIST_*  second parallel test port, unused -> tied to 0.
//
//  SYNTHESIS / SIMULATION
//    For synthesis the macro is a black box: the (* blackbox *) declaration
//    is in this file (there is no PDK Verilog model in the flow), and its
//    GDS/LEF/Liberty views come from the MACROS block in src/config.json.
//    For simulation `rtl/loom_sram_model.v` defines the same macro module
//    behaviourally when LOOM_SIM is defined, so this wrapper's arbitration
//    logic is the SAME RTL in simulation and in silicon.
//
//  HIERARCHY / MACRO PLACEMENT
//    loom_mem instantiates this wrapper as `u_pmem`, and the macro below is
//    named `sram`, so after synthesis flattening its hierarchical instance
//    name is `u_mem.u_pmem.sram`; that is the key used in the `MACROS` and
//    `PDN_MACRO_CONNECTIONS` blocks of src/config.json.
// ============================================================================

`default_nettype none

// ---------------------------------------------------------------------------
// Black-box declaration of the IHP macro (synthesis + lint only; the
// behavioural model in loom_sram_model.v replaces it when LOOM_SIM is set).
// ---------------------------------------------------------------------------
`ifndef LOOM_SIM
/* verilator lint_off UNDRIVEN */
/* verilator lint_off UNUSEDSIGNAL */
(* blackbox *)
module RM_IHPSG13_1P_1024x32_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [9:0]  A_ADDR,
    input  wire [31:0] A_DIN,
    input  wire        A_DLY,
    output wire [31:0] A_DOUT,
    input  wire [31:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [9:0]  A_BIST_ADDR,
    input  wire [31:0] A_BIST_DIN,
    input  wire [31:0] A_BIST_BM
);
endmodule
/* verilator lint_on UNUSEDSIGNAL */
/* verilator lint_on UNDRIVEN */
`endif

module loom_pmem_sram (
    input  wire        clk,
    input  wire        rst_n,
    // instruction fetch: address in cycle N -> imem_data in cycle N+1
    input  wire [9:0]  imem_addr,
    output wire [31:0] imem_data,
    // host write port (PMEM word address, executed at the end of the cycle)
    input  wire        host_we,
    input  wire [9:0]  host_addr,
    input  wire [31:0] host_wdata,
    // host read port: host_re in cycle N -> host_rdata/host_rvalid in N+1
    input  wire        host_re,
    output wire [31:0] host_rdata,
    output wire        host_rvalid
);

  localparam [31:0] HALT_WORD = 32'h0200_0000;

  // -------------------------------------------------------------------------
  // Single-port arbitration: host write > host read > instruction fetch.
  // -------------------------------------------------------------------------
  wire host_wr = host_we;
  wire host_rd = host_re && !host_we;
  wire fetch   = !host_wr && !host_rd;

  wire [9:0]  a_addr = (host_wr || host_rd) ? host_addr : imem_addr;
  wire        a_wen  = host_wr;
  wire        a_ren  = !host_wr;        // fetch or host read

  // one-cycle-delayed access qualifiers (the macro output is registered)
  reg fetch_q;
  reg host_rd_q;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      fetch_q   <= 1'b0;
      host_rd_q <= 1'b0;
    end else begin
      fetch_q   <= fetch;
      host_rd_q <= host_rd;
    end
  end

  wire [31:0] a_dout;

  // DOUT during cycle N+1 belongs to the access issued in cycle N: give it to
  // the fetch port only if that access was really an instruction fetch; a
  // stolen fetch returns HALT (threads are halted while the host uses PMEM).
  assign imem_data   = fetch_q ? a_dout : HALT_WORD;
  assign host_rdata  = a_dout;      // valid while host_rvalid is high
  assign host_rvalid = host_rd_q;

  // -------------------------------------------------------------------------
  // The macro.  Instance name `sram` is used by src/config.json.
  // -------------------------------------------------------------------------
  RM_IHPSG13_1P_1024x32_c2_bm_bist sram (
      .A_CLK      (clk),
      .A_MEN      (1'b1),           // A_MEN enables every operation
      .A_WEN      (a_wen),
      .A_REN      (a_ren),
      .A_ADDR     (a_addr),
      .A_DIN      (host_wdata),
      .A_DLY      (1'b1),           // datasheet: must be tied high
      .A_DOUT     (a_dout),
      .A_BM       (32'hFFFF_FFFF),  // full-word writes
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(10'd0),
      .A_BIST_DIN (32'd0),
      .A_BIST_BM  (32'd0)
  );

endmodule

`default_nettype wire

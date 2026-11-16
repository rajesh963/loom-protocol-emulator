// ============================================================================
//  loom_sram_model.v — behavioural simulation model of the IHP
//                      RM_IHPSG13_1P_1024x32_c2_bm_bist SRAM macro
// ============================================================================
//  Owner: wavecapt (SRAM integration).  SIMULATION ONLY.
//
//  This file defines the macro module behaviourally when LOOM_SIM is defined,
//  so the cocotb suite (and any Icarus/Verilator RTL simulation) runs without
//  the IHP PDK.  It is guarded by `ifdef LOOM_SIM`: synthesis, LibreLane and
//  the TT GDS action compile this file to nothing and use the
//  (* blackbox *) declaration in rtl/loom_pmem_sram.v instead (the physical
//  views come from the MACROS block in src/config.json).
//
//  The model implements the macro, NOT the wrapper, so rtl/loom_pmem_sram.v's
//  arbitration / HALT / host-read logic is the same RTL in simulation and in
//  silicon — the full cocotb regression exercises it.
//
//  BEHAVIOUR (IHP datasheet, RM_IHPSG13_1P_1024x32_c2_bm_bist):
//    * all inputs sampled on the rising edge of A_CLK;
//    * A_MEN=1 is required for any operation: A_REN=1 reads, A_WEN=1 writes,
//      both = write-through (outputs the new word);
//    * read data is registered: address/REN on edge N -> A_DOUT valid in
//      cycle N+1 (one-cycle data access);
//    * A_BM is a per-bit write mask, 1 = write that bit;
//    * A_DLY must be tied high by the wrapper (timing parameter only here);
//    * the A_BIST_* port is a second test port, unused by LOOM.
//
//  Unlike real silicon (which has no reset), the model starts up full of HALT
//  words (32'h0200_0000) to match the FF-array PMEM behaviour and keep
//  simulation deterministic; the host programs PMEM before use anyway.
// ============================================================================

`default_nettype none

`ifdef LOOM_SIM

module RM_IHPSG13_1P_1024x32_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [9:0]  A_ADDR,
    input  wire [31:0] A_DIN,
    input  wire        A_DLY,
    output reg  [31:0] A_DOUT,
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

  localparam [31:0] HALT_WORD = 32'h0200_0000;

  reg [31:0] mem [0:1023];
  integer i;
  initial begin
    for (i = 0; i < 1024; i = i + 1) mem[i] = HALT_WORD;
  end

  // write data with the per-bit mask applied; the non-masked bits keep their
  // old value (A_BM = all ones for LOOM's full-word writes)
  wire [31:0] wr_data = (A_DIN & A_BM) | (mem[A_ADDR] & ~A_BM);

  always @(posedge A_CLK) begin
    if (A_MEN) begin
      if (A_WEN) mem[A_ADDR] <= wr_data;
      if (A_REN) A_DOUT <= A_WEN ? wr_data : mem[A_ADDR];
    end
  end

  // The BIST port is unused; keep the inputs referenced for lint cleanliness.
  wire _unused = &{A_DLY, A_BIST_CLK, A_BIST_EN, A_BIST_MEN, A_BIST_WEN,
                   A_BIST_REN, A_BIST_ADDR, A_BIST_DIN, A_BIST_BM, 1'b0};

endmodule

`endif // LOOM_SIM

`default_nettype wire

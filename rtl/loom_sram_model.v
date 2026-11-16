// ============================================================================
//  loom_sram_model.v — behavioural simulation model of the IHP single-port
//                      SRAM macro used by LOOM's program memory
// ============================================================================
//  Owner: wavecapt (SRAM integration).  SIMULATION ONLY.
//
//  Defines the selected macro module behaviourally when LOOM_SIM is set, so
//  the cocotb suite (and any Icarus/Verilator RTL simulation) runs without the
//  IHP PDK.  Without LOOM_SIM this file compiles to nothing: synthesis and
//  LibreLane use the (* blackbox *) declaration in src/sram_bb.v and the
//  physical views from MACROS in src/config.json instead.
//
//  Selected macro (same switch as rtl/loom_pmem_sram.v):
//    (default)        RM_IHPSG13_1P_1024x32_c2_bm_bist : 1024 x 32, [9:0]
//    -DLOOM_PMEM_512  RM_IHPSG13_1P_512x32_c2_bm_bist  :  512 x 32, [8:0]
//
//  The model implements the macro, NOT the wrapper, so
//  rtl/loom_pmem_sram.v's arbitration / HALT / host-read logic is the SAME RTL
//  in simulation and in silicon and the cocotb regression exercises it.
//
//  BEHAVIOUR (IHP datasheet, RM_IHPSG13_1P_*):
//    * all inputs sampled on the rising edge of A_CLK;
//    * A_MEN=1 is required for any operation: A_REN=1 reads, A_WEN=1 writes,
//      both = write-through (outputs the new word);
//    * read data is registered: address/REN on edge N -> A_DOUT valid in
//      cycle N+1 (one-cycle data access);
//    * A_BM is the per-bit write mask (1 = write that bit);
//    * A_DLY is a timing parameter, tied high by the wrapper;
//    * the A_BIST_* port is a second test port, unused by LOOM.
//
//  Unlike real silicon (no reset), the model starts up full of HALT words
//  (32'h0200_0000) to match the FF-array PMEM behaviour and keep simulation
//  deterministic; the host programs PMEM before use anyway.
// ============================================================================

`default_nettype none

`ifdef LOOM_SIM

`ifdef LOOM_PMEM_512
// ---------------------------------------------------------------------------
// 512 x 32 (area-reduced SRAM build; still fits the 4 x 128-word program slots
// at 0x000/0x080/0x100/0x180, top address 509 -> see loom_pmem_sram.v)
// ---------------------------------------------------------------------------
module RM_IHPSG13_1P_512x32_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [8:0]  A_ADDR,
    input  wire [31:0] A_DIN,
    input  wire        A_DLY,
    output reg  [31:0] A_DOUT,
    input  wire [31:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [8:0]  A_BIST_ADDR,
    input  wire [31:0] A_BIST_DIN,
    input  wire [31:0] A_BIST_BM
);

  localparam [31:0] HALT_WORD = 32'h0200_0000;

  reg [31:0] mem [0:511];
  integer i;
  initial begin
    for (i = 0; i < 512; i = i + 1) mem[i] = HALT_WORD;
  end

  wire [31:0] wr_data = (A_DIN & A_BM) | (mem[A_ADDR] & ~A_BM);

  always @(posedge A_CLK) begin
    if (A_MEN) begin
      if (A_WEN) mem[A_ADDR] <= wr_data;
      if (A_REN) A_DOUT <= A_WEN ? wr_data : mem[A_ADDR];
    end
  end

  wire _unused = &{A_DLY, A_BIST_CLK, A_BIST_EN, A_BIST_MEN, A_BIST_WEN,
                   A_BIST_REN, A_BIST_ADDR, A_BIST_DIN, A_BIST_BM, 1'b0};

endmodule

`else
// ---------------------------------------------------------------------------
// 1024 x 32 (default SRAM build)
// ---------------------------------------------------------------------------
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

  wire [31:0] wr_data = (A_DIN & A_BM) | (mem[A_ADDR] & ~A_BM);

  always @(posedge A_CLK) begin
    if (A_MEN) begin
      if (A_WEN) mem[A_ADDR] <= wr_data;
      if (A_REN) A_DOUT <= A_WEN ? wr_data : mem[A_ADDR];
    end
  end

  wire _unused = &{A_DLY, A_BIST_CLK, A_BIST_EN, A_BIST_MEN, A_BIST_WEN,
                   A_BIST_REN, A_BIST_ADDR, A_BIST_DIN, A_BIST_BM, 1'b0};

endmodule

`endif // LOOM_PMEM_512

`endif // LOOM_SIM

`default_nettype wire

// ============================================================================
// loom_mem.v — LOOM memory subsystem
//
//   PMEM : program memory, two interchangeable build flavours with IDENTICAL
//          cycle behaviour (address in cycle N -> data to the core in N+1):
//            FF build (default) : flip-flop array, PMEM_WORDS x 32
//                                 (128 words with -DLOOM_PMEM_SMALL — the
//                                 area-safe fallback build; 1024 in sim).
//            SRAM build         : one IHP single-port SRAM macro through
//                                 rtl/loom_pmem_sram.v, selected with
//                                 -DLOOM_PMEM_SRAM (see tools/select_build.sh):
//                                   RM_IHPSG13_1P_1024x32 (140 183 um^2, 1024
//                                   words) by default, or
//                                   RM_IHPSG13_1P_512x32 ( 79 720 um^2,  512
//                                   words) with -DLOOM_PMEM_512.
//          The flavour is chosen by the build define, not by the PMEM_IMPL
//          parameter, because (a) the TinyTapeout flow selects builds through
//          VERILOG_DEFINES and (b) a generate-if would add a hierarchy level
//          and rename the flattened macro instance (which must stay
//          `u_mem.u_pmem.sram` for MACROS/PDN_MACRO_CONNECTIONS).  PMEM_IMPL
//          is kept as the interface default ("ff") for compatibility.
//          SRAM simulation additionally needs -DLOOM_SIM so that
//          rtl/loom_sram_model.v provides the macro behaviourally (no PDK).
//
//   DMEM : DMEM_WORDS x 16 FF array (64, or 32 with -DLOOM_DMEM_SMALL),
//          combinational read, three write sources
//          (core instruction > host > capture engine).  Addresses fold
//          modulo DMEM_WORDS (DMEM_MASK).
//
// Host (linear 10-bit) address map:
//   0x000 .. 0x3BF   PMEM word (words above the implemented depth read HALT)
//   0x3C0 .. 0x3FF   DMEM word (16-bit, returned zero-extended)
//
// Host read timing is identical in both flavours: host_re in cycle N ->
// host_rvalid + host_rdata in cycle N+1.
//
// NOTE ON HIERARCHY: the FF-array PMEM stays at module scope (`u_mem.pmem`)
// so test/integration/tb_top.v can preload it through the hierarchy; the SRAM
// branch replaces it with the wrapper and therefore has no `pmem` array.
//
// Owner: Lead.  PMEM SRAM flavour + wrapper added by wavecapt.
// ============================================================================
`default_nettype none
`include "loom_bus.vh"

module loom_mem #(
    parameter PMEM_WORDS = 1024,
    parameter DMEM_WORDS = 64,
    // "ff" = flip-flop array (portable).  "sram" = IHP SRAM macro wrapper.
    // The SRAM build is selected by the -DLOOM_PMEM_SRAM build define (the
    // top-level instance keeps its "ff" default); see the header.
    parameter PMEM_IMPL  = "ff"
) (
    input  wire        clk,
    input  wire        rst_n,
    // instruction fetch (1-cycle synchronous read)
    input  wire [9:0]  imem_addr,
    output wire [31:0] imem_data,
    // core data port
    input  wire [5:0]  dmem_addr,
    input  wire        dmem_we,
    input  wire [15:0] dmem_wdata,
    output wire [15:0] dmem_rdata,
    // wave player read port
    input  wire [5:0]  wave_addr,
    output wire [15:0] wave_data,
    // capture engine write port
    input  wire        cap_we,
    input  wire [5:0]  cap_addr,
    input  wire [15:0] cap_wdata,
    // host port
    input  wire        host_we,
    input  wire        host_re,
    input  wire [9:0]  host_addr,
    input  wire [31:0] host_wdata,
    output wire [31:0] host_rdata,
    output wire        host_rvalid
);

  localparam [9:0]  DMEM_BASE = 10'h3C0;
  localparam [31:0] HALT_WORD = 32'h0200_0000;
  // parameterised DMEM: addresses fold modulo DMEM_WORDS (power of two)
  localparam [5:0]  DMEM_MASK = DMEM_WORDS[5:0] - 6'd1;

  // host accesses aimed at the program memory (below the DMEM window)
  wire host_pmem_we = host_we && (host_addr < DMEM_BASE);
  wire host_pmem_re = host_re && (host_addr < DMEM_BASE);

  // --------------------------------------------------------------------------
  // DMEM (combinational read)
  // --------------------------------------------------------------------------
  reg [15:0] dmem [0:DMEM_WORDS-1];
  integer j;
  initial begin
    for (j = 0; j < DMEM_WORDS; j = j + 1) dmem[j] = 16'd0;
  end

  assign dmem_rdata = dmem[dmem_addr & DMEM_MASK];
  assign wave_data  = dmem[wave_addr & DMEM_MASK];

  // --------------------------------------------------------------------------
  // DMEM writes: core instruction > host > capture
  // (host/capture must not race a running thread; documented in the datasheet)
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (dmem_we)
      dmem[dmem_addr & DMEM_MASK] <= dmem_wdata;
    else if (host_we && (host_addr >= DMEM_BASE))
      dmem[host_addr[5:0] & DMEM_MASK] <= host_wdata[15:0];
    else if (cap_we)
      dmem[cap_addr & DMEM_MASK] <= cap_wdata;
  end

  // --------------------------------------------------------------------------
  // PMEM — selected at compile time (see the header note on PMEM_IMPL)
  // --------------------------------------------------------------------------
  wire [31:0] pmem_host_rdata;   // PMEM host read data, valid with host_rvalid

`ifdef LOOM_PMEM_SRAM
  // ---------------- IHP SRAM macro build (rtl/loom_pmem_sram.v) ------------
  loom_pmem_sram u_pmem (
      .clk        (clk),
      .rst_n      (rst_n),
      .imem_addr  (imem_addr),
      .imem_data  (imem_data),
      .host_we    (host_pmem_we),
      .host_addr  (host_addr),
      .host_wdata (host_wdata),
      .host_re    (host_pmem_re),
      .host_rdata (pmem_host_rdata),
      .host_rvalid()
  );
`else
  // ---------------- FF-array PMEM build (default / fallback) ---------------
  reg [31:0] pmem [0:PMEM_WORDS-1];
  integer i;
  initial begin
    for (i = 0; i < PMEM_WORDS; i = i + 1) pmem[i] = HALT_WORD;
  end

  // 1-cycle synchronous read; out-of-range words read as HALT
  reg [31:0] imem_q;
  always @(posedge clk) begin
    if (imem_addr < PMEM_WORDS) imem_q <= pmem[imem_addr];
    else                        imem_q <= HALT_WORD;
  end
  assign imem_data = imem_q;

  // host program loading
  always @(posedge clk) begin
    if (host_pmem_we && (host_addr < PMEM_WORDS))
      pmem[host_addr] <= host_wdata;
  end

  // host readback (the DMEM mux below selects it only for PMEM reads)
  reg [31:0] host_pmem_q;
  always @(posedge clk) begin
    if (host_addr < PMEM_WORDS) host_pmem_q <= pmem[host_addr];
    else                        host_pmem_q <= HALT_WORD;
  end
  assign pmem_host_rdata = host_pmem_q;
`endif

  // --------------------------------------------------------------------------
  // Host read path (identical timing for both PMEM flavours):
  //   host_re in cycle N -> host_rvalid=1 in N+1, host_rdata valid in N+1.
  // --------------------------------------------------------------------------
  reg        host_rvalid_q;
  reg        host_dmem_sel_q;     // the previous host read was a DMEM word
  reg [15:0] host_dmem_q;         // registered DMEM read data

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      host_rvalid_q   <= 1'b0;
      host_dmem_sel_q <= 1'b0;
      host_dmem_q     <= 16'd0;
    end else begin
      host_rvalid_q   <= host_re;
      host_dmem_sel_q <= host_re && (host_addr >= DMEM_BASE);
      host_dmem_q     <= dmem[host_addr[5:0] & DMEM_MASK];
    end
  end

  assign host_rvalid = host_rvalid_q;
  assign host_rdata  = host_dmem_sel_q ? {16'd0, host_dmem_q} : pmem_host_rdata;

endmodule
`default_nettype wire

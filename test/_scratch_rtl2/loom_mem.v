// ============================================================================
// loom_mem.v — LOOM memory subsystem
//
//   PMEM : PMEM_WORDS x 32, 1-cycle synchronous read (instruction fetch),
//          written only by the host (program loading).
//   DMEM : 64 x 16 FF array, combinational read, three write sources
//          (core instruction > host > capture engine).
//
// Host (linear 10-bit) address map:
//   0x000 .. 0x3BF   PMEM word (words above PMEM_WORDS read as HALT)
//   0x3C0 .. 0x3FF   DMEM word (16-bit, returned zero-extended)
//
// Owner: Lead.
// ============================================================================
`default_nettype none
`include "loom_bus.vh"

module loom_mem #(
    parameter PMEM_WORDS = 1024,
    parameter DMEM_WORDS = 64,
    // "ff" = flip-flop array (portable).  "sram" = IHP 1024x32 macro wrapper.
    parameter PMEM_IMPL  = "ff"
) (
    input  wire        clk,
    input  wire        rst_n,
    // instruction fetch (1-cycle synchronous read)
    input  wire [9:0]  imem_addr,
    output reg  [31:0] imem_data,
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
    output reg  [31:0] host_rdata,
    output reg         host_rvalid
);

  localparam [9:0] DMEM_BASE = 10'h3C0;

  // --------------------------------------------------------------------------
  // PMEM
  // --------------------------------------------------------------------------
  reg [31:0] pmem [0:PMEM_WORDS-1];
  integer i;
  initial begin
    for (i = 0; i < PMEM_WORDS; i = i + 1) pmem[i] = 32'h0200_0000; // HALT
  end

  always @(posedge clk) begin
    if (imem_addr < PMEM_WORDS)
      imem_data <= pmem[imem_addr];
    else
      imem_data <= 32'h0200_0000;   // HALT: runaway PC stops harmlessly
  end

  // --------------------------------------------------------------------------
  // DMEM (64 x 16, combinational read)
  // --------------------------------------------------------------------------
  reg [15:0] dmem [0:DMEM_WORDS-1];
  integer j;
  initial begin
    for (j = 0; j < DMEM_WORDS; j = j + 1) dmem[j] = 16'd0;
  end

  assign dmem_rdata = dmem[dmem_addr];
  assign wave_data  = dmem[wave_addr];

  // --------------------------------------------------------------------------
  // Host port (write PMEM, write DMEM, read either)
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      host_rvalid <= 1'b0;
      host_rdata  <= 32'd0;
    end else begin
      host_rvalid <= host_re;
      if (host_re) begin
        if (host_addr >= DMEM_BASE)
          host_rdata <= {16'd0, dmem[host_addr[5:0]]};
        else if (host_addr < PMEM_WORDS)
          host_rdata <= pmem[host_addr];
        else
          host_rdata <= 32'h0200_0000;
      end
    end
  end

  // --------------------------------------------------------------------------
  // DMEM writes: core instruction > host > capture
  // (host/capture must not race a running thread; documented in the datasheet)
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (dmem_we)
      dmem[dmem_addr] <= dmem_wdata;
    else if (host_we && (host_addr >= DMEM_BASE))
      dmem[host_addr[5:0]] <= host_wdata[15:0];
    else if (cap_we)
      dmem[cap_addr] <= cap_wdata;
  end

  // --------------------------------------------------------------------------
  // PMEM writes (host program loading)
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (host_we && (host_addr < DMEM_BASE) && (host_addr < PMEM_WORDS))
      pmem[host_addr] <= host_wdata;
  end

endmodule
`default_nettype wire

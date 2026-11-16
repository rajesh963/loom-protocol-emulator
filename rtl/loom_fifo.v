// ============================================================================
//  loom_fifo.v -- parameterised synchronous FIFO (LOOM host/thread byte queues)
//
//  Interface (spec/OWNERS.md, frozen):
//    DEPTH  number of entries   (must be a power of two; DEPTH == 1<<AW)
//    AW     pointer width       (log2(DEPTH))
//    WIDTH  data width
//
//  Behaviour:
//    * fall-through read: rd_data is the current head, no output register
//    * full  : count == DEPTH (a wr_en while full is a no-op)
//    * empty : count == 0     (an rd_en while empty is a no-op)
//    * count : current occupancy, [AW:0]
//    * asynchronous active-low reset: empty, full = 0, count = 0, pointers = 0
//    * simultaneous rd+wr while not full/not empty: both happen, count unchanged
//      (a write while full is still dropped even if a read is concurrent -- the
//       user must look at `full`; this is the documented contract)
//
//  Synthesizable Verilog-2005, single clock, no initial blocks, no latches.
// ============================================================================
`default_nettype none

module loom_fifo #(
  parameter DEPTH = 8,
  parameter AW    = 3,
  parameter WIDTH = 8
)(
  input  wire             clk,
  input  wire             rst_n,
  input  wire             wr_en,
  input  wire [WIDTH-1:0] wr_data,
  output wire             full,
  input  wire             rd_en,
  output wire [WIDTH-1:0] rd_data,
  output wire             empty,
  output wire [AW:0]      count
);

  // Occupancy is measured in entries; DEPTH must be 1<<AW.
  localparam [AW:0] DEPTH_C = DEPTH;

  reg [WIDTH-1:0] mem [0:DEPTH-1];
  reg [AW-1:0]    wr_ptr;
  reg [AW-1:0]    rd_ptr;
  reg [AW:0]      cnt;

  wire do_wr = wr_en & ~full;
  wire do_rd = rd_en & ~empty;

  assign full    = (cnt == DEPTH_C);
  assign empty   = (cnt == {AW+1{1'b0}});
  assign count   = cnt;
  assign rd_data = mem[rd_ptr];

  always @(posedge clk) begin
    if (!rst_n) begin
      wr_ptr <= {AW{1'b0}};
      rd_ptr <= {AW{1'b0}};
      cnt    <= {(AW+1){1'b0}};
    end else begin
      if (do_wr) begin
        mem[wr_ptr] <= wr_data;
        wr_ptr      <= wr_ptr + 1'b1;
      end
      if (do_rd) begin
        rd_ptr <= rd_ptr + 1'b1;
      end
      cnt <= cnt + {{AW{1'b0}}, do_wr} - {{AW{1'b0}}, do_rd};
    end
  end

endmodule

`default_nettype wire

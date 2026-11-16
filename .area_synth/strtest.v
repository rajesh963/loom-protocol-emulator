`default_nettype none
module strtest #(parameter IMPL = "ff") (
  input wire a, output wire y
);
  `ifdef FORCE_SRAM
    localparam USE_SRAM = 1'b1;
  `else
    localparam USE_SRAM = 1'b0;
  `endif
  generate
    if (USE_SRAM || (IMPL == "sram")) begin : g_sram
      assign y = a;
    end else begin : g_ff
      assign y = ~a;
    end
  endgenerate
endmodule
`default_nettype wire

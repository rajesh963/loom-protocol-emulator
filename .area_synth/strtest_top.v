module strtest_top(input wire a, output wire y);
  strtest #(.IMPL("sram")) u(.a(a), .y(y));
endmodule

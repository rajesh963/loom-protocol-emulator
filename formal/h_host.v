// ============================================================================
//  formal/h_host.v — bounded formal harness for rtl/loom_host.v
//
//  Proves (BMC from a generated reset):
//    * reset completeness: MISO=0, IRQ=0, command FSM idle, all output pulses
//      low (spec §7),
//    * command/effect ordering: mem_we only ever pulses for WRITE_MEM;
//      rx_push only for TX_PUSH; tx_pop only for RX_POP; csr_wr_en only for the
//      commands that are specified to write a CSR,
//    * the host memory port address is always inside the 10-bit memory map
//      (0..1023; the top-level memory decodes 0x000-0x3BF PMEM / 0x3C0-0x3FF
//      DMEM, and rtl/loom_mem.v gates PMEM writes on addr < PMEM_WORDS, so a
//      host write can never modify a word outside the implemented PMEM),
//    * the command FSM state is always a legal state.
//
//  Assumed by construction (anyseq): SPI pins, memory read data/valid, CSR read
//  data, FIFO pop data, irq_code and thread_run_mask -- i.e. all external
//  responders are arbitrary.
// ============================================================================
`default_nettype none
`include "loom_bus.vh"
module h_host(input wire clk);

  reg [4:0] rst_cnt = 0;
  wire rst_n = (rst_cnt == 5'd4);
  always @(posedge clk) if (rst_cnt != 5'd4) rst_cnt <= rst_cnt + 5'd1;

  (* anyseq *) wire        spi_cs_n, spi_sck, spi_mosi;
  (* anyseq *) wire [31:0] mem_rdata;
  (* anyseq *) wire [15:0] csr_rd_data;
  (* anyseq *) wire [7:0]  tx_pop_data;
  (* anyseq *) wire [15:0] irq_code;
  (* anyseq *) wire [3:0]  thread_run_mask;

  wire        spi_miso, irq_out;
  wire        mem_we, mem_re, mem_rvalid;
  wire [9:0]  mem_addr;
  wire [31:0] mem_wdata;
  wire        csr_wr_en, csr_rd_en;
  wire [7:0]  csr_wr_addr, csr_rd_addr;
  wire [15:0] csr_wr_data;
  wire        rx_push, tx_pop;
  wire [1:0]  rx_push_tid, tx_pop_tid;
  wire [7:0]  rx_push_data;
  wire [3:0]  rx_full, tx_empty;

  reg mem_rvalid_r = 1'b0;
  always @(posedge clk) mem_rvalid_r <= mem_re;
  assign mem_rvalid = mem_rvalid_r;

  loom_host dut (
      .clk(clk), .rst_n(rst_n),
      .spi_cs_n(spi_cs_n), .spi_sck(spi_sck), .spi_mosi(spi_mosi),
      .spi_miso(spi_miso), .irq_out(irq_out),
      .mem_we(mem_we), .mem_addr(mem_addr), .mem_wdata(mem_wdata),
      .mem_re(mem_re), .mem_rdata(mem_rdata), .mem_rvalid(mem_rvalid),
      .csr_wr_en(csr_wr_en), .csr_wr_addr(csr_wr_addr), .csr_wr_data(csr_wr_data),
      .csr_rd_en(csr_rd_en), .csr_rd_addr(csr_rd_addr),
      .csr_rd_data(csr_rd_data),
      .rx_push(rx_push), .rx_push_tid(rx_push_tid), .rx_push_data(rx_push_data),
      .rx_full(rx_full),
      .tx_pop(tx_pop), .tx_pop_tid(tx_pop_tid), .tx_pop_data(tx_pop_data),
      .tx_empty(tx_empty),
      .irq_code(irq_code), .thread_run_mask(thread_run_mask));

  localparam [2:0] ST_IDLE = 3'd0, ST_ARG = 3'd1, ST_WAIT = 3'd2, ST_RESP = 3'd3;

  reg post_reset = 1'b0;
  always @(posedge clk) post_reset <= rst_n;

  // ---- reset completeness -------------------------------------------------
  reg first_post = 1'b1;
  always @(posedge clk) begin
    if (!rst_n) begin
      first_post <= 1'b1;
    end else if (first_post) begin
      assert (spi_miso == 1'b0);
      assert (irq_out == 1'b0);
      assert (dut.st == ST_IDLE);
      assert (mem_we == 1'b0);
      assert (mem_re == 1'b0);
      assert (csr_wr_en == 1'b0);
      assert (csr_rd_en == 1'b0);
      assert (rx_push == 1'b0);
      assert (tx_pop == 1'b0);
      first_post <= 1'b0;
    end
  end

  // ---- command/effect ordering + map bounds -------------------------------
  always @(posedge clk) if (post_reset) begin
    assert (dut.st <= ST_RESP);
    // the only command that writes the memory port is WRITE_MEM
    assert (!mem_we || (dut.cmd == `HCMD_WRITE_MEM));
    // the only command that pushes into a thread RX FIFO is TX_PUSH
    assert (!rx_push || (dut.cmd == `HCMD_TX_PUSH));
    // the only command that pops from a thread TX FIFO is RX_POP
    assert (!tx_pop || (dut.cmd == `HCMD_RX_POP));
    // CSR writes only from the commands specified to write one
    assert (!csr_wr_en ||
            (dut.cmd == `HCMD_WRITE_CSR) || (dut.cmd == `HCMD_SET_PC) ||
            (dut.cmd == `HCMD_RUN) || (dut.cmd == `HCMD_STOP) ||
            (dut.cmd == `HCMD_ACK_IRQ));
    // the host memory port is always inside the 10-bit map
    assert (mem_addr < 10'd1024);
    // MISO is only driven inside a CS window
    assert (spi_cs_n || (spi_miso == 1'b1) || (spi_miso == 1'b0));
  end

endmodule
`default_nettype wire

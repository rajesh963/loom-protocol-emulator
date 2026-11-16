// ============================================================================
// tt_um_rajesh963_loom.v — Tiny Tapeout top level for the LOOM protocol loom
//
// Pin map (see spec/ARCHITECTURE.md §2 and docs/info.md):
//   ui_in[0]  = H_CS_N     uo_out[0] = H_MISO
//   ui_in[1]  = H_SCK      uo_out[1] = H_IRQ
//   ui_in[2]  = H_MOSI     uo_out[7:2] = protocol outputs P[15:10]
//   ui_in[7:3]= protocol inputs P[15:11]
//   uio[7:0]  = protocol bidirectional P[7:0], output-enabled by uio_oe
// ============================================================================
`default_nettype none
`include "loom_bus.vh"

`ifdef LOOM_PMEM_SMALL
  localparam PMEM_WORDS_C = 128;      // FF fallback build (area-safe)
`else
  localparam PMEM_WORDS_C = 1024;     // full program space (simulation / SRAM build)
`endif

`ifdef LOOM_DMEM_SMALL
  localparam DMEM_WORDS_C = 32;       // area-reduced build (wave buffer <= 32 patterns)
`else
  localparam DMEM_WORDS_C = 64;
`endif

module tt_um_rajesh963_loom (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

  // --------------------------------------------------------------------------
  // Pin vectors
  // --------------------------------------------------------------------------
  wire [15:0] pins_in  = {ui_in, uio_in};   // [15:8]=ui_in, [7:0]=uio_in
  wire [15:0] pins_out;                     // [15:10]=uo[7:2], [7:0]=uio_out
  wire [7:0]  pins_dir;

  // --------------------------------------------------------------------------
  // Host link pins
  // --------------------------------------------------------------------------
  wire spi_cs_n = ui_in[0];
  wire spi_sck  = ui_in[1];
  wire spi_mosi = ui_in[2];
  wire spi_miso;
  wire irq_out;

  // --------------------------------------------------------------------------
  // Core <-> memory
  // --------------------------------------------------------------------------
  wire [9:0]  imem_addr;
  wire [31:0] imem_data;
  wire [5:0]  dmem_addr;
  wire        dmem_we;
  wire [15:0] dmem_wdata;
  wire [15:0] dmem_rdata;

  // --------------------------------------------------------------------------
  // Wave player
  // --------------------------------------------------------------------------
  wire        wave_start, wave_loop, wave_oneshot;
  wire [5:0]  wave_addr;
  wire [6:0]  wave_len;
  wire [15:0] wave_period;
  wire        wave_busy, wave_done, wave_active;
  wire [5:0]  wave_dmem_addr;
  wire [15:0] wave_dmem_data;
  wire [15:0] wave_pins;

  // --------------------------------------------------------------------------
  // Host memory / CSR / FIFO ports
  // --------------------------------------------------------------------------
  wire        host_we, host_re;
  wire [9:0]  host_addr;
  wire [31:0] host_wdata, host_rdata;
  wire        host_rvalid;

  wire        host_csrw_en, host_csrr_en;
  wire [7:0]  host_csrw_addr, host_csrr_addr;
  wire [15:0] host_csrw_data, host_csrr_data;

  wire        host_rx_push, host_tx_pop;
  wire [1:0]  host_rx_tid, host_tx_tid;
  wire [7:0]  host_rx_data, host_tx_data;
  wire [3:0]  rx_full, tx_empty;
  wire [15:0] irq_code;
  wire [3:0]  thread_run_mask;

  // --------------------------------------------------------------------------
  // Capture engine (v1: build-time option, default off)
  // --------------------------------------------------------------------------
  wire [15:0] cap_ctrl;
  wire [15:0] cap_status;
  wire [5:0]  cap_count;
  wire        cap_full, cap_active;
  wire        cap_we;
  wire [5:0]  cap_addr;
  wire [15:0] cap_wdata;

`ifdef LOOM_ENABLE_CAPTURE
  assign cap_status = {4'd0, cap_full, cap_active, cap_count[5:0], 2'b00};

  loom_capture u_capture (
      .clk(clk), .rst_n(rst_n),
      .cfg_en(cap_ctrl[0]), .cfg_clear(cap_ctrl[1]),
      .cfg_mask(16'hFFFF), .cfg_value(16'hFFFF), .cfg_div(cap_ctrl[7:4]),
      .pins_in(pins_in), .tmr(16'd0),
      .cap_we(cap_we), .cap_addr(cap_addr), .cap_wdata(cap_wdata),
      .cap_count(cap_count), .cap_full(cap_full), .cap_active(cap_active)
  );
`else
  assign cap_status = 16'd0;
  assign cap_we     = 1'b0;
  assign cap_addr   = 6'd0;
  assign cap_wdata  = 16'd0;
`endif

  // --------------------------------------------------------------------------
  // Core
  // --------------------------------------------------------------------------
  loom_core #(
      .NUM_THREADS(4), .EQ_DEPTH(4), .FIFO_DEPTH(8), .DMEM_WORDS(DMEM_WORDS_C)
  ) u_core (
      .clk(clk), .rst_n(rst_n),
      .imem_addr(imem_addr), .imem_data(imem_data),
      .dmem_addr(dmem_addr), .dmem_we(dmem_we), .dmem_wdata(dmem_wdata),
      .dmem_rdata(dmem_rdata), .dmem_denied(1'b0),
      .pins_out(pins_out), .pins_dir(pins_dir), .pins_in(pins_in),
      .wave_start(wave_start), .wave_loop(wave_loop), .wave_oneshot(wave_oneshot),
      .wave_addr(wave_addr), .wave_len(wave_len), .wave_period(wave_period),
      .wave_busy(wave_busy), .wave_done(wave_done),
      .wave_active(wave_active), .wave_pins(wave_pins),
      .cap_ctrl(cap_ctrl), .cap_stat(cap_status),
      .host_csrw_en(host_csrw_en), .host_csrw_addr(host_csrw_addr),
      .host_csrw_data(host_csrw_data),
      .host_csrr_en(host_csrr_en), .host_csrr_addr(host_csrr_addr),
      .host_csrr_data(host_csrr_data),
      .host_rx_push(host_rx_push), .host_rx_tid(host_rx_tid),
      .host_rx_data(host_rx_data), .rx_full(rx_full),
      .host_tx_pop(host_tx_pop), .host_tx_tid(host_tx_tid),
      .host_tx_data(host_tx_data), .tx_empty(tx_empty),
      .irq_code(irq_code), .thread_run_mask(thread_run_mask)
  );

  // --------------------------------------------------------------------------
  // Host bridge
  // --------------------------------------------------------------------------
  loom_host u_host (
      .clk(clk), .rst_n(rst_n),
      .spi_cs_n(spi_cs_n), .spi_sck(spi_sck), .spi_mosi(spi_mosi),
      .spi_miso(spi_miso), .irq_out(irq_out),
      .mem_we(host_we), .mem_addr(host_addr), .mem_wdata(host_wdata),
      .mem_re(host_re), .mem_rdata(host_rdata), .mem_rvalid(host_rvalid),
      .csr_wr_en(host_csrw_en), .csr_wr_addr(host_csrw_addr),
      .csr_wr_data(host_csrw_data),
      .csr_rd_en(host_csrr_en), .csr_rd_addr(host_csrr_addr),
      .csr_rd_data(host_csrr_data),
      .rx_push(host_rx_push), .rx_push_tid(host_rx_tid), .rx_push_data(host_rx_data),
      .rx_full(rx_full),
      .tx_pop(host_tx_pop), .tx_pop_tid(host_tx_tid), .tx_pop_data(host_tx_data),
      .tx_empty(tx_empty),
      .irq_code(irq_code), .thread_run_mask(thread_run_mask)
  );

  // --------------------------------------------------------------------------
  // Wave player
  // --------------------------------------------------------------------------
  loom_wave u_wave (
      .clk(clk), .rst_n(rst_n),
      .cfg_start(wave_start), .cfg_loop(wave_loop), .cfg_oneshot(wave_oneshot),
      .cfg_addr(wave_addr), .cfg_len(wave_len), .cfg_period(wave_period),
      .busy(wave_busy), .done(wave_done),
      .dmem_addr(wave_dmem_addr), .dmem_data(wave_dmem_data),
      .wave_pins(wave_pins), .wave_valid(wave_active)
  );

  // --------------------------------------------------------------------------
  // Memory
  // --------------------------------------------------------------------------
  loom_mem #(
      .PMEM_WORDS(PMEM_WORDS_C), .DMEM_WORDS(DMEM_WORDS_C), .PMEM_IMPL("ff")
  ) u_mem (
      .clk(clk), .rst_n(rst_n),
      .imem_addr(imem_addr), .imem_data(imem_data),
      .dmem_addr(dmem_addr), .dmem_we(dmem_we), .dmem_wdata(dmem_wdata),
      .dmem_rdata(dmem_rdata),
      .wave_addr(wave_dmem_addr), .wave_data(wave_dmem_data),
      .cap_we(cap_we), .cap_addr(cap_addr), .cap_wdata(cap_wdata),
      .host_we(host_we), .host_re(host_re), .host_addr(host_addr),
      .host_wdata(host_wdata), .host_rdata(host_rdata),
      .host_rvalid(host_rvalid)
  );

  // --------------------------------------------------------------------------
  // Tiny Tapeout pin assignment
  // --------------------------------------------------------------------------
  assign uo_out  = {pins_out[15:10], irq_out, spi_miso};
  assign uio_out = pins_out[7:0];
  assign uio_oe  = pins_dir;

  // `ena` is high whenever the design is powered/selected; nothing to gate.
  wire _unused = &{ena, 1'b0};

endmodule
`default_nettype wire

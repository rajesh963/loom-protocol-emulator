// ============================================================================
//  tb_host.v -- self-checking testbench for rtl/loom_host.v
//
//  Bit-bangs real SPI mode-0 frames at 10 MHz SCK with a 60 MHz clk, against
//  stub memory / CSR / per-thread FIFO models, and checks the canonical host
//  framing:
//    CMD transfer MISO = 0x80, arg transfers = 0x00, payload on the transfers
//    after the command byte (LSB byte first), PING -> 0xA5, STATUS = 4 thread
//    bytes {halted,en,tx_full,tx_empty,rx_full,rx_empty,2'b00}.
//
//  Covered: PING, SET_ADDR + WRITE_MEM/READ_MEM round trip + auto-increment,
//  WRITE_CSR/READ_CSR, TX_PUSH -> thread RX FIFO, thread TX FIFO -> RX_POP,
//  SET_PC -> TPC CSR, RUN/STOP -> host-only 0x30/0x31 CSRs, STATUS against
//  modeled TSTATUS/TFIFO CSRs, IRQ set/sticky/ACK_IRQ clear with no per-byte
//  IRQ glitch, CS abort mid-byte and at a byte boundary, back-to-back commands
//  in one CS window, stalled-read watchdog, unknown command.
//
//  Prints "HOST_TB: PASS"; $fatal (non-zero exit) on any failure.
// ============================================================================
`timescale 1ns/1ps
`default_nettype none

module tb_host;

  // --------------------------------------------------------------------------
  // clock / reset / error bookkeeping
  // --------------------------------------------------------------------------
  reg         clk;
  reg         rst_n;
  integer     errors;
  integer     spi_xfer_id;        // byte-transfer counter for the IRQ monitor
  integer     irq_trans_in_xfer;  // IRQ level transitions inside one transfer
  reg         irq_prev;
  integer     trace_on;           // +trace: dump every transfer (wire trace)
  integer     txn_no;             // CS window number
  integer     xfer_in_txn;        // transfer number inside the window
  integer     bit_ns;             // SCK bit period in ns (100 = 10 MHz)

  always #8.333 clk = ~clk;       // ~60 MHz

  // --------------------------------------------------------------------------
  // SPI master side
  // --------------------------------------------------------------------------
  reg  spi_cs_n_r;
  reg  spi_sck_r;
  reg  spi_mosi_r;
  wire spi_miso;
  wire irq_out;

  // --------------------------------------------------------------------------
  // DUT
  // --------------------------------------------------------------------------
  wire        mem_we;
  wire [9:0]  mem_addr;
  wire [31:0] mem_wdata;
  wire        mem_re;
  wire [31:0] mem_rdata;
  wire        mem_rvalid;
  wire        csr_wr_en;
  wire [7:0]  csr_wr_addr;
  wire [15:0] csr_wr_data;
  wire        csr_rd_en;
  wire [7:0]  csr_rd_addr;
  wire [15:0] csr_rd_data;
  wire        rx_push;
  wire [1:0]  rx_push_tid;
  wire [7:0]  rx_push_data;
  wire [3:0]  rx_full;
  wire        tx_pop;
  wire [1:0]  tx_pop_tid;
  wire [7:0]  tx_pop_data;
  wire [3:0]  tx_empty;
  wire [15:0] irq_code;
  wire [3:0]  thread_run_mask;

  reg  [3:0]  run_mask_r;
  reg  [15:0] irq_req;            // test-controlled "irq instruction" request
  reg  [15:0] irq_code_r;         // core-side IRQ register model

  loom_host dut (
    .clk          (clk),
    .rst_n        (rst_n),
    .spi_cs_n     (spi_cs_n_r),
    .spi_sck      (spi_sck_r),
    .spi_mosi     (spi_mosi_r),
    .spi_miso     (spi_miso),
    .irq_out      (irq_out),
    .mem_we       (mem_we),
    .mem_addr     (mem_addr),
    .mem_wdata    (mem_wdata),
    .mem_re       (mem_re),
    .mem_rdata    (mem_rdata),
    .mem_rvalid   (mem_rvalid),
    .csr_wr_en    (csr_wr_en),
    .csr_wr_addr  (csr_wr_addr),
    .csr_wr_data  (csr_wr_data),
    .csr_rd_en    (csr_rd_en),
    .csr_rd_addr  (csr_rd_addr),
    .csr_rd_data  (csr_rd_data),
    .rx_push      (rx_push),
    .rx_push_tid  (rx_push_tid),
    .rx_push_data (rx_push_data),
    .rx_full      (rx_full),
    .tx_pop       (tx_pop),
    .tx_pop_tid   (tx_pop_tid),
    .tx_pop_data  (tx_pop_data),
    .tx_empty     (tx_empty),
    .irq_code     (irq_code),
    .thread_run_mask(run_mask_r)
  );

  assign irq_code        = irq_code_r;
  assign thread_run_mask = run_mask_r;

  // --------------------------------------------------------------------------
  // stub memory: 1-cycle registered read, mem_rvalid one-clk pulse
  // --------------------------------------------------------------------------
  reg [31:0] mem [0:1023];
  reg [31:0] mem_rdata_r;
  reg        mem_rvalid_r;
  reg        mem_stall;           // force the watchdog path

  always @(posedge clk) begin
    mem_rvalid_r <= 1'b0;
    if (mem_re && !mem_stall) begin
      mem_rdata_r  <= mem[mem_addr];
      mem_rvalid_r <= 1'b1;
    end
    if (mem_we)
      mem[mem_addr] <= mem_wdata;
  end
  assign mem_rdata  = mem_rdata_r;
  assign mem_rvalid = mem_rvalid_r;

  // --------------------------------------------------------------------------
  // stub per-thread FIFOs (loom_fifo instances; thread side driven by the TB)
  // --------------------------------------------------------------------------
  wire [7:0] rx_rd  [0:3];
  wire [7:0] tx_rd  [0:3];
  wire [3:0] tx_full_v;
  reg  [3:0] txpush_en;
  reg  [7:0] txpush_data;
  reg  [7:0] tx_pop_data_r;

  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_rx0 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(rx_push && (rx_push_tid == 2'd0)), .wr_data(rx_push_data),
    .full(rx_full[0]), .rd_en(1'b0), .rd_data(rx_rd[0]), .empty(), .count());
  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_rx1 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(rx_push && (rx_push_tid == 2'd1)), .wr_data(rx_push_data),
    .full(rx_full[1]), .rd_en(1'b0), .rd_data(rx_rd[1]), .empty(), .count());
  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_rx2 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(rx_push && (rx_push_tid == 2'd2)), .wr_data(rx_push_data),
    .full(rx_full[2]), .rd_en(1'b0), .rd_data(rx_rd[2]), .empty(), .count());
  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_rx3 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(rx_push && (rx_push_tid == 2'd3)), .wr_data(rx_push_data),
    .full(rx_full[3]), .rd_en(1'b0), .rd_data(rx_rd[3]), .empty(), .count());

  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_tx0 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(txpush_en[0]), .wr_data(txpush_data), .full(tx_full_v[0]),
    .rd_en(tx_pop && (tx_pop_tid == 2'd0)), .rd_data(tx_rd[0]),
    .empty(tx_empty[0]), .count());
  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_tx1 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(txpush_en[1]), .wr_data(txpush_data), .full(tx_full_v[1]),
    .rd_en(tx_pop && (tx_pop_tid == 2'd1)), .rd_data(tx_rd[1]),
    .empty(tx_empty[1]), .count());
  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_tx2 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(txpush_en[2]), .wr_data(txpush_data), .full(tx_full_v[2]),
    .rd_en(tx_pop && (tx_pop_tid == 2'd2)), .rd_data(tx_rd[2]),
    .empty(tx_empty[2]), .count());
  loom_fifo #(.DEPTH(8), .AW(3), .WIDTH(8)) u_tx3 (
    .clk(clk), .rst_n(rst_n),
    .wr_en(txpush_en[3]), .wr_data(txpush_data), .full(tx_full_v[3]),
    .rd_en(tx_pop && (tx_pop_tid == 2'd3)), .rd_data(tx_rd[3]),
    .empty(tx_empty[3]), .count());

  always @(tx_pop_tid or tx_rd[0] or tx_rd[1] or tx_rd[2] or tx_rd[3]) begin
    case (tx_pop_tid)
      2'd0:    tx_pop_data_r = tx_rd[0];
      2'd1:    tx_pop_data_r = tx_rd[1];
      2'd2:    tx_pop_data_r = tx_rd[2];
      default: tx_pop_data_r = tx_rd[3];
    endcase
  end
  assign tx_pop_data = tx_pop_data_r;

  // --------------------------------------------------------------------------
  // FIFO occupancy / peek helpers
  // --------------------------------------------------------------------------
  function [3:0] rx_cnt(input integer t);
    begin
      case (t)
        0: rx_cnt = u_rx0.count;
        1: rx_cnt = u_rx1.count;
        2: rx_cnt = u_rx2.count;
        default: rx_cnt = u_rx3.count;
      endcase
    end
  endfunction

  function [3:0] tx_cnt(input integer t);
    begin
      case (t)
        0: tx_cnt = u_tx0.count;
        1: tx_cnt = u_tx1.count;
        2: tx_cnt = u_tx2.count;
        default: tx_cnt = u_tx3.count;
      endcase
    end
  endfunction

  function [7:0] rx_peek(input integer t);
    begin
      case (t)
        0: rx_peek = u_rx0.mem[u_rx0.rd_ptr];
        1: rx_peek = u_rx1.mem[u_rx1.rd_ptr];
        2: rx_peek = u_rx2.mem[u_rx2.rd_ptr];
        default: rx_peek = u_rx3.mem[u_rx3.rd_ptr];
      endcase
    end
  endfunction

  // --------------------------------------------------------------------------
  // stub CSR block (core model)
  //   * ordinary CSRs             : read/write a 256x16 register file
  //   * 0x40+16t+0x02 TSTATUS     : {14'b0, halted, en}
  //   * 0x40+16t+0x03 TFIFO       : {4'b0, rx_count, tx_count, 2'b0, rx_full, tx_full}
  //   * write 0x30 RUNMASK / 0x31 STOPMASK  : updates the thread halt/en model
  //   * write 0x14 CSR_IRQ        : clears the IRQ code (as the core will)
  // --------------------------------------------------------------------------
  reg [15:0] csr_mem [0:255];
  reg [15:0] th_st0, th_st1, th_st2, th_st3;   // {halted, en}
  integer    csr_wr_events;                    // total number of CSR writes
  reg [7:0]  csr_wr_addr_last;
  reg [15:0] csr_wr_data_last;

  function [15:0] csr_rd_val(input [7:0] a);
    begin
      csr_rd_val = csr_mem[a];
      case (a)
        8'h42: csr_rd_val = {14'b0, th_st0};
        8'h52: csr_rd_val = {14'b0, th_st1};
        8'h62: csr_rd_val = {14'b0, th_st2};
        8'h72: csr_rd_val = {14'b0, th_st3};
        8'h43: csr_rd_val = {4'b0, rx_cnt(0), tx_cnt(0), 2'b0, rx_full[0], tx_full_v[0]};
        8'h53: csr_rd_val = {4'b0, rx_cnt(1), tx_cnt(1), 2'b0, rx_full[1], tx_full_v[1]};
        8'h63: csr_rd_val = {4'b0, rx_cnt(2), tx_cnt(2), 2'b0, rx_full[2], tx_full_v[2]};
        8'h73: csr_rd_val = {4'b0, rx_cnt(3), tx_cnt(3), 2'b0, rx_full[3], tx_full_v[3]};
        default: ;
      endcase
    end
  endfunction

  always @(posedge clk) begin
    if (!rst_n) begin
      csr_wr_events    <= 0;
      csr_wr_addr_last <= 8'h00;
      csr_wr_data_last <= 16'h0000;
      th_st0           <= 2'b10;          // reset: halted, not enabled
      th_st1           <= 2'b10;
      th_st2           <= 2'b10;
      th_st3           <= 2'b10;
    end else begin
      if (csr_wr_en) begin
        csr_mem[csr_wr_addr] <= csr_wr_data;
        csr_wr_events        <= csr_wr_events + 1;
        csr_wr_addr_last     <= csr_wr_addr;
        csr_wr_data_last     <= csr_wr_data;
        if (csr_wr_addr == 8'h30) begin      // RUNMASK: clear halt, en |= mask
          if (csr_wr_data[0]) th_st0 <= 2'b01;
          if (csr_wr_data[1]) th_st1 <= 2'b01;
          if (csr_wr_data[2]) th_st2 <= 2'b01;
          if (csr_wr_data[3]) th_st3 <= 2'b01;
        end
        if (csr_wr_addr == 8'h31) begin      // STOPMASK: en &= ~mask
          if (csr_wr_data[0]) th_st0[0] <= 1'b0;
          if (csr_wr_data[1]) th_st1[0] <= 1'b0;
          if (csr_wr_data[2]) th_st2[0] <= 1'b0;
          if (csr_wr_data[3]) th_st3[0] <= 1'b0;
        end
        if (csr_wr_addr == 8'h14)            // ACK_IRQ -> core drops the code
          irq_code_r <= 16'h0000;
      end
    end
  end
  assign csr_rd_data = csr_rd_val(csr_rd_addr);   // combinational read port

  always @(posedge clk) begin
    if (!rst_n) irq_code_r <= 16'h0000;
    else if (irq_req != 16'h0000) irq_code_r <= irq_req;
  end

  // --------------------------------------------------------------------------
  // IRQ glitch monitor: at most one irq_out transition inside a byte transfer
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (rst_n && (spi_xfer_id > 0) && (irq_out !== irq_prev)) begin
      irq_prev = irq_out;
      irq_trans_in_xfer = irq_trans_in_xfer + 1;
      if (irq_trans_in_xfer > 1) begin
        errors = errors + 1;
        $display("FAIL: irq_out glitched (%0d transitions) inside byte transfer #%0d",
                 irq_trans_in_xfer, spi_xfer_id);
      end
    end
  end

  // --------------------------------------------------------------------------
  // checking helpers
  // --------------------------------------------------------------------------
  task chk8(input [255:0] label, input [7:0] got, input [7:0] exp);
    begin
      if (got !== exp) begin
        errors = errors + 1;
        $display("FAIL: %0s : got 0x%02h expected 0x%02h", label, got, exp);
      end
    end
  endtask

  task chk16(input [255:0] label, input [15:0] got, input [15:0] exp);
    begin
      if (got !== exp) begin
        errors = errors + 1;
        $display("FAIL: %0s : got 0x%04h expected 0x%04h", label, got, exp);
      end
    end
  endtask

  task chk32(input [255:0] label, input [31:0] got, input [31:0] exp);
    begin
      if (got !== exp) begin
        errors = errors + 1;
        $display("FAIL: %0s : got 0x%08h expected 0x%08h", label, got, exp);
      end
    end
  endtask

  // --------------------------------------------------------------------------
  // SPI master: 10 MHz SCK (100 ns period, 50 % duty), mode 0, MSB first
  //   MOSI changes 45 ns before the rising edge
  //   MISO is sampled 5 ns after the rising edge
  // --------------------------------------------------------------------------
  task spi_byte(input [7:0] mosi, output [7:0] miso);
    integer k;
    begin
      spi_xfer_id       = spi_xfer_id + 1;
      irq_trans_in_xfer = 0;
      for (k = 7; k >= 0; k = k - 1) begin
        spi_mosi_r = mosi[k];
        #(bit_ns/2 - 5);             // MOSI setup before the rising edge
        spi_sck_r  = 1'b1;          // rising edge: DUT samples MOSI
        #5;
        miso[k] = spi_miso;         // master samples MISO 5 ns after the edge
        #(bit_ns/2 - 5);            // rest of the SCK high phase
        spi_sck_r  = 1'b0;          // falling edge
        #5;
      end
      spi_mosi_r = 1'b0;
      if (trace_on)
        $display("TRACE txn %0d xfer %0d  MOSI %02h  MISO %02h",
                 txn_no, xfer_in_txn, mosi, miso);
      xfer_in_txn = xfer_in_txn + 1;
    end
  endtask

  // send `nbits` MSB-first bits then stop (used for the abort test)
  task spi_partial(input [7:0] mosi, input integer nbits);
    integer k;
    begin
      spi_xfer_id       = spi_xfer_id + 1;
      irq_trans_in_xfer = 0;
      for (k = 7; k > (7 - nbits); k = k - 1) begin
        spi_mosi_r = mosi[k];
        #(bit_ns/2 - 5);
        spi_sck_r  = 1'b1;
        #5;
        #(bit_ns/2 - 5);
        spi_sck_r  = 1'b0;
        #5;
      end
      spi_mosi_r = 1'b0;
    end
  endtask

  task txn_begin;
    begin
      spi_sck_r  = 1'b0;
      spi_mosi_r = 1'b0;
      #100;
      txn_no      = txn_no + 1;
      xfer_in_txn = 0;
      if (trace_on) $display("TRACE txn %0d: CS low", txn_no);
      spi_cs_n_r = 1'b0;
      #120;                          // CS setup before the first SCK edge
    end
  endtask

  task txn_end;
    begin
      #50;
      spi_cs_n_r = 1'b1;
      if (trace_on) $display("TRACE txn %0d: CS high (MISO=%b)", txn_no, spi_miso);
      #250;
    end
  endtask

  // one checked transfer
  task xfer(input [255:0] label, input [7:0] mosi, input [7:0] exp);
    reg [7:0] got;
    begin
      spi_byte(mosi, got);
      chk8(label, got, exp);
    end
  endtask

  // push a byte into thread `t`'s TX FIFO from the "thread" side
  task thread_tx_push(input [1:0] t, input [7:0] d);
    begin
      @(negedge clk);
      txpush_en   = 4'b0001 << t;
      txpush_data = d;
      @(negedge clk);
      txpush_en   = 4'b0000;
      txpush_data = 8'h00;
      #1;
    end
  endtask

  // raise the core IRQ register (models the `irq` instruction)
  task raise_irq(input [15:0] code);
    begin
      @(negedge clk);
      irq_req = code;
      repeat (3) @(negedge clk);
      irq_req = 16'h0000;
      #1;
    end
  endtask

  // --------------------------------------------------------------------------
  // stimulus
  // --------------------------------------------------------------------------
  integer i;

  initial begin
    errors            = 0;
    clk               = 1'b0;
    rst_n             = 1'b0;
    spi_cs_n_r        = 1'b1;
    spi_sck_r         = 1'b0;
    spi_mosi_r        = 1'b0;
    run_mask_r        = 4'b0000;
    irq_req           = 16'h0000;
    irq_prev          = 1'b0;
    spi_xfer_id       = 0;
    irq_trans_in_xfer = 0;
    mem_stall         = 1'b0;
    txpush_en         = 4'b0000;
    txpush_data       = 8'h00;
    trace_on          = 0;
    txn_no            = 0;
    xfer_in_txn       = 0;
    bit_ns            = 100;
    if ($test$plusargs("trace"))  trace_on = 1;
    if ($value$plusargs("bitns=%d", bit_ns)) trace_on = trace_on;
    $display("HOST_TB: SCK period = %0d ns (%0d kHz)", bit_ns, 1000000/bit_ns);
    for (i = 0; i < 1024; i = i + 1) mem[i] = 32'h0000_0000;
    for (i = 0; i < 256;  i = i + 1) csr_mem[i] = 16'h0000;

    repeat (4) @(posedge clk);
    #1;
    chk8("MISO idle (reset, CS high)", spi_miso, 8'h00);
    chk8("IRQ idle (reset)", {7'd0, irq_out}, 8'h00);
    rst_n = 1'b1;
    repeat (3) @(posedge clk);
    #1;
    irq_prev = irq_out;
    chk8("MISO after reset release", spi_miso, 8'h00);
    chk8("IRQ after reset release", {7'd0, irq_out}, 8'h00);

    // ===================== 1. PING =====================
    txn_begin;
    xfer("PING cmd",         8'h0D, 8'h80);
    xfer("PING arg -> 0xA5", 8'h5A, 8'hA5);
    txn_end;
    chk8("MISO idle after PING txn", spi_miso, 8'h00);

    // ===================== 2. SET_ADDR + WRITE_MEM =====================
    txn_begin;
    xfer("SET_ADDR cmd",  8'h01, 8'h80);
    xfer("SET_ADDR a0",   8'h10, 8'h00);
    xfer("SET_ADDR a1",   8'h00, 8'h00);
    xfer("SET_ADDR a2",   8'h00, 8'h00);
    txn_end;

    txn_begin;
    xfer("WRITE_MEM cmd", 8'h02, 8'h80);
    xfer("WRITE_MEM b0",  8'hEF, 8'h00);
    xfer("WRITE_MEM b1",  8'hBE, 8'h00);
    xfer("WRITE_MEM b2",  8'hAD, 8'h00);
    xfer("WRITE_MEM b3",  8'hDE, 8'h00);
    txn_end;
    chk32("mem[0x010] = 0xDEADBEEF", mem[10'h010], 32'hDEAD_BEEF);

    // auto-increment: second write lands at 0x011
    txn_begin;
    xfer("WRITE_MEM2 cmd", 8'h02, 8'h80);
    xfer("WRITE_MEM2 b0",  8'h44, 8'h00);
    xfer("WRITE_MEM2 b1",  8'h33, 8'h00);
    xfer("WRITE_MEM2 b2",  8'h22, 8'h00);
    xfer("WRITE_MEM2 b3",  8'h11, 8'h00);
    txn_end;
    chk32("mem[0x011] = 0x11223344 (auto-incr)", mem[10'h011], 32'h1122_3344);
    chk32("mem[0x010] unchanged", mem[10'h010], 32'hDEAD_BEEF);

    // ===================== 3. READ_MEM round trip =====================
    txn_begin;
    xfer("SET_ADDR(0x10) cmd", 8'h01, 8'h80);
    xfer("SET_ADDR a0",        8'h10, 8'h00);
    xfer("SET_ADDR a1",        8'h00, 8'h00);
    xfer("SET_ADDR a2",        8'h00, 8'h00);
    xfer("READ_MEM cmd",       8'h03, 8'h80);
    xfer("READ_MEM b0",        8'h00, 8'hEF);
    xfer("READ_MEM b1",        8'h00, 8'hBE);
    xfer("READ_MEM b2",        8'h00, 8'hAD);
    xfer("READ_MEM b3",        8'h00, 8'hDE);
    // auto-increment read: next READ_MEM starts at 0x011
    xfer("READ_MEM+1 cmd",     8'h03, 8'h80);
    xfer("READ_MEM+1 b0",      8'h00, 8'h44);
    xfer("READ_MEM+1 b1",      8'h00, 8'h33);
    xfer("READ_MEM+1 b2",      8'h00, 8'h22);
    xfer("READ_MEM+1 b3",      8'h00, 8'h11);
    txn_end;

    // ===================== 4. WRITE_CSR / READ_CSR =====================
    txn_begin;
    xfer("WRITE_CSR cmd", 8'h04, 8'h80);
    xfer("WRITE_CSR csr", 8'h10, 8'h00);
    xfer("WRITE_CSR d0",  8'hFE, 8'h00);
    xfer("WRITE_CSR d1",  8'hCA, 8'h00);
    txn_end;
    chk16("csr[0x10] = 0xCAFE", csr_mem[8'h10], 16'hCAFE);

    txn_begin;
    xfer("READ_CSR cmd",  8'h05, 8'h80);
    xfer("READ_CSR csr",  8'h10, 8'h00);
    xfer("READ_CSR d0",   8'h00, 8'hFE);
    xfer("READ_CSR d1",   8'h00, 8'hCA);
    txn_end;

    // ===================== 5. TX_PUSH (host -> thread RX FIFO) =========
    chk8("thread1 rx fifo empty", {4'd0, rx_cnt(1)}, 8'd0);
    txn_begin;
    xfer("TX_PUSH cmd",   8'h06, 8'h80);
    xfer("TX_PUSH t",     8'h01, 8'h00);
    xfer("TX_PUSH data",  8'hA7, 8'h00);
    txn_end;
    chk8("thread1 rx fifo count=1", {4'd0, rx_cnt(1)}, 8'd1);
    chk8("thread1 rx fifo head=0xA7", rx_peek(1), 8'hA7);
    chk8("thread0 rx fifo still empty", {4'd0, rx_cnt(0)}, 8'd0);

    // ===================== 6. RX_POP (thread TX FIFO -> host) =========
    thread_tx_push(2'd2, 8'h3C);
    thread_tx_push(2'd2, 8'hC3);
    chk8("thread2 tx fifo count=2", {4'd0, tx_cnt(2)}, 8'd2);
    txn_begin;
    xfer("RX_POP cmd",    8'h07, 8'h80);
    xfer("RX_POP t",      8'h02, 8'h00);
    xfer("RX_POP data 1", 8'h00, 8'h3C);
    txn_end;
    txn_begin;
    xfer("RX_POP cmd 2",  8'h07, 8'h80);
    xfer("RX_POP t 2",    8'h02, 8'h00);
    xfer("RX_POP data 2", 8'h00, 8'hC3);
    txn_end;
    chk8("thread2 tx fifo drained", {4'd0, tx_cnt(2)}, 8'd0);
    // empty FIFO: returns 0x00, no deadlock
    txn_begin;
    xfer("RX_POP empty cmd", 8'h07, 8'h80);
    xfer("RX_POP empty t",   8'h03, 8'h00);
    xfer("RX_POP empty data",8'h00, 8'h00);
    txn_end;

    // ===================== 7. SET_PC -> thread TPC CSR =================
    txn_begin;
    xfer("SET_PC cmd",   8'h08, 8'h80);
    xfer("SET_PC t",     8'h02, 8'h00);
    xfer("SET_PC p0",    8'h23, 8'h00);
    xfer("SET_PC p1",    8'h01, 8'h00);
    txn_end;
    chk16("csr[0x40+16*2+TPC] = 0x0123", csr_mem[8'h68], 16'h0123);
    chk8 ("SET_PC csr addr", csr_wr_addr_last, 8'h68);

    // ===================== 8. RUN / STOP host-only CSRs ===============
    txn_begin;
    xfer("RUN cmd",  8'h09, 8'h80);
    xfer("RUN mask", 8'h05, 8'h00);
    txn_end;
    chk16("RUNMASK csr[0x30] = 0x0005", csr_mem[8'h30], 16'h0005);
    chk8 ("RUN csr addr", csr_wr_addr_last, 8'h30);

    txn_begin;
    xfer("STOP cmd",  8'h0A, 8'h80);
    xfer("STOP mask", 8'h02, 8'h00);
    txn_end;
    chk16("STOPMASK csr[0x31] = 0x0002", csr_mem[8'h31], 16'h0002);
    chk8 ("STOP csr addr", csr_wr_addr_last, 8'h31);

    // ===================== 9. STATUS =================================
    // thread model: RUN 0x05 -> t0,t2 {halted=0,en=1}; t1,t3 {1,0}
    // fifo model at this point: rx1 has 1 byte, tx2 will be filled to 8
    chk8("thread0 halted=0 en=1", th_st0, 2'b01);
    chk8("thread1 halted=1 en=0", th_st1, 2'b10);
    for (i = 0; i < 8; i = i + 1) thread_tx_push(2'd2, 8'h80 + i[7:0]);
    chk8("thread2 tx fifo full", {3'd0, tx_full_v[2]}, 8'd1);
    run_mask_r = 4'b0101;
    repeat (3) @(posedge clk);
    txn_begin;
    xfer("STATUS cmd", 8'h0B, 8'h80);
    // t0: halt=0 en=1 tx_full=0 tx_empty=1 rx_full=0 rx_empty=1 -> 0101_0100
    xfer("STATUS s0",  8'h00, 8'h54);
    // t1: halt=1 en=0 tx_full=0 tx_empty=1 rx_full=0 rx_empty=0 -> 1001_0000
    xfer("STATUS s1",  8'h00, 8'h90);
    // t2: halt=0 en=1 tx_full=1 tx_empty=0 rx_full=0 rx_empty=1 -> 0110_0100
    xfer("STATUS s2",  8'h00, 8'h64);
    // t3: halt=1 en=0 tx_full=0 tx_empty=1 rx_full=0 rx_empty=1 -> 1001_0100
    xfer("STATUS s3",  8'h00, 8'h94);
    txn_end;

    // ===================== 10. IRQ set / sticky / ACK ==================
    raise_irq(16'h0042);
    repeat (4) @(posedge clk);
    chk8("IRQ raised", {7'd0, irq_out}, 8'd1);
    txn_begin;
    xfer("PING while irq cmd", 8'h0D, 8'h80);
    xfer("PING while irq arg", 8'h5A, 8'hA5);
    txn_end;
    chk8("IRQ still set after PING", {7'd0, irq_out}, 8'd1);
    // ACK_IRQ: clears the bridge latch and writes CSR_IRQ=0 so the core drops it
    txn_begin;
    xfer("ACK_IRQ cmd", 8'h0C, 8'h80);
    txn_end;
    chk8 ("IRQ cleared by ACK_IRQ", {7'd0, irq_out}, 8'd0);
    chk16("core IRQ code cleared", irq_code_r, 16'h0000);
    chk8 ("ACK_IRQ wrote csr 0x14", csr_wr_addr_last, 8'h14);
    chk16("ACK_IRQ wrote data 0", csr_wr_data_last, 16'h0000);
    repeat (8) @(posedge clk);
    chk8("IRQ stays low after ACK", {7'd0, irq_out}, 8'd0);

    // ===================== 11. CS abort in the middle of a byte ======
    txn_begin;
    xfer("SET_ADDR(0x20) cmd", 8'h01, 8'h80);
    xfer("SET_ADDR a0", 8'h20, 8'h00);
    xfer("SET_ADDR a1", 8'h00, 8'h00);
    xfer("SET_ADDR a2", 8'h00, 8'h00);
    txn_end;
    txn_begin;
    xfer("WRITE_MEM(0x20) cmd", 8'h02, 8'h80);
    xfer("w0", 8'hAA, 8'h00);
    xfer("w1", 8'hBB, 8'h00);
    xfer("w2", 8'hCC, 8'h00);
    xfer("w3", 8'hDD, 8'h00);
    txn_end;
    chk32("mem[0x20] = 0xDDCCBBAA", mem[10'h020], 32'hDDCC_BBAA);

    // abort a WRITE_MEM half-way through a data byte
    txn_begin;
    xfer("abort WRITE_MEM cmd", 8'h02, 8'h80);
    xfer("abort arg0", 8'h11, 8'h00);
    spi_partial(8'h22, 3);            // 3 of 8 bits of arg1 ...
    txn_end;                          // ... then CS deasserts mid-byte
    chk32("aborted write did not touch mem[0x21]", mem[10'h021], 32'h0000_0000);
    chk32("aborted write did not touch mem[0x20]", mem[10'h020], 32'hDDCC_BBAA);
    txn_begin;
    xfer("PING after byte abort cmd", 8'h0D, 8'h80);
    xfer("PING after byte abort arg", 8'h5A, 8'hA5);
    txn_end;

    // ===================== 12. CS abort at a byte boundary ===========
    txn_begin;
    xfer("boundary abort cmd", 8'h02, 8'h80);
    xfer("boundary abort arg0", 8'h33, 8'h00);
    txn_end;                          // clean byte boundary, command incomplete
    chk32("boundary abort did not write mem[0x21]", mem[10'h021], 32'h0000_0000);
    txn_begin;
    xfer("PING after boundary abort cmd", 8'h0D, 8'h80);
    xfer("PING after boundary abort arg", 8'h5A, 8'hA5);
    txn_end;

    // ===================== 12b. long CS-low window =====================
    // (a) CS asserted for 50 us before the first SCK edge
    txn_begin;
    #50000;
    xfer("long-CS PING cmd", 8'h0D, 8'h80);
    xfer("long-CS PING arg", 8'h5A, 8'hA5);
    txn_end;
    // (b) 30 us pause between the bytes of one command, CS held low throughout
    txn_begin;
    xfer("paused SET_ADDR cmd", 8'h01, 8'h80);
    #30000;
    xfer("paused SET_ADDR a0", 8'h10, 8'h00);
    #30000;
    xfer("paused SET_ADDR a1", 8'h00, 8'h00);
    #30000;
    xfer("paused SET_ADDR a2", 8'h00, 8'h00);
    txn_end;
    // the paused SET_ADDR really took effect: read MEM[0x10] back
    txn_begin;
    xfer("after-pause READ_MEM cmd", 8'h03, 8'h80);
    xfer("after-pause READ_MEM b0", 8'h00, 8'hEF);
    xfer("after-pause READ_MEM b1", 8'h00, 8'hBE);
    xfer("after-pause READ_MEM b2", 8'h00, 8'hAD);
    xfer("after-pause READ_MEM b3", 8'h00, 8'hDE);
    txn_end;

    // ===================== 12c. command split across CS windows ========
    // start a WRITE_MEM, abort it by deasserting CS after one argument, then
    // send the remaining bytes in a NEW CS window: they are decoded as fresh
    // commands, nothing is written, and the engine is clean.
    txn_begin;
    xfer("split WRITE_MEM cmd", 8'h02, 8'h80);
    xfer("split arg0", 8'h55, 8'h00);
    txn_end;                                  // abort
    txn_begin;
    xfer("split remainder b1 as CMD", 8'h00, 8'h80);   // unknown -> ignored
    xfer("split remainder b2 as CMD", 8'h00, 8'h80);
    xfer("split remainder b3 as CMD", 8'h00, 8'h80);
    txn_end;
    chk32("split command wrote nothing", mem[10'h021], 32'h0000_0000);
    txn_begin;
    xfer("PING after split cmd", 8'h0D, 8'h80);
    xfer("PING after split arg", 8'h5A, 8'hA5);
    txn_end;

    // ===================== 13. back-to-back commands, one CS window ===
    txn_begin;
    xfer("btb PING cmd",  8'h0D, 8'h80);
    xfer("btb PING arg",  8'h5A, 8'hA5);
    xfer("btb SET_ADDR cmd", 8'h01, 8'h80);
    xfer("btb SET_ADDR a0",  8'h10, 8'h00);
    xfer("btb SET_ADDR a1",  8'h00, 8'h00);
    xfer("btb SET_ADDR a2",  8'h00, 8'h00);
    xfer("btb READ_MEM cmd", 8'h03, 8'h80);
    xfer("btb READ_MEM b0",  8'h00, 8'hEF);
    xfer("btb READ_MEM b1",  8'h00, 8'hBE);
    xfer("btb READ_MEM b2",  8'h00, 8'hAD);
    xfer("btb READ_MEM b3",  8'h00, 8'hDE);
    xfer("btb PING2 cmd", 8'h0D, 8'h80);
    xfer("btb PING2 arg", 8'h5A, 8'hA5);
    txn_end;

    // back-to-back across two CS windows
    txn_begin; xfer("btw PING cmd", 8'h0D, 8'h80); xfer("btw PING arg", 8'h5A, 8'hA5); txn_end;
    txn_begin; xfer("btw PING2 cmd", 8'h0D, 8'h80); xfer("btw PING2 arg", 8'h5A, 8'hA5); txn_end;

    // ===================== 14. stalled-read watchdog =================
    mem_stall = 1'b1;
    txn_begin;
    xfer("stall SET_ADDR cmd", 8'h01, 8'h80);
    xfer("stall SET_ADDR a0",  8'h10, 8'h00);
    xfer("stall SET_ADDR a1",  8'h00, 8'h00);
    xfer("stall SET_ADDR a2",  8'h00, 8'h00);
    xfer("stall READ_MEM cmd", 8'h03, 8'h80);
    xfer("stall READ_MEM b0",  8'h00, 8'h00);
    xfer("stall READ_MEM b1",  8'h00, 8'h00);
    xfer("stall READ_MEM b2",  8'h00, 8'h00);
    xfer("stall READ_MEM b3",  8'h00, 8'h00);
    txn_end;
    mem_stall = 1'b0;
    txn_begin;
    xfer("recover PING cmd", 8'h0D, 8'h80);
    xfer("recover PING arg", 8'h5A, 8'hA5);
    txn_end;

    // ===================== 15. unknown command / PING any arg =========
    txn_begin;
    xfer("unknown cmd", 8'h7E, 8'h80);     // ignored, engine stays free
    xfer("after unknown PING cmd", 8'h0D, 8'h80);
    xfer("after unknown PING arg", 8'h5A, 8'hA5);
    txn_end;
    txn_begin;
    xfer("PING any arg cmd", 8'h0D, 8'h80);
    xfer("PING any arg -> 0xA5", 8'h00, 8'hA5);
    txn_end;

    repeat (10) @(posedge clk);
    chk8("final MISO low", spi_miso, 8'h00);
    chk8("final IRQ low", {7'd0, irq_out}, 8'h00);

    if (errors == 0) begin
      $display("HOST_TB: PASS");
      $finish;
    end else begin
      $display("HOST_TB: FAIL (%0d errors)", errors);
      $fatal(1);
    end
  end

  // global timeout so a deadlock never hangs the CI job
  initial begin
    #40000000;                       // 40 ms of simulated time (slow-SCK runs)
    $display("HOST_TB: FAIL (timeout)");
    $fatal(1);
  end

endmodule

`default_nettype wire

// ============================================================================
//  loom_host.v -- LOOM host bridge: SPI mode-0 slave + host command engine
//
//  Ports: spec/OWNERS.md (verbatim).  Constants: rtl/loom_bus.vh (frozen).
//  Protocol: spec/ARCHITECTURE.md sec.6 + the canonical framing agreed with the
//  Lead (host/loom_host.py and the verification suite assume this exactly).
//
// ----------------------------------------------------------------------------
//  TIMING / SCK SAMPLING (max SCK)
// ----------------------------------------------------------------------------
//  * spi_cs_n / spi_sck / spi_mosi are asynchronous to clk and are captured by
//    2-flop synchronisers plus one history flop; SCK edges are detected in the
//    clk domain.  SINGLE CLOCK DOMAIN (clk only), no gated/derived clocks.
//  * Max SCK = clk / 6  (= 10 MHz at 60 MHz clk; up to 11 MHz at 66 MHz clk).
//    The SPI physical layer spends 2..3 clk detecting an SCK edge and launches
//    the next MISO bit on that detection edge, i.e. MISO changes 2..3 clk
//    after the sampling rising edge and is stable >= 3 clk before the next
//    rising edge.  (A literal "shift on the falling edge" is NOT possible at
//    10 MHz: the 2-FF synchroniser latency of one half period would place the
//    shift exactly on top of the master's next sampling edge.)
//  * CS setup: >= 3 clk (>= 50 ns at 10 MHz SCK / 60 MHz clk) between CS
//    assertion and the first SCK rising edge; 3 clk between the last SCK edge
//    and CS deassertion.  CS frames one transaction; CS low time may be long.
//  * MOSI is sampled on the synchronised SCK rising edge while CS is low.
//  * MISO is 0 while CS is high and after reset.
//  * The design is fully static CMOS: no latches, no `initial`, all state has
//    an asynchronous active-low reset.
//
// ----------------------------------------------------------------------------
//  FRAMING (canonical; every byte transfer is full duplex)
// ----------------------------------------------------------------------------
//  * Transfer 0 is the command byte; its MISO byte is 0x80 (accepted) or 0x81
//    (busy -- only observable if the engine were still executing a previous
//    command; CS deassert aborts, so a fresh CS assert normally sees 0x80).
//  * All argument transfers return 0x00.
//  * Read payloads are returned on the transfers AFTER the command byte
//    (the host clocks dummy bytes); argument bytes are LSB-first, fields
//    byte-aligned.
//
//    CMD            args (host->chip)              MISO after the CMD byte
//    0x01 SET_ADDR  a0,a1,a2   (a[23:0])           none
//    0x02 WRITE_MEM w0,w1,w2,w3(w[31:0])           none (mem_we pulse)
//    0x03 READ_MEM  (none)                         b0,b1,b2,b3 = MEM[addr],
//                                                  addr auto-increments
//    0x04 WRITE_CSR csr,d0,d1  (d[15:0])           none (csr_wr_en pulse)
//    0x05 READ_CSR  csr                            b0,b1 = csr[7:0],[15:8]
//    0x06 TX_PUSH   t,d                            none (rx_push pulse)
//    0x07 RX_POP    t                              b0 = TX FIFO byte
//                                                  (0x00 if empty, no error)
//    0x08 SET_PC    t,p0,p1    (pc[15:0])          none (csr_wr 0x48+16t)
//    0x09 RUN       mask[3:0]                      none (csr_wr 0x30 = mask)
//    0x0A STOP      mask[3:0]                      none (csr_wr 0x31 = mask)
//    0x0B STATUS    (none)                         b0..b3 = thread 0..3
//    0x0C ACK_IRQ   (none)                         none (csr_wr 0x14 = 0)
//    0x0D PING      any byte                       0xA5
//    (unknown commands are ignored; the engine stays free)
//
//  STATUS thread byte (thread t on transfer t+1):
//     bit7 = halted         (CSR TSTATUS bit1, csr 0x40+16t+0x02)
//     bit6 = en             (CSR TSTATUS bit0)
//     bit5 = tx_full        (CSR TFIFO 0x40+16t+0x03: tx_count[3:0]==8)
//     bit4 = tx_empty       (direct thread TX-FIFO empty input)
//     bit3 = rx_full        (direct thread RX-FIFO full input)
//     bit2 = rx_empty       (CSR TFIFO: rx_count[3:0]==0)
//     bit1:0 = 2'b00
//  STATUS is assembled from four TSTATUS + four TFIFO CSR reads (eight
//  back-to-back reads issued on consecutive clk starting with the command
//  byte's last SCK edge; captured 2 clk after each issue, so the host must
//  answer pipelined reads: csr_rd_data = f(csr_rd_addr) one cycle after
//  csr_rd_en).  Thread 0's byte has no transfer boundary before it is needed,
//  so it is launched in two steps: {halted,en} as soon as the first read
//  returns, then the full byte when the TFIFO read returns -- bit7 never
//  changes, so MISO is glitch-free.
//
//  ADDRESS MAP (core-side decode, see rtl/loom_mem.v): host mem_addr[9:0]
//    0x000-0x3BF = PMEM word address (1024-word build uses 0x000-0x3BF)
//    0x3C0-0x3FF = DMEM word address (DMEM_WORDS = 64)
//  SET_ADDR takes a 24-bit pointer; only the low 10 bits reach mem_addr and the
//  pointer auto-increments by one word after every WRITE_MEM / READ_MEM.
//
//  Core-side CSR addresses (the core MUST implement these):
//    0x30 RUNMASK   : write {12'b0, mask[3:0]}  "clear halt, en |= mask"
//    0x31 STOPMASK  : write {12'b0, mask[3:0]}  "en &= ~mask"
//    0x40+16t+0x08  : TPC (SET_PC)
//    0x40+16t+0x02  : TSTATUS = {14'b0, halted, en}   (read, for STATUS)
//    0x40+16t+0x03  : TFIFO, counts at [11:8]=rx_count, [7:4]=tx_count
//                     (read, for STATUS; also [1]=rx_full,[0]=tx_full)
//    0x14 CSR_IRQ   : write 0 = clear the core IRQ code (HCMD_ACK_IRQ)
//
// ----------------------------------------------------------------------------
//  HANDSHAKES
// ----------------------------------------------------------------------------
//  * mem_re is a one-clk pulse with mem_addr stable in that cycle; answer with
//    a one-clk mem_rvalid (mem_rdata valid in that cycle), one cycle later.
//  * csr_rd_en is a one-clk pulse; csr_rd_data is sampled on the second clk
//    after the issue edge ("valid the cycle after csr_rd_en", registered).
//  * mem_we / csr_wr_en are one-clk pulses; writes take effect immediately.
//  * tx_pop is a one-clk pulse, tx_pop_data valid in that same cycle; sampled
//    on the following edge.  rx_push is a one-clk pulse.
//  * A 4-clk watchdog on every read handshake: a missing/late response makes
//    the read return 0 and the command complete, so the SPI slave can never
//    deadlock the host.
//  * The next command byte is only accepted when the previous command, its
//    argument bytes, its response bytes and every handshake are complete.
//  * irq_out is a registered level: set when irq_code != 0, cleared by
//    HCMD_ACK_IRQ (which also writes CSR_IRQ=0 so the core drops the code).
//    It can therefore never glitch low-high-low inside a byte transfer: it is
//    only cleared at a byte boundary (the ACK command's last SCK edge) and set
//    only by a sticky IRQ event.
// ============================================================================
`default_nettype none
// frozen bus/command constants; resolved from the repo root (or rtl/ with -I)
`ifndef LOOM_BUS_VH
 `include "rtl/loom_bus.vh"
`endif

module loom_host (
  input  wire        clk,
  input  wire        rst_n,
  // Tiny Tapeout host pins (async to clk on the SCK side: 2-FF sync inside)
  input  wire        spi_cs_n,      // ui_in[0]
  input  wire        spi_sck,       // ui_in[1]
  input  wire        spi_mosi,      // ui_in[2]
  output wire        spi_miso,      // uo_out[0]
  output wire        irq_out,       // uo_out[1]
  // host -> memory port: mem_addr[9:0] = 0x000-0x3BF PMEM, 0x3C0-0x3FF DMEM
  // (the core/memory decodes the region; the bridge only passes the pointer)
  output reg         mem_we,
  output reg  [9:0]  mem_addr,
  output reg  [31:0] mem_wdata,
  output reg         mem_re,        // start a read
  input  wire [31:0] mem_rdata,
  input  wire        mem_rvalid,    // one-cycle pulse: mem_rdata valid
  // host -> core CSR port (core owns all CSRs / thread state)
  output reg         csr_wr_en,
  output reg  [7:0]  csr_wr_addr,
  output reg  [15:0] csr_wr_data,
  output reg         csr_rd_en,
  output reg  [7:0]  csr_rd_addr,
  input  wire [15:0] csr_rd_data,   // valid the cycle after csr_rd_en
  // host <-> per-thread byte FIFOs
  output reg         rx_push,       // push a byte into thread RX FIFO
  output reg  [1:0]  rx_push_tid,
  output reg  [7:0]  rx_push_data,
  input  wire [3:0]  rx_full,
  output reg         tx_pop,        // pop a byte from thread TX FIFO
  output reg  [1:0]  tx_pop_tid,
  input  wire [7:0]  tx_pop_data,   // valid same cycle as tx_pop
  input  wire [3:0]  tx_empty,
  // irq / status sources
  input  wire [15:0] irq_code,
  input  wire [3:0]  thread_run_mask
);

  // --------------------------------------------------------------------------
  // command engine states
  // --------------------------------------------------------------------------
  localparam [2:0] ST_IDLE = 3'd0;   // waiting for a command byte
  localparam [2:0] ST_ARG  = 3'd1;   // collecting argument bytes
  localparam [2:0] ST_WAIT = 3'd2;   // read / pop handshake in flight
  localparam [2:0] ST_RESP = 3'd3;   // response bytes queued to the TX shifter

  localparam [1:0] W_MEM = 2'd0;     // mem_re     .. mem_rvalid
  localparam [1:0] W_CSR = 2'd1;     // csr_rd_en  .. csr_rd_data
  localparam [1:0] W_POP = 2'd2;     // tx_pop     .. tx_pop_data

  // --------------------------------------------------------------------------
  // SPI pin capture: 2-flop synchronisers + one history flop for edge detect
  // --------------------------------------------------------------------------
  reg [2:0] sck_s;                   // [0] meta, [1] sync, [2] history
  reg [2:0] cs_s;
  reg       mosi_s;

  wire sck_rise = sck_s[1] & ~sck_s[2];   // synchronised SCK rising edge
  wire cs_rise  = cs_s[1]  & ~cs_s[2];    // CS deasserted (end of window)
  wire cs_fall  = ~cs_s[1] &  cs_s[2];    // CS asserted  (start of window)
  wire cs_act   = ~cs_s[1];               // synchronised CS, active low

  // --------------------------------------------------------------------------
  // byte engine
  // --------------------------------------------------------------------------
  reg  [2:0] bit_cnt;                // MOSI bits captured in this transfer
  reg  [7:0] rx_shift;
  reg  [7:0] tx_shift;

  wire [7:0] rx_byte   = {rx_shift[6:0], mosi_s};                // complete byte
  wire       byte_done = cs_act & sck_rise & (bit_cnt == 3'd7);  // end of transfer

  // MISO: MSB of the transmit shifter; 0 outside a CS window (and after reset).
  assign spi_miso = cs_act ? tx_shift[7] : 1'b0;

  // --------------------------------------------------------------------------
  // host interrupt level (registered, sticky)
  // --------------------------------------------------------------------------
  reg        irq_r;
  reg [1:0]  irq_hold;
  reg        ack_irq;                // one-clk pulse at ACK_IRQ completion
  assign irq_out = irq_r;

  // --------------------------------------------------------------------------
  // command engine state
  // --------------------------------------------------------------------------
  reg [2:0]  st;
  reg [7:0]  cmd;
  reg [2:0]  nargs;                  // argument bytes expected
  reg [2:0]  argi;                   // argument bytes received
  reg [7:0]  ab0, ab1, ab2, ab3;     // argument byte latches (LSB first)
  reg [23:0] addr_ptr;               // word address pointer
  reg [2:0]  resp_len;
  reg [2:0]  resp_idx;               // response bytes already launched
  reg [31:0] resp_sh;                // remaining response bytes, LSB next
  reg [2:0]  dead;                   // watchdog: clk since handshake issue
  reg [1:0]  wait_kind;

  // --------------------------------------------------------------------------
  // STATUS fetch engine (TSTATUS + TFIFO per thread, see header)
  // --------------------------------------------------------------------------
  reg        stat_en;                // fetch in progress
  reg [3:0]  stat_i;                 // next read index (0..7), 8 = done
  reg        p1_v;                   // read-issued pipeline
  reg [3:0]  p1_i;
  reg        p2_v;                   // capture pipeline (2 clk after issue)
  reg [3:0]  p2_i;
  reg [7:0]  statb [0:3];            // assembled status byte per thread
  reg        resp_is_status;

  wire [7:0] stat_rd_addr = 8'h42 + {2'b00, stat_i[2:1], 4'b0000} + {7'b0, stat_i[0]};
  wire [3:0] cap_tx_cnt   = csr_rd_data[7:4];
  wire [3:0] cap_rx_cnt   = csr_rd_data[11:8];
  wire       cap_tx_full  = (cap_tx_cnt == 4'd8);
  wire       cap_rx_empty = (cap_rx_cnt == 4'd0);

  // --------------------------------------------------------------------------
  // main sequential process (single clock domain, all state reset)
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      sck_s        <= 3'b000;
      cs_s         <= 3'b111;
      mosi_s       <= 1'b0;
      bit_cnt      <= 3'd0;
      rx_shift     <= 8'h00;
      tx_shift     <= `HCMD_RESP_OK;   // first command transfer returns 0x80
      mem_we       <= 1'b0;
      mem_addr     <= 10'd0;
      mem_wdata    <= 32'd0;
      mem_re       <= 1'b0;
      csr_wr_en    <= 1'b0;
      csr_wr_addr  <= 8'h00;
      csr_wr_data  <= 16'h0000;
      csr_rd_en    <= 1'b0;
      csr_rd_addr  <= 8'h00;
      rx_push      <= 1'b0;
      rx_push_tid  <= 2'd0;
      rx_push_data <= 8'h00;
      tx_pop       <= 1'b0;
      tx_pop_tid   <= 2'd0;
      ack_irq      <= 1'b0;
      st           <= ST_IDLE;
      cmd          <= 8'h00;
      nargs        <= 3'd0;
      argi         <= 3'd0;
      ab0          <= 8'h00;
      ab1          <= 8'h00;
      ab2          <= 8'h00;
      ab3          <= 8'h00;
      addr_ptr     <= 24'd0;
      resp_len     <= 3'd0;
      resp_idx     <= 3'd0;
      resp_sh      <= 32'h0000_0000;
      dead         <= 3'd0;
      wait_kind    <= W_MEM;
      stat_en      <= 1'b0;
      stat_i       <= 4'd8;
      p1_v         <= 1'b0;
      p1_i         <= 4'd0;
      p2_v         <= 1'b0;
      p2_i         <= 4'd0;
      resp_is_status <= 1'b0;
      statb[0]     <= 8'h00;
      statb[1]     <= 8'h00;
      statb[2]     <= 8'h00;
      statb[3]     <= 8'h00;
    end else begin
      // ----------------------------------------------------------------------
      // pulse defaults (command logic below overrides for one clk)
      // ----------------------------------------------------------------------
      mem_we    <= 1'b0;
      mem_re    <= 1'b0;
      csr_wr_en <= 1'b0;
      csr_rd_en <= 1'b0;
      rx_push   <= 1'b0;
      tx_pop    <= 1'b0;
      ack_irq   <= 1'b0;

      // ----------------------------------------------------------------------
      // SPI pin pipeline
      // ----------------------------------------------------------------------
      sck_s  <= {sck_s[1:0], spi_sck};
      cs_s   <= {cs_s[1:0],  spi_cs_n};
      mosi_s <= spi_mosi;

      // ----------------------------------------------------------------------
      // byte engine: sample MOSI, launch next MISO bit, count bit boundaries
      // ----------------------------------------------------------------------
      if (cs_fall) begin
        bit_cnt  <= 3'd0;
        rx_shift <= 8'h00;
      end else if (cs_act & sck_rise) begin
        rx_shift <= {rx_shift[6:0], mosi_s};
        bit_cnt  <= bit_cnt + 3'd1;
        if (bit_cnt != 3'd7)
          tx_shift <= {tx_shift[6:0], 1'b0};   // byte_done loads a fresh byte instead
      end

      // ----------------------------------------------------------------------
      // handshake watchdog: counts clk cycles since the request was issued
      // ----------------------------------------------------------------------
      if ((st == ST_WAIT) && (dead != 3'd7))
        dead <= dead + 3'd1;

      // ----------------------------------------------------------------------
      // read / pop completion (may land between two byte boundaries)
      // ----------------------------------------------------------------------
      if (st == ST_WAIT) begin
        case (wait_kind)
          W_MEM: begin
            if (mem_rvalid) begin
              tx_shift <= mem_rdata[7:0];
              resp_sh  <= {8'h00, mem_rdata[31:8]};
              resp_len <= 3'd4;
              resp_idx <= 3'd1;
              st       <= ST_RESP;
            end else if (dead >= 3'd4) begin      // stalled read -> 0
              tx_shift <= 8'h00;
              resp_sh  <= 32'h0000_0000;
              resp_len <= 3'd4;
              resp_idx <= 3'd1;
              st       <= ST_RESP;
            end
          end
          W_CSR: begin
            if (dead >= 3'd1) begin               // data valid the cycle after
              tx_shift <= csr_rd_data[7:0];
              resp_sh  <= {16'h0000, csr_rd_data[15:8]};
              resp_len <= 3'd2;
              resp_idx <= 3'd1;
              st       <= ST_RESP;
            end else if (dead >= 3'd4) begin
              tx_shift <= 8'h00;
              resp_sh  <= 32'h0000_0000;
              resp_len <= 3'd2;
              resp_idx <= 3'd1;
              st       <= ST_RESP;
            end
          end
          default: begin                          // W_POP: data in the pop cycle
            tx_shift <= tx_pop_data;
            resp_len <= 3'd1;
            resp_idx <= 3'd1;
            st       <= ST_RESP;
          end
        endcase
      end

      // ----------------------------------------------------------------------
      // STATUS fetch: issue one CSR read per clk, capture 2 clk after issue
      // ----------------------------------------------------------------------
      if (stat_en && (stat_i < 4'd8)) begin
        csr_rd_en   <= 1'b1;
        csr_rd_addr <= stat_rd_addr;
        p1_v        <= 1'b1;
        p1_i        <= stat_i;
        stat_i      <= stat_i + 4'd1;
      end else begin
        p1_v <= 1'b0;
      end
      p2_v <= p1_v;
      p2_i <= p1_i;

      if (p2_v && stat_en) begin
        if (p2_i[0] == 1'b0) begin
          // TSTATUS = {14'b0, halted, en}
          statb[p2_i[2:1]] <= {csr_rd_data[1:0], statb[p2_i[2:1]][5:0]};
        end else begin
          // TFIFO counts: [11:8] = rx_count, [7:4] = tx_count
          statb[p2_i[2:1]] <= {statb[p2_i[2:1]][7:6],
                               cap_tx_full,
                               tx_empty[p2_i[2:1]],
                               rx_full[p2_i[2:1]],
                               cap_rx_empty,
                               2'b00};
        end
        if (p2_i == 4'd7)
          stat_en <= 1'b0;                     // last read captured
        // thread 0's byte is needed before its transfer boundary: launch it
        if (resp_is_status && (p2_i[2:1] == 2'd0)) begin
          if (p2_i[0] == 1'b0)
            tx_shift <= {csr_rd_data[1:0], 6'b000000};      // bit7 = halted
          else
            tx_shift <= {statb[0][7:6], cap_tx_full,
                         tx_empty[0], rx_full[0], cap_rx_empty, 2'b00};
        end
      end

      // ----------------------------------------------------------------------
      // end of a byte transfer: decode byte / issue command phase
      // ----------------------------------------------------------------------
      if (byte_done) begin
        case (st)
          // ---------------- new command byte -------------------------------
          ST_IDLE: begin
            cmd  <= rx_byte;
            argi <= 3'd0;
            case (rx_byte)
              `HCMD_SET_ADDR: begin
                nargs <= 3'd3; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_WRITE_MEM: begin
                nargs <= 3'd4; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_READ_MEM: begin
                mem_re    <= 1'b1;
                mem_addr  <= addr_ptr[9:0];
                addr_ptr  <= addr_ptr + 24'd1;
                dead      <= 3'd0;
                wait_kind <= W_MEM;
                st        <= ST_WAIT;
                tx_shift  <= 8'h00;                // filled when data arrives
              end
              `HCMD_WRITE_CSR: begin
                nargs <= 3'd3; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_READ_CSR: begin
                nargs <= 3'd1; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_TX_PUSH: begin
                nargs <= 3'd2; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_RX_POP: begin
                nargs <= 3'd1; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_SET_PC: begin
                nargs <= 3'd3; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_RUN: begin
                nargs <= 3'd1; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_STOP: begin
                nargs <= 3'd1; st <= ST_ARG; tx_shift <= 8'h00;
              end
              `HCMD_STATUS: begin
                // issue the first CSR read immediately, then one per clk
                stat_en   <= 1'b1;
                stat_i    <= 4'd1;
                csr_rd_en <= 1'b1;
                csr_rd_addr <= 8'h42;              // TSTATUS thread 0
                p1_v      <= 1'b1;
                p1_i      <= 4'd0;
                resp_is_status <= 1'b1;
                resp_len  <= 3'd4;
                resp_idx  <= 3'd1;
                st        <= ST_RESP;
                tx_shift  <= 8'h00;
              end
              `HCMD_ACK_IRQ: begin
                ack_irq     <= 1'b1;
                csr_wr_en   <= 1'b1;
                csr_wr_addr <= `CSR_IRQ;           // 0x14: write 0 = clear
                csr_wr_data <= 16'h0000;
                st          <= ST_IDLE;
                tx_shift    <= `HCMD_RESP_OK;
              end
              `HCMD_PING: begin
                // 0x0D returns 0xA5 on the argument transfer itself
                nargs <= 3'd1; st <= ST_ARG; tx_shift <= 8'hA5;
              end
              default: begin                        // unknown command: ignore
                st       <= ST_IDLE;
                tx_shift <= `HCMD_RESP_OK;
              end
            endcase
          end

          // ---------------- argument bytes ---------------------------------
          ST_ARG: begin
            if (argi == 3'd0) ab0 <= rx_byte;
            if (argi == 3'd1) ab1 <= rx_byte;
            if (argi == 3'd2) ab2 <= rx_byte;
            if (argi == 3'd3) ab3 <= rx_byte;

            if (argi == (nargs - 3'd1)) begin
              // ---- last argument: execute the command ---------------------
              argi     <= 3'd0;
              st       <= ST_IDLE;
              tx_shift <= `HCMD_RESP_OK;           // free for the next command
              case (cmd)
                `HCMD_SET_ADDR: begin
                  addr_ptr <= {8'h00, rx_byte, ab1, ab0};
                end
                `HCMD_WRITE_MEM: begin
                  mem_we    <= 1'b1;
                  mem_addr  <= addr_ptr[9:0];
                  mem_wdata <= {rx_byte, ab2, ab1, ab0};
                  addr_ptr  <= addr_ptr + 24'd1;
                end
                `HCMD_WRITE_CSR: begin
                  csr_wr_en   <= 1'b1;
                  csr_wr_addr <= ab0;
                  csr_wr_data <= {rx_byte, ab1};
                end
                `HCMD_READ_CSR: begin
                  csr_rd_en   <= 1'b1;
                  csr_rd_addr <= rx_byte;
                  dead        <= 3'd0;
                  wait_kind   <= W_CSR;
                  st          <= ST_WAIT;
                  tx_shift    <= 8'h00;
                end
                `HCMD_TX_PUSH: begin
                  if (!rx_full[ab0[1:0]]) begin
                    rx_push      <= 1'b1;
                    rx_push_tid  <= ab0[1:0];
                    rx_push_data <= rx_byte;
                  end
                end
                `HCMD_RX_POP: begin
                  if (!tx_empty[rx_byte[1:0]]) begin
                    tx_pop      <= 1'b1;
                    tx_pop_tid  <= rx_byte[1:0];
                    st          <= ST_WAIT;
                    wait_kind   <= W_POP;
                    dead        <= 3'd0;
                    tx_shift    <= 8'h00;
                  end else begin                    // empty FIFO -> return 0x00
                    tx_shift <= 8'h00;
                    resp_len <= 3'd1;
                    resp_idx <= 3'd1;
                    st       <= ST_RESP;
                  end
                end
                `HCMD_SET_PC: begin
                  csr_wr_en   <= 1'b1;
                  csr_wr_addr <= 8'h48 + {2'b00, ab0[1:0], 4'b0000};
                  csr_wr_data <= {rx_byte, ab1};
                end
                `HCMD_RUN: begin
                  csr_wr_en   <= 1'b1;
                  csr_wr_addr <= 8'h30;             // host-only RUNMASK
                  csr_wr_data <= {12'h000, rx_byte[3:0]};
                end
                `HCMD_STOP: begin
                  csr_wr_en   <= 1'b1;
                  csr_wr_addr <= 8'h31;             // host-only STOPMASK
                  csr_wr_data <= {12'h000, rx_byte[3:0]};
                end
                `HCMD_PING: begin
                  // already returned 0xA5 on this transfer; argument ignored
                  st       <= ST_IDLE;
                  tx_shift <= `HCMD_RESP_OK;
                end
                default: begin
                  st       <= ST_IDLE;
                  tx_shift <= `HCMD_RESP_OK;
                end
              endcase
            end else begin
              argi     <= argi + 3'd1;
              tx_shift <= 8'h00;                    // filler during arguments
            end
          end

          // ---------------- response bytes --------------------------------
          ST_RESP: begin
            if (resp_idx < resp_len) begin
              if (resp_is_status)
                tx_shift <= statb[resp_idx[1:0]];
              else begin
                tx_shift <= resp_sh[7:0];
                resp_sh  <= {8'h00, resp_sh[31:8]};
              end
              resp_idx <= resp_idx + 3'd1;
            end else begin
              st             <= ST_IDLE;
              resp_is_status <= 1'b0;
              tx_shift       <= `HCMD_RESP_OK;
            end
          end

          // late read handshake: keep the framed 0x00 byte, stay in WAIT
          default: begin
            st <= ST_WAIT;
          end
        endcase
      end

      // ----------------------------------------------------------------------
      // CS deasserted: end of transaction.  Any command, argument set,
      // pending response or read handshake is discarded; a partially received
      // byte is dropped.  MISO/IRQ return to their idle values.
      // ----------------------------------------------------------------------
      if (cs_rise) begin
        bit_cnt        <= 3'd0;
        rx_shift       <= 8'h00;
        tx_shift       <= `HCMD_RESP_OK;
        st             <= ST_IDLE;
        nargs          <= 3'd0;
        argi           <= 3'd0;
        ab0            <= 8'h00;
        ab1            <= 8'h00;
        ab2            <= 8'h00;
        ab3            <= 8'h00;
        resp_len       <= 3'd0;
        resp_idx       <= 3'd0;
        resp_sh        <= 32'h0000_0000;
        resp_is_status <= 1'b0;
        dead           <= 3'd0;
        stat_en        <= 1'b0;
        stat_i         <= 4'd8;
        p1_v           <= 1'b0;
        p2_v           <= 1'b0;
        mem_we         <= 1'b0;
        mem_re         <= 1'b0;
        csr_wr_en      <= 1'b0;
        csr_rd_en      <= 1'b0;
        rx_push        <= 1'b0;
        tx_pop         <= 1'b0;
      end
    end
  end

  // --------------------------------------------------------------------------
  // host interrupt latch
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      irq_r    <= 1'b0;
      irq_hold <= 2'd0;
    end else if (ack_irq) begin
      irq_r    <= 1'b0;                    // cleared by HCMD_ACK_IRQ
      irq_hold <= 2'd2;                    // ignore a stale irq_code for 2 clk
    end else if (irq_hold != 2'd0) begin
      irq_hold <= irq_hold - 2'd1;         // keep low while the core clears
      irq_r    <= 1'b0;
    end else if (irq_code != 16'h0000) begin
      irq_r    <= 1'b1;                    // set on any IRQ code
    end
  end

endmodule

`default_nettype wire

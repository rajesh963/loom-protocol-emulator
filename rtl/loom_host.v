// ============================================================================
//  loom_host.v -- LOOM host bridge: SPI mode-0 slave + host command engine
//
//  Ports: spec/OWNERS.md (verbatim).  Constants: rtl/loom_bus.vh (frozen).
//  Protocol: spec/ARCHITECTURE.md sec.6 (canonical framing).
//  Parameter FIFO_DEPTH (default 8, area build 4) sets the per-thread core FIFO
//  depth the bridge assumes when it reports `tx_full` in STATUS; the bridge
//  stores no FIFO bytes and its area is independent of the value.  The port
//  list is unchanged, so a top that does not override it gets the default 8.
//
//  AREA NOTE: this module is deliberately written for minimum flip-flops.
//  The argument bytes live in ONE 32-bit word (`argw`, one byte lane per
//  argument) which is also the memory write-data port, so mem_wdata/mem_addr/
//  csr_wr_addr/csr_wr_data/rx_push_*/tx_pop_tid/csr_rd_addr are COMBINATIONAL
//  outputs derived from it: no shadow copies.  Commands that need the last
//  argument byte (all writes) fire their one-clk enable pulse in an "issue"
//  phase (`iph`) one clk later, when the fully latched word is stable.  The
//  response path is a 24-bit shifter (byte 0 goes straight to the TX shifter),
//  the STATUS fetch keeps only two assembled bytes (ping-pong) and the command
//  state is a 4-bit opcode + a 2-bit FSM.  The wire protocol is unchanged.
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
//     bit5 = tx_full        (CSR TFIFO 0x40+16t+0x03: tx_count == FIFO_DEPTH)
//     bit4 = tx_empty       (direct thread TX-FIFO empty input)
//     bit3 = rx_full        (direct thread RX-FIFO full input)
//     bit2 = rx_empty       (CSR TFIFO: rx_count[3:0]==0)
//     bit1:0 = 2'b00
//  STATUS is assembled from four TSTATUS + four TFIFO CSR reads (eight
//  back-to-back reads issued on consecutive clk starting with the command
//  byte's last SCK edge; the host CSR read port is combinational on
//  csr_rd_addr, so each read is captured one clk after it is issued).
//  Thread 0's byte has no transfer boundary before it is needed, so it is
//  launched in two steps: {halted,en} as soon as the first read returns, then
//  the full byte when the TFIFO read returns -- bit7 never changes, so MISO is
//  glitch-free.  Bytes 1..3 are held in a two-entry ping-pong (thread parity).
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
//  ADDRESS MAP (core-side decode, see rtl/loom_mem.v): host mem_addr[9:0]
//    0x000-0x3BF = PMEM word address (1024-word build uses 0x000-0x3BF)
//    0x3C0-0x3FF = DMEM word address (DMEM_WORDS = 64)
//  SET_ADDR takes a 24-bit pointer; only the low 10 bits reach mem_addr (the
//  pointer register is 10 bits wide) and it auto-increments by one word after
//  every completed WRITE_MEM / READ_MEM.
//
// ----------------------------------------------------------------------------
//  HANDSHAKES
// ----------------------------------------------------------------------------
//  * mem_re is a one-clk registered pulse with mem_addr stable in that cycle;
//    answer with a one-clk mem_rvalid (mem_rdata valid in that cycle), one
//    cycle later.
//  * csr_rd_en is a one-clk registered pulse; the core presents csr_rd_data
//    combinationally from csr_rd_addr, which is held stable for the whole
//    handshake (READ_CSR) or driven by the fetch index (STATUS).  The data is
//    sampled on the second clk after the issue edge.
//  * mem_we / csr_wr_en / rx_push are one-clk COMBINATIONAL pulses asserted in
//    the issue phase, one clk after the last argument byte was latched; the
//    address/data outputs are stable for exactly that cycle.  Writes take
//    effect immediately.
//  * tx_pop is a one-clk registered pulse, tx_pop_data valid in that same
//    cycle; sampled on the following edge.  tx_pop_tid is combinational.
//  * A 4-clk watchdog on every read handshake: a missing/late response makes
//    the read return 0 and the command complete, so the SPI slave can never
//    deadlock the host.
//  * The next command byte is only accepted when the previous command, its
//    argument bytes, its response bytes and every handshake are complete.
//  * irq_out is a registered level: set when irq_code != 0, cleared by
//    HCMD_ACK_IRQ (which also writes CSR_IRQ=0 so the core drops the code).
//    It can therefore never glitch low-high-low inside a byte transfer: it is
//    only cleared at a byte boundary (the ACK command's issue phase) and set
//    only by a sticky IRQ event.
// ============================================================================
`default_nettype none
// frozen bus/command constants; resolved from the repo root (or rtl/ with -I)
`ifndef LOOM_BUS_VH
 `include "rtl/loom_bus.vh"
`endif

module loom_host #(
  // Bytes per per-thread TX/RX FIFO of the core.  The bridge stores no FIFO
  // data itself; it only needs the depth to report `tx_full` in STATUS
  // (tx_count == FIFO_DEPTH).  Area builds may pass 4 (LOOM_FIFO_SMALL).
  parameter FIFO_DEPTH = 8
)(
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
  // command opcodes (4-bit internal encoding of the 8-bit HCMD_* byte)
  // --------------------------------------------------------------------------
  localparam [3:0] OP_NONE    = 4'd0;
  localparam [3:0] OP_SETADDR = 4'd1;
  localparam [3:0] OP_WMEM    = 4'd2;
  localparam [3:0] OP_RMEM    = 4'd3;
  localparam [3:0] OP_WCSR    = 4'd4;
  localparam [3:0] OP_RCSR    = 4'd5;
  localparam [3:0] OP_TPUSH   = 4'd6;
  localparam [3:0] OP_RPOP    = 4'd7;
  localparam [3:0] OP_SETPC   = 4'd8;
  localparam [3:0] OP_RUN     = 4'd9;
  localparam [3:0] OP_STOP    = 4'd10;
  localparam [3:0] OP_STATUS  = 4'd11;
  localparam [3:0] OP_ACKIRQ  = 4'd12;
  localparam [3:0] OP_PING    = 4'd13;

  // command engine states
  localparam [1:0] ST_IDLE = 2'd0;   // waiting for a command byte
  localparam [1:0] ST_ARG  = 2'd1;   // collecting argument bytes
  localparam [1:0] ST_WAIT = 2'd2;   // read / pop handshake in flight
  localparam [1:0] ST_RESP = 2'd3;   // response bytes queued to the TX shifter

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
  reg  [7:0] tx_shift;               // MISO shifter (MSB out first)

  wire [7:0] rx_byte   = {rx_shift[6:0], mosi_s};                // complete byte
  wire       byte_done = cs_act & sck_rise & (bit_cnt == 3'd7);  // end of transfer

  // MISO: MSB of the transmit shifter; 0 outside a CS window (and after reset).
  assign spi_miso = cs_act ? tx_shift[7] : 1'b0;

  // --------------------------------------------------------------------------
  // host interrupt level (registered, sticky)
  // --------------------------------------------------------------------------
  reg        irq_r;
  reg [1:0]  irq_hold;
  assign irq_out = irq_r;

  // --------------------------------------------------------------------------
  // command engine state (kept as small as possible; see AREA NOTE above)
  // --------------------------------------------------------------------------
  reg [1:0]  st;
  reg [3:0]  op;                     // internal opcode of the running command
  reg [1:0]  argi;                   // argument bytes received (0..3)
  reg [31:0] argw;                   // argument word, one byte lane per arg
  reg [9:0]  addr_ptr;               // word address pointer (mem_addr[9:0])
  reg        iph;                    // issue phase: pulse the write enables
  reg [2:0]  dead;                   // watchdog: clk since handshake issue
  reg [1:0]  wait_kind;
  reg [1:0]  resp_left;              // response bytes still to launch
  reg        resp_is_status;
  reg [23:0] resp_sh;                // remaining response bytes, LSB next
  // mem_re / csr_rd_en / tx_pop are OUTPUT regs (declared in the port list)

  // --------------------------------------------------------------------------
  // STATUS fetch engine (TSTATUS + TFIFO per thread, see header)
  // --------------------------------------------------------------------------
  reg        stat_en;                // fetch in progress
  reg [2:0]  stat_i;                 // next read index (0..7)
  reg        p1_v;                   // read-issued pipeline (capture next clk)
  reg [2:0]  p1_i;
  reg [7:0]  statb [0:3];            // assembled status byte per thread
  wire [1:0] stat_slot = 2'd0 - resp_left;   // byte index 4-resp_left: 1,2,3
  wire [7:0] stat_addr = 8'h42 + {2'b00, p1_i[2:1], 4'b0000} + {7'b0, p1_i[0]};
  localparam [3:0] FIFO_FULL_CNT = FIFO_DEPTH;   // tx_full threshold
  wire [3:0] cap_tx_cnt   = csr_rd_data[7:4];
  wire [3:0] cap_rx_cnt   = csr_rd_data[11:8];
  wire       cap_tx_full  = (cap_tx_cnt == FIFO_FULL_CNT);
  wire       cap_rx_empty = (cap_rx_cnt == 4'd0);   // depth independent

  // --------------------------------------------------------------------------
  // command decode: HCMD_* byte -> 4-bit internal opcode
  // --------------------------------------------------------------------------
  reg [3:0] op_d;
  always @(*) begin
    case (rx_byte)
      `HCMD_SET_ADDR:  op_d = OP_SETADDR;
      `HCMD_WRITE_MEM: op_d = OP_WMEM;
      `HCMD_READ_MEM:  op_d = OP_RMEM;
      `HCMD_WRITE_CSR: op_d = OP_WCSR;
      `HCMD_READ_CSR:  op_d = OP_RCSR;
      `HCMD_TX_PUSH:   op_d = OP_TPUSH;
      `HCMD_RX_POP:    op_d = OP_RPOP;
      `HCMD_SET_PC:    op_d = OP_SETPC;
      `HCMD_RUN:       op_d = OP_RUN;
      `HCMD_STOP:      op_d = OP_STOP;
      `HCMD_STATUS:    op_d = OP_STATUS;
      `HCMD_ACK_IRQ:   op_d = OP_ACKIRQ;
      `HCMD_PING:      op_d = OP_PING;
      default:         op_d = OP_NONE;
    endcase
  end

  // argument-byte count minus one, derived from the opcode (no register)
  reg [1:0] nm1;
  always @(*) begin
    case (op)
      OP_WMEM:                       nm1 = 2'd3;
      OP_SETADDR, OP_WCSR, OP_SETPC: nm1 = 2'd2;
      OP_TPUSH:                      nm1 = 2'd1;
      default:                       nm1 = 2'd0;
    endcase
  end

  // --------------------------------------------------------------------------
  // command outputs.  Everything derived from `argw` is combinational: the
  // argument word is the write data, the address is the address pointer, and
  // the one-clk enables live in the issue phase (`iph`) one clk after the last
  // argument byte, when the whole word is latched.
  // --------------------------------------------------------------------------
  wire ack_irq = iph & (op == OP_ACKIRQ);

  always @(*) begin
    mem_we       = 1'b0;
    mem_addr     = addr_ptr;
    mem_wdata    = argw;
    csr_wr_en    = 1'b0;
    csr_wr_addr  = 8'h00;
    csr_wr_data  = 16'h0000;
    csr_rd_addr  = p1_v ? stat_addr : argw[7:0];
    rx_push      = 1'b0;
    rx_push_tid  = argw[1:0];
    rx_push_data = argw[15:8];
    tx_pop_tid   = argw[1:0];
    if (iph) begin
      case (op)
        OP_WMEM:   mem_we = 1'b1;
        OP_WCSR: begin
          csr_wr_en   = 1'b1;
          csr_wr_addr = argw[7:0];
          csr_wr_data = argw[23:8];
        end
        OP_SETPC: begin
          csr_wr_en   = 1'b1;
          csr_wr_addr = 8'h48 + {2'b00, argw[1:0], 4'b0000};
          csr_wr_data = argw[23:8];
        end
        OP_RUN: begin
          csr_wr_en   = 1'b1;
          csr_wr_addr = 8'h30;                     // host-only RUNMASK
          csr_wr_data = {12'h000, argw[3:0]};
        end
        OP_STOP: begin
          csr_wr_en   = 1'b1;
          csr_wr_addr = 8'h31;                     // host-only STOPMASK
          csr_wr_data = {12'h000, argw[3:0]};
        end
        OP_ACKIRQ: begin
          csr_wr_en   = 1'b1;
          csr_wr_addr = `CSR_IRQ;                  // 0x14: write 0 = clear
          csr_wr_data = 16'h0000;
        end
        OP_TPUSH: begin
          if (!rx_full[argw[1:0]])
            rx_push = 1'b1;
        end
        default: ;
      endcase
    end
  end

  // --------------------------------------------------------------------------
  // main sequential process (single clock domain, all state reset)
  // --------------------------------------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      sck_s          <= 3'b000;
      cs_s           <= 3'b111;
      mosi_s         <= 1'b0;
      bit_cnt        <= 3'd0;
      rx_shift       <= 8'h00;
      tx_shift       <= `HCMD_RESP_OK;   // first command transfer returns 0x80
      mem_re         <= 1'b0;
      csr_rd_en      <= 1'b0;
      tx_pop         <= 1'b0;
      st             <= ST_IDLE;
      op             <= OP_NONE;
      argi           <= 2'd0;
      argw           <= 32'h0000_0000;
      addr_ptr       <= 10'd0;
      iph            <= 1'b0;
      dead           <= 3'd0;
      wait_kind      <= W_MEM;
      resp_left      <= 2'd0;
      resp_is_status <= 1'b0;
      resp_sh        <= 24'h00_0000;
      stat_en        <= 1'b0;
      stat_i         <= 3'd0;
      p1_v           <= 1'b0;
      p1_i           <= 3'd0;
      statb[0]       <= 8'h00;
      statb[1]       <= 8'h00;
      statb[2]       <= 8'h00;
      statb[3]       <= 8'h00;
    end else begin
      // ----------------------------------------------------------------------
      // pulse defaults (command logic below overrides for one clk)
      // ----------------------------------------------------------------------
      mem_re    <= 1'b0;
      csr_rd_en <= 1'b0;
      tx_pop    <= 1'b0;

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
      // issue phase: the write enables were combinational last clk; close it
      // out and bump the address pointer after a WRITE_MEM
      // ----------------------------------------------------------------------
      if (iph) begin
        iph <= 1'b0;
        if (op == OP_WMEM)
          addr_ptr <= addr_ptr + 10'd1;
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
              tx_shift  <= mem_rdata[7:0];
              resp_sh   <= {8'h00, mem_rdata[31:8]};
              resp_left <= 2'd3;
              st        <= ST_RESP;
              addr_ptr  <= addr_ptr + 10'd1;
            end else if (dead >= 3'd4) begin      // stalled read -> 0
              tx_shift  <= 8'h00;
              resp_sh   <= 24'h00_0000;
              resp_left <= 2'd3;
              st        <= ST_RESP;
              addr_ptr  <= addr_ptr + 10'd1;
            end
          end
          W_CSR: begin
            if (dead >= 3'd1) begin               // combinational host read port
              tx_shift  <= csr_rd_data[7:0];
              resp_sh   <= {8'h00, csr_rd_data[15:8]};
              resp_left <= 2'd1;
              st        <= ST_RESP;
            end else if (dead >= 3'd4) begin
              tx_shift  <= 8'h00;
              resp_sh   <= 24'h00_0000;
              resp_left <= 2'd1;
              st        <= ST_RESP;
            end
          end
          default: begin                          // W_POP: data in the pop cycle
            tx_shift  <= tx_pop_data;
            resp_left <= 2'd0;
            st        <= ST_RESP;
          end
        endcase
      end

      // ----------------------------------------------------------------------
      // STATUS fetch: one CSR read per clk, captured 1 clk after it is issued
      // ----------------------------------------------------------------------
      if (stat_en) begin
        csr_rd_en <= 1'b1;
        p1_v      <= 1'b1;
        p1_i      <= stat_i;
        if (stat_i == 3'd7) begin
          stat_en <= 1'b0;                        // last read issued
        end else begin
          stat_i  <= stat_i + 3'd1;
        end
      end else begin
        p1_v <= 1'b0;
      end

      if (p1_v) begin
        if (p1_i[0] == 1'b0) begin
          // TSTATUS = {14'b0, halted, en}
          statb[p1_i[2:1]] <= {csr_rd_data[1:0], statb[p1_i[2:1]][5:0]};
        end else begin
          // TFIFO counts: [11:8] = rx_count, [7:4] = tx_count
          statb[p1_i[2:1]] <= {statb[p1_i[2:1]][7:6],
                             cap_tx_full,
                             tx_empty[p1_i[2:1]],
                             rx_full[p1_i[2:1]],
                             cap_rx_empty,
                             2'b00};
        end
        // thread 0's byte has no transfer boundary before it is needed
        if (resp_is_status && (p1_i[2:1] == 2'd0)) begin
          if (p1_i[0] == 1'b0)
            tx_shift <= {csr_rd_data[1:0], 6'b000000};   // bit7 = halted
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
            op       <= op_d;
            argi     <= 2'd0;
            tx_shift <= `HCMD_RESP_OK;             // free for the next command
            case (op_d)
              OP_RMEM: begin
                mem_re    <= 1'b1;
                dead      <= 3'd0;
                wait_kind <= W_MEM;
                st        <= ST_WAIT;
                tx_shift  <= 8'h00;                // filled when data arrives
              end
              OP_STATUS: begin
                stat_en   <= 1'b1;                 // issue TSTATUS thread 0 now
                stat_i    <= 3'd1;
                csr_rd_en <= 1'b1;
                p1_v      <= 1'b1;
                p1_i      <= 3'd0;
                resp_is_status <= 1'b1;
                resp_left <= 2'd3;
                st        <= ST_RESP;
                tx_shift  <= 8'h00;
              end
              OP_ACKIRQ: begin
                iph       <= 1'b1;                 // csr_wr 0x14 = 0 next clk
              end
              OP_PING: begin
                // 0x0D returns 0xA5 on the argument transfer itself
                st       <= ST_ARG;
                tx_shift <= 8'hA5;
              end
              OP_NONE: begin                       // unknown: ignore
                st       <= ST_IDLE;
              end
              default: begin                       // commands with arguments
                st       <= ST_ARG;
                tx_shift <= 8'h00;
              end
            endcase
          end

          // ---------------- argument bytes ---------------------------------
          ST_ARG: begin
            case (argi)
              2'd0:    argw[7:0]   <= rx_byte;
              2'd1:    argw[15:8]  <= rx_byte;
              2'd2:    argw[23:16] <= rx_byte;
              default: argw[31:24] <= rx_byte;
            endcase
            if (argi == nm1) begin
              // ---- last argument: execute (or arm the issue phase) --------
              argi     <= 2'd0;
              st       <= ST_IDLE;
              tx_shift <= `HCMD_RESP_OK;           // free for the next command
              case (op)
                OP_SETADDR: begin
                  addr_ptr <= {6'b0, argw[9:8], argw[7:0]};
                end
                OP_WMEM, OP_WCSR, OP_SETPC,
                OP_RUN, OP_STOP, OP_TPUSH: begin
                  iph <= 1'b1;                     // enable pulse next clk
                end
                OP_RCSR: begin
                  csr_rd_en <= 1'b1;
                  dead      <= 3'd0;
                  wait_kind <= W_CSR;
                  st        <= ST_WAIT;
                  tx_shift  <= 8'h00;
                end
                OP_RPOP: begin
                  if (!tx_empty[rx_byte[1:0]]) begin
                    tx_pop    <= 1'b1;
                    dead      <= 3'd0;
                    wait_kind <= W_POP;
                    st        <= ST_WAIT;
                    tx_shift  <= 8'h00;
                  end else begin                   // empty FIFO -> return 0x00
                    tx_shift  <= 8'h00;
                    resp_left <= 2'd0;
                    st        <= ST_RESP;
                  end
                end
                default: ;                         // PING: 0xA5 already sent
              endcase
            end else begin
              argi     <= argi + 2'd1;
              tx_shift <= 8'h00;                   // filler during arguments
            end
          end

          // ---------------- response bytes --------------------------------
          ST_RESP: begin
            if (resp_left != 2'd0) begin
              if (resp_is_status)
                tx_shift <= statb[stat_slot];
              else begin
                tx_shift <= resp_sh[7:0];
                resp_sh  <= {8'h00, resp_sh[23:8]};
              end
              resp_left <= resp_left - 2'd1;
            end else begin
              st             <= ST_IDLE;
              resp_is_status <= 1'b0;
              tx_shift       <= `HCMD_RESP_OK;
            end
          end

          // ST_WAIT: a byte arriving before the handshake completed is kept
          // framed as 0x00 and ignored (it is a payload transfer).
          default: ;
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
        op             <= OP_NONE;
        argi           <= 2'd0;
        argw           <= 32'h0000_0000;
        iph            <= 1'b0;
        resp_left      <= 2'd0;
        resp_sh        <= 24'h00_0000;
        resp_is_status <= 1'b0;
        dead           <= 3'd0;
        stat_en        <= 1'b0;
        p1_v           <= 1'b0;
        mem_re         <= 1'b0;
        csr_rd_en      <= 1'b0;
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

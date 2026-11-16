// ============================================================================
//  loom_capture.v - LOOM timestamped pin capture ("logic analyser" mode)
//  Owner: wavecapt.  Verilog-2005, synthesizable, one clock, async low reset.
// ============================================================================
//  Records a trigger-qualified, timestamped stream of pin snapshots into a
//  64-entry, 16-bit-wide buffer, for protocol record & replay (stretch goal).
//
//  ENTRY PACKING (16 bits, one DMEM word per entry):
//      cap_wdata[15:8] = pins_in[7:0]   pin snapshot (uio[7:0]) at the
//                                       trigger cycle
//      cap_wdata[7:0]  = tmr[7:0]       low byte of the free-running cycle
//                                       counter (CSR TMR) at the same cycle
//  The timestamp is an absolute low byte; the host reconstructs exact
//  spacing as long as consecutive entries are less than 256 cycles apart
//  (successive deltas are (tmr - prev_tmr) mod 256).  For longer gaps (or a
//  capture that starts mid-wrap) the host anchors the capture by reading the
//  TMR CSR before/after; tmr[7:0] repeats every 256 cycles and the full
//  16-bit TMR wraps every 65_536 cycles.
//  (To capture the other pin byte instead, the core can present a rearranged
//  pins_in vector; the snapshot byte is always pins_in[7:0].)
//
//  TRIGGER
//      match = ((pins_in & cfg_mask) == (cfg_value & cfg_mask))
//  Only the masked bits are compared (bits of cfg_value outside cfg_mask are
//  ignored).  cfg_mask = 0 matches every cycle.
//
//  RECORDING
//      fire = cap_active && match && (div_cnt >= cfg_div)
//  * one entry is written per fire, to the next buffer address;
//  * cfg_div: record every (cfg_div+1)-th MATCHING cycle (0 = every match).
//    div_cnt counts matching cycles and is cleared by every recorded entry;
//    the >= compare makes a cfg_div change while armed safe;
//  * the outputs cap_we/cap_addr/cap_wdata are REGISTERED: cap_we is a
//    1-cycle pulse and the DMEM write happens in the cycle after the trigger
//    cycle, carrying the trigger-cycle snapshot and timestamp.
//
//  BUFFER / STATUS
//  64 entries at addresses 0..63 (6-bit, wraps).  Recording stops after the
//  64th entry (no overwrite):
//      cap_count[5:0] : write pointer = number of entries recorded so far
//                       (0..63); when cap_full=1 it stays at 63 and all 64
//                       entries are valid  =>  N = cap_full ? 64 : cap_count
//      cap_full       : 1 once all 64 entries are written (until a clear/arm)
//      cap_active     : capture is armed and not yet full
//  => the core must reserve 64 x 16 = 1024 bits of DMEM (words 0..63) for the
//  capture buffer, or map this write port to a dedicated 64x16 memory.  The
//  DMEM of spec §5 is only 64 x 16 total, so the capture buffer and a wave
//  buffer cannot both live in it: an integration decision for the Lead.
//
//  CONTROL (level/pulse compatible - a 1-cycle pulse or a held CSR bit both
//  work; cfg_clear has priority)
//      cfg_en    : the RISING EDGE of cfg_en arms capture and restarts from
//                  entry 0 (clears pointer, full and the divider
//                  accumulator), so a 1-cycle pulse or a level-style
//                  CSR bit that goes 0->1 both arm exactly once; holding
//                  cfg_en high does not re-arm
//      cfg_clear : while high, capture is disarmed and the pointer/count/
//                  full/divider are held cleared (a 1-cycle pulse or a
//                  level-style CSR bit both work)
//  The trigger config is NOT latched: cfg_mask/cfg_value/cfg_div may change
//  while armed.
//
//  CSR HOOK-UP SUGGESTION (loom_bus.vh CAPCTRL 0x1A write, CAPSTAT 0x1B read)
//  The frozen CSR map gives only two 16-bit capture CSRs, but the module needs
//  mask[15:0], value[15:0], div[3:0], en and clear (38 config bits).  The core
//  should therefore keep a small config bank and use CAPCTRL writes as the
//  programming port, e.g.
//      CAPCTRL write  [13:12] = sel  00 mask[7:0], 01 mask[15:8],
//                                    10 value[7:0], 11 value[15:8]
//                     [9]     = cfg_en
//                     [8]     = cfg_clear
//                     [7:4]   = cfg_div
//                     [3:0]   = payload nibble for the selected byte
//      CAPSTAT  read  [9:4] = cap_count, [1] = cap_full, [0] = cap_active
//  (or the Lead may approve adding CAPMASK/CAPVAL CSRs to loom_bus.vh).
//
//  RESET
//    async active-low: disarmed, empty, all state defined.
// ============================================================================

`default_nettype none

module loom_capture (
  input  wire        clk,
  input  wire        rst_n,
  // configuration / control (from the core CSR block)
  input  wire        cfg_en,        // rising edge (pulse or level): arm
  input  wire        cfg_clear,     // level/pulse: disarm + clear status
  input  wire [15:0] cfg_mask,      // trigger mask
  input  wire [15:0] cfg_value,     // trigger expected value (masked compare)
  input  wire [3:0]  cfg_div,       // record every (cfg_div+1)-th match
  // sampled inputs
  input  wire [15:0] pins_in,       // protocol pin inputs (snapshot: [7:0])
  input  wire [15:0] tmr,           // free-running cycle counter (CSR TMR)
  // DMEM write port (registered: cap_we is a 1-cycle pulse)
  output reg         cap_we,
  output reg  [5:0]  cap_addr,
  output reg  [15:0] cap_wdata,
  // status
  output wire [5:0]  cap_count,
  output wire        cap_full,
  output wire        cap_active
);

  localparam [5:0] CAP_LAST = 6'd63;

  reg        active_q;
  reg        full_q;
  reg [5:0]  wr_q;        // next buffer address = number of entries recorded
  reg [3:0]  div_q;       // matching-cycle divider accumulator
  reg        en_q;        // cfg_en history: arm on a rising edge only

  assign cap_count  = wr_q;
  assign cap_full   = full_q;
  assign cap_active = active_q;

  // masked trigger compare
  wire match = ((pins_in & cfg_mask) == (cfg_value & cfg_mask));
  wire arm   = cfg_en && !en_q;               // rising edge arms (pulse or level)
  wire fire  = active_q && match && (div_q >= cfg_div);
  // snapshot of the trigger cycle (low pin byte + low timestamp byte)
  wire [15:0] sample = {pins_in[7:0], tmr[7:0]};

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      active_q <= 1'b0;
      full_q   <= 1'b0;
      wr_q     <= 6'd0;
      div_q    <= 4'd0;
      en_q     <= 1'b0;
      cap_we   <= 1'b0;
      cap_addr <= 6'd0;
      cap_wdata<= 16'd0;

    end else begin
      cap_we <= 1'b0;                 // default: single-cycle write pulse
      en_q   <= cfg_en;               // remembers the level for edge detect

      if (cfg_clear) begin            // disarm + clear (highest priority)
        active_q <= 1'b0;
        full_q   <= 1'b0;
        wr_q     <= 6'd0;
        div_q    <= 4'd0;
        cap_addr <= 6'd0;
        cap_wdata<= 16'd0;

      end else if (arm) begin         // rising edge of cfg_en: arm from entry 0
        active_q <= 1'b1;
        full_q   <= 1'b0;
        wr_q     <= 6'd0;
        div_q    <= 4'd0;
        cap_addr <= 6'd0;
        cap_wdata<= 16'd0;

      end else if (fire) begin
        cap_we    <= 1'b1;
        cap_addr  <= wr_q;
        cap_wdata <= sample;
        div_q     <= 4'd0;
        if (wr_q == CAP_LAST) begin
          full_q   <= 1'b1;           // 64th entry written: stop, no overwrite
          active_q <= 1'b0;
        end else begin
          wr_q <= wr_q + 6'd1;
        end

      end else if (active_q && match) begin
        div_q <= div_q + 4'd1;        // matching cycle, but not due to record
      end
    end
  end

endmodule

`default_nettype wire

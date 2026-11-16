// ============================================================================
//  loom_wave.v - LOOM generic fractional-period waveform / pattern player
//  Owner: wavecapt.  Verilog-2005, synthesizable, one clock, async low reset.
// ============================================================================
//  WHAT IT DOES
//    Streams a buffer of 16-bit pin patterns out of DMEM, one pattern per
//    symbol period.  The period is 12.4 unsigned fixed point
//    (cfg_period[15:4] = whole cycles, cfg_period[3:0] = 1/16 cycle) and is
//    generated with a 4-bit fractional carry accumulator, so the AVERAGE
//    symbol period is exact and drift-free over arbitrarily long runs
//    (no divider, no multiplier: adder + comparator + mux only).
//    cfg_period[15:4]==0 (a period below one cycle, i.e. cfg_period 0..15)
//    degenerates to one pattern per cycle; the fraction still accumulates
//    harmlessly.
//
//  TIMING (all times in clocks; "start cycle" = the cycle in which cfg_start
//  is sampled high - cfg_start must be a single-cycle pulse)
//    * cfg_start is latched at the end of the start cycle: busy rises,
//      done falls, cfg_addr/cfg_len/cfg_period/cfg_loop/cfg_oneshot latched.
//    * FIRST PATTERN: wave_valid=1 and wave_pins=DMEM[cfg_addr] in cycle
//          start + max(1, cfg_period[15:4])
//      i.e. one whole period after the start command; the fractional part of
//      the period is accumulated from the following symbols on.
//    * Following patterns are driven at the accumulated boundaries, one per
//      cfg_period cycles on average (carry accumulator, drift-free).
//    * ONE-SHOT / single pass: the last pattern is driven for its full
//      period, then busy=0, done=1, wave_valid=0, wave_pins=0.  done is a
//      level and stays set until the next cfg_start.
//    * LOOP: wraps to cfg_addr after the last pattern and continues forever
//      (busy stays 1, done stays 0).
//    * loop && !oneshot -> continuous; otherwise the buffer is played once
//      (loop=0 stops after one pass whether or not oneshot is set).
//    * cfg_start while busy: the new configuration is latched into a shadow
//      register file and made active at the next symbol boundary, so the
//      currently running symbol is never truncated, glitched or shortened.
//      If cfg_start lands exactly in the last cycle of a one-shot pass the
//      buffer wraps one extra symbol before the restart, so that a start
//      request is never lost and no symbol is cut short.
//    * wave_valid is 1 from the first pattern until the player stops; when
//      wave_valid=0 wave_pins is forced to 0 (the core merges wave_pins into
//      pins_out with a simple OR-free priority mux).
//
//  DMEM READ PORT
//    dmem_data is the combinational output of the 64x16 FF-array DMEM
//    (spec 5).  dmem_addr is a REGISTER: the pattern for the next symbol is
//    addressed one whole symbol ahead of the cycle that emits it, so the
//    address is stable for >=1 cycle before dmem_data is sampled, and
//    dmem_data is captured directly by the wave_pins register at the symbol
//    boundary (one DMEM mux + one flop: easy to close at 60 MHz).
//    While idle dmem_addr tracks cfg_addr, so the first pattern is already
//    on dmem_data in the start cycle.
//      INTEGRATION REQUIREMENT: cfg_addr (CSR WADDR) must be stable at least
//      one cycle before cfg_start is pulsed.  That is guaranteed by the host
//      protocol order (WADDR/WLEN/WPER are written before WCTRL.start).
//    Pattern addresses are (cfg_addr + k) mod 64, k = 0..cfg_len-1 (6-bit
//    wrap), so a buffer may straddle the top of DMEM.  cfg_len is clamped to
//    1..64 (0 -> 1).
//
//  CSR HOOK-UP (loom_bus.vh)
//    WCTRL.start -> cfg_start (1-cycle pulse),  WCTRL.loop -> cfg_loop,
//    WCTRL.oneshot -> cfg_oneshot, WADDR -> cfg_addr, WLEN -> cfg_len,
//    WPER -> cfg_period,  WSTATUS read = {14'b0, busy, done}.
//    NOTE: WCTRL.done_rst has no dedicated port in the frozen interface.
//    The core should implement done_rst by clearing the done bit in its own
//    WSTATUS read path (or the host can simply re-issue a start, which also
//    clears done).
//
//  RESET
//    async active-low: idle, busy=0, done=0, wave_valid=0, wave_pins=0,
//    dmem_addr=0, every register defined (no X after reset).
// ============================================================================

`default_nettype none

module loom_wave (
  input  wire        clk,
  input  wire        rst_n,
  // configuration (from the core CSR block)
  input  wire        cfg_start,     // 1-cycle pulse
  input  wire        cfg_loop,
  input  wire        cfg_oneshot,
  input  wire [5:0]  cfg_addr,      // DMEM start word
  input  wire [6:0]  cfg_len,       // 1..64 patterns
  input  wire [15:0] cfg_period,    // 12.4 fixed point
  output wire        busy,
  output wire        done,          // level until cfg_start
  // DMEM read port (combinational read: dmem_data valid in the same cycle)
  output reg  [5:0]  dmem_addr,
  input  wire [15:0] dmem_data,
  // pins
  output reg  [15:0] wave_pins,
  output reg         wave_valid
);

  // --------------------------------------------------------------------------
  // configuration currently in effect
  // --------------------------------------------------------------------------
  reg        loop_q;
  reg        oneshot_q;
  reg [5:0]  base_q;        // buffer start word
  reg [6:0]  len_q;         // 1..64
  reg [11:0] pint_q;        // whole cycles per symbol
  reg [3:0]  pfrac_q;       // 1/16 cycle fraction per symbol

  // pending-restart shadow copy (cfg_start while busy)
  reg        pend_q;
  reg        s_loop_q;
  reg        s_oneshot_q;
  reg [5:0]  s_base_q;
  reg [6:0]  s_len_q;
  reg [11:0] s_pint_q;
  reg [3:0]  s_pfrac_q;

  // --------------------------------------------------------------------------
  // runtime state
  // --------------------------------------------------------------------------
  reg        busy_q;
  reg        done_q;
  reg        started_q;     // 0 until the first pattern of this run is on the pins
  reg [6:0]  idx_q;         // index of the pattern currently driven on wave_pins
  reg [6:0]  off_q;         // offset of the pattern currently addressed on dmem_addr
  reg [12:0] cnt_q;         // cycles-1 until the next symbol boundary
  reg [3:0]  facc_q;        // fractional-cycle carry accumulator (1/16 cycle)

  assign busy = busy_q;
  assign done = done_q;

  // --------------------------------------------------------------------------
  // configuration decode / helpers
  // --------------------------------------------------------------------------
  // cfg_len is documented as 1..64; clamp defensively (0 -> 1, >64 -> 64)
  wire [6:0]  len_new   = (cfg_len == 7'd0) ? 7'd1 :
                          ((cfg_len > 7'd64) ? 7'd64 : cfg_len);
  wire [11:0] pint_new  = cfg_period[15:4];
  wire [3:0]  pfrac_new = cfg_period[3:0];

  // First pattern appears max(1, pint) cycles after the start cycle.  For
  // pint <= 1 that boundary is the start cycle itself, so the first pattern
  // is emitted immediately; otherwise the first boundary is a normal tick.
  wire        first_now = (pint_new <= 12'd1);

  // offset of the pattern to address after a fresh first pattern is emitted
  wire [6:0]  off0_new  = (len_new == 7'd1) ? 7'd0 : 7'd1;
  wire [6:0]  off0_sh   = (s_len_q == 7'd1) ? 7'd0 : 7'd1;
  wire [6:0]  off0_cur  = (len_q  == 7'd1) ? 7'd0 : 7'd1;

  // interval from the first pattern to the second = pint (carry from a zero
  // accumulator is always 0 because pfrac < 16), clamped to >=1 cycle
  wire [12:0] int1_new  = (pint_new == 12'd0) ? 13'd0 : {1'b0, pint_new} - 13'd1;
  wire [12:0] int1_sh   = (s_pint_q == 12'd0) ? 13'd0 : {1'b0, s_pint_q} - 13'd1;
  wire [12:0] int1_cur  = (pint_q  == 12'd0) ? 13'd0 : {1'b0, pint_q}  - 13'd1;

  // cycles-1 to wait before the first pattern when pint >= 2
  // (the tick test happens one cycle after this value is loaded)
  wire [12:0] intw_new  = {1'b0, pint_new} - 13'd2;

  // normal symbol advance: wrap offset at len
  wire [6:0]  off_inc   = off_q + 7'd1;
  wire [6:0]  off_next  = (off_inc == len_q) ? 7'd0 : off_inc;

  // fractional carry accumulation for the next symbol interval
  wire [4:0]  facc_sum  = {1'b0, facc_q} + {1'b0, pfrac_q};
  wire [12:0] dur_add   = {1'b0, pint_q} + {12'd0, facc_sum[4]};
  wire [12:0] int_next  = (dur_add == 13'd0) ? 13'd0 : (dur_add - 13'd1);

  wire        run_loop  = loop_q && !oneshot_q;
  wire        tick      = busy_q && (cnt_q == 13'd0);

  // --------------------------------------------------------------------------
  // player
  // --------------------------------------------------------------------------
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      loop_q   <= 1'b0;
      oneshot_q<= 1'b0;
      base_q   <= 6'd0;
      len_q    <= 7'd1;
      pint_q   <= 12'd0;
      pfrac_q  <= 4'd0;
      pend_q   <= 1'b0;
      s_loop_q <= 1'b0;
      s_oneshot_q <= 1'b0;
      s_base_q <= 6'd0;
      s_len_q  <= 7'd1;
      s_pint_q <= 12'd0;
      s_pfrac_q<= 4'd0;
      busy_q   <= 1'b0;
      done_q   <= 1'b0;
      started_q<= 1'b0;
      idx_q    <= 7'd0;
      off_q    <= 7'd0;
      cnt_q    <= 13'd0;
      facc_q   <= 4'd0;
      dmem_addr<= 6'd0;
      wave_pins<= 16'd0;
      wave_valid<= 1'b0;

    // ---------------------------------------------------------------- start
    end else if (cfg_start && !busy_q) begin
      base_q    <= cfg_addr;
      len_q     <= len_new;
      pint_q    <= pint_new;
      pfrac_q   <= pfrac_new;
      loop_q    <= cfg_loop;
      oneshot_q <= cfg_oneshot;
      busy_q    <= 1'b1;
      done_q    <= 1'b0;
      pend_q    <= 1'b0;
      idx_q     <= 7'd0;
      if (first_now) begin
        // first boundary is the start cycle: drive pattern 0 now
        wave_pins  <= dmem_data;          // DMEM[cfg_addr], tracked while idle
        wave_valid <= 1'b1;
        started_q  <= 1'b1;
        off_q      <= off0_new;
        dmem_addr  <= cfg_addr + off0_new[5:0];
        facc_q     <= pfrac_new;
        cnt_q      <= int1_new;
      end else begin
        // wait max(1,pint) = pint cycles for the first pattern
        wave_pins  <= 16'd0;
        wave_valid <= 1'b0;
        started_q  <= 1'b0;
        off_q      <= 7'd0;
        dmem_addr  <= cfg_addr;
        facc_q     <= 4'd0;
        cnt_q      <= intw_new;
      end

    // ---------------------------------------------------------------- running
    end else if (busy_q) begin
      // request a clean restart at the next symbol boundary
      if (cfg_start) begin
        s_base_q   <= cfg_addr;
        s_len_q    <= len_new;
        s_pint_q   <= pint_new;
        s_pfrac_q  <= pfrac_new;
        s_loop_q   <= cfg_loop;
        s_oneshot_q<= cfg_oneshot;
        pend_q     <= 1'b1;
      end

      if (tick) begin
        if (pend_q) begin
          // ---- restart takes effect here (new pattern 0 is on dmem_data) ----
          base_q    <= s_base_q;
          len_q     <= s_len_q;
          pint_q    <= s_pint_q;
          pfrac_q   <= s_pfrac_q;
          loop_q    <= s_loop_q;
          oneshot_q <= s_oneshot_q;
          pend_q    <= cfg_start;     // another request on this very cycle
          idx_q     <= 7'd0;
          off_q     <= off0_sh;
          dmem_addr <= s_base_q + off0_sh[5:0];
          wave_pins <= dmem_data;
          wave_valid<= 1'b1;
          started_q <= 1'b1;
          facc_q    <= s_pfrac_q;
          cnt_q     <= int1_sh;
          done_q    <= 1'b0;
        end else if (!started_q) begin
          // ---- first pattern of the current configuration ----
          idx_q     <= 7'd0;
          off_q     <= off0_cur;
          dmem_addr <= base_q + off0_cur[5:0];
          wave_pins <= dmem_data;
          wave_valid<= 1'b1;
          started_q <= 1'b1;
          facc_q    <= pfrac_q;
          cnt_q     <= int1_cur;
        end else if ((!run_loop) && (idx_q == len_q - 7'd1) && !cfg_start) begin
          // ---- one pass complete: release the pins ----
          busy_q    <= 1'b0;
          done_q    <= 1'b1;
          started_q <= 1'b0;
          wave_pins <= 16'd0;
          wave_valid<= 1'b0;
          idx_q     <= 7'd0;
          off_q     <= 7'd0;
          cnt_q     <= 13'd0;
          facc_q    <= 4'd0;
          dmem_addr <= cfg_addr;
        end else begin
          // ---- next pattern, same configuration ----
          idx_q     <= idx_q + 7'd1;
          off_q     <= off_next;
          dmem_addr <= base_q + off_next[5:0];
          wave_pins <= dmem_data;
          wave_valid<= 1'b1;
          facc_q    <= facc_sum[3:0];
          cnt_q     <= int_next;
        end
      end else begin
        cnt_q <= cnt_q - 13'd1;
      end

      // A restart request also re-points the DMEM read port at the new
      // pattern 0 from the next cycle on (takes priority over the address
      // written by the tick branch above).
      if (cfg_start) dmem_addr <= cfg_addr;

    // ---------------------------------------------------------------- idle
    end else begin
      dmem_addr <= cfg_addr;      // keep pattern 0 of the next run ready
      off_q     <= 7'd0;
      idx_q     <= 7'd0;
      cnt_q     <= 13'd0;
      facc_q    <= 4'd0;
      pend_q    <= 1'b0;
      started_q <= 1'b0;
      wave_pins <= 16'd0;
      wave_valid<= 1'b0;
      // done_q holds its level until the next cfg_start
    end
  end

endmodule

`default_nettype wire

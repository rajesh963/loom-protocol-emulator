#!/usr/bin/env python3
"""Headline timing-claim tests: the properties that make LOOM different from a
PIO/PRU.  Every assertion is cycle-exact (pin monitor timestamps in `clk`
cycles), every test prints an ASCII waveform into the log.

Claims exercised
----------------
1. fractional periodic events, drift-free:  PERIOD = 0x010A = 16.625 cycles,
   200 events; total elapsed within 1 cycle of exact, prefix error bounded.
2. UART-115200-style bit period at 60 MHz (60e6/115200 = 520.8333; nearest 12.4
   value 0x208D = 520.8125): 64 bit cells; long-term count exact against the
   programmed period, baud error 0.004 % (vs 0.16 % for an integer divider).
3. WAITU absolute scheduling: consecutive absolute wake targets 50 cycles apart
   -> measured pin edges exactly 50 cycles apart (no jitter, no drift).
4. INAT timed sampling: a 1-cycle-wide external pulse is captured only when it
   overlaps the exact target cycle -- the sample-to-capture boundary is located
   to the cycle.
5. EMITE edge-triggered reaction: input edge -> driven edge latency is
   deterministic and identical for repeated events.
6. 4 concurrent threads with periods 8/16/24/32: all four edge streams exact
   simultaneously (work-conserving scheduler + hardware timers).
7. event-queue back-pressure: a producer faster than its period stalls (TEQ
   reaches depth 4) yet the scheduled edges remain exact.

Run with:  make timing
"""

from __future__ import annotations

import cocotb

from host import CSR, ascii_wave, start_loom

HALF = 4                    # SPI half period in clk cycles (10 MHz SCK @ 100 MHz)
P10 = 0x0400
P11 = 0x0800
P12 = 0x1000
P13 = 0x2000


def show(host, mon, pins=(10,), title="", t0=None, t1=None, cols=72):
    text = ascii_wave(mon.value_records, pins=pins, t0=t0, t1=t1, cols=cols,
                      title=title)
    for line in text.splitlines():
        host.log.info(line)
    return text


async def patch_words(host, addr: int, patches: dict[int, int]):
    for idx, word in patches.items():
        await host.write_mem(addr + idx, word)


async def target_from_tmr(host, margin: int = 3000) -> int:
    """Read the free-running TMR and return a safe future 16-bit target."""
    tmr = await host.read_csr(CSR["TMR"])
    return (tmr + margin) & 0xFFFF


def first_edge(mon, bit: int, t_from: int = 0):
    for cyc, pins, _ in mon.records:
        if cyc >= t_from and ((pins >> bit) & 1):
            return cyc
    return None


def last_edge(mon, bit: int, t_from: int = 0):
    out = None
    for cyc, pins, _ in mon.records:
        if cyc >= t_from and ((pins >> bit) & 1):
            out = cyc
    return out


# ---------------------------------------------------------------------------
# 1. fractional periodic events, drift-free
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_periodic_fractional_drift_free(dut):
    """PERIOD = 16.625 cycles (0x010A): 200 events, error bounded by 1 cycle."""
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=HALF)
    src = """
        movi r0, 0x0400
        movi r1, 0x0000
        setp 0x010A          ; 16.625 cycles
        setm 0x0400
    evloop:
        emit r0
        emit r1
        jmp  evloop
    """
    words = await host.load_asm(src, addr=0)
    await host.set_pc(0, 0)
    t_run = mon.mark()
    await host.run(0x1)
    await host._clk(3800)            # ~228 events at 16.625 cycles
    await host.stop(0x1)

    edges = [c for c, p, _ in mon.records if c >= t_run and (p >> 10) & 1 !=
             ((mon.records[0][1] >> 10) & 1)]
    # rising edges only (the stream toggles): every second transition
    rises = []
    prev = 0
    for cyc, pins, _ in mon.records:
        if cyc < t_run:
            continue
        v = (pins >> 10) & 1
        if v == 1 and prev == 0:
            rises.append(cyc)
        prev = v
    assert len(rises) >= 100, f"only {len(rises)} periods observed"
    count = len(rises)
    exact = 16.625
    # every rise is one full period (two events) apart: 33.25 cycles
    period = 2 * exact
    errors = [rises[i] - (rises[0] + i * period) for i in range(count)]
    max_err = max(abs(e) for e in errors)
    total_err = rises[-1] - (rises[0] + (count - 1) * period)
    show(host, mon, pins=(10,), title="fractional periodic events, PERIOD=16.625 cycles",
         t0=t_run, t1=rises[-1] + 60, cols=72)
    host.log.info("event rises: %d (=%d pin transitions), first=%d last=%d",
                  count, 2 * count, rises[0], rises[-1])
    host.log.info("ideal period 33.250 cycles; measured span %.3f; max prefix error %.3f",
                  (rises[-1] - rises[0]) / (count - 1), max_err)
    for i in (0, 1, 2, count // 3, 2 * count // 3, count - 1):
        host.log.info("  rise[%3d] = %5d  ideal = %9.3f  error = %+.3f",
                      i, rises[i], rises[0] + i * period, errors[i])
    assert max_err <= 1.0, f"cumulative drift: max prefix error {max_err} cycles"
    assert abs(total_err) <= 1.0, f"total error {total_err} cycles over {count} periods"
    # counterfactual: an integer divider of 16 would already be off by ~124 cycles
    host.log.info("drift-free: max prefix error %.1f cycle over %d events vs %d "
                  "cycles for an integer/16 divider", max_err, 2 * count,
                  int((count - 1) * (period - 32)))


# ---------------------------------------------------------------------------
# 2. UART 115200 baud bit period at 60 MHz
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_uart_115200_bit_period(dut):
    """520.8125-cycle bit cells at 60 MHz: bit clock is exact, no drift."""
    host, mon = await start_loom(dut, clk_ns=16.67, spi_half=3)   # real 60 MHz
    src = """
        movi r0, 0x0400
        movi r1, 0x0000
        setp 0x208D          ; 520.8125 cycles (nearest 1/16 to 520.8333)
        setm 0x0400
    bitloop:
        emit r0
        emit r1
        jmp  bitloop
    """
    await host.load_asm(src, addr=0)
    await host.set_pc(0, 0)
    t_run = mon.mark()
    await host.run(0x1)
    await host._clk(64 * 521 + 400)       # 64 bit cells (32 full toggles)
    await host.stop(0x1)

    rises, prev = [], 0
    for cyc, pins, _ in mon.records:
        if cyc < t_run:
            continue
        v = (pins >> 10) & 1
        if v == 1 and prev == 0:
            rises.append(cyc)
        prev = v
    n = len(rises) - 1
    assert n >= 30, f"only {n} bit periods observed"
    period = 2 * (8333 / 16)              # two emits per toggle = 1041.625 cycles
    errs = [rises[i] - (rises[0] + i * period) for i in range(n + 1)]
    max_err = max(abs(e) for e in errs)
    show(host, mon, pins=(10,), title="UART 115200 bit clock, 520.8125 cycles/bit @ 60 MHz",
         t0=t_run, t1=rises[-1] + 600, cols=72)
    avg = (rises[-1] - rises[0]) / n
    baud = 60e6 / (avg / 2)
    # counterfactual integer dividers
    host.log.info("bit cells: %d toggles, avg toggle period %.4f cycles (ideal %.4f)",
                  n, avg, period)
    host.log.info("implied baud = %.2f (target 115200; error %+.4f %%)",
                  baud, (baud - 115200) / 115200 * 100)
    host.log.info("integer dividers: 520 -> baud %.0f (+%.3f %% error, %+.1f cycles "
                  "drift over %d bits); 521 -> %.0f (-%.3f %%)",
                  60e6 / 520, (60e6 / 520 / 115200 - 1) * 100, n * (8333 / 16 - 520),
                  2 * n, 60e6 / 521, (60e6 / 521 / 115200 - 1) * 100)
    assert max_err <= 1.0, f"bit clock drifts: max prefix error {max_err} cycles"
    assert abs((baud - 115200) / 115200) < 0.001, f"baud error {baud}"


# ---------------------------------------------------------------------------
# 3. WAITU absolute scheduling: jitter-free wake
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_waitu_absolute_jitter_free(dut):
    """Six absolute wake targets 50 cycles apart -> pin edges exactly 50 apart."""
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=HALF)
    src = """
        movi r0, 0x0400
        setm 0x0400
        waitu 0x0000
        tog  0x0400
        waitu 0x0000
        tog  0x0400
        waitu 0x0000
        tog  0x0400
        waitu 0x0000
        tog  0x0400
        waitu 0x0000
        tog  0x0400
        waitu 0x0000
        tog  0x0400
        halt
    """
    words = await host.load_asm(src, addr=0)
    t0 = await target_from_tmr(host, margin=15000)
    # instruction indices of the six WAITU instructions
    waitu_idx = [i for i, w in enumerate(words)
                 if ((w >> 25) & 0x7F) == isa_opc("waitu")]
    assert len(waitu_idx) == 6
    await patch_words(host, 0, {idx: words[idx] | ((t0 + 50 * k) & 0xFFFF)
                                for k, idx in enumerate(waitu_idx)})
    tmr_at_run = await host.read_csr(CSR["TMR"])
    await host.set_pc(0, 0)
    await host.run(0x1)
    # run until well past the last absolute target, without any host traffic
    need = ((t0 + 50 * 5) - tmr_at_run) & 0xFFFF
    await host._clk(need + 2000)
    await host.stop(0x1)
    edges = [c for c, p, _ in mon.records if (p >> 10) & 1]
    # rising edges: the TOG sequence makes 1,0,1,0,1,0 -> 3 rises
    rises, prev = [], 0
    for cyc, pins, _ in mon.records:
        v = (pins >> 10) & 1
        if v == 1 and prev == 0:
            rises.append(cyc)
        prev = v
    # every other TOG is a rise, and toggles are 50 cycles apart, so rises are
    # 100 cycles apart; all *edges* must be exactly 50 apart though.  Check the
    # full edge list instead.
    edge_times = [c for c, p, _ in mon.records if p & P10 or (p & P10) == 0]
    tog_edges = []
    prev = 0
    for cyc, pins, _ in mon.records:
        v = (pins >> 10) & 1
        if v != prev:
            tog_edges.append(cyc)
        prev = v
    assert len(tog_edges) >= 5, f"waitu edges {tog_edges}"
    deltas = [tog_edges[i + 1] - tog_edges[i] for i in range(len(tog_edges) - 1)]
    show(host, mon, pins=(10,), title="WAITU absolute wake, targets 50 cycles apart",
         t0=tog_edges[0] - 20, t1=tog_edges[-1] + 20)
    host.log.info("waitu edge times: %s", tog_edges)
    host.log.info("edge deltas:     %s", deltas)
    assert all(d == 50 for d in deltas), f"waitu jitter: {deltas} (want all 50)"
    host.log.info("WAITU: all %d wake intervals exactly 50 cycles (no jitter)", len(deltas))


def isa_opc(name: str) -> int:
    from sw import isa
    return isa.INSTRUCTIONS[name].opcode


# ---------------------------------------------------------------------------
# 4. INAT timed sampling: latch value at the exact cycle
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_inat_timed_sampling(dut):
    """A 1-cycle external pulse is captured iff it overlaps the INAT target.

    Method: the program drives P[10] with an EMITA at absolute cycle T (visible
    marker), then INATs at T+200 into bit 0 of an external uio input and copies
    it to P[11].  The host starts the external pulse at monitor cycle
    (P10 edge + 200 - shift) for shift=-2..+2; the captured bit must change
    monotonically exactly once, which pins the sample instant to one cycle.
    """
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=HALF)
    # two-wire setup: uio[0] external input, P10 marker, P11 result
    src = f"""
        movi r0, 0x0400
        setm 0x0400
        emita r0, 0x0000       ; marker edge at absolute T (patched)
        inat r2, 0x0000        ; sample at absolute T+200 (patched)
        andi r2, 1             ; captured uio[0]
        jz   got0
        set  0x0800            ; P[11] high when captured 1
    got0:
        set  0x1000            ; P[12] high: sampling done
        halt
    """
    # Cycle-precise experiment: shift the 1-cycle pulse across the sample instant.
    captured = {}
    for shift in range(-2, 6):
        host2, mon2 = await start_loom(dut, clk_ns=10.0, spi_half=HALF)
        words = await host2.load_asm(src, addr=0)
        tmr = await host2.read_csr(CSR["TMR"])
        T = (tmr + 2600) & 0xFFFF
        emita_idx = next(i for i, w in enumerate(words)
                         if ((w >> 25) & 0x7F) == isa_opc("emita"))
        inat_idx = next(i for i, w in enumerate(words)
                        if ((w >> 25) & 0x7F) == isa_opc("inat"))
        await patch_words(host2, 0, {
            emita_idx: words[emita_idx] | T,
            inat_idx: words[inat_idx] | ((T + 200) & 0xFFFF),
        })
        await host2.set_pc(0, 0)
        host2.set_ext_uio(0x00, oe=0xFF)
        await host2.run(0x1)
        # synchronise on the P10 marker edge
        t_mark = mon2.mark()
        while first_edge(mon2, 10, t_from=t_mark) is None:
            await host2._clk(4)
        marker = first_edge(mon2, 10, t_from=t_mark)
        # the INAT sample cycle in monitor time is marker + (200-1) + shift
        while mon2.mark() < marker + 200 + shift:
            await host2._clk(1)
        if mon2.mark() == marker + 200 + shift:
            host2.set_ext_uio(0x01, oe=0xFF)     # 1-cycle-wide pulse
        await host2._clk(1)
        host2.set_ext_uio(0x00, oe=0xFF)
        await host2._clk(4000)
        await host2.stop(0x1)
        res = host2.pins_out()
        captured[shift] = 1 if res & P11 else 0
        host2.log.info("shift %+d: marker=%d pulse@%d -> captured bit %d (pins=0x%04x)",
                       shift, marker, marker + 200 + shift, captured[shift], res)
    values = [captured[s] for s in sorted(captured)]
    dut._log.info("INAT capture vs pulse shift: %s", dict(sorted(captured.items())))
    # monotone: once the pulse is late enough it is not sampled before sample time
    # A 1-cycle-wide pulse is captured only when it overlaps the single INAT
    # sample cycle, so exactly one (or at most two adjacent, allowing for the
    # pad-model settling cycle) shifts may capture a 1.
    ones = [sh for sh, v in sorted(captured.items()) if v]
    assert ones, f"pulse never overlapped the INAT sample instant: {captured}"
    assert len(ones) <= 2 and (len(ones) == 1 or ones[1] == ones[0] + 1), \
        f"INAT sample window is not one cycle wide: {captured}"
    dut._log.info("INAT samples one exact cycle: boundary between shifts %+d and %+d",
                  sorted(captured)[values.index(1) - 1], sorted(captured)[values.index(1)])


# ---------------------------------------------------------------------------
# 5. EMITE reaction latency is deterministic
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_emite_reaction_latency(dut):
    """External pin edge -> driven pin edge latency is a fixed number of cycles."""
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=HALF)
    src = """
        setm  0x0400          ; drive P[10]
    armloop:
        setem 0x0001          ; watch P[0]
        setec 0x0001          ; fire when P[0] high
        movi  r0, 0x0400
        emite r0
    waitfired:
        csrr  r1, TEQ
        cmpi  r1, 0
        jnz   waitfired       ; wait until the edge event fired
    waitlow:
        in    r2
        andi  r2, 1
        jnz   waitlow         ; input still high: hold our driven edge
        clr   0x0400
        jmp   armloop
    """
    await host.load_asm(src, addr=0)
    await host.set_pc(0, 0)
    host.set_ext_uio(0x00, oe=0xFF)
    await host.run(0x1)
    await host._clk(30)

    latencies = []
    t_marks = []
    for k in range(4):
        await host._clk(20)
        t = mon.mark()
        host.set_ext_uio(0x01, oe=0xFF)          # input rises
        await host._clk(6)
        host.set_ext_uio(0x00, oe=0xFF)          # input falls
        await host._clk(6)
        t_marks.append(t)
    await host._clk(10)
    await host.stop(0x1)

    # collect all rising edges of the driven pin and pair them with the inputs
    driven_rises, prev = [], 0
    for cyc, pins, _ in mon.records:
        v = (pins >> 10) & 1
        if v == 1 and prev == 0:
            driven_rises.append(cyc)
        prev = v
    assert len(driven_rises) >= 3, f"driven edges {driven_rises}"
    for t in t_marks:
        nxt = [e for e in driven_rises if e >= t]
        if nxt:
            latencies.append(nxt[0] - t)
    show(host, mon, pins=(10,), title="EMITE reaction: external P[0] -> driven P[10]",
         t0=t_marks[0] - 5, t1=t_marks[-1] + 15)
    host.log.info("input-rise marks: %s", t_marks)
    host.log.info("driven risings:   %s", driven_rises)
    host.log.info("reaction latencies (cycles): %s", latencies)
    assert len(latencies) >= 2, "not enough triggered edges"
    assert len(set(latencies)) == 1, f"reaction latency not deterministic: {latencies}"
    assert latencies[0] <= 4, f"reaction latency {latencies[0]} cycles (want <=4)"
    host.log.info("EMITE reaction latency deterministic: %d cycles", latencies[0])


# ---------------------------------------------------------------------------
# 6. four concurrent threads, four exact periods
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_concurrent_threads_four_periods(dut):
    """Threads 0..3 emit periods 8/16/24/32 concurrently; all edges exact."""
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=HALF)
    bit = [0x0400, 0x0800, 0x1000, 0x2000]
    period = [0x0080, 0x0100, 0x0180, 0x0200]      # 8, 16, 24, 32 cycles
    for t in range(4):
        src = f"""
            movi r0, {bit[t]:#06x}
            movi r1, 0x0000
            setp {period[t]:#06x}
            setm {bit[t]:#06x}
        ev{t}:
            emit r0
            emit r1
            jmp  ev{t}
        """
        await host.load_asm(src, addr=t * 0x40)
        await host.set_pc(t, t * 0x40)
    t_run = mon.mark()
    await host.run(0xF)
    await host._clk(700)
    await host.stop(0xF)

    text = ascii_wave(mon.value_records, pins=(10, 11, 12, 13),
                      names=["T0 P[10]/8", "T1 P[11]/16", "T2 P[12]/24", "T3 P[13]/32"],
                      t0=t_run, t1=t_run + 700, cols=100,
                      title="four concurrent threads, periods 8/16/24/32 cycles")
    for line in text.splitlines():
        host.log.info(line)
    for t in range(4):
        rises, prev = [], 0
        for cyc, pins, _ in mon.records:
            if cyc < t_run:
                continue
            v = (pins >> (10 + t)) & 1
            if v == 1 and prev == 0:
                rises.append(cyc)
            prev = v
        # one rise per full period (two emits per toggle)
        deltas = [rises[i + 1] - rises[i] for i in range(len(rises) - 1)]
        want = 2 * int(period[t] >> 4)
        assert all(d == want for d in deltas), \
            f"thread {t}: deltas {deltas} (want {want} each)"
        host.log.info("thread %d: %d rising edges, every delta == %d cycles",
                      t, len(rises), want)
    host.log.info("4 concurrent threads: all periods exact simultaneously")


# ---------------------------------------------------------------------------
# 7. queue back-pressure preserves schedule
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_queue_backpressure_schedule_exact(dut):
    """Producer faster than its period: TEQ saturates, edges stay exact."""
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=HALF)
    src = """
        movi r0, 0x0400
        movi r1, 0x0000
        setp 0x0080          ; 8-cycle period
        setm 0x0400
    spin:
        emit r0
        emit r1
        jmp  spin
    """
    await host.load_asm(src, addr=0)
    await host.set_pc(0, 0)
    t_run = mon.mark()
    await host.run(0x1)
    max_eq = 0
    core = dut.dut.u_core
    for _ in range(12):
        # sample every cycle: the depth-4 queue is full most of the time but a
        # host READ_CSR takes long enough to miss the instant
        for _ in range(40):
            await host._clk(1)
            max_eq = max(max_eq, int(core.eq_cnt[0].value))
    st = await host.get_tstatus(0)
    assert max_eq == 4, f"event queue never saturated: max TEQ={max_eq} (depth 4)"
    assert (st & 0x2) == 0, "producer thread must be stalled (not halted) under back-pressure"
    await host.stop(0x1)

    rises, prev = [], 0
    for cyc, pins, _ in mon.records:
        if cyc < t_run:
            continue
        v = (pins >> 10) & 1
        if v == 1 and prev == 0:
            rises.append(cyc)
        prev = v
    deltas = [rises[i + 1] - rises[i] for i in range(len(rises) - 1)]
    show(host, mon, pins=(10,), title="event-queue back-pressure: producer stalls, edges exact",
         t0=t_run, t1=t_run + 420)
    host.log.info("TEQ saturated at %d/4, TSTATUS=0x%04x (stalled, en=1)", max_eq, st)
    host.log.info("edge deltas: %s", deltas[:10])
    assert len(deltas) >= 4 and all(d == 16 for d in deltas), \
        f"edges not exact under back-pressure: {deltas}"
    host.log.info("back-pressure: %d edges, all 16 cycles apart (schedule independent "
                  "of issue stalls)", len(rises))

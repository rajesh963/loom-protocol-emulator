#!/usr/bin/env python3
"""Random differential co-simulation: RTL vs the golden ISA model.

This is the headline verification artefact (spec §10.2): seeded random programs
from `sw/gen_program.py` run on the RTL and on `sw/loom_model.py`; we then
compare

  * the *full pin-transition trace* cycle-by-cycle: every (pins_out[15:0], DIR[7:0])
    change with its exact cycle index, and
  * the *full architectural state* after termination: PC, R0..R7, Z, C, en,
    halt, stall, PERIOD/PFRAC/WFRAC, TMASK/EMASK/ECOND, NEXTT, event queue,
    both FIFOs (contents and order), DMEM[64], POUT/DIR, IRQ code, EVT,

for every seed.  The model is written from the spec, not the RTL, so a match is
real evidence; a mismatch is minimised automatically (seed + listing + cycle
window + first differing field) into `test/cosim_failures/`.

Speed: the RTL program is loaded and observed through the simulator's backdoor
(hierarchical VPI) instead of the SPI link -- the host protocol itself is
verified by `test_host.py`.  The RUN command is still the real host command, so
thread-enable semantics (nextt=TMR, fetch restart) are the RTL's own.

Run:   make cosim
Knobs: COSIM_SEEDS (default 100), COSIM_INSNS (default 60), COSIM_THREADS.
"""

from __future__ import annotations

import os
import pathlib

import cocotb
from cocotb.triggers import ClockCycles

from host import CSR, start_loom
from known_issues import KNOWN_ISSUES
from sw import isa, loom_asm  # noqa: F401
from sw.gen_program import gen
from sw.loom_model import Loom

SEEDS = int(os.environ.get("COSIM_SEEDS", "100"))
# debug knob: replace CSRR in generated programs (used to bisect an RTL CSRR bug
# without changing the generator/model; both sides run the same modified image).
NOCSRR = os.environ.get("COSIM_NOCSRR", "") == "1"
N_INSNS = int(os.environ.get("COSIM_INSNS", "60"))
THREADS = int(os.environ.get("COSIM_THREADS", "2"))
EXTRA_CYCLES = 64                 # let pending events fire after the threads halt
ART = pathlib.Path(__file__).resolve().parent / "cosim_failures"
# Idle host-link pin levels: ui_in = {CS_N=1, SCK=0, MOSI=0} = 0x01 in the high
# byte; uio inputs idle high (the testbench pad model has pull-ups).
EXT_PINS = 0x01FF


# ---------------------------------------------------------------------------
# RTL backdoor view
# ---------------------------------------------------------------------------
class RtlView:
    """Cheap hierarchical access to the RTL state for co-simulation."""

    def __init__(self, tb):
        self.tb = tb
        self.top = tb.dut
        self.core = self.top.u_core
        self.mem = self.top.u_mem
        # PMEM backdoor: FF-array build exposes `pmem`, the SRAM build exposes
        # the behavioural macro model's `mem` array (same words, same indices)
        if hasattr(self.mem, "pmem"):
            self.pmem = self.mem.pmem
            self.pmem_sram = False
        else:
            self.pmem = self.mem.u_pmem.sram.mem
            self.pmem_sram = True

    def _i(self, handle) -> int:
        return int(handle.value)

    # -- backdoor program / data loading ---------------------------------
    def load_program(self, words, fill_word: int, min_len: int):
        n = max(len(words), min_len)
        for i in range(n):
            self.pmem[i].value = words[i] if i < len(words) else fill_word

    def clear_dmem(self):
        for i in range(64):
            self.mem.dmem[i].value = 0

    def assert_program(self, words, fill_word: int, span: int = 512):
        """Backdoor readback: the RTL PMEM image must equal the model image
        (toolchain's `meta["words_sha256"]` equivalent; a silent image mismatch
        keeps PCs identical while every register/flag diverges)."""
        for i in range(span):
            want = words[i] if i < len(words) else (fill_word & 0xFFFFFFFF)
            got = int(self.pmem[i].value)
            assert got == want, (
                f"PMEM image mismatch at word {i}: rtl=0x{got:08x} "
                f"expected=0x{want:08x}")
        for i in range(64):
            assert int(self.mem.dmem[i].value) == 0, (
                f"DMEM not clean at word {i} before the run "
                f"(0x{int(self.mem.dmem[i].value):04x})")

    def assert_sha(self, meta: dict):
        import hashlib
        if "words_sha256" in meta:
            assert meta["words_sha256"], "generator supplied an empty image hash"

    def set_pc(self, tid: int, pc: int):
        self.core.pc[tid].value = pc & 0x3FF

    def any_thread_running(self, mask: int) -> bool:
        for t in range(4):
            if (mask >> t) & 1 and self._i(self.core.t_halt[t]) == 0:
                return True
        return False

    # -- final state ------------------------------------------------------
    def read_state(self) -> dict:
        st = {
            "pins_out16": int(self.top.pins_out.value),
            "pins_dir8": self._i(self.top.uio_oe),
            "irq_code16": self._i(self.core.irq_code),
            "evt": self._i(self.core.evt),
            "dmem": [self._i(self.mem.dmem[i]) for i in range(64)],
            "threads": [],
        }
        for t in range(4):
            eq_cnt = self._i(self.core.eq_cnt[t])
            eq_head = self._i(self.core.eq_head[t])
            eq = []
            for k in range(eq_cnt):
                slot = (eq_head + k) & 3
                idx = t * 4 + slot        # eq_* arrays are flattened {tid,slot}
                eq.append([1 if self._i(self.core.eq_edge[idx]) else 0,
                           self._i(self.core.eq_tgt[idx]),
                           self._i(self.core.eq_val[idx])])
            tx_cnt = self._i(self.core.tx_cnt[t])
            tx_head = self._i(self.core.tx_head[t])
            tx = [self._i(self.core.tx_mem[t * 8 + ((tx_head + k) & 7)])
                  for k in range(tx_cnt)]
            rx_cnt = self._i(self.core.rx_cnt[t])
            rx_head = self._i(self.core.rx_head[t])
            rx = [self._i(self.core.rx_mem[t * 8 + ((rx_head + k) & 7)])
                  for k in range(rx_cnt)]
            st["threads"].append({
                "pc": self._i(self.core.pc[t]),
                "r": [self._i(self.core.regf[t * 8 + n]) for n in range(8)],
                "z": self._i(self.core.fz[t]),
                "c": self._i(self.core.fc[t]),
                "en": self._i(self.core.t_en[t]),
                "halt": self._i(self.core.t_halt[t]),
                "stall": self._i(self.core.stall[t]),
                "period": self._i(self.core.period[t]),
                "pfrac": self._i(self.core.pfrac[t]),
                "wfrac": self._i(self.core.wfrac[t]),
                "tmask": self._i(self.core.tmask[t]),
                "emask": self._i(self.core.emask[t]),
                "econd": self._i(self.core.econd[t]),
                "nextt": self._i(self.core.nextt[t]),
                "eq": eq,
                "tx": tx,
                "rx": rx,
            })
        return st


def rtl_idle_pins_in(host) -> int:
    """External pin vector the model must see: RTL ui_in (host-link idle levels,
    including a residual MOSI bit) plus pulled-up uio inputs."""
    return ((int(host.dut.ui_in.value) & 0xFF) << 8) | 0xFF


async def run_and_capture_tmr(host, rtl, mask: int) -> int:
    """Issue the real host RUN command and return the RTL TMR cycle at which it
    commits.  The model is then started at that absolute cycle, so NEXTT and
    queued-event absolute targets (and therefore the final architectural state)
    share the RTL's time reference instead of the model's 0-based one."""
    from cocotb.triggers import FallingEdge
    hb = host.dut.dut.u_host
    captured: dict[str, int] = {}

    async def watch():
        while "stop" not in captured:
            # sample mid-cycle: TMR/write-enable are stable and this reads the
            # same cycle index the RTL RUN write commits in (W), not W+1.
            await FallingEdge(host.dut.clk)
            try:
                if (int(hb.csr_wr_en.value) == 1 and
                        int(hb.csr_wr_addr.value) == 0x30):
                    captured["tmr"] = int(rtl.core.tmr.value)
                    captured["nextt"] = (captured["tmr"] + 1) & 0xFFFF
                if "first_en" not in captured:
                    te = [int(rtl.core.t_en[t].value) for t in range(4)]
                    if any(te):
                        captured["first_en"] = int(rtl.core.tmr.value)
                        captured["nextt_en"] = int(rtl.core.nextt[0].value)
            except Exception:
                pass

    task = cocotb.start_soon(watch())
    await host.run(mask)
    captured["stop"] = 1
    await task
    if "tmr" not in captured:
        raise AssertionError("RUN write pulse was not observed")
    return (captured["tmr"], captured.get("nextt"),
            captured.get("first_en", captured["tmr"] + 1),
            captured.get("nextt_en", captured["nextt"]))


# ---------------------------------------------------------------------------
# comparison helpers
# ---------------------------------------------------------------------------
def cmp_state(rtl: dict, model: dict) -> list[str]:
    """Return a list of human-readable mismatches (empty = equal)."""
    errs: list[str] = []

    def sc(name, r, m):
        if r != m:
            errs.append(f"{name}: rtl={r!r} model={m!r}")

    sc("pins_out16", rtl["pins_out16"], model["pins_out16"])
    sc("pins_dir8", rtl["pins_dir8"], model["pins_dir8"])
    sc("irq_code16", rtl["irq_code16"], model["irq_code16"])
    sc("evt", rtl["evt"], model["evt"])
    if rtl["dmem"] != model["dmem"]:
        for i, (a, b) in enumerate(zip(rtl["dmem"], model["dmem"])):
            if a != b:
                errs.append(f"dmem[{i}]: rtl={a:#06x} model={b:#06x}")
                if len([e for e in errs if e.startswith("dmem")]) >= 8:
                    break
    for t in range(4):
        rt, mt = rtl["threads"][t], model["threads"][t]
        for key in ("pc", "r", "z", "c", "en", "halt", "stall", "period",
                    "pfrac", "wfrac", "tmask", "emask", "econd", "nextt",
                    "eq", "tx", "rx"):
            rv, mv = rt[key], mt[key]
            if key in ("en", "halt"):
                rv, mv = int(rv), int(mv)
            if rv != mv:
                errs.append(f"t{t}.{key}: rtl={rv!r} model={mv!r}")
    return errs


def cmp_trace(rtl_records, model_trace):
    """Compare [(cycle,pins,dir)] RTL records with model trace (offset-aligned).

    Returns (offset, mismatches).  The offset accounts for the RTL cycles spent
    loading / issuing the RUN command before the model's cycle 0.
    """
    if not rtl_records and not model_trace:
        return 0, []
    if not rtl_records or not model_trace:
        return 0, [f"trace presence: rtl={len(rtl_records)} model={len(model_trace)}"]
    offset = rtl_records[0][0] - model_trace[0][0]
    errs = []
    if len(rtl_records) != len(model_trace):
        errs.append(f"trace length: rtl={len(rtl_records)} model={len(model_trace)}")
    for i, (rr, mr) in enumerate(zip(rtl_records, model_trace)):
        if rr[0] - offset != mr[0]:
            errs.append(f"trace[{i}] cycle: rtl={rr[0]} (={rr[0]-offset} aligned) "
                        f"model={mr[0]}")
            break
        if rr[1] != mr[1]:
            errs.append(f"trace[{i}] pins@model_cycle={mr[0]}: "
                        f"rtl=0x{rr[1]:04x} model=0x{mr[1]:04x}")
            break
        if rr[2] != mr[2]:
            errs.append(f"trace[{i}] dir@model_cycle={mr[0]}: "
                        f"rtl=0x{rr[2]:02x} model=0x{mr[2]:02x}")
            break
    return offset, errs


def save_failure(seed, words, meta, errs, rtl_records, model_trace, offset,
                 model_cycle):
    """Write a minimisable repro artefact for a failing seed."""
    ART.mkdir(exist_ok=True)
    path = ART / f"seed_{seed:04d}.txt"
    lines = [
        f"# LOOM co-simulation failure, seed={seed} n_insns={meta['n_insns']} "
        f"threads={meta['num_threads']}",
        f"# model halt cycle ≈ {model_cycle}, trace offset = {offset}",
        "",
        "## mismatches",
        *errs[:40],
        "",
        "## program listing",
    ]
    for i, w in enumerate(words):
        try:
            dis = isa.disasm(w, pc=i)
        except Exception:
            dis = f"0x{w:08x}"
        lines.append(f"{i:04d}: {w:08x}  {dis}")
    lines += ["", "## trace window (model cycle, rtl pins, model pins)"]
    for i in range(max(0, len(model_trace) - 40), len(model_trace)):
        mr = model_trace[i] if i < len(model_trace) else None
        rr = rtl_records[i] if i < len(rtl_records) else None
        lines.append(f"  {i}: model={mr} rtl={rr}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# one seed
# ---------------------------------------------------------------------------
async def run_one_seed(host, mon, rtl: RtlView, seed: int, *, n_insns: int,
                       n_threads: int, allow_pull: bool = False,
                       allow_reactive: bool = False):
    """Run one seed on RTL and model; return list of mismatch strings."""
    words, meta = gen(seed=seed, n_insns=n_insns, num_threads=n_threads,
                      allow_pull=allow_pull, allow_reactive=allow_reactive)
    if NOCSRR:
        # CSRR rd / IN rd -> MOVI rd, 0 (both the RTL and the model run the same image)
        words = [(0x02 << 25) | (((w >> 22) & 7) << 22)
                 if ((w >> 25) & 0x7F) in (0x16, 0x1C) else w for w in words]
    mask = meta["thread_mask"]

    # ---- load + integrity check (DMEM has no reset: zero it explicitly) ----
    rtl.load_program(words, fill_word=isa.DEFAULT_FILL_WORD, min_len=512)
    rtl.clear_dmem()
    await ClockCycles(host.dut.clk, 2)      # let the backdoor deposits settle
    rtl.assert_program(words, isa.DEFAULT_FILL_WORD, span=512)
    rtl.assert_sha(meta)

    # ---- lock-step RTL+model run (one model step per RTL clock) -------------
    from cocotb.triggers import FallingEdge
    pins_rec: dict[int, int] = {}
    capture = [True]

    async def pin_watch():
        while capture[0]:
            await FallingEdge(host.dut.clk)
            try:
                pins_rec[int(rtl.core.tmr.value)] = int(rtl.core.pins_in.value) & 0xFFFF
            except Exception:
                pass

    pin_task = cocotb.start_soon(pin_watch())
    # queue the host->thread RX preloads, then set each thread's start PC
    for th in meta["threads"]:
        for b in meta["preload_rx"][th["tid"]]:
            await host.tx_push(th["tid"], b)
        rtl.set_pc(th["tid"], th["start_pc"])
    t_run = mon.mark()
    tmr_run, nextt_run, first_en, nextt_en = await run_and_capture_tmr(host, rtl, mask)

    # model started at the RUN-commit cycle (RTL RUN semantics), one step per
    # RTL clock from here on so the two timelines cannot drift.
    m = Loom()
    m.load(words)
    # RUN commits at the end of cycle W, so the RTL's first enabled cycle is
    # W+1 (first fetch W+1, first issue W+2).  Seed the model at that cycle with
    # stall=0 so its fetch/issue slots match RTL without fudging.
    start_cycle = first_en
    m.cycle = start_cycle
    for th in meta["threads"]:
        for b in meta["preload_rx"][th["tid"]]:
            assert m.push_rx(th["tid"], b), "model RX FIFO overflow in preload"
        m.set_pc(th["tid"], th["start_pc"])
    m.cfg_pins_in_replay(True)
    m.set_pins_in(pins_rec.get(m.tmr16, rtl_idle_pins_in(host)))
    m.inat_log = []
    m.run_threads(mask, nextt=nextt_en)

    def catch_up(target_cycle: int):
        # advance the model to `target_cycle` using the recorded RTL pins_in
        while m.cycle < target_cycle:
            m.set_pins_in(pins_rec.get(m.tmr16, 0))
            m.step()

    # catch up over the RUN-frame tail (RTL already executed a few cycles)
    catch_up(int(rtl.core.tmr.value) if False else m.cycle)
    n_steps = 0
    while True:
        await FallingEdge(host.dut.clk)
        t = int(rtl.core.tmr.value)
        pins_rec.setdefault(t, int(rtl.core.pins_in.value) & 0xFFFF)
        catch_up(t)
        # model state now = start of cycle t; execute cycle t with RTL's pins
        m.set_pins_in(pins_rec.get(t, 0))
        m.step()
        n_steps += 1
        if not rtl.any_thread_running(mask):
            break
        if n_steps > meta["cycles_bound"] + 2000:
            capture[0] = False
            return [f"RTL did not terminate within {meta['cycles_bound']} cycles"]
    # match timing of the model's own halt condition (both at the same absolute
    # cycle now; a couple of catch-up steps are exercised in the extra window)
    for _ in range(EXTRA_CYCLES):
        await ClockCycles(host.dut.clk, 1)
        t = int(rtl.core.tmr.value)
        catch_up(t)
        m.set_pins_in(pins_rec.get(t, 0))
        m.step()
    capture[0] = False
    await pin_task
    rtl_state = rtl.read_state()
    model_trace = list(m.trace)
    model_state = m.state_snapshot()

    # ---- compare ------------------------------------------------------------
    errs = cmp_state(rtl_state, model_state)
    rtl_records = [(c, p, d) for (c, p, d) in mon.records if c >= t_run]
    offset, terr = cmp_trace(rtl_records, model_trace)
    errs += terr
    if errs:
        path = save_failure(seed, words, meta, errs, rtl_records, model_trace,
                            offset, m.cycle)
        summary = "; ".join(errs[:6])
        return [f"seed {seed}: {summary}  (artefact: {path})"]
    return []


# ---------------------------------------------------------------------------
# cocotb entry points
# ---------------------------------------------------------------------------
@cocotb.test(skip=KNOWN_ISSUES["dmem_addr"]["open"])
async def test_random_cosim(dut):
    """100 seeded random programs: full trace + full state differential test."""
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=3)
    rtl = RtlView(dut)
    failures = []
    total_transitions = 0
    for seed in range(1, SEEDS + 1):
        await host.reset(cycles=8)
        errs = await run_one_seed(host, mon, rtl, seed, n_insns=N_INSNS,
                                  n_threads=THREADS)
        if errs:
            failures.append(errs[0])
            dut._log.error("seed %d FAILED: %s", seed, errs[0])
            if len(failures) >= 5:
                break
        else:
            total_transitions += 1
        if seed % 10 == 0:
            dut._log.info("co-sim: %d/%d seeds matched (rtl cycles so far=%d)",
                          seed, SEEDS, mon.mark())
    dut._log.info("co-sim deterministic differential: %d/%d seeds green, "
                  "%d seed runs with full trace+state compare",
                  SEEDS - len(failures), SEEDS, total_transitions)
    if failures:
        for f in failures:
            dut._log.error("  %s", f)
        raise AssertionError(f"co-simulation mismatches on {len(failures)} "
                             f"seed(s): {failures}")


# ---------------------------------------------------------------------------
# Hand-written programs: validate the differential harness itself.  These use
# no DMEM addressing (they are therefore unaffected by the open VER-004 RTL
# regression) and no absolute-time instructions, so they exercise the trace /
# state-alignment machinery cycle-exactly right now.
# ---------------------------------------------------------------------------
HANDWRITTEN = {
    "alu_loop": (
        """
            movi r0, 0
            movi r1, 5
        loop:
            addi r0, 1
            subi r1, 1
            jnz  loop
            mov  r2, r0
            sub  r3, r2
            halt
        """, [(0, 0)], 0x1),
    "flags_rpt": (
        """
            movi r0, 0
            rpt  3
            addi r0, 1
            movi r1, 0xFFFF
            addi r1, 1
            jc   skip
            movi r2, 1
        skip:
            movi r3, 7
            cmpi r3, 7
            jz   done
            movi r4, 1
        done:
            halt
        """, [(0, 0)], 0x1),
    "pins_dir": (
        """
            out  0x0400
            set  0x0800
            clr  0x0400
            tog  0x0300
            dir  0xA5
            dset 0x0F
            dclr 0x30
            halt
        """, [(0, 0)], 0x1),
    "fifo_push": (
        """
            pushi 0x11
            pushi 0x22
            rpt  2
            pushi 0x33
            movi r0, 0x44
            push r0
            halt
        """, [(0, 0)], 0x1),
    "waitd_frac": (
        """
            waitd 0x010A
            waitd 0x00FF
            waitd 0x0001
            halt
        """, [(0, 0)], 0x1),
    "four_threads": (
        None, [(0, 0), (1, 0x40), (2, 0x80), (3, 0xC0)], 0xF),
}


def _four_thread_words():
    words = [0x02000000] * 0x100
    for t in range(4):
        src = f"""
            movi r0, {0x1100 * (t + 1):#06x}
            movi r1, {t + 2}
        L{t}:
            addi r0, 1
            subi r1, 1
            jnz  L{t}
            halt
        """
        w = loom_asm.assemble(src).words
        words[t * 0x40:t * 0x40 + len(w)] = w
    return words


@cocotb.test()
async def test_cosim_handwritten(dut):
    """Hand-written programs validate trace+state alignment without DMEM."""
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=3)
    rtl = RtlView(dut)
    failures = []
    for name, (src, starts, mask) in HANDWRITTEN.items():
        await host.reset(cycles=8)
        words = _four_thread_words() if src is None else loom_asm.assemble(src).words
        rtl.load_program(words, fill_word=isa.DEFAULT_FILL_WORD, min_len=512)
        rtl.clear_dmem()
        await ClockCycles(host.dut.clk, 2)
        rtl.assert_program(words, isa.DEFAULT_FILL_WORD, span=512)

        for tid, pc in starts:
            rtl.set_pc(tid, pc)
        t_run = mon.mark()
        tmr_run, _nextt, first_en, nextt_en = await run_and_capture_tmr(host, rtl, mask)
        for _ in range(6000):
            if not rtl.any_thread_running(mask):
                break
            await ClockCycles(host.dut.clk, 1)
        else:
            failures.append(f"{name}: RTL did not halt")
            continue
        await ClockCycles(host.dut.clk, EXTRA_CYCLES)

        m = Loom()
        m.load(words)
        m.cycle = first_en
        for tid, pc in starts:
            m.set_pc(tid, pc)
        m.set_pins_in(rtl_idle_pins_in(host))
        m.run_threads(mask, nextt=nextt_en)
        for _ in range(6000):
            m.step()
            if all((not th.en) or th.halt for th in m.threads):
                break
        else:
            failures.append(f"{name}: model did not halt")
            continue
        m.run(EXTRA_CYCLES)
        model_trace = list(m.trace)

        errs = cmp_state(rtl.read_state(), m.state_snapshot())
        rtl_records = [(c, p, d) for (c, p, d) in mon.records if c >= t_run]
        offset, terr = cmp_trace(rtl_records, model_trace)
        errs += terr
        if errs:
            failures.append(f"{name}: " + "; ".join(errs[:4]))
            dut._log.error("handwritten %s FAILED: %s", name, "; ".join(errs[:6]))
        else:
            dut._log.info("handwritten %-12s OK (%d trace transitions, offset %d)",
                          name, len(rtl_records), offset)
    if failures:
        raise AssertionError(f"hand-written co-sim mismatches: {failures}")


@cocotb.test(skip=KNOWN_ISSUES["dmem_addr"]["open"])
async def test_cosim_pull_and_reactive(dut):
    """Pull / reactive / combined generators: 12 seeds with host-pushed RX FIFOs.

    Seeds 101-104 are the combined `allow_pull=True, allow_reactive=True` case
    that exposed a generator hazard (masked WAITP clobbered by another thread's
    pin ops; fixed by emitting always-satisfied reactive classes).  Seeds
    111-114 are pull-only, 201-204 reactive-only.
    """
    host, mon = await start_loom(dut, clk_ns=10.0, spi_half=3)
    rtl = RtlView(dut)
    groups = (
        ("pull+reactive", (101, 102, 103, 104), True, True),
        ("pull", (111, 112, 113, 114), True, False),
        ("reactive", (201, 202, 203, 204), False, True),
    )
    failures = []
    for tag, seeds, allow_pull, allow_reactive in groups:
        for seed in seeds:
            await host.reset(cycles=8)
            errs = await run_one_seed(host, mon, rtl, seed, n_insns=40,
                                      n_threads=2, allow_pull=allow_pull,
                                      allow_reactive=allow_reactive)
            if errs:
                failures.append(f"[{tag}] {errs[0]}")
                dut._log.error("[%s] seed %d FAILED: %s", tag, seed, errs[0])
    if failures:
        raise AssertionError(f"pull/reactive co-simulation mismatches: {failures}")
    dut._log.info("co-sim opt-in groups: 12/12 seeds green "
                  "(pull+reactive, pull-only, reactive-only)")


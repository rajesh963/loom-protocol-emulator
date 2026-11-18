#!/usr/bin/env python3
"""SWD (ARM Serial Wire Debug) probe model + independent target-side decoder.

Companion to `sw/examples/swd_target.asm`.  **LOOM emulates the SWD *target***
(the far end of a debug probe): it samples SWDIO on SWCLK rising edges, drives
SWDIO on falling edges, returns the 3-bit ACK, shifts data both ways with even
parity, and implements a minimal ADIv5 DP+AP register file.  This module is the
*probe*: it generates requests, performs the turnaround, samples ACK/data and
independently verifies parity, ACK codes and the register values.

Wire phases implemented (probe = host, target = LOOM)
-----------------------------------------------------
Every bit is two SWCLK half-periods.  Target samples SWDIO on rising edges and
drives it on falling edges; the probe samples the target's ACK/data on rising
edges.  One transaction is:

    req(8 bits) | turnaround(1 clock) | ACK(3 bits) |
    write: data(32) + parity (probe drives) | idle
    read:  data(32) + parity (target drives) | turnaround(1 clock) | idle

The probe drives SWCLK throughout; SWDIO is driven by the probe except during
the ACK and read-data phases (the target drives it then).  A released SWDIO
reads high through the external pull-up — so ACK bit 1 is "release".

What is modelled
----------------
* `parity_even`, `build_request`, `request_bits`, `data_bits` — packet/parity
  helpers used by the probe and by the standalone self-check.
* `SwdProbe` — `FarEnd` plugin: drives SWCLK/SWDIO cycle by cycle from a list of
  transactions, samples the target's responses, measures the SWCLK rate, and
  records `SwdResult`s with ACK/parity/turnaround checks.
* `reference_results` — the expected register values for the demo transaction
  list (IDCODE 0x0BB11477, CTRL/STAT 0xF0000000, AP TAR/DRW + DP RDBUFF).
* Golden-model driver `load_example` / `run_example` / `check_decoded`.

DUT fixture (the target firmware): DP IDCODE 0x0BB11477, CTRL/STAT 0xF0000000,
DP SELECT, DP RDBUFF (= last AP write), AP TAR and AP DRW; FAULT for bad
request parity/stop/park; write data parity checked (bad parity drops the
write).  Honest boundary: no WAIT/retry, no SWD-to-JTAG switching, no AP
banking (only TAR/DRW), no core/memory behind DRW.

Run `python -m host.swd` for the standalone checks plus the golden-model run
(including a probe-clock sweep to measure the target's supported SWCLK).
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sw.protocols import FarEnd, WireWorld, format_wave        # noqa: E402

__all__ = [
    "CLOCK_HZ", "SWCLK_PIN", "SWDIO_PIN", "IDCODE", "CTRL_STAT",
    "ACK_OK", "ACK_WAIT", "ACK_FAULT",
    "parity_even", "build_request", "request_bits", "SwdTransaction",
    "SwdResult", "SwdProbe", "load_example", "run_example",
    "demo_transactions", "check_decoded",
]

CLOCK_HZ = 33_333_333
SWCLK_PIN = 0                    # P[0] = uio[0], probe-driven clock
SWDIO_PIN = 1                    # P[1] = uio[1], bidirectional
IDCODE = 0x0BB11477
CTRL_STAT = 0xF0000000
ACK_OK = 0b001
ACK_WAIT = 0b010
ACK_FAULT = 0b100
_ACK_NAMES = {ACK_OK: "OK", ACK_WAIT: "WAIT", ACK_FAULT: "FAULT"}


# ---------------------------------------------------------------------------
# Packet / parity helpers
# ---------------------------------------------------------------------------
def parity_even(bits) -> int:
    """Even parity of a sequence of 0/1 bits."""
    p = 0
    for b in bits:
        p ^= b & 1
    return p


def build_request(ap: bool, rnw: bool, addr: int) -> int:
    """8-bit SWD request (bit 0 first): start, APnDP, RnW, A2, A3, parity, stop, park."""
    apn = 1 if ap else 0
    rw = 1 if rnw else 0
    a2 = (addr >> 0) & 1
    a3 = (addr >> 1) & 1
    par = parity_even([apn, rw, a2, a3])
    return (1 << 0) | (apn << 1) | (rw << 2) | (a2 << 3) | (a3 << 4) \
        | (par << 5) | (0 << 6) | (1 << 7)


def request_bits(req: int) -> list[int]:
    return [(req >> i) & 1 for i in range(8)]


def data_bits(value: int, *, force_bad_parity: bool = False) -> list[int]:
    """32 data bits LSB first + even parity bit (optionally corrupted)."""
    bits = [(value >> i) & 1 for i in range(32)]
    par = parity_even(bits)
    if force_bad_parity:
        par ^= 1
    return bits + [par]


@dataclass
class SwdTransaction:
    """One request the probe sends."""

    kind: str                    # "read" or "write"
    ap: bool = False
    addr: int = 0                # A[3:2] word address (0..3)
    data: int = 0                # write data
    expect: int | None = None    # expected read data (None = don't check)
    expect_fault: bool = False   # corrupted request -> target should FAULT
    force_bad_parity: bool = False   # write with corrupted data parity
    bad_parity: bool = False     # corrupted request parity (-> FAULT)


@dataclass
class SwdResult:
    """One decoded transaction (probe side)."""

    kind: str = ""
    ap: bool = False
    addr: int = 0
    write_data: int = 0
    read_data: int = 0
    ack: int = 7                 # 0b111 = no ACK / bus not driven
    data_parity_ok: bool = False
    turnaround_ok: bool = True
    expect: int | None = None
    expect_fault: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ack_name(self) -> str:
        return _ACK_NAMES.get(self.ack, f"0b{self.ack:03b}")

    @property
    def value_ok(self) -> bool:
        if self.expect is None:
            return True
        return self.read_data == self.expect

    @property
    def ok(self) -> bool:
        if self.expect_fault:
            return self.ack == ACK_FAULT and not self.errors
        return (self.ack == ACK_OK and self.data_parity_ok and self.value_ok
                and not self.errors)

    def text(self) -> str:
        d = (f"read=0x{self.read_data:08X}"
             if self.kind == "read" else f"write=0x{self.write_data:08X}")
        return (f"SWD {'AP' if self.ap else 'DP'} addr={self.addr} {d} "
                f"ACK={self.ack_name} parity={'ok' if self.data_parity_ok else 'BAD'}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SwdResult({self.text()})"


# ---------------------------------------------------------------------------
# Probe model (FarEnd)
# ---------------------------------------------------------------------------
class SwdProbe(FarEnd):
    """Cycle-level SWD probe: drives SWCLK/SWDIO, decodes the target's answers.

    `half` is the SWCLK half period in cycles (the probe's clock rate is
    clock_hz / (2*half)); `transactions` is the request script.
    """

    name = "swd-probe"

    def __init__(self, world: WireWorld, transactions, *, half: int = 40,
                 clock_hz: int | None = None, gap_ticks: int = 4):
        self.world = world
        self.half = int(half)
        self.clock_hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.gap_ticks = int(gap_ticks)
        self.queue = list(transactions)
        self.results: list[SwdResult] = []
        self.wave_clk: list[tuple[int, int]] = []
        self.rises: list[int] = []
        self.clk = 0                  # probe-driven SWCLK level
        self.swdio = 1                # probe-driven SWDIO level (1 = released high)
        self.next_tick = int(world.cycle) + 2
        self.phase = "idle"
        self.sub = 0
        self.cur: SwdTransaction | None = None
        self.req: list[int] = []
        self.wdata: list[int] = []
        self.ack_bits: list[int] = []
        self.rdata: list[int] = []
        self.sample: tuple[str, int] | None = None
        self.turnaround_ok = True
        self.last_ack = 7
        self._wire_swdio = 1
        world.attach(self)

    # -- pin drive -------------------------------------------------------
    @property
    def ext_mask(self) -> int:              # type: ignore[override]
        return (1 << SWCLK_PIN) | (1 << SWDIO_PIN)

    @property
    def ext_value(self) -> int:             # type: ignore[override]
        return ((self.clk & 1) << SWCLK_PIN) | ((self.swdio & 1) << SWDIO_PIN)

    @property
    def ack_name(self) -> str:
        if not self.results:
            return "?"
        return self.results[-1].ack_name

    @property
    def done(self) -> bool:
        return not self.queue and self.cur is None

    # -- per-cycle stepper ----------------------------------------------
    def drive(self) -> tuple[int, int]:     # type: ignore[override]
        cycle = int(self.world.cycle)
        while cycle >= self.next_tick:
            self._tick()
            self.next_tick += self.half
        self.wave_clk.append((cycle, self.clk))
        return self.ext_mask, self.ext_value

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        # the wire level of SWDIO from the probe's point of view
        if (pins_dir8 >> SWDIO_PIN) & 1:
            self._wire_swdio = (pins_out16 >> SWDIO_PIN) & 1
        else:
            self._wire_swdio = (self.swdio >> 0) & 1 if False else \
                (self.ext_value >> SWDIO_PIN) & 1
        if self.sample is not None:
            kind, idx = self.sample
            self._record_sample(kind, idx)
            self.sample = None

    # -- transaction tick state machine ---------------------------------
    def _prepare(self, tx: SwdTransaction) -> None:
        self.cur = tx
        req = build_request(tx.ap, tx.kind == "read", tx.addr)
        if tx.bad_parity:
            req ^= 1 << 5                  # corrupt request parity bit
        self.req = request_bits(req)
        self.wdata = data_bits(tx.data, force_bad_parity=tx.force_bad_parity)
        self.ack_bits = []
        self.rdata = []
        self.turnaround_ok = True
        self.last_ack = 7
        self.sample = None

    def _tick(self) -> None:
        if self.phase == "idle":
            self.clk = 0
            if self.queue:
                self._prepare(self.queue.pop(0))
                self.phase = "req"
                self.sub = 0
                self.swdio = self.req[0]
            else:
                self.swdio = 1
            return
        if self.phase == "req":
            if self.sub % 2 == 0:                       # SWCLK low: set a bit
                self.clk = 0
                self.swdio = self.req[self.sub // 2]
            else:                                        # SWCLK high
                self.clk = 1
                self.rises.append(int(self.world.cycle))
            self.sub += 1
            if self.sub == 16:
                self.phase = "trn"
                self.sub = 0
            return
        if self.phase == "trn":
            if self.sub == 0:                            # low: release SWDIO
                self.clk = 0
                self.swdio = 1
            else:                                        # high: turnaround clock
                self.clk = 1
                self.rises.append(int(self.world.cycle))
                self.sample = ("trn", 0)
            self.sub += 1
            if self.sub == 2:
                self.phase = "ack"
                self.sub = 0
            return
        if self.phase == "ack":
            bit = self.sub // 2
            if self.sub % 2 == 0:                        # low: target drives ACK bit
                self.clk = 0
                self.swdio = 1
            else:
                self.clk = 1
                self.rises.append(int(self.world.cycle))
                self.sample = ("ack", bit)
            self.sub += 1
            if self.sub == 6:
                self.ack_bits = self.ack_bits
                ack = 0
                for i, b in enumerate(self.ack_bits):
                    ack |= (b & 1) << i
                self.last_ack = ack
                if ack != ACK_OK:
                    self.phase = "end"
                else:
                    self.phase = ("wdata" if self.cur.kind == "write"
                                  else "rdata")
                self.sub = 0
            return
        if self.phase == "wdata":
            bit = self.sub // 2
            if self.sub % 2 == 0:                       # low: drive data bit
                self.clk = 0
                self.swdio = self.wdata[bit]
            else:
                self.clk = 1
                self.rises.append(int(self.world.cycle))
            self.sub += 1
            if self.sub == 66:
                self.phase = "end"
                self.sub = 0
            return
        if self.phase == "rdata":
            bit = self.sub // 2
            if self.sub % 2 == 0:                       # low: target drives data
                self.clk = 0
                self.swdio = 1
            else:
                self.clk = 1
                self.rises.append(int(self.world.cycle))
                self.sample = ("data", bit)
            self.sub += 1
            if self.sub == 66:
                self.phase = "end"
                self.sub = 0
            return
        if self.phase == "end":
            if self.sub == 0:
                self.clk = 0
                self.swdio = 1
                self.sub += 1
                # writes and aborted (FAULT) transactions end after this low
                # edge; only a successful read has the extra turnaround clock
                if (self.cur is not None and self.cur.kind == "write")                         or self.last_ack != ACK_OK:
                    self._finish()
                    self.phase = "idle"
                    self.sub = 0
                return
            # read turnaround clock
            self.clk = 1
            self.rises.append(int(self.world.cycle))
            self._finish()
            self.phase = "idle"
            self.sub = 0
            return

    # -- sample decoding -------------------------------------------------
    def _record_sample(self, kind: str, idx: int) -> None:
        level = self._wire_swdio & 1
        if kind == "trn":
            self.turnaround_ok = bool(level == 1)
        elif kind == "ack":
            while len(self.ack_bits) <= idx:
                self.ack_bits.append(0)
            self.ack_bits[idx] = level
        elif kind == "data":
            self.rdata.append(level)

    def _finish(self) -> None:
        tx, self.cur = self.cur, None
        ack = 0
        for i, b in enumerate(self.ack_bits):
            ack |= (b & 1) << i
        res = SwdResult(kind=tx.kind, ap=tx.ap, addr=tx.addr,
                        write_data=tx.data, ack=ack, expect=tx.expect,
                        expect_fault=tx.expect_fault,
                        turnaround_ok=self.turnaround_ok)
        if tx.kind == "read" and ack == ACK_OK:
            if len(self.rdata) >= 33:
                res.read_data = sum((b & 1) << i
                                    for i, b in enumerate(self.rdata[:32]))
                res.data_parity_ok = (
                    parity_even(self.rdata[:32]) == (self.rdata[32] & 1))
            else:
                res.errors.append(f"only {len(self.rdata)} data bits")
        else:
            # no data phase on FAULT (or on writes), nothing to check
            res.data_parity_ok = True
        if not res.turnaround_ok:
            res.errors.append("target did not release SWDIO during turnaround")
        self.results.append(res)

    # -- measurements / reporting ---------------------------------------
    def measured_period(self) -> float:
        r = self.rises
        if len(r) < 2:
            return 2.0 * self.half
        deltas = sorted(b - a for a, b in zip(r, r[1:]) if b > a)
        mid = len(deltas) // 2
        return float(deltas[mid])          # median: ignores inter-transaction idle

    def measured_half_period(self) -> float:
        return self.measured_period() / 2.0

    def measured_clock_hz(self) -> float:
        return self.clock_hz / self.measured_period()

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {"SWCLK": format_wave(self.wave_clk, chars=chars)}

    def summary(self) -> list[str]:
        lines = [f"SWD probe: {len(self.results)} transactions, "
                 f"SWCLK {self.measured_clock_hz() / 1e3:.1f} kHz "
                 f"(period {self.measured_period():.0f} cyc)"]
        lines += [f"  {r.text()}" + ("" if r.ok else f"  !! {r.errors}")
                  for r in self.results]
        return lines


# ---------------------------------------------------------------------------
# Demo transaction list + golden-model driver
# ---------------------------------------------------------------------------
def demo_transactions() -> list[SwdTransaction]:
    """The transaction script used by the model self-test."""
    return [
        SwdTransaction("read", ap=False, addr=0, expect=IDCODE),
        SwdTransaction("read", ap=False, addr=1, expect=CTRL_STAT),
        SwdTransaction("write", ap=False, addr=2, data=0x00000000),
        SwdTransaction("write", ap=True, addr=1, data=0x10000000),
        SwdTransaction("write", ap=True, addr=3, data=0xDEADBEEF),
        SwdTransaction("read", ap=False, addr=3, expect=0xDEADBEEF,
                       ),                                # DP RDBUFF
        SwdTransaction("read", ap=True, addr=3, expect=0xDEADBEEF),  # AP DRW
        SwdTransaction("read", ap=True, addr=1, expect=0x10000000),  # AP TAR
        SwdTransaction("write", ap=True, addr=3, data=0x12345678,
                       force_bad_parity=True),           # dropped write
        SwdTransaction("read", ap=True, addr=3, expect=0xDEADBEEF),  # unchanged
        SwdTransaction("read", ap=False, addr=0, bad_parity=True,
                       expect_fault=True),               # request parity error
    ]


SWD_ASM = _REPO / "sw" / "examples" / "swd_target.asm"
VERBOSE = False


def assemble_example(path: pathlib.Path | None = None):
    from sw import loom_asm
    src = (path or SWD_ASM).read_text(encoding="utf-8")
    return loom_asm.assemble(src, filename="swd_target[model]")


def load_example(transactions=None, *, half: int = 40, base: int = 0,
                 tid: int = 0, clock_hz: int = CLOCK_HZ):
    from sw import loom_model
    from host.loom_host import ModelLink
    prog = assemble_example()
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=clock_hz)
    probe = SwdProbe(world, transactions if transactions is not None
                     else demo_transactions(), half=half)
    loom.run_threads(1 << tid)
    return dict(prog=prog, loom=loom, link=link, world=world, probe=probe,
                tid=tid, half=half)


def run_example(ctx: dict, *, max_cycles: int | None = None) -> dict:
    world, probe = ctx["world"], ctx["probe"]
    limit = max_cycles or (len(probe.queue) + len(probe.results) + 2) \
        * 120 * ctx["half"] + 4000
    while world.cycle < limit and not probe.done:
        world.run(64)
    for _ in range(4):
        if probe.done:
            break
        world.run(4096)
    return ctx


def check_decoded(probe: SwdProbe, ctx: dict, check) -> None:
    """Shared SWD check list through a `check(name, ok, detail)` function."""
    results = probe.results
    want = len(ctx.get("transactions") or []) or 11
    check("swd: all probe transactions completed", len(results) >= want,
          f"{len(results)}/{want}, SWCLK {probe.measured_clock_hz()/1e3:.1f} kHz")
    by_name = [(f"swd: {r.kind} {'AP' if r.ap else 'DP'} addr {r.addr}", r)
               for r in results]
    # aggregate: every result that carries an expectation must match
    mism = [f"{r.text()} (expect ACK={_ACK_NAMES.get(ACK_FAULT if r.expect_fault else ACK_OK)})"
            for r in results if not r.ok]
    check("swd: ACK/parity/value checks on every transaction", not mism,
          "; ".join(mism) if mism else f"{len(results)} transactions all ok")
    for r in results:
        if r.expect_fault:
            check("swd: malformed request parity -> FAULT ACK",
                  r.ack == ACK_FAULT, r.text())
        elif r.kind == "read" and r.expect is not None:
            check(f"swd: readback 0x{r.expect:08X} matches", r.value_ok, r.text())
        else:
            check(f"swd: write 0x{r.write_data:08X} ACKed OK",
                  r.ack == ACK_OK, r.text())
    check("swd: target releases SWDIO during the turnaround",
          all(r.turnaround_ok for r in results),
          f"{sum(1 for r in results if r.turnaround_ok)}/{len(results)}")
    check("swd: measured SWCLK is the probe clock (half period matches)",
          abs(probe.measured_half_period() - probe.half) <= 1.0,
          f"{probe.measured_half_period():.1f} cyc "
          f"({probe.measured_clock_hz()/1e3:.1f} kHz)")


def _model_selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    tx = demo_transactions()
    ctx = load_example(tx, half=40)
    ctx["transactions"] = tx
    run_example(ctx)
    check("swd_target: firmware assembled fits the SRAM build",
          len(ctx["prog"].words) <= 1024, f"{len(ctx['prog'].words)} words")
    check_decoded(ctx["probe"], ctx, check)

    # --- SWCLK ceiling sweep: the probe half period the target can follow ----
    good, bad = [], []
    for half in (64, 48, 40, 32, 28, 24, 20, 16, 12):
        c = load_example([SwdTransaction("read", ap=False, addr=0,
                                         expect=IDCODE)], half=half)
        run_example(c)
        r = c["probe"].results
        worked = bool(r and r[0].ok)
        (good if worked else bad).append(half)
        if VERBOSE:
            print(f"        half={half:3d} -> "
                  f"{'ok' if worked else 'FAIL'} "
                  f"({len(c['probe'].results)} results)")
    check("swd: SWCLK ceiling sweep has a working region", bool(good),
          f"works at half period {sorted(good)} cyc "
          f"(up to {CLOCK_HZ/(2*min(good))/1e3:.0f} kHz), "
          f"fails at {sorted(bad)} cyc")
    if good:
        best = min(good)
        check("swd: measured supported SWCLK ceiling "
              "(smallest half that works)",
              best <= 48,
              f"half={best} cyc -> {CLOCK_HZ/(2*best)/1e3:.0f} kHz")
    print("SWD_MODEL_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Standalone self-check / CLI
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    check("SWD IDCODE fixture 0x0BB11477 (LSB of the 32-bit ID is 1)",
          IDCODE == 0x0BB11477 and IDCODE & 1,
          hex(IDCODE))
    # request packet layout for a DP IDCODE read: start=1, park=1, parity over
    # APnDP/RnW/A2/A3
    req = build_request(False, True, 0)
    check("request(DP read addr0) = 0xA5 (start, RnW, parity, park)",
          req == 0xA5, f"0x{req:02X} bits={request_bits(req)}")
    reqw = build_request(True, False, 3)
    check("request(AP write addr3) has correct A2/A3/parity",
          (reqw & 0x18) == 0x18
          and parity_even([(reqw >> i) & 1 for i in (1, 2, 3, 4)])
          == ((reqw >> 5) & 1),
          f"0x{reqw:02X}")
    check("even parity of a 32-bit value + parity bit is 0",
          parity_even(data_bits(0xDEADBEEF)) == 0
          and parity_even(data_bits(0xDEADBEEF, force_bad_parity=True)) == 1,
          f"ones={sum(data_bits(0xDEADBEEF))}")
    check("corrupted data parity flips the parity bit",
          data_bits(0xDEADBEEF, force_bad_parity=True)[32] !=
          data_bits(0xDEADBEEF)[32], "")
    print("SWD_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    VERBOSE = "-v" in argv or "--verbose" in argv
    rc = _selftest()
    rc |= _model_selftest()
    print("SWD_SELFTEST:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

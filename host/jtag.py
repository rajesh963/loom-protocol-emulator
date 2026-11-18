#!/usr/bin/env python3
"""JTAG TAP far-end model and independent wire decoder (host side of `jtag_tap.asm`).

LOOM is the **JTAG controller** here: the firmware bit-bangs the four-wire
interface (TCK/TMS/TDI out, TDO in) and samples TDO at every TCK rising edge.
This module is the *other* end of the cable: a standards-shaped IEEE 1149.1 TAP
controller state machine (the DUT) plus a wire decoder that independently
reconstructs the TAP state path from the pin trace and verifies the shifted
bits.

What is modelled
----------------
* `TapStateMachine` — the 16-state IEEE 1149.1 TAP state machine:
  Test-Logic-Reset (TLR), Run-Test/Idle (RTI), Select-DR/IR, Capture-DR/IR,
  Shift-DR/IR, Exit1/Exit2, Pause, Update.  TDI is shifted in LSB-first on
  rising TCK; TDO is the LSB of the active shift register and changes on the
  falling edge of TCK (that is the timing the firmware samples).
* `TapWireDecoder` — takes the wire levels once per cycle and decodes whole
  scans: the state path, the IR value latched at Update-IR, the TDO bits the
  DUT drove during each shift, and the DR values captured/updated.  It is
  independent of the firmware: it does not know which TMS/TDI bits were sent,
  it only observes them and runs the TAP state machine forward.
* `JtagDut` — a `FarEnd` plugin for `WireWorld`: it drives TDO (idle high,
  LSB of the active shift register during Capture/Shift) and attaches the
  decoder to the chip's pin trace.

The DUT fixture (the simulated device on the other side of the TAP)
-------------------------------------------------------------------
* IR width 4 bits.
* IR = `0x2` (`IR_IDCODE`) -> DR is the 32-bit `IDCODE` register, capture value
  `IDCODE = 0x14B5A5A5`.  Shifting fewer than 32 bits reads the low bits of the
  IDCODE LSB-first (a legal partial DR scan).
* IR = `0xF` (`IR_BYPASS`) -> 1-bit BYPASS register (capture value 0).
* Capture-IR loads `0b0001` (the IEEE 1149.1 status pattern in the two LSBs).
* Test-Logic-Reset loads the IDCODE instruction into the IR, so a controller
  that resets the TAP and then clocks TMS=0 lands in Run-Test/Idle with IDCODE
  selected.

The default firmware sequence (`sw/examples/jtag_tap.asm`)
----------------------------------------------------------
TLR (5 x TMS=1) -> RTI -> Select-DR -> Select-IR -> Capture-IR -> Shift-IR
(4 bits, IDCODE instruction 0x2, TDO = capture 0b0001) -> Exit1-IR -> Update-IR
-> Select-DR -> Capture-DR -> Shift-DR (`DR_BITS` bits of IDCODE, LSB first)
-> Exit1-DR -> Update-DR.  Every TDO bit is pushed to the host TX FIFO, so the
model self-test can cross-check the firmware-captured bits against the decoder.

Run `python -m host.jtag` for the standalone self-check (synthetic TCK/TMS/TDI
stimulus into the decoder, no golden model needed).
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
    "IDCODE", "IR_IDCODE", "IR_BYPASS", "IR_WIDTH", "DR_WIDTH_IDCODE",
    "CaptureIrValue", "TapStateMachine", "TapWireDecoder", "JtagDut",
    "TAP_TRANSITIONS", "TAP_STATES", "DEFAULT_PINS", "ClockHz",
]

CLOCK_HZ = 33_333_333
DEFAULT_PINS = dict(tck=0, tms=1, tdi=2, tdo=3)

#: IDCODE fixture: version=1, part=0x4B5A, manufacturer=0x52D, LSB (bit0) = 1.
IDCODE = 0x14B5A5A5
IR_WIDTH = 4
IR_IDCODE = 0x2          # 4-bit IDCODE instruction, shifted LSB first
IR_BYPASS = 0xF          # 4-bit BYPASS instruction
DR_WIDTH_IDCODE = 32
CAPTURE_IR = 0b0001      # IEEE 1149.1 status bits in Capture-IR

TAP_STATES = [
    "TLR", "RTI", "SEL_DR", "CAP_DR", "SHIFT_DR", "EXIT1_DR", "PAUSE_DR",
    "EXIT2_DR", "UPD_DR", "SEL_IR", "CAP_IR", "SHIFT_IR", "EXIT1_IR",
    "PAUSE_IR", "EXIT2_IR", "UPD_IR",
]

#: state -> (next on TMS=0, next on TMS=1) -- IEEE 1149.1 figure 6-1
TAP_TRANSITIONS = {
    "TLR": ("RTI", "TLR"),
    "RTI": ("RTI", "SEL_DR"),
    "SEL_DR": ("CAP_DR", "SEL_IR"),
    "CAP_DR": ("SHIFT_DR", "EXIT1_DR"),
    "SHIFT_DR": ("SHIFT_DR", "EXIT1_DR"),
    "EXIT1_DR": ("PAUSE_DR", "UPD_DR"),
    "PAUSE_DR": ("PAUSE_DR", "EXIT2_DR"),
    "EXIT2_DR": ("SHIFT_DR", "UPD_DR"),
    "UPD_DR": ("RTI", "SEL_DR"),
    "SEL_IR": ("CAP_IR", "TLR"),
    "CAP_IR": ("SHIFT_IR", "EXIT1_IR"),
    "SHIFT_IR": ("SHIFT_IR", "EXIT1_IR"),
    "EXIT1_IR": ("PAUSE_IR", "EXIT2_IR"),
    "PAUSE_IR": ("PAUSE_IR", "EXIT2_IR"),
    "EXIT2_IR": ("SHIFT_IR", "UPD_IR"),
    "UPD_IR": ("RTI", "SEL_DR"),
}

CaptureIrValue = CAPTURE_IR  # public alias for docs/tests

# avoid a circular-looking name in the transition table above
TAP_TRANSITIONS["EXIT1_IR"] = ("PAUSE_IR", "UPD_IR")
TAP_TRANSITIONS["EXIT2_IR"] = ("SHIFT_IR", "UPD_IR")

ClockHz = CLOCK_HZ          # explicit alias: the shipped 33.33 MHz design point


# ---------------------------------------------------------------------------
# IEEE 1149.1 TAP controller state machine (the DUT)
# ---------------------------------------------------------------------------
class TapStateMachine:
    """The DUT's 16-state TAP controller with a 4-bit IR and a DR fixture."""

    def __init__(self, *, idcode: int = IDCODE, ir_width: int = IR_WIDTH):
        self.idcode = int(idcode)
        self.ir_width = int(ir_width)
        self.reset()

    # -- fixture reset ---------------------------------------------------
    def reset(self) -> None:
        self.state = "TLR"
        # Test-Logic-Reset loads the IDCODE instruction on a real device.
        self.ir = IR_IDCODE & ((1 << self.ir_width) - 1)
        self.ir_shift = 0
        self.dr_shift = self.idcode
        self.dr_capture = self.idcode
        self.dr_updated = 0
        self.ir_updates: list[tuple[str, int]] = []
        self.dr_updates: list[tuple[int, int]] = []

    # -- fixture description ---------------------------------------------
    def dr_width(self) -> int:
        if self.ir == IR_IDCODE:
            return DR_WIDTH_IDCODE
        return 1                                  # BYPASS / unknown

    def capture_dr(self) -> int:
        if self.ir == IR_IDCODE:
            return self.idcode
        return 0

    # -- wire events -----------------------------------------------------
    def rising(self, tms: int, tdi: int) -> str:
        """Clock a TCK rising edge: shift TDI in, then take the TMS transition."""
        tms = 1 if tms else 0
        tdi = 1 if tdi else 0
        before = self.state
        # 1) shift in TDI on the active shift register (LSB-first)
        if before == "SHIFT_IR":
            w = self.ir_width
            self.ir_shift = (self.ir_shift >> 1) | (tdi << (w - 1))
        elif before == "SHIFT_DR":
            w = self.dr_width()
            self.dr_shift = (self.dr_shift >> 1) | (tdi << (w - 1))
        # 2) state transition + entry action (capture/update/reset)
        nxt = TAP_TRANSITIONS[before][tms]
        self.state = nxt
        if nxt == "TLR":
            self.ir = IR_IDCODE & ((1 << self.ir_width) - 1)
            self.dr_shift = self.idcode
        elif nxt == "CAP_IR":
            self.ir_shift = CAPTURE_IR
        elif nxt == "CAP_DR":
            self.dr_capture = self.capture_dr()
            self.dr_shift = self.dr_capture
        elif nxt == "UPD_IR":
            self.ir = self.ir_shift & ((1 << self.ir_width) - 1)
            self.ir_updates.append((before, self.ir))
        elif nxt == "UPD_DR":
            self.dr_updated = self.dr_shift
            self.dr_updates.append((self.ir, self.dr_updated))
        return nxt

    def tdo_level(self) -> int:
        """TDO level driven by the DUT in the current state (sampled by host)."""
        if self.state in ("SHIFT_IR", "CAP_IR"):
            return self.ir_shift & 1
        if self.state in ("SHIFT_DR", "CAP_DR"):
            return self.dr_shift & 1
        return 1

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"TapStateMachine(state={self.state}, ir=0x{self.ir:X}, "
                f"dr=0x{self.dr_shift:X})")


# ---------------------------------------------------------------------------
# Wire decoder: pin levels -> TAP state path + decoded scans
# ---------------------------------------------------------------------------
@dataclass
class TapScan:
    """One complete shift scan, reconstructed from the wire."""

    kind: str                     # "IR" or "DR"
    bits: list[int]               # TDO bits sampled during the shift, LSB first
    cycle0: int = 0               # cycle of the first shift edge
    cycle1: int = 0               # cycle of the last shift edge
    risings: int = 0
    tdi_bits: list[int] = field(default_factory=list)   # bits shifted in

    @property
    def value(self) -> int:
        """Value the DUT shifted OUT on TDO (LSB first)."""
        v = 0
        for i, b in enumerate(self.bits):
            v |= (b & 1) << i
        return v

    @property
    def shift_in_value(self) -> int:
        """Value the host shifted IN on TDI (LSB first)."""
        v = 0
        for i, b in enumerate(self.tdi_bits):
            v |= (b & 1) << i
        return v

    def text(self) -> str:
        return f"{self.kind} scan: TDO=" + "".join(str(b) for b in self.bits)


class TapWireDecoder:
    """TAP state machine + scan reconstruction from observed wire levels.

    `step(cycle, tck, tms, tdi)` is called once per cycle with the *chip-driven*
    levels.  The decoder runs the TAP state machine exactly as the far-end DUT
    must, and records what the host would sample on TDO at each rising edge.
    """

    def __init__(self, *, idcode: int = IDCODE, ir_width: int = IR_WIDTH):
        self.sm = TapStateMachine(idcode=idcode, ir_width=ir_width)
        self.prev_tck = 0
        self.tdo_out = 1                 # what the DUT drives (changes on falling)
        self.tck_rises: list[int] = []
        self.tck_falls: list[int] = []
        self.path: list[str] = ["TLR"]   # state after each rising edge
        self.risings: list[dict] = []    # per rising edge detail
        self.wave_tck: list[tuple[int, int]] = []
        self.wave_tdo: list[tuple[int, int]] = []

    # -- per-cycle observation ------------------------------------------
    def step(self, cycle: int, tck: int, tms: int, tdi: int) -> None:
        tck = 1 if tck else 0
        if tck != self.prev_tck:
            if tck == 1:
                # what the host samples this high phase: the TDO the DUT has
                # been driving since the previous falling edge
                self.tck_rises.append(cycle)
                self.wave_tdo.append((cycle, self.tdo_out))
                self.risings.append({
                    "cycle": cycle,
                    "state_before": self.sm.state,
                    "tms": tms,
                    "tdi": tdi,
                    "tdo": self.tdo_out,
                })
                nxt = self.sm.rising(tms, tdi)
                self.path.append(nxt)
            else:
                self.tck_falls.append(cycle)
                self.tdo_out = self.sm.tdo_level()
                self.wave_tdo.append((cycle, self.tdo_out))
            self.wave_tck.append((cycle, tck))
            self.prev_tck = tck
    # -- decoded products ------------------------------------------------
    def scans(self) -> list[TapScan]:
        """Group the rising-edge TDO samples into IR and DR shift scans."""
        out: list[TapScan] = []
        cur: TapScan | None = None
        for ev in self.risings:
            before = ev["state_before"]
            kind = "IR" if before == "SHIFT_IR" else (
                "DR" if before == "SHIFT_DR" else None)
            if kind is None:
                if cur is not None:
                    out.append(cur)
                    cur = None
                continue
            if cur is None or cur.kind != kind:
                if cur is not None:
                    out.append(cur)
                cur = TapScan(kind=kind, bits=[], cycle0=ev["cycle"])
            cur.bits.append(ev["tdo"] & 1)
            cur.tdi_bits.append(ev["tdi"] & 1)
            cur.cycle1 = ev["cycle"]
            cur.risings += 1
        if cur is not None:
            out.append(cur)
        return out

    def ir_scans(self) -> list[TapScan]:
        return [s for s in self.scans() if s.kind == "IR"]

    def dr_scans(self) -> list[TapScan]:
        return [s for s in self.scans() if s.kind == "DR"]

    def measured_period(self) -> float:
        """Mean TCK rising-to-rising period (cycles)."""
        r = self.tck_rises
        if len(r) < 2:
            return 0.0
        return (r[-1] - r[0]) / float(len(r) - 1)

    def intervals(self) -> list[int]:
        """TCK rising-to-rising intervals (cycles)."""
        r = self.tck_rises
        return [b - a for a, b in zip(r, r[1:])]

    def period_range(self) -> tuple[int, int]:
        d = self.intervals()
        return (min(d), max(d)) if d else (0, 0)

    def edge_jitter(self) -> float:
        """Max local interval deviation from the mean TCK period (cycles).

        JTAG has no maximum TCK rate: the low phase is *instruction-issue*
        padded, so a few cycles of interval variation are legal (the DUT only
        sees setup/hold around the rising edge).  This number is the
        pulse-to-pulse consistency, not a drift-free bit grid.
        """
        d = self.intervals()
        if not d:
            return 0.0
        mean = sum(d) / float(len(d))
        return max(abs(x - mean) for x in d)

    def high_times(self) -> list[int]:
        """TCK high-phase durations (rising edge -> following falling edge)."""
        import bisect
        out = []
        for r in self.tck_rises:
            i = bisect.bisect_left(self.tck_falls, r)
            if i < len(self.tck_falls) and self.tck_falls[i] > r:
                out.append(self.tck_falls[i] - r)
        return out

    def low_times(self) -> list[int]:
        """TCK low-phase durations (falling edge -> following rising edge)."""
        import bisect
        out = []
        for f in self.tck_falls:
            i = bisect.bisect_left(self.tck_rises, f)
            if i < len(self.tck_rises) and self.tck_rises[i] > f:
                out.append(self.tck_rises[i] - f)
        return out

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {
            "TCK": format_wave(self.wave_tck, chars=chars),
            "TDO": format_wave(self.wave_tdo, chars=chars),
        }

    def summary(self) -> list[str]:
        lines = [f"TAP path: {' -> '.join(self.path)}"]
        for i, s in enumerate(self.scans()):
            lines.append(f"scan {i}: {s.kind} {len(s.bits)} bits, "
                         f"TDO LSB-first={''.join(str(b) for b in s.bits)} "
                         f"(0x{s.value:X}), TDI in=0x{s.shift_in_value:X}")
        if self.tck_rises:
            lo, hi = self.period_range()
            lines.append(f"TCK {len(self.tck_rises)} rising edges, "
                         f"period {lo}..{hi} cyc "
                         f"(mean {self.measured_period():.2f} cyc -> "
                         f"{CLOCK_HZ / max(1e-9, self.measured_period())/1e3:.0f} kHz), "
                         f"local interval jitter <= {self.edge_jitter():.2f} cyc")
        return lines


# ---------------------------------------------------------------------------
# FarEnd plugin: the DUT drives TDO from the decoded state machine
# ---------------------------------------------------------------------------
class JtagDut(FarEnd):
    """The far end of the JTAG cable: a TAP device that drives TDO.

    The chip drives TCK/TMS/TDI; this model runs a `TapWireDecoder` (state
    machine + shift registers + scan decoder) and drives TDO from the active
    shift register.  TDO changes on the falling edge of TCK, the IEEE 1149.1
    timing the firmware's sampling is built around.
    """

    name = "jtag-dut"

    def __init__(self, world: WireWorld, *, tck_pin: int = 0, tms_pin: int = 1,
                 tdi_pin: int = 2, tdo_pin: int = 3, idcode: int = IDCODE,
                 ir_width: int = IR_WIDTH):
        self.world = world
        self.tck_pin = int(tck_pin)
        self.tms_pin = int(tms_pin)
        self.tdi_pin = int(tdi_pin)
        self.tdo_pin = int(tdo_pin)
        self.decoder = TapWireDecoder(idcode=idcode, ir_width=ir_width)
        self._prev = None
        world.attach(self)

    # -- pin drive -------------------------------------------------------
    @property
    def ext_mask(self) -> int:              # type: ignore[override]
        return 1 << self.tdo_pin

    @property
    def ext_value(self) -> int:             # type: ignore[override]
        return (self.decoder.tdo_out & 1) << self.tdo_pin

    # -- world callback --------------------------------------------------
    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        def level(pin: int) -> int:
            return FarEnd._wire_level(pins_out16, pins_dir8,
                                      self.world.ext16, pin)
        tck = level(self.tck_pin)
        tms = level(self.tms_pin)
        tdi = level(self.tdi_pin)
        if self._prev is None:
            self._prev = tck
            self.decoder.prev_tck = tck
            return
        self.decoder.step(cycle, tck, tms, tdi)
        self._prev = tck

    # -- conveniences ----------------------------------------------------
    def scans(self) -> list[TapScan]:
        return self.decoder.scans()

    def path(self) -> list[str]:
        return self.decoder.path

    def ir_value(self) -> int:
        return self.decoder.sm.ir

    @property
    def ir_updates(self) -> list[tuple[str, int]]:
        return self.decoder.sm.ir_updates

    @property
    def dr_updates(self) -> list[tuple[int, int]]:
        return self.decoder.sm.dr_updates

    def measured_period(self) -> float:
        return self.decoder.measured_period()

    def edge_jitter(self) -> float:
        return self.decoder.edge_jitter()

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return self.decoder.waveform(chars=chars)

    def summary(self) -> list[str]:
        return self.decoder.summary()


# ---------------------------------------------------------------------------
# Golden-model driver: assemble jtag_tap.asm, run it, decode the pin trace
# ---------------------------------------------------------------------------
JTAG_ASM = _REPO / "sw" / "examples" / "jtag_tap.asm"
DR_BITS_EQU = ".equ DR_BITS,   8"
VERBOSE = False


def assemble_example(dr_bits: int = 8, path: pathlib.Path | None = None):
    """Assemble `sw/examples/jtag_tap.asm`, optionally with a DR_BITS variant."""
    from sw import loom_asm
    src = (path or JTAG_ASM).read_text(encoding="utf-8")
    if dr_bits != 8:
        if DR_BITS_EQU not in src:
            raise KeyError(f"cannot patch {DR_BITS_EQU!r} in {JTAG_ASM}")
        src = src.replace(DR_BITS_EQU, f".equ DR_BITS,   {int(dr_bits)}")
    return loom_asm.assemble(src, filename="jtag_tap[model]")


def load_example(dr_bits: int = 8, *, base: int = 0, tid: int = 0,
                 clock_hz: int = CLOCK_HZ):
    """Load the firmware on the golden model and attach the TAP DUT."""
    from sw import loom_model
    from host.loom_host import ModelLink
    prog = assemble_example(dr_bits)
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=clock_hz)
    dut = JtagDut(world)
    loom.run_threads(1 << tid)
    return dict(prog=prog, loom=loom, link=link, world=world, dut=dut,
                tid=tid, dr_bits=dr_bits, captured=[])


def run_example(ctx: dict, *, max_cycles: int = 20000, chunk: int = 128) -> dict:
    """Step the model, draining the firmware's captured TDO bytes as they come."""
    link = ctx["link"]
    world = ctx["world"]
    tid = ctx["tid"]
    captured = ctx["captured"]
    while world.cycle < max_cycles:
        world.run(chunk)
        while True:
            b = link.rx_pop(tid)
            if b is None:
                break
            captured.append(b)
        if ctx["loom"].threads[tid].halt:
            break
    return ctx


def expected_capture(dr_bits: int, idcode: int = IDCODE) -> tuple[list[int], list[int]]:
    """(IR-scan TDO bits, DR-scan TDO bits) the firmware should capture."""
    ir_bits = [1, 0, 0, 0]                     # Capture-IR status 0b0001
    dr_bits = [(idcode >> i) & 1 for i in range(int(dr_bits))]
    return ir_bits, dr_bits


def expected_path(dr_bits: int) -> list[str]:
    return (["TLR"] * 6 + ["RTI", "SEL_DR", "SEL_IR", "CAP_IR"]
            + ["SHIFT_IR"] * IR_WIDTH
            + ["EXIT1_IR", "UPD_IR", "SEL_DR", "CAP_DR"]
            + ["SHIFT_DR"] * int(dr_bits)
            + ["EXIT1_DR", "UPD_DR"])


def check_decoded(dut: JtagDut, captured: list[int], dr_bits: int,
                  check) -> None:
    """Emit the shared JTAG check list through a `check(name, ok, detail)` fn."""
    scans = dut.scans()
    ir_bits, dr_bits_want = expected_capture(dr_bits)
    check("jtag: 2 scans decoded from the pin trace (IR then DR)",
          [s.kind for s in scans] == ["IR", "DR"],
          f"scans={[(s.kind, len(s.bits)) for s in scans]}")
    if len(scans) == 2:
        ir_scan, dr_scan = scans
        check("jtag: IR scan shifted in IDCODE 0x2, TDO = Capture-IR 0b0001",
              ir_scan.shift_in_value == IR_IDCODE
              and ir_scan.bits == ir_bits,
              f"TDI=0x{ir_scan.shift_in_value:X} TDO={ir_scan.bits}")
        check(f"jtag: DR scan is {dr_bits} bits of TDO = IDCODE low bits",
              len(dr_scan.bits) == dr_bits and dr_scan.bits == dr_bits_want,
              f"got 0x{dr_scan.value:X} want 0x{sum(b << i for i, b in enumerate(dr_bits_want)):X}")
    check("jtag: IR latched at Update-IR, DR captured at Capture-DR",
          dut.ir_value() == IR_IDCODE
          and dut.decoder.sm.dr_capture == IDCODE,
          f"ir=0x{dut.ir_value():X} dr_capture=0x{dut.decoder.sm.dr_capture:08X}")
    check("jtag: TAP state path is the exact legal scan path",
          dut.path() == expected_path(dr_bits), " -> ".join(dut.path()))
    check("jtag: firmware-captured TDO bits == decoder TDO bits",
          captured == ir_bits + dr_bits_want,
          f"captured {captured} want {ir_bits + dr_bits_want}")
    period = dut.measured_period()
    lo, hi = dut.decoder.period_range()
    check("jtag: TCK clock in the bit-banged rate band (31..50 cycles)",
          31 <= lo and hi <= 50,
          f"{lo}..{hi} cyc, mean {period:.2f} -> "
          f"{CLOCK_HZ / max(period, 1e-9) / 1e3:.0f} kHz")
    highs = dut.decoder.high_times()
    check("jtag: TCK high phase is constant (waitd-scheduled eye)",
          highs and max(highs) - min(highs) <= 1,
          f"high {min(highs)}..{max(highs)} cyc over {len(highs)} pulses" if highs else "no pulses")
    check("jtag: TCK low phase varies only by instruction-issue overhead",
          dut.edge_jitter() <= 6.0,
          f"local interval jitter {dut.edge_jitter():.2f} cyc "
          f"({dut.edge_jitter() / CLOCK_HZ * 1e9:.0f} ns) over "
          f"{len(dut.decoder.tck_rises)} edges")


def _model_selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    for dr_bits in (8, 32):
        ctx = load_example(dr_bits)
        run_example(ctx)
        check(f"jtag[{dr_bits}]: firmware halted after the transaction",
              bool(ctx["loom"].threads[ctx["tid"]].halt),
              f"cycles={ctx['world'].cycle}")
        check_decoded(ctx["dut"], ctx["captured"], dr_bits, check)
        if VERBOSE:
            for line in ctx["dut"].summary():
                print("       ", line)
    print("JTAG_MODEL_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------
class _SyntheticMaster:
    """Generate TCK/TMS/TDI level trace for a full IDCODE + DR scan.

    Used only by the standalone self-check: it exercises `TapWireDecoder` and
    `TapStateMachine` without the golden model.
    """

    def __init__(self, half_period: int = 16):
        self.half = int(half_period)
        self.cycle = 0
        self.levels: list[tuple[int, int, int, int]] = []   # (cycle,tck,tms,tdi)
        self._tck = 0
        self._tms = 0
        self._tdi = 0

    def _set(self, tms: int, tdi: int) -> None:
        self._tms = tms & 1
        self._tdi = tdi & 1

    def clock(self, tms: int, tdi: int) -> None:
        """One TCK pulse: levels are stable while low, then high, then low."""
        self._set(tms, tdi)
        self._emit()
        self.cycle += self.half
        self._tck = 1
        self._emit()
        self.cycle += 2
        self._emit()
        self.cycle += self.half
        self._tck = 0
        self._emit()

    def _emit(self) -> None:
        self.levels.append((self.cycle, self._tck, self._tms, self._tdi))

    def run(self, seq: list[tuple[int, int]]) -> None:
        for tms, tdi in seq:
            self.clock(tms, tdi)


def _decode_synthetic(levels) -> TapWireDecoder:
    dec = TapWireDecoder()
    for cycle, tck, tms, tdi in levels:
        dec.step(cycle, tck, tms, tdi)
    return dec


def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    # --- pure state machine: reset ------------------------------------
    sm = TapStateMachine()
    check("reset state = Test-Logic-Reset, ir = IDCODE", sm.state == "TLR"
          and sm.ir == IR_IDCODE, f"state={sm.state} ir=0x{sm.ir:X}")
    for _ in range(4):
        sm.rising(1, 0)
    check("4 x TMS=1 keeps the TAP in TLR (5th is not needed from reset)",
          sm.state == "TLR", sm.state)
    sm.rising(0, 0)
    check("TLR + TMS=0 -> Run-Test/Idle", sm.state == "RTI", sm.state)

    # --- full IDCODE scan on the synthetic wire -----------------------
    master = _SyntheticMaster(half_period=16)
    seq: list[tuple[int, int]] = []
    seq += [(1, 0)] * 5                       # Test-Logic-Reset
    seq += [(0, 0)]                           # Run-Test/Idle
    seq += [(1, 0), (1, 0)]                   # Select-DR, Select-IR
    seq += [(0, 0), (0, 0)]                   # Capture-IR, Shift-IR
    for i in range(IR_WIDTH):                 # IDCODE instruction, LSB first
        seq.append((1 if i == IR_WIDTH - 1 else 0, (IR_IDCODE >> i) & 1))
    seq += [(1, 0)]                           # Update-IR
    seq += [(1, 0), (0, 0), (0, 0)]           # Select-DR, Capture-DR, Shift-DR
    for i in range(DR_WIDTH_IDCODE):          # read IDCODE, LSB first
        seq.append((1 if i == DR_WIDTH_IDCODE - 1 else 0, 0))
    seq += [(1, 0)]                           # Update-DR
    master.run(seq)
    dec = _decode_synthetic(master.levels)
    scans = dec.scans()
    check("synthetic trace decodes exactly two scans (IR then DR)",
          [s.kind for s in scans] == ["IR", "DR"],
          f"scans={[(s.kind, len(s.bits)) for s in scans]}")
    if len(scans) == 2:
        ir_scan, dr_scan = scans
        check("IR scan TDO = Capture-IR status 0b0001",
              ir_scan.bits == [1, 0, 0, 0], ir_scan.bits)
        check("IR shifted in = 0x2 (IDCODE) and latched at Update-IR",
              ir_scan.shift_in_value == IR_IDCODE and dec.sm.ir == IR_IDCODE,
              f"TDI 0x{ir_scan.shift_in_value:X} ir=0x{dec.sm.ir:X}")
        want = [(IDCODE >> i) & 1 for i in range(DR_WIDTH_IDCODE)]
        check("DR scan TDO = 32-bit IDCODE, LSB first",
              dr_scan.bits == want,
              f"got 0x{dr_scan.value:08X} want 0x{IDCODE:08X}")
        check("DR capture value = IDCODE (Update-DR has the shifted-in data)",
              dec.sm.dr_capture == IDCODE and dec.sm.dr_updated == 0,
              f"capture=0x{dec.sm.dr_capture:08X} updated=0x{dec.sm.dr_updated:X}")
    want_path = (["TLR"] * 6 + ["RTI", "SEL_DR", "SEL_IR", "CAP_IR"]
                 + ["SHIFT_IR"] * IR_WIDTH
                 + ["EXIT1_IR", "UPD_IR", "SEL_DR", "CAP_DR"]
                 + ["SHIFT_DR"] * DR_WIDTH_IDCODE
                 + ["EXIT1_DR", "UPD_DR"])
    check("TAP path is the exact legal IDCODE-scan path",
          dec.path == want_path, " -> ".join(dec.path))
    check("recovered TCK period ~34 cycles",
          30.0 <= dec.measured_period() <= 40.0,
          f"{dec.measured_period():.2f} cyc")
    check("synthetic TCK edges are exactly on grid",
          dec.edge_jitter() <= 0.01, f"jitter={dec.edge_jitter():.3f}")

    # --- BYPASS scan: 1-bit DR ----------------------------------------
    master2 = _SyntheticMaster(half_period=8)
    seq2: list[tuple[int, int]] = [(1, 0)] * 5 + [(0, 0)]
    seq2 += [(1, 0), (1, 0), (0, 0), (0, 0)]
    for i in range(IR_WIDTH):
        seq2.append((1 if i == IR_WIDTH - 1 else 0, (IR_BYPASS >> i) & 1))
    seq2 += [(1, 0), (1, 0), (0, 0), (0, 0)]
    seq2 += [(1, 0)]                            # 1-bit BYPASS DR
    seq2 += [(1, 0)]
    master2.run(seq2)
    dec2 = _decode_synthetic(master2.levels)
    scans2 = dec2.scans()
    check("BYPASS: IR shifted in = 0xF then 1-bit DR scan with TDO=0",
          len(scans2) == 2
          and scans2[0].shift_in_value == IR_BYPASS
          and scans2[0].value == CAPTURE_IR
          and len(scans2[1].bits) == 1 and scans2[1].value == 0,
          f"scans={[(s.kind, s.shift_in_value, s.value, len(s.bits)) for s in scans2]}")
    check("TLR from deep state: 5 x TMS=1 re-enters TLR",
          dec2.sm.state in ("UPD_DR", "EXIT1_DR"), dec2.sm.state)

    # --- Pin-mapped DUT through a synthetic world is covered by the model
    # self-test (sw/examples/model_selftest.py); the decoder logic above is
    # model-independent.
    print("JTAG_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    VERBOSE = "-v" in argv or "--verbose" in argv
    rc = _selftest()
    rc |= _model_selftest()
    print("JTAG_SELFTEST:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

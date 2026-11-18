#!/usr/bin/env python3
"""NEC IR remote-control far-end model + independent frame decoder.

Companion to `sw/examples/ir_nec_tx.asm`.  The chip is the **IR remote
transmitter**: it drives gate of an IR LED (high = LED on / carrier burst) and
synthesises the 38 kHz carrier itself from the event scheduler
(`setp 0x1B6A` = 438.625 cycles per half period -> 38.00 kHz at 33.33 MHz).

What is modelled
----------------
* `NecDecoder` — cycle-level receiver: detects carrier marks (runs of ~877-cycle
  toggles) and spaces (long low), measures every mark and space in cycles,
  classifies the pulse-distance bits, assembles the 32-bit NEC payload
  (address, ~address, command, ~command), checks the complements, measures the
  carrier frequency / duty / edge jitter from the mark edges, and reports the
  lead mark / lead space / bit timings.
* `NecFarEnd` — a `FarEnd` plugin attaching the decoder to the chip's pin.
* `build_payload`, `synthetic_events` — reference encoder + event-driven
  synthetic trace for the standalone self-check (no golden model needed; the
  decoder only needs edges plus mark-end "gap" probes).

NEC frame assumed (all times nominal):
    9 ms mark | 4.5 ms space | 32 x (562.5 us mark + 562.5/1687.5 us space)
    | 562.5 us final mark | idle low
Bit 0 = 562.5 us space, bit 1 = 1687.5 us space; data is transmitted LSB-first
per byte, address first.

Run `python -m host.ir_nec` for the standalone self-check plus the golden-model
run of ir_nec_tx.asm.
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field
from fractions import Fraction

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sw.protocols import Clock124, FarEnd, WireWorld, format_wave   # noqa: E402

__all__ = [
    "CLOCK_HZ", "CARRIER_HZ", "IR_PIN", "HALF124", "HALF_CYCLES",
    "MARK_CYCLES", "LEAD_MARK_CYCLES", "LEAD_SPACE_CYCLES",
    "BIT0_SPACE_CYCLES", "BIT1_SPACE_CYCLES",
    "NecFrame", "NecDecoder", "NecFarEnd", "build_payload",
    "payload_words", "synthetic_events", "assemble_example", "load_example",
    "run_example", "check_decoded",
]

CLOCK_HZ = 33_333_333
CARRIER_HZ = 38_000
IR_PIN = 10                      # P[10] = uo[2]
# firmware constants: HALF = 0x1B6A (7018 in 12.4 fixed point)
HALF124 = 0x1B6A
HALF_CYCLES = HALF124 / 16.0                       # 438.625 cycles
MARK_PERIODS = 22                                  # 562.5 us mark = 22 carrier periods
LEAD_PERIODS = 342                                 # 9 ms lead mark
MARK_CYCLES = (2 * MARK_PERIODS - 1) * HALF_CYCLES  # 18860.875
LEAD_MARK_CYCLES = (2 * LEAD_PERIODS - 1) * HALF_CYCLES  # 299580.875
LEAD_SPACE_CYCLES = 9e-3 * CLOCK_HZ - LEAD_MARK_CYCLES   # nominal 4.5 ms = 150000
LEAD_SPACE_CYCLES = 150_000
BIT0_SPACE_CYCLES = 18_750                         # 562.5 us
BIT1_SPACE_CYCLES = 56_250                         # 1687.5 us
MARK_NOMINAL = 18_750                              # 562.5 us in cycles
FINAL_MARK = MARK_CYCLES
GAP_CYCLES = int(4 * HALF_CYCLES)                  # 1754: no edge for this long ends a mark


def _fixed124(cycles: float) -> int:
    return int(round(cycles * 16.0))


# ---------------------------------------------------------------------------
# Payload / reference frame
# ---------------------------------------------------------------------------
def payload_bits(address: int, command: int) -> list[int]:
    """32 NEC payload bits in transmission order (LSB first per byte)."""
    address &= 0xFF
    command &= 0xFF
    bits: list[int] = []
    for byte in (address, (~address) & 0xFF, command, (~command) & 0xFF):
        bits += [(byte >> i) & 1 for i in range(8)]
    return bits


def payload_words(address: int, command: int) -> list[int]:
    """The two DMEM words ir_nec_tx.asm reads (bits 0..15 and 16..31)."""
    bits = payload_bits(address, command)
    return [sum(bits[i] << i for i in range(16)),
            sum(bits[16 + i] << i for i in range(16))]


def build_payload(address: int, command: int) -> list[int]:
    """Reference event list: [(kind, cycles)] for one NEC frame."""
    bits = payload_bits(address, command)
    events: list[tuple[str, float]] = [("mark", LEAD_MARK_CYCLES),
                                       ("space", LEAD_SPACE_CYCLES)]
    for b in bits:
        events.append(("mark", MARK_CYCLES))
        events.append(("space", BIT1_SPACE_CYCLES if b else BIT0_SPACE_CYCLES))
    events.append(("mark", FINAL_MARK))
    return events


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------
@dataclass
class NecFrame:
    """One decoded NEC frame plus carrier / timing measurements."""

    address: int = 0
    command: int = 0
    address_ok: bool = False
    command_ok: bool = False
    bits: list[int] = field(default_factory=list)
    marks: list[int] = field(default_factory=list)      # cycles
    spaces: list[int] = field(default_factory=list)
    carrier_hz: float = 0.0
    carrier_duty: float = 0.0
    half_period: float = 0.0
    edge_residual_max: float = 0.0
    lead_mark: int = 0
    lead_space: int = 0
    final_mark: int = 0
    bit0_count: int = 0
    bit1_count: int = 0
    start_cycle: int = 0
    end_cycle: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.lead_mark_ok and self.lead_space_ok and self.address_ok
                and self.command_ok and 24_000 <= self.carrier_hz <= 56_000
                and not self.errors)

    @property
    def lead_mark_ok(self) -> bool:
        return abs(self.lead_mark - MARK_NOMINAL * 16) < MARK_NOMINAL * 3

    @property
    def lead_space_ok(self) -> bool:
        return 120_000 <= self.lead_space <= 190_000

    def text(self) -> str:
        return (f"NEC addr=0x{self.address:02X} cmd=0x{self.command:02X} "
                f"(~addr {'ok' if self.address_ok else 'BAD'}, "
                f"~cmd {'ok' if self.command_ok else 'BAD'}) "
                f"lead={self.lead_mark / CLOCK_HZ * 1e6:.2f}us/"
                f"{self.lead_space / CLOCK_HZ * 1e6:.0f}us "
                f"carrier={self.carrier_hz / 1e3:.2f}kHz "
                f"duty={self.carrier_duty * 100:.1f}% "
                f"marks={len(self.marks)} spaces={len(self.spaces)} "
                f"bits0={self.bit0_count} bits1={self.bit1_count}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"NecFrame({self.text()})"


class NecDecoder:
    """IR receiver: per-cycle pin level -> carrier marks -> NEC frame."""

    def __init__(self, *, clock_hz: int = CLOCK_HZ):
        self.clock_hz = int(clock_hz)
        self.half = clock_hz / (2.0 * CARRIER_HZ)      # 438.596 cycles
        self.gap = int(4 * self.half)
        self.state = "idle"
        self.prev_level = 0
        self.wave: list[tuple[int, int]] = [(0, 0)]
        self.last_edge = 0
        self.mark_start = 0
        self.last_fall = 0
        self.marks: list[int] = []
        self.spaces: list[int] = []
        self.carrier_edges: list[int] = []
        self.high_edges: list[tuple[int, int]] = []   # (rise, fall) pairs
        self._rise: int | None = None
        self.frames: list[NecFrame] = []
        self._done = False

    # -- public ----------------------------------------------------------
    def step(self, cycle: int, level: int) -> None:
        level &= 1
        # a mark ends when the carrier stops toggling (long low)
        if (self.state == "mark" and level == 0
                and cycle - self.last_edge > self.gap):
            self.marks.append(self.last_edge - self.mark_start)
            self.mark_start = -1
            self.state = "space"
        if level != self.prev_level:
            self.wave.append((cycle, level))
            if self.state == "idle":
                if level == 1:
                    self.mark_start = cycle
                    self.last_edge = cycle
                    self._rise = cycle
                    self.carrier_edges.append(cycle)
                    self.state = "mark"
            elif self.state == "mark":
                self.carrier_edges.append(cycle)
                self.last_edge = cycle
                if level == 0:
                    self.last_fall = cycle
                    if self._rise is not None:
                        self.high_edges.append((self._rise, cycle))
                        self._rise = None
                else:
                    self._rise = cycle
            elif self.state == "space":
                if level == 1:
                    self.spaces.append(cycle - self.last_fall)
                    self.mark_start = cycle
                    self.last_edge = cycle
                    self._rise = cycle
                    self.carrier_edges.append(cycle)
                    self.state = "mark"
            self.prev_level = level
        if not self._done and len(self.marks) >= 34 and len(self.spaces) >= 33:
            self._finish()

    def flush(self) -> None:
        if not self._done and len(self.marks) >= 34 and len(self.spaces) >= 33:
            self._finish()

    # -- decode ----------------------------------------------------------
    def _finish(self) -> None:
        self._done = True
        self.state = "done"
        fr = NecFrame()
        fr.start_cycle = self.marks and 0
        fr.marks = list(self.marks)
        fr.spaces = list(self.spaces)
        fr.lead_mark = self.marks[0]
        fr.lead_space = self.spaces[0]
        fr.final_mark = self.marks[33]
        threshold = (BIT0_SPACE_CYCLES + BIT1_SPACE_CYCLES) / 2.0
        bits: list[int] = []
        for i in range(32):
            mark = self.marks[1 + i]
            space = self.spaces[1 + i]
            if not (MARK_NOMINAL * 0.6 <= mark <= MARK_NOMINAL * 1.6):
                fr.errors.append(f"bit {i} mark {mark} cycles out of range")
            bit = 1 if space > threshold else 0
            bits.append(bit)
        fr.bits = bits
        fr.bit0_count = bits.count(0)
        fr.bit1_count = bits.count(1)
        addr = sum(bits[i] << i for i in range(8))
        not_addr = sum(bits[8 + i] << i for i in range(8))
        cmd = sum(bits[16 + i] << i for i in range(8))
        not_cmd = sum(bits[24 + i] << i for i in range(8))
        fr.address, fr.command = addr, cmd
        fr.address_ok = ((addr ^ not_addr) & 0xFF) == 0xFF
        fr.command_ok = ((cmd ^ not_cmd) & 0xFF) == 0xFF
        # carrier: edge intervals *inside* the marks (the long space gaps
        # between marks are excluded)
        edges = self.carrier_edges
        intervals = [b - a for a, b in zip(edges, edges[1:])
                     if b - a <= self.gap]
        if len(intervals) >= 8:
            mean = sum(intervals) / len(intervals)
            fr.half_period = mean
            fr.carrier_hz = self.clock_hz / (2.0 * mean)
            fr.edge_residual_max = max(abs(iv - mean) for iv in intervals)
            high = sum(f - r for r, f in self.high_edges)
            total = sum(intervals)
            fr.carrier_duty = high / total if total else 0.0
        self.frames.append(fr)


class NecFarEnd(FarEnd):
    """IR receiver attached to a `WireWorld` (decode only)."""

    name = "ir-nec-receiver"

    def __init__(self, world: WireWorld, *, pin: int = IR_PIN,
                 clock_hz: int | None = None):
        self.world = world
        self.pin = int(pin)
        hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.decoder = NecDecoder(clock_hz=hz)
        world.attach(self)

    @property
    def frames(self) -> list[NecFrame]:
        return self.decoder.frames

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        self.decoder.step(cycle, (pins_out16 >> self.pin) & 1)

    def flush(self) -> None:
        self.decoder.flush()

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {"IR": format_wave(self.decoder.wave, chars=chars)}


# ---------------------------------------------------------------------------
# Synthetic event trace (edge-driven: the decoder only needs edges + gaps)
# ---------------------------------------------------------------------------
def _mark_edges(t0: int, periods: int) -> tuple[list[tuple[int, int]], int]:
    """Carrier edges of a mark starting high at t0; returns (events, end)."""
    clk = Clock124(HALF124)
    t = t0
    level = 1
    events = [(t, 1)]
    for _ in range(2 * periods - 1):
        t += clk.advance()
        level ^= 1
        events.append((t, level))
    return events, t


def synthetic_events(address: int, command: int,
                     clock_hz: int = CLOCK_HZ) -> list[tuple[int, int]]:
    """Event-driven trace: (cycle, level) at carrier edges + mark-end probes.

    A real NEC frame is ~2.27 Mcycles; the decoder needs only the edges plus a
    low probe after each mark's last falling edge to detect the mark end, so
    this trace stays tiny.
    """
    bits = payload_bits(address, command)
    phase: list[tuple[str, int]] = [("mark", LEAD_PERIODS),
                                    ("space", LEAD_SPACE_CYCLES)]
    for b in bits:
        phase.append(("mark", MARK_PERIODS))
        phase.append(("space", BIT1_SPACE_CYCLES if b else BIT0_SPACE_CYCLES))
    phase.append(("mark", MARK_PERIODS))
    events: list[tuple[int, int]] = [(0, 0)]
    t = int(0.5 * HALF124)          # a few cycles of idle low
    last_low = t
    for kind, dur in phase:
        if kind == "mark":
            ev, end = _mark_edges(t, dur)
            events.extend(ev)
            last_low = end
            # probe the line low well after the last edge: ends the mark
            events.append((last_low + GAP_CYCLES + 10, 0))
            t = last_low
        else:
            # space ends at the next mark's first rising edge
            t = last_low + dur
    return events


# ---------------------------------------------------------------------------
# Golden-model driver
# ---------------------------------------------------------------------------
IR_ASM = _REPO / "sw" / "examples" / "ir_nec_tx.asm"
VERBOSE = False


def assemble_example(path: pathlib.Path | None = None):
    from sw import loom_asm
    src = (path or IR_ASM).read_text(encoding="utf-8")
    return loom_asm.assemble(src, filename="ir_nec_tx[model]")


def load_example(address: int = 0x04, command: int = 0x08, *, base: int = 0,
                 tid: int = 0, clock_hz: int = CLOCK_HZ):
    from sw import loom_model
    from host.loom_host import ModelLink
    prog = assemble_example()
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    words = payload_words(address, command)
    loom.load_dmem(words, 0)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=clock_hz)
    far = NecFarEnd(world)
    loom.run_threads(1 << tid)
    return dict(prog=prog, loom=loom, link=link, world=world, far=far, tid=tid,
                address=address, command=command)


def run_example(ctx: dict, *, max_cycles: int = 2_600_000,
                chunk: int = 8192) -> dict:
    world, far = ctx["world"], ctx["far"]
    while world.cycle < max_cycles:
        world.run(chunk)
        if far.frames:
            break
        if ctx["loom"].threads[ctx["tid"]].halt:
            break
    world.run(int(GAP_CYCLES * 2))
    far.flush()
    return ctx


def expected_events(address: int, command: int) -> tuple[int, int]:
    """(number of marks, number of spaces) in one NEC frame."""
    return 34, 33


def check_decoded(far: NecFarEnd, ctx: dict, check) -> None:
    """Shared NEC check list through a `check(name, ok, detail)` function."""
    frames = far.frames
    check("ir_nec: frame captured from the pin trace", len(frames) >= 1,
          f"{len(frames)} frames, errors={far.decoder.frames[0].errors if frames else []}")
    if not frames:
        return
    fr = frames[0]
    check("ir_nec: 9 ms lead mark + 4.5 ms lead space",
          abs(fr.lead_mark - LEAD_MARK_CYCLES) < 3000
          and abs(fr.lead_space - LEAD_SPACE_CYCLES) < 5000,
          f"lead mark {fr.lead_mark} cyc ({fr.lead_mark / CLOCK_HZ * 1e6:.2f} us), "
          f"space {fr.lead_space} cyc ({fr.lead_space / CLOCK_HZ * 1e6:.1f} us)")
    check("ir_nec: 34 marks / 33 spaces (lead + 32 bits + final mark)",
          len(fr.marks) >= 34 and len(fr.spaces) >= 33,
          f"marks={len(fr.marks)} spaces={len(fr.spaces)}")
    check("ir_nec: 32 pulse-distance bits (16 ones / 16 zeros by construction)",
          fr.bit0_count + fr.bit1_count == 32 and fr.bit0_count == 16
          and fr.bit1_count == 16,
          f"zeros={fr.bit0_count} ones={fr.bit1_count}")
    check("ir_nec: message address/cmd byte-exact with complements",
          fr.address == ctx["address"] and fr.command == ctx["command"]
          and fr.address_ok and fr.command_ok,
          f"addr=0x{fr.address:02X} cmd=0x{fr.command:02X} "
          f"~addr={'ok' if fr.address_ok else 'BAD'} "
          f"~cmd={'ok' if fr.command_ok else 'BAD'}")
    check("ir_nec: 38 kHz carrier recovered from the mark edges",
          abs(fr.carrier_hz - CARRIER_HZ) < 0.5e3
          and fr.edge_residual_max <= 1.0,
          f"{fr.carrier_hz / 1e3:.3f} kHz, half period {fr.half_period:.3f} cyc, "
          f"edge residual <= {fr.edge_residual_max:.2f} cyc")
    check("ir_nec: carrier duty cycle ~50 %",
          abs(fr.carrier_duty - 0.5) < 0.02,
          f"duty {fr.carrier_duty * 100:.1f} %")
    check("ir_nec: 562.5 us bit marks (-0.6 %/nominal window)",
          all(MARK_NOMINAL * 0.9 <= fr.marks[1 + i] <= MARK_NOMINAL * 1.1
              for i in range(32)),
          f"mark range {min(fr.marks[1:33])}..{max(fr.marks[1:33])} cycles")


def _model_selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    ctx = load_example(0x04, 0x08)
    run_example(ctx)
    check_decoded(ctx["far"], ctx, check)
    if VERBOSE and ctx["far"].frames:
        print("       ", ctx["far"].frames[0].text())
    print("IR_NEC_MODEL_SELFTEST:", "PASS" if ok else "FAIL")
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

    check("NEC payload words pack 32 bits (0x04/0x08 -> 04 FB 08 F7)",
          payload_bits(0x04, 0x08) ==
          [0, 0, 1, 0, 0, 0, 0, 0,
           1, 1, 0, 1, 1, 1, 1, 1,
           0, 0, 0, 1, 0, 0, 0, 0,
           1, 1, 1, 0, 1, 1, 1, 1],
          f"words={[hex(w) for w in payload_words(0x04, 0x08)]}")

    events = synthetic_events(0x04, 0x08)
    dec = NecDecoder()
    for cycle, level in events:
        dec.step(cycle, level)
    dec.flush()
    check("synthetic: NEC frame decodes", len(dec.frames) == 1,
          f"{len(dec.frames)} frames, errors={dec.frames[0].errors if dec.frames else []}")
    if dec.frames:
        fr = dec.frames[0]
        check("synthetic: address 0x04 / command 0x08 with complements",
              fr.address == 0x04 and fr.command == 0x08
              and fr.address_ok and fr.command_ok, fr.text())
        check("synthetic: 32 bits, 16 ones / 16 zeros",
              fr.bit0_count == 16 and fr.bit1_count == 16,
              f"0s={fr.bit0_count} 1s={fr.bit1_count}")
        check("synthetic: 38 kHz carrier / 50 % duty",
              abs(fr.carrier_hz - CARRIER_HZ) < 500
              and abs(fr.carrier_duty - 0.5) < 0.02,
              f"{fr.carrier_hz / 1e3:.3f} kHz, duty {fr.carrier_duty * 100:.1f} %")
        check("synthetic: lead mark ~9 ms, lead space ~4.5 ms",
              abs(fr.lead_mark - 299_580) < 50
              and abs(fr.lead_space - 150_000) < 50,
              f"lead mark {fr.lead_mark} cyc, space {fr.lead_space} cyc")
        check("synthetic: bit marks ~562.5 us",
              all(abs(fr.marks[1 + i] - 18_860) <= 2 for i in range(32)),
              f"{min(fr.marks[1:33])}..{max(fr.marks[1:33])} cycles")
        check("synthetic: carrier edges on the 12.4 half-period grid",
              fr.edge_residual_max <= 1.0,
              f"half period {fr.half_period:.3f} cyc, "
              f"residual <= {fr.edge_residual_max:.2f} cyc")
        check("synthetic: events-per-frame small (edge-driven trace)",
              len(events) < 4000, f"{len(events)} events "
              f"(real frame {2272000} cycles)")
    print("IR_NEC_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    VERBOSE = "-v" in argv or "--verbose" in argv
    rc = _selftest()
    rc |= _model_selftest()
    print("IR_NEC_SELFTEST:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

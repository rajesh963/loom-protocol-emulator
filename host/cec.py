#!/usr/bin/env python3
"""HDMI-CEC (Consumer Electronics Control) receiver + frame decoder.

Companion to `sw/examples/cec_tx.asm`.  The chip is the **CEC initiator**: it
drives the single-wire open-drain CEC line (pull low for the low phase, release
for the high phase; a real board adds the standard CEC pull-up and the HDMI
connector — external parts allowed).

What is modelled
----------------
* `CecDecoder` — cycle-level receiver: segments the START bit and every
  data/EOM/ACK bit from the low/high phase durations, decodes the 10-bit blocks
  (8 data bits LSB first + EOM + ACK), assembles the frame (initiator address,
  destination address, opcode, optional operand), and measures the phase
  durations against the CEC 600 us-bit timing.
* `CecFarEnd` — a `FarEnd` plugin attaching the decoder to the chip's pin.
* `encode_reference` functions + `synthetic_levels` for the standalone check.

CEC wire format assumed (initiator, no follower / NACK ACK slots):
    START: 3.7 ms low + 0.8 ms high
    data bit 0: 1.5 ms low + 0.9 ms high
    data bit 1: 0.6 ms low + 1.8 ms high
    block: 8 data bits LSB first, EOM bit, ACK bit
    frame: START + header block (initiator<<4 | destination) + opcode block
           (+ optional operand blocks); the EOM bit is 1 in the last block.
A follower would pull the ACK slot low; this demo is initiator-only, so the ACK
slots are recessive (logical 1 == NACK) and the decoder reports them.

Run `python -m host.cec` for the standalone self-check plus the golden-model
run of cec_tx.asm.
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
    "CLOCK_HZ", "CEC_PIN", "START_LOW", "START_HIGH", "BIT0_LOW",
    "BIT0_HIGH", "BIT1_LOW", "BIT1_HIGH", "BIT_PERIOD", "BLOCK_BITS",
    "CecBlock", "CecFrame", "CecDecoder", "CecFarEnd", "encode_reference",
    "synthetic_levels", "assemble_example", "load_example", "run_example",
    "check_decoded",
]

CLOCK_HZ = 33_333_333
CEC_PIN = 0                      # P[0] = uio[0], open-drain
BLOCK_BITS = 10                  # 8 data + EOM + ACK
START_LOW = 3.7e-3 * CLOCK_HZ
START_HIGH = 0.8e-3 * CLOCK_HZ
BIT0_LOW = 1.5e-3 * CLOCK_HZ
BIT0_HIGH = 0.9e-3 * CLOCK_HZ
BIT1_LOW = 0.6e-3 * CLOCK_HZ
BIT1_HIGH = 1.8e-3 * CLOCK_HZ
BIT_PERIOD = 2.4e-3 * CLOCK_HZ
HEADER = (0x4 << 4) | 0x0        # initiator 0x4 (Playback 1) -> TV
OPCODE = 0x04                    # Image View On


@dataclass
class CecBlock:
    """One 10-bit CEC block."""

    data: int = 0
    eom: int = 0
    ack: int = 0
    low_cycles: list[int] = field(default_factory=list)
    high_cycles: list[int] = field(default_factory=list)

    def text(self) -> str:
        return f"0x{self.data:02X} EOM={self.eom} ACK={self.ack}"


@dataclass
class CecFrame:
    """One decoded CEC frame."""

    initiator: int = 0
    destination: int = 0
    opcode: int | None = None
    operands: list[int] = field(default_factory=list)
    start_low: int = 0
    start_high: int = 0
    blocks: list[CecBlock] = field(default_factory=list)
    bit_period: float = 0.0
    bit_rate: float = 0.0
    phase_residual_max: float = 0.0
    start_cycle: int = 0
    end_cycle: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def start_ok(self) -> bool:
        return (abs(self.start_low - START_LOW) <= START_LOW * 0.1
                and abs(self.start_high - START_HIGH) <= START_HIGH * 0.1)

    @property
    def eom_ok(self) -> bool:
        return bool(self.blocks) and self.blocks[-1].eom == 1

    @property
    def ok(self) -> bool:
        return (self.start_ok and self.eom_ok and not self.errors
                and abs(self.bit_period - BIT_PERIOD) <= BIT_PERIOD * 0.05)

    def text(self) -> str:
        return (f"CEC {self.initiator:#x}->{self.destination:#x} "
                f"opcode={self.opcode if self.opcode is None else hex(self.opcode)} "
                f"blocks={[b.text() for b in self.blocks]} "
                f"start={self.start_low}/{self.start_high} cyc "
                f"bit={self.bit_period:.0f} cyc ({self.bit_rate/1e3:.2f} kbit/s)")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CecFrame({self.text()})"


class CecDecoder:
    """CEC receiver: per-cycle line level -> start bit, blocks, frame."""

    def __init__(self, *, clock_hz: int = CLOCK_HZ):
        self.clock_hz = int(clock_hz)
        self.state = "idle"
        self.prev_level = 1
        self.wave: list[tuple[int, int]] = [(0, 1)]
        self.low_start = 0
        self.high_start = 0
        self.pending_low = 0
        self.start_low = 0
        self.start_high = 0
        self.bits: list[list[int]] = []       # (symbol, low, high)
        self.blocks: list[CecBlock] = []
        self.frame: CecFrame | None = None
        self.frames: list[CecFrame] = []
        self.errors: list[str] = []
        self._block_bits: list[int] = []
        self._block_low: list[int] = []
        self._block_high: list[int] = []
        self._bit_index = 0
        self.last_cycle = 0

    # -- public ----------------------------------------------------------
    def step(self, cycle: int, level: int) -> None:
        self.last_cycle = int(cycle)
        level &= 1
        if level == self.prev_level:
            return
        self.wave.append((cycle, level))
        if level == 0:
            self._on_falling(cycle)
        else:
            self._on_rising(cycle)
        self.prev_level = level

    def flush(self) -> None:
        # the final bit's high phase ends in the idle line (no falling edge):
        # measure it up to the end of capture, then finish the frame
        if self.frame is not None and self.state == "high" and self.pending_low:
            # the final bit's high phase merges into the idle line, so its end
            # is not observable: use the nominal high for the final symbol's
            # timing record (the low phase is what identifies the bit)
            sym = 1 if self.pending_low < 35_000 else 0
            self._record_bit(self.pending_low,
                             BIT1_HIGH if sym else BIT0_HIGH)
            self.pending_low = 0
        if self.frame is not None:
            self._finish()

    # -- edges -----------------------------------------------------------
    def _on_falling(self, cycle: int) -> None:
        if self.state == "idle":
            self.low_start = cycle
            self.state = "low"
            self.frame = CecFrame(start_cycle=cycle)
            self.start_low = 0
            self.start_high = 0
            self._bit_index = 0
            self._block_bits = []
            self._block_low = []
            self._block_high = []
        elif self.state == "high":
            high = cycle - self.high_start
            self._record_bit(self.pending_low, high)
            self.low_start = cycle
            self.state = "low"

    def _on_rising(self, cycle: int) -> None:
        if self.state == "low":
            low = cycle - self.low_start
            if self._bit_index == 0:
                # the START bit has its own (long-low) encoding
                if low > 90_000:
                    self.start_low = low
                    self.high_start = cycle
                    self.state = "high"
                    self._bit_index = 1
                    return
                self.errors.append(f"start bit low {low} cycles (want ~{START_LOW:.0f})")
            self.pending_low = low
            self.high_start = cycle
            self.state = "high"

    def _record_bit(self, low: int, high: int) -> None:
        if self._bit_index == 1 and self.start_low:
            self.start_high = high
            self._bit_index = 2
            return
        if low > 90_000:
            self.errors.append(f"unexpected long low {low} cycles")
        sym = 1 if low < 35_000 else 0
        want_low = BIT1_LOW if sym else BIT0_LOW
        want_high = BIT1_HIGH if sym else BIT0_HIGH
        if abs(low - want_low) > want_low * 0.2:
            self.errors.append(f"bit {low} cycles low (want ~{want_low:.0f})")
        if abs(high - want_high) > want_high * 0.2:
            self.errors.append(f"bit {high} cycles high (want ~{want_high:.0f})")
        self._block_bits.append(sym)
        self._block_low.append(low)
        self._block_high.append(high)
        if len(self._block_bits) == BLOCK_BITS:
            blk = CecBlock(
                data=sum(self._block_bits[i] << i for i in range(8)),
                eom=self._block_bits[8], ack=self._block_bits[9],
                low_cycles=self._block_low, high_cycles=self._block_high)
            self.frame.blocks.append(blk)
            self._block_bits = []
            self._block_low = []
            self._block_high = []
            if blk.eom == 1:
                self._finish()

    def _finish(self) -> None:
        fr, self.frame = self.frame, None
        self.state = "idle"
        fr.start_low = self.start_low
        fr.start_high = self.start_high
        if not fr.blocks:
            self.errors.append("no complete block")
            self.frames.append(fr)
            return
        hdr = fr.blocks[0].data
        fr.initiator = (hdr >> 4) & 0x0F
        fr.destination = hdr & 0x0F
        if len(fr.blocks) >= 2:
            fr.opcode = fr.blocks[1].data
            fr.operands = [b.data for b in fr.blocks[2:]]
        # timing: every data bit should be 2.4 ms (low+high)
        periods = [lo + hi for blk in fr.blocks
                   for lo, hi in zip(blk.low_cycles, blk.high_cycles)]
        if periods:
            fr.bit_period = sum(periods) / len(periods)
            fr.bit_rate = self.clock_hz / fr.bit_period
            fr.phase_residual_max = max(
                abs(p - BIT_PERIOD) for p in periods)
        fr.end_cycle = fr.start_cycle
        fr.errors.extend(self.errors)
        self.frames.append(fr)

    # -- reporting -------------------------------------------------------
    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {"CEC": format_wave(self.wave, chars=chars)}

    def summary(self) -> list[str]:
        return [f"frame {i}: {f.text()} "
                f"delivery={'ok' if f.ok else 'BAD'} "
                f"phase residual <= {f.phase_residual_max:.0f} cyc"
                for i, f in enumerate(self.frames)]


class CecFarEnd(FarEnd):
    """CEC bus receiver attached to a `WireWorld` (decode only)."""

    name = "cec-receiver"

    def __init__(self, world: WireWorld, *, pin: int = CEC_PIN,
                 clock_hz: int | None = None):
        self.world = world
        self.pin = int(pin)
        hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.decoder = CecDecoder(clock_hz=hz)
        world.attach(self)

    @property
    def frames(self) -> list[CecFrame]:
        return self.decoder.frames

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        # open-drain: DIR=1 pulls the bus low, DIR=0 releases it to the pull-up
        level = FarEnd._wire_level(pins_out16, pins_dir8,
                                   self.world.ext16, self.pin)
        self.decoder.step(cycle, level)

    def flush(self) -> None:
        self.decoder.flush()

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return self.decoder.waveform(chars)


# ---------------------------------------------------------------------------
# Reference encoder + synthetic trace
# ---------------------------------------------------------------------------
def encode_bit(sym: int) -> tuple[float, float]:
    return (BIT1_LOW, BIT1_HIGH) if sym else (BIT0_LOW, BIT0_HIGH)


def encode_reference(initiator: int = 0x4, destination: int = 0x0,
                     opcode: int = OPCODE, operands=()) -> dict:
    """Reference frame: [(kind, low_cycles, high_cycles)] including START."""
    blocks: list[tuple[list[int], int]] = []
    header = ((initiator & 0xF) << 4) | (destination & 0xF)
    blocks.append(([(header >> i) & 1 for i in range(8)], 0))   # EOM=0
    blocks.append(([(opcode >> i) & 1 for i in range(8)], 1))   # EOM=1
    for op in operands:
        blocks.append(([(op >> i) & 1 for i in range(8)], 1))
    events: list[tuple[str, float, float]] = [("start", START_LOW, START_HIGH)]
    for data_bits, eom in blocks:
        for b in data_bits:
            lo, hi = encode_bit(b)
            events.append(("bit", lo, hi))
        lo, hi = encode_bit(eom)          # EOM bit
        events.append(("bit", lo, hi))
        lo, hi = encode_bit(1)            # ACK slot: NACK (recessive = logical 1)
        events.append(("bit", lo, hi))
    return {"initiator": initiator, "destination": destination,
            "opcode": opcode, "operands": list(operands),
            "events": events, "blocks": len(blocks)}


def synthetic_levels(enc: dict) -> list[tuple[int, int]]:
    """Per-cycle (cycle, level) trace for a reference CEC frame."""
    cycles: dict[int, int] = {c: 1 for c in range(0, 20)}
    t = 20.0
    for _kind, low, high in enc["events"]:
        for c in range(int(round(t)), int(round(t + low))):
            cycles[c] = 0
        t += low
        for c in range(int(round(t)), int(round(t + high))):
            cycles[c] = 1
        t += high
    last = max(cycles)
    for c in range(last + 1, last + 50):
        cycles[c] = 1
    return sorted(cycles.items())


# ---------------------------------------------------------------------------
# Golden-model driver
# ---------------------------------------------------------------------------
CEC_ASM = _REPO / "sw" / "examples" / "cec_tx.asm"
VERBOSE = False


def assemble_example(path: pathlib.Path | None = None):
    from sw import loom_asm
    src = (path or CEC_ASM).read_text(encoding="utf-8")
    return loom_asm.assemble(src, filename="cec_tx[model]")


def load_example(*, base: int = 0, tid: int = 0, clock_hz: int = CLOCK_HZ):
    from sw import loom_model
    from host.loom_host import ModelLink
    prog = assemble_example()
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=clock_hz)
    far = CecFarEnd(world)
    loom.run_threads(1 << tid)
    return dict(prog=prog, loom=loom, link=link, world=world, far=far, tid=tid)


def run_example(ctx: dict, *, max_cycles: int = 2_000_000,
                chunk: int = 8192) -> dict:
    world, far = ctx["world"], ctx["far"]
    while world.cycle < max_cycles:
        world.run(chunk)
        if far.frames:
            break
        if ctx["loom"].threads[ctx["tid"]].halt:
            break
    world.run(int(5e-3 * CLOCK_HZ))
    far.flush()
    return ctx


def check_decoded(far: CecFarEnd, enc: dict, check) -> None:
    """Shared CEC check list through a `check(name, ok, detail)` function."""
    frames = far.frames
    check("cec: frame captured from the CEC pin trace", len(frames) >= 1,
          f"{len(frames)} frames, errors={far.decoder.errors}")
    if not frames:
        return
    f = frames[0]
    check("cec: START bit 3.7 ms low + 0.8 ms high",
          f.start_ok,
          f"low={f.start_low} cyc ({f.start_low/CLOCK_HZ*1e3:.2f} ms), "
          f"high={f.start_high} cyc ({f.start_high/CLOCK_HZ*1e3:.2f} ms)")
    check("cec: header block initiator 0x4 -> destination 0x0",
          f.initiator == enc["initiator"]
          and f.destination == enc["destination"],
          f"{f.initiator:#x}->{f.destination:#x}")
    check("cec: opcode block = 0x04 (Image View On), EOM=1 ends the frame",
          f.opcode == enc["opcode"] and f.eom_ok,
          f"opcode={f.opcode if f.opcode is None else hex(f.opcode)} "
          f"EOM={f.blocks[-1].eom if f.blocks else None}")
    check("cec: ACK slots are NACK (recessive 1, no follower modelled)",
          all(b.ack == 1 for b in f.blocks),
          f"ACK bits={[b.ack for b in f.blocks]}")
    check("cec: blocks are 8 data bits + EOM + ACK (10-bit blocks)",
          len(f.blocks) == 2
          and all(len(b.low_cycles) == BLOCK_BITS for b in f.blocks),
          f"{len(f.blocks)} blocks")
    check("cec: 2.4 ms data bit period (600 us-bit CEC) on the waitd grid",
          abs(f.bit_period - BIT_PERIOD) <= BIT_PERIOD * 0.02
          and f.phase_residual_max <= BIT_PERIOD * 0.01,
          f"{f.bit_period:.1f} cyc/bit -> {f.bit_rate/1e3:.2f} kbit/s "
          f"(nominal 416.7 bit/s), phase residual <= "
          f"{f.phase_residual_max:.0f} cyc")


def _model_selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    enc = encode_reference()
    ctx = load_example()
    run_example(ctx)
    check("cec_tx: size < 128 words", len(ctx["prog"].words) < 128,
          f"{len(ctx['prog'].words)} words")
    check_decoded(ctx["far"], enc, check)
    if VERBOSE:
        for line in ctx["far"].decoder.summary():
            print("       ", line)
    print("CEC_MODEL_SELFTEST:", "PASS" if ok else "FAIL")
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

    enc = encode_reference()
    levels = synthetic_levels(enc)
    dec = CecDecoder()
    for cycle, level in levels:
        dec.step(cycle, level)
    dec.flush()
    check("synthetic: CEC frame decodes", len(dec.frames) == 1,
          f"{len(dec.frames)} frames, errors={dec.frames[0].errors if dec.frames else []}")
    if dec.frames:
        f = dec.frames[0]
        check("synthetic: initiator/destination/opcode decode",
              f.initiator == 0x4 and f.destination == 0x0
              and f.opcode == 0x04, f.text())
        check("synthetic: 2 x 10-bit blocks, EOM=1 ends the frame",
              len(f.blocks) == 2 and [b.eom for b in f.blocks] == [0, 1],
              f"{[b.text() for b in f.blocks]}")
        check("synthetic: START bit and 2.4 ms bit period recovered",
              f.start_ok and abs(f.bit_period - BIT_PERIOD) < 10,
              f"start={f.start_low}/{f.start_high}, bit={f.bit_period:.1f} cyc")
    print("CEC_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    VERBOSE = "-v" in argv or "--verbose" in argv
    rc = _selftest()
    rc |= _model_selftest()
    print("CEC_SELFTEST:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""MIDI 1.0 far-end receiver + independent message decoder (host side of midi_tx.asm).

Companion to `sw/examples/midi_tx.asm`.  The chip is a MIDI 8N1 UART
transmitter at the standard 31250 baud (midi_tx.asm's `setp 0x42AB` =
1066.6875 cycles/bit at 33.33 MHz -> 31250.6 baud, +0.002 %).

What is modelled
----------------
* `MidiDecoder` — cycle-level UART receiver: detects the start bit (falling
  edge), samples every 8N1 frame at bit centres, validates start/stop framing,
  reconstructs bytes, and assembles MIDI messages (status byte + 1/2 data
  bytes, running-status aware).  Fits the bit period and edge residual from
  the byte start edges (back-to-back frames are exactly 10 bit cells apart).
* `MidiFarEnd` — a `FarEnd` plugin attaching the decoder to the chip's pin.
* `synthetic_levels` — reference 8N1 trace for the standalone self-check.

MIDI messages decoded: 0x8n Note Off, 0x9n Note On (velocity 0 = Note Off),
0xAn poly aftertouch, 0xBn control change, 0xCn program change, 0xDn channel
aftertouch, 0xEn pitch bend, plus system common bytes recorded as raw events.

Run `python -m host.midi` for the standalone self-check plus the golden-model
run of midi_tx.asm (Note On + Note Off + Program Change).
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
    "CLOCK_HZ", "MIDI_BAUD", "MIDI_PIN", "FRAME_BITS", "NOTE_ON",
    "MidiMessage", "MidiDecoder", "MidiFarEnd", "synthetic_levels",
    "assemble_example", "load_example", "run_example", "check_decoded",
]

CLOCK_HZ = 33_333_333
MIDI_BAUD = 31_250
MIDI_PIN = 10                    # P[10] = uo[2]
FRAME_BITS = 10                  # start + 8 data + stop
NOTE_ON = 0x90
NOTE_OFF = 0x80
PROGRAM_CHANGE = 0xC0
BITPER = round(CLOCK_HZ * 16 / MIDI_BAUD) / 16.0     # 1066.6875 cycles

_STATUS_DATA_BYTES = {
    0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xE0: 2,
    0xC0: 1, 0xD0: 1,
}
_TYPE_NAMES = {
    0x80: "Note Off", 0x90: "Note On", 0xA0: "Poly Aftertouch",
    0xB0: "Control Change", 0xC0: "Program Change", 0xD0: "Channel Aftertouch",
    0xE0: "Pitch Bend",
}


@dataclass
class MidiFrame:
    """One decoded 8N1 byte."""

    value: int
    cells: list[int]
    start_cycle: int

    @property
    def start_ok(self) -> bool:
        return bool(self.cells) and self.cells[0] == 0

    @property
    def stop_ok(self) -> bool:
        return len(self.cells) == FRAME_BITS and self.cells[9] == 1


@dataclass
class MidiMessage:
    """One decoded MIDI message."""

    status: int
    data: list[int] = field(default_factory=list)

    @property
    def kind(self) -> int:
        return self.status & 0xF0

    @property
    def channel(self) -> int:
        return self.status & 0x0F

    @property
    def name(self) -> str:
        return _TYPE_NAMES.get(self.kind, f"System 0x{self.status:02X}")

    def text(self) -> str:
        d = " ".join(f"0x{b:02X}" for b in self.data)
        return f"{self.name} ch{self.channel + 1} {d}"


class MidiDecoder:
    """MIDI 8N1 receiver: per-cycle line level -> bytes -> messages."""

    def __init__(self, *, clock_hz: int = CLOCK_HZ, baud: int = MIDI_BAUD):
        self.clock_hz = int(clock_hz)
        self.baud = int(baud)
        self.period = self.clock_hz / float(self.baud)
        self.state = "idle"
        self.prev_level = 1
        self.wave: list[tuple[int, int]] = [(0, 1)]
        self.next_sample = 0.0
        self.cells: list[int] = []
        self.frame_start = 0
        self.frames: list[MidiFrame] = []
        self.bytes: list[int] = []
        self.messages: list[MidiMessage] = []
        self.errors: list[str] = []
        self.start_cycles: list[int] = []
        self._pending: list[int] = []
        self._last_status: int | None = None

    # -- public ----------------------------------------------------------
    def step(self, cycle: int, level: int) -> None:
        level &= 1
        if level != self.prev_level:
            self.wave.append((cycle, level))
            if self.state == "idle" and level == 0:
                self.state = "frame"
                self.frame_start = cycle
                self.cells = []
                self.next_sample = cycle + self.period / 2.0
        self.prev_level = level
        if self.state == "frame" and cycle >= self.next_sample:
            self.cells.append(level)
            self.next_sample += self.period
            if len(self.cells) == 1 and level != 0:
                # not a start bit (glitch / initial line level): abandon
                self.state = "idle"
                return
            if len(self.cells) == FRAME_BITS:
                self._finish_frame()

    def flush(self) -> None:
        if self.state == "frame" and len(self.cells) == FRAME_BITS:
            self._finish_frame()

    # -- decode ----------------------------------------------------------
    def _finish_frame(self) -> None:
        cells = self.cells
        value = sum((cells[1 + i] & 1) << i for i in range(8))
        frame = MidiFrame(value=value, cells=list(cells),
                          start_cycle=self.frame_start)
        if not frame.start_ok:
            self.errors.append("start bit not dominant")
        if not frame.stop_ok:
            self.errors.append("stop bit not recessive")
        self.frames.append(frame)
        if frame.start_ok and frame.stop_ok:
            self.start_cycles.append(self.frame_start)
            self.bytes.append(value)
            self._pending.append(value)
            self._parse()
        self.state = "idle"

    def _parse(self) -> None:
        while self._pending:
            status = self._pending[0]
            if status < 0x80:
                # running status: data bytes after a channel status reuse it
                last = self._last_status
                if last is None:
                    self._pending.pop(0)
                    self.errors.append(f"data byte 0x{status:02X} without status")
                    continue
                n = _STATUS_DATA_BYTES.get(last & 0xF0)
                if n is None or len(self._pending) < n:
                    return
                self.messages.append(
                    MidiMessage(last, list(self._pending[:n])))
                del self._pending[:n]
                continue
            kind = status & 0xF0
            if kind == 0xF0:
                self.messages.append(MidiMessage(status, []))
                self._pending.pop(0)
                continue
            n = _STATUS_DATA_BYTES.get(kind)
            if n is None:
                self._pending.pop(0)
                self.errors.append(f"unknown status 0x{status:02X}")
                continue
            if len(self._pending) < 1 + n:
                return                               # wait for data bytes
            data = self._pending[1:1 + n]
            self._last_status = status
            self.messages.append(MidiMessage(status, list(data)))
            del self._pending[:1 + n]

    # -- measurements ----------------------------------------------------
    def measured_bit_period(self) -> float:
        starts = self.start_cycles
        if len(starts) < 3:
            return 0.0
        p0 = self.period * FRAME_BITS
        idx = [round((t - starts[0]) / p0) for t in starts]
        n = len(idx)
        mm = sum(idx) / n
        mt = sum(starts) / n
        den = sum((m - mm) ** 2 for m in idx)
        if den <= 0:
            return 0.0
        return (sum((m - mm) * (t - mt) for m, t in zip(idx, starts))
                / den) / FRAME_BITS

    def edge_residual_max(self) -> float:
        starts = self.start_cycles
        if len(starts) < 3:
            return 0.0
        p = self.measured_bit_period() * FRAME_BITS
        return max(abs((t - starts[0]) - round((t - starts[0]) / p) * p)
                   for t in starts)

    def summary(self) -> list[str]:
        period = self.measured_bit_period()
        out = [f"MIDI {len(self.bytes)} bytes, {len(self.messages)} messages, "
               f"bit period {period:.4f} cyc "
               f"({self.clock_hz / max(1e-9, period)/1e3:.1f} kBd), "
               f"start-edge residual <= {self.edge_residual_max():.2f} cyc"]
        for m in self.messages:
            out.append(f"  {m.text()}")
        return out

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {"MIDI_TX": format_wave(self.wave, chars=chars)}


class MidiFarEnd(FarEnd):
    """MIDI receiver attached to a `WireWorld` (decode only)."""

    name = "midi-receiver"

    def __init__(self, world: WireWorld, *, pin: int = MIDI_PIN,
                 clock_hz: int | None = None):
        self.world = world
        self.pin = int(pin)
        hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.decoder = MidiDecoder(clock_hz=hz)
        world.attach(self)

    @property
    def messages(self) -> list[MidiMessage]:
        return self.decoder.messages

    @property
    def frames(self) -> list[MidiFrame]:
        return self.decoder.frames

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        self.decoder.step(cycle, (pins_out16 >> self.pin) & 1)

    def flush(self) -> None:
        self.decoder.flush()

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return self.decoder.waveform(chars)


# ---------------------------------------------------------------------------
# Synthetic reference trace
# ---------------------------------------------------------------------------
def encode_frame(value: int) -> list[int]:
    """8N1 bit cells: start(0), data LSB first, stop(1)."""
    return [0] + [(value >> i) & 1 for i in range(8)] + [1]


def synthetic_levels(data, *, clock_hz: int = CLOCK_HZ,
                     period: float | None = None) -> list[tuple[int, int]]:
    """Per-cycle (cycle, level) trace for back-to-back 8N1 frames."""
    p = float(period if period is not None else clock_hz / MIDI_BAUD)
    cycles: dict[int, int] = {c: 1 for c in range(0, 10)}
    t = 10.0
    for byte in data:
        for bit in encode_frame(byte):
            end = t + p
            for c in range(int(round(t)), int(round(end))):
                cycles[c] = bit
            t = end
    last = max(cycles)
    for c in range(last + 1, last + 50):
        cycles[c] = 1
    return sorted(cycles.items())


# ---------------------------------------------------------------------------
# Golden-model driver
# ---------------------------------------------------------------------------
MIDI_ASM = _REPO / "sw" / "examples" / "midi_tx.asm"
VERBOSE = False


def assemble_example(path: pathlib.Path | None = None):
    from sw import loom_asm
    src = (path or MIDI_ASM).read_text(encoding="utf-8")
    return loom_asm.assemble(src, filename="midi_tx[model]")


def load_example(*, base: int = 0, tid: int = 0, clock_hz: int = CLOCK_HZ):
    from sw import loom_model
    from host.loom_host import ModelLink
    prog = assemble_example()
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=clock_hz)
    far = MidiFarEnd(world)
    loom.run_threads(1 << tid)
    return dict(prog=prog, loom=loom, link=link, world=world, far=far, tid=tid)


def run_example(ctx: dict, data, *, max_cycles: int = 120000) -> dict:
    world, link, tid = ctx["world"], ctx["link"], ctx["tid"]
    payload = list(bytearray(data))
    sent = 0
    while world.cycle < max_cycles:
        while sent < len(payload) and link.tx_push(tid, payload[sent]):
            sent += 1
        world.run(64)
        if len(ctx["far"].frames) >= len(payload):
            break
        if ctx["loom"].threads[tid].halt:
            break
    world.run(2 * FRAME_BITS * int(BITPER) + 512)
    ctx["far"].flush()
    ctx["sent"] = sent
    return ctx


def check_decoded(far: MidiFarEnd, ctx: dict, data, check) -> None:
    """Shared MIDI check list through a `check(name, ok, detail)` function."""
    dec = far.decoder
    check("midi: all host bytes serialised as 8N1 frames",
          len(far.frames) >= len(data),
          f"{len(far.frames)} frames, errors={dec.errors}")
    check("midi: every frame has a 0 start bit and a 1 stop bit",
          all(f.start_ok and f.stop_ok for f in far.frames),
          f"{sum(1 for f in far.frames if f.start_ok and f.stop_ok)}/"
          f"{len(far.frames)} valid")
    check("midi: transmitted bytes byte-exact", dec.bytes[:len(data)] == list(data),
          f"got {[hex(b) for b in dec.bytes[:len(data)]]}")
    msgs = far.messages
    check(f"midi: {len(msgs)} messages assembled from the byte stream",
          len(msgs) >= 3, f"{[m.text() for m in msgs]}")
    if len(msgs) >= 3:
        check("midi: Note On 0x90 0x3C 0x64 decoded",
              msgs[0].status == NOTE_ON and msgs[0].data == [0x3C, 0x64],
              msgs[0].text())
        check("midi: Note Off 0x80 0x3C 0x00 decoded",
              msgs[1].status == NOTE_OFF and msgs[1].data == [0x3C, 0x00],
              msgs[1].text())
        check("midi: Program Change 0xC0 0x05 decoded (1 data byte)",
              msgs[2].status == PROGRAM_CHANGE and msgs[2].data == [0x05],
              msgs[2].text())
    period = dec.measured_bit_period()
    check("midi: 31250 baud on the event grid",
          abs(period - BITPER) < 0.02 and dec.edge_residual_max() <= 1.0,
          f"{period:.4f} cyc/bit -> {CLOCK_HZ/period/1e3:.3f} kBd, "
          f"start-edge residual <= {dec.edge_residual_max():.2f} cyc")


def _model_selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    data = bytes([NOTE_ON, 0x3C, 0x64, NOTE_OFF, 0x3C, 0x00,
                  PROGRAM_CHANGE, 0x05])
    ctx = load_example()
    run_example(ctx, data)
    check_decoded(ctx["far"], ctx, data, check)
    check("midi: host bytes all pushed", ctx.get("sent") == len(data),
          f"sent={ctx.get('sent')}")
    if VERBOSE:
        for line in ctx["far"].decoder.summary():
            print("       ", line)
    print("MIDI_MODEL_SELFTEST:", "PASS" if ok else "FAIL")
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

    data = [NOTE_ON, 0x3C, 0x64, NOTE_OFF, 0x3C, 0x00, PROGRAM_CHANGE, 0x05]
    levels = synthetic_levels(data)
    dec = MidiDecoder()
    for cycle, level in levels:
        dec.step(cycle, level)
    dec.flush()
    check("synthetic: bytes decoded byte-exact", dec.bytes == data,
          f"{[hex(b) for b in dec.bytes]}")
    check("synthetic: 3 messages assembled", len(dec.messages) == 3,
          f"{[m.text() for m in dec.messages]}")
    if len(dec.messages) == 3:
        check("synthetic: Note On / Note Off / Program Change",
              dec.messages[0].status == 0x90
              and dec.messages[1].status == 0x80
              and dec.messages[2].status == 0xC0,
              f"{[m.text() for m in dec.messages]}")
    check("synthetic: 31250 baud period recovered",
          abs(dec.measured_bit_period() - BITPER) < 0.02,
          f"{dec.measured_bit_period():.4f} cyc/bit -> "
          f"{CLOCK_HZ/dec.measured_bit_period()/1e3:.3f} kBd")
    check("synthetic: all start edges on the bit grid",
          dec.edge_residual_max() <= 0.75,
          f"max residual {dec.edge_residual_max():.2f} cyc")
    print("MIDI_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    VERBOSE = "-v" in argv or "--verbose" in argv
    rc = _selftest()
    rc |= _model_selftest()
    print("MIDI_SELFTEST:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

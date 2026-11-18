#!/usr/bin/env python3
"""DMX512 far-end receiver + independent frame decoder (host side of dmx_tx.asm).

Companion to `sw/examples/dmx_tx.asm`.  The chip is the DMX512 **transmitter**:
one UART-style data line at 250 kbaud, 8N2 (start, 8 data LSB-first, 2 stop),
preceded by a BREAK (>= 88 us low) and a mark-after-break (MAB, >= 8 us high).
On a real board an RS-485 transceiver turns the single pin into the DMX
differential pair (external parts are allowed by the competition FAQ).

What is modelled
----------------
* `DmxDecoder` — cycle-level receiver state machine: measures the BREAK and MAB
  durations, recovers the 133.3125-cycle bit grid from the slot start bits,
  samples every 11-bit slot at bit centres, validates start/2-stop framing,
  checks the 0x00 start code, keeps the slot values and fits the bit period /
  edge residual from the slot starts.
* `DmxFarEnd` — a `FarEnd` plugin attaching the decoder to the chip's pin.
* `encode_reference` / `synthetic_levels` — reference framing used by the
  standalone self-check (no golden model needed).

DMX512 wire format assumed:
    BREAK (>=88 us low) | MAB (>=8 us high) |
    start-code slot 0x00 | data slot 0 ... data slot 511
each slot = 11 bit cells: start(0), data[7:0] LSB first, stop(1), stop(1).

Run `python -m host.dmx` for the standalone self-check plus the golden-model
run of dmx_tx.asm (a full 512-slot, 513-slot frame).
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
    "CLOCK_HZ", "DMX_BAUD", "DMX_PIN", "SLOT_BITS", "SLOTS",
    "BREAK_MIN_CYCLES", "MAB_MIN_CYCLES", "DmxSlot", "DmxFrame",
    "DmxDecoder", "DmxFarEnd", "encode_reference", "synthetic_levels",
    "assemble_example", "load_example", "run_example", "check_decoded",
]

CLOCK_HZ = 33_333_333
DMX_BAUD = 250_000
DMX_PIN = 10                     # P[10] = uo[2]
SLOT_BITS = 11                   # start + 8 data + 2 stop
SLOTS = 512                      # DMX512 universe size (data slots)
START_CODE = 0x00
BREAK_MIN_CYCLES = 88e-6 * CLOCK_HZ     # 2933.3 cycles
MAB_MIN_CYCLES = 8e-6 * CLOCK_HZ        # 266.7 cycles
NOMINAL_BIT_CYCLES = CLOCK_HZ / DMX_BAUD    # 133.3333


@dataclass
class DmxSlot:
    """One decoded 11-bit slot cell group."""

    value: int = 0
    start_cycle: int = 0
    cells: list[int] = field(default_factory=list)

    @property
    def start_ok(self) -> bool:
        return bool(self.cells) and self.cells[0] == 0

    @property
    def stop_ok(self) -> bool:
        return len(self.cells) == SLOT_BITS and self.cells[9] == 1 \
            and self.cells[10] == 1


@dataclass
class DmxFrame:
    """One decoded DMX512 frame."""

    break_cycles: int = 0
    mab_cycles: int = 0
    start_code: int = 0
    data: list[int] = field(default_factory=list)
    slots: list[DmxSlot] = field(default_factory=list)
    bit_period: float = 0.0
    bit_rate: float = 0.0
    edge_residual_max: float = 0.0
    start_cycle: int = 0
    end_cycle: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def break_ok(self) -> bool:
        return self.break_cycles >= BREAK_MIN_CYCLES

    @property
    def mab_ok(self) -> bool:
        return self.mab_cycles >= MAB_MIN_CYCLES

    @property
    def framing_ok(self) -> bool:
        return bool(self.slots) and all(s.start_ok and s.stop_ok
                                        for s in self.slots)

    @property
    def ok(self) -> bool:
        return (self.break_ok and self.mab_ok and self.start_code == START_CODE
                and self.framing_ok and not self.errors)

    def text(self) -> str:
        head = ",".join(f"{b:02X}" for b in self.data[:8])
        return (f"break={self.break_cycles}cyc "
                f"({self.break_cycles / CLOCK_HZ * 1e6:.1f}us) "
                f"MAB={self.mab_cycles}cyc ({self.mab_cycles / CLOCK_HZ * 1e6:.1f}us) "
                f"start=0x{self.start_code:02X} slots={len(self.data)} "
                f"[{head}{'...' if len(self.data) > 8 else ''}] "
                f"{self.bit_rate/1e3:.2f} kbaud")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DmxFrame({self.text()})"


class DmxDecoder:
    """DMX512 receiver: per-cycle line level -> break/MAB/slot decode."""

    def __init__(self, *, clock_hz: int = CLOCK_HZ, baud: int = DMX_BAUD,
                 slots: int = SLOTS):
        self.clock_hz = int(clock_hz)
        self.baud = int(baud)
        self.slots_expected = int(slots)
        self.period = self.clock_hz / float(self.baud)
        self.frames: list[DmxFrame] = []
        self.errors: list[str] = []
        self.wave: list[tuple[int, int]] = [(0, 1)]
        self.prev_level: int = 1          # the line idles high before a BREAK
        self.state = "idle"
        self._break_start = 0
        self._mab_start = 0
        self._cur: DmxFrame | None = None
        self._slot_start = 0
        self._cells: list[int] = []
        self._next_sample = 0.0

    # -- public ----------------------------------------------------------
    def step(self, cycle: int, level: int) -> None:
        level &= 1
        if level != self.prev_level:
            self.wave.append((cycle, level))
            self._on_edge(cycle, level)
        self.prev_level = level
        if self.state == "slot" and cycle >= self._next_sample:
            self._cells.append(level)
            self._next_sample += self.period
            if len(self._cells) == SLOT_BITS:
                self._finish_slot(cycle)

    def flush(self) -> None:
        """Finalise a partially captured frame (e.g. at end of a capture)."""
        if self.state == "slot" and len(self._cells) >= SLOT_BITS:
            self._finish_slot(self._slot_start)
        if self._cur is not None:
            self._finish_frame()

    # -- edges -----------------------------------------------------------
    def _on_edge(self, cycle: int, level: int) -> None:
        if self.state == "idle":
            if level == 0:
                self._break_start = cycle
                self.state = "break_low"
        elif self.state == "break_low":
            if level == 1:
                dur = cycle - self._break_start
                if dur < BREAK_MIN_CYCLES:
                    self.state = "idle"           # not a BREAK: ignore noise
                    return
                self._cur = DmxFrame(break_cycles=dur, start_cycle=cycle)
                self._mab_start = cycle
                self.state = "mab_high"
        elif self.state == "mab_high":
            if level == 0:
                dur = cycle - self._mab_start
                self._cur.mab_cycles = dur
                if dur < MAB_MIN_CYCLES:
                    self._cur.errors.append(f"MAB {dur} < {MAB_MIN_CYCLES:.0f} cycles")
                self._begin_slot(cycle)
        elif self.state == "between_slots":
            if level == 0:
                self._begin_slot(cycle)
        # in "slot" state, edges inside the slot are expected (data) -- ignored

    # -- slots -----------------------------------------------------------
    def _begin_slot(self, cycle: int) -> None:
        self.state = "slot"
        self._slot_start = cycle
        self._cells = []
        self._next_sample = cycle + self.period / 2.0

    def _finish_slot(self, cycle: int) -> None:
        self._cur.end_cycle = cycle
        cells = self._cells
        slot = DmxSlot(value=sum((cells[1 + i] & 1) << i for i in range(8)),
                       start_cycle=self._slot_start,
                       cells=list(cells))
        if not slot.start_ok:
            self._cur.errors.append(f"slot start bit {cells[0]} != 0")
        if not slot.stop_ok:
            self._cur.errors.append(f"slot stop bits {cells[9]}, {cells[10]} != 1,1")
        self._cur.slots.append(slot)
        if len(self._cur.slots) == 1:
            self._cur.start_code = slot.value
        elif len(self._cur.slots) <= self.slots_expected + 1:
            self._cur.data.append(slot.value)
        self.state = "between_slots"
        if len(self._cur.slots) >= self.slots_expected + 1:
            self._finish_frame()

    def _finish_frame(self) -> None:
        frame, self._cur = self._cur, None
        # bit period from the slot start edges: back-to-back slots are exactly
        # SLOT_BITS bit cells apart
        starts = [s.start_cycle for s in frame.slots]
        if len(starts) >= 4:
            p0 = self.period * SLOT_BITS
            idx = [round((t - starts[0]) / p0) for t in starts]
            n = len(idx)
            mm = sum(idx) / n
            mt = sum(starts) / n
            den = sum((m - mm) ** 2 for m in idx)
            if den > 0:
                slot_p = sum((m - mm) * (t - mt)
                             for m, t in zip(idx, starts)) / den
                frame.bit_period = slot_p / SLOT_BITS
                frame.bit_rate = self.clock_hz / frame.bit_period
                frame.edge_residual_max = max(
                    abs((t - starts[0]) - m * slot_p)
                    for m, t in zip(idx, starts))
        frame.errors.extend(self.errors)
        self.frames.append(frame)
        self.state = "idle"

    # -- rendering -------------------------------------------------------
    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {"DMX": format_wave(self.wave, chars=chars)}

    def summary(self) -> list[str]:
        out = []
        for i, f in enumerate(self.frames):
            out.append(f"frame {i}: {f.text()} "
                       f"delivery={'ok' if f.ok else 'BAD'} "
                       f"bit_period={f.bit_period:.3f} cyc "
                       f"edge_residual<={f.edge_residual_max:.2f} cyc")
        return out


class DmxFarEnd(FarEnd):
    """DMX512 universe receiver attached to a `WireWorld` (decode only)."""

    name = "dmx-receiver"

    def __init__(self, world: WireWorld, *, pin: int = DMX_PIN,
                 clock_hz: int | None = None, slots: int = SLOTS):
        self.world = world
        self.pin = int(pin)
        hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.decoder = DmxDecoder(clock_hz=hz, slots=slots)
        world.attach(self)

    @property
    def frames(self) -> list[DmxFrame]:
        return self.decoder.frames

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        self.decoder.step(cycle, (pins_out16 >> self.pin) & 1)

    def flush(self) -> None:
        self.decoder.flush()

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return self.decoder.waveform(chars)


# ---------------------------------------------------------------------------
# Reference encoder + synthetic trace (standalone checks)
# ---------------------------------------------------------------------------
def encode_slot(value: int) -> list[int]:
    """One DMX slot: start(0), data LSB first, 2 stop(1)."""
    return [0] + [(value >> i) & 1 for i in range(8)] + [1, 1]


def encode_reference(data, *, slots: int = SLOTS,
                     break_cycles: int = 4000, mab_cycles: int = 400) -> dict:
    """Reference frame: break/MAB + start-code slot + padded data slots."""
    data = list(bytearray(data))[:slots]
    data = data + [0] * (slots - len(data))
    cells = encode_slot(START_CODE)
    for b in data:
        cells += encode_slot(b)
    return {"break_cycles": break_cycles, "mab_cycles": mab_cycles,
            "start_code": START_CODE, "data": data, "cells": cells,
            "slot_bits": SLOT_BITS, "slots": slots}


def synthetic_levels(enc: dict, *, clock_hz: int = CLOCK_HZ,
                     period: float | None = None) -> list[tuple[int, int]]:
    """Per-cycle (cycle, level) trace for a reference DMX frame."""
    p = float(period if period is not None else clock_hz / DMX_BAUD)
    cycles: dict[int, int] = {}
    t = 0
    # idle high
    cycles[t] = 1
    t += 10
    # BREAK low
    for c in range(t, t + enc["break_cycles"]):
        cycles[c] = 0
    t += enc["break_cycles"]
    # MAB high
    for c in range(t, t + enc["mab_cycles"]):
        cycles[c] = 1
    t += enc["mab_cycles"]
    for bit in enc["cells"]:
        start = t
        end = t + p
        for c in range(int(round(start)), int(round(end))):
            cycles[c] = bit
        t = end
    last = max(cycles)
    for c in range(last + 1, last + 50):
        cycles[c] = 1
    return sorted(cycles.items())


# ---------------------------------------------------------------------------
# Golden-model driver
# ---------------------------------------------------------------------------
DMX_ASM = _REPO / "sw" / "examples" / "dmx_tx.asm"
VERBOSE = False


def assemble_example(slots: int | None = None, path: pathlib.Path | None = None):
    from sw import loom_asm
    src = (path or DMX_ASM).read_text(encoding="utf-8")
    if slots is not None:
        src = src.replace(f".equ SLOTS,      512",
                          f".equ SLOTS,      {int(slots)}")
    return loom_asm.assemble(src, filename="dmx_tx[model]")


def load_example(*, slots: int = SLOTS, base: int = 0, tid: int = 0,
                 clock_hz: int = CLOCK_HZ):
    from sw import loom_model
    from host.loom_host import ModelLink
    prog = assemble_example(slots)
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=clock_hz)
    far = DmxFarEnd(world, slots=slots)
    loom.run_threads(1 << tid)
    return dict(prog=prog, loom=loom, link=link, world=world, far=far,
                tid=tid, slots=slots)


def run_example(ctx: dict, data, *, max_cycles: int | None = None,
                chunk: int = 512) -> dict:
    """Push the slot bytes through the FIFO while the model runs."""
    world, link, tid = ctx["world"], ctx["link"], ctx["tid"]
    far = ctx["far"]
    payload = list(bytearray(data))
    sent = 0
    limit = max_cycles or int((ctx["slots"] + 4) * SLOT_BITS
                              * NOMINAL_BIT_CYCLES + 30000)
    while world.cycle < limit:
        # keep the RX FIFO ahead: push while there is room
        while sent < len(payload) and link.tx_push(tid, payload[sent]):
            sent += 1
        world.run(chunk)
        if far.frames:
            break
        if ctx["loom"].threads[tid].halt:
            break
    # the last slot's stop-bit events may still be queued when the thread
    # halts: keep the pin model running long enough for them to fire
    world.run(int(3 * SLOT_BITS * NOMINAL_BIT_CYCLES) + 300)
    far.flush()
    ctx["sent"] = sent
    return ctx


def check_decoded(far: DmxFarEnd, ctx: dict, data, check) -> None:
    """Shared DMX check list through a `check(name, ok, detail)` function."""
    frames = far.frames
    check("dmx: frame captured", len(frames) >= 1,
          f"{len(frames)} frames, errors={far.decoder.errors}")
    if not frames:
        return
    f = frames[0]
    data = list(bytearray(data))
    check("dmx: BREAK >= 88 us and MAB >= 8 us",
          f.break_ok and f.mab_ok,
          f"break={f.break_cycles}cyc ({f.break_cycles/CLOCK_HZ*1e6:.1f}us), "
          f"MAB={f.mab_cycles}cyc ({f.mab_cycles/CLOCK_HZ*1e6:.1f}us)")
    check("dmx: start code = 0x00 and 512 data slots",
          f.start_code == START_CODE and len(f.data) == ctx["slots"],
          f"start=0x{f.start_code:02X} slots={len(f.data)}")
    check("dmx: every slot has a 0 start bit and 2 stop bits",
          f.framing_ok, f"{len(f.slots)} slots checked")
    check("dmx: slot data byte-exact", f.data == data,
          f"first mismatch at "
          f"{next((i for i, (a, b) in enumerate(zip(f.data, data)) if a != b), None)}")
    check("dmx: 11-bit slot framing, 250 kbaud on the event grid",
          abs(f.bit_period - round(CLOCK_HZ * 16 / DMX_BAUD) / 16) < 0.02
          and f.edge_residual_max <= 1.0,
          f"{f.bit_period:.4f} cyc/bit -> {f.bit_rate/1e3:.3f} kbaud, "
          f"edge residual <= {f.edge_residual_max:.2f} cyc")


def _model_selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    data = bytes(((i * 37 + 11) & 0xFF) for i in range(SLOTS))
    ctx = load_example()
    run_example(ctx, data)
    check_decoded(ctx["far"], ctx, data, check)
    check("dmx: host pushed all 512 slot bytes", ctx.get("sent") == SLOTS,
          f"sent={ctx.get('sent')}")
    if VERBOSE:
        for line in ctx["far"].decoder.summary():
            print("       ", line)
    print("DMX_MODEL_SELFTEST:", "PASS" if ok else "FAIL")
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

    # small frame on a synthetic level trace (no golden model)
    data = [0x00, 0x01, 0x7F, 0x80, 0xFF, 0xA5, 0x5A, 0x42]
    enc = encode_reference(data, slots=8)
    levels = synthetic_levels(enc)
    dec = DmxDecoder(slots=8)
    for cycle, level in levels:
        dec.step(cycle, level)
    dec.flush()
    check("synthetic DMX frame decodes", len(dec.frames) == 1,
          f"{len(dec.frames)} frames, errors={dec.errors}")
    if dec.frames:
        f = dec.frames[0]
        check("synthetic: BREAK/MAB durations legal", f.break_ok and f.mab_ok,
              f"break={f.break_cycles} MAB={f.mab_cycles}")
        check("synthetic: start code + 8 slots byte-exact",
              f.start_code == 0 and f.data == data,
              f"start=0x{f.start_code:02X} data={[hex(b) for b in f.data]}")
        check("synthetic: 11-bit framing validates",
              f.framing_ok, f"{len(f.slots)} slots")
        check("synthetic: 250 kbaud bit period recovered",
              abs(f.bit_period - 133.3333) < 0.02,
              f"{f.bit_period:.4f} cyc -> {f.bit_rate/1e3:.3f} kbaud")
        check("synthetic: slot starts on the bit grid",
              f.edge_residual_max <= 0.75,
              f"max residual {f.edge_residual_max:.2f} cyc")
    print("DMX_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    VERBOSE = "-v" in argv or "--verbose" in argv
    rc = _selftest()
    rc |= _model_selftest()
    print("DMX_SELFTEST:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

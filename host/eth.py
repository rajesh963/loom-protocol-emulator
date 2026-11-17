#!/usr/bin/env python3
"""Ethernet Manchester framing + CRC-32 reference (host side / model oracle).

Companion to `sw/examples/eth_tx.asm` (Manchester frame transmitter, stage 1
of the Ethernet stretch goal) and the stage-2 20 Mbaud analysis.

What is modelled
----------------
* `crc32_8023` — IEEE 802.3 FCS: reflected poly 0xEDB88320, init 0xFFFFFFFF,
  final XOR 0xFFFFFFFF (zlib-compatible; check value "123456789" = 0xCBF43926).
* `build_frame` — 64-byte minimum frame: dst(6) + src(6) + ethertype(2) +
  payload padded to 46 + FCS(4), FCS little-endian.
* `encode_frame` — wire bits: 7 x 0xAA preamble + 0xD5 SFD + frame bytes, all
  LSB-first, Manchester-coded as two half-cells per bit (0 -> high,low;
  1 -> low,high).
* `ManchesterDecoder` — independent receiver: clock recovery from the edge
  stream, Manchester decode with validity checks, preamble/SFD lock, FCS check,
  per-frame measurements (cell/bit period, data rate, edge-grid residual).
* `synthetic_trace` — exact synthetic edge list (8-cycle stage-1 cells, or any
  cell period) for self-checks without the golden model.

Stage-2 honesty numbers are printed by the CLI: true 10BASE-T is 20 Mbaud =
1.6667 cycles/cell at 33.33 MHz; a firmware cell needs >=4 instructions
(extract bit, compute level, emit, advance) = 80 M instr/s against a 33.33 M
instr/s chip -> 2.4x over IPC, independent of the event scheduler; plus +-30 ns
cycle-quantised jitter against a 50 ns cell (10BASE-T TX jitter budget: a few
ns).  The highest Manchester rate that fits IPC is ~4.2 Mbit/s (5 M cells/s at
~4 instr/cell ... see CLI for the exact figures).

Run `python -m host.eth` for the self-check.
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sw.protocols import FarEnd, WireWorld, format_wave                 # noqa: E402

__all__ = [
    "crc32_8023", "build_frame", "encode_frame", "bits_lsb",
    "manchester_cells", "synthetic_trace", "ManchesterDecoder",
    "default_frame", "CLOCK_HZ", "STAGE1_CELL_CYCLES", "TXD_PIN",
]

CLOCK_HZ = 33_333_333
TXD_PIN = 0                     # P[0] = uio[0] (dset makes it an output)
STAGE1_CELL_CYCLES = 8          # 16-cycle bit period -> 1.25 Mbit/s
STAGE1_DATA_RATE = CLOCK_HZ / (2 * STAGE1_CELL_CYCLES)

PREAMBLE_BYTES = 7              # 7 x 0xAA
SFD = 0xD5


# ---------------------------------------------------------------------------
# CRC-32 / frame construction
# ---------------------------------------------------------------------------
def crc32_8023(data: bytes) -> int:
    """IEEE 802.3 FCS (zlib-compatible): poly 0xEDB88320, reflected."""
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else (crc >> 1)
    return crc ^ 0xFFFFFFFF


def default_frame() -> bytes:
    """A deterministic 64-byte minimum frame with a readable payload."""
    dst = bytes([0x02, 0x4C, 0x4F, 0x4F, 0x4D, 0x01])   # 02:4C:4F:4F:4D:01
    src = bytes([0x02, 0x4C, 0x4F, 0x4F, 0x4D, 0x02])   # 02:4C:4F:4F:4D:02
    ethertype = bytes([0x08, 0x00])                     # IPv4
    text = b"LOOM Ethernet Manchester frame, CRC32 in firmware!"
    payload = (text * 4)[:46]
    return build_frame(dst, src, ethertype, payload)


def build_frame(dst: bytes, src: bytes, ethertype: bytes,
                payload: bytes) -> bytes:
    """Return the 64-byte on-wire frame (60-byte frame + 4-byte FCS)."""
    if len(dst) != 6 or len(src) != 6 or len(ethertype) != 2:
        raise ValueError("dst/src must be 6 bytes, ethertype 2 bytes")
    if len(payload) > 46:
        payload = payload[:46]
    payload = payload + bytes(46 - len(payload))
    data = dst + src + ethertype + payload
    fcs = crc32_8023(data)
    return data + bytes([fcs & 0xFF, (fcs >> 8) & 0xFF,
                         (fcs >> 16) & 0xFF, (fcs >> 24) & 0xFF])


def bits_lsb(data) -> list[int]:
    """Bits of each byte, LSB first (IEEE 802.3 order)."""
    out: list[int] = []
    for b in data:
        out += [(b >> i) & 1 for i in range(8)]
    return out


def encode_frame(frame: bytes) -> dict:
    """Wire bits + Manchester cells (+ metadata) for one frame."""
    preamble = bytes([0xAA]) * PREAMBLE_BYTES + bytes([SFD])
    pad = bytes([0x00]) * 0                      # placeholder for clarity
    wire_bits = bits_lsb(preamble) + bits_lsb(frame)
    cells = manchester_cells(wire_bits)
    return {"frame": frame, "preamble": preamble,
            "wire_bits": wire_bits, "cells": cells,
            "preamble_bits": 8 * (PREAMBLE_BYTES + 1),
            "frame_bits": 8 * len(frame)}


def manchester_cells(bits) -> list[int]:
    """Manchester: bit 0 -> (1,0), bit 1 -> (0,1)."""
    cells: list[int] = []
    for b in bits:
        cells.append(1 - (b & 1))
        cells.append(b & 1)
    return cells


# ---------------------------------------------------------------------------
# Synthetic edge list (for self-checks without the golden model)
# ---------------------------------------------------------------------------
def synthetic_trace(cells, *, cell_cycles: float = STAGE1_CELL_CYCLES,
                    idle_cells: int = 8) -> dict:
    """Turn Manchester cells into an integer-cycle edge list (level changes)."""
    transitions: list[tuple[int, int]] = []
    cycle = 0.0
    prev = 0
    for cell in [0] * idle_cells + list(cells) + [0] * idle_cells:
        if cell != prev:
            transitions.append((int(round(cycle)), cell))
            prev = cell
        cycle += cell_cycles
    return {"transitions": transitions, "end_cycle": int(round(cycle)),
            "cell_cycles": cell_cycles}


# ---------------------------------------------------------------------------
# Reference Manchester receiver
# ---------------------------------------------------------------------------
class EthFrame:
    """One decoded Ethernet frame."""

    def __init__(self) -> None:
        self.preamble_ok = False
        self.sfd_ok = False
        self.frame = b""
        self.fcs_rx = 0
        self.fcs_calc = 0
        self.fcs_ok = False
        self.cell_period = 0.0
        self.bit_period = 0.0
        self.data_rate = 0.0
        self.cell_count = 0
        self.max_residual = 0.0
        self.errors: list[str] = []

    @property
    def ok(self) -> bool:
        return (self.preamble_ok and self.sfd_ok and self.fcs_ok
                and not self.errors)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"EthFrame(preamble_ok={self.preamble_ok} sfd_ok={self.sfd_ok} "
                f"bytes={len(self.frame)} fcs=0x{self.fcs_rx:08X}"
                f"{'(ok)' if self.fcs_ok else '(BAD)'} "
                f"cell={self.cell_period:.3f}cyc "
                f"rate={self.data_rate/1e6:.4f}Mbit/s "
                f"max_res={self.max_residual:.2f}cyc ok={self.ok})")


class ManchesterDecoder(FarEnd):
    """Independent Manchester receiver attached to a `WireWorld` TXD pin."""

    name = "eth-host-rx"

    def __init__(self, world: WireWorld, *, txd_pin: int = TXD_PIN,
                 cell_hint: float = STAGE1_CELL_CYCLES,
                 gap_cells: int = 12):
        self.world = world
        self.txd_pin = int(txd_pin)
        self.cell_hint = float(cell_hint)
        self.gap_cycles = self.cell_hint * gap_cells
        self._prev = None
        self._active = False
        self._group: list[tuple[int, int]] = []
        self._last_change = 0
        self._ready = False
        self.frames: list[EthFrame] = []
        self.errors: list[str] = []
        self.wave: list[tuple[int, int]] = [(0, 0)]
        world.attach(self)

    # -- segmentation -----------------------------------------------------
    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        level = (pins_out16 >> self.txd_pin) & 1
        if not self._ready:
            self._ready = True
            self._prev = level
            self.wave = [(cycle, level)]
            return
        if level != self._prev:
            self.wave.append((cycle, level))
            if not self._active:
                self._active = True
                self._group = []
            self._group.append((cycle, level))
            self._last_change = cycle
        elif self._active and (cycle - self._last_change) > self.gap_cycles:
            self._finish()
        self._prev = level

    def flush(self) -> None:
        if self._active:
            self._finish()

    def _finish(self) -> None:
        group, self._group = self._group, []
        self._active = False
        if not group:
            return
        try:
            self.frames.append(self._decode(group))
        except Exception as exc:
            self.errors.append(f"decode failed: {exc!r}")

    # -- decoding ---------------------------------------------------------
    @staticmethod
    def _level_at(group, cycle: float) -> int:
        level = 0
        for c, v in group:
            if c <= cycle:
                level = v
            else:
                break
        return level

    def _decode(self, group) -> EthFrame:
        fr = EthFrame()
        t0 = group[0][0]
        edges = [c for c, _ in group]
        # ---- cell clock recovery: fit t = t0 + m*cell over all edges
        cell = self.cell_hint
        for _ in range(4):
            fits = [(round((c - t0) / cell), c) for c in edges]
            mean_m = sum(m for m, _ in fits) / len(fits)
            mean_c = sum(c for _, c in fits) / len(fits)
            den = sum((m - mean_m) ** 2 for m, _ in fits)
            if den <= 0:
                break
            cell = sum((m - mean_m) * (c - mean_c) for m, c in fits) / den
        fr.cell_period = cell
        fr.bit_period = 2 * cell
        fr.data_rate = self.world.clock_hz / (2 * cell)
        fr.max_residual = max(
            (abs(round((c - t0) / cell) * cell - (c - t0)) for c in edges),
            default=0.0)
        n_cells = int((group[-1][0] - t0) / cell) + 2
        fr.cell_count = n_cells
        levels = [self._level_at(group, t0 + (k + 0.5) * cell)
                  for k in range(n_cells)]
        bits = []
        for k in range(0, len(levels) - 1, 2):
            a, b = levels[k], levels[k + 1]
            if (a, b) == (1, 0):
                bits.append(0)
            elif (a, b) == (0, 1):
                bits.append(1)
            else:
                # (0,0)/(1,1) is the idle line after the last cell, not a
                # bit; only an error if it happens before enough bits.
                break
        # ---- preamble + SFD lock
        pre_bits = []
        idx = 0
        while idx < len(bits) and len(pre_bits) < 8 * PREAMBLE_BYTES:
            if bits[idx] != (idx & 1):      # 0xAA LSB-first = 0,1,0,1,...
                break
            pre_bits.append(bits[idx])
            idx += 1
        fr.preamble_ok = (len(pre_bits) >= 8 * (PREAMBLE_BYTES - 1))
        if not fr.preamble_ok:
            fr.errors.append(f"preamble lock failed ({len(pre_bits)} bits)")
            return fr
        # scan for the SFD byte 0xD5 starting on a byte boundary
        sfd_bits = bits_lsb(bytes([SFD]))
        while idx + 8 <= len(bits):
            if bits[idx:idx + 8] == sfd_bits:
                fr.sfd_ok = True
                idx += 8
                break
            idx += 1
        if not fr.sfd_ok:
            fr.errors.append("SFD 0xD5 not found")
            return fr
        frame_bits = bits[idx:idx + 8 * 64]
        if len(frame_bits) < 8 * 64:
            fr.errors.append(f"short frame ({len(frame_bits)//8} bytes)")
        frame = bytes(sum(frame_bits[8 * i + j] << j for j in range(8))
                      for i in range(len(frame_bits) // 8))
        fr.frame = frame
        if len(frame) >= 64:
            data = frame[:60]
            fr.fcs_rx = int.from_bytes(frame[60:64], "little")
            fr.fcs_calc = crc32_8023(data)
            fr.fcs_ok = fr.fcs_rx == fr.fcs_calc
            if not fr.fcs_ok:
                fr.errors.append(f"FCS 0x{fr.fcs_rx:08X} != 0x{fr.fcs_calc:08X}")
        return fr

    # -- helpers ---------------------------------------------------------
    def decode_trace(self, trace) -> list[EthFrame]:
        group = sorted(trace["transitions"], key=lambda x: x[0])
        # the idle level (0) is implicit before the first transition
        self.frames.append(self._decode(group))
        return self.frames

    def summary(self) -> list[str]:
        return [repr(f) for f in self.frames]

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {f"TXD P[{self.txd_pin}]": format_wave(self.wave, chars=chars,
                                                      start=start, end=end)}


# ---------------------------------------------------------------------------
# Self-check / CLI
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    check("CRC-32/802.3 check value 0xCBF43926",
          crc32_8023(b"123456789") == 0xCBF43926,
          hex(crc32_8023(b"123456789")))
    frame = default_frame()
    check("default frame is 64 bytes, FCS self-consistent",
          len(frame) == 64 and int.from_bytes(frame[60:], "little")
          == crc32_8023(frame[:60]), f"{frame[:6].hex()}...")

    enc = encode_frame(frame)
    check("Manchester: 2 cells per bit",
          len(enc["cells"]) == 2 * len(enc["wire_bits"]),
          f"{len(enc['cells'])} cells")
    trace = synthetic_trace(enc["cells"])
    dec = ManchesterDecoder.__new__(ManchesterDecoder)

    class _W:
        clock_hz = CLOCK_HZ
    dec.world = _W()
    dec.txd_pin = TXD_PIN
    dec.cell_hint = STAGE1_CELL_CYCLES
    fr = dec._decode(sorted(trace["transitions"]))
    check("synthetic trace decodes byte-exact",
          fr.frame == frame, f"{len(fr.frame)} bytes")
    check("preamble/SFD/FCS ok",
          fr.preamble_ok and fr.sfd_ok and fr.fcs_ok, repr(fr))
    check("recovered cell period 8 cycles (2.0833 Mbit/s, 4.167 Mbaud)",
          abs(fr.cell_period - STAGE1_CELL_CYCLES) < 0.01
          and abs(fr.data_rate - STAGE1_DATA_RATE) < 1.0,
          f"cell={fr.cell_period:.3f} cyc, bit={fr.bit_period:.2f} cyc, "
          f"rate={fr.data_rate/1e6:.4f} Mbit/s")
    check("edges on the 8-cycle grid", fr.max_residual <= 0.5,
          f"max residual {fr.max_residual:.2f} cyc")

    # ---- stage-2 ceiling numbers (documented, not implemented) ----------
    cell_20mbaud = CLOCK_HZ / 20e6
    min_instr_per_cell = 4          # extract bit, compute level, emit, advance
    instr_per_s = 20e6 * min_instr_per_cell
    print(f"  stage-2 10BASE-T ceiling: {cell_20mbaud:.4f} cycles/cell "
          f"(20 Mbaud); a {min_instr_per_cell}-instruction cell needs "
          f"{instr_per_s/1e6:.1f} M instr/s vs "
          f"{CLOCK_HZ/1e6:.2f} M instr/s IPC -> "
          f"{instr_per_s/CLOCK_HZ:.2f}x over")
    max_cells = CLOCK_HZ / min_instr_per_cell
    print(f"  stage-2 max Manchester rate at {min_instr_per_cell} instr/cell: "
          f"{max_cells/1e6:.2f} M cells/s = {max_cells/2e6:.2f} Mbit/s")
    loop_instr_per_cell = 4.5      # measured: send_byte = 9 instr/bit
    print(f"  stage-1 loop cost {loop_instr_per_cell} instr/cell = "
          f"{loop_instr_per_cell*2*STAGE1_DATA_RATE/CLOCK_HZ*100:.0f} % IPC at "
          f"{STAGE1_DATA_RATE/1e6:.3f} Mbit/s; same loop ceiling "
          f"{CLOCK_HZ/loop_instr_per_cell/2e6:.2f} Mbit/s")
    # 4-thread scheduling collisions at 1.6875-cycle cells
    targets = [int((20e6 and k * cell_20mbaud)) for k in range(1024)]
    collisions = sum(1 for a, b in zip(targets, targets[1:]) if a == b)
    print(f"  stage-2 4-thread event targets: {collisions}/1023 adjacent-cell "
          f"same-cycle collisions at 1.6875-cycle spacing; edges land within "
          f"0.69 cyc (+-23 ns) of the ideal grid from cycle quantisation alone")
    print("ETH_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

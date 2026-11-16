"""UART VIP: 8N1 (optional parity) reference model, pure Python.

Two directions:
  * :class:`UartDecoder` -- given chip-side transitions [(cycle, level)] of the
    TX line, recover bytes, check start/stop framing, measure baud error.
  * :class:`UartDriver` -- produce scheduled [(cycle, level)] transitions for a
    byte stream, so a test can drive the chip's RX pin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass
class UartFrame:
    byte: int
    start_cycle: int
    data_cycles: list[int]
    stop_cycle: int
    stop_ok: bool
    parity_ok: bool | None
    samples: list[int]

    @property
    def ok(self) -> bool:
        return self.stop_ok and (self.parity_ok is not False)


class UartDecoder:
    """Decode an 8N1 (optionally 8E1/8O1) UART bit stream from edge times."""

    def __init__(self, baud_period: float, *, parity: str | None = None,
                 tolerance: float = 0.05):
        self.period = float(baud_period)
        self.parity = parity            # None | "even" | "odd"
        self.tolerance = tolerance
        self.frames: list[UartFrame] = []
        self.errors: list[str] = []

    def level_at(self, transitions: Sequence[tuple[int, int]], cycle: float) -> int:
        lvl = 1
        for c, v in transitions:
            if c <= cycle:
                lvl = v
            else:
                break
        return lvl

    def decode(self, transitions: Sequence[tuple[int, int]],
               end_cycle: int) -> list[UartFrame]:
        tr = sorted(transitions)
        self.frames = []
        c = tr[0][0] if tr else 0
        while c < end_cycle:
            # find a falling edge of sufficient width (start bit)
            start = None
            for i, (cc, v) in enumerate(tr):
                if cc < c:
                    continue
                if v == 0 and self.level_at(tr, cc - 1) == 1:
                    start = cc
                    break
            if start is None or start + 9.5 * self.period > end_cycle:
                break
            samples, data_cycles = [], []
            for k in range(8):
                t = start + (1.5 + k) * self.period
                data_cycles.append(int(round(t)))
                samples.append(self.level_at(tr, t))
            stop_t = start + 9.5 * self.period
            stop = self.level_at(tr, stop_t)
            parity_ok = None
            if self.parity is not None:
                par_t = start + 9.5 * self.period
                p = self.level_at(tr, par_t)
                ones = sum(samples) % 2
                exp = ones if self.parity == "odd" else 1 - ones
                parity_ok = (p == exp)
                stop_t = start + 10.5 * self.period
                stop = self.level_at(tr, stop_t)
            value = 0
            for i, b in enumerate(samples):
                value |= b << i                  # LSB first on the wire
            fr = UartFrame(byte=value, start_cycle=int(start),
                           data_cycles=data_cycles, stop_cycle=int(round(stop_t)),
                           stop_ok=bool(stop), parity_ok=parity_ok,
                           samples=samples)
            self.frames.append(fr)
            if not fr.ok:
                self.errors.append(
                    f"framing error in byte 0x{value:02x} at cycle {int(start)}: "
                    f"start/stop/samples={samples}")
            # resume scanning from mid-stop-bit (never past the next start
            # edge, which rounding can put one cycle before start+10*period)
            c = int(start + 9.5 * self.period)

    def baud_error(self, known_cycles: int | None = None) -> float:
        """Average measured bit period vs the nominal decoder period."""
        if len(self.frames) < 2:
            return 0.0
        span = self.frames[-1].start_cycle - self.frames[0].start_cycle
        nbytes = len(self.frames) - 1
        if nbytes <= 0:
            return 0.0
        measured = span / (nbytes * 10)
        return (measured - self.period) / self.period


class UartDriver:
    """Schedule UART frames for the chip's RX pin.

    ``transitions()`` returns [(cycle, level)] with the line idle-high, one start
    bit, 8 data bits LSB-first, optional parity, one stop bit per byte.
    """

    def __init__(self, baud_period: float, *, parity: str | None = None):
        self.period = float(baud_period)
        self.parity = parity

    def transitions(self, data: Iterable[int], *, t_start: int = 0,
                    idle_bits: float = 2.0) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = [(0, 1)]
        t = t_start + idle_bits * self.period
        for byte in data:
            bits = [0]                                   # start
            bits += [(byte >> i) & 1 for i in range(8)]  # LSB first
            if self.parity is not None:
                ones = sum((byte >> i) & 1 for i in range(8)) % 2
                bits.append(ones if self.parity == "odd" else 1 - ones)
            bits.append(1)                               # stop
            for i, b in enumerate(bits):
                out.append((int(round(t + i * self.period)), b))
            t += len(bits) * self.period
        return out

    def frame_bits(self, byte: int) -> list[int]:
        bits = [0] + [(byte >> i) & 1 for i in range(8)]
        if self.parity is not None:
            ones = sum((byte >> i) & 1 for i in range(8)) % 2
            bits.append(ones if self.parity == "odd" else 1 - ones)
        bits.append(1)
        return bits

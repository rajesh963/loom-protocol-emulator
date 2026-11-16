"""I2C VIP: open-drain bus, controller/target models, decoder.  Pure Python.

The LOOM I2C firmware uses *open drain via DIR*: a `uio` pin is pulled low by
setting its DIR bit (with POUT bit 0) and released by clearing DIR.  The far
end therefore models the bus as wired-AND:

    line = chip_low AND external_low      (1 = released / pulled up)

API
---
`I2cTargetModel`  -- reactive target for DUT-as-controller tests.  Feed it the
                     bus levels each poll; it returns the drive it wants on SDA
                     (and optionally SCL for clock stretching).
`I2cMasterModel`  -- scheduled controller for DUT-as-target tests: returns the
                     bus level schedule plus the ACK sample instants.
`decode`          -- recover START/STOP/bytes/ACK from level traces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass
class I2cEvent:
    kind: str                     # 'start' | 'stop' | 'byte' | 'ack'
    cycle: int
    byte: int | None = None
    ack: int | None = None        # 0 = ACK (SDA low), 1 = NAK


def _level(tr: Sequence[tuple[int, int]], cycle: float) -> int:
    lvl = 1
    for c, v in tr:
        if c <= cycle:
            lvl = v
        else:
            break
    return lvl


def decode(scl_tr: Sequence[tuple[int, int]], sda_tr: Sequence[tuple[int, int]]):
    """Decode an I2C transaction arc from (cycle, level) traces."""
    scl_tr = sorted(scl_tr)
    sda_tr = sorted(sda_tr)
    events: list[I2cEvent] = []
    if not scl_tr or not sda_tr:
        return events
    cycles = sorted({c for c, _ in scl_tr} | {c for c, _ in sda_tr})
    prev_scl = _level(scl_tr, cycles[0] - 1)
    prev_sda = _level(sda_tr, cycles[0] - 1)
    bits: list[int] = []
    in_frame = False
    for c in cycles:
        scl = _level(scl_tr, c)
        sda = _level(sda_tr, c)
        if scl == 1 and sda != prev_sda:
            if sda == 0 and not in_frame:
                events.append(I2cEvent("start", c))
                in_frame = True
                bits = []
            elif sda == 1 and in_frame:
                events.append(I2cEvent("stop", c))
                in_frame = False
        if scl == 1 and prev_scl == 0 and in_frame:
            bits.append(sda)
            if len(bits) == 9:
                value = 0
                for b in bits[:8]:
                    value = (value << 1) | b
                events.append(I2cEvent("byte", c, value))
                events.append(I2cEvent("ack", c, None, bits[8]))
                bits = []
        prev_scl, prev_sda = scl, sda
    return events


class I2cTargetModel:
    """Reactive I2C target (used when the DUT is the controller).

    ``feed(cycle, scl, sda)`` with the current bus levels returns
    ``(want_sda_low, stretch)``: whether the far end should pull SDA low, and
    whether it should hold SCL low (clock stretching) for this poll.

    Write path is modelled fully (address, register pointer, data, ACK); read
    path drives a small register file.  The ACK is asserted during the SCL low
    phase *before* the 9th clock and held through the high phase, exactly as a
    real target does.
    """

    def __init__(self, address: int, register_file: Sequence[int] | None = None,
                 *, stretch_cycles: int = 0):
        self.address = address & 0x7F
        self.regs = list(register_file if register_file is not None else
                         [0] * 8)
        self.stretch_per_poll = stretch_cycles
        self._stretch_pending = 0
        self._stretch_left = 0
        self.prev_scl = 1
        self.prev_sda = 1
        self.bits: list[int] = []
        self.byte_i = 0
        self.state = "idle"           # idle | sample | ack | tx
        self.reg_ptr = 0
        self.read_mode = False
        self.ack_out = False
        self.tx_bits: list[int] = []
        self.events: list[I2cEvent] = []
        self.data_rx: list[int] = []

    def feed(self, cycle: int, scl: int, sda: int):
        scl, sda = scl & 1, sda & 1
        want_low = False
        stretch = 0
        # START / STOP: SDA changes while SCL is high
        if scl == 1 and self.prev_scl == 1:
            if self.prev_sda == 1 and sda == 0:
                self.state = "sample"
                self.bits = []
                self.byte_i = 0
                self.ack_out = False
                self._ack_drive = False
                self._ack_done = False
                self.events.append(I2cEvent("start", cycle))
            elif self.prev_sda == 0 and sda == 1:
                self.state = "idle"
                self.ack_out = False
                self.events.append(I2cEvent("stop", cycle))
        sample_rise = (scl == 1 and self.prev_scl == 0)
        if self.state == "sample":
            if sample_rise:
                self.bits.append(sda)
                if len(self.bits) == 8:
                    value = 0
                    for b in self.bits:
                        value = (value << 1) | b
                    self.bits = []
                    self.byte_i += 1
                    if self.byte_i == 1:
                        self.read_mode = bool(value & 1)
                        self.match = (value >> 1) == self.address
                        self.ack_out = self.match
                        self.events.append(I2cEvent(
                            "match" if self.match else "nomatch", cycle, value))
                    else:
                        if self.read_mode:
                            self.ack_out = True
                        else:
                            self.data_rx.append(value)
                            self.ack_out = True
                            self.events.append(I2cEvent("byte", cycle, value))
                    self.events.append(I2cEvent("byte", cycle, value))
                    self.state = "ack"
                    if self.stretch_per_poll:
                        # hold SCL low only starting from the ACK's low phase;
                        # pulling it low while the master's high phase is still
                        # running would look like a corrupted bit to both ends
                        self._stretch_pending = self.stretch_per_poll
        elif self.state == "ack":
            # ACK must only start during an SCL *low* phase: pulling SDA low
            # while SCL is high would be a START/STOP condition on the bus.
            if scl == 0 and not getattr(self, "_ack_drive", False):
                self._ack_drive = True
            want_low = bool(getattr(self, "_ack_drive", False)) and self.ack_out
            if sample_rise:                    # 9th clock rising
                self.events.append(I2cEvent("ack", cycle, None, sda))
                self._ack_done = True
            if scl == 0 and self.prev_scl == 1 and getattr(self, "_ack_done", False):
                self._ack_done = False
                self._ack_drive = False
                self.ack_out = False
                want_low = False
                if self.read_mode and self.match:
                    value = self.regs[self.reg_ptr % len(self.regs)]
                    self.tx_bits = [(value >> i) & 1 for i in range(7, -1, -1)]
                    self.state = "tx"
                else:
                    if self.byte_i >= 2 and not self.read_mode:
                        self.reg_ptr = (self.reg_ptr + 1) & 7
                    self.state = "sample"
        elif self.state == "tx":
            if scl == 0 and self.prev_scl == 1:
                if self.tx_bits:
                    want_low = self.tx_bits.pop(0) == 0
                else:
                    want_low = False
                    self.state = "sample"
        if self._stretch_pending and scl == 0:
            self._stretch_left = self._stretch_pending
            self._stretch_pending = 0
        if self._stretch_left > 0:
            stretch = 1
            self._stretch_left -= 1
        self.prev_scl, self.prev_sda = scl, sda
        return want_low, stretch


class I2cMasterModel:
    """Scheduled I2C controller (used when the DUT is the target).

    ``schedule(ops)`` returns ``(events, sample_cycles)`` where events is
    [(cycle, scl_level, sda_level)] (1 = released / pull-up, 0 = pull low) and
    sample_cycles are the instants at which an ACK bit should be sampled.
    """

    def __init__(self, *, half_period: int = 300, setup: int = 20):
        self.half = int(half_period)
        self.setup = int(setup)

    def _bit_events(self, t: int, bit: int, events, samples, *, sample=False):
        events.append((t, 0, bit))               # SCL low, SDA = bit
        t += self.half
        events.append((t, 1, bit))               # SCL high, data valid
        t += self.half
        events.append((t, 0, bit))
        return t

    def schedule(self, address: int, writes: Sequence[int] = (),
                 reads: int = 0, reg: int = 0, t_start: int = 0):
        """One transaction: [START] addr+W, reg, writes..., [STOP].

        If ``reads`` > 0 a repeated START + addr+R is issued and ``reads`` bytes
        are clocked in; the ACK slots are added to ``samples``.
        """
        events: list[tuple[int, int, int]] = [(t_start, 1, 1)]
        samples: list[int] = []
        t = t_start + self.setup
        # START: SDA falls while SCL high
        events.append((t, 1, 0))
        t += self.half
        events.append((t, 0, 0))
        t += self.half

        def send_byte(t, byte):
            for i in range(7, -1, -1):
                t = self._bit_events(t, (byte >> i) & 1, events, samples)
            # ACK clock: release SDA, 9th pulse
            events.append((t, 0, 1))
            t += self.half
            events.append((t, 1, 1))
            samples.append(t + self.half // 2)
            t += self.half
            events.append((t, 0, 1))
            return t

        t = send_byte(t, (address << 1) | (1 if reads else 0))
        if writes or reads:
            t = send_byte(t, reg)
        for b in writes:
            t = send_byte(t, b)
        if reads:
            # repeated START
            events.append((t, 1, 0))
            t += self.half
            events.append((t, 1, 1))
            t += self.half
            events.append((t, 0, 1))
            t += self.half
            t = send_byte(t, (address << 1) | 1)
            for _ in range(reads):
                for i in range(7, -1, -1):
                    t = self._bit_events(t, 1, events, samples)   # released SDA
                # master ACK/NAK clock: for the last byte NAK
                events.append((t, 0, 1))
                t += self.half
                events.append((t, 1, 1))
                t += self.half
                events.append((t, 0, 1))
        # STOP: SDA rises while SCL high
        events.append((t, 1, 0))
        t += self.half
        events.append((t, 1, 1))
        return events, samples

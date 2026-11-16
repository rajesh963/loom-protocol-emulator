"""SPI VIP: modes 0-3, pure Python.

* :class:`SpiSlaveModel` -- a reactive far-end SPI *target*: feed it the levels
  the DUT drives on SCK/MOSI/CS_N each poll and it tells you what to drive on
  MISO.  Used when the DUT is the SPI master.
* :class:`SpiMasterModel` -- a scheduled far-end SPI *master*: generates the
  SCK/MOSI/CS_N event schedule and the MISO sample instants for a transfer.
  Used when the DUT is the SPI target.
* :func:`decode` -- recover transactions from (cycle, sck, mosi, cs_n) level
  traces; checks CPOL/CPHA and bit order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

MODES = (0, 1, 2, 3)


def idle_sck(mode: int) -> int:
    return (mode >> 1) & 1


def sample_on_leading(mode: int) -> bool:
    """CPHA==0 samples on the leading edge, CPHA==1 on the trailing edge."""
    return (mode & 1) == 0


@dataclass
class SpiTransfer:
    mode: int
    mosi_bytes: list[int]
    miso_bytes: list[int]
    cs_low_cycle: int
    cs_high_cycle: int
    edges: list[tuple[int, int]]          # (cycle, new SCK level)
    bits: int = 0
    setup_violations: list[str] = field(default_factory=list)


class SpiSlaveModel:
    """Reactive SPI target for DUT-master tests.

    Call :meth:`feed` with the chip-driven levels (1/0 for SCK, MOSI, CS_N) as
    often as convenient; it returns the MISO level the far end should drive.
    The model samples MOSI on the correct edge for the mode and shifts MISO out
    on the opposite edge, MSB first.
    """

    def __init__(self, mode: int, miso_bytes: Sequence[int]):
        assert mode in MODES
        self.mode = mode
        self.reply = list(miso_bytes)
        self.idle = idle_sck(mode)
        self.prev_sck = self.idle
        self.prev_cs = 1
        self.cs_active = False
        self.bit_i = 0
        self.rx = 0
        self.tx = 0
        self.miso = 0
        self.transfers: list[SpiTransfer] = []
        self._cur: SpiTransfer | None = None
        self._cur_bits = 0

    def _begin(self, cycle: int):
        self.cs_active = True
        self.prev_sck = self.idle
        self.bit_i = 0
        self.rx = 0
        self.tx_byte = self.reply.pop(0) & 0xFF if self.reply else 0
        # number of MISO bits already presented to the master
        self.presented = 0
        # CPHA=0 presents the first MISO bit during CS setup; CPHA=1 drives it
        # on the leading edge instead.
        if sample_on_leading(self.mode):
            self.miso = (self.tx_byte >> 7) & 1
            self.presented = 1
        else:
            self.miso = 0
        self._cur = SpiTransfer(mode=self.mode, mosi_bytes=[], miso_bytes=[],
                                cs_low_cycle=cycle, cs_high_cycle=-1, edges=[])
        self.transfers.append(self._cur)

    def _end(self, cycle: int):
        if self._cur is not None:
            self._cur.cs_high_cycle = cycle
        self.cs_active = False
        self.miso = 0

    def feed(self, cycle: int, sck: int, mosi: int, cs_n: int) -> int:
        """Advance the model; returns the MISO level to drive now."""
        if cs_n == 0 and self.prev_cs == 1:
            self._begin(cycle)
        elif cs_n == 1 and self.prev_cs == 0:
            self._end(cycle)
        if not self.cs_active:
            self.prev_sck, self.prev_cs = sck, cs_n
            return 0
        leading = (sck != self.prev_sck) and (sck != self.idle)
        trailing = (sck != self.prev_sck) and (sck == self.idle)
        if self._cur is not None and sck != self.prev_sck:
            self._cur.edges.append((cycle, sck))
        do_sample = leading if sample_on_leading(self.mode) else trailing
        do_shift = trailing if sample_on_leading(self.mode) else leading
        if do_sample:
            self.rx = (self.rx << 1) | (mosi & 1)
            self._cur_bits += 1
            if self._cur_bits == 8:
                self._cur.mosi_bytes.append(self.rx & 0xFF)
                self._cur.miso_bytes.append(self.tx_byte)
                self.rx = 0
                self._cur_bits = 0
                self.tx_byte = self.reply.pop(0) & 0xFF if self.reply else 0
                self.presented = 0
                self.miso = 0
        if do_shift:
            if self.presented < 8:
                self.miso = (self.tx_byte >> (7 - self.presented)) & 1
                self.presented += 1
            else:
                self.miso = 0
        self.prev_sck, self.prev_cs = sck, cs_n
        return self.miso & 1


class SpiMasterModel:
    """Scheduled SPI master for DUT-target tests.

    ``schedule()`` returns (events, samples):
      events  = [(cycle, {pin_bit: level})] changes to apply to the far-end
                input drives (SCK/MOSI/CS_N bits in the 16-bit pin vector)
      samples = [(cycle, bit_position)] instants at which the master samples
                MISO (the test should read the MISO line then).
    """

    def __init__(self, mode: int, *, half_period: int, cs_setup: int = 4,
                 cs_hold: int = 4):
        assert mode in MODES
        self.mode = mode
        self.half = int(half_period)
        self.cs_setup = int(cs_setup)
        self.cs_hold = int(cs_hold)

    def schedule(self, mosi_bytes: Sequence[int], *, sck_bit: int, mosi_bit: int,
                 cs_bit: int, t_start: int = 0):
        idle = idle_sck(self.mode)
        events: list[tuple[int, dict[int, int]]] = []
        samples: list[tuple[int, int]] = []
        t = t_start
        for byte in mosi_bytes:
            level = {sck_bit: idle, cs_bit: 0}
            events.append((t, dict(level)))
            t += self.cs_setup
            bits = [(byte >> i) & 1 for i in range(7, -1, -1)]   # MSB first
            for i, b in enumerate(bits):
                level = {sck_bit: idle, cs_bit: 0, mosi_bit: b}
                events.append((t, dict(level)))
                t += self.half
                if sample_on_leading(self.mode):
                    samples.append((t, i))
                level = {sck_bit: 1 - idle, cs_bit: 0, mosi_bit: b}
                events.append((t, dict(level)))
                t += self.half
                if not sample_on_leading(self.mode):
                    samples.append((t, i))
            level = {sck_bit: idle, cs_bit: 0, mosi_bit: bits[-1]}
            events.append((t, dict(level)))
            t += self.cs_hold
            events.append((t, {sck_bit: idle, cs_bit: 1, mosi_bit: 0}))
            t += 2 * self.half
        return events, samples


def decode(sck_tr: Sequence[tuple[int, int]], mosi_tr: Sequence[tuple[int, int]],
           cs_tr: Sequence[tuple[int, int]], mode: int):
    """Decode an SPI transaction trace into SpiTransfers.

    sck_tr/mosi_tr/cs_tr are [(cycle, level)] transition lists; the returned
    transfers include the exact SCK edge cycles (so setup/hold can be checked
    against MISO transitions if desired).
    """
    idle = idle_sck(mode)
    events = sorted(
        [(c, "sck", v) for c, v in sck_tr]
        + [(c, "mosi", v) for c, v in mosi_tr]
        + [(c, "cs", v) for c, v in cs_tr])
    transfers: list[SpiTransfer] = []
    cur = None
    sck = idle
    mosi = 0
    cs = 1
    bits = 0
    rx = rxm = 0
    for cyc, kind, v in events:
        if kind == "cs":
            if v == 0 and cs == 1:
                cur = SpiTransfer(mode=mode, mosi_bytes=[], miso_bytes=[],
                                  cs_low_cycle=cyc, cs_high_cycle=-1, edges=[])
                transfers.append(cur)
                bits = 0
                rx = rxm = 0
            elif v == 1 and cs == 0 and cur is not None:
                cur.cs_high_cycle = cyc
                cur = None
            cs = v
        elif kind == "mosi":
            mosi = v
        elif kind == "sck":
            if cs == 0 and cur is not None:
                cur.edges.append((cyc, v))
                leading = (v != sck) and (v != idle)
                trailing = (v != sck) and (v == idle)
                do_sample = leading if sample_on_leading(mode) else trailing
                do_shift = trailing if sample_on_leading(mode) else leading
                if do_sample:
                    rx = (rx << 1) | mosi
                    bits += 1
                    if bits == 8:
                        cur.mosi_bytes.append(rx & 0xFF)
                        cur.miso_bytes.append(rxm & 0xFF)
                        rx = 0
                        bits = 0
                if do_shift:
                    rxm = ((rxm << 1) & 0xFF) | 0
            sck = v
    return transfers

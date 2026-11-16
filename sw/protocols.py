#!/usr/bin/env python3
"""LOOM protocol helpers — the *software side* of the protocol-emulator examples.

These classes are the far end of the wire.  They are used in two ways:

1. **Application side (works on real silicon).**  A LOOM protocol thread is a
   byte-stream endpoint: the host pushes bytes into the thread's RX FIFO
   (`TX_PUSH`) and reads bytes the thread produced from its TX FIFO (`RX_POP`),
   through `host/loom_host.py`'s `ByteTransport` (real chip) or `ModelLink`
   (golden model).  The `app_*` / `xfer` methods below do exactly that, plus the
   host doorbell (`WRITE_CSR EVT` = 1<<evt_bit) the firmware waits on.

2. **Wire side (simulation only).**  To *show* the chip emulating a protocol,
   the model must see the other end of the wire.  `WireWorld` steps a
   `loom_model.Loom` one cycle at a time; each attached far-end model drives the
   chip's input pins and decodes the chip's output pins, exactly as an
   external UART/SPI/I2C device would.  This is how `host/demos/demo_all.py`
   produces its decoded transcript and ASCII waveforms, and how
   `sw/examples/model_selftest.py` proves each example byte-exact.

Classes
-------
`WireWorld`          per-cycle simulator loop + pin-mux between far-end models
`UartBus`            far-end UART device (8N1, fractional baud) + app helpers
`SpiBus`             far-end SPI target (modes 0-3) + app helpers
`SpiMasterModel`     far-end SPI master, for testing `spi_slave.asm`
`I2cBus`             far-end I2C target with a register file + app helpers
`I2cMasterModel`     far-end I2C master, for testing `i2c_target.asm`
`format_wave`        ASCII waveform renderer used by the demos

Pin map of the example programs (P[n] is the *instruction* pin index; the
pin vector has P[15:10]=uo_out[7:2], P[9:8]=host, P[7:0]=uio):

    protocol      chip output pins        chip input pins
    ------------  ----------------------  --------------------------------
    UART          TX  P[10] (uo[2])       RX  P[11] (ui[3])
    SPI master    SCK P[0]  (uio[0])      MISO P[2] (uio[2])
                  MOSI P[1] (uio[1])
                  CS_N P[3] (uio[3])
    SPI slave     MISO P[0] (uio[0])      SCK P[13] (ui[5])
                                          MOSI P[14] (ui[6])
                                          CS_N P[12] (ui[4])
    I2C master    SCL P[4] (uio[4], OD)   SDA P[5] (uio[5], OD, bidir)
                  SDA P[5] (uio[5], OD)
    I2C target    SCL P[6] (uio[6], OD)   SDA P[7] (uio[7], OD, bidir)
                  SDA P[7] (uio[7], OD)

Open-drain convention in the model: for a `uio` pin the model reads the driven
level when `DIR`=1 and the external stimulus (`ext_in`, i.e. the far end's
pull-down) when `DIR`=0.  So a far-end open-drain device "releases" a line by
setting its ext bit to 1 and "pulls low" by setting it to 0; the chip does the
same with `DIR` (1 = pull low).  `0xFFFF` means "everything released, pulled
up" and is the default external state.

SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "ProtocolError", "fixed124", "Clock124", "WireWorld", "FarEnd",
    "UartBus", "SpiBus", "SpiMasterModel", "I2cBus", "I2cMasterModel",
    "format_wave", "render_levels",
]

DEFAULT_CLOCK_HZ = 60_000_000
ALL_PINS = 0xFFFF
HOST_BITS = 0x0300          # P[9:8] are the host link, never protocol pins


class ProtocolError(RuntimeError):
    """Raised when a protocol helper cannot complete an operation."""


# ---------------------------------------------------------------------------
# 12.4 fixed-point time (the event scheduler's native format)
# ---------------------------------------------------------------------------
def fixed124(cycles: float | int) -> int:
    """Cycles -> 12.4 fixed point (integer part <<4 | fractional sixteenths)."""
    return int(round(float(cycles) * 16.0)) & 0xFFFF


class Clock124:
    """A 12.4 fixed-point relaxation oscillator (matches `setp`/`waitd`).

    ``advance()`` returns this period's whole-cycle duration and carries the
    fractional remainder, so N periods are exactly N*period cycles (drift-free).
    """

    def __init__(self, period124: int):
        self.period = period124 & 0xFFFF
        self.frac = 0

    def reset(self, fraction: int = 0) -> None:
        self.frac = fraction & 0xF

    def advance(self) -> int:
        f = self.frac + (self.period & 0xF)
        carry = f >> 4
        self.frac = f & 0xF
        return (self.period >> 4) + carry


# ---------------------------------------------------------------------------
# ASCII waveform rendering
# ---------------------------------------------------------------------------
def render_levels(levels: Sequence[int], *, low: str = "_", high: str = "-") -> str:
    """Render a sequence of 0/1 levels as one ASCII line."""
    return "".join(high if lv else low for lv in levels)


def format_wave(transitions: Sequence[tuple[int, int]], *, start: int | None = None,
                end: int | None = None, chars: int = 96, low: str = "_",
                high: str = "-") -> str:
    """Sample a level-transition list ``[(cycle, level), ...]`` into ``chars``."""
    if not transitions:
        return ""
    times = [c for c, _ in transitions]
    if start is None:
        start = times[0]
    if end is None:
        end = times[-1] + 1
    span = max(1, end - start)
    step = span / chars
    out = []
    for i in range(chars):
        t = start + i * step
        idx = bisect.bisect_right(times, t) - 1
        if idx < 0:
            idx = 0
        out.append(high if transitions[idx][1] else low)
    return "".join(out)


# ---------------------------------------------------------------------------
# Wire world
# ---------------------------------------------------------------------------
class FarEnd:
    """A far-end device attached to a `WireWorld`.

    Subclasses implement:
      ``ext_mask``  bits of `pins_in` this device drives (property/attribute)
      ``ext_value`` level it currently drives on those bits
      ``observe(cycle, pins_out16, pins_dir8)`` called after every cycle
    and refresh ``ext_value`` in :meth:`observe` (one-cycle model latency).
    """

    ext_mask: int = 0
    ext_value: int = 0
    name: str = "far-end"

    def drive(self) -> tuple[int, int]:
        """Return ``(mask, value)`` of external pins to drive this cycle."""
        return self.ext_mask, self.ext_value

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:  # noqa: D401
        """Observe the pin bus after a cycle.  Override in subclasses."""
        raise NotImplementedError

    # observed wire level for an open-drain uio pin, from the far end's view
    @staticmethod
    def _wire_level(pins_out16: int, pins_dir8: int, ext16: int, bit: int) -> int:
        if (pins_dir8 >> bit) & 1:
            return (pins_out16 >> bit) & 1
        return (ext16 >> bit) & 1


class WireWorld:
    """Steps a `ModelLink`/`Loom` one cycle at a time with far-end models.

    Each cycle: every attached far-end's drive value is OR-ed onto the external
    pin stimulus, the model is stepped, then every far-end observes the pins.
    """

    def __init__(self, link: Any, *, clock_hz: int = DEFAULT_CLOCK_HZ,
                 initial_ext: int = ALL_PINS):
        self.link = link
        for attr in ("set_pins_in", "step"):
            if not hasattr(link, attr):
                raise ProtocolError(
                    f"WireWorld needs a model link with .{attr}(); got {link!r}. "
                    "A real chip (ByteTransport) cannot expose pins per cycle: "
                    "use the app_* helpers instead.")
        self.clock_hz = int(clock_hz)
        self.static_ext = initial_ext & ALL_PINS
        self.agents: list[FarEnd] = []
        self.cycle = 0
        self.ext16 = self.static_ext
        self.timed_out = False

    # -- wiring ----------------------------------------------------------
    def attach(self, agent: FarEnd) -> FarEnd:
        self.agents.append(agent)
        return agent

    @property
    def link_clock_hz(self) -> int:
        return self.clock_hz

    # -- stepping --------------------------------------------------------
    def step(self) -> None:
        ext = self.static_ext
        for agent in self.agents:
            mask, value = agent.drive()
            ext = (ext & ~mask) | (value & mask)
        self.ext16 = ext & ALL_PINS
        self.link.set_pins_in(self.ext16)
        self.link.step()
        cycle = self.cycle
        pout = int(self.link.pins_out16)
        dir8 = int(self.link.pins_dir8)
        for agent in self.agents:
            agent.observe(cycle, pout, dir8)
        self.cycle += 1

    def run(self, cycles: int) -> None:
        for _ in range(int(cycles)):
            self.step()

    def run_until(self, predicate: Callable[[], bool], max_cycles: int,
                  what: str = "condition") -> bool:
        """Step until ``predicate()`` is true.  Returns False on timeout."""
        for _ in range(int(max_cycles)):
            if predicate():
                return True
            self.step()
        self.timed_out = not predicate()
        if self.timed_out and what:
            raise ProtocolError(
                f"timed out after {max_cycles} cycles waiting for {what}")
        return not self.timed_out


# ---------------------------------------------------------------------------
# UART
# ---------------------------------------------------------------------------
@dataclass
class UartFrame:
    """One decoded 8N1 frame (for transcripts / waveforms)."""

    byte: int
    cells: list[int]            # [start, d0..d7, stop]
    start_cycle: int

    def cell_text(self) -> str:
        """One-line 8N1 cell diagram: ``S d0 d1 ... d7 P``."""
        body = " ".join(str(b) for b in self.cells[1:8])
        return f"S {body} P"


class UartBus(FarEnd):
    """A UART device: bit-bangs bytes to the chip's RX pin, decodes its TX pin.

    ``app_send``/``app_recv`` move bytes through the host SPI bridge FIFOs
    (works on silicon); ``wire_send``/``wire_recv`` are the simulated far end
    of the wire (needs a `WireWorld`).

    The bit clock is 12.4 fixed point, exactly like the firmware's `setp`
    register, so a 520.8333-cycle bit at 115200 baud is reproduced bit-exactly
    (the far end and the chip agree to <1/16 cycle per bit, no drift).
    """

    def __init__(self, world: WireWorld | None = None, *, link: Any | None = None,
                 tx_pin: int = 10, rx_pin: int = 11, baud: int = 115200,
                 clock_hz: int | None = None, tx_tid: int = 0, rx_tid: int = 1,
                 evt_bit: int = 0):
        self.world = world
        self.link = link if link is not None else (world.link if world else None)
        if self.link is None:
            raise ProtocolError("UartBus needs a world or a link")
        hz = int(clock_hz if clock_hz is not None else
                 (world.clock_hz if world else DEFAULT_CLOCK_HZ))
        self.clock_hz = hz
        self.baud = int(baud)
        self.period124 = fixed124(hz / self.baud)
        self.tx_pin = int(tx_pin)           # chip output (decode)
        self.rx_pin = int(rx_pin)           # chip input  (drive)
        self.tx_tid = int(tx_tid)           # thread running uart_tx.asm
        self.rx_tid = int(rx_tid)           # thread running uart_rx.asm
        self.evt_bit = int(evt_bit)
        self.name = "uart"

        # --- far-end transmitter (far end -> chip RX pin) ---
        self._sendq: deque[tuple[int, int]] = deque()   # (level, duration124)
        self._tx_level = 1
        self._tx_left = 0
        self._tx_clock = Clock124(self.period124)

        # --- far-end receiver (decode chip TX pin) ---
        self._rx_state = "idle"
        self._rx_next = 0
        self._rx_clock = Clock124(self.period124)
        self._rx_half_clock = Clock124(self.period124 >> 1)
        self._rx_bits: list[int] = []
        self._rx_cells: list[int] = []
        self._prev_rx = 1
        self.frames: list[UartFrame] = []       # decoded chip->far-end bytes
        self.bytes_sent: list[int] = []         # far-end -> chip bytes queued

        # --- app-level buffers ---
        self.app_rx: list[int] = []             # chip RX firmware -> host FIFO
        self.frames_tx: list[UartFrame] = []    # chip TX firmware -> wire
        self.wave_tx: list[tuple[int, int]] = []   # (cycle, level) chip TX pin
        self.wave_rx: list[tuple[int, int]] = []   # (cycle, level) chip RX pin
        if world is not None:
            world.attach(self)

    # -- pin drive -------------------------------------------------------
    @property
    def ext_mask(self) -> int:              # type: ignore[override]
        return 1 << self.rx_pin

    @property
    def ext_value(self) -> int:             # type: ignore[override]
        return (self._tx_level & 1) << self.rx_pin

    # -- far-end UART TX (drives chip RX) --------------------------------
    def wire_send(self, data: bytes | Iterable[int]) -> None:
        """Queue bytes for the far-end UART to transmit on the chip's RX pin."""
        for b in bytes(bytearray(data)):
            self.bytes_sent.append(b)
            self._sendq.append((0, self.period124))            # start
            for i in range(8):
                self._sendq.append(((b >> i) & 1, self.period124))
            self._sendq.append((1, self.period124))            # stop
        if self._tx_left == 0 and self._sendq:
            self._tx_next_segment()

    def _tx_next_segment(self) -> None:
        if not self._sendq:
            self._tx_level = 1
            self._tx_left = 0
            return
        level, period = self._sendq.popleft()
        self._tx_level = level
        self._tx_left = self._tx_clock.advance()

    def _far_end_tick(self, cycle: int) -> None:
        if self._tx_left > 0:
            self._tx_left -= 1
        if self._tx_left == 0 and self._sendq:
            self._tx_next_segment()

    # -- far-end UART RX (decodes chip TX) -------------------------------
    def _decode(self, cycle: int, level: int) -> None:
        if self._rx_state == "idle":
            if level == 0:
                self._rx_state = "start"
                half = self._rx_half_clock.advance()
                self._rx_next = cycle + max(1, half)
                self._rx_cells = []
                self._rx_bits = []
            return
        if cycle < self._rx_next:
            return
        if self._rx_state == "start":
            if level != 0:
                self._rx_state = "idle"         # glitch, resync
                return
            self._rx_state = "data"
            self._rx_next = cycle + self._rx_clock.advance()
            return
        if self._rx_state == "data":
            self._rx_bits.append(level)
            self._rx_cells.append(level)
            if len(self._rx_bits) == 8:
                self._rx_state = "stop"
                self._rx_next = cycle + self._rx_clock.advance()
            else:
                self._rx_next = cycle + self._rx_clock.advance()
            return
        if self._rx_state == "stop":
            if level == 1:
                value = 0
                for i, b in enumerate(self._rx_bits):
                    value |= b << i
                self.frames.append(UartFrame(
                    byte=value, cells=[0] + list(self._rx_cells) + [1],
                    start_cycle=cycle))
            self._rx_state = "idle"

    # -- World callbacks --------------------------------------------------
    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        self._far_end_tick(cycle)
        level = (pins_out16 >> self.tx_pin) & 1
        if level != self._prev_rx or not self.wave_tx:
            self.wave_tx.append((cycle, level))
        self._prev_rx = level
        self._decode(cycle, level)
        if not self.wave_rx or self.wave_rx[-1][1] != self._tx_level:
            self.wave_rx.append((cycle, self._tx_level))

    # -- app side (host SPI bridge) --------------------------------------
    def app_send(self, data: bytes | Iterable[int], *, run: bool = True,
                 timeout_cycles: int | None = None) -> list[int]:
        """Host -> chip TX firmware (uart_tx.asm): push bytes + doorbell."""
        payload = bytes(bytearray(data))
        target = len(self.frames) + len(payload)
        for b in payload:
            if not self.link.tx_push(self.tx_tid, b):
                raise ProtocolError("chip TX RX-FIFO is full")
        self.link.write_csr(0x13, 1 << self.evt_bit)     # EVT doorbell
        if run and self.world is not None:
            budget = timeout_cycles or (len(payload) * 12 + 8) * max(
                1, self.period124 >> 4) + 2000
            self.world.run_until(lambda: len(self.frames) >= target, budget,
                                 "UART TX bytes on the wire")
        return list(payload)

    def app_recv(self, max_bytes: int | None = None) -> bytes:
        """Chip RX firmware (uart_rx.asm) -> host: drain the thread TX FIFO."""
        out = bytearray()
        while max_bytes is None or len(out) < max_bytes:
            b = self.link.rx_pop(self.rx_tid)
            if b is None:
                break
            out.append(b)
        return bytes(out)

    def wire_recv(self, clear: bool = True) -> bytes:
        """Bytes the chip transmitted on its TX pin, as decoded by the far end."""
        data = bytes(f.byte for f in self.frames)
        if clear:
            self.frames.clear()
            self.wave_tx.clear()
        return data

    def wire_flush(self, cycles: int | None = None) -> None:
        if self.world is None:
            return
        self.world.run(cycles or (self.period124 >> 4) * 12)

    # -- rendering -------------------------------------------------------
    def frame_waves(self, frames: Sequence[UartFrame] | None = None) -> list[str]:
        """ASCII cell diagram, one line per frame: ``S d0 d1 .. d7 P``."""
        frames = self.frames if frames is None else frames
        return [f"0x{f.byte:02X}  {f.cell_text()}" for f in frames]

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {
            "TX P[%d]" % self.tx_pin: format_wave(self.wave_tx, chars=chars),
            "RX P[%d]" % self.rx_pin: format_wave(self.wave_rx, chars=chars),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"UartBus(baud={self.baud}, tx_pin=P[{self.tx_pin}], "
                f"rx_pin=P[{self.rx_pin}])")

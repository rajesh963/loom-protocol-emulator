#!/usr/bin/env python3
"""LOOM protocol helpers — the *software side* of the protocol-emulator examples.

These classes are the far end of the wire.  They are used in two ways:

1. **Application side (works on real silicon).**  A LOOM protocol thread is a
   byte-stream endpoint: the host pushes bytes into the thread's RX FIFO
   (`TX_PUSH`) and reads bytes the thread produced from its TX FIFO (`RX_POP`),
   through `host/loom_host.py`'s `ByteTransport` (real chip) or `ModelLink`
   (golden model).  The `app_*` / `xfer` methods below do exactly that.  The
   firmware polls its own RX-FIFO occupancy every 256 cycles while idle, so no
   host doorbell is needed (the `evt_bit` constructor argument is kept for
   compatibility and documented as unused).

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
    "Ws2812", "Ps2Host", "OneWireMaster", "format_wave", "render_levels",
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
    """Cycles -> 12.4 fixed point (integer part <<4 | fractional sixteenths).

    Unlike the hardware register this is *not* masked to 16 bits, so a far-end
    model can represent slow bit periods (e.g. 6250 cycles at 9600 baud) that
    the chip builds from multiple CELLS event cells instead.
    """
    return int(round(float(cycles) * 16.0))


class Clock124:
    """A 12.4 fixed-point relaxation oscillator (matches `setp`/`waitd`).

    ``advance()`` returns this period's whole-cycle duration and carries the
    fractional remainder, so N periods are exactly N*period cycles (drift-free).
    """

    def __init__(self, period124: int):
        self.period = int(period124)
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
        self._ready = False                     # ignore the reset cycle
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
        if not self._ready:
            self._ready = True
            self._prev_rx = level
            self.wave_tx.append((cycle, level))
            self.wave_rx.append((cycle, self._tx_level))
            return
        if level != self._prev_rx or not self.wave_tx:
            self.wave_tx.append((cycle, level))
        self._prev_rx = level
        self._decode(cycle, level)
        if not self.wave_rx or self.wave_rx[-1][1] != self._tx_level:
            self.wave_rx.append((cycle, self._tx_level))

    # -- app side (host SPI bridge) --------------------------------------
    def app_send(self, data: bytes | Iterable[int], *, run: bool = True,
                 timeout_cycles: int | None = None) -> list[int]:
        """Host -> chip TX firmware (uart_tx.asm): push bytes and wait for the wire."""
        payload = bytes(bytearray(data))
        target = len(self.frames) + len(payload)
        # The RX FIFO is 8 deep; when it fills, step the model so the firmware
        # drains it (one byte per 10 bit periods) and retry the push.
        for b in payload:
            guard = 0
            while not self.link.tx_push(self.tx_tid, b):
                if self.world is None:
                    raise ProtocolError("chip TX RX-FIFO is full")
                self.world.run(1024)
                guard += 1
                if guard > 65536:
                    raise ProtocolError("chip TX RX-FIFO stuck full")
        if run and self.world is not None:
            budget = timeout_cycles or (len(payload) * 12 + 8) * max(
                1, self.period124 >> 4) + 2000
            self.world.run_until(lambda: len(self.frames) >= target, budget,
                                 "UART TX bytes on the wire")
            # let the TX thread finish draining and go back to sleep, so a
            # following operation does not contend with its event queue
            self.world.run(2 * 10 * max(1, self.period124 >> 4) + 512)
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

    def wire_recv(self, clear: bool = True, clear_wave: bool = False) -> bytes:
        """Bytes the chip transmitted on its TX pin, as decoded by the far end."""
        data = bytes(f.byte for f in self.frames)
        if clear:
            self.frames.clear()
        if clear_wave:
            self.wave_tx.clear()
            self.wave_rx.clear()
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

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {
            "TX P[%d]" % self.tx_pin: format_wave(self.wave_tx, chars=chars,
                                                  start=start, end=end),
            "RX P[%d]" % self.rx_pin: format_wave(self.wave_rx, chars=chars,
                                                  start=start, end=end),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"UartBus(baud={self.baud}, tx_pin=P[{self.tx_pin}], "
                f"rx_pin=P[{self.rx_pin}])")


# ---------------------------------------------------------------------------
# SPI — far-end target (for spi_master.asm)
# ---------------------------------------------------------------------------
@dataclass
class SpiTransaction:
    """One CS frame: the bytes the master shifted out and the bytes it got back."""

    mosi: list[int]
    miso: list[int]

    def text(self) -> str:
        return ("MOSI " + " ".join(f"{b:02X}" for b in self.mosi) +
                "  ->  MISO " + " ".join(f"{b:02X}" for b in self.miso))


class SpiBus(FarEnd):
    """A far-end SPI target: decodes the chip master's SCK/MOSI/CS, drives MISO.

    ``app_xfer`` pushes bytes to the chip SPI thread and reads back the bytes it
    shifted in (host SPI bridge).  With a `WireWorld` the far-end target is
    simulated cycle by cycle and the full MOSI/MISO transcript is recorded.
    """

    def __init__(self, world: WireWorld | None = None, *, link: Any | None = None,
                 sck_pin: int = 0, mosi_pin: int = 1, miso_pin: int = 2,
                 cs_pin: int = 3, mode: int = 0,
                 resp_fn: Callable[[int], int] | None = None,
                 tid: int = 2, evt_bit: int = 2):
        self.world = world
        self.link = link if link is not None else (world.link if world else None)
        if self.link is None:
            raise ProtocolError("SpiBus needs a world or a link")
        self.sck_pin, self.mosi_pin = int(sck_pin), int(mosi_pin)
        self.miso_pin, self.cs_pin = int(miso_pin), int(cs_pin)
        self.mode = int(mode) & 3
        self.cpol = (self.mode >> 1) & 1
        self.cpha = self.mode & 1
        self.tid = int(tid)
        self.evt_bit = int(evt_bit)
        self.name = "spi"
        self.resp_fn = resp_fn if resp_fn is not None else \
            (lambda b: (b + 1) & 0xFF)      # default fixture: increment loopback

        self._prev_sck = self.cpol
        self._prev_cs = 1
        self._ready = False
        self._active = False
        self._nsampled = 0
        self._bit = 0
        self._mosi_shift = 0
        self._resp = 0xFF
        self._miso_level = 1
        self._ext_value = 1 << self.miso_pin
        self._cur: SpiTransaction | None = None
        self.transactions: list[SpiTransaction] = []
        self.app_rx: list[int] = []
        self.wave: dict[int, list[tuple[int, int]]] = {
            self.sck_pin: [], self.mosi_pin: [], self.miso_pin: [], self.cs_pin: []}
        if world is not None:
            world.attach(self)

    @property
    def ext_mask(self) -> int:              # type: ignore[override]
        return 1 << self.miso_pin

    @property
    def ext_value(self) -> int:             # type: ignore[override]
        return self._ext_value

    def set_response(self, fn: Callable[[int], int]) -> None:
        """Set ``fn(mosi_byte) -> miso_byte`` (default: increment loopback)."""
        self.resp_fn = fn

    # -- decoding --------------------------------------------------------
    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        sck = (pins_out16 >> self.sck_pin) & 1
        mosi = (pins_out16 >> self.mosi_pin) & 1
        cs = (pins_out16 >> self.cs_pin) & 1
        if not self._ready:
            self._ready = True
            self._prev_sck, self._prev_cs = sck, cs
            return

        for pin, level in ((self.sck_pin, sck), (self.mosi_pin, mosi),
                           (self.cs_pin, cs), (self.miso_pin, self._miso_level)):
            hist = self.wave[pin]
            if not hist or hist[-1][1] != level:
                hist.append((cycle, level))

        if self._prev_cs and not cs:                    # CS falling: new frame
            self._active = True
            self._nsampled = 0
            self._bit = 0
            self._mosi_shift = 0
            self._cur = SpiTransaction(mosi=[], miso=[])
            self._drive_miso(0)
        elif not self._prev_cs and cs:                  # CS rising: frame done
            self._active = False
            if self._cur is not None:
                self.transactions.append(self._cur)
                self._cur = None
            self._miso_level = 1
            self._ext_value = 1 << self.miso_pin
        elif self._active:
            leading = (self._prev_sck == self.cpol and sck != self.cpol)
            trailing = (self._prev_sck != self.cpol and sck == self.cpol)
            if leading:
                if self.cpha == 0:
                    self._sample_mosi(mosi)
                else:
                    self._drive_miso(self._bit)
            elif trailing:
                if self.cpha == 0:
                    if self._nsampled == 8:
                        self._finish_byte()
                        self._nsampled = 0
                        self._bit = 0
                        self._drive_miso(0)            # next byte's bit 0
                    else:
                        self._bit = min(7, self._bit + 1)
                        self._drive_miso(self._bit)
                else:
                    self._sample_mosi(mosi)
                    if self._nsampled == 8:
                        self._finish_byte()
                        self._nsampled = 0
                        self._bit = 0
                    else:
                        self._bit = min(7, self._bit + 1)
        self._prev_sck = sck
        self._prev_cs = cs

    def _drive_miso(self, idx: int) -> None:
        self._miso_level = (self._resp >> (7 - idx)) & 1
        self._ext_value = (self._miso_level << self.miso_pin) | \
                          (self._ext_value & ~(1 << self.miso_pin))

    def _sample_mosi(self, level: int) -> None:
        self._mosi_shift = ((self._mosi_shift << 1) | level) & 0xFF
        self._nsampled += 1

    def _finish_byte(self) -> None:
        byte = self._mosi_shift & 0xFF
        miso = self._resp & 0xFF
        assert self._cur is not None
        self._cur.mosi.append(byte)
        self._cur.miso.append(miso)
        self.app_rx.append(miso)
        self._resp = self.resp_fn(byte) & 0xFF
        self._mosi_shift = 0

    # -- app side --------------------------------------------------------
    def app_xfer(self, data: bytes | Iterable[int], *, run: bool = True,
                 timeout_cycles: int | None = None) -> list[int]:
        """Host -> chip SPI master: queue bytes, then read the MISO bytes back."""
        payload = bytes(bytearray(data))
        start = len(self.transactions)
        for b in payload:
            if not self.link.tx_push(self.tid, b):
                raise ProtocolError("SPI thread RX FIFO full")
        if run and self.world is not None:
            budget = timeout_cycles or len(payload) * 4000 + 20000
            self.world.run_until(
                lambda: (len(self.transactions) - start) >= len(payload), budget,
                "SPI transactions")
            self.world.run(64)          # let the firmware PUSH the last byte
        out = []
        while True:
            b = self.link.rx_pop(self.tid)
            if b is None:
                break
            out.append(b)
        return out

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {f"{n} P[{p}]": format_wave(h, chars=chars, start=start, end=end)
                for n, p, h in (
                    ("SCK", self.sck_pin, self.wave[self.sck_pin]),
                    ("MOSI", self.mosi_pin, self.wave[self.mosi_pin]),
                    ("MISO", self.miso_pin, self.wave[self.miso_pin]),
                    ("CS_N", self.cs_pin, self.wave[self.cs_pin]))}

    def transcript(self) -> list[str]:
        return [t.text() for t in self.transactions]


class SpiMasterModel(FarEnd):
    """A far-end SPI master (for testing ``spi_slave.asm``).

    The chip is the *target*: SCK/MOSI/CS_N are chip input pins, MISO is a chip
    output pin.  ``xfer(bytes)`` clocks 8-bit frames at ``spi_hz`` and returns
    the bytes the chip shifted out.
    """

    def __init__(self, world: WireWorld, *, sck_pin: int = 4, mosi_pin: int = 5,
                 cs_pin: int = 6, miso_pin: int = 0, spi_hz: int = 1_000_000,
                 mode: int = 0, clock_hz: int | None = None):
        self.world = world
        self.sck_pin, self.mosi_pin = int(sck_pin), int(mosi_pin)
        self.cs_pin, self.miso_pin = int(cs_pin), int(miso_pin)
        self.clock_hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.half = Clock124(fixed124(self.clock_hz / (2.0 * spi_hz)))
        self.mode = mode & 3
        self.cpol = (self.mode >> 1) & 1
        self.cpha = self.mode & 1
        self.name = "spi-master"
        self._cs = 1
        self._sck = self.cpol
        self._mosi = 0
        self._miso = 1
        self._state = "idle"
        self._left = 0
        self._pending: deque[int] = deque()
        self._byte = 0
        self._bit_index = 0
        self._rx = 0
        self.received: list[int] = []
        self.sent: list[int] = []
        self.wave: dict[int, list[tuple[int, int]]] = {
            self.sck_pin: [], self.mosi_pin: [], self.cs_pin: [], self.miso_pin: []}
        world.attach(self)

    @property
    def ext_mask(self) -> int:              # type: ignore[override]
        return (1 << self.sck_pin) | (1 << self.mosi_pin) | (1 << self.cs_pin)

    @property
    def ext_value(self) -> int:             # type: ignore[override]
        return ((self._sck & 1) << self.sck_pin | (self._mosi & 1) << self.mosi_pin
                | (self._cs & 1) << self.cs_pin)

    def xfer(self, data: bytes | Iterable[int],
             max_cycles: int | None = None) -> list[int]:
        payload = bytes(bytearray(data))
        start = len(self.received)
        self._pending.extend(payload)
        self.sent.extend(payload)
        budget = max_cycles or (len(payload) * 12 + 8) * max(
            1, (self.half.period >> 4) * 2 + 2) + 200
        self.world.run_until(lambda: len(self.received) - start >= len(payload),
                             budget, "SPI slave response")
        return self.received[start:]

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        self._miso = (pins_out16 >> self.miso_pin) & 1
        for pin, level in ((self.sck_pin, self._sck), (self.mosi_pin, self._mosi),
                           (self.cs_pin, self._cs), (self.miso_pin, self._miso)):
            hist = self.wave[pin]
            if not hist or hist[-1][1] != level:
                hist.append((cycle, level))
        if self._left > 0:
            self._left -= 1
        if self._left > 0:
            return
        self._tick()

    def _bit_of(self, b: int, idx: int) -> int:
        return (b >> (7 - idx)) & 1 if idx < 8 else 0

    def _tick(self) -> None:
        if self._state == "idle":
            if not self._pending:
                return
            self._byte = self._pending.popleft()
            self._bit_index = 0
            self._rx = 0
            self._cs = 0
            self._sck = self.cpol
            self._mosi = self._bit_of(self._byte, 0)
            self._left = self.half.advance()
            self._state = "lead"
            return
        if self._state == "lead":
            self._sck = 1 - self.cpol
            if self.cpha == 0:
                self._rx = ((self._rx << 1) | self._miso) & 0xFF
            else:
                self._mosi = self._bit_of(self._byte, self._bit_index)
            self._left = self.half.advance()
            self._state = "trail"
            return
        if self._state == "trail":
            self._sck = self.cpol
            if self.cpha == 0:
                self._mosi = self._bit_of(self._byte, self._bit_index + 1)
            else:
                self._rx = ((self._rx << 1) | self._miso) & 0xFF
            self._bit_index += 1
            self._left = self.half.advance()
            self._state = "end" if self._bit_index == 8 else "lead"
            return
        if self._state == "end":
            self._cs = 1
            self._sck = self.cpol
            self._left = self.half.advance()
            self.received.append(self._rx & 0xFF)
            self._state = "idle"

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {f"{n} P[{p}]": format_wave(h, chars=chars) for n, p, h in (
            ("SCK", self.sck_pin, self.wave[self.sck_pin]),
            ("MOSI", self.mosi_pin, self.wave[self.mosi_pin]),
            ("MISO", self.miso_pin, self.wave[self.miso_pin]),
            ("CS_N", self.cs_pin, self.wave[self.cs_pin]))}


# ---------------------------------------------------------------------------
# I2C — far-end target with a register file (for i2c_master.asm)
# ---------------------------------------------------------------------------
@dataclass
class I2cTransaction:
    """One I2C message as seen by the far-end target."""

    kind: str                       # "write" | "read"
    address: int                    # 7-bit device address
    payload: list[int]              # bytes written or read by the master
    ops: list[str]

    def text(self) -> str:
        return " ".join(self.ops)


class I2cBus(FarEnd):
    """A far-end I2C target with an 8-byte auto-increment register file.

    The chip is the I2C *master* (`i2c_master.asm`); this model answers the
    address, drives ACK/NAK and the read data.  Both lines are open-drain: the
    far end pulls a line low by setting its ext bit to 0 and releases it
    (external pull-up) with 1.  ``stretch_cycles`` > 0 stretches the clock on
    every ACK (target holds SCL low after the master releases it), which
    exercises the master's WAITP clock-stretch tolerance.
    """

    def __init__(self, world: WireWorld | None = None, *, link: Any | None = None,
                 scl_pin: int = 4, sda_pin: int = 5, address: int = 0x50,
                 stretch_cycles: int = 0, registers: Sequence[int] | None = None,
                 tid: int = 3, evt_bit: int = 3):
        self.world = world
        self.link = link if link is not None else (world.link if world else None)
        if self.link is None:
            raise ProtocolError("I2cBus needs a world or a link")
        self.scl_pin, self.sda_pin = int(scl_pin), int(sda_pin)
        self.address = int(address) & 0x7F
        self.stretch_cycles = max(0, int(stretch_cycles))
        self.regs = [int(v) & 0xFF for v in (registers or [0] * 8)]
        self.ptr = 0
        self.tid = int(tid)
        self.evt_bit = int(evt_bit)
        self.name = "i2c"
        self._ext = 0xFFFF
        self._prev_scl = self._prev_sda = 1
        self._ready = False
        self._state = "idle"
        self._bits = 0
        self._shift = 0
        self._rbit = 0
        self._curbyte = 0
        self._is_read = False
        self._matched = False
        self._nwrite = 0
        self._ack_level = 1
        self._stretch_left = 0
        self._cur: I2cTransaction | None = None
        self.transactions: list[I2cTransaction] = []
        self.wave: dict[int, list[tuple[int, int]]] = {
            self.scl_pin: [], self.sda_pin: []}
        if world is not None:
            world.attach(self)

    @property
    def ext_mask(self) -> int:          # type: ignore[override]
        return (1 << self.scl_pin) | (1 << self.sda_pin)

    @property
    def ext_value(self) -> int:         # type: ignore[override]
        return self._ext

    def _wire(self, pout: int, dir8: int, bit: int) -> int:
        if (dir8 >> bit) & 1:
            return (pout >> bit) & 1
        return (self._ext >> bit) & 1

    def _set_sda(self, level: int) -> None:
        self._ext = (self._ext & ~(1 << self.sda_pin)) | ((level & 1) << self.sda_pin)

    def _release_sda(self) -> None:
        self._ext |= 1 << self.sda_pin

    def set_register(self, index: int, value: int) -> None:
        self.regs[int(index) & 7] = int(value) & 0xFF

    # -- bus state machine ----------------------------------------------
    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        scl = self._wire(pins_out16, pins_dir8, self.scl_pin)
        sda = self._wire(pins_out16, pins_dir8, self.sda_pin)
        if not self._ready:
            self._ready = True
            self._prev_scl, self._prev_sda = scl, sda
            self.wave[self.scl_pin].append((cycle, scl))
            self.wave[self.sda_pin].append((cycle, sda))
            return
        for pin, level in ((self.scl_pin, scl), (self.sda_pin, sda)):
            hist = self.wave[pin]
            if not hist or hist[-1][1] != level:
                hist.append((cycle, level))

        if self._stretch_left > 0:
            self._stretch_left -= 1
            if self._stretch_left == 0:
                self._ext |= 1 << self.scl_pin
        rising = self._prev_scl == 0 and scl == 1
        falling = self._prev_scl == 1 and scl == 0
        if self._prev_scl == 1 and scl == 1:
            if self._prev_sda == 1 and sda == 0:            # START / repeated START
                self._begin(repeated=self._state != "idle")
            elif self._prev_sda == 0 and sda == 1:          # STOP
                self._finish()
        elif rising:
            self._on_rising(sda)
        elif falling:
            self._on_falling()
        self._prev_scl, self._prev_sda = scl, sda

    def _begin(self, repeated: bool) -> None:
        if self._cur is not None:
            self._cur.ops.append("Sr" if repeated else "S")
            self.transactions.append(self._cur)
        self._cur = I2cTransaction(kind="?", address=0, payload=[],
                                   ops=["Sr" if repeated else "S"])
        self._state = "addr"
        self._bits = 0
        self._shift = 0
        self._nwrite = 0

    def _finish(self) -> None:
        if self._cur is not None:
            self._cur.ops.append("P")
            self.transactions.append(self._cur)
            self._cur = None
        self._state = "idle"
        self._ext |= (1 << self.scl_pin) | (1 << self.sda_pin)

    def _stretch(self) -> None:
        if self.stretch_cycles:
            self._ext &= ~(1 << self.scl_pin)
            self._stretch_left = self.stretch_cycles

    def _on_rising(self, sda: int) -> None:
        st = self._state
        if st == "addr":
            self._shift = ((self._shift << 1) | sda) & 0xFF
            self._bits += 1
            if self._bits == 8:
                self._state = "addr_ack_wait"
        elif st == "wdata":
            self._shift = ((self._shift << 1) | sda) & 0xFF
            self._bits += 1
            if self._bits == 8:
                self._state = "wack_wait"
        elif st == "rdata":
            if self._rbit >= 8:
                self._ack_level = sda
                self._state = "rack_wait"
            else:
                self._rbit += 1
        elif st == "rack_wait":
            self._ack_level = sda

    def _on_falling(self) -> None:
        st = self._state
        if st == "addr_ack_wait":
            self._matched = (self._shift >> 1) == self.address
            self._is_read = bool(self._shift & 1)
            self._cur.address = self._shift >> 1
            self._cur.ops.append(f"0x{self._shift:02X}")
            if self._matched:
                self._set_sda(0)
                self._cur.ops.append("A")
                self._stretch()
            else:
                self._release_sda()
                self._cur.ops.append("N")
            self._state = "addr_ack"
        elif st == "addr_ack":
            self._release_sda()
            if not self._matched:
                self._state = "wait_stop"
            elif self._is_read:
                self._cur.kind = "read"
                self._curbyte = self.regs[self.ptr]
                self._cur.ops.append(f"0x{self._curbyte:02X}")
                self._rbit = 0
                self._set_sda((self._curbyte >> 7) & 1)
                self._state = "rdata"
            else:
                self._cur.kind = "write"
                self._bits = 0
                self._shift = 0
                self._state = "wdata"
        elif st == "wack_wait":
            self._set_sda(0)                         # ACK this data byte
            self._stretch()
            self._state = "wack"
        elif st == "wack":
            self._release_sda()
            value = self._shift & 0xFF
            self._cur.ops.append(f"0x{value:02X}")
            if self._nwrite == 0:
                self.ptr = value & 7                 # first data byte = pointer
            else:
                self.regs[self.ptr] = value
                self.ptr = (self.ptr + 1) & 7
                self._cur.payload.append(value)
            self._nwrite += 1
            self._bits = 0
            self._shift = 0
            self._state = "wdata"
        elif st == "rdata":
            if self._rbit < 8:
                self._set_sda((self._curbyte >> (7 - self._rbit)) & 1)
            else:
                self._release_sda()
        elif st == "rack_wait":
            if self._ack_level == 0:                 # master ACK -> next byte
                self.regs[self.ptr] = self._curbyte
                self._cur.payload.append(self._curbyte)
                self.ptr = (self.ptr + 1) & 7
                self._curbyte = self.regs[self.ptr]
                self._rbit = 0
                self._set_sda((self._curbyte >> 7) & 1)
                self._state = "rdata"
            else:                                    # NAK -> done
                self.regs[self.ptr] = self._curbyte
                self._cur.payload.append(self._curbyte)
                self._release_sda()
                self._state = "wait_stop"

    # -- app side (host SPI bridge) --------------------------------------
    def _completed(self, start: int, kind: str) -> bool:
        return any(t.kind == kind and t.ops and t.ops[-1] == "P"
                   for t in self.transactions[start:])

    def app_write(self, address: int, register: int, data: int, *,
                  timeout_cycles: int | None = None) -> None:
        """Host -> chip I2C master: START addr+W, reg, data, STOP."""
        if self.world is None:
            raise ProtocolError("I2cBus.app_write needs a WireWorld")
        start = len(self.transactions)
        for b in (0x00, (address & 0x7F) << 1, register & 0xFF, data & 0xFF):
            if not self.link.tx_push(self.tid, b):
                raise ProtocolError("I2C thread RX FIFO full")
        self.world.run_until(lambda: self._completed(start, "write"),
                             timeout_cycles or 40000, "I2C write")

    def app_read(self, address: int, register: int, *,
                 timeout_cycles: int | None = None) -> int | None:
        """Host -> chip I2C master: START addr+W, reg, Sr, addr+R, byte, NAK, STOP."""
        if self.world is None:
            raise ProtocolError("I2cBus.app_read needs a WireWorld")
        start = len(self.transactions)
        for b in (0x01, (address & 0x7F) << 1, register & 0xFF, 0x00):
            if not self.link.tx_push(self.tid, b):
                raise ProtocolError("I2C thread RX FIFO full")
        self.world.run_until(lambda: self._completed(start, "read"),
                             timeout_cycles or 40000, "I2C read")
        self.world.run(32)                       # let the firmware PUSH the byte
        return self.link.rx_pop(self.tid)

    def transcript(self) -> list[str]:
        return [t.text() for t in self.transactions]

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {f"{n} P[{p}]": format_wave(h, chars=chars, start=start, end=end)
                for n, p, h in (
                    ("SCL", self.scl_pin, self.wave[self.scl_pin]),
                    ("SDA", self.sda_pin, self.wave[self.sda_pin]))}


class I2cMasterModel(FarEnd):
    """A far-end I2C master (for testing ``i2c_target.asm``).

    Drives SCL/SDA (open-drain, ext bit 0 = pull low) with a queue of half-cycle
    micro-ops, and tolerates the chip target stretching the clock.  Provides the
    standard register protocol: ``START addr+W reg (data)* STOP`` and
    ``START addr+W reg Sr addr+R (data)* NAK STOP``.
    """

    def __init__(self, world: WireWorld, *, scl_pin: int = 6, sda_pin: int = 7,
                 i2c_hz: int = 100_000, clock_hz: int | None = None):
        self.world = world
        self.scl_pin, self.sda_pin = int(scl_pin), int(sda_pin)
        self.clock_hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.half = Clock124(fixed124(self.clock_hz / (2.0 * i2c_hz)))
        self.name = "i2c-master"
        self._ext = 0xFFFF
        self._ops: deque[tuple[int, int, str]] = deque()
        self._cur: tuple[int, int, str] | None = None
        self._left = 0
        self._busy = False
        self._rx = 0
        self._rx_bits = 0
        self._expect_scl = 1
        self.received: list[int] = []
        self.transactions: list[str] = []
        self._log: list[str] = []
        self.wave: dict[int, list[tuple[int, int]]] = {
            self.scl_pin: [], self.sda_pin: []}
        self._prev = (1, 1)
        world.attach(self)

    @property
    def ext_mask(self) -> int:          # type: ignore[override]
        return (1 << self.scl_pin) | (1 << self.sda_pin)

    @property
    def ext_value(self) -> int:         # type: ignore[override]
        return self._ext

    def _wire(self, pout: int, dir8: int, bit: int) -> int:
        if (dir8 >> bit) & 1:
            return (pout >> bit) & 1
        return (self._ext >> bit) & 1

    # -- transaction builders -------------------------------------------
    def _op(self, scl: int, sda: int, tag: str = "") -> None:
        self._ops.append((scl, sda, tag))

    def _tx_bit(self, value: int) -> None:
        # SDA changes on the SCL falling edge (0-cycle hold): the same stress
        # the RTL VIP applies, so this model catches setup-time-sensitive
        # target firmware bugs.
        self._op(0, value, "sda")
        self._op(1, value, "clk")

    def _rx_bit(self) -> None:
        self._op(0, 1, "rel")
        self._op(1, 1, "rx")
        self._op(0, 1)

    def _mack(self, ack: int, drive: bool = False) -> None:
        """ACK slot: drive=True -> master drives SDA (read path),
        drive=False -> release SDA and let the target answer (write path)."""
        sda_hi = (1 if ack else 0) if drive else 1
        self._op(0, 1 if drive else 1, "rel")
        self._op(1, sda_hi, "mack")
        self._op(0, sda_hi)

    def _tx_byte(self, value: int) -> None:
        for i in range(7, -1, -1):
            self._tx_bit((value >> i) & 1)

    def _rx_byte(self) -> None:
        for _ in range(8):
            self._rx_bit()

    def _start(self, repeated: bool = False) -> None:
        if repeated:
            self._op(0, 1, "rel")
            self._op(1, 1)
        self._op(1, 0)                      # SDA falls while SCL high
        self._op(0, 0)

    def _stop(self) -> None:
        self._op(0, 0)
        self._op(1, 0)
        self._op(1, 1, "stop")              # SDA rises while SCL high
        self._op(1, 1, "idle")

    def write(self, address: int, data: Iterable[int], *,
              max_cycles: int | None = None) -> None:
        payload = [int(b) & 0xFF for b in data]
        self._ops.clear()
        self._log = [f"S {address:02X}W"]
        self._start()
        self._tx_byte((address << 1) | 0)
        self._mack(0)
        for b in payload:
            self._tx_byte(b)
            self._mack(0)
        self._stop()
        self._begin(max_cycles)

    def read(self, address: int, register: int, count: int = 1, *,
             max_cycles: int | None = None) -> list[int]:
        self._ops.clear()
        self._log = [f"S {address:02X}W reg{register:02X} Sr {address:02X}R"]
        self._start()
        self._tx_byte((address << 1) | 0)
        self._mack(0)
        self._tx_byte(register & 0xFF)
        self._mack(0)
        self._start(repeated=True)
        self._tx_byte((address << 1) | 1)
        self._mack(0)
        for n in range(count):
            self._rx_byte()
            self._mack(1 if n == count - 1 else 0, drive=True)   # NAK last
        self._stop()
        self._begin(max_cycles)
        return list(self.received[-count:])

    def _begin(self, max_cycles: int | None) -> None:
        self.received.clear()
        self._busy = True
        self._cur = None
        self._left = 0
        self.world.run_until(lambda: not self._busy,
                             max_cycles or 200_000, "I2C master job")
        # let the chip target finish its STOP handling and return to idle
        self.world.run(4 * (self.half.period >> 4) + 128)
        self.transactions.append(" ".join(self._log))

    # -- engine ---------------------------------------------------------
    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        scl = self._wire(pins_out16, pins_dir8, self.scl_pin)
        sda = self._wire(pins_out16, pins_dir8, self.sda_pin)
        for pin, level in ((self.scl_pin, scl), (self.sda_pin, sda)):
            hist = self.wave[pin]
            if not hist or hist[-1][1] != level:
                hist.append((cycle, level))
        if not self._busy:
            return
        # clock stretching: if our micro-op drives SCL high but the wire is
        # still low (target stretching), do not advance time.
        if self._left > 0:
            if self._expect_scl == 1 and scl == 0:
                return
            self._left -= 1
        if self._left > 0:
            return
        if self._cur is not None:
            self._complete(self._cur, sda)
            self._cur = None
        if not self._ops:
            self._busy = False
            self._ext |= (1 << self.scl_pin) | (1 << self.sda_pin)
            return
        op = self._ops.popleft()
        self._cur = op
        self._ext = (((op[0] & 1) << self.scl_pin) |
                     ((op[1] & 1) << self.sda_pin))
        self._expect_scl = op[0]
        self._left = max(1, self.half.advance())

    def _complete(self, op: tuple[int, int, str], sda: int) -> None:
        tag = op[2]
        if tag == "rx":
            self._rx = ((self._rx << 1) | sda) & 0xFF
            self._rx_bits += 1
            if self._rx_bits == 8:
                self.received.append(self._rx)
                self._rx = 0
                self._rx_bits = 0

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {f"{n} P[{p}]": format_wave(h, chars=chars) for n, p, h in (
            ("SCL", self.scl_pin, self.wave[self.scl_pin]),
            ("SDA", self.sda_pin, self.wave[self.sda_pin]))}


# ---------------------------------------------------------------------------
# WS2812 / WS2812B — far-end LED-strip decoder (for ws2812.asm)
# ---------------------------------------------------------------------------
class Ws2812(FarEnd):
    """Decode a WS2812 data line into GRB bytes and LED colours.

    A bit is a high pulse whose width encodes 0 (short) or 1 (long); a low
    period longer than the latch threshold ends the frame.  The decoder measures
    the pulse widths from the model's cycle-accurate pin trace, so it checks the
    firmware's 12.4 fixed-point event schedule, not just the byte order.
    """

    def __init__(self, world: WireWorld, *, data_pin: int = 12,
                 one_threshold: int = 36, reset_cycles: int = 1500):
        self.world = world
        self.data_pin = int(data_pin)
        self.one_threshold = int(one_threshold)
        self.reset_cycles = int(reset_cycles)
        self.name = "ws2812"
        self._prev = 1
        self._ready = False
        self._rise = 0
        self._fall = 0
        self._bits: list[int] = []
        self.bits: list[int] = []
        self.bytes: list[int] = []
        self.leds: list[tuple[int, int, int]] = []      # (G, R, B)
        self.pulses: list[tuple[int, int, int]] = []    # (rise_cycle, high, low)
        self.wave: list[tuple[int, int]] = [(0, 0)]
        world.attach(self)

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        level = (pins_out16 >> self.data_pin) & 1
        if not self._ready:
            self._ready = True
            self._prev = level
            self.wave = [(cycle, level)]
            self._rise = self._fall = cycle
            return
        if level != self._prev:
            self.wave.append((cycle, level))
            if level:
                self._rise = cycle
            else:
                self._fall = cycle
                high = cycle - self._rise
                self.pulses.append((self._rise, high, 0))
                self._bits.append(1 if high > self.one_threshold else 0)
                self.bits = list(self._bits)
        elif not level and self._bits and (cycle - self._fall) > self.reset_cycles:
            self._flush()          # long low = latch: end of the LED frame
        self._prev = level

    def _flush(self) -> None:
        bits = self._bits
        for i in range(0, len(bits) - len(bits) % 24, 24):
            chunk = bits[i:i + 24]
            value = 0
            for b in chunk:
                value = (value << 1) | b
            g = (value >> 16) & 0xFF
            r = (value >> 8) & 0xFF
            b = value & 0xFF
            self.bytes.extend([g, r, b])
            self.leds.append((g, r, b))
        self._bits = []

    def flush(self) -> None:
        """Force a frame flush (e.g. at the end of a capture)."""
        if self._bits:
            self._flush()

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {f"DATA P[{self.data_pin}]": format_wave(self.wave, chars=chars,
                                                        start=start, end=end)}


# ---------------------------------------------------------------------------
# PS/2 — far-end host receiver (for ps2_target.asm)
# ---------------------------------------------------------------------------
class Ps2Host(FarEnd):
    """A PS/2 host receiver: samples DATA on CLK falling edges, checks parity.

    The chip is the PS/2 *device* (ps2_target.asm): CLK and DATA are chip output
    pins, the host only samples.  Frames are 11 bits: start(0), 8 data LSB
    first, odd parity, stop(1).
    """

    def __init__(self, world: WireWorld, *, clk_pin: int = 13, data_pin: int = 14):
        self.world = world
        self.clk_pin, self.data_pin = int(clk_pin), int(data_pin)
        self.name = "ps2-host"
        self._prev_clk = 1
        self._ready = False
        self._bits: list[int] = []
        self.bytes: list[int] = []
        self.frames: list[dict] = []
        self.wave: dict[int, list[tuple[int, int]]] = {
            self.clk_pin: [], self.data_pin: []}
        world.attach(self)

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        clk = (pins_out16 >> self.clk_pin) & 1
        data = (pins_out16 >> self.data_pin) & 1
        for pin, level in ((self.clk_pin, clk), (self.data_pin, data)):
            hist = self.wave[pin]
            if not hist or hist[-1][1] != level:
                hist.append((cycle, level))
        if not self._ready:
            self._ready = True
            self._prev_clk = clk
            return
        if self._prev_clk == 1 and clk == 0:          # falling edge: sample DATA
            self._bits.append(data)
            if len(self._bits) == 11:
                bits = self._bits
                self._bits = []
                payload = bits[1:9]
                value = 0
                for i, b in enumerate(payload):
                    value |= b << i
                ones = sum(payload) + bits[9]
                ok = bits[0] == 0 and bits[10] == 1 and (ones % 2) == 1
                self.frames.append(dict(byte=value, bits=bits, parity_ok=ok))
                if ok:
                    self.bytes.append(value)
        self._prev_clk = clk

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {f"{n} P[{p}]": format_wave(h, chars=chars, start=start, end=end)
                for n, p, h in (
                    ("CLK", self.clk_pin, self.wave[self.clk_pin]),
                    ("DATA", self.data_pin, self.wave[self.data_pin]))}


# ---------------------------------------------------------------------------
# 1-Wire — far-end master (for onewire.asm, DS18B20-style device)
# ---------------------------------------------------------------------------
class OneWireMaster(FarEnd):
    """A 1-Wire master for the `onewire.asm` device example.

    Drives DQ (open drain: ext bit 0 = pull low, 1 = release to the pull-up) and
    implements standard-speed reset/presence, write-0/1 slots and read slots at
    60 MHz (1 us = 60 cycles).  After a reset the device answers with a presence
    pulse; ROM/function bytes are then transferred with slot timing.
    """

    def __init__(self, world: WireWorld, *, dq_pin: int = 7):
        self.world = world
        self.dq_pin = int(dq_pin)
        self.name = "1wire-master"
        self._ext = 0xFFFF
        self._level = 1
        self.frames: list[bytes] = []
        self.wave: list[tuple[int, int]] = []
        world.attach(self)

    @property
    def ext_mask(self) -> int:          # type: ignore[override]
        return 1 << self.dq_pin

    @property
    def ext_value(self) -> int:         # type: ignore[override]
        return self._ext

    # -- low-level wire control -----------------------------------------
    def _pull(self, low: bool) -> None:
        if low:
            self._ext &= ~(1 << self.dq_pin)
        else:
            self._ext |= 1 << self.dq_pin

    def _run(self, cycles: int) -> None:
        self.world.run(int(cycles))

    def _sample(self) -> int:
        return self._level

    # -- protocol ---------------------------------------------------------
    def reset(self, *, low_cycles: int = 30_000) -> bool:
        """RESET pulse (500 us low), then return True if a PRESENCE pulse was seen."""
        self._pull(True)
        self._run(low_cycles)
        self._pull(False)
        self._run(1_800)                    # 30 us: device presence starts
        self._run(2_400)                    # sample at ~70 us after release
        presence = self._sample() == 0
        self._run(18_000)                   # finish the presence window (~370 us)
        return presence

    def write_byte(self, value: int) -> None:
        """Send 8 bits LSB-first: 1 = 6 us low slot, 0 = 60 us low slot."""
        for i in range(8):
            bit = (value >> i) & 1
            self._pull(True)
            self._run(360 if bit else 3_600)      # 6 us / 60 us low
            self._pull(False)
            self._run(1_800)                      # 30 us recovery
        self.frames.append(bytes([value]))

    def read_byte(self) -> int:
        """Clock 8 read slots; sample DQ 15 us into each slot."""
        value = 0
        for i in range(8):
            self._pull(True)
            self._run(180)                        # 3 us start pulse
            self._pull(False)
            self._run(720)                        # sample at ~15 us
            bit = 1 if self._sample() else 0      # device pulls low for a 0 bit
            value |= bit << i
            self._run(2_700)                      # finish the 60 us slot
            self._run(600)                        # 10 us recovery
        return value

    def read_scratchpad(self, count: int = 9) -> bytes:
        return bytes(self.read_byte() for _ in range(count))

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        level = self._wire_level(pins_out16, pins_dir8, self._ext, self.dq_pin)
        self._level = level
        if not self.wave or self.wave[-1][1] != level:
            self.wave.append((cycle, level))

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {f"DQ P[{self.dq_pin}]": format_wave(self.wave, chars=chars,
                                                   start=start, end=end)}

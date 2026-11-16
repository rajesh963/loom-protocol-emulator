#!/usr/bin/env python3
"""LOOM SPI bit-bang layer (owner: toolchain).

Pin-level SPI mode-0 master used by host-side clients (MicroPython/PIO, a USB
SPI dongle, a cocotb SPI driver, or a GPIO bit-banging script).  It produces the
exact `H_CS_N` / `H_SCK` / `H_MOSI` / `H_MISO` sequence the LOOM host bridge
expects (spec §6): SPI mode 0, MSB first, chip select active low, MISO sampled
on the **rising** edge of SCK, MOSI changed on the **falling** edge, one byte
in / one byte out per 8 SCK cycles, multi-byte args LSB first.

Pin mapping (Tiny Tapeout top):
    H_CS_N = ui_in[0]      H_SCK = ui_in[1]      H_MOSI = ui_in[2]
    H_MISO = uo_out[0]     H_IRQ = uo_out[1]

Classes
-------
    PinIO            abstract pin interface: set_cs(), set_sck(), set_mosi(),
                     read_miso(), optional delay() and cs_idle()
    SPIBitBang       one bit-banged SPI master (transfer(byte) -> byte)
    BitBangTransport `host.loom_host.Transport` adapter: use any SPIBitBang as
                     the transport for `ByteTransport` (CS is asserted once per
                     command / payload group, see `command_window`)
    FakeChipSPI      tiny loopback "chip" that samples MOSI on SCK rising edges
                     and drives MISO; used by the self-test and by host tests

Semantics / decisions
---------------------
  * `SPIBitBang.transfer()` performs one 8-bit transfer with CS already low
    (assert_cs()/release_cs() are explicit) so a whole command `[CMD][ARG...]`
    stays inside one CS window, as the hardware command FSM requires.
  * `BitBangTransport.xfer()` is the byte-level adapter: it asserts CS, does the
    byte, and releases CS if `auto_cs` is set (default True).  For multi-byte
    commands use `ByteTransport`'s framing inside `transport.command_window()`
    (context manager) to hold CS low for the whole command.
  * MISO is sampled at the SCK rising edge (mode 0); MOSI is set before the
    rising edge and held through it, changing only while SCK is low.
  * `delay()` is called after every level change; it defaults to no-op.  A real
    GPIO client passes e.g. `time.sleep_us(1)` for a <=10 MHz SCK (spec limit).

MicroPython sketch (copy/paste, not executed here)
-------------------------------------------------
    from machine import Pin
    from host.spi_bitbang import PinIO, SPIBitBang
    from host.loom_host import ByteTransport

    class MicroPinIO(PinIO):
        def __init__(self):
            self.cs  = Pin(cs_pin,  Pin.OUT, value=1)
            self.sck = Pin(sck_pin, Pin.OUT, value=0)
            self.mosi= Pin(mosi_pin,Pin.OUT, value=0)
            self.miso= Pin(miso_pin,Pin.IN)
        def set_cs(self, v):  self.cs.value(int(v))
        def set_sck(self, v): self.sck.value(int(v))
        def set_mosi(self, v):self.mosi.value(int(v))
        def read_miso(self):  return self.miso.value()

    host = ByteTransport(BitBangTransport(SPIBitBang(MicroPinIO())))
    host.ping(); host.write_mem(0, program_words); host.run(0xF)
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any, Callable, Iterable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from host.loom_host import ByteTransport, ModelTransport, Transport  # type: ignore
else:
    from host.loom_host import ByteTransport, ModelTransport, Transport

__all__ = ["PinIO", "SPIBitBang", "BitBangTransport", "FakeChipSPI"]


class PinIO:
    """Abstract 4-wire SPI pin interface (level-level, no clock implied)."""

    def set_cs(self, value: int) -> None:       # pragma: no cover - interface
        raise NotImplementedError

    def set_sck(self, value: int) -> None:      # pragma: no cover - interface
        raise NotImplementedError

    def set_mosi(self, value: int) -> None:     # pragma: no cover - interface
        raise NotImplementedError

    def read_miso(self) -> int:                 # pragma: no cover - interface
        raise NotImplementedError

    def delay(self) -> None:
        """Half-SCK-period settle delay (no-op by default)."""


class SPIBitBang:
    """SPI mode-0 master, MSB first, CS active low."""

    def __init__(self, pins: PinIO, *, cs_active_low: bool = True):
        if not isinstance(pins, PinIO) and not all(
                hasattr(pins, name) for name in
                ("set_cs", "set_sck", "set_mosi", "read_miso")):
            raise TypeError("SPIBitBang needs a PinIO-like object")
        self.pins = pins
        self.cs_active_low = cs_active_low
        self.cs_level = 1 if cs_active_low else 0
        self.cs_asserted = False
        self.set_idle()

    # -- chip select -----------------------------------------------------
    def set_idle(self) -> None:
        self.pins.set_sck(0)
        self.pins.set_mosi(0)
        self.pins.set_cs(1 if self.cs_active_low else 0)
        self.cs_asserted = False

    def assert_cs(self) -> None:
        self.pins.set_sck(0)
        self.pins.set_cs(0 if self.cs_active_low else 1)
        self.cs_asserted = True
        self.pins.delay()

    def release_cs(self) -> None:
        self.pins.set_sck(0)
        if self.cs_active_low:
            self.pins.set_cs(1)
        else:
            self.pins.set_cs(0)
        self.cs_asserted = False
        self.pins.delay()

    # -- one byte --------------------------------------------------------
    def transfer(self, mosi_byte: int) -> int:
        """Full-duplex 8-bit transfer; CS must be asserted by the caller."""
        miso = 0
        byte = int(mosi_byte) & 0xFF
        for bit in range(7, -1, -1):            # MSB first
            self.pins.set_mosi((byte >> bit) & 1)
            self.pins.delay()
            self.pins.set_sck(1)                # rising edge: chip samples MOSI
            self.pins.delay()
            miso = (miso << 1) | (self.pins.read_miso() & 1)
            self.pins.set_sck(0)                # falling edge: chip drives MISO
            self.pins.delay()
        return miso & 0xFF

    # -- convenience -----------------------------------------------------
    def transfer_bytes(self, payload: Iterable[int]) -> list[int]:
        return [self.transfer(b) for b in payload]

    def command_window(self):
        """Context manager holding CS low for a whole `[CMD][ARG...]` group."""
        return _CSWindow(self)


class _CSWindow:
    def __init__(self, spi: SPIBitBang):
        self.spi = spi

    def __enter__(self) -> SPIBitBang:
        self.spi.assert_cs()
        return self.spi

    def __exit__(self, *exc: Any) -> None:
        self.spi.release_cs()


class BitBangTransport(Transport):
    """`Transport` adapter: lets ByteTransport drive a bit-banged SPI master.

    Each `xfer()` is one SPI byte in its own CS window (``auto_cs=True``).  To
    keep a whole LOOM command in one CS window, wrap a group of `ByteTransport`
    calls in the transport's ``command_window()`` context manager; inside it,
    `xfer()` no longer touches CS.
    """

    def __init__(self, spi: SPIBitBang, *, auto_cs: bool = True,
                 cs_setup: bool = True):
        self.spi = spi
        self.auto_cs = auto_cs
        self.cs_setup = cs_setup
        self._in_window = 0

    def command_window(self):
        return _TransportWindow(self)

    def xfer(self, byte: int) -> int:
        if self._in_window:
            return self.spi.transfer(byte)
        if self.auto_cs:
            self.spi.assert_cs()
            try:
                return self.spi.transfer(byte)
            finally:
                self.spi.release_cs()
        if self.cs_setup and not self.spi.cs_asserted:
            self.spi.assert_cs()
        return self.spi.transfer(byte)


class _TransportWindow:
    def __init__(self, transport: BitBangTransport):
        self.transport = transport

    def __enter__(self):
        self.transport._in_window += 1
        if self.transport._in_window == 1 and self.transport.cs_setup:
            self.transport.spi.assert_cs()
        return self.transport

    def __exit__(self, *exc: Any) -> None:
        self.transport._in_window -= 1
        if self.transport._in_window == 0 and self.transport.cs_setup:
            self.transport.spi.release_cs()


# ---------------------------------------------------------------------------
# Test double: a chip that talks the LOOM byte protocol through pin levels
# ---------------------------------------------------------------------------
class FakeChipSPI:
    """Minimal chip-side SPI slave for loopback tests of the bit-bang layer.

    It reuses `ModelTransport`'s command FSM: on each SCK rising edge it shifts
    a MOSI bit in; on each falling edge it shifts the next MISO bit out.
    """

    def __init__(self, chip: ModelTransport | None = None):
        self.chip = chip if chip is not None else ModelTransport()
        self._rx_shift = 0
        self._rx_bits = 0
        self._tx_shift = self.chip.next_miso   # idle MISO byte (0x80 accepted)
        self._rx_byte = 0
        self.cs_n = 1
        self.sck = 0
        self.mosi = 0

    # PinIO-compatible chip-side hooks --------------------------------
    def set_cs(self, value: int) -> None:
        self.cs_n = int(value) & 1
        if self.cs_n:
            self._rx_bits = 0
            self._rx_shift = 0

    def set_sck(self, value: int) -> None:
        value = int(value) & 1
        if value == self.sck:
            return
        self.sck = value
        if not self.cs_n:
            if value == 1:                    # rising: sample MOSI
                self._rx_shift = ((self._rx_shift << 1) | (self.mosi & 1)) & 0xFF
                self._rx_bits += 1
            else:                             # falling: a full byte arrived
                if self._rx_bits == 8:
                    self._rx_bits = 0
                    self._rx_byte = self._rx_shift
                    self._rx_shift = 0
                    # Process the byte just received at the end of this transfer,
                    # then latch the MISO byte the chip prepared for the next one.
                    self.chip.xfer(self._rx_byte)
                    self._tx_shift = self.chip.next_miso

    def set_mosi(self, value: int) -> None:
        self.mosi = int(value) & 1

    def read_miso(self) -> int:
        if self.cs_n:
            return 0
        bit = (self._tx_shift >> 7) & 1
        self._tx_shift = ((self._tx_shift << 1) | 1) & 0xFF
        return bit

    def delay(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Self-test: bit-bang through fake pins into the model chip
# ---------------------------------------------------------------------------
def _selftest() -> int:
    from sw import loom_asm

    chip = FakeChipSPI()
    spi = SPIBitBang(chip)                     # the *chip* is the "pins" here
    transport = BitBangTransport(spi)
    host = ByteTransport(transport)

    errors: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
        if not cond:
            errors.append(name)

    check("ping over bit-banged pins", host.ping())
    prog = loom_asm.assemble("""
        movi r0, 0x99
        push r0
        halt
    """, filename="<spi-selftest>")
    host.write_mem(0, prog.words)
    check("read_mem over bit-banged pins", host.read_mem(0, len(prog.words)) == prog.words)
    host.set_pc(0, 0)
    host.run(1)
    chip.chip.loom.run(30)
    check("RX_POP over bit-banged pins", host.pop_tx(0) == 0x99)

    # CS held across a whole command window
    with transport.command_window():
        status = trans_status = transport.xfer(0x0D)       # PING cmd
        transport.xfer(0x5A)                               # arg
        reply = transport.xfer(0x00)                       # reply byte
    check("CS window framing", status == 0x80 and reply == 0xA5,
          f"status=0x{status:02X} reply=0x{reply:02X}")

    print("BITBANG_SELFTEST:", "PASS" if not errors else "FAIL")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

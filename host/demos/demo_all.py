#!/usr/bin/env python3
"""LOOM end-to-end demo — four protocol engines running at once on one chip.

Run from anywhere:

    python host/demos/demo_all.py                 # golden-model run, full output
    python host/demos/demo_all.py --cycle-limit 500000
    python host/demos/demo_all.py --quick         # skip the ASCII waveforms

What happens
------------
The four example firmware images are loaded into PMEM at fixed, relocatable base
addresses (one per thread), all four threads are started, and then the host
plays the other end of each wire:

    t0  uart_tx.asm    host -> UART wire   (115200 8N1)
    t1  uart_rx.asm    UART wire -> host   (115200 8N1)   [full duplex with t0]
    t2  spi_master.asm host -> SPI byte, MISO byte -> host (mode 0, ~1 MHz)
    t3  i2c_master.asm host job -> START/addr/reg/data/STOP (100 kHz, open drain)

The far ends of the wires are modelled by `sw/protocols.py` (UART device, SPI
target, I2C register-file target), so the transcript below is decoded from the
actual pin waveforms the firmware produced — not from the firmware's intent.

This is the same scenario `sw/examples/model_selftest.py` asserts on; the demo
adds the human-readable transcript and ASCII waveforms.

On real silicon the host talks to the chip through `host/loom_host.py`'s SPI
bridge instead (the same `app_send`/`app_xfer`/`app_write` helpers work there);
only the waveform capture is model-only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw import loom_asm, loom_model                                     # noqa: E402
from sw.protocols import (                                               # noqa: E402
    UartBus, SpiBus, I2cBus, WireWorld, format_wave,
)
from host.loom_host import ModelLink                                     # noqa: E402

#: one program per thread; all images are position independent
THREADS = [
    ("uart_tx", 0, 0x000),      # tid 0
    ("uart_rx", 1, 0x080),      # tid 1
    ("spi_master", 2, 0x100),   # tid 2
    ("i2c_master", 3, 0x180),   # tid 3
]
MANIFEST = ROOT / "sw" / "prog" / "manifest.json"
EXAMPLES = ROOT / "sw" / "examples"
CLOCK_HZ = 33_333_333          # shipped design point (fallback if no manifest)

BANNER = r"""
  +--------------------------------------------------------------------+
  |  L O O M  -  a programmable protocol loom                          |
  |  4 threads, one event scheduler, 19 protocol pins, 0 jitter         |
  |  this demo: UART + SPI + I2C emulated entirely by firmware          |
  +--------------------------------------------------------------------+
"""


def load_images(quick: bool) -> dict[str, loom_asm.AssemblyResult]:
    """Load the built images from sw/prog if fresh, else assemble the sources."""
    images: dict[str, loom_asm.AssemblyResult] = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text())
        names = {n for n, _t, _b in THREADS}
        ok = names <= set(manifest.get("programs", {}))
        if ok:
            for name in names:
                hexpath = ROOT / manifest["programs"][name]["image"]
                if not hexpath.exists():
                    ok = False
                    break
                words = [int(line, 16) for line in
                         hexpath.read_text().splitlines() if line.strip()]
                images[name] = loom_asm.AssemblyResult(
                    words=words, symbols={}, relocs=[], listing=[],
                    filename=str(hexpath))
        if ok:
            return images
    if not quick:
        print("[setup] sw/prog manifest not found - assembling sources directly")
    for name, _tid, _base in THREADS:
        images[name] = loom_asm.assemble_file(EXAMPLES / f"{name}.asm")
    return images


def wave_lines(title: str, waves: dict[str, str], chars: int) -> list[str]:
    out = [f"  {title}"]
    width = max(len(k) for k in waves) if waves else 0
    for label, wave in waves.items():
        out.append(f"    {label:>{width}}  |{wave}|")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LOOM three-protocol demo")
    ap.add_argument("--quick", action="store_true", help="no ASCII waveforms")
    ap.add_argument("--chars", type=int, default=96, help="waveform width")
    args = ap.parse_args(argv)

    print(BANNER)
    t0 = time.perf_counter()

    images = load_images(args.quick)
    loom = loom_model.Loom()
    for name, tid, base in THREADS:
        words = images[name].words
        loom.load(words, base)
        loom.set_pc(tid, base)
        print(f"[setup] t{tid}: {name:11s} loaded at PMEM 0x{base:03X} "
              f"({len(words):3d} words)")
    link = ModelLink(loom)
    world = WireWorld(link)

    uart = UartBus(world, tx_tid=0, rx_tid=1)
    spi = SpiBus(world, tid=2)
    i2c = I2cBus(world, address=0x50, tid=3,
                 registers=[0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80])
    print("[setup] far ends attached: UART device (P10/P11), SPI target "
          "(uio[0..3]), I2C target 0x50 (uio[4:5], register file)")

    loom.run_threads(0b1111)
    world.run(600)
    print(f"[run  ] all 4 threads enabled at cycle {world.cycle}")
    print()

    # ------------------------------------------------------------------ UART
    print("[UART ] full duplex, 115200 8N1, BAUD_DIV=0x208D (520.8125 cyc/bit)")
    uart_tx0 = world.cycle
    uart.app_send(b"Hello")
    uart_tx1 = world.cycle
    tx_frames = list(uart.frames)
    tx_bytes = uart.wire_recv()
    print(f"  host -> t0 RX-FIFO -> UART TX pin: decoded {tx_bytes!r}")
    print("  frames (start, 8 data LSB-first, stop):")
    for f in tx_frames:
        print(f"    0x{f.byte:02X}  {f.cell_text()}")
    uart_rx0 = world.cycle
    uart.wire_send(b"LOOM")
    world.run((4 * 12 + 8) * round(CLOCK_HZ / 115200) + 2000)
    uart_rx1 = world.cycle
    rx_bytes = uart.app_recv()
    print(f"  UART RX pin -> t1 -> host TX-FIFO: decoded {rx_bytes!r}")
    if not args.quick:
        print("  TX waveform (host -> chip -> wire):")
        print("\n".join(wave_lines("", uart.waveform(args.chars, start=uart_tx0 - 8,
                                                      end=uart_tx1 + 8), args.chars)[1:]))
        print("  RX waveform (wire -> chip):")
        print("\n".join(wave_lines("", uart.waveform(args.chars, start=uart_rx0 - 8,
                                                      end=uart_rx1 + 8), args.chars)[1:]))
    print()

    # ------------------------------------------------------------------- SPI
    print("[SPI  ] mode 0, full duplex, SCK ~1.03 MHz (QUARTER_CYCLES=4)")
    mosi = bytes([0x12, 0x34])
    spi0 = world.cycle
    miso = spi.app_xfer(mosi)
    spi1 = world.cycle
    print(f"  MOSI {list(mosi)} -> t2 -> MISO {[hex(b) for b in miso]}  "
          f"(far-end fixture: byte+1, one-frame pipeline -> first byte 0xFF)")
    for line in spi.transcript():
        print(f"    {line}")
    if not args.quick:
        print("\n".join(wave_lines("SPI wires:", spi.waveform(
            args.chars, start=spi0 - 8, end=spi1 + 8), args.chars)))
    print()

    # ------------------------------------------------------------------- I2C
    print("[I2C  ] 100 kHz open-drain, clock stretching tolerant, target 0x50")
    i2c0 = world.cycle
    i2c.app_write(0x50, 4, 0x9C)
    value = i2c.app_read(0x50, 4)
    i2c1 = world.cycle
    print(f"  write reg4=0x9C -> target register file: "
          f"{[hex(v) for v in i2c.regs]}")
    print(f"  read  reg4     -> {value!r}" if value is not None
          else "  read  reg4     -> None")
    for line in i2c.transcript():
        print(f"    {line}")
    if not args.quick:
        print("\n".join(wave_lines("I2C wires:", i2c.waveform(
            args.chars, start=i2c0 - 8, end=i2c1 + 8), args.chars)))
    print()

    # --------------------------------------------------------------- summary
    ok = (tx_bytes == b"Hello" and rx_bytes == b"LOOM"
          and miso == [0xFF, 0x13] and i2c.regs[4] == 0x9C and value == 0x9C)
    wall = time.perf_counter() - t0
    sim_us = world.cycle / CLOCK_HZ * 1e6
    print(f"[done ] {world.cycle} cycles = {sim_us:.1f} us of chip time, "
          f"{wall:.2f} s of model time")
    print("DEMO_ALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

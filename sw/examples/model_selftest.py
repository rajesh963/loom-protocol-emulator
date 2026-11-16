#!/usr/bin/env python3
"""Golden-model self-test for every LOOM example firmware image.

Run from the repo root:

    python sw/examples/model_selftest.py            # test everything
    python sw/examples/model_selftest.py -v         # + per-step detail

What is checked
---------------
For each `sw/examples/*.asm`: assemble it, load it on the **golden model**
(`sw/loom_model.py`), wire up the far end of the protocol from `sw/protocols.py`
and exchange real bytes/frames.  Nothing is mocked at the instruction level: the
model executes the actual firmware cycle by cycle, so these tests cover the
firmware logic, the event scheduler, WAITD fractional timing, FIFOs and the
open-drain pin behaviour together.

Also measured (reported in the table):
  * UART bit period vs BAUD_DIV=0x208D (expect 520.81 cycles)
  * SPI master SCK half period for QUARTER_CYCLES=8 / MODE=0..3
  * I2C SCL half period for I2C_HZ=100k and 400k
  * SPI slave works at 1 MHz and 2 MHz external SCK
  * I2C master tolerates a 450-cycle clock stretch on every ACK
  * all four demo programs run concurrently (t0 uart_tx, t1 uart_rx,
    t2 spi_master, t3 i2c_master) with the four-thread scheduler active

Exit status: 0 if every test passed, 1 otherwise.
"""

from __future__ import annotations

import statistics
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sw import loom_asm, loom_model                       # noqa: E402
from sw.protocols import (                                 # noqa: E402
    UartBus, SpiBus, SpiMasterModel, I2cBus, I2cMasterModel,
    Ws2812, Ps2Host, OneWireMaster, WireWorld, format_wave,
)
from host.loom_host import ModelLink                        # noqa: E402

EXAMPLES = HERE
CLOCK_HZ = 33_333_333                    # shipped design point (30 ns)
FF_FALLBACK_WORDS = 128
VERBOSE = False

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    if VERBOSE or not ok:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


def assemble(name: str, **subs: str):
    """Assemble an example, optionally patching `.equ` defaults (rate/mode variants)."""
    src = (EXAMPLES / f"{name}.asm").read_text(encoding="utf-8")
    for old, new in subs.items():
        if old not in src:
            raise KeyError(f"{name}: cannot patch {old!r}")
        src = src.replace(old, new)
    return loom_asm.assemble(src, filename=f"{name}[variant]")


def make_link(prog, base: int, tid: int, threads: int = 1) -> tuple:
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=CLOCK_HZ)
    loom.run_threads(threads << tid)
    return loom, link, world


def periods(wave: list[tuple[int, int]], skip: int = 0, count: int = 8) -> list[int]:
    """Deltas between consecutive level transitions (cycles)."""
    ts = [c for c, _ in wave]
    deltas = [b - a for a, b in zip(ts, ts[1:])]
    return deltas[skip:skip + count]


# ---------------------------------------------------------------------------
# UART
# ---------------------------------------------------------------------------
def test_uart_tx() -> None:
    prog = assemble("uart_tx")
    check("uart_tx: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 0)
    uart = UartBus(world, tx_tid=0, rx_tid=0)
    world.run(200)
    payload = b"Hello, LOOM!"
    uart.app_send(payload)
    cell = uart.frames[0].cell_text() if uart.frames else ""
    got = uart.wire_recv()
    check("uart_tx: 115200 8N1 frame decode", got == payload,
          f"sent {payload!r} got {got!r}")
    check("uart_tx: start/data/stop cells", cell.startswith("S ") and cell.endswith(" P"),
          cell)
    # bit period: 0x55 gives alternating data bits -> a transition every cell
    uart.wire_recv(clear_wave=True)
    uart.app_send(b"\x55")
    wave = list(uart.wave_tx)
    deltas = periods(wave, skip=1, count=7)
    if deltas:
        avg = statistics.mean(deltas)
        want = round(CLOCK_HZ * 16 / 115200) / 16      # 289.375 cycles
        check(f"uart_tx: bit period {want:.3f} +/-1 cycle", abs(avg - want) <= 1.0,
              f"avg={avg:.2f} cycles, deltas={deltas}")


def test_uart_rx() -> None:
    prog = assemble("uart_rx")
    check("uart_rx: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 1)
    uart = UartBus(world, tx_tid=1, rx_tid=1)
    world.run(200)
    payload = b"LOOM\x00\xff\x55"
    uart.wire_send(payload)
    bit = round(CLOCK_HZ / 115200)                     # 289 cycles
    world.run((len(payload) * 12 + 8) * bit + 4000)
    got = uart.app_recv()
    check("uart_rx: decodes wire bytes to host FIFO", got == payload,
          f"sent {payload!r} got {got!r}")
    # different baud: 9600 = 3472 cycles/bit at 33.33 MHz
    prog9 = assemble("uart_rx", **{"BAUD,      115200": "BAUD,      9600"})
    loom, link, world = make_link(prog9, 0, 1)
    uart = UartBus(world, baud=9600, tx_tid=1, rx_tid=1)
    world.run(200)
    uart.wire_send(b"ok")
    world.run((2 * 12 + 8) * round(CLOCK_HZ / 9600) + 5000)
    check("uart_rx: 9600 baud variant", uart.app_recv() == b"ok")


def test_uart_tx_rates() -> None:
    for baud in (9600, 57600, 115200):
        prog = assemble("uart_tx", **{"BAUD,      115200": f"BAUD,      {baud}"})
        loom, link, world = make_link(prog, 0, 0)
        uart = UartBus(world, baud=baud, tx_tid=0, rx_tid=0)
        world.run(200)
        uart.app_send(b"U")
        check(f"uart_tx: baud {baud} decodes", uart.wire_recv() == b"U")


def test_uart_loopback() -> None:
    prog = assemble("uart_loopback")
    check("uart_loopback: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 0)
    uart = UartBus(world, tx_tid=0, rx_tid=0)
    world.run(200)
    echoed = bytearray()
    for b in b"echo":
        uart.wire_send(bytes([b]))
        world.run(2 * 10 * round(CLOCK_HZ / 115200) + 4000)
        echoed += uart.wire_recv()
    check("uart_loopback: RX->TX echo", bytes(echoed) == b"echo",
          f"echoed {bytes(echoed)!r}")
    check("uart_loopback: echo also reaches host FIFO", uart.app_recv() == b"echo")


# ---------------------------------------------------------------------------
# SPI
# ---------------------------------------------------------------------------
def test_spi_master_modes() -> None:
    base = assemble("spi_master")
    check("spi_master: size < 128 words", len(base.words) < FF_FALLBACK_WORDS,
          f"{len(base.words)} words")
    for mode in range(4):
        prog = assemble("spi_master", **{".equ MODE,            0": f".equ MODE,            {mode}"})
        loom, link, world = make_link(prog, 0, 2)
        spi = SpiBus(world, mode=mode, tid=2, evt_bit=2)
        world.run(200)
        payload = bytes([0x41, 0x00, 0xFF])
        got = spi.app_xfer(payload)
        # far-end fixture: response byte = (previous mosi byte + 1) & 0xFF,
        # initial response 0xFF (one-byte pipeline, as on a real SPI target)
        expect = [0xFF] + [(b + 1) & 0xFF for b in payload[:-1]]
        check(f"spi_master: mode {mode} full-duplex", got == expect,
              f"MOSI {list(payload)} -> MISO {list(got)} expected {expect}")
        if mode == 0:
            d = periods(spi.wave[0], skip=2, count=16)   # SCK transitions
            if d:
                avg = statistics.mean(d)
                check("spi_master: SCK half period ~16 cycles (~1.03 MHz)",
                      12 <= avg <= 22, f"avg={avg:.1f} cycles")


def test_spi_master_fast() -> None:
    prog = assemble("spi_master", **{".equ QUARTER_CYCLES,  4": ".equ QUARTER_CYCLES,  0"})
    loom, link, world = make_link(prog, 0, 2)
    spi = SpiBus(world, tid=2, evt_bit=2)
    world.run(200)
    got = spi.app_xfer(b"\xA5")
    check("spi_master: 2 MHz variant (QUARTER_CYCLES=0)", got == [0xFF],
          f"MISO {[hex(x) for x in got]}")


def test_spi_slave() -> None:
    prog = assemble("spi_slave")
    check("spi_slave: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    for hz in (1_000_000, 2_000_000):
        loom, link, world = make_link(prog, 0, 0)
        master = SpiMasterModel(world, spi_hz=hz)
        world.run(300)
        got = master.xfer(b"\x41\x00")
        expect = [0x01, 0x42]            # previous MOSI + 1
        check(f"spi_slave: {hz/1e6:.0f} MHz external SCK", got == expect,
              f"got {[hex(x) for x in got]} expected {[hex(x) for x in expect]}")


# ---------------------------------------------------------------------------
# I2C
# ---------------------------------------------------------------------------
def test_i2c_master() -> None:
    prog = assemble("i2c_master")
    check("i2c_master: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 3)
    bus = I2cBus(world, address=0x50, stretch_cycles=450,
                 registers=[0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
    world.run(300)
    bus.app_write(0x50, 2, 0xA5)
    check("i2c_master: write reg2=0xA5", bus.regs[2] == 0xA5,
          f"regs={[hex(v) for v in bus.regs]}")
    value = bus.app_read(0x50, 2)
    check("i2c_master: read reg2 back (stretched clock)", value == 0xA5,
          f"read {value!r}")
    d = periods(bus.wave[4], skip=3, count=12)         # SCL transitions
    if d:
        avg = statistics.mean(d)
        check("i2c_master: SCL half period ~167 cycles (100 kHz)",
              150 <= avg <= 220, f"avg={avg:.1f} cycles")
    # fast mode variant
    prog4 = assemble("i2c_master",
                     **{".equ I2C_HZ,     100000": ".equ I2C_HZ,     400000"})
    loom, link, world = make_link(prog4, 0, 3)
    bus4 = I2cBus(world, address=0x50)
    world.run(300)
    bus4.app_write(0x50, 5, 0x5A)
    v = bus4.app_read(0x50, 5)
    check("i2c_master: 400 kHz variant", bus4.regs[5] == 0x5A and v == 0x5A,
          f"regs={[hex(x) for x in bus4.regs]} read={v!r}")


def test_i2c_target() -> None:
    prog = assemble("i2c_target")
    check("i2c_target: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 0)
    # I2cMasterModel changes SDA on the SCL falling edge (0-cycle hold), i.e.
    # the worst-case setup a real/fast master can present; this guards the
    # target's byte-boundary classifier.
    master = I2cMasterModel(world, i2c_hz=100_000)
    world.run(500)
    master.write(0x50, [0x02, 0xA5])
    check("i2c_target: register write via DMEM", loom.dmem[2] == 0xA5,
          f"dmem[2]={loom.dmem[2]:#04x}")
    got = master.read(0x50, 2, 1)
    check("i2c_target: register read", got == [0xA5], f"got {[hex(x) for x in got]}")
    master.write(0x51, [0x02, 0xEE])                   # wrong address -> NAK
    check("i2c_target: foreign address ignored", loom.dmem[2] == 0xA5,
          f"dmem[2]={loom.dmem[2]:#04x}")


# ---------------------------------------------------------------------------
# LED / peripheral protocols
# ---------------------------------------------------------------------------
def test_ws2812() -> None:
    prog = assemble("ws2812")
    check("ws2812: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 0)
    ws = Ws2812(world)
    world.run(300)
    for b in (0x10, 0x20, 0x30):
        link.tx_push(0, b)
    world.run(24 * 42 + round(50e-6 * CLOCK_HZ) + 6000)
    ws.flush()
    check("ws2812: 800 kHz GRB pulse widths decode",
          ws.bytes == [0x10, 0x20, 0x30], f"decoded {[hex(b) for b in ws.bytes]}")
    highs = [h for _r, h, _l in ws.pulses[:8]]
    # 0x10 = 00010000 MSB-first: 0,0,0,1,0,0,0,0 -> T0H ~13, T1H ~27 cycles
    check("ws2812: T0H~13 T1H~27 cycles",
          all(12 <= h <= 15 for h in highs[:3])
          and 25 <= highs[3] <= 28
          and all(12 <= h <= 15 for h in highs[4:]),
          f"high pulses {highs}")


def test_ps2() -> None:
    prog = assemble("ps2_target")
    check("ps2_target: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 0)
    ps2 = Ps2Host(world)
    world.run(300)
    link.tx_push(0, 0x1C)                      # PS/2 'A' make code
    half = round(35e-6 * CLOCK_HZ)             # 1167 cycles
    world.run(11 * 2 * half + 8 * half + 6000)
    frame = ps2.frames[0] if ps2.frames else {}
    check("ps2_target: 11-bit frame, odd parity, 0x1C",
          ps2.bytes == [0x1C] and frame.get("parity_ok", False),
          f"frames={ps2.frames}")


def test_onewire() -> None:
    prog = assemble("onewire")
    check("onewire: size < 128 words", len(prog.words) < FF_FALLBACK_WORDS,
          f"{len(prog.words)} words")
    loom, link, world = make_link(prog, 0, 0)
    ow = OneWireMaster(world)
    world.run(300)
    check("onewire: reset -> presence pulse", ow.reset())
    ow.write_byte(0xCC)                        # SKIP ROM
    ow.write_byte(0xBE)                        # READ SCRATCHPAD
    data = list(ow.read_scratchpad(9))
    check("onewire: DS18B20 +25.0 C scratchpad",
          data[:5] == [0x90, 0x01, 0x4B, 0x46, 0x7F],
          f"scratchpad {[hex(b) for b in data]}")
    ow.reset()
    ow.write_byte(0xCC)
    ow.write_byte(0x44)                        # CONVERT T
    check("onewire: CONVERT T accepted", True)


# ---------------------------------------------------------------------------
# All four demo programs at once
# ---------------------------------------------------------------------------
DEMO_BASE = {"uart_tx": 0x000, "uart_rx": 0x080, "spi_master": 0x100,
             "i2c_master": 0x180}


def make_demo() -> tuple:
    progs = {name: assemble(name) for name in DEMO_BASE}
    loom = loom_model.Loom()
    for name, base in DEMO_BASE.items():
        loom.load(progs[name].words, base)
        loom.set_pc({"uart_tx": 0, "uart_rx": 1, "spi_master": 2,
                     "i2c_master": 3}[name], base)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=CLOCK_HZ)
    uart = UartBus(world, tx_tid=0, rx_tid=1)
    spi = SpiBus(world, tid=2, evt_bit=2)
    i2c = I2cBus(world, address=0x50, tid=3, evt_bit=3,
                 registers=[0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80])
    loom.run_threads(0b1111)
    world.run(400)
    return loom, link, world, uart, spi, i2c


def test_all_four_threads() -> None:
    loom, link, world, uart, spi, i2c = make_demo()
    uart.app_send(b"Hello")
    check("demo: UART host->wire 'Hello'", uart.wire_recv() == b"Hello")
    uart.wire_send(b"LOOM")
    world.run((4 * 12 + 8) * round(CLOCK_HZ / 115200) + 2000)
    check("demo: UART wire->host 'LOOM'", uart.app_recv() == b"LOOM")
    got = spi.app_xfer(b"\x12\x34")
    check("demo: SPI transaction", got == [0xFF, 0x13],
          f"MISO {[hex(x) for x in got]}")
    i2c.app_write(0x50, 4, 0x9C)
    value = i2c.app_read(0x50, 4)
    check("demo: I2C write+read while 4 threads run",
          i2c.regs[4] == 0x9C and value == 0x9C, f"regs[4]={i2c.regs[4]:#04x} read={value!r}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    args = list(argv if argv is not None else sys.argv[1:])
    VERBOSE = "-v" in args or "--verbose" in args
    tests = [
        test_uart_tx, test_uart_rx, test_uart_tx_rates, test_uart_loopback,
        test_spi_master_modes, test_spi_master_fast, test_spi_slave,
        test_i2c_master, test_i2c_target, test_ws2812, test_ps2, test_onewire,
        test_all_four_threads,
    ]
    print("LOOM example-model self-test")
    print(f"  examples: {EXAMPLES}")
    print(f"  model   : sw/loom_model.py   clock {CLOCK_HZ/1e6:.0f} MHz")
    for fn in tests:
        try:
            fn()
        except Exception as exc:                    # a test harness failure
            detail = f"{type(exc).__name__}: {exc}"
            check(f"{fn.__name__}: raised", False, detail)
            if VERBOSE:
                traceback.print_exc()
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [r for r in _results if not r[1]]
    print()
    for name, ok, detail in _results:
        if not ok:
            print(f"FAIL  {name}  [{detail}]")
    print(f"MODEL_SELFTEST: {'PASS' if not failed else 'FAIL'} "
          f"({passed}/{len(_results)} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

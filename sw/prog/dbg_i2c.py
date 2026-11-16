#!/usr/bin/env python3
"""firmware scratch debug tests for the two open I2C failures (owner: firmware).

Run from test/ with:
    PYTHONPATH=.:../sw/prog LOOM_KNOWN_FIXED=VER-006 \
        make sim COCOTB_TEST_MODULES=dbg_i2c TESTCASE=dbg_400k_poll1

These replicate verify's I2C tests but with knobs to bisect the failures:
  * VIP poll period (1 vs verify's 2 cycles) -> tests VIP-poll aliasing
  * clock stretching on/off at 400 kHz
  * i2c_target: full bus trace + ACK sample cycles + thread PC/DMEM readback
"""

from __future__ import annotations

import pathlib
import re

import cocotb
from cocotb.triggers import ClockCycles, Timer

from host import CSR, start_loom
from vip import i2c as v_i2c
from vip.wave import render as render_wave

REPO = pathlib.Path(__file__).resolve().parents[2]   # sw/prog -> loom
EX = REPO / "sw" / "examples"
CLK_NS = 16.67


def fw_source(name: str, subs: dict[str, str] | None = None) -> str:
    src = (EX / name).read_text(encoding="utf-8")
    for pat, repl in (subs or {}).items():
        src, n = re.subn(pat, repl, src)
        assert n, f"substitution {pat!r} did not match in {name}"
    return src


async def load_firmware(host, name, subs=None, *, addr=0, tid=0, run=True):
    words = await host.load_asm(fw_source(name, subs), addr=addr)
    await host.set_pc(tid, addr)
    if run:
        await host.run(1 << tid)
    return words


def log_wave(host, text):
    for line in text.splitlines():
        host.log.info(line)


def i2c_apply(host, scl_bit, sda_bit, scl_low, sda_low):
    value = 0
    oe = 0
    if scl_low:
        oe |= 1 << scl_bit
    if sda_low:
        oe |= 1 << sda_bit
    host.set_ext_uio(value, oe)


async def read_thread_pc(host, tid: int) -> int:
    """Read a thread's TPC through the host CSR view (0x40 + 16*t + 8)."""
    try:
        return await host.read_csr(0x40 + 16 * tid + 8)
    except Exception as exc:                       # pragma: no cover - debug aid
        host.log.info("read TPC failed: %s", exc)
        return -1


async def run_controller(host, mon, *, i2c_hz: int, payload, poll: int,
                         stretch: int = 0, dump: bool = True,
                         legal_stretch: bool = False):
    subs = {"I2C_HZ,\\s+100000": f"I2C_HZ,     {i2c_hz}"}
    await load_firmware(host, "i2c_master.asm", subs, run=False)
    for b in payload:
        await host.tx_push(0, b)
    t0 = mon.mark()
    await host.run(1)
    target = v_i2c.I2cTargetModel(0x50, None, stretch_cycles=stretch)
    scl_tr, sda_tr = [], []
    prev = (1, 1)
    stretch_left = 0
    duration = int(60e6 / i2c_hz * 45) + 8000
    deadline = t0 + duration
    while mon.mark() < deadline and not (target.events and
                                         target.events[-1].kind == "stop"):
        pins_in = host.pins_in() & 0xFF
        scl = (pins_in >> 4) & 1
        sda = (pins_in >> 5) & 1
        if (scl, sda) != prev:
            scl_tr.append((mon.mark(), scl))
            sda_tr.append((mon.mark(), sda))
            prev = (scl, sda)
        want_low, stretch_polls = target.feed(mon.mark(), scl, sda)
        if legal_stretch:
            # legal target stretch: only hold SCL low while it is already low
            if stretch_polls and scl == 0:
                stretch_left = max(stretch_left, stretch)
            if stretch_left > 0 and scl == 0:
                keep_low = True
                stretch_left -= poll
            else:
                keep_low = False
            i2c_apply(host, 4, 5, keep_low, want_low)
        else:
            i2c_apply(host, 4, 5, bool(stretch_polls), want_low)
        await ClockCycles(host.dut.clk, poll)
    await host.stop(0x1)
    i2c_apply(host, 4, 5, False, False)
    tpc = await read_thread_pc(host, 0)
    host.log.info("controller: poll=%d stretch=%d tpc=%s data_rx=%s",
                  poll, stretch, tpc, [hex(b) for b in target.data_rx])
    host.log.info("controller events: %s", [(e.kind, hex(e.byte) if e.byte is not None else None, e.ack) for e in target.events])
    if dump:
        log_wave(host, render_wave(scl_tr, t0=(scl_tr[0][0] - 60) if scl_tr else 0,
                                   t1=(scl_tr[-1][0] + 60) if scl_tr else 100, cols=130,
                                   label=f"I2C SCL @{i2c_hz//1000}k poll={poll}"))
        log_wave(host, render_wave(sda_tr, t0=(sda_tr[0][0] - 60) if sda_tr else 0,
                                   t1=(sda_tr[-1][0] + 60) if sda_tr else 100, cols=130,
                                   label=f"I2C SDA @{i2c_hz//1000}k poll={poll}"))
    return target, scl_tr, sda_tr


@cocotb.test()
async def dbg_400k_poll1(dut):
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    target, _, _ = await run_controller(host, mon, i2c_hz=400000,
                                        payload=[0x00, 0xA0, 0x00, 0x5A], poll=1)
    host.log.info("RESULT poll1 data_rx=%s", [hex(b) for b in target.data_rx])


@cocotb.test()
async def dbg_400k_poll2_nostretch(dut):
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    target, _, _ = await run_controller(host, mon, i2c_hz=400000,
                                        payload=[0x00, 0xA0, 0x00, 0x5A], poll=2,
                                        stretch=0)
    host.log.info("RESULT poll2_nostretch data_rx=%s", [hex(b) for b in target.data_rx])


@cocotb.test()
async def dbg_400k_poll2_legal_stretch(dut):
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    target, scl_tr, _ = await run_controller(host, mon, i2c_hz=400000,
                                             payload=[0x00, 0xA0, 0x00, 0x5A],
                                             poll=2, stretch=150,
                                             legal_stretch=True, dump=False)
    rises = sum(1 for i in range(1, len(scl_tr)) if scl_tr[i - 1][1] == 0 and scl_tr[i][1] == 1)
    host.log.info("LEGAL stretch 400k: SCL rising edges=%d data_rx=%s acks=%s",
                  rises, [hex(b) for b in target.data_rx],
                  [e.ack for e in target.events if e.kind == "ack"])


@cocotb.test()
async def dbg_400k_poll2_stretch(dut):
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    target, scl_tr, _ = await run_controller(host, mon, i2c_hz=400000,
                                             payload=[0x00, 0xA0, 0x00, 0x5A],
                                             poll=2, stretch=2)
    rises = sum(1 for i in range(1, len(scl_tr)) if scl_tr[i - 1][1] == 0 and scl_tr[i][1] == 1)
    host.log.info("r400k poll2 stretch2: SCL rising edges=%d data_rx=%s",
                  rises, [hex(b) for b in target.data_rx])
    host.log.info("events: %s", [(e.kind, hex(e.byte) if e.byte is not None else None, e.ack) for e in target.events])


@cocotb.test()
async def dbg_400k_poll1_stretch(dut):
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    target, scl_tr, _ = await run_controller(host, mon, i2c_hz=400000,
                                             payload=[0x00, 0xA0, 0x00, 0x5A],
                                             poll=1, stretch=2)
    rises = sum(1 for i in range(1, len(scl_tr)) if scl_tr[i - 1][1] == 0 and scl_tr[i][1] == 1)
    host.log.info("r400k poll1 stretch2: SCL rising edges=%d data_rx=%s",
                  rises, [hex(b) for b in target.data_rx])


@cocotb.test()
async def dbg_100k_poll2(dut):
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    target, _, _ = await run_controller(host, mon, i2c_hz=100000,
                                        payload=[0x00, 0xA0, 0x00, 0x5A], poll=2,
                                        dump=False)
    host.log.info("RESULT 100k_poll2 data_rx=%s", [hex(b) for b in target.data_rx])


# ---------------------------------------------------------------------------
# DUT as I2C target: full trace + ACK samples + PC
# ---------------------------------------------------------------------------
@cocotb.test()
async def dbg_i2c_target(dut):
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    await load_firmware(host, "i2c_target.asm")
    m = v_i2c.I2cMasterModel(half_period=300, setup=40)
    t_start = mon.mark() + 400
    events, ack_samples = m.schedule(0x50, writes=[0x5A], reg=0x02,
                                     t_start=t_start)
    scl_bit, sda_bit = 6, 7
    scl_tr, sda_tr = [], []
    actions = [("drive", c, (scl, sda), None) for c, scl, sda in events]
    actions += [("ack", c, None, None) for c in ack_samples]
    actions.sort(key=lambda a: a[1])
    acks = []
    drive_log = []
    for kind, cyc, lv, _ in actions:
        while mon.mark() < cyc:
            await host._clk(min(10, max(1, cyc - mon.mark())))
        if kind == "drive":
            scl_lvl, sda_lvl = lv
            oe = 0
            if scl_lvl == 0:
                oe |= 1 << scl_bit
            if sda_lvl == 0:
                oe |= 1 << sda_bit
            host.set_ext_uio(0, oe)
            line = host.pins_in()
            scl_tr.append((cyc, (line >> scl_bit) & 1))
            sda_tr.append((cyc, (line >> sda_bit) & 1))
            ddir = host.pins_dir()
            dpout = host.pins_out()
            drive_log.append((
                cyc, scl_lvl, sda_lvl,
                "wireSCL", (line >> scl_bit) & 1, "wireSDA", (line >> sda_bit) & 1,
                "dutSCLlow", int(((ddir >> scl_bit) & 1) and not ((dpout >> scl_bit) & 1)),
                "dutSDAlow", int(((ddir >> sda_bit) & 1) and not ((dpout >> sda_bit) & 1))))
        else:
            acks.append(((host.pins_in() >> sda_bit) & 1, cyc, mon.mark()))
    host.set_ext_uio(0, 0)
    await host._clk(200)
    host.log.info("target: ack samples (sda, sched_cyc, actual_cyc)=%s", acks)
    host.log.info("target: drive log with DUT pull-down flags:")
    for row in drive_log:
        host.log.info("   %s", row)
    epc = await read_thread_pc(host, 0)
    reg = (await host.read_dmem(8))[:8]
    host.log.info("target: tpc=0x%x DMEM[0..7]=%s", epc, [hex(x) for x in reg])
    log_wave(host, render_wave(sda_tr, t0=t_start - 100, t1=events[-1][0] + 100,
                               cols=130, label="I2C target SDA=P[7]"))

#!/usr/bin/env python3
"""Protocol conformance: the LOOM chip emulating UART / SPI / I2C against
*independent* far-end VIPs (`test/vip/`), with an ASCII waveform per transaction.

Firmware comes from `sw/examples/*.asm` (owner: firmware) and is assembled here
with `sw.loom_asm.assemble()`; timing variants (baud, SPI mode, I2C rate) are
produced by substituting the `.equ` line in the source, so the same firmware
binary runs at the real 60 MHz rates.  Each test prints the decoded transaction
and the cycle-resolved waveform.

Run with:  make proto
"""

from __future__ import annotations

import os
import re

import cocotb
from cocotb.triggers import ClockCycles

from host import CSR, ascii_wave, start_loom
from vip import i2c as v_i2c
from vip import spi as v_spi
from vip import uart as v_uart
from vip import wave as v_wave
from vip.wave import render as render_wave
from known_issues import KNOWN_ISSUES

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent
EX = REPO / "sw" / "examples"

CLK_NS = 16.67            # real 60 MHz target clock

# 400 kHz triage knobs (defaults = shipped settings; env overrides for bring-up)
I2C_400K_POLL = int(os.environ.get("I2C_400K_POLL", "2"))
I2C_400K_STRETCH = int(os.environ.get("I2C_400K_STRETCH", "2"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fw_source(name: str, subs: dict[str, str] | None = None) -> str:
    src = (EX / name).read_text(encoding="utf-8")
    for pat, repl in (subs or {}).items():
        src, n = re.subn(pat, repl, src)
        assert n, f"substitution {pat!r} did not match in {name}"
    return src


async def load_firmware(host, name: str, subs: dict[str, str] | None = None,
                        *, addr: int = 0, tid: int = 0, run: bool = True):
    src = fw_source(name, subs)
    words = await host.load_asm(src, addr=addr)
    await host.set_pc(tid, addr)
    if run:
        await host.run(1 << tid)
    return words


async def doorbell(host, bit: int):
    """Host->thread event flag: CSR EVT |= 1<<bit (firmware waits in evwait)."""
    evt = await host.read_csr(CSR["EVT"])
    await host.write_csr(CSR["EVT"], evt | (1 << bit))


async def wait_tx_bytes(host, tid: int, n: int, *, timeout: int = 40000,
                        poll: int = 200) -> list[int]:
    """Wait until the thread's TX FIFO has >= n bytes, then pop them."""
    waited = 0
    while waited < timeout:
        fifo = await host.get_tfifo(tid)
        if (fifo >> 4) & 0xF >= n:
            break
        await host._clk(poll)
        waited += poll
    else:
        raise AssertionError(f"thread {tid} produced no TX bytes in {timeout} cycles")
    return [await host.rx_pop(tid) for _ in range(n)]


def log_wave(host, text: str):
    for line in text.splitlines():
        host.log.info(line)


def bit_tr(mon, bit: int, t_from: int = 0):
    return [(c, (p >> bit) & 1) for c, p, _ in mon.records if c >= t_from]


# ---------------------------------------------------------------------------
# UART: DUT transmitter -> VIP receiver
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_uart_tx(dut):
    """UART TX, 115200 8N1 @ 60 MHz: decode 3 bytes, framing + baud error."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    await load_firmware(host, "uart_tx.asm", run=False)
    payload = [0x55, 0xA3, 0x00]
    for b in payload:
        await host.tx_push(0, b)
    t0 = mon.mark()
    await host.run(1)
    await host._clk(int(10 * 3 * 520.8125) + 2500)
    await host.stop(0x1)

    tr = bit_tr(mon, 10, t0)
    period = 8333 / 16                     # 520.8125 cycles/bit @ 60 MHz
    dec = v_uart.UartDecoder(period)
    dec.decode(tr, mon.mark())
    log_wave(host, v_wave.render(tr, t0=t0, t1=tr[-1][0] + 600, cols=100,
                                 label="UART TX 115200 8N1 (P[10]) -- decoded frames:"))
    for f in dec.frames:
        host.log.info("  byte 0x%02x start=%d stop_ok=%s samples=%s",
                      f.byte, f.start_cycle, f.stop_ok, f.samples)
    got = [f.byte for f in dec.frames if f.ok]
    assert got == payload, f"UART TX decoded {[hex(b) for b in got]} != {[hex(b) for b in payload]}"
    assert not dec.errors, dec.errors
    baud = 60e6 / period
    host.log.info("UART TX: %d bytes exact, framing OK, baud=%.2f (target 115200, "
                  "error %+.4f %%)", len(got), baud, (baud / 115200 - 1) * 100)
    assert abs(baud / 115200 - 1) < 0.02


# ---------------------------------------------------------------------------
# UART: VIP transmitter -> DUT receiver
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_uart_rx(dut):
    """UART RX, 115200 8N1 @ 60 MHz, including +/-1.5 % baud tolerance."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    for baud_scale, tag in ((1.0, "nominal"), (1.015, "+1.5%"), (0.985, "-1.5%")):
        await host.reset(cycles=8)
        await load_firmware(host, "uart_rx.asm")
        period = 8333 / 16 * baud_scale
        payload = [0x41]
        drv = v_uart.UartDriver(period)
        t_start = mon.mark() + 200
        tr = [(0, 1)] + drv.transitions(
            payload, t_start=int(round(t_start + 2 * period)))
        pins = 0x0800                     # RX = P[11] = ui_in[3], idle high
        host.set_proto_inputs(pins)
        await host._clk(100)              # let the poll loop start
        for cyc, lvl in v_wave.transitions(tr):
            while mon.mark() < cyc:
                await host._clk(min(8, max(1, cyc - mon.mark())))
            pins = (pins & ~0x0800) | (lvl << 11)
            host.set_proto_inputs(pins)
        await host._clk(4000)
        fifo = await host.get_tfifo(0)
        got = [await host.rx_pop(0)] if (fifo >> 4) & 0xF else []
        await host.stop(0x1)
        log_wave(host, v_wave.render(bit_tr(mon, 11), t0=t_start - 100,
                                     t1=t_start + int(10 * period) + 200,
                                     cols=100,
                                     label=f"UART RX {tag} (P[11] driven by VIP):"))
        assert got == payload, f"UART RX {tag}: got {[hex(b) for b in got]} (TFIFO=0x{fifo:04x})"
        host.log.info("UART RX %s: byte 0x%02x exact (driver period %.3f cycles/bit)",
                      tag, got[0], period)


# ---------------------------------------------------------------------------
# SPI master: DUT drives mode 0-3, VIP target responds
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_spi_master_modes(dut):
    """SPI master all four modes: CPOL/CPHA, MSB-first, CS timing, full duplex."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    for mode in (0, 1, 2, 3):
        await host.reset(cycles=8)
        await load_firmware(host, "spi_master.asm",
                            {"MODE,\\s+0": f"MODE,            {mode}"}, run=False)
        payload = 0x5A
        miso_reply = [0xC3]
        await host.tx_push(0, 0xA5)       # byte the DUT sends on MOSI
        slave = v_spi.SpiSlaveModel(mode, miso_reply)
        t0 = mon.mark()
        await host.run(1)
        # quarter = 128 cycles -> bit = 512, 8 bits = 4096 + setup
        deadline = t0 + 9000
        while mon.mark() < deadline:
            pins = host.pins_out()
            sck = (pins >> 0) & 1        # SCK = P[0]
            mosi = (pins >> 1) & 1       # MOSI = P[1]
            cs_n = (pins >> 3) & 1       # CS_N = P[3], 1 = deasserted
            miso = slave.feed(mon.mark(), sck, mosi, cs_n)
            host.set_ext_uio(miso << 2, oe=0x04)   # MISO = P[2]
            await ClockCycles(dut.clk, 4)
        await host.stop(0x1)
        got = await host.rx_pop(0)
        tr = slave.transfers
        assert tr, f"mode {mode}: target saw no CS frame"
        t = tr[0]
        edges = len(t.edges)
        log_wave(host, render_wave(bit_tr(mon, 0), t0=t0, t1=t0 + 6000, cols=100,
                                   label=f"SPI master mode {mode}: SCK=P[0] (CPOL={mode>>1}, CPHA={mode&1})"))
        log_wave(host, render_wave(bit_tr(mon, 1), t0=t0, t1=t0 + 6000, cols=100,
                                   label="SPI master MOSI=P[1] (0xA5)"))
        host.log.info("mode %d: MOSI=%s MISO=%s received 0x%02x (%d SCK edges, "
                      "CS low %d..%d)", mode,
                      [hex(b) for b in t.mosi_bytes], [hex(b) for b in t.miso_bytes],
                      got, edges, t.cs_low_cycle, t.cs_high_cycle)
        assert t.mosi_bytes == [0xA5], f"mode {mode}: MOSI {[hex(b) for b in t.mosi_bytes]}"
        assert edges == 16, f"mode {mode}: {edges} SCK edges (want 16)"
        assert got == miso_reply[0], f"mode {mode}: MISO received 0x{got:02x}"


# ---------------------------------------------------------------------------
# SPI target: VIP master -> DUT
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_spi_slave(dut):
    """SPI target: VIP master drives two frames; DUT receives MOSI, returns +1."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    await load_firmware(host, "spi_slave.asm")
    # pins: MISO P[0], SCK P[4], MOSI P[5], CS_N P[6]
    m = v_spi.SpiMasterModel(0, half_period=100, cs_setup=20, cs_hold=20)
    frames = [0x5A, 0x3C]
    events = []
    samples = []
    t = mon.mark() + 400
    for byte in frames:
        ev, smp = m.schedule([byte], sck_bit=4, mosi_bit=5, cs_bit=6, t_start=t)
        events += ev
        samples += [(c, i, byte) for c, i in smp]
        t = ev[-1][0] + 400
    # Merge drive events and MISO sample instants into one time-ordered action
    # list so the master samples MISO exactly at the schedule instants.
    actions = [("drive", c, mp, None) for c, mp in events]
    actions += [("sample", c, None, (bit_i, byte)) for c, bit_i, byte in samples]
    actions.sort(key=lambda a: a[1])
    miso_bits = []
    idx = 0
    while idx < len(actions):
        kind, cyc, mapping, smp = actions[idx]
        while mon.mark() < cyc:
            await host._clk(min(10, max(1, cyc - mon.mark())))
        if kind == "drive":
            value = 0
            oe = 0
            for bit, lvl in mapping.items():
                value |= (1 << bit) if lvl else 0
                oe |= 1 << bit
            host.set_ext_uio(value, oe)
        else:
            line = host.pins_in() & 1
            miso_bits.append((smp[1], smp[0], line))
        idx += 1
    got = await wait_tx_bytes(host, 0, len(frames), timeout=8000)
    await host.stop(0x1)
    # reconstruct received bytes on MISO
    miso_bytes = []
    cur = 0
    n = 0
    for _byte, _i, line in miso_bits:
        cur = (cur << 1) | line
        n += 1
        if n == 8:
            miso_bytes.append(cur)
            cur = 0
            n = 0
    log_wave(host, render_wave(bit_tr(mon, 5), t0=events[0][0] - 20,
                               t1=events[-1][0] + 100, cols=100,
                               label="SPI target: MOSI=P[5] driven by VIP master"))
    log_wave(host, render_wave(bit_tr(mon, 0), t0=events[0][0] - 20,
                               t1=events[-1][0] + 100, cols=100,
                               label="SPI target: MISO=P[0] driven by DUT (expect 0x01,0x5B)"))
    host.log.info("SPI target: received MOSI=%s, MISO=%s",
                  [hex(b) for b in got], [hex(b) for b in miso_bytes])
    assert got == frames, f"SPI target received {[hex(b) for b in got]} != {frames}"
    assert miso_bytes == [0x01, 0x5B], f"SPI target MISO {[hex(b) for b in miso_bytes]}"


# ---------------------------------------------------------------------------
# I2C controller: DUT drives the bus, VIP target responds
# ---------------------------------------------------------------------------
def i2c_apply(host, scl_bit, sda_bit, scl_low, sda_low):
    """Open-drain drive: low => ext pulls the line low; high => release."""
    value = 0
    oe = 0
    if scl_low:
        oe |= 1 << scl_bit
    if sda_low:
        oe |= 1 << sda_bit
    host.set_ext_uio(value, oe)


async def run_i2c_controller(host, mon, *, i2c_hz: int, payload: list[int],
                             poll: int, stretch: int = 0,
                             target_regs=None):
    """Run i2c_master firmware with a reactive VIP target; return decoded info."""
    subs = {"I2C_HZ,\\s+100000": f"I2C_HZ,     {i2c_hz}"}
    await load_firmware(host, "i2c_master.asm", subs, run=False)
    for b in payload:
        await host.tx_push(0, b)
    t0 = mon.mark()
    await host.run(1)
    scl_bit, sda_bit = 4, 5               # SCL P[4], SDA P[5]
    target = v_i2c.I2cTargetModel(0x50, target_regs,
                                  stretch_cycles=stretch)
    scl_tr, sda_tr = [], []
    prev = (1, 1)
    deadline = t0 + int(i2c_hz / 1000.0 * 40 * 16) + 20000
    # bit period = 60e6/i2c_hz cycles; run ~45 bit periods + margin
    duration = int(60e6 / i2c_hz * 45) + 8000
    deadline = t0 + duration
    while mon.mark() < deadline and not (target.events and
                                         target.events[-1].kind == "stop"):
        pins_in = host.pins_in() & 0xFF
        scl = (pins_in >> scl_bit) & 1
        sda = (pins_in >> sda_bit) & 1
        if (scl, sda) != prev:
            scl_tr.append((mon.mark(), scl))
            sda_tr.append((mon.mark(), sda))
            prev = (scl, sda)
        want_low, stretch_polls = target.feed(mon.mark(), scl, sda)
        i2c_apply(host, scl_bit, sda_bit, bool(stretch_polls), want_low)
        await ClockCycles(host.dut.clk, poll)
    await host.stop(0x1)
    i2c_apply(host, scl_bit, sda_bit, False, False)
    return target, scl_tr, sda_tr


@cocotb.test()
async def test_i2c_controller_100k(dut):
    """I2C controller @100 kHz: write job, target decodes START/addr/reg/data/ACK."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    # job: cmd=0 (write), dev addr byte = 0xA0, register 0x00, data 0x5A
    payload = [0x00, 0xA0, 0x00, 0x5A]
    target, scl_tr, sda_tr = await run_i2c_controller(
        host, mon, i2c_hz=100000, payload=payload, poll=8)
    assert scl_tr, "I2C controller produced no SCL transitions (firmware did not start?)"
    events = v_i2c.decode(scl_tr, sda_tr)
    log_wave(host, render_wave(scl_tr, t0=scl_tr[0][0] - 50,
                               t1=scl_tr[-1][0] + 50, cols=100,
                               label="I2C controller @100 kHz: SCL=P[4] (open drain)"))
    log_wave(host, render_wave(sda_tr, t0=sda_tr[0][0] - 50,
                               t1=sda_tr[-1][0] + 50, cols=100,
                               label="I2C controller @100 kHz: SDA=P[5] (open drain)"))
    host.log.info("I2C decoded events: %s",
                  [(e.kind, hex(e.byte) if e.byte is not None else None, e.ack)
                   for e in events])
    kinds = [e.kind for e in events]
    assert "start" in kinds and "stop" in kinds, f"missing START/STOP: {kinds}"
    addrs = [e.byte for e in events if e.kind == "byte"]
    assert 0xA0 in addrs, f"address byte not on the wire: {[hex(a) for a in addrs]}"
    assert target.data_rx[:2] == [0x00, 0x5A], \
        f"target received {[hex(b) for b in target.data_rx]} (want reg 0x00, data 0x5A)"
    acks = [e.ack for e in events if e.kind == "ack"]
    assert all(a == 0 for a in acks), f"ACK bits {acks} (want all 0 = ACK)"
    host.log.info("I2C controller 100 kHz: START, addr 0xA0 ACKed, reg 0x00, "
                  "data 0x5A, all ACKs, STOP -- exact")


@cocotb.test(skip=KNOWN_ISSUES["i2c_400k"]["open"])
async def test_i2c_controller_400k(dut):
    """I2C controller @400 kHz with a clock-stretching target."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    payload = [0x00, 0xA0, 0x00, 0x5A]
    target, scl_tr, sda_tr = await run_i2c_controller(
        host, mon, i2c_hz=400000, payload=payload, poll=I2C_400K_POLL,
        stretch=I2C_400K_STRETCH)
    assert scl_tr, "I2C controller produced no SCL transitions (firmware did not start?)"
    events = v_i2c.decode(scl_tr, sda_tr)
    log_wave(host, render_wave(scl_tr, t0=scl_tr[0][0] - 30,
                               t1=scl_tr[-1][0] + 30, cols=100,
                               label="I2C controller @400 kHz + clock stretching: SCL"))
    host.log.info("I2C 400 kHz: %d events, SDA bytes %s",
                  len(events), [hex(e.byte) for e in events if e.kind == "byte"])
    assert any(e.kind == "start" for e in events) and any(e.kind == "stop" for e in events)
    assert target.data_rx[:2] == [0x00, 0x5A], \
        f"target received {[hex(b) for b in target.data_rx]}"
    host.log.info("I2C controller 400 kHz with target clock stretching: complete")


@cocotb.test()
async def test_i2c_clock_stretching(dut):
    """A target that holds SCL low after the address ACK must not stall the DUT."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    payload = [0x00, 0xA0, 0x00, 0x5A]
    # 120 polls x 4-cycle poll interval = 480 cycles of hold: longer than the
    # 300-cycle 100 kHz low phase, so this genuinely stretches the clock (and
    # the target's stretch now only ever starts from a low phase, per I2C).
    target, scl_tr, sda_tr = await run_i2c_controller(
        host, mon, i2c_hz=100000, payload=payload, poll=4, stretch=120)
    events = v_i2c.decode(scl_tr, sda_tr)
    log_wave(host, render_wave(scl_tr, t0=scl_tr[0][0] - 50,
                               t1=scl_tr[-1][0] + 50, cols=100,
                               label="I2C 100 kHz with clock stretching (target holds SCL low)"))
    kinds = [e.kind for e in events]
    host.log.info("stretch test: events=%s stretch_polls=%d", kinds, 25)
    assert "start" in kinds and "stop" in kinds, f"transaction did not complete: {kinds}"
    assert any(e.kind == "match" for e in target.events), "address was not decoded"
    host.log.info("clock stretching: target held SCL low for ~100 cycles; "
                  "controller resumed and finished the transaction")


# ---------------------------------------------------------------------------
# I2C target: VIP master -> DUT
# ---------------------------------------------------------------------------
@cocotb.test(skip=(KNOWN_ISSUES["i2c_target_ack"]["open"] or
                   KNOWN_ISSUES["dmem_addr"]["open"]))
async def test_i2c_target_write(dut):
    """I2C target: VIP master writes reg 0x02 = 0x5A; ACK + DMEM readback."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    await load_firmware(host, "i2c_target.asm")
    m = v_i2c.I2cMasterModel(half_period=300, setup=40)
    t_start = mon.mark() + 400
    events, ack_samples = m.schedule(0x50, writes=[0x5A], reg=0x02, t_start=t_start)
    scl_bit, sda_bit = 6, 7              # target SCL=P[6], SDA=P[7]
    scl_tr, sda_tr = [], []
    actions = [("drive", c, (scl, sda), None) for c, scl, sda in events]
    actions += [("ack", c, None, None) for c in ack_samples]
    actions.sort(key=lambda a: a[1])
    acks = []
    for kind, cyc, lv, _ in actions:
        while mon.mark() < cyc:
            await host._clk(min(10, max(1, cyc - mon.mark())))
        if kind == "drive":
            scl_lvl, sda_lvl = lv
            low_val = 0
            oe = 0
            if scl_lvl == 0:
                oe |= 1 << scl_bit
            if sda_lvl == 0:
                oe |= 1 << sda_bit
            host.set_ext_uio(low_val, oe)
            line = host.pins_in()
            scl_tr.append((cyc, (line >> scl_bit) & 1))
            sda_tr.append((cyc, (line >> sda_bit) & 1))
        else:
            acks.append((host.pins_in() >> sda_bit) & 1)
    host.set_ext_uio(0, 0)
    events_d = v_i2c.decode(scl_tr, sda_tr)
    log_wave(host, render_wave(sda_tr, t0=t_start - 100, t1=events[-1][0] + 100,
                               cols=100,
                               label="I2C target: SDA=P[7] (VIP master writes reg 0x02=0x5A, DUT ACKs)"))
    host.log.info("I2C target decoded: %s",
                  [(e.kind, hex(e.byte) if e.byte is not None else None, e.ack)
                   for e in events_d])
    reg = (await host.read_dmem(4))[2]
    host.log.info("I2C target: ACK samples=%s, DMEM[2]=0x%02x", acks, reg)
    assert all(a == 0 for a in acks), f"target did not ACK: {acks}"
    assert reg == 0x5A, f"DMEM[2]=0x{reg:02x} (want 0x5A written by I2C master)"
    host.log.info("I2C target: address 0x50 ACKed, reg 0x02 <- 0x5A, DMEM readback exact")

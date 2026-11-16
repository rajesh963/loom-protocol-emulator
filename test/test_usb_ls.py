#!/usr/bin/env python3
"""RTL verification of the USB 1.1 low-speed device transmitter (stretch goal).

The chip drives single-ended D+ on uio[0] (P[0], push-pull, NRZI, idle J = low)
from sw/examples/usb_ls_tx.asm.  The host supplies a 4-byte payload through the
normal TX FIFO path; this layer captures the *RTL pin trace*, decodes it with
the independent reference receiver in host/usb_ls.py (NRZI, bit unstuffing,
SYNC/PID+complement/CRC16/EOP) and asserts the architectural claims:

  * SYNC, PID+complement, payload, CRC16 and EOP decode exactly;
  * every NRZI edge sits on the 22.25-cycle event grid (max residual < 1 cycle,
    i.e. integer-cycle quantisation only -- the scheduler, not CPU timing,
    places the edges);
  * the bit rate is within the USB LS +/-0.25 % budget;
  * the stuffed-bit count matches the reference encoder for the payload;
  * wire-edge intervals are (near) integer multiples of the recovered bit
    period (22/23, 44/45, 66/67, 88/89, 111/112, 133/134, 155/156 cycles).

Both RTL tests are pin-level only (no backdoor), so they also run under
`sand GATES=yes` for the gl_test job.
"""

from __future__ import annotations

import importlib.util
import pathlib

import cocotb

from host import start_loom
from vip import wave as v_wave

REPO = pathlib.Path(__file__).resolve().parent.parent
EX = REPO / "sw" / "examples"


def _load_usb_ls():
    """Load the repo-level host/usb_ls.py reference receiver.

    test/host.py shadows the repo `host/` package on sys.path, so load the
    reference codec by path instead of importing the package.
    """
    path = REPO / "host" / "usb_ls.py"
    spec = importlib.util.spec_from_file_location("loom_usb_ls", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usb_ls = _load_usb_ls()
CLK_NS = 30.0                  # shipped 33.33 MHz target clock
CLOCK_HZ = 33333333
BIT_PERIOD = 22.25             # setp 0x0164 (356/16) in usb_ls_tx.asm
CAPTURE_CYCLES = 4200          # packet (<=1602) + EOP (45) + idle gap


class _World:
    """Minimal stand-in for `sw.protocols.WireWorld` (the decoder only needs
    the clock rate and an `attach()` to register itself)."""

    def __init__(self, clock_hz: int) -> None:
        self.clock_hz = clock_hz

    def attach(self, _agent) -> None:      # pragma: no cover - trivial
        pass


async def _run_packet(host, mon, payload: bytes):
    """Load/RUN the firmware for one payload and return (packet, decoder, trace)."""
    src = (EX / "usb_ls_tx.asm").read_text(encoding="utf-8")
    await host.reset(cycles=8)
    await host.load_asm(src, addr=0)
    await host.set_pc(0, 0)
    for b in payload:
        await host.tx_push(0, b)
    t0 = mon.mark()
    await host.run(0x1)
    await host._clk(CAPTURE_CYCLES)
    await host.stop(0x1)

    # Reconstruct the D+ level for every cycle from the PinMonitor transitions
    # (monitor samples once per cycle and records changes, so this is exact).
    recs = [(c, p & 1) for c, p, _d in mon.records if c >= t0]
    dec = usb_ls.UsbLsDecoder(_World(CLOCK_HZ))
    level, idx, end = 0, 0, t0 + CAPTURE_CYCLES
    tr = []
    for c in range(t0, end + 1):
        while idx < len(recs) and recs[idx][0] <= c:
            new_level = recs[idx][1]
            if new_level != level:          # only real D+ edges count
                tr.append((recs[idx][0], new_level))
                level = new_level
            idx += 1
        dec.observe(c, level, 1)
    dec.flush()
    if dec.errors:
        raise AssertionError(f"USB LS decoder errors: {dec.errors}")
    assert len(dec.packets) == 1, (
        f"expected one packet in {CAPTURE_CYCLES} cycles, got "
        f"{len(dec.packets)} ({dec.summary()})")
    return dec.packets[0], dec, tr, t0


def _check_packet(host, pkt, dec, tr, t0, payload: bytes, stuff: int, crc: int):
    ref = usb_ls.encode_reference(payload)
    assert ref["crc"] == crc and ref["stuff_count"] == stuff
    log = host.log
    for line in dec.summary():
        log.info("USB LS: %s", line)
    log.info("USB LS D+ waveform:")
    for line in v_wave.render(tr, t0=t0, t1=tr[-1][0] + 100, cols=100,
                              label="D+ = uio[0] (NRZI)").splitlines():
        log.info(line)

    assert pkt.sync_ok, f"SYNC decode failed: samples={pkt.samples[:10]}"
    assert pkt.pid_ok and pkt.pid == usb_ls.PID_DATA0, \
        f"PID 0x{pkt.pid:02X} (want DATA0 0xC3 with valid complement)"
    assert pkt.payload == payload, \
        f"payload {pkt.payload.hex()} != {payload.hex()}"
    assert pkt.crc_rx == crc and pkt.crc_ok, \
        f"CRC 0x{pkt.crc_rx:04X} (want 0x{crc:04X})"
    assert pkt.eop_ok, "EOP cells not both low"
    assert pkt.stuff_count == stuff, \
        f"stuffed bits {pkt.stuff_count} != expected {stuff}"
    assert abs(pkt.period_cycles - BIT_PERIOD) < 0.10, \
        f"bit period {pkt.period_cycles:.3f} cycles (want {BIT_PERIOD})"
    err = pkt.bit_rate / usb_ls.USB_LS_BIT_RATE - 1
    assert abs(err) < 0.0025, \
        f"bit rate {pkt.bit_rate/1e6:.4f} Mbit/s, error {err*100:+.3f}% > 0.25%"
    assert pkt.grid_residual_max < 1.0, \
        f"max edge residual {pkt.grid_residual_max:.3f} cycles (want < 1.0: " \
        f"edges must be on the event grid, not CPU-jittered)"

    # Wire-edge interval histogram: every gap must be a near-integer number of
    # bit cells, with the observed per-cell quantisation (22/23 cycles).
    edges = [c for c, _ in tr]
    gaps = [b - a for a, b in zip(edges, edges[1:])]
    hist: dict[int, int] = {}
    for g in gaps:
        hist[g] = hist.get(g, 0) + 1
    allowed = {22, 23, 44, 45, 66, 67, 88, 89, 111, 112, 133, 134, 155, 156}
    bad = [g for g in hist if g not in allowed]
    assert not bad, f"non-grid wire gaps {bad} (hist {hist})"
    assert {22, 23} & set(hist), f"no single-bit gaps: {hist}"
    log.info("USB LS: period=%.3f cycles, rate=%.4f Mbit/s (%+.3f %%), "
             "stuff=%d, max grid residual=%.2f cycles, gaps=%s",
             pkt.period_cycles, pkt.bit_rate / 1e6, err * 100,
             pkt.stuff_count, pkt.grid_residual_max, hist)
    return err


@cocotb.test()
async def test_usb_ls_tx_packet_ff(dut):
    """USB LS DATA0 packet with payload FF FF FF FF: 6 stuffed bits, CRC 0x4FFE."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    payload = bytes([0xFF, 0xFF, 0xFF, 0xFF])
    pkt, dec, tr, t0 = await _run_packet(host, mon, payload)
    _check_packet(host, pkt, dec, tr, t0, payload, stuff=6, crc=0x4FFE)


@cocotb.test()
async def test_usb_ls_tx_packet_loom(dut):
    """USB LS DATA0 packet with payload 'LOOM': 0 stuffed bits, CRC 0x992C."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)
    payload = b"LOOM"
    pkt, dec, tr, t0 = await _run_packet(host, mon, payload)
    _check_packet(host, pkt, dec, tr, t0, payload, stuff=0, crc=0x992C)

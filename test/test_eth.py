#!/usr/bin/env python3
"""RTL verification of the Ethernet (802.3) Manchester frame transmitter.

`sw/examples/eth_tx.asm` transmits a 64-byte minimum Ethernet frame with IEEE
802.3 Manchester line coding and a firmware-computed CRC-32 FCS on single-ended
TXD = uio[0] (P[0], push-pull, idle low).  The host preloads DMEM[0..29] with
the 60 data bytes (little-endian byte pairs) and leaves DMEM[30..31] zero; the
firmware computes the FCS into DMEM[30..31] and streams
preamble + SFD + frame + IPG.  A matching CRC-32 on the wire therefore proves
the **firmware** computed the FCS.

The RTL TXD pin trace is decoded with the independent Manchester receiver in
`host/eth.py` (edge-based cell clock, preamble/SFD lock, byte and FCS checks) and
asserts:
  * 7 x 0xAA preamble + 0xD5 SFD lock, 64 frame bytes byte-exact, FCS exact;
  * recovered cell period 8.000 cycles, bit period 16.00, rate 2.0833 Mbit/s;
  * **max edge residual 0.00 cycle** - every Manchester edge is on the exact
    event grid (scheduler, not instruction timing, places the cells);
  * the frame is exactly 9216 cycles on the wire (576 bits x 16) and the TXD
    line then stays quiet for at least the 1536-cycle IPG (the firmware also
    spends untimed instructions recomputing the FCS after the IPG, so the
    frame-to-frame gap is IPG + that compute time; the 1536-cycle IPG is the
    guaranteed quiet floor, which is what is asserted here).

One cocotb test covers two host-preloaded frames (the documented default frame
with FCS 0xF57743B9, and an arbitrary custom frame with its own FCS), both
pin-level only so this also runs in the gl_test netlist.
"""

from __future__ import annotations

import importlib.util
import pathlib

import cocotb

from host import start_loom
from vip import wave as v_wave

REPO = pathlib.Path(__file__).resolve().parent.parent
EX = REPO / "sw" / "examples"
CLK_NS = 30.0                 # shipped 33.33 MHz target clock
CLOCK_HZ = 33333333
FRAME_CYCLES = 9216           # 576 bits x 16 cycles
IPG_CYCLES = 1536


def _load_eth():
    """Load the repo-level host/eth.py receiver (test/host.py shadows the repo
    `host/` package on sys.path)."""
    path = REPO / "host" / "eth.py"
    spec = importlib.util.spec_from_file_location("loom_eth", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eth = _load_eth()


class _World:
    def __init__(self, clock_hz: int) -> None:
        self.clock_hz = clock_hz

    def attach(self, _agent) -> None:      # pragma: no cover - trivial
        pass


async def _capture_frame(host, mon, frame60: bytes):
    """Preload a 60-byte frame, transmit, return (frame, decoder, trace, t_first)."""
    assert len(frame60) == 60
    src = (EX / "eth_tx.asm").read_text(encoding="utf-8")
    await host.reset(cycles=8)
    await host.load_asm(src, addr=0)
    words = [frame60[2 * i] | (frame60[2 * i + 1] << 8) for i in range(30)]
    await host.write_dmem_block(0, words)     # DMEM[0..29]; FCS words 30/31 = 0
    await host.write_dmem(30, 0)
    await host.write_dmem(31, 0)
    await host.set_pc(0, 0)
    t0 = mon.mark()
    await host.run(0x1)
    # the firmware does CRC-32 before the first preamble edge (untimed): wait
    # for the first TXD crossing rather than using a fixed offset
    t_first = None
    while mon.mark() < t0 + 25000 and t_first is None:
        await host._clk(16)
        for c, p, _d in mon.records:
            if c >= t0 and (p & 1):
                t_first = c
                break
    assert t_first is not None, \
        "no TXD edge within 25000 cycles (firmware did not transmit)"
    # frame (9216) + IPG (1536) + margin; the next preamble edge comes after
    # the firmware's post-IPG FCS recompute, so this window sees it too
    await host._clk(FRAME_CYCLES + 6000)
    await host.stop(0x1)
    t_end = mon.mark()

    recs = [(c, p & 1) for c, p, _d in mon.records if t0 <= c <= t_end]
    dec = eth.ManchesterDecoder(_World(CLOCK_HZ))
    level, idx, tr = 0, 0, []
    for c in range(t0, t_end + 1):
        while idx < len(recs) and recs[idx][0] <= c:
            new_level = recs[idx][1]
            if new_level != level:
                tr.append((recs[idx][0], new_level))
                level = new_level
            idx += 1
        dec.observe(c, level, 1)
    dec.flush()
    complete = [f for f in dec.frames if len(f.frame) >= 64]
    assert complete, (
        f"no complete frame decoded (first edge {t_first}, "
        f"frames={[repr(f) for f in dec.frames]}, errors={dec.errors})")
    return complete[0], dec, tr, t_first


def _check_frame(host, fr, dec, tr, t_first, expected: bytes):
    log = host.log
    for line in dec.summary():
        log.info("Ethernet: %s", line)
    for line in v_wave.render(tr, t0=t_first, t1=t_first + 1200, cols=100,
                              label="TXD = uio[0] (Manchester, first 1200 cycles)"
                              ).splitlines():
        log.info(line)
    want_fcs = int.from_bytes(expected[60:], "little")
    assert fr.preamble_ok, "preamble lock failed"
    assert fr.sfd_ok, "SFD 0xD5 not found"
    assert fr.frame == expected, (
        f"frame mismatch:\n got {fr.frame.hex()}\n want {expected.hex()}")
    assert fr.fcs_rx == want_fcs and fr.fcs_ok, \
        f"FCS 0x{fr.fcs_rx:08X} (want 0x{want_fcs:08X}, calc 0x{fr.fcs_calc:08X})"
    assert abs(fr.cell_period - 8.0) < 0.01, \
        f"cell period {fr.cell_period:.3f} cycles (want 8.000)"
    assert abs(fr.bit_period - 16.0) < 0.02, \
        f"bit period {fr.bit_period:.3f} cycles (want 16.00)"
    err = fr.data_rate / 2083333.0 - 1
    assert abs(err) < 0.001, \
        f"data rate {fr.data_rate/1e6:.4f} Mbit/s (want 2.0833, error {err*100:+.3f}%)"
    assert fr.max_residual <= 0.01, \
        f"max edge residual {fr.max_residual:.3f} cycles (want 0.00: edges must "\
        f"be on the exact event grid, no CPU/issue jitter)"

    # Frame wire time and IPG: the first frame's cells occupy exactly 9216
    # cycles; after that TXD must stay quiet for at least the 1536-cycle IPG.
    t_frame_end = t_first + FRAME_CYCLES
    in_frame = [c for c, _l in tr if c <= t_frame_end]
    after = [c for c, _l in tr if c > t_frame_end]
    assert in_frame, "no edges inside the frame window"
    assert in_frame[-1] <= t_frame_end, \
        f"edge at {in_frame[-1]} past the 9216-cycle frame window ({t_frame_end})"
    quiet = (after[0] - t_frame_end) if after else None
    assert after, ("no TXD edge after the frame within the capture window "
                   "(cannot check the IPG quiet floor)")
    assert quiet >= IPG_CYCLES, \
        f"post-frame quiet gap {quiet} cycles < IPG {IPG_CYCLES} cycles"
    log.info("Ethernet: frame=%d bytes, FCS=0x%08X ok, cell=%.3f cyc, "
             "bit=%.2f cyc, rate=%.4f Mbit/s, max_residual=%.2f cyc, "
             "frame_window=%d cyc, post-frame quiet=%d cyc (IPG floor %d)",
             len(fr.frame), fr.fcs_rx, fr.cell_period, fr.bit_period,
             fr.data_rate / 1e6, fr.max_residual, FRAME_CYCLES, quiet, IPG_CYCLES)


@cocotb.test()
async def test_eth_frame_tx(dut):
    """Ethernet 802.3 Manchester frame TX: default frame (FCS 0xF57743B9) and a
    second host-preloaded frame, decoded from the RTL TXD pin trace."""
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)

    default = eth.default_frame()
    assert len(default) == 64
    fr, dec, tr, t_first = await _capture_frame(host, mon, default[:60])
    _check_frame(host, fr, dec, tr, t_first, default)

    custom = eth.build_frame(
        bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01]),
        bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x02]),
        bytes([0x08, 0x00]),
        b"LOOM stage-1 RTL Manchester decode test!")
    assert len(custom) == 64
    fr, dec, tr, t_first = await _capture_frame(host, mon, custom[:60])
    _check_frame(host, fr, dec, tr, t_first, custom)

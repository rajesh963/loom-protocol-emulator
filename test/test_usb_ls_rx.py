#!/usr/bin/env python3
"""USB LS differential-receiver *readiness* test (pin path + reference decoder).

There is **no on-chip USB RX firmware** (the unverified draft was dropped), so
this is deliberately not a device-response test: it drives an external
differential host waveform (LS D+ = uio[0], D- = uio[1], open-drain pad model,
chip undriven) and checks that the RTL-visible `pins_in` sequence decodes with
the reference device receiver in `host/usb_ls_rx.py` - DPLL seeded from SYNC,
NRZI, bit unstuffing, PID+complement, CRC-5/CRC-16 and EOP.

What it proves, honestly:
  * the tb open-drain pad model passes a real differential LS line through to
    the chip's `pins_in` (no inversion/aliasing on uio[0]/uio[1], chip high-Z);
  * the reference RX algorithm recovers a SETUP token + 8-byte DATA0
    GET_DESCRIPTOR request at nominal and +/-0.25 % host clock drift.

It does **not** prove enumeration or any firmware RX path; those remain follow-on
work.  Pin-level only, so it also runs in the gl_test netlist.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import cocotb

from host import CSR, start_loom
from vip import wave as v_wave

REPO = pathlib.Path(__file__).resolve().parent.parent
CLK_NS = 30.0                 # shipped 33.33 MHz target clock
CLOCK_HZ = 33333333


def _load_attr(path: pathlib.Path, name: str, patch_host: dict | None = None):
    """Load a repo module by path, optionally with a temporary fake `host` pkg
    (test/host.py shadows the repo host/ package on sys.path)."""
    saved = {k: sys.modules.get(k) for k in (patch_host or {})}
    if patch_host:
        sys.modules.update(patch_host)
    try:
        spec = importlib.util.spec_from_file_location(f"loom_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load_usb_rx():
    usb_ls = _load_attr(REPO / "host" / "usb_ls.py", "usb_ls")
    host_pkg = types.ModuleType("host")
    host_pkg.__path__ = [str(REPO / "host")]     # let `host.usb_ls` resolve
    host_pkg.usb_ls = usb_ls
    return _load_attr(REPO / "host" / "usb_ls_rx.py", "usb_ls_rx",
                      patch_host={"host": host_pkg,
                                  "host.usb_ls": usb_ls})


rx = _load_usb_rx()


def _state_at(transitions, k: int):
    st = (0, 1)                                # idle J
    for c, s in transitions:
        if c <= k:
            st = s
        else:
            break
    return st


@cocotb.test()
async def test_usb_ls_rx_reference_decoder(dut):
    """Drive a differential SETUP+DATA0 LS waveform and decode the RTL pin state."""
    setup_data = bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x40, 0x00])
    packets = [rx.encode_packet("token", pid=rx.PID_SETUP, addr=0, endp=0),
               rx.encode_packet("data", pid=rx.PID_DATA0, payload=setup_data)]
    host, mon = await start_loom(dut, clk_ns=CLK_NS, spi_half=3)

    # The chip must be high-Z on uio[0]/uio[1]: threads halted, DIR = 0.
    assert await host.read_csr(CSR["PDIR"]) == 0, \
        "PDIR not 0 after reset: the chip would fight the external differential drive"

    for scale, tag in ((1.0, "nominal"), (1.0025, "host +0.25%"),
                       (0.9975, "host -0.25%")):
        await host.reset(cycles=8)
        assert await host.read_csr(CSR["PDIR"]) == 0
        trace = rx.synthetic_trace(packets, clock_hz=CLOCK_HZ,
                                   clock_scale=scale)
        tr = sorted(trace["transitions"], key=lambda x: x[0])
        n = int(trace["cycles"]) + 8
        states = [_state_at(tr, k) for k in range(n)]
        # drive D+/D- externally (oe = both pins) cycle by cycle and sample the
        # RTL's pins_in path in the same cycle
        t_start = mon.mark() + 50
        await host._clk(t_start - mon.mark())
        sampled = []
        for k, st in enumerate(states):
            host.set_ext_uio((st[0] << 0) | (st[1] << 1), 0b11)
            await host._clk(1)
            sampled.append((k, host.pins_in() & 0b11))
        host.set_ext_uio(0, 0)

        mismatched = [k for k, got in sampled
                      if got != ((states[k][0] << 0) | (states[k][1] << 1))]
        assert not mismatched, (
            f"pad path mismatch at cycles {mismatched[:8]} ({len(mismatched)} "
            f"of {len(states)}): pins_in != externally driven D+/D-")

        trans = []
        prev = (0, 1)
        for k, got in sampled:
            st = (got & 1, (got >> 1) & 1)
            if st != prev:
                trans.append((k, st))
                prev = st
        dec = rx.UsbDeviceDecoder(clock_hz=CLOCK_HZ)
        dec.decode_stream(trans)
        host.log.info("USB LS RX readiness [%s]: decoded %d packets, "
                      "external drive %d cycles, pad mismatches 0",
                      tag, len(dec.packets), len(states))
        for line in dec.summary():
            host.log.info("  %s", line)
        dptr = [(c, s[0]) for c, s in trans]
        dmtr = [(c, s[1]) for c, s in trans]
        for line in v_wave.render(dptr, t0=0, t1=len(states), cols=100,
                                  label=f"D+ = uio[0] (external host TX, {tag})"
                                  ).splitlines():
            host.log.info(line)
        for line in v_wave.render(dmtr, t0=0, t1=len(states), cols=100,
                                  label=f"D- = uio[1] (external host TX, {tag})"
                                  ).splitlines():
            host.log.info(line)

        assert not dec.errors, dec.errors
        assert len(dec.packets) == 2, \
            f"[{tag}] expected SETUP + DATA0, got {[repr(p) for p in dec.packets]}"
        tok, dat = dec.packets
        assert tok.kind == "token" and tok.pid == rx.PID_SETUP \
            and tok.addr == 0 and tok.endp == 0 and tok.crc5_ok, repr(tok)
        assert dat.kind == "data" and dat.payload == setup_data and dat.crc16_ok, \
            repr(dat)
        assert all(p.sync_ok and p.pid_ok and p.eop_ok for p in dec.packets), \
            [(p.sync_ok, p.pid_ok, p.eop_ok) for p in dec.packets]
        want = 22.25 * scale
        assert abs(tok.period_cycles - want) < 0.05, \
            f"[{tag}] recovered period {tok.period_cycles:.4f} cyc (want {want:.4f})"
        assert abs(tok.clock_offset) < 0.005, \
            f"[{tag}] clock offset {tok.clock_offset*100:+.3f}%"
        resid = max(p.grid_residual_max for p in dec.packets)
        assert resid <= 1.0, f"[{tag}] grid residual {resid:.2f} cycles"
        host.log.info("USB LS RX readiness [%s]: SETUP(addr=0,endp=0,CRC5 ok) + "
                      "DATA0(%d bytes, CRC16 ok), EOP ok, period=%.4f cyc "
                      "(%+.3f%%), offset=%+.3f%%, residual<=%.2f cyc",
                      tag, len(dat.payload), tok.period_cycles,
                      (tok.period_cycles / 22.25 - 1) * 100,
                      tok.clock_offset * 100, resid)

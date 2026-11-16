#!/usr/bin/env python3
"""USB 1.1 Low-Speed far-end model: NRZI decode, bit unstuffing, PID/CRC16.

This is the *host side* of the `sw/examples/usb_ls_tx.asm` stretch example.  The
chip drives a single-ended D+ line on `uio[0]` (P[0], NRZI, LS idle J = low),
because LOOM has one wire and the competition allows external parts: a real
board needs a USB connector/Pmod with D- tied to the LS idle level (high) and a
1.5 kOhm pull-up on D-, so monitoring D+ alone is enough to recover the packet.

Implements
----------
`crc16_usb(data)`     USB CRC16: poly 0x8005 bit-reflected (0xA001), init
                      0xFFFF, final XOR 0xFFFF, LSB-first.  Verified against the
                      published CRC-16/USB check value for "123456789" = 0xB4C8.
`encode_reference(p)` reference encoder: SYNC + PID + data + CRC16 + stuffing +
                      EOP -> encoded NRZI levels (used to predict what the chip
                      must put on the wire and to count stuffed bits).
`UsbLsDecoder`        FarEnd plugin for `WireWorld`: segments packets by the
                      idle gap, recovers the bit period from the SYNC edges
                      (the same information a USB DPLL locks to), then samples,
                      NRZI-decodes, unstuffs and checks SYNC/PID/complement/
                      CRC16/EOP.  Reports period + measured bit rate.

Packet format assumed (device -> host, DATA0):
    idle J | SYNC 8 bits (00000001, NRZI KJKJKJKK) | PID | data (4 bytes) |
    CRC16 (16 bits, LSB first) | EOP (SE0, 2 bit cells) | idle J
Data bits after SYNC are bit-stuffed (a 0 inserted after six consecutive 1s);
SYNC is never stuffed and EOP is not NRZI coded.

The payload length is fixed at 4 bytes by the firmware (one DATA0 packet per
host command), which is what lets a single-ended decoder find the EOP: in the
single-ended model J and SE0 are both "D+ low", so the decoder relies on the
known packet length and then checks that D+ is held low for the 2 EOP cells.
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sw.protocols import FarEnd, WireWorld, format_wave        # noqa: E402

__all__ = ["crc16_usb", "encode_reference", "UsbLsDecoder", "UsbPacket",
           "DP_PIN", "PID_DATA0", "PAYLOAD_BYTES"]

DP_PIN = 0                     # P[0] = uio[0] is the D+ line in the example
PID_DATA0 = 0xC3               # PID nibble 0011 + complement 1100, LSB first
PAYLOAD_BYTES = 4              # data field length fixed by usb_ls_tx.asm
USB_LS_BIT_RATE = 1_500_000
SYNC_BITS = [0, 0, 0, 0, 0, 0, 0, 1]     # LSB first; NRZI from J = KJKJKJKK


# ---------------------------------------------------------------------------
# CRC-16/USB
# ---------------------------------------------------------------------------
def crc16_usb(data: bytes, crc: int = 0xFFFF) -> int:
    """USB data CRC16 (poly 0x8005 reflected, init 0xFFFF, final XOR 0xFFFF)."""
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else (crc >> 1)
    return crc ^ 0xFFFF


# ---------------------------------------------------------------------------
# Reference encoder (what the firmware must produce)
# ---------------------------------------------------------------------------
def _nrz_encode(data_bits):
    """NRZI-encode decoded data bits starting from J (level 0).

    Returns the encoded levels: a 0 data bit toggles, a 1 holds.
    """
    level = 0
    levels = []
    for bit in data_bits:
        if bit == 0:
            level ^= 1
        levels.append(level)
    return levels


def encode_reference(payload: bytes, pid: int = PID_DATA0) -> dict:
    """Build the reference packet: decoded bits, NRZI levels, stuffing count."""
    payload = bytes(bytearray(payload))
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError(f"usb_ls_tx.asm sends exactly {PAYLOAD_BYTES} data bytes")
    crc = crc16_usb(payload)
    packet_bytes = bytes([pid]) + payload + bytes([crc & 0xFF, crc >> 8])
    # SYNC: not stuffed
    data_bits = list(SYNC_BITS)
    stuff = 0
    ones = 0
    for byte in packet_bytes:
        for i in range(8):
            bit = (byte >> i) & 1
            if ones == 6:
                data_bits.append(0)          # stuffed zero
                stuff += 1
                ones = 0
            data_bits.append(bit)
            ones = ones + 1 if bit else 0
    levels = _nrz_encode(data_bits)
    # EOP: D+ forced low for two bit cells (single-ended SE0)
    eop = [0, 0]
    return {
        "pid": pid,
        "payload": payload,
        "crc": crc,
        "bytes": packet_bytes,
        "data_bits": data_bits,
        "levels": levels,
        "eop": eop,
        "stuff_count": stuff,
        "encoded_bits": len(data_bits),
    }


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------
class UsbPacket:
    """One decoded device->host LS packet."""

    def __init__(self) -> None:
        self.sync_ok = False
        self.pid = 0
        self.pid_ok = False
        self.payload = b""
        self.crc_rx = 0
        self.crc_calc = 0
        self.crc_ok = False
        self.eop_ok = False
        self.stuff_count = 0
        self.encoded_bits = 0
        self.period_cycles = 0.0
        self.bit_rate = 0.0
        self.samples: list[int] = []
        self.start_cycle = 0
        self.end_cycle = 0
        self.edge_cycles: list[int] = []
        self.grid_residual_max = 0.0
        self.errors: list[str] = []

    @property
    def ok(self) -> bool:
        return (self.sync_ok and self.pid_ok and self.crc_ok and self.eop_ok
                and not self.errors)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"UsbPacket(pid=0x{self.pid:02X}, payload={self.payload!r}, "
                f"crc=0x{self.crc_rx:04X}/0x{self.crc_calc:04X}, "
                f"stuff={self.stuff_count}, rate={self.bit_rate/1e6:.4f} Mbit/s, "
                f"ok={self.ok})")


class UsbLsDecoder(FarEnd):
    """A USB LS host-side receiver attached to a `WireWorld` (single-ended D+).

    Segments packets on the idle gap, recovers the bit period from the SYNC
    edges, samples the line at bit centres, NRZI-decodes, unstuffs and checks
    SYNC/PID/CRC16/EOP.
    """

    name = "usb-ls-host"

    def __init__(self, world: WireWorld, *, dp_pin: int = DP_PIN,
                 payload_bytes: int = PAYLOAD_BYTES,
                 idle_gap_bits: float = 20.0):
        self.world = world
        self.dp_pin = int(dp_pin)
        self.payload_bytes = int(payload_bytes)
        self.bit_period_nominal = world.clock_hz / USB_LS_BIT_RATE
        self.gap_cycles = self.bit_period_nominal * float(idle_gap_bits)
        self._prev = 1
        self._ready = False
        self._active = False
        self._group: list[tuple[int, int]] = []
        self._last_change = 0
        self.packets: list[UsbPacket] = []
        self.errors: list[str] = []
        self.wave: list[tuple[int, int]] = [(0, 1)]
        world.attach(self)

    # -- pin observation / segmentation ---------------------------------
    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        level = (pins_out16 >> self.dp_pin) & 1
        if not self._ready:
            self._ready = True
            self._prev = level
            self.wave = [(cycle, level)]
            return
        if level != self._prev:
            self.wave.append((cycle, level))
            if not self._active:
                self._active = True
                self._group = []
            self._group.append((cycle, level))
            self._last_change = cycle
        elif self._active and (cycle - self._last_change) > self.gap_cycles:
            self._finish()
        self._prev = level

    def flush(self) -> None:
        """Decode a packet that is still in progress (e.g. at end of capture)."""
        if self._active:
            self._finish()

    def _finish(self) -> None:
        group, self._group = self._group, []
        self._active = False
        if not group:
            return
        try:
            self.packets.append(self._decode(group))
        except Exception as exc:            # keep debugging cheap
            self.errors.append(f"decode failed: {exc!r}")
            raise

    # -- decoding --------------------------------------------------------
    @staticmethod
    def _level_at(group, cycle: float) -> int:
        level = 0                      # idle J before the first transition
        for c, v in group:
            if c <= cycle:
                level = v
            else:
                break
        return level

    def _decode(self, group) -> UsbPacket:
        pkt = UsbPacket()
        t0 = group[0][0]
        if group[0][1] != 1:
            pkt.errors.append("packet does not start with a 0->1 transition")
        # --- clock recovery: the first 7 SYNC bits are 0 -> 7 transitions
        # exactly one bit period apart (t0 .. t0+6P).
        edges = [c for c, _ in group]
        k_sync = 7
        if len(edges) < k_sync:
            pkt.errors.append(f"only {len(edges)} transitions (need >= {k_sync})")
            return pkt
        period = (edges[k_sync - 1] - edges[0]) / float(k_sync - 1)
        if period <= 1.0:
            pkt.errors.append(f"bad recovered period {period:.2f}")
            return pkt
        # Refine with a two-pass least-squares fit over every transition (the
        # firmware's 12.4 carry makes edge intervals alternate 22/23 cycles, so
        # a 6-interval SYNC estimate alone is ~0.4 % off; the fitted value is
        # the average bit period a DPLL would converge to).
        for _ in range(2):
            fits = [(round((t - t0) / period), t) for t in edges
                    if (t - t0) / period >= 0.5]
            if len(fits) < 3:
                break
            mm = sum(m for m, _ in fits) / len(fits)
            mt = sum(t for _, t in fits) / len(fits)
            den = sum((m - mm) ** 2 for m, _ in fits)
            if den <= 0:
                break
            period = sum((m - mm) * (t - mt) for m, t in fits) / den
        pkt.period_cycles = period
        pkt.bit_rate = self.world.clock_hz / period
        pkt.start_cycle = int(t0)
        pkt.end_cycle = int(group[-1][0])
        pkt.edge_cycles = [int(c) for c in edges]
        # every NRZI edge must sit on the 22.25-cycle event grid (max residual
        # to a fitted bit-boundary index) -- proves the scheduler, not the CPU,
        # placed the edges (no clamp / no instruction-timing jitter).
        pkt.grid_residual_max = max(
            abs(round((t - t0) / period) * period - (t - t0)) for t in edges)

        # --- sample at bit centres with the recovered clock
        n_bits = int((group[-1][0] - t0) / period) + 6
        levels = [self._level_at(group, t0 + (k + 0.5) * period)
                  for k in range(n_bits)]
        # NRZI: transition -> 0, hold -> 1; idle J (0) before bit 0.
        bits = []
        prev = 0
        for lv in levels:
            bits.append(0 if lv != prev else 1)
            prev = lv
        pkt.samples = bits

        # --- SYNC
        sync = bits[:8]
        pkt.sync_ok = sync == SYNC_BITS
        if not pkt.sync_ok:
            pkt.errors.append(f"SYNC {sync} != {SYNC_BITS}")

        # --- unstuff after SYNC: 8 PID bits + data*8 + 16 CRC bits
        want = 8 + 8 * self.payload_bytes + 16
        unstuffed: list[int] = []
        ones = 0
        idx = 8
        while len(unstuffed) < want:
            if idx >= len(bits):
                pkt.errors.append("ran out of bits while unstuffing")
                return pkt
            b = bits[idx]
            idx += 1
            if ones == 6:
                if b != 0:
                    pkt.errors.append("stuffing violation (6 ones not followed by 0)")
                pkt.stuff_count += 1
                ones = 0
                continue
            unstuffed.append(b)
            ones = ones + 1 if b else 0

        def value_of(bs):
            v = 0
            for i, b in enumerate(bs):
                v |= b << i                 # USB sends LSB first
            return v

        pkt.pid = value_of(unstuffed[:8])
        pkt.pid_ok = (pkt.pid >> 4) == ((~pkt.pid) & 0x0F)
        pkt.payload = bytes(value_of(unstuffed[8 + 8 * i:16 + 8 * i])
                            for i in range(self.payload_bytes))
        pkt.crc_rx = value_of(unstuffed[8 + 8 * self.payload_bytes:want])
        pkt.crc_calc = crc16_usb(pkt.payload)
        pkt.crc_ok = pkt.crc_rx == pkt.crc_calc
        pkt.encoded_bits = idx

        # --- EOP: the two cells after the last encoded bit must be D+ low
        eop0 = self._level_at(group, t0 + (idx + 0.5) * period)
        eop1 = self._level_at(group, t0 + (idx + 1.5) * period)
        pkt.eop_ok = (eop0 == 0 and eop1 == 0)
        if not pkt.eop_ok:
            pkt.errors.append(f"EOP cells {eop0},{eop1} (want 0,0)")
        return pkt

    # -- reporting -------------------------------------------------------
    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        return {f"D+ P[{self.dp_pin}]": format_wave(self.wave, chars=chars,
                                                    start=start, end=end)}

    def summary(self) -> list[str]:
        lines = []
        for i, p in enumerate(self.packets):
            lines.append(
                f"packet {i}: SYNC={'ok' if p.sync_ok else 'BAD'} "
                f"PID=0x{p.pid:02X}{'' if p.pid_ok else ' (bad complement)'} "
                f"payload={p.payload.hex()} "
                f"CRC=0x{p.crc_rx:04X}{' ok' if p.crc_ok else ' BAD'} "
                f"stuff={p.stuff_count} EOP={'ok' if p.eop_ok else 'BAD'} "
                f"period={p.period_cycles:.3f} cyc -> {p.bit_rate/1e6:.4f} Mbit/s "
                f"grid_residual<={p.grid_residual_max:.2f} cyc")
        return lines


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    check("crc16_usb('123456789') == 0xB4C8 (CRC-16/USB)",
          crc16_usb(b"123456789") == 0xB4C8, hex(crc16_usb(b"123456789")))
    ref = encode_reference(bytes([0xFF, 0x00, 0xA5, 0x5A]))
    check("reference encoder: packet bytes = PID+data+CRC",
          ref["bytes"] == bytes([0xC3, 0xFF, 0x00, 0xA5, 0x5A,
                                 ref["crc"] & 0xFF, ref["crc"] >> 8]),
          ref["bytes"].hex())
    check("reference encoder: stuffing occurs for 0xFF payload",
          ref["stuff_count"] >= 1, f"stuff={ref['stuff_count']}")
    check("reference encoder: 8 SYNC + <=72 encoded bits",
          8 <= ref["encoded_bits"] <= 80, f"{ref['encoded_bits']} bits")
    # synthetic waveform: reference levels at 22.25 cycles/bit + idle tail
    period = 22.25
    transitions = []
    level = 0
    for k, lv in enumerate(ref["levels"]):
        t = int(round(k * period))
        if lv != level:
            transitions.append((t, lv))
            level = lv
    for k in range(len(ref["levels"]), len(ref["levels"]) + 4):
        pass
    # trailing idle transition is none; append a fake next packet start far away
    class _W:  # minimal world stand-in for the decoder
        clock_hz = 33_333_333
    dec = UsbLsDecoder.__new__(UsbLsDecoder)
    dec.world = _W()
    dec.dp_pin = DP_PIN
    dec.payload_bytes = PAYLOAD_BYTES
    dec.bit_period_nominal = _W.clock_hz / USB_LS_BIT_RATE
    dec.gap_cycles = dec.bit_period_nominal * 20
    packet = dec._decode(transitions)
    check("reference waveform decodes (SYNC/PID/CRC/EOP)",
          packet.sync_ok and packet.pid_ok and packet.crc_ok and packet.eop_ok
          and packet.payload == ref["payload"],
          f"pid=0x{packet.pid:02X} payload={packet.payload.hex()} "
          f"crc={'ok' if packet.crc_ok else 'BAD'} eop={packet.eop_ok}")
    check("recovered period == 22.25 cycles (+/-0.02)",
          abs(packet.period_cycles - period) < 0.02,
          f"{packet.period_cycles:.4f}")
    print("USB_LS_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

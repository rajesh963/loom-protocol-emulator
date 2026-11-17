#!/usr/bin/env python3
"""USB 1.1 Low-Speed host-side reference: differential encoder + device RX decoder.

Companion to the `usb_ls_tx.asm` stretch prototype and the (feasibility) RX
prototype.  This module is **host side + algorithm reference** for the
"enumerable USB device" stretch goal:

* `crc5_usb` / `crc16_usb` — USB token/data CRCs, validated against the
  published CRC-5/USB and CRC-16/USB check values.
* `encode_packet` / `synthetic_trace` — a reference host transmitter: builds
  token (SETUP/OUT/IN), DATA0/DATA1, ACK packets as a **differential**
  (D+, D-) NRZI waveform with bit stuffing, CRC, EOP and LS idle J.
* `UsbDeviceDecoder` — a reference device receiver: packet framing on EOP,
  **clock recovery/DPLL** seeded from the SYNC field (KJKJKJKK) and refined over
  every transition, NRZI decode, bit unstuffing, PID+complement, CRC5/CRC16 and
  SYNCH/EOP checks.  It is the *algorithm* a firmware RX path must implement,
  and the oracle for the golden-model firmware test.
* `UsbHostTransmitter` — the same encoder as a `WireWorld` far end, so a
  firmware-under-test sees a real differential line cycle by cycle.

Line states (LS): J = (D+,D-) = (0,1) = idle/1, K = (1,0) = 0, SE0 = (0,0)
(EOP), SE1 = (1,1) illegal.  NRZI: a 0 data bit toggles J<->K, a 1 holds.
Bit stuffing inserts a 0 after six consecutive 1s (SYNC is never stuffed; this
module stuffs every packet after SYNC, which is USB's rule except for SOF, and
SOF is not generated here).

Run `python -m host.usb_ls_rx` for the rung-1 feasibility self-test: CRC check
values, then a synthetic differential SETUP token + 8-byte DATA0 (a
GET_DESCRIPTOR request) decoded at 1.5 Mbit/s nominal and at +/-0.25 % host
clock offset, reporting recovered period, bit rate, DPLL residual and fields.
"""

from __future__ import annotations

import pathlib
import sys
from collections import deque

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sw.protocols import FarEnd, WireWorld, format_wave                 # noqa: E402
from host.usb_ls import crc16_usb, USB_LS_BIT_RATE                      # noqa: E402

__all__ = [
    "J", "K", "SE0", "SE1", "state_name",
    "crc5_usb", "token_crc5", "encode_packet", "synthetic_trace",
    "UsbDeviceDecoder", "UsbHostTransmitter", "UsbRxPacket",
    "DP_PIN", "DM_PIN", "PID_SETUP", "PID_OUT", "PID_IN", "PID_DATA0",
    "PID_DATA1", "PID_ACK",
]

DP_PIN = 0                      # uio[0] = D+
DM_PIN = 1                      # uio[1] = D-
BIT_PERIOD_124 = 356            # 22.25 cycles at 33.33 MHz (setp 0x0164)

J = (0, 1)                      # idle / 1
K = (1, 0)                      # 0
SE0 = (0, 0)                    # EOP
SE1 = (1, 1)                    # illegal

PID_SETUP = 0x2D
PID_OUT = 0xE1
PID_IN = 0x69
PID_DATA0 = 0xC3
PID_DATA1 = 0x4B
PID_ACK = 0xD2
TOKEN_PIDS = (PID_SETUP, PID_OUT, PID_IN)
DATA_PIDS = (PID_DATA0, PID_DATA1)
SYNC_BITS = [0, 0, 0, 0, 0, 0, 0, 1]


def state_name(state) -> str:
    return {J: "J", K: "K", SE0: "SE0", SE1: "SE1"}.get(tuple(state), str(state))


# ---------------------------------------------------------------------------
# CRCs
# ---------------------------------------------------------------------------
def crc5_usb(bits, xorout: int = 0x1F) -> int:
    """USB token CRC5 (poly x^5+x^2+1 reflected -> 0x14, init 0x1F, xorout 0x1F).

    `bits` are data bits in transmit order (LSB first).  Catalogue CRC-5/USB
    check value for "123456789" is 0x19.
    """
    crc = 0x1F
    for b in bits:
        crc = ((crc >> 1) ^ 0x14) if (crc ^ b) & 1 else (crc >> 1)
    return (crc ^ xorout) & 0x1F


def token_crc5(addr: int, endp: int) -> int:
    bits = [(addr >> i) & 1 for i in range(7)] + [(endp >> i) & 1 for i in range(4)]
    return crc5_usb(bits)


def _bits_of(value: int, nbits: int) -> list[int]:
    return [(value >> i) & 1 for i in range(nbits)]


def _value_of(bits) -> int:
    v = 0
    for i, b in enumerate(bits):
        v |= b << i
    return v


# ---------------------------------------------------------------------------
# Reference host encoder
# ---------------------------------------------------------------------------
def _stuff(bits: list[int]) -> tuple[list[int], int]:
    """Bit-stuff a decoded-bit list (after SYNC): a 0 after six consecutive 1s."""
    out: list[int] = []
    ones = 0
    stuffed = 0
    for b in bits:
        if ones == 6:
            out.append(0)
            stuffed += 1
            ones = 0
        out.append(b)
        ones = ones + 1 if b else 0
    return out, stuffed


def _nrz_states(bits, start=J) -> list[tuple[int, int]]:
    """NRZI-encode decoded bits into differential line states from `start`."""
    state = tuple(start)
    states = []
    for b in bits:
        if b == 0:
            state = K if state == J else J
        states.append(state)
    return states


def encode_packet(kind: str, *, pid: int | None = None, addr: int = 0,
                  endp: int = 0, payload: bytes = b"") -> dict:
    """Encode one LS packet: {'decoded_bits','states','bytes','stuff_count',...}."""
    kind = kind.lower()
    if kind == "token":
        pid = PID_SETUP if pid is None else pid
        addr &= 0x7F
        endp &= 0x0F
        crc5 = token_crc5(addr, endp)
        after = (_bits_of(pid, 8) + _bits_of(addr, 7) + _bits_of(endp, 4)
                 + _bits_of(crc5, 5))
        meta = {"type": "token", "pid": pid, "addr": addr, "endp": endp,
                "crc5": crc5}
    elif kind == "data":
        pid = PID_DATA0 if pid is None else pid
        payload = bytes(bytearray(payload))
        crc = crc16_usb(payload)
        after = _bits_of(pid, 8)
        for byte in payload:
            after += _bits_of(byte, 8)
        after += _bits_of(crc, 16)
        meta = {"type": "data", "pid": pid, "payload": payload, "crc16": crc}
    elif kind == "ack":
        pid = PID_ACK if pid is None else pid
        after = _bits_of(pid, 8)
        meta = {"type": "ack", "pid": pid}
    elif kind == "sof":
        # SOF is fixed: 11-bit frame number + CRC5, defined as NOT bit stuffed
        after = _bits_of(0xA5, 8) + _bits_of(0, 11) + _bits_of(token_crc5(0, 0), 5)
        meta = {"type": "sof", "pid": 0xA5}
    else:
        raise ValueError(f"unknown packet kind {kind!r}")

    decoded = list(SYNC_BITS)
    if kind == "sof":
        stuffed_bits, stuffed = after, 0      # SOF: no bit stuffing
    else:
        stuffed_bits, stuffed = _stuff(after)
    decoded += stuffed_bits
    meta.update(decoded_bits=decoded, states=_nrz_states(decoded),
                stuffed_bits=stuffed_bits, stuff_count=stuffed,
                encoded_bits=len(decoded))
    return meta


def _advance(period124: int, frac: list[int], scale: float = 1.0) -> int:
    p = int(round(period124 * scale))
    f = frac[0] + (p & 0xF)
    frac[0] = f & 0xF
    return (p >> 4) + (f >> 4)


def synthetic_trace(packets: list[dict], *, clock_hz: int = 33_333_333,
                    bit_period124: int = BIT_PERIOD_124,
                    clock_scale: float = 1.0,
                    idle_bits: int = 3) -> dict:
    """Build a differential transition list [(cycle, (dp,dm))] from packets.

    The line starts at idle J, each packet is preceded by `idle_bits` J cells,
    followed by EOP (SE0 for 2 cells) and `idle_bits` J cells.  Bit boundaries
    use the same 12.4 fractional carry as the firmware/host event scheduler.
    """
    segs: list[tuple[tuple[int, int], int]] = []      # (state, duration124)
    for _ in range(idle_bits):
        segs.append((J, bit_period124))
    for pkt in packets:
        for _ in range(idle_bits):
            segs.append((J, bit_period124))
        for st in pkt["states"]:
            segs.append((st, bit_period124))
        segs.append((SE0, bit_period124))
        segs.append((SE0, bit_period124))
        for _ in range(idle_bits):
            segs.append((J, bit_period124))

    transitions: list[tuple[int, tuple[int, int]]] = []
    cycle = 0
    frac = [0]
    prev = None
    for state, period in segs:
        if state != prev:
            transitions.append((cycle, tuple(state)))
            prev = state
        cycle += _advance(period, frac, clock_scale)
    return {"transitions": transitions, "cycles": cycle, "clock_hz": clock_hz}


# ---------------------------------------------------------------------------
# Reference device decoder (algorithm oracle)
# ---------------------------------------------------------------------------
class UsbRxPacket:
    """One decoded host->device packet."""

    def __init__(self) -> None:
        self.kind = ""                  # token | data | ack
        self.pid = 0
        self.pid_ok = False
        self.addr = None
        self.endp = None
        self.crc5 = None
        self.crc5_calc = None
        self.crc5_ok = None
        self.payload = b""
        self.crc16 = None
        self.crc16_calc = None
        self.crc16_ok = None
        self.sync_ok = False
        self.eop_ok = False
        self.stuff_count = 0
        self.period_cycles = 0.0
        self.bit_rate = 0.0
        self.clock_offset = 0.0
        self.grid_residual_max = 0.0
        self.samples: list[str] = []
        self.errors: list[str] = []

    @property
    def ok(self) -> bool:
        checks = [self.sync_ok, self.pid_ok, self.eop_ok, not self.errors]
        if self.kind == "token":
            checks.append(bool(self.crc5_ok))
        elif self.kind == "data":
            checks.append(bool(self.crc16_ok))
        return all(checks)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        extra = ""
        if self.kind == "token":
            extra = (f" addr={self.addr} endp={self.endp} "
                     f"crc5=0x{self.crc5:02X}({'ok' if self.crc5_ok else 'BAD'})")
        elif self.kind == "data":
            extra = (f" payload={self.payload.hex()} "
                     f"crc16=0x{self.crc16:04X}({'ok' if self.crc16_ok else 'BAD'})")
        return (f"UsbRxPacket({self.kind} pid=0x{self.pid:02X}{extra} "
                f"stuff={self.stuff_count} EOP={'ok' if self.eop_ok else 'BAD'} "
                f"P={self.period_cycles:.3f}cyc rate={self.bit_rate/1e6:.4f}Mbit/s "
                f"off={self.clock_offset*100:+.3f}% ok={self.ok})")


class UsbDeviceDecoder:
    """Reference LS device receiver: framing, DPLL, NRZI, unstuff, PID/CRC checks."""

    def __init__(self, *, clock_hz: int = 33_333_333,
                 nominal_period124: int = BIT_PERIOD_124):
        self.clock_hz = clock_hz
        self.nominal_period = nominal_period124 / 16.0
        self.packets: list[UsbRxPacket] = []
        self.errors: list[str] = []

    # -- packet segmentation --------------------------------------------
    @staticmethod
    def _level_at(transitions, cycle: float):
        state = J
        for c, st in transitions:
            if c <= cycle:
                state = st
            else:
                break
        return state

    def decode_stream(self, transitions) -> list[UsbRxPacket]:
        """Decode every packet in a differential transition list."""
        tr = sorted(transitions, key=lambda x: x[0])
        i = 0
        while i < len(tr):
            # find a J -> K transition: SYNC bit 0
            while i < len(tr) and not (tr[i][1] == K
                                       and (i == 0 or tr[i - 1][1] == J)):
                i += 1
            if i >= len(tr):
                break
            t0 = tr[i][0]
            pkt, consumed = self._decode_one(tr, i, t0)
            if pkt is not None:
                self.packets.append(pkt)
            i = consumed
        return self.packets

    def _decode_one(self, tr, start_idx: int, t0: int):
        nominal = self.nominal_period
        # --- pass 1: find EOP + collect edges with the nominal period
        states = []
        k = 0
        eop_at = None
        eop_count = 0
        while True:
            c = t0 + (k + 0.5) * nominal
            if c > tr[-1][0] + 4 * nominal:
                break
            st = self._level_at(tr, c)
            states.append(st)
            if st == SE0:
                eop_count += 1
                if eop_count == 2:
                    eop_at = k
                    break
            else:
                eop_count = 0
            k += 1
            if k > 140:                        # 8-byte data packet + margin
                break
        if eop_at is None:
            return None, start_idx + 1
        # --- pass 2: refine the bit period from every non-EOP transition
        edges = [c for c, st in tr if t0 <= c < t0 + eop_at * nominal]
        period = nominal
        if len(edges) >= 7:
            period = (edges[6] - edges[0]) / 6.0
            for _ in range(3):
                fits = [(round((c - t0) / period), c) for c in edges]
                mean_m = sum(m for m, _ in fits) / len(fits)
                mean_c = sum(c for _, c in fits) / len(fits)
                den = sum((m - mean_m) ** 2 for m, _ in fits)
                if den <= 0:
                    break
                period = (sum((m - mean_m) * (c - mean_c) for m, c in fits) / den)
        # --- pass 3: sample + NRZI decode until EOP
        pkt = UsbRxPacket()
        pkt.period_cycles = period
        pkt.bit_rate = self.clock_hz / period
        pkt.clock_offset = period / nominal - 1.0
        pkt.grid_residual_max = max(
            (abs(round((c - t0) / period) * period - (c - t0)) for c in edges),
            default=0.0)
        bits = []
        samples = []
        prev = J
        k = 0
        eop_seen = 0
        last_c = None
        while k < eop_at + 3:
            c = t0 + (k + 0.5) * period
            st = self._level_at(tr, c)
            samples.append(state_name(st))
            if st == SE0:
                eop_seen += 1
                pkt.eop_ok = eop_seen >= 2
                if eop_seen >= 2:
                    last_c = c
                    break
            else:
                if st not in (J, K):
                    pkt.errors.append("SE1 on the line")
                bits.append(0 if st != prev else 1)
                prev = st
            k += 1
        pkt.samples = samples
        if not pkt.eop_ok:
            pkt.errors.append("EOP not seen")
        # --- SYNC + PID + unstuff
        pkt.sync_ok = bits[:8] == SYNC_BITS
        if not pkt.sync_ok:
            pkt.errors.append(f"SYNC {bits[:8]}")
        unstuffed: list[int] = []
        ones = 0
        idx = 8
        while idx < len(bits):
            b = bits[idx]
            idx += 1
            if ones == 6:
                if b != 0:
                    pkt.errors.append("stuffing violation")
                pkt.stuff_count += 1
                ones = 0
                continue
            unstuffed.append(b)
            ones = ones + 1 if b else 0
        pid_bits = unstuffed[:8]
        pkt.pid = _value_of(pid_bits)
        pkt.pid_ok = (pkt.pid >> 4) == ((~pkt.pid) & 0x0F)
        if not pkt.pid_ok:
            pkt.errors.append(f"PID complement 0x{pkt.pid:02X}")
        rest = unstuffed[8:]
        pid_nib = pkt.pid & 0x0F
        if pid_nib in (0xD, 0x1, 0x9):                      # SETUP / OUT / IN
            pkt.kind = "token"
            if len(rest) < 16:
                pkt.errors.append("short token")
            else:
                pkt.addr = _value_of(rest[0:7])
                pkt.endp = _value_of(rest[7:11])
                pkt.crc5 = _value_of(rest[11:16])
                pkt.crc5_calc = crc5_usb(rest[0:11])
                pkt.crc5_ok = pkt.crc5 == pkt.crc5_calc
                if not pkt.crc5_ok:
                    pkt.errors.append(
                        f"CRC5 0x{pkt.crc5:02X} != 0x{pkt.crc5_calc:02X}")
        elif pid_nib in (0x3, 0xB):                         # DATA0 / DATA1
            pkt.kind = "data"
            n = len(rest) - 16          # rest already excludes the PID
            if n < 0 or n % 8:
                pkt.errors.append(f"bad data length ({len(rest)} bits)")
            else:
                nb = n // 8
                pkt.payload = bytes(
                    _value_of(rest[8 * i:8 * i + 8]) for i in range(nb))
                pkt.crc16 = _value_of(rest[8 * nb:8 * nb + 16])
                pkt.crc16_calc = crc16_usb(pkt.payload)
                pkt.crc16_ok = pkt.crc16 == pkt.crc16_calc
                if not pkt.crc16_ok:
                    pkt.errors.append(
                        f"CRC16 0x{pkt.crc16:04X} != 0x{pkt.crc16_calc:04X}")
        elif pid_nib == 0x2:                                # ACK
            pkt.kind = "ack"
        else:
            pkt.errors.append(f"unhandled PID 0x{pkt.pid:02X}")
        # resume scanning after the EOP cells
        consumed = start_idx + 1
        end = last_c if last_c is not None else t0 + eop_at * period
        while consumed < len(tr) and tr[consumed][0] <= end + period:
            consumed += 1
        return pkt, consumed

    # -- convenience -----------------------------------------------------
    def decode_trace(self, trace) -> list[UsbRxPacket]:
        return self.decode_stream(trace["transitions"])

    def summary(self) -> list[str]:
        return [repr(p) for p in self.packets]


# ---------------------------------------------------------------------------
# Host transmitter as a WireWorld far end
# ---------------------------------------------------------------------------
class UsbHostTransmitter(FarEnd):
    """Drives the differential D+/D- pins of a `WireWorld` from packet schedules."""

    name = "usb-ls-host-tx"

    def __init__(self, world: WireWorld, *, dp_pin: int = DP_PIN,
                 dm_pin: int = DM_PIN, bit_period124: int = BIT_PERIOD_124,
                 clock_scale: float = 1.0, idle_bits: int = 3):
        self.world = world
        self.dp_pin, self.dm_pin = int(dp_pin), int(dm_pin)
        self.bit_period124 = int(bit_period124)
        self.clock_scale = float(clock_scale)
        self.idle_bits = int(idle_bits)
        self._segs: deque[tuple[int, int, int]] = deque()   # (dp, dm, duration124)
        self._left = 0
        self._dp, self._dm = J
        self.frac = 0
        self._ready = False
        self._prev = J
        self.wave: list[tuple[int, int, int]] = []          # (cycle, dp, dm)
        world.attach(self)

    @property
    def ext_mask(self) -> int:                                     # type: ignore[override]
        return (1 << self.dp_pin) | (1 << self.dm_pin)

    @property
    def ext_value(self) -> int:                                    # type: ignore[override]
        return (self._dp << self.dp_pin) | (self._dm << self.dm_pin)

    # -- scheduling ------------------------------------------------------
    def _idle(self, bits: int | None = None) -> None:
        for _ in range(self.idle_bits if bits is None else bits):
            self._segs.append((J[0], J[1], self.bit_period124))

    def queue_packet(self, pkt: dict) -> None:
        self._idle()
        for st in pkt["states"]:
            self._segs.append((st[0], st[1], self.bit_period124))
        self._segs.append((SE0[0], SE0[1], self.bit_period124))
        self._segs.append((SE0[0], SE0[1], self.bit_period124))
        self._idle()

    def queue_setup(self, *, addr: int = 0, endp: int = 0,
                    payload: bytes = b"") -> None:
        self.queue_packet(encode_packet("token", pid=PID_SETUP,
                                        addr=addr, endp=endp))
        self._idle(1)
        self.queue_packet(encode_packet("data", pid=PID_DATA0, payload=payload))

    @property
    def busy(self) -> bool:
        return bool(self._segs) or self._left > 0

    # -- engine ----------------------------------------------------------
    def _next(self) -> None:
        if not self._segs:
            self._dp, self._dm = J
            self._left = 0
            return
        self._dp, self._dm, period = self._segs.popleft()
        f = self.frac + ((int(round(period * self.clock_scale))) & 0xF)
        self.frac = f & 0xF
        self._left = (int(round(period * self.clock_scale)) >> 4) + (f >> 4)

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        if not self._ready:
            self._ready = True
            self._next()
        if self._left > 0:
            self._left -= 1
        if self._left > 0:
            return
        self._next()
        if (self._dp, self._dm) != self._prev:
            self.wave.append((cycle, self._dp, self._dm))
            self._prev = (self._dp, self._dm)

    def waveform(self, chars: int = 96, start: int | None = None,
                 end: int | None = None) -> dict[str, str]:
        dptr = [(c, dp) for c, dp, _dm in self.wave]
        dmtr = [(c, dm) for c, _dp, dm in self.wave]
        return {"D+ P[0]": format_wave(dptr, chars=chars, start=start, end=end),
                "D- P[1]": format_wave(dmtr, chars=chars, start=start, end=end)}


# ---------------------------------------------------------------------------
# Self-test (rung-1 feasibility)
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    # CRC conventions
    check("CRC-16/USB check value 0xB4C8",
          crc16_usb(b"123456789") == 0xB4C8, hex(crc16_usb(b"123456789")))
    check("CRC-5/USB check value 0x19",
          crc5_usb([(b >> i) & 1 for b in b"123456789" for i in range(8)]) == 0x19,
          hex(crc5_usb([(b >> i) & 1 for b in b"123456789" for i in range(8)])))

    # A real enumeration request: GET_DESCRIPTOR(Device), 64 bytes, wIndex 0
    setup_data = bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x40, 0x00])
    packets = [encode_packet("token", pid=PID_SETUP, addr=0, endp=0),
               encode_packet("data", pid=PID_DATA0, payload=setup_data)]
    for scale, tag in ((1.0, "nominal"), (1.0025, "host +0.25%"),
                       (0.9975, "host -0.25%")):
        trace = synthetic_trace(packets, clock_scale=scale)
        dec = UsbDeviceDecoder(clock_hz=trace["clock_hz"])
        dec.decode_trace(trace)
        check(f"[{tag}] two packets decoded", len(dec.packets) == 2,
              f"{len(dec.packets)} packets, errors={dec.errors}")
        if len(dec.packets) != 2:
            continue
        tok, dat = dec.packets
        check(f"[{tag}] SETUP token pid/addr/endp/CRC5",
              tok.kind == "token" and tok.pid == PID_SETUP and tok.addr == 0
              and tok.endp == 0 and tok.crc5_ok,
              repr(tok))
        check(f"[{tag}] DATA0 payload + CRC16",
              dat.kind == "data" and dat.payload == setup_data and dat.crc16_ok,
              repr(dat))
        check(f"[{tag}] SYNCH/EOP/PID-complement ok",
              all(p.sync_ok and p.pid_ok and p.eop_ok for p in dec.packets),
              f"sync={[p.sync_ok for p in dec.packets]}")
        check(f"[{tag}] clock recovery within 0.02 cycle",
              abs(tok.period_cycles - 22.25 * scale) < 0.02,
              f"P={tok.period_cycles:.4f} cyc, predicted {22.25*scale:.4f}, "
              f"offset {tok.clock_offset*100:+.3f}%")
        check(f"[{tag}] edges on the bit grid (<=1.0 cyc residual)",
              max(tok.grid_residual_max, dat.grid_residual_max) <= 1.0,
              f"residuals {tok.grid_residual_max:.2f}/{dat.grid_residual_max:.2f}")
        check(f"[{tag}] stuffing counts", tok.stuff_count == 0,
              f"token stuff={tok.stuff_count}")
        print(f"        token: {tok}")
        print(f"        data : {dat}")
    print("USB_LS_RX_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

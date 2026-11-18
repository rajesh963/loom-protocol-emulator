#!/usr/bin/env python3
"""CAN 2.0A (standard 11-bit ID) far-end model + independent frame decoder.

Companion to `sw/examples/can_tx.asm`.  LOOM is the **CAN controller's
transmitter**: it drives a single TXD pin (recessive = 1, dominant = 0), the
external transceiver turns that into CANH/CANL, and the receiver on the bus
drives the ACK slot dominant through the transceiver back onto RXD, which the
firmware samples.

What is modelled
----------------
* `crc15_can` — the ISO 11898-1 CRC-15: generator 0x4599
  (x^15+x^14+x^10+x^8+x^7+x^4+x^3+1), register init 0, non-reflected, no
  final XOR.  Validated against the published CRC-15/CAN check value
  (``crc15_can(b"123456789") == 0x059E``) and against the "remainder is zero"
  receiver property: ``crc15_can(frame_bits + crc_bits_msb_first) == 0``.
* `build_frame_bits` — SOF, 11-bit ID (MSB first), RTR, IDE, r0, 4-bit DLC and
  DLC data bytes (MSB first), all in transmission order (dominant=0).
* `stuff_bits` — dynamic bit stuffing after five consecutive identical bits,
  applied to the SOF..end-of-CRC-sequence region only (the CRC delimiter, ACK
  and EOF are fixed-form and never stuffed).
* `CanFarEnd` — a `FarEnd` plugin: a cycle-level receiver that recovers the bit
  grid, samples/stuffs/destuffs the frame, checks the CRC, and drives the ACK
  slot dominant on RXD (the chip samples RXD and reports "ACK seen").
* `CanWireDecoder` — the pure protocol state machine behind it, usable from a
  synthetic level trace with no golden model; it reports the decoded frame, the
  stuffing positions, the fitted bit period and the edge residual.

Frame assumed (classical CAN, standard format, data frame):
    SOF | ID[10:0] | RTR=0 | IDE=0 | r0=0 | DLC[3:0] | data (8*DLC bits) |
    CRC15 (MSB first, stuffed) | CRC delimiter (1) | ACK slot (TX 1, RX 0) |
    ACK delimiter (1) | EOF 7 x 1 | IFS 3 x 1
Bit stuffing is applied to SOF..CRC sequence inclusive; delimiters are not
stuffed.  (The transmitter's 15th CRC bit never gets a trailing stuff bit
before the delimiter: the stuffed region ends with the CRC sequence.)

Run `python -m host.can` for the standalone self-check plus the golden-model
run of can_tx.asm.
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sw.protocols import FarEnd, WireWorld, format_wave        # noqa: E402

__all__ = [
    "CLOCK_HZ", "CAN_BAUD", "POLY", "TXD_PIN", "RXD_PIN",
    "crc15_can", "CanFrame", "DecodedFrame", "build_frame_bits",
    "crc_bits", "stuff_bits", "encode_frame", "pack_bit_words",
    "CanWireDecoder", "CanFarEnd", "assemble_example", "load_example",
    "run_example", "expected_ack", "check_decoded",
]

CLOCK_HZ = 33_333_333            # shipped LOOM design point (30 ns)
CAN_BAUD = 500_000               # 500 kbit/s
POLY = 0x4599                    # CAN CRC-15 generator (without the x^15 term)
TXD_PIN = 10                     # P[10] = uo[2]
RXD_PIN = 11                     # P[11] = ui[3]
STUFF_RUN = 5                    # insert a stuff bit after 5 identical bits
FRAME_HDR_BITS = 19              # SOF + ID + RTR + IDE + r0 + DLC
CRC_BITS = 15
DELIM_BITS = 1
EOF_BITS = 7
IFS_BITS = 3


# ---------------------------------------------------------------------------
# CRC-15 / CAN
# ---------------------------------------------------------------------------
def crc15_can(bits, crc: int = 0) -> int:
    """ISO 11898-1 CRC-15 over a sequence of 0/1 bits (transmission order).

    Register level, exactly the CAN 2.0 pseudocode::

        CRCNXT = NXTBIT ^ CRC_RG(14)
        CRC_RG = (CRC_RG << 1) & 0x7FFF
        if CRCNXT: CRC_RG ^= 0x4599

    ``crc15_can(bits_of(b"123456789")) == 0x059E`` (CRC-15/CAN check value).
    """
    crc &= 0x7FFF
    for b in bits:
        fb = ((crc >> 14) & 1) ^ (b & 1)
        crc = (crc << 1) & 0x7FFF
        if fb:
            crc ^= POLY
    return crc


def bits_msb(value: int, width: int) -> list[int]:
    """`width` bits of `value`, most significant first (CAN field order)."""
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def bits_value(bits) -> int:
    """Bit list in transmission order -> integer (first bit = LSB)."""
    v = 0
    for i, b in enumerate(bits):
        v |= (b & 1) << i
    return v


def field_value(bits) -> int:
    """CAN field bits (transmitted MSB first) -> integer."""
    v = 0
    for b in bits:
        v = (v << 1) | (b & 1)
    return v


def crc_check_vector() -> int:
    """CRC-15/CAN check value for the ASCII string ``b"123456789"``."""
    bits: list[int] = []
    for byte in b"123456789":
        bits += bits_msb(byte, 8)
    return crc15_can(bits)


# ---------------------------------------------------------------------------
# Frame construction / reference encoder
# ---------------------------------------------------------------------------
@dataclass
class CanFrame:
    """A classical CAN standard data frame (2.0A)."""

    ident: int = 0x123
    dlc: int = 8
    data: bytes = b"LOOMCAN!"
    rtr: int = 0          # 0 = data frame (dominant)
    ide: int = 0          # 0 = standard frame
    r0: int = 0           # reserved, dominant

    def __post_init__(self) -> None:
        self.ident = int(self.ident) & 0x7FF
        self.data = bytes(bytearray(self.data))
        if self.dlc is None:
            self.dlc = len(self.data)
        self.dlc = int(self.dlc)
        if not 0 <= self.dlc <= 8:
            raise ValueError("classical CAN DLC is 0..8")
        if len(self.data) > self.dlc:
            self.data = self.data[:self.dlc]
        self.data = self.data + bytes(self.dlc - len(self.data))


def build_frame_bits(frame: CanFrame) -> list[int]:
    """SOF..end-of-data bits in transmission order (dominant = 0)."""
    bits = [0]                                        # SOF dominant
    bits += bits_msb(frame.ident, 11)                 # ID, MSB first
    bits.append(frame.rtr & 1)                        # RTR (0 = data)
    bits.append(frame.ide & 1)                        # IDE (0 = standard)
    bits.append(frame.r0 & 1)                         # r0 (dominant)
    bits += bits_msb(frame.dlc, 4)                    # DLC
    for byte in frame.data:                           # data, MSB first
        bits += bits_msb(byte, 8)
    return bits


def crc_bits(crc: int) -> list[int]:
    """The 15-bit CRC sequence in transmission order (bit 14 first)."""
    return bits_msb(crc & 0x7FFF, CRC_BITS)


def stuff_bits(bits) -> tuple[list[int], list[int]]:
    """Apply CAN dynamic bit stuffing.

    Returns ``(stuffed_bits, stuff_positions)`` where ``stuff_positions[i]`` is
    the index (in the *unstuffed* stream) of the bit in front of which a stuff
    bit was inserted.
    """
    out: list[int] = []
    positions: list[int] = []
    run = 0
    last = None
    for i, b in enumerate(bits):
        b &= 1
        if run == STUFF_RUN:
            # complement of the five identical bits that just went out; this is
            # unconditional (the receiver destuffs deterministically)
            out.append(1 - (last & 1))
            positions.append(i)
            run = 0
            last = None
        out.append(b)
        if run == 0 or b != last:
            run = 1
        else:
            run += 1
        last = b
    return out, positions


def encode_frame(frame: CanFrame | None = None) -> dict:
    """Full reference encoding: fields, CRC, stuffed wire bits and counts."""
    frame = frame or CanFrame()
    frame_bits = build_frame_bits(frame)
    crc = crc15_can(frame_bits)
    crc_seq = crc_bits(crc)
    stuffed, positions = stuff_bits(frame_bits + crc_seq)
    # trailer: CRC delimiter, ACK slot (transmitter sends recessive), ACK
    # delimiter, 7 x EOF, 3 x IFS.  All recessive; not stuffed.
    trailer = [1] * (DELIM_BITS + 1 + DELIM_BITS + EOF_BITS + IFS_BITS)
    wire = list(stuffed) + trailer
    return {
        "frame": frame,
        "frame_bits": frame_bits,
        "crc": crc,
        "crc_bits": crc_seq,
        "stuffed_bits": stuffed,
        "stuff_positions": positions,
        "stuff_count": len(positions),
        "trailer_bits": trailer,
        "wire_bits": wire,
        "frame_bit_count": len(frame_bits),
        "crc_remainder": crc15_can(frame_bits + crc_seq),
    }


def pack_bit_words(bits) -> list[int]:
    """Pack a transmission-order bit stream into 16-bit DMEM words (LSB first).

    Word ``k`` holds bits ``16k .. 16k+15`` with the earliest bit in bit 0 --
    the layout `can_tx.asm` shifts out of DMEM[1..].
    """
    words: list[int] = []
    for base in range(0, len(bits), 16):
        word = 0
        for i, b in enumerate(bits[base:base + 16]):
            word |= (b & 1) << i
        words.append(word)
    return words


# ---------------------------------------------------------------------------
# Pure wire decoder (TXD level trace -> frame)
# ---------------------------------------------------------------------------
@dataclass
class DecodedFrame:
    """One decoded CAN frame plus its wire-level quality metrics."""

    ident: int = 0
    dlc: int = 0
    data: bytes = b""
    rtr: int = 0
    ide: int = 0
    r0: int = 0
    crc_rx: int = 0
    crc_ok: bool = False
    stuff_count: int = 0
    stuff_positions: list[int] = field(default_factory=list)
    sof_ok: bool = False
    crc_delimiter_ok: bool = False
    ack_slot_recessive: bool = False
    ack_delimiter_ok: bool = False
    eof_ok: bool = False
    bit_period: float = 0.0
    bit_rate: float = 0.0
    edge_residual_max: float = 0.0
    start_cycle: int = 0
    end_cycle: int = 0
    wire_bit_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.sof_ok and self.crc_ok and self.crc_delimiter_ok
                and self.ack_delimiter_ok and self.eof_ok and not self.errors)

    def text(self) -> str:
        return (f"ID=0x{self.ident:03X} DLC={self.dlc} "
                f"data={self.data.hex()} CRC=0x{self.crc_rx:04X} "
                f"{'ok' if self.crc_ok else 'BAD'} stuff={self.stuff_count}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"DecodedFrame({self.text()}, period={self.bit_period:.3f} cyc, "
                f"{self.bit_rate/1e3:.1f} kbit/s)")


class CanWireDecoder:
    """CAN receiver state machine over a per-cycle TXD level trace.

    Feed one ``step(cycle, level)`` per cycle.  The decoder recovers the bit
    grid from the SOF falling edge (nominal ``clock_hz/can_baud`` period),
    samples at bit centres, destuffs, validates the fields and CRC, and exposes
    the RXD level the far end must drive for the ACK slot.
    """

    def __init__(self, *, clock_hz: int = CLOCK_HZ, can_baud: int = CAN_BAUD):
        self.clock_hz = int(clock_hz)
        self.baud = int(can_baud)
        self.period_nominal = self.clock_hz / float(self.baud)
        self.frames: list[DecodedFrame] = []
        self.errors: list[str] = []
        self.wave: list[tuple[int, int]] = []
        self.rxd_drive: list[tuple[int, int]] = []   # (cycle, rxd level)
        self._reset_frame()
        self.prev_level: int | None = None
        self.state = "idle"

    # -- per-frame state -------------------------------------------------
    def _reset_frame(self) -> None:
        self.state = "idle"
        self.t0 = 0.0
        self.next_sample = 0.0
        self.edges: list[int] = []
        self.sampled: list[int] = []          # TDO-like: raw TXD samples
        self.unstuffed: list[int] = []
        self.stuff_positions: list[int] = []
        self.run = 0
        self.last = None
        self.dlc = None
        self.expected = None
        self.trailer: list[int] = []
        self.ack_start: float | None = None
        self.ack_end: float | None = None
        self.frame_start = 0

    # -- public ----------------------------------------------------------
    def step(self, cycle: int, level: int) -> None:
        level &= 1
        if self.prev_level is None:
            self.prev_level = level
            self.wave = [(cycle, level)]
            return
        if level != self.prev_level:
            self.wave.append((cycle, level))
            if self.state == "idle":
                if level == 0:                      # recessive -> dominant = SOF
                    self._begin(cycle)
            elif self.state in ("frame", "trailer"):
                self.edges.append(cycle)
        self.prev_level = level
        if self.state in ("frame", "trailer") and cycle >= self.next_sample:
            self._sample(cycle, level)

    def ack_level(self, cycle: int) -> int:
        """RXD level the far end should drive at `cycle` (0 = dominant ACK)."""
        if self.ack_start is not None and self.ack_end is not None:
            if self.ack_start <= cycle < self.ack_end:
                return 0
        return 1

    # -- sampling --------------------------------------------------------
    def _begin(self, cycle: int) -> None:
        self._reset_frame()                 # fresh per-frame accumulators
        self.state = "frame"
        self.t0 = float(cycle)
        self.frame_start = cycle
        self.edges = [cycle]
        self.next_sample = cycle + self.period_nominal / 2.0

    def _sample(self, cycle: int, level: int) -> None:
        self.sampled.append(level)
        if self.state == "frame":
            self._feed(level)
        else:
            self._feed_trailer(level)
        self.next_sample += self.period_nominal

    def _feed(self, bit: int) -> None:
        if self.run == STUFF_RUN:
            if bit == self.last:
                self.errors.append("stuff bit not complemented")
            self.stuff_positions.append(len(self.unstuffed))
            self.run = 0
            return
        self.unstuffed.append(bit)
        if self.run == 0 or bit != self.last:
            self.run = 1
        else:
            self.run += 1
        self.last = bit
        if len(self.unstuffed) == FRAME_HDR_BITS:
            self.dlc = field_value(self.unstuffed[15:19])
            if self.dlc > 8:
                self.errors.append(f"DLC {self.dlc} > 8")
                self.dlc = 8
            self.expected = FRAME_HDR_BITS + 8 * self.dlc + CRC_BITS
        if self.expected is not None and len(self.unstuffed) >= self.expected:
            # last CRC bit: the ACK slot is two bit cells after this sample
            t = self.next_sample
            self.ack_start = t + 1.5 * self.period_nominal
            self.ack_end = self.ack_start + self.period_nominal
            self.state = "trailer"

    def _feed_trailer(self, bit: int) -> None:
        self.trailer.append(bit)
        if len(self.trailer) == DELIM_BITS + 1 + DELIM_BITS + EOF_BITS:
            self._finish()

    # -- decode ----------------------------------------------------------
    def _finish(self) -> None:
        self.state = "idle"
        u = self.unstuffed
        f = DecodedFrame()
        f.wire_bit_count = len(self.sampled)
        f.stuff_count = len(self.stuff_positions)
        f.stuff_positions = list(self.stuff_positions)
        f.start_cycle = self.frame_start
        f.end_cycle = self.wave[-1][0] if self.wave else self.frame_start
        tr = self.trailer
        f.sof_ok = bool(u and u[0] == 0)
        f.crc_delimiter_ok = tr[0] == 1
        f.ack_slot_recessive = tr[1] == 1
        f.ack_delimiter_ok = tr[2] == 1
        f.eof_ok = all(b == 1 for b in tr[3:])
        if len(u) >= FRAME_HDR_BITS + CRC_BITS:
            f.ident = field_value(u[1:12])
            f.rtr = u[12]
            f.ide = u[13]
            f.r0 = u[14]
            f.dlc = field_value(u[15:19])
            f.data = bytes(field_value(u[19 + 8 * i:27 + 8 * i])
                           for i in range(f.dlc))
            f.crc_rx = field_value(u[-CRC_BITS:])
            f.crc_ok = crc15_can(u) == 0      # frame+CRC remainder property
        # bit period + edge grid residual (TXD edges sit on bit boundaries)
        if len(self.edges) >= 4:
            p0 = self.period_nominal
            idx = [round((t - self.t0) / p0) for t in self.edges]
            n = len(idx)
            mm = sum(idx) / n
            mt = sum(self.edges) / n
            den = sum((m - mm) ** 2 for m in idx)
            if den > 0:
                p = sum((m - mm) * (t - mt)
                        for m, t in zip(idx, self.edges)) / den
                f.bit_period = p
                f.bit_rate = self.clock_hz / p
                f.edge_residual_max = max(
                    abs((t - self.t0) - m * p)
                    for m, t in zip(idx, self.edges))
        for e in self.errors:
            f.errors.append(e)
        self.frames.append(f)

    # -- rendering -------------------------------------------------------
    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {"TXD": format_wave(self.wave, chars=chars)}

    def summary(self) -> list[str]:
        out = []
        for i, f in enumerate(self.frames):
            out.append(f"frame {i}: {f.text()} delivery="
                       f"{'ok' if f.ok else 'BAD'} "
                       f"period={f.bit_period:.3f} cyc "
                       f"({f.bit_rate/1e3:.2f} kbit/s) "
                       f"edge_residual<={f.edge_residual_max:.2f} cyc")
        return out


# ---------------------------------------------------------------------------
# FarEnd plugin: drives RXD during the ACK slot
# ---------------------------------------------------------------------------
class CanFarEnd(FarEnd):
    """CAN bus far end: decodes TXD and drives the ACK slot on RXD."""

    name = "can-bus"

    def __init__(self, world: WireWorld, *, txd_pin: int = TXD_PIN,
                 rxd_pin: int = RXD_PIN, clock_hz: int | None = None,
                 can_baud: int = CAN_BAUD):
        self.world = world
        self.txd_pin = int(txd_pin)
        self.rxd_pin = int(rxd_pin)
        hz = int(clock_hz if clock_hz is not None else world.clock_hz)
        self.decoder = CanWireDecoder(clock_hz=hz, can_baud=can_baud)
        self._rxd = 1
        self.ack_cycles: list[int] = []
        world.attach(self)

    @property
    def ext_mask(self) -> int:              # type: ignore[override]
        return 1 << self.rxd_pin

    @property
    def ext_value(self) -> int:             # type: ignore[override]
        return (self._rxd & 1) << self.rxd_pin

    def observe(self, cycle: int, pins_out16: int, pins_dir8: int) -> None:
        level = (pins_out16 >> self.txd_pin) & 1
        self.decoder.step(cycle, level)
        nxt = self.decoder.ack_level(cycle + 1)
        if nxt == 0 and self._rxd == 1:
            self.ack_cycles.append(cycle + 1)
        self._rxd = nxt

    # conveniences
    @property
    def frames(self) -> list[DecodedFrame]:
        return self.decoder.frames

    def flush(self) -> None:
        """Decode a frame whose trailer was captured (normal path does this)."""

    def waveform(self, chars: int = 96) -> dict[str, str]:
        return {"TXD": format_wave(self.decoder.wave, chars=chars)}

    def summary(self) -> list[str]:
        return self.decoder.summary()


# ---------------------------------------------------------------------------
# Golden-model driver: assemble can_tx.asm, preload the TX mailbox, decode
# ---------------------------------------------------------------------------
CAN_ASM = _REPO / "sw" / "examples" / "can_tx.asm"
VERBOSE = False


def assemble_example(path: pathlib.Path | None = None):
    from sw import loom_asm
    src = (path or CAN_ASM).read_text(encoding="utf-8")
    return loom_asm.assemble(src, filename="can_tx[model]")


def load_example(frame: CanFrame | None = None, *, base: int = 0, tid: int = 0,
                 clock_hz: int = CLOCK_HZ):
    """Assemble can_tx.asm, load the frame bits into the model's DMEM mailbox."""
    from sw import loom_model
    from host.loom_host import ModelLink
    enc = encode_frame(frame)
    prog = assemble_example()
    loom = loom_model.Loom()
    loom.load(prog.words, base)
    loom.set_pc(tid, base)
    # DMEM[0] = frame bit count; DMEM[1..] = packed SOF..data bits (LSB first)
    payload = [enc["frame_bit_count"]] + pack_bit_words(enc["frame_bits"])
    loom.load_dmem(payload, 0)
    link = ModelLink(loom)
    world = WireWorld(link, clock_hz=clock_hz)
    bus = CanFarEnd(world)
    loom.run_threads(1 << tid)
    return dict(prog=prog, enc=enc, loom=loom, link=link, world=world,
                bus=bus, tid=tid, frame=enc["frame"])


def run_example(ctx: dict, *, max_cycles: int = 40000, chunk: int = 64) -> dict:
    """Step until the decoder has a frame (or the firmware halts), then flush."""
    world = ctx["world"]
    bus = ctx["bus"]
    while world.cycle < max_cycles:
        world.run(chunk)
        if bus.frames:
            break
        if ctx["loom"].threads[ctx["tid"]].halt:
            break
    # the transmitter may have halted right at the last CRC bit: give the
    # decoder the remaining delimiter/EOF bit cells (all recessive)
    world.run(int(CLOCK_HZ / CAN_BAUD * 14) + 256)
    return ctx


def expected_ack() -> bool:
    return True


def check_decoded(bus: CanFarEnd, ctx: dict, check) -> None:
    """Shared CAN check list through a `check(name, ok, detail)` function."""
    frames = bus.frames
    check("can: frame captured from the TXD pin trace", len(frames) >= 1,
          f"{len(frames)} frames, errors={bus.decoder.errors}")
    if not frames:
        return
    dec, enc, frame = frames[0], ctx["enc"], ctx["frame"]
    check("can: SOF + 11-bit ID + RTR/IDE/r0 + DLC fields",
          dec.sof_ok and dec.ident == frame.ident and dec.rtr == 0
          and dec.ide == 0 and dec.r0 == 0 and dec.dlc == frame.dlc,
          f"SOF={dec.sof_ok} ID=0x{dec.ident:03X} DLC={dec.dlc}")
    check("can: data field byte-exact", dec.data == frame.data,
          f"got {dec.data.hex()} want {frame.data.hex()}")
    check("can: CRC-15 check value is 0x059E (CRC-15/CAN catalogue)",
          crc_check_vector() == 0x059E, hex(crc_check_vector()))
    check("can: independent CRC-15 remainder over frame+CRC == 0",
          dec.crc_ok and dec.crc_rx == enc["crc"],
          f"rx=0x{dec.crc_rx:04X} want=0x{enc['crc']:04X}")
    check(f"can: bit stuffing decoded ({dec.stuff_count} stuff bits), "
          f"positions match the reference encoder",
          dec.stuff_positions == enc["stuff_positions"]
          and dec.stuff_count == enc["stuff_count"],
          f"decoded={dec.stuff_positions} reference={enc['stuff_positions']}")
    check("can: CRC delimiter, ACK delimiter and 7-bit EOF all recessive",
          dec.crc_delimiter_ok and dec.ack_delimiter_ok and dec.eof_ok,
          f"delim={dec.crc_delimiter_ok} ackdelim={dec.ack_delimiter_ok} "
          f"eof={dec.eof_ok}")
    check("can: far end drove the ACK slot dominant on RXD",
          bool(bus.ack_cycles), f"ACK drive from cycle "
          f"{bus.ack_cycles[0] if bus.ack_cycles else None}")
    check("can: firmware reported ACK seen (host TX FIFO byte 1)",
          ctx.get("ack_reported") == 1,
          f"reported={ctx.get('ack_reported')}")
    check("can: bit period on the event grid",
          abs(dec.bit_period - round(CLOCK_HZ * 16 / CAN_BAUD) / 16) < 0.02
          and dec.edge_residual_max <= 1.0,
          f"{dec.bit_period:.4f} cyc/bit -> {dec.bit_rate/1e3:.2f} kbit/s, "
          f"edge residual <= {dec.edge_residual_max:.2f} cyc")


def _model_selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    for frame in (CanFrame(ident=0x123, dlc=8, data=b"LOOMCAN!"),
                  CanFrame(ident=0x7FF, dlc=0, data=b""),
                  CanFrame(ident=0x0AA, dlc=2, data=b"\xA5\x5A")):
        ctx = load_example(frame)
        run_example(ctx)
        ack = ctx["link"].rx_pop(ctx["tid"])
        ctx["ack_reported"] = ack
        check(f"can[{frame.ident:03X}/{frame.dlc}]: firmware ran (ACK byte "
              f"reported)", ack is not None, f"ack byte={ack}")
        check_decoded(ctx["bus"], ctx, check)
        if VERBOSE:
            for line in ctx["bus"].summary():
                print("       ", line)
    print("CAN_MODEL_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Standalone self-check: encoder + pure decoder on a synthetic level trace
# ---------------------------------------------------------------------------
def _synthetic_trace(enc: dict, *, period: float, t0: int = 100) -> list:
    """Per-cycle (cycle, txd level) for a reference frame at `period` cycles."""
    cycles: dict[int, int] = {c: 1 for c in range(0, t0)}   # idle recessive
    for k, bit in enumerate(enc["wire_bits"]):
        start = int(round(t0 + k * period))
        end = int(round(t0 + (k + 1) * period))
        for c in range(start, end):
            cycles[c] = bit
    last = max(cycles) if cycles else t0
    for c in range(last + 1, last + 20):
        cycles[c] = 1
    return sorted(cycles.items())


def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    check("CRC-15/CAN check value '123456789' -> 0x059E",
          crc_check_vector() == 0x059E, f"0x{crc_check_vector():04X}")

    # encoder/CRC/destuff self-consistency on a few frames
    for frame in (CanFrame(0x123, 8, b"LOOMCAN!"),
                  CanFrame(0x7FF, 0, b""),
                  CanFrame(0x0AA, 2, b"\xA5\x5A"),
                  CanFrame(0x000, 8, b"\xFF" * 8),      # stuffing stress
                  CanFrame(0x555, 8, b"\x00" * 8)):     # stuffing stress
        enc = encode_frame(frame)
        packed_ok = (len(pack_bit_words(enc["frame_bits"])) * 16
                     >= len(enc["frame_bits"]))
        check(f"encode 0x{frame.ident:03X}/DLC{frame.dlc}: CRC remainder 0 "
              f"and stuffing",
              enc["crc_remainder"] == 0 and packed_ok
              and enc["stuff_count"] >= 0,
              f"CRC=0x{enc['crc']:04X} stuff={enc['stuff_count']} "
              f"wire={len(enc['wire_bits'])} bits")

    # decode the synthetic trace with a *different* bit period than nominal
    period = (CLOCK_HZ * 16 / CAN_BAUD) / 16.0      # 66.6875 cycles
    frame = CanFrame(0x123, 8, b"LOOMCAN!")
    enc = encode_frame(frame)
    levels = _synthetic_trace(enc, period=period)
    dec = CanWireDecoder()
    ack_drives = []
    for cycle, level in levels:
        dec.step(cycle, level)
        if dec.ack_level(cycle + 1) == 0:
            ack_drives.append(cycle + 1)
    check("synthetic trace decodes one frame", len(dec.frames) == 1,
          f"{len(dec.frames)} frames, errors={dec.errors}")
    if dec.frames:
        f = dec.frames[0]
        check("synthetic: SOF field dominant",
              f.sof_ok, f"SOF={f.sof_ok}")
        check("synthetic: ID/DLC/data byte-exact",
              f.ident == 0x123 and f.dlc == 8 and f.data == b"LOOMCAN!",
              f.text())
        check("synthetic: CRC remainder ok", f.crc_ok, hex(f.crc_rx))
        check("synthetic: stuffing positions match reference",
              f.stuff_positions == enc["stuff_positions"],
              f"{f.stuff_positions} vs {enc['stuff_positions']}")
        check("synthetic: trailer/EOF ok",
              f.crc_delimiter_ok and f.ack_delimiter_ok and f.eof_ok,
              f"delim={f.crc_delimiter_ok} eof={f.eof_ok}")
        check("synthetic: recovered bit period 66.6875 +/- 0.02 cyc",
              abs(f.bit_period - period) < 0.02,
              f"{f.bit_period:.4f} cyc ({f.bit_rate/1e3:.2f} kbit/s)")
        check("synthetic: all TXD edges on the bit grid",
              f.edge_residual_max <= 0.75,
              f"max residual {f.edge_residual_max:.2f} cyc")
        check("synthetic: ACK slot driven dominant on RXD",
              bool(ack_drives), f"ACK drive at {ack_drives[:1]}")
    print("CAN_SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    VERBOSE = "-v" in argv or "--verbose" in argv
    rc = _selftest()
    rc |= _model_selftest()
    print("CAN_SELFTEST:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

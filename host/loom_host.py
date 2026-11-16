#!/usr/bin/env python3
"""LOOM host-side protocol client (owner: toolchain).

Transport-agnostic implementation of the LOOM SPI byte protocol from
`spec/ARCHITECTURE.md` §6 (and the Lead-blessed framing below).  The same code
drives
  (a) a real chip / a MicroPython SPI master  -> `Transport` over pins,
  (b) the cocotb RTL host bridge              -> `Transport` over SPI cosim,
  (c) the golden Python model                 -> `ModelTransport`.

Canonical framing (Lead decision 2026-11-16; hostbus implements exactly this)
-----------------------------------------------------------------------------
Every 8-bit SPI transfer simultaneously returns one byte:
  * CMD transfer   -> 0x80 accepted / 0x81 busy (never 0x00)
  * ARG transfer   -> 0x00
  * afterwards, read payloads appear one byte per extra transfer, LSB first
Multi-byte arguments are LSB first.  Per command:
    0x01 SET_ADDR   args a[23:0] (3B)            -> status on CMD transfer
    0x02 WRITE_MEM  args w[31:0] (4B)            -> status; addr++
    0x03 READ_MEM   no args                       -> 4B payload (next transfers)
    0x04 WRITE_CSR  args csr, d[15:0] (3B)        -> status
    0x05 READ_CSR   args csr                      -> 2B payload
    0x06 TX_PUSH    args t[1:0], d[7:0]           -> status (host -> thread RX)
    0x07 RX_POP     args t[1:0]                   -> 1B payload (0x00 if empty)
    0x08 SET_PC     args t[1:0], pc[15:0] (3B)    -> status
    0x09 RUN        args mask[3:0]                -> status
    0x0A STOP       args mask[3:0]                -> status
    0x0B STATUS     no args                       -> 4B, one thread byte per t
    0x0C ACK_IRQ    no args                       -> status
    0x0D PING       args 0x5A                     -> 0xA5 on the transfer after it

STATUS payload bytes are `{halt, en, tx_full, tx_empty, rx_full, rx_empty, 2'b00}`
(bit7=halt, bit6=en, bit5=tx_full, bit4=tx_empty, bit3=rx_full, bit2=rx_empty).
Host memory map: word addresses 0x000..0x3FF = PMEM, 0x400..0x43F = DMEM (a
DMEM read returns `{16'h0, word}`, a write takes the low 16 bits).  The address
pointer auto-increments after every WRITE_MEM/READ_MEM.

Classes
-------
    Transport              abstract: xfer(byte) -> byte (one full-duplex byte)
    ByteTransport          command framing over any Transport
    ModelTransport         chip side of the protocol over a sw.loom_model.Loom
    ModelLink              direct duck-typed model driver (no byte protocol),
                           for protocol firmware/helpers that just want the API
    HostProtocolError, HostBusyError

Example: loopback against the golden model
------------------------------------------
    from host.loom_host import ByteTransport, ModelTransport
    from sw.loom_model import Loom
    chip = ModelTransport(Loom())
    host = ByteTransport(chip)
    assert host.ping()
    host.write_mem(0, [0x02000000])      # halt
    host.set_pc(0, 0); host.run(0x1)

Semantic notes (host side)
--------------------------
  * `TX_PUSH` to a full RX FIFO is silently dropped by the bridge (the command
    is accepted on the CMD transfer, before the FIFO is consulted); poll
    TFIFO/STATUS before pushing, or treat push_rx() as best-effort.
  * `RX_POP` on an empty TX FIFO returns 0x00 and sets `last_rx_pop_empty`.
  * The client raises `HostBusyError` if the chip answers 0x81 on a CMD
    transfer; retry the whole command after polling STATUS.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any, Iterable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from sw import isa  # type: ignore
    from sw import loom_model  # type: ignore
else:
    from sw import isa
    from sw import loom_model

__all__ = [
    "Transport", "ByteTransport", "ModelTransport", "ModelLink",
    "HostProtocolError", "HostBusyError",
    "RESP_OK", "RESP_BUSY", "RESP_IDLE",
    "decode_status_byte", "STATUS_BITS",
]

RESP_OK = isa.RESP_OK            # 0x80
RESP_BUSY = isa.RESP_BUSY        # 0x81
RESP_IDLE = isa.RESP_IDLE        # 0x00
PING_SENTINEL = isa.PING_SENTINEL
PING_REPLY = isa.PING_REPLY

#: bit positions of the per-thread STATUS payload byte
STATUS_BITS = {
    "halt": 7, "en": 6, "tx_full": 5, "tx_empty": 4,
    "rx_full": 3, "rx_empty": 2,
}


def decode_status_byte(byte: int) -> dict[str, int]:
    """Decode one thread's STATUS payload byte into named flag bits."""
    return {name: (byte >> bit) & 1 for name, bit in STATUS_BITS.items()}


class HostProtocolError(RuntimeError):
    """The chip answered something other than 0x80 on a CMD transfer."""


class HostBusyError(HostProtocolError):
    """The chip answered 0x81 (busy); retry after polling."""


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------
class Transport:
    """Abstract full-duplex SPI byte transport (one byte in / one byte out)."""

    def xfer(self, byte: int) -> int:            # pragma: no cover - interface
        raise NotImplementedError


class ByteTransport:
    """LOOM command framing (`[CMD][ARG...]` + pipelined responses)."""

    def __init__(self, transport: Transport):
        if not hasattr(transport, "xfer"):
            raise TypeError("ByteTransport needs an object with xfer(byte)->byte")
        self.transport = transport
        self.last_status = RESP_IDLE
        self.last_arg_status = RESP_IDLE
        self.last_rx_pop_empty = False

    # -- low level -------------------------------------------------------
    def xfer(self, byte: int) -> int:
        return self.transport.xfer(int(byte) & 0xFF) & 0xFF

    def _command(self, name: str, args: Sequence[int] = (),
                 nresp: int = 0) -> list[int]:
        spec = isa.HOST_CMD_BY_NAME[name]
        if len(args) != spec.n_args:
            raise ValueError(f"{name}: expected {spec.n_args} arg byte(s), "
                             f"got {len(args)}")
        window = getattr(self.transport, "command_window", None)
        if window is not None:
            # hold H_CS_N low for the whole [CMD][ARG...][payload] group
            with window():
                return self._command_inner(spec, name, args, nresp)
        return self._command_inner(spec, name, args, nresp)

    def _command_inner(self, spec: "isa.HostCmdSpec", name: str,
                       args: Sequence[int], nresp: int) -> list[int]:
        status = self.xfer(spec.opcode)
        self.last_status = status
        if status == RESP_BUSY:
            raise HostBusyError(f"{name}: chip busy (0x81)")
        if status != RESP_OK:
            raise HostProtocolError(
                f"{name}: unexpected CMD status 0x{status:02X} (want 0x80)")
        for arg in args:
            resp = self.xfer(int(arg) & 0xFF)   # arg transfers return 0x00
            if resp != RESP_IDLE:
                # Some bridges keep status on arg transfers; accept but record.
                self.last_arg_status = resp
        return [self.xfer(RESP_IDLE) for _ in range(nresp)]

    # -- memory ----------------------------------------------------------
    def set_addr(self, addr: int) -> None:
        a = int(addr) & 0xFFFFFF
        self._command("SET_ADDR", (a & 0xFF, (a >> 8) & 0xFF, (a >> 16) & 0xFF))

    def write_mem(self, addr: int, words: int | Iterable[int]) -> None:
        """Set the address pointer, then write one or more 32-bit words (auto-inc)."""
        if isinstance(words, int):
            words = [words]
        words = [int(w) & 0xFFFFFFFF for w in words]
        if not words:
            return
        self.set_addr(addr)
        for w in words:
            self._command("WRITE_MEM",
                          (w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF,
                           (w >> 24) & 0xFF))

    def read_mem(self, addr: int, count: int = 1) -> list[int]:
        """Read `count` 32-bit words starting at `addr` (pointer auto-inc)."""
        self.set_addr(addr)
        out: list[int] = []
        for _ in range(int(count)):
            b = self._command("READ_MEM", (), nresp=4)
            out.append(b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24))
        return out

    # -- CSRs ------------------------------------------------------------
    def write_csr(self, addr: int, value: int) -> None:
        a, v = int(addr) & 0xFF, int(value) & 0xFFFF
        self._command("WRITE_CSR", (a, v & 0xFF, (v >> 8) & 0xFF))

    def read_csr(self, addr: int) -> int:
        lo, hi = self._command("READ_CSR", (int(addr) & 0xFF,), nresp=2)
        return lo | (hi << 8)

    def read_thread_csr(self, tid: int, n: int) -> int:
        return self.read_csr(isa.csr_host_addr(tid, n))

    def write_thread_csr(self, tid: int, n: int, value: int) -> None:
        self.write_csr(isa.csr_host_addr(tid, n), value)

    # -- threads ---------------------------------------------------------
    def set_pc(self, tid: int, pc: int) -> None:
        t, p = int(tid) & 0x3, int(pc) & 0xFFFF
        self._command("SET_PC", (t, p & 0xFF, (p >> 8) & 0xFF))

    def run(self, mask: int = 0xF) -> None:
        self._command("RUN", (int(mask) & 0xF,))

    def stop(self, mask: int = 0xF) -> None:
        self._command("STOP", (int(mask) & 0xF,))

    def status(self) -> list[int]:
        """Four per-thread STATUS bytes (thread 0 first)."""
        return self._command("STATUS", (), nresp=4)

    def status_flags(self) -> list[dict[str, int]]:
        return [decode_status_byte(b) for b in self.status()]

    def ack_irq(self) -> None:
        self._command("ACK_IRQ")

    def ping(self) -> bool:
        reply = self._command("PING", (PING_SENTINEL,), nresp=1)[0]
        return reply == PING_REPLY

    # -- FIFOs -----------------------------------------------------------
    def push_rx(self, tid: int, byte: int) -> bool:
        """Host -> thread `tid` RX FIFO (command TX_PUSH).  Best effort."""
        self._command("TX_PUSH", (int(tid) & 0x3, int(byte) & 0xFF))
        return True

    def pop_tx(self, tid: int) -> int | None:
        """Thread `tid` TX FIFO -> host (command RX_POP).  None if empty."""
        byte = self._command("RX_POP", (int(tid) & 0x3,), nresp=1)[0]
        self.last_rx_pop_empty = (byte == 0x00)
        return None if self.last_rx_pop_empty else byte


# ---------------------------------------------------------------------------
# Chip-side protocol over the golden model
# ---------------------------------------------------------------------------
class ModelTransport(Transport):
    """Chip side of the LOOM SPI byte protocol, backed by a `Loom` model.

    Emulates the real slave's transfer pipeline: during transfer `k` the chip
    drives the MISO byte it prepared from everything received up to transfer
    `k-1`, and the byte received in transfer `k` is processed at its end.  That
    is the only physically realisable interpretation, and it produces exactly
    the Lead-blessed host framing:

      transfer 0 (CMD)        -> 0x80 idle/accepted (or 0x81 if the chip was busy)
      ARG transfers           -> 0x00
      READ payload transfers  -> payload bytes, LSB first, on the following transfers

    The model's host operations (`csr_write`, memory pointer, FIFOs, RUN/STOP)
    are used, so the same host code works against model and silicon.
    """

    IDLE, ARGS, PAYLOAD = 0, 1, 2

    def __init__(self, loom: "loom_model.Loom | None" = None):
        self.loom = loom if loom is not None else loom_model.Loom()
        self.mem_ptr = 0
        self._state = self.IDLE
        self._spec: "isa.HostCmdSpec | None" = None
        self._args: list[int] = []
        self._payload: list[int] = []
        self._next_miso = RESP_OK        # MISO the chip drives on next transfer
        self._busy = False               # command FSM busy (reports 0x81)
        self.dropped_tx_pushes = 0

    # -- helpers ---------------------------------------------------------
    def _enqueue(self, bytes_: Iterable[int]) -> None:
        self._payload.extend(int(b) & 0xFF for b in bytes_)

    def _execute(self, spec: "isa.HostCmdSpec", args: list[int]) -> None:
        loom = self.loom
        p = self.mem_ptr
        if spec.name == "SET_ADDR":
            self.mem_ptr = args[0] | (args[1] << 8) | (args[2] << 16)
        elif spec.name == "WRITE_MEM":
            word = args[0] | (args[1] << 8) | (args[2] << 16) | (args[3] << 24)
            loom.host_write_mem(p, word)
            self.mem_ptr = (p + 1) & 0xFFFFFF
        elif spec.name == "READ_MEM":
            word = loom.host_read_mem(p) & 0xFFFFFFFF
            self.mem_ptr = (p + 1) & 0xFFFFFF
            self._enqueue([word & 0xFF, (word >> 8) & 0xFF,
                           (word >> 16) & 0xFF, (word >> 24) & 0xFF])
        elif spec.name == "WRITE_CSR":
            loom.csr_write(args[0], args[1] | (args[2] << 8))
        elif spec.name == "READ_CSR":
            v = loom.csr_read(args[0]) & 0xFFFF
            self._enqueue([v & 0xFF, (v >> 8) & 0xFF])
        elif spec.name == "TX_PUSH":
            if not loom.push_rx(args[0] & 0x3, args[1]):
                self.dropped_tx_pushes += 1
        elif spec.name == "RX_POP":
            byte = loom.pop_tx(args[0] & 0x3)
            self._enqueue([0x00 if byte is None else byte & 0xFF])
        elif spec.name == "SET_PC":
            loom.set_pc(args[0] & 0x3, args[1] | (args[2] << 8))
        elif spec.name == "RUN":
            loom.csr_write(isa.CSR_RUN, args[0] & 0xF)
        elif spec.name == "STOP":
            loom.csr_write(isa.CSR_STOP, args[0] & 0xF)
        elif spec.name == "STATUS":
            self._enqueue(self._status_bytes())
        elif spec.name == "ACK_IRQ":
            loom.ack_irq()
        elif spec.name == "PING":
            self._enqueue([PING_REPLY if args[0] == PING_SENTINEL else 0x00])
        else:  # pragma: no cover - unreachable (table-driven)
            raise AssertionError(spec.name)

    def _load_payload(self) -> None:
        """Present the first queued payload byte on the next transfer, if any."""
        if self._payload:
            self._next_miso = self._payload.pop(0)
            self._state = self.PAYLOAD
        else:
            self._next_miso = RESP_OK
            self._state = self.IDLE

    def _status_bytes(self) -> list[int]:
        out: list[int] = []
        for th in self.loom.threads:
            tx_full = 1 if th.tx_count >= loom_model.FIFO_DEPTH else 0
            tx_empty = 1 if th.tx_count == 0 else 0
            rx_full = 1 if th.rx_count >= loom_model.FIFO_DEPTH else 0
            rx_empty = 1 if th.rx_count == 0 else 0
            b = ((1 if th.halt else 0) << 7) | ((1 if th.en else 0) << 6) \
                | (tx_full << 5) | (tx_empty << 4) | (rx_full << 3) | (rx_empty << 2)
            out.append(b)
        return out

    # -- Transport API ---------------------------------------------------
    def xfer(self, byte: int) -> int:
        """Return the MISO byte for this transfer, then process the MOSI byte."""
        byte &= 0xFF
        out = self._next_miso
        if self._state == self.PAYLOAD:
            # host is clocking read payload bytes out; the MOSI byte is a dummy
            if self._payload:
                self._next_miso = self._payload.pop(0)
            else:
                self._state = self.IDLE
                self._next_miso = RESP_OK
            return out
        if self._state == self.ARGS:
            self._args.append(byte)
            spec = self._spec
            assert spec is not None
            if len(self._args) < spec.n_args:
                self._next_miso = RESP_IDLE
                return out
            args, self._args = self._args, []
            self._state = self.IDLE
            self._spec = None
            self._execute(spec, args)
            self._load_payload()
            return out
        # IDLE: this byte is a command
        if self._busy:
            self._next_miso = RESP_BUSY
            return out
        spec = isa.HOST_CMD_BY_OPCODE.get(byte)
        if spec is None:
            self._next_miso = RESP_BUSY
            return out
        if spec.n_args == 0:
            self._execute(spec, [])
            self._load_payload()
            return out
        self._spec = spec
        self._args = []
        self._state = self.ARGS
        self._next_miso = RESP_IDLE
        return out

    # -- convenience -----------------------------------------------------
    @property
    def next_miso(self) -> int:
        """MISO byte the chip will drive on the next transfer.

        A pin-level test double calls `xfer(byte)` at the end of a transfer and
        then reads this to know what to shift out next (physical slave timing).
        """
        return self._next_miso & 0xFF

    @property
    def irq_asserted(self) -> bool:
        return self.loom.irq_asserted


# ---------------------------------------------------------------------------
# Direct model link (no byte protocol) for protocol firmware / helpers
# ---------------------------------------------------------------------------
class ModelLink:
    """Duck-typed direct driver for the golden model.

    This is the surface `sw/protocols.py` (firmware) and protocol VIPs should
    use when they do not care about SPI framing: push/pop bytes, poke CSRs,
    set up and release threads, and step/run the model while driving pins.
    """

    def __init__(self, loom: "loom_model.Loom | None" = None, **kwargs: Any):
        self.loom = loom if loom is not None else loom_model.Loom(**kwargs)

    # -- program / thread control ---------------------------------------
    def load(self, words: Iterable[int], addr: int = 0) -> None:
        self.loom.load(words, addr)

    def load_dmem(self, words: Iterable[int], addr: int = 0) -> None:
        self.loom.load_dmem(words, addr)

    def set_pc(self, tid: int, pc: int) -> None:
        self.loom.set_pc(tid, pc)

    def run_threads(self, mask: int = 0xF) -> None:
        self.loom.run_threads(mask)

    def stop_threads(self, mask: int = 0xF) -> None:
        self.loom.stop_threads(mask)

    def ping(self) -> bool:
        return True

    # -- CSR / FIFO ------------------------------------------------------
    def write_csr(self, addr: int, value: int) -> bool:
        return self.loom.csr_write(addr, value)

    def read_csr(self, addr: int) -> int:
        return self.loom.csr_read(addr)

    def tx_push(self, tid: int, byte: int) -> bool:
        """Push one host byte into thread `tid`'s RX FIFO."""
        return self.loom.push_rx(tid, byte)

    def rx_pop(self, tid: int) -> int | None:
        """Pop one byte from thread `tid`'s TX FIFO (None if empty)."""
        return self.loom.pop_tx(tid)

    # aliases with the host-bridge names
    push_rx = tx_push
    pop_tx = rx_pop

    # -- pins / time -----------------------------------------------------
    def set_pins_in(self, value16: int) -> None:
        self.loom.set_pins_in(value16)

    def step(self) -> None:
        self.loom.step()

    def run(self, n: int) -> None:
        self.loom.run(n)

    def run_with_pins(self, n: int, fn) -> None:  # noqa: ANN001 - callback
        self.loom.run_with_pins(n, fn)

    @property
    def pins_out16(self) -> int:
        return self.loom.pins_out16

    @property
    def pins_dir8(self) -> int:
        return self.loom.pins_dir8

    @property
    def pins_in16(self) -> int:
        return self.loom.pins_in16

    @property
    def tmr16(self) -> int:
        return self.loom.tmr16

    @property
    def trace(self):
        return self.loom.trace

    def state_snapshot(self) -> dict[str, Any]:
        return self.loom.state_snapshot()

    def ack_irq(self) -> None:
        self.loom.ack_irq()


# ---------------------------------------------------------------------------
# Self-test: full host<->chip loopback against the model
# ---------------------------------------------------------------------------
def _selftest() -> int:
    errors: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
        if not cond:
            errors.append(name)

    loom = loom_model.Loom()
    chip = ModelTransport(loom)
    host = ByteTransport(chip)

    check("ping", host.ping())
    # program: thread 0 pushes 0x41,0x42 then halts
    from sw import loom_asm
    prog = loom_asm.assemble("""
        movi r0, 0x41
        push r0
        addi r0, 1
        push r0
        halt
    """, filename="<host-selftest>")
    host.write_mem(0, prog.words)
    back = host.read_mem(0, len(prog.words))
    check("write_mem/read_mem round-trip", back == prog.words,
          f"{len(back)} words")
    host.set_pc(0, 0)
    host.run(0x1)
    chip.loom.run(40)
    a = host.pop_tx(0)
    b = host.pop_tx(0)
    c = host.pop_tx(0)
    check("TX FIFO bytes via RX_POP", (a, b, c) == (0x41, 0x42, None),
          f"got {a},{b},{c}")
    # CSR round trip + thread bank addressing
    host.write_csr(0x40 + 16 * 0 + loom_model.CSR_TPERIOD, 0x1234)
    check("thread-bank CSR write/read", host.read_csr(0x40) == 0x1234)
    host.write_csr(0x10, 0x00AB)              # POUT
    check("global CSR POUT", host.read_csr(0x10) == 0x00AB)
    # FIFO: host pushes into thread RX, thread pulls
    prog2 = loom_asm.assemble("""
        pull r0
        halt
    """, filename="<host-selftest2>")
    host.write_mem(0x100, prog2.words)
    host.stop(0xF)
    host.set_pc(1, 0x100)
    check("push_rx to thread 1", host.push_rx(1, 0x5A))
    host.run(0x2)                              # mask thread 1 only
    chip.loom.run(20)
    check("thread pulled RX byte", chip.loom.reg(1, 0) == 0x5A,
          f"r0=0x{chip.loom.reg(1,0):02X}")
    # STATUS payload
    st = host.status_flags()
    check("STATUS 4 bytes", len(st) == 4 and st[1]["halt"] == 1,
          f"thread1={st[1]}")
    # ACK_IRQ path
    chip.loom.irq_code = 0x42
    host.ack_irq()
    check("ACK_IRQ clears", chip.loom.irq_code == 0)

    # ModelLink surface
    link = ModelLink(loom_model.Loom())
    link.load(loom_asm.assemble("movi r0, 7\nhalt", filename="<l>").words)
    link.run_threads(1)
    link.run(20)
    check("ModelLink direct API", link.loom.reg(0, 0) == 7 and link.ping())

    print("HOST_SELFTEST:", "PASS" if not errors else "FAIL")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

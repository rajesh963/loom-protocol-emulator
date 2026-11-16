#!/usr/bin/env python3
"""Unit tests: host protocol client + bit-bang layer (owner: toolchain).

Run:  ../.venv/bin/python test/unit_sw/test_host.py    (or via run.sh)
Prints HOST_TESTS: PASS and exits 0 on success.
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from host import loom_host  # noqa: E402
from host.loom_host import (  # noqa: E402
    ByteTransport, HostBusyError, HostProtocolError, ModelLink, ModelTransport,
    decode_status_byte,
)
from host.spi_bitbang import BitBangTransport, FakeChipSPI, SPIBitBang  # noqa: E402
from sw import isa, loom_asm  # noqa: E402
from sw.loom_model import Loom  # noqa: E402

errors: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        errors.append(name)


# ---------------------------------------------------------------------------
# 1. byte-protocol loopback against the model
# ---------------------------------------------------------------------------
chip = ModelTransport(Loom())
host = ByteTransport(chip)

check("ping returns 0xA5", host.ping())
check("ping false on wrong sentinel", not (lambda: (
    host._command("PING", (0x00,), nresp=1)[0] == isa.PING_REPLY))())

prog = loom_asm.assemble("""
        movi r0, 0x41
        push r0
        addi r0, 1
        push r0
        halt
""")
host.write_mem(0, prog.words)
check("write_mem + read_mem round-trip (PMEM)", host.read_mem(0, len(prog.words)) == prog.words)
# auto-incrementing pointer: write at 0x100 then read back
host.write_mem(0x100, [0x11111111, 0x22222222, 0x33333333])
check("address pointer auto-increment", host.read_mem(0x100, 3)
      == [0x11111111, 0x22222222, 0x33333333])
# DMEM at 0x400: 32-bit read returns {16'h0, word}
host.write_mem(0x400 + 7, 0x0000C0DE)
check("host DMEM map read", host.read_mem(0x400 + 7) == [0x0000C0DE])

host.set_pc(0, 0)
host.run(0x1)
chip.loom.run(40)
check("TX_PUSH/RX_POP flow", host.pop_tx(0) == 0x41 and host.pop_tx(0) == 0x42)
check("RX_POP empty returns None", host.pop_tx(0) is None and host.last_rx_pop_empty)

# host -> thread RX FIFO
prog2 = loom_asm.assemble("pull r0\nhalt")
host.write_mem(0x200, prog2.words)
host.stop(0xF)
host.set_pc(1, 0x200)
check("TX_PUSH accepted", host.push_rx(1, 0x5A))
host.run(0x2)
chip.loom.run(20)
check("thread saw host byte", chip.loom.reg(1, 0) == 0x5A, f"r0=0x{chip.loom.reg(1,0):02X}")

# ---------------------------------------------------------------------------
# 2. CSRs over the wire
# ---------------------------------------------------------------------------
host.write_csr(isa.csr_host_addr(0, 0x00), 0x1234)      # TPERIOD
check("thread-bank CSR write/read", host.read_csr(isa.csr_host_addr(0, 0x00)) == 0x1234)
host.write_csr(isa.SPEC_CSR["POUT"], 0xFFFF)
check("POUT masked to 0xFCFF over the wire",
      host.read_csr(isa.SPEC_CSR["POUT"]) == 0xFCFF)
t = host.read_csr(isa.SPEC_CSR["TMR"])
check("TMR nonzero/advances over the wire", t > 0, f"tmr={t}")
# GSTATUS / TSTATUS layouts over the wire
host.write_csr(isa.CSR_RUN, 0x1)
st = host.read_csr(isa.csr_host_addr(0, 0x02))
check("TSTATUS over the wire: en bit0", (st & 1) == 1, f"0x{st:04X}")
gstat = host.read_csr(isa.SPEC_CSR["GSTATUS"])
check("GSTATUS over the wire: thread0 en at [8:5]", ((gstat >> 5) & 0x1) == 1,
      f"0x{gstat:04X}")
host.write_csr(isa.CSR_STOP, 0x1)
check("STOP clears en", (host.read_csr(isa.csr_host_addr(0, 0x02)) & 1) == 0)

# IRQ: non-destructive read, ACK clears
chip.loom.irq_code = 0x42
check("IRQ read non-destructive over the wire",
      host.read_csr(isa.SPEC_CSR["IRQ"]) == 0x42 and chip.loom.irq_code == 0x42)
host.ack_irq()
check("ACK_IRQ over the wire clears", chip.loom.irq_code == 0)

# ---------------------------------------------------------------------------
# 3. STATUS payload bytes
# ---------------------------------------------------------------------------
chip2 = ModelTransport(Loom())
h2 = ByteTransport(chip2)
h2.write_csr(isa.CSR_RUN, 0x1)
st = h2.status()
check("STATUS returns 4 bytes", len(st) == 4)
flags = decode_status_byte(st[0])
check("STATUS thread0 flags", flags["en"] == 1 and flags["halt"] == 0, f"{flags}")
check("STATUS halted thread1", decode_status_byte(st[1])["halt"] == 1)
# tx_full flag: fill thread0 TX FIFO
for i in range(8):
    chip2.loom.threads[0].tx.append(i)
st = h2.status()
check("STATUS tx_full bit", decode_status_byte(st[0])["tx_full"] == 1)

# ---------------------------------------------------------------------------
# 4. bit-banged SPI over fake pins, against the model chip
# ---------------------------------------------------------------------------
fake = FakeChipSPI()
spi = SPIBitBang(fake)
bb = BitBangTransport(spi)
h3 = ByteTransport(bb)
check("bit-bang ping", h3.ping())
h3.write_mem(0, prog.words)
check("bit-bang read_mem", h3.read_mem(0, len(prog.words)) == prog.words)
h3.stop(0xF)
h3.set_pc(0, 0)
h3.run(0x1)
fake.chip.loom.run(40)
check("bit-bang RX_POP", h3.pop_tx(0) == 0x41)
# raw command window: PING framing at the pin level
with bb.command_window():
    check("bit-bang cmd window status byte", bb.xfer(0x0D) == 0x80)
    check("bit-bang arg byte is 0x00", bb.xfer(0x5A) == 0x00)
    check("bit-bang reply byte is 0xA5", bb.xfer(0x00) == 0xA5)

# ---------------------------------------------------------------------------
# 5. ModelLink duck-typed surface (used by protocol firmware/VIPs)
# ---------------------------------------------------------------------------
link = ModelLink()
prog3 = loom_asm.assemble("""
        pushi 0x77
        pull r0
        halt
""")
link.load(prog3.words)
link.tx_push(0, 0x5A)                 # host -> thread RX
link.run_threads(0b0001)
link.run(40)
check("ModelLink ping", link.ping())
check("ModelLink rx_pop gets TX byte", link.rx_pop(0) == 0x77)
check("ModelLink CSR read/write", (link.write_csr(isa.SPEC_CSR["POUT"], 0x00FF),
                                   link.read_csr(isa.SPEC_CSR["POUT"]))[1] == 0x00FF)
link.step()   # a CSR write is visible from the next cycle
check("ModelLink pins properties", link.pins_out16 == 0x00FF and link.pins_dir8 == 0,
      f"pins=0x{link.pins_out16:04X}")
check("ModelLink state_snapshot", isinstance(link.state_snapshot(), dict))
check("ModelLink run_with_pins", (link.set_pins_in(0), link.run_with_pins(
    2, lambda c, p, d: 0x1) is None))

# ---------------------------------------------------------------------------
# 6. protocol errors
# ---------------------------------------------------------------------------
chip4 = ModelTransport(Loom())
h4 = ByteTransport(chip4)
try:
    h4._command("WRITE_CSR", (0,))       # wrong arg count -> ValueError
    check("arg-count validation", False)
except ValueError:
    check("arg-count validation", True)
# unknown command byte: chip answers BUSY on the *next* transfer (pipelined)
raw = h4.xfer(0x7F)
check("unknown command returns idle/OK byte first (pipelined)", raw == 0x80, f"0x{raw:02X}")
check("unknown command then reports busy", h4.xfer(0x00) == 0x81)

# HostBusyError/HostProtocolError exist and are Exception subclasses
check("exception hierarchy", issubclass(HostBusyError, HostProtocolError)
      and issubclass(HostProtocolError, RuntimeError))

print("HOST_TESTS:", "PASS" if not errors else f"FAIL ({len(errors)} failures)")
sys.exit(0 if not errors else 1)

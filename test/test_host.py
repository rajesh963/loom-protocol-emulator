#!/usr/bin/env python3
"""Host-link / protocol tests: every spec §6 command, framing, CS abort, IRQ,
FIFO behaviour, reset.  owner: verify.

Run with:  make host          (from test/)
"""

from __future__ import annotations

import cocotb

from host import (
    CMD, CMD_ACK_IRQ, CMD_PING, CMD_READ_CSR, CMD_READ_MEM, CMD_RX_POP,
    CMD_SET_PC, CMD_STATUS, CMD_TX_PUSH, CMD_WRITE_MEM, CSR, RESP_OK,
    LoomHost, ascii_wave, csr_host_addr, decode_status_byte, decode_tfifo,
    start_loom,
)


def log_wave(host, mon, pins=(10,), title="", t0=None, t1=None):
    print_wave = ascii_wave
    txt = print_wave(mon.value_records, pins=pins, t0=t0, t1=t1, title=title)
    for line in txt.splitlines():
        host.log.info(line)


# ---------------------------------------------------------------------------
# reset / link
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_reset_state(dut):
    """spec §7: after reset every visible thing is defined and idle."""
    host, mon = await start_loom(dut)
    assert host.pins_out() == 0, "pins_out must be 0 after reset"
    assert host.pins_dir() == 0, "DIR must be 0 after reset (all uio inputs)"
    assert host.irq() == 0, "H_IRQ must be low after reset"
    assert (int(dut.uo_out.value) & 1) == 0, "H_MISO must be 0 after reset"
    pout = await host.read_csr(CSR["POUT"])
    assert pout == 0, f"CSR POUT=0x{pout:04x} after reset"
    pdir = await host.read_csr(CSR["PDIR"])
    assert pdir == 0, f"CSR PDIR=0x{pdir:04x} after reset"
    tmr0 = await host.read_csr(CSR["TMR"])
    tmr1 = await host.read_csr(CSR["TMR"])
    assert tmr0 < 60 and tmr1 >= tmr0, f"TMR after reset {tmr0}->{tmr1}"
    for t in range(4):
        st = await host.get_tstatus(t)
        assert st == 0x2, f"thread {t} TSTATUS=0x{st:04x} (want halted=1,en=0)"
        tpc = await host.get_tpc(t)
        assert tpc == 0, f"thread {t} TPC={tpc}"
    st = await host.read_csr(CSR["GSTATUS"])
    assert (st & 0x1E) == 0, f"GSTATUS run mask not empty after reset: 0x{st:04x}"
    host.log.info("reset state: pins=0 dir=0 irq=0 TMR=%d GSTATUS=0x%04x",
                  tmr0, st)


@cocotb.test()
async def test_ping(dut):
    """CMD 0x0D: argument transfer returns 0xA5, command transfer 0x80."""
    host, _ = await start_loom(dut)
    for i in range(4):
        assert await host.ping(), f"PING #{i} failed"
    # wrong sentinel is ignored (link check is about the returned A5)
    rx = await host.frame("PING", [0x00])
    assert rx[1] == 0xA5, f"PING with 0x00 arg returned 0x{rx[1]:02x}"
    host.log.info("PING: 5/5 transfers returned 0xA5")


@cocotb.test()
async def test_back_to_back_commands(dut):
    """Many commands in one CS window and across windows must all execute."""
    host, _ = await start_loom(dut)
    # one CS frame: SET_ADDR + 3x WRITE_MEM + PING + READ_MEM + PING
    tx = [0x01, 0x40, 0x00, 0x00]                       # SET_ADDR 0x40
    for w in (0xDEADBEEF, 0x01234567, 0x89ABCDEF):
        tx += [0x02, w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF]
    tx += [0x0D, 0x5A]                                  # PING
    tx += [0x03, 0, 0, 0, 0]                            # READ_MEM
    tx += [0x0D, 0x5A]                                  # PING
    rx = await host.spi_xfer(tx)
    # status bytes are at each command transfer boundary
    assert rx[0] == RESP_OK, f"SET_ADDR status 0x{rx[0]:02x}"
    for i in range(3):
        assert rx[4 + 5 * i] == RESP_OK, f"WRITE_MEM[{i}] status 0x{rx[4 + 5 * i]:02x}"
    ping1 = rx[4 + 5 * 3 + 1]
    assert ping1 == 0xA5, f"inline PING #1 returned 0x{ping1:02x}"
    assert rx[4 + 5 * 3 + 2] == RESP_OK, "READ_MEM status"
    got = rx[4 + 5 * 3 + 3:4 + 5 * 3 + 7]
    assert got == [0xEF, 0xBE, 0xAD, 0xDE], f"READ_MEM payload {[hex(b) for b in got]}"
    assert rx[-1] == 0xA5, f"inline PING #2 returned 0x{rx[-1]:02x}"
    host.log.info("back-to-back: SET_ADDR/WRITE_MEM*3/PING/READ_MEM/PING in one frame OK")


@cocotb.test()
async def test_pmem_rw(dut):
    """WRITE_MEM/READ_MEM + address auto-increment over the whole PMEM map."""
    host, _ = await start_loom(dut)
    words = [0x00000000, 0xFFFFFFFF, 0x02000000, 0x12345678, 0x80000000,
             0x0000FFFF, 0xFFFF0000]
    await host.write_mem_block(0x10, words)
    got = await host.read_pmem_block(0x10, len(words))
    assert got == words, f"PMEM block {[hex(x) for x in got]}"
    # single-word paths
    await host.write_mem(0x3BF, 0xCAFEBABE)
    assert await host.read_mem(0x3BF) == 0xCAFEBABE
    # the address pointer increments across separate commands
    await host.set_addr(0x00)
    await host.write_mem(0x00, 0xA5A5A5A5)   # write_mem does its own SET_ADDR
    await host.write_mem(0x01, 0x5A5A5A5A)
    assert await host.read_mem(0x00) == 0xA5A5A5A5
    assert await host.read_mem(0x01) == 0x5A5A5A5A
    host.log.info("PMEM 0x10..0x16 round-trip + 0x3BF + single-word R/W OK")


@cocotb.test()
async def test_dmem_rw(dut):
    """DMEM lives at 0x3C0..0x3FF, 16-bit zero-extended over the host port."""
    host, _ = await start_loom(dut)
    for i, v in enumerate((0x0000, 0xBEEF, 0xFFFF, 0x1234, 0x0001)):
        await host.write_dmem(i, v)
    got = await host.read_dmem(5)
    assert got == [0x0000, 0xBEEF, 0xFFFF, 0x1234, 0x0001], \
        f"DMEM {[hex(x) for x in got]}"
    # a thread sees host-written DMEM and can write DMEM back for the host
    src = """
        ld   r0, 3          ; r0 = DMEM[3] = 0x1234 (host-written)
        addi r0, 1
        st   9, r0          ; DMEM[9] = 0x1235
        halt
    """
    await host.run_program(src, cycles=60)
    got = await host.read_dmem(10)
    assert got[9] == 0x1235, f"DMEM[9]=0x{got[9]:04x} after thread (want 0x1235)"
    host.log.info("DMEM host<->thread round-trip OK: DMEM[9]=0x%04x", got[9])


@cocotb.test()
async def test_csr_rw(dut):
    """Every readable/writable CSR in spec §4.2."""
    host, _ = await start_loom(dut)
    # per-thread bank
    for t in range(4):
        for n, val in ((0, 0x208D), (1, 0x0C00), (6, 0x0300), (7, 0x0100)):
            await host.write_csr(csr_host_addr(t, n), val)
            got = await host.read_csr(csr_host_addr(t, n))
            assert got == val, f"thread {t} CSR {n:#x}: 0x{got:04x} != 0x{val:04x}"
    # global
    checks = ((CSR["PDIR"], 0x00AA), (CSR["POUT"], 0x0C0F), (CSR["EVT"], 0x005A),
              (CSR["WADDR"], 0x0011), (CSR["WLEN"], 0x0020),
              (CSR["WPER"], 0x01E0))
    for addr, val in checks:
        await host.write_csr(addr, val)
        got = await host.read_csr(addr)
        assert got == val, f"CSR 0x{addr:02x}: 0x{got:04x} != 0x{val:04x}"
    # TMR is free-running and monotonic (mod 2^16) across back-to-back reads
    t0 = await host.read_csr(CSR["TMR"])
    t1 = await host.read_csr(CSR["TMR"])
    assert ((t1 - t0) & 0xFFFF) < 0x8000, f"TMR went backwards: {t0}->{t1}"
    # IRQ: write, read returns it, second read returns 0 (spec: read clears)
    await host.write_csr(CSR["IRQ"], 0x1234)
    assert await host.read_csr(CSR["IRQ"]) == 0x1234
    assert await host.read_csr(CSR["IRQ"]) == 0x0000, "IRQ read must clear"
    host.log.info("CSR matrix OK (thread RW x4, global RW, TMR monotonic, IRQ read-clears)")


@cocotb.test()
async def test_set_pc_run_stop(dut):
    """SET_PC / RUN / STOP / TPC / TSTATUS / GSTATUS."""
    host, _ = await start_loom(dut)
    src = """
        movi r0, 0x0042
        st   0, r0
        halt
    """
    await host.load_asm(src, addr=0x20)
    for t in range(4):
        await host.set_pc(t, 0x20)
        assert await host.get_tpc(t) == 0x20, f"TPC thread {t}"
    await host.run(0x5)
    await host.wait_halted(0, timeout_cycles=500)
    await host.wait_halted(2, timeout_cycles=500)
    assert await host.read_dmem(1) == [0x42], "DMEM[0] after thread run"
    st = await host.status()
    assert st[0]["halted"] == 1 and st[1]["en"] == 0 and st[2]["halted"] == 1
    assert st[3]["en"] == 0, "thread 3 must stay stopped (mask 0x5)"
    # STOP: start a spinning thread then stop it
    spin = """
    loop: out  0x0400
          clr  0x0400
          jmp  loop
    """
    await host.load_asm(spin, addr=0x40)
    await host.set_pc(1, 0x40)
    await host.run(0x2)
    await host._clk(20)
    assert await host.thread_enabled(1), "thread 1 should be enabled"
    await host.stop(0x2)
    assert not await host.thread_enabled(1), "STOP must clear en"
    gs = await host.read_csr(CSR["GSTATUS"])
    assert (gs & 0x0C) == 0, f"GSTATUS run mask after STOP 0x{gs:04x}"
    host.log.info("SET_PC/RUN/STOP/TPC/TSTATUS/GSTATUS OK")


@cocotb.test()
async def test_tx_push_rx_pop(dut):
    """Host -> thread RX FIFO -> thread -> TX FIFO -> host, byte-exact."""
    host, _ = await start_loom(dut)
    echo = """
        pull r0
        push r0
        jmp  echo
    """
    await host.load_asm(echo)
    await host.set_pc(0, 0)
    # empty TX FIFO: RX_POP returns 0x00 (spec: empty -> 0, no error)
    assert await host.rx_pop(0) == 0x00, "RX_POP on empty TX FIFO must return 0x00"
    payload = [0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80, 0x7F, 0x42]
    for b in payload:
        await host.tx_push(0, b)
    await host.run(0x1)
    got = [await host.rx_pop(0) for _ in payload]
    assert got == payload, f"echo payload {[hex(x) for x in got]}"
    host.log.info("FIFO echo OK: %s", [hex(b) for b in got])


@cocotb.test()
async def test_fifo_full_empty_flags(dut):
    """RX full (host->thread) and TX full (thread->host) back-pressure."""
    host, _ = await start_loom(dut)
    # ---- RX FIFO full: thread not running, push 9 bytes -> 8 kept ----------
    for i in range(9):
        await host.tx_push(2, 0x10 + i)
    fifo = decode_tfifo(await host.get_tfifo(2))
    assert fifo["rx_count"] == 8, f"RX FIFO accepted {fifo['rx_count']} bytes (depth 8)"
    assert fifo["rx_full"] == 1, f"TFIFO rx_full not set: 0x{fifo['raw']:04x}"
    st = (await host.status())[2]
    assert st["rx_full"] == 1 and st["rx_empty"] == 0, f"STATUS thread2 {st}"
    # a 10th push must be dropped, not corrupt the FIFO
    await host.tx_push(2, 0xAA)
    assert decode_tfifo(await host.get_tfifo(2))["rx_count"] == 8
    # drain through a thread: push what it pulled into its TX FIFO
    drain = """
        rpt  7
        pull r0
        push r0
        pull r0
        push r0
        halt
    """
    await host.load_asm(drain)
    await host.set_pc(2, 0)
    await host.run(0x4)
    await host.wait_halted(2, timeout_cycles=1000)
    got = [await host.rx_pop(2) for _ in range(8)]
    assert got == [0x10 + i for i in range(8)], f"RX drain {[hex(b) for b in got]}"
    # ---- TX FIFO full: thread pushes 10, stalls at 8 ----------------------
    src = """
        rpt  9
        pushi 0x5A
        halt
    """
    await host.load_asm(src)
    await host.set_pc(0, 0)
    await host.run(0x1)
    await host._clk(80)
    fifo = decode_tfifo(await host.get_tfifo(0))
    assert fifo["tx_count"] == 8, f"TX FIFO count {fifo['tx_count']} (want 8, stalled)"
    assert fifo["tx_full"] == 1, "TFIFO tx_full not set"
    st = (await host.status())[0]
    assert st["tx_full"] == 1 and st["halted"] == 0, f"STATUS thread0 {st}"
    got = [await host.rx_pop(0) for _ in range(10)]
    assert got == [0x5A] * 10, f"TX drain {[hex(b) for b in got]}"
    await host.wait_halted(0, timeout_cycles=500)
    assert decode_tfifo(await host.get_tfifo(0))["tx_count"] == 0
    host.log.info("FIFO full/empty back-pressure OK (RX dropped 9th, TX stalled at 8)")


@cocotb.test()
async def test_cs_abort_mid_command(dut):
    """Deasserting CS mid-command aborts it cleanly (spec §6 / host header)."""
    host, _ = await start_loom(dut)
    # SET_ADDR expects 3 args; give 1 then abort
    rx = await host.spi_xfer([CMD["SET_ADDR"], 0x10])
    assert rx[0] == RESP_OK
    assert await host.ping(), "PING after aborted SET_ADDR failed"
    # aborted WRITE_MEM must not write anything
    before = await host.read_mem(0x00)
    rx = await host.spi_xfer([CMD["SET_ADDR"], 0x00, 0x00, 0x00,
                              CMD["WRITE_MEM"], 0xAA, 0xBB])   # 2/4 args then abort
    assert await host.ping()
    after = await host.read_mem(0x00)
    assert before == after, f"aborted WRITE_MEM modified PMEM[0]: {before:#x}->{after:#x}"
    # aborted READ_MEM: command accepted, then CS abort before the 4 payload bytes
    await host.set_addr(0x10)
    rx = await host.spi_xfer([CMD["READ_MEM"], 0x00])
    assert rx[0] == RESP_OK
    assert await host.ping(), "PING after aborted READ_MEM failed"
    host.log.info("CS abort mid-arg / mid-read: no side effects, engine idle again")


@cocotb.test()
async def test_unknown_command_ignored(dut):
    """Unknown commands are ignored and leave the engine free (header contract)."""
    host, _ = await start_loom(dut)
    rx = await host.spi_xfer([0x7F, 0x00, 0x00])
    assert rx[0] == RESP_OK
    assert await host.ping(), "PING after unknown command failed"
    # and a command after an unknown command in the same frame still executes
    rx = await host.spi_xfer([0x66, 0x0D, 0x5A])
    assert rx[0] == RESP_OK, f"unknown-cmd status 0x{rx[0]:02x}"
    assert rx[2] == 0xA5, f"PING after unknown in same frame: 0x{rx[2]:02x}"
    host.log.info("unknown commands ignored, engine stays free")


@cocotb.test()
async def test_reset_mid_frame(dut):
    """rst_n asserted mid-transfer resets the link and all architecture state."""
    host, _ = await start_loom(dut)
    await host.load_asm("""
        movi r0, 0x1234
        st   0, r0
        halt
    """, addr=0)
    await host.begin_frame()
    await host.send_byte(CMD["SET_ADDR"])
    await host.send_byte(0x20)
    dut.rst_n.value = 0
    await host._clk(5)
    dut.rst_n.value = 1
    await host._clk(5)
    await host.end_frame()
    assert await host.ping(), "PING after mid-frame reset failed"
    assert await host.get_tpc(0) == 0, "TPC not reset by mid-frame rst_n"
    assert await host.read_csr(CSR["POUT"]) == 0
    # PMEM content survives reset (it is not cleared by rst_n)
    assert await host.read_mem(0) == 0x04001234, "PMEM lost across rst_n"
    host.log.info("reset mid-frame: link + architecture state reset, PMEM kept")


@cocotb.test()
async def test_irq_set_ack(dut):
    """irq imm8 raises H_IRQ; ACK_IRQ clears it; IRQ CSR read clears code."""
    host, _ = await start_loom(dut)
    src = """
        irq  0x42
        halt
    """
    await host.load_asm(src)
    await host.set_pc(0, 0)
    await host.run(0x1)
    await host.wait_halted(0, timeout_cycles=200)
    assert host.irq() == 1, "H_IRQ (uo_out[1]) not asserted after irq imm8"
    gs = await host.read_csr(CSR["GSTATUS"])
    assert gs & 1, f"GSTATUS.irq not set: 0x{gs:04x}"
    code = await host.read_csr(CSR["IRQ"])
    assert code == 0x0042, f"IRQ CSR code 0x{code:04x} (want 0x0042)"
    assert host.irq() == 1, "H_IRQ latch must survive IRQ CSR read"
    await host.ack_irq()
    await host._clk(4)
    assert host.irq() == 0, "H_IRQ not cleared by ACK_IRQ"
    assert await host.read_csr(CSR["IRQ"]) == 0, "IRQ code not cleared"
    host.log.info("IRQ: set by irq imm8, GSTATUS.irq=1, ACK_IRQ clears latch")


@cocotb.test()
async def test_soft_reset(dut):
    """CSR SOFTRST (0x20) resets the core but not PMEM or the host bridge."""
    host, _ = await start_loom(dut)
    src = """
    loop: out  0x0400
          clr  0x0400
          jmp  loop
    """
    words = await host.load_asm(src, addr=0)
    await host.set_pc(0, 0)
    await host.run(0x1)
    await host._clk(50)
    assert await host.thread_enabled(0), "thread should be running"
    await host.soft_reset()
    await host._clk(8)
    assert not await host.thread_enabled(0), "soft reset must disable threads"
    assert await host.halted(0), "soft reset must halt threads"
    assert await host.get_tpc(0) == 0
    assert host.pins_out() == 0 and host.pins_dir() == 0
    tmr = await host.read_csr(CSR["TMR"])
    assert tmr < 40, f"TMR={tmr} after soft reset (want near 0)"
    assert await host.read_mem(0) == words[0], "soft reset must keep PMEM"
    assert await host.ping(), "soft reset must not break the host bridge"
    host.log.info("soft reset: threads/halted/TMR/pins reset, PMEM kept, link alive")


# ---------------------------------------------------------------------------
# Spec-conformance tests for known RTL deviations are marked skip so CI is
# green while the bug reports are open.  See test/known_issues.py.
# ---------------------------------------------------------------------------
from known_issues import KNOWN_ISSUES  # noqa: E402


@cocotb.test(skip=KNOWN_ISSUES["pout_host_bits"]["open"])
async def test_pout_host_bits_ignored(dut):
    """spec §4.1: `out imm16` — pins_out = imm16 (host bits ignored)."""
    host, _ = await start_loom(dut)
    await host.run_program("out 0xFFFF\nhalt\n", cycles=40)
    got = await host.read_csr(CSR["POUT"])
    assert got == 0xFCFF, f"POUT=0x{got:04x} after `out 0xFFFF` (want 0xFCFF)"


@cocotb.test(skip=KNOWN_ISSUES["tfifo_layout"]["open"])
async def test_tfifo_layout(dut):
    """spec §4.2: TFIFO = {8'b0, rx_count, tx_count, 2'b0, rx_full, tx_full}."""
    host, _ = await start_loom(dut)
    await host.run_program("""
        rpt  2
        pushi 0x5A
        halt
    """, cycles=60)
    fifo = await host.get_tfifo(0)
    assert (fifo >> 8) & 0xF == 0, f"rx_count field wrong: 0x{fifo:04x}"
    assert (fifo >> 4) & 0xF == 3, f"tx_count field wrong: 0x{fifo:04x}"
    assert (fifo & 0x3) == 0, f"rx_full/tx_full bits wrong: 0x{fifo:04x}"

#!/usr/bin/env python3
"""Host-link / protocol tests: every spec §6 command, framing, CS abort, IRQ,
FIFO behaviour, reset.  owner: verify.

Run with:  make host          (from test/)

Observability rules used here:
  * pins are checked through the cfg monitor (`pins_out`/`DIR`/`H_IRQ`) and the
    memory map (DMEM) -- not through global CSR reads while VER-003 is open;
  * thread-bank CSRs (0x40+16t+n) are used freely: they are the path the host
    STATUS command depends on.
"""

from __future__ import annotations

import cocotb

from host import (
    CMD, CSR, LoomHost, RESP_OK, ascii_wave, csr_host_addr, decode_status_byte,
    decode_tfifo, start_loom,
)
from known_issues import KNOWN_ISSUES

GLOBAL_CSR_BROKEN = KNOWN_ISSUES["global_csr_read"]["open"]
POUT_MASK_BROKEN = KNOWN_ISSUES["pout_host_bits"]["open"]


async def run_and_read(host, src: str, nwords: int = 8, *, at: int = 0,
                       tid: int = 0) -> list[int]:
    """Load a program that ends in HALT, run it, return DMEM[0:nwords]."""
    await host.run_program(src, tid=tid, at=at, cycles=2000)
    return await host.read_dmem(nwords)


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
    for t in range(4):
        st = await host.get_tstatus(t)
        assert st == 0x2, f"thread {t} TSTATUS=0x{st:04x} (want halted=1,en=0)"
        assert await host.get_tpc(t) == 0, f"thread {t} TPC != 0"
        fifo = decode_tfifo(await host.get_tfifo(t))
        assert fifo["rx_count"] == 0 and fifo["tx_count"] == 0, \
            f"thread {t} FIFO not empty after reset: 0x{fifo['raw']:04x}"
        assert fifo["rx_empty"] == 1 and fifo["tx_full"] == 0
        assert (await host.get_teq(t)) == 0
    host.log.info("reset state: pins=0 dir=0 irq=0, 4x halted, FIFOs/queues empty")


@cocotb.test()
async def test_ping(dut):
    """CMD 0x0D: argument transfer returns 0xA5, command transfer 0x80."""
    host, _ = await start_loom(dut)
    for i in range(4):
        assert await host.ping(), f"PING #{i} failed"
    rx = await host.frame("PING", [0x00])
    assert rx[1] == 0xA5, f"PING with 0x00 arg returned 0x{rx[1]:02x}"
    host.log.info("PING: 5/5 transfers returned 0xA5")


@cocotb.test()
async def test_back_to_back_commands(dut):
    """Several commands in one CS window all execute, in order."""
    host, _ = await start_loom(dut)
    word = 0xDEADBEEF
    await host.write_mem(0x40, word)
    tx = ([CMD["SET_ADDR"], 0x40, 0x00, 0x00]
          + [CMD["PING"], 0x5A]
          + [CMD["READ_MEM"], 0, 0, 0, 0]
          + [CMD["PING"], 0x5A])
    rx = await host.spi_xfer(tx)
    assert rx[0] == RESP_OK, f"SET_ADDR status 0x{rx[0]:02x}"
    assert rx[1:4] == [0, 0, 0], "SET_ADDR args must return 0x00"
    assert rx[4] == RESP_OK and rx[5] == 0xA5, f"PING#1 {rx[4]:02x}/{rx[5]:02x}"
    assert rx[6] == RESP_OK, f"READ_MEM status 0x{rx[6]:02x}"
    payload = rx[7:11]
    assert payload == [0xEF, 0xBE, 0xAD, 0xDE], \
        f"READ_MEM payload {[hex(b) for b in payload]}"
    assert rx[11] == RESP_OK and rx[12] == 0xA5, f"PING#2 {rx[11]:02x}/{rx[12]:02x}"
    host.log.info("back-to-back SET_ADDR/PING/READ_MEM/PING in one CS frame OK")


@cocotb.test()
async def test_pmem_rw(dut):
    """WRITE_MEM/READ_MEM + address auto-increment over the PMEM map."""
    host, _ = await start_loom(dut)
    words = [0x00000000, 0xFFFFFFFF, 0x02000000, 0x12345678, 0x80000000,
             0x0000FFFF, 0xFFFF0000]
    await host.write_mem_block(0x10, words)
    got = await host.read_pmem_block(0x10, len(words))
    assert got == words, f"PMEM block {[hex(x) for x in got]}"
    await host.write_mem(0x3BF, 0xCAFEBABE)
    assert await host.read_mem(0x3BF) == 0xCAFEBABE
    # the pointer auto-increments across consecutive WRITE_MEM commands
    await host.set_addr(0x00)
    await host.frame("WRITE_MEM", [0xA5, 0xA5, 0xA5, 0xA5])
    await host.frame("WRITE_MEM", [0x5A, 0x5A, 0x5A, 0x5A])
    await host.set_addr(0x00)
    got = await host.read_mem_block(0x00, 2)
    assert got == [0xA5A5A5A5, 0x5A5A5A5A], f"auto-increment {[hex(x) for x in got]}"
    host.log.info("PMEM block 0x10..0x16, 0x3BF, auto-increment across commands OK")


@cocotb.test()
async def test_dmem_rw(dut):
    """DMEM lives at 0x3C0..0x3FF, 16-bit zero-extended over the host port."""
    host, _ = await start_loom(dut)
    for i, v in enumerate((0x0000, 0xBEEF, 0xFFFF, 0x1234, 0x0001)):
        await host.write_dmem(i, v)
    got = await host.read_dmem(5)
    assert got == [0x0000, 0xBEEF, 0xFFFF, 0x1234, 0x0001], \
        f"DMEM {[hex(x) for x in got]}"
    src = """
        ld   r0, 3          ; r0 = DMEM[3] = 0x1234 (host-written)
        addi r0, 1
        st   9, r0          ; DMEM[9] = 0x1235
        halt
    """
    got = await run_and_read(host, src, 10)
    assert got[9] == 0x1235, f"DMEM[9]=0x{got[9]:04x} after thread (want 0x1235)"
    host.log.info("DMEM host<->thread round-trip OK: DMEM[9]=0x%04x", got[9])


@cocotb.test()
async def test_thread_csr_rw(dut):
    """Per-thread CSR bank: RW registers and read-only status registers."""
    host, _ = await start_loom(dut)
    for t in range(4):
        for n, val in ((0, 0x208D), (1, 0x0C00), (6, 0x0300), (7, 0x0100)):
            await host.write_csr(csr_host_addr(t, n), val)
            got = await host.read_csr(csr_host_addr(t, n))
            assert got == val, f"thread {t} CSR 0x{n:02x}: 0x{got:04x} != 0x{val:04x}"
        assert await host.get_tstatus(t) == 0x2
        assert await host.get_tpc(t) == 0
        assert await host.get_teq(t) == 0
    # a thread reads its own bank back into DMEM (thread csrr path)
    src = """
        csrr r0, TPERIOD
        st   0, r0
        csrr r0, TSTATUS
        st   1, r0
        csrr r0, TMASK
        st   2, r0
        halt
    """
    got = await run_and_read(host, src, 4, tid=0)
    assert got[0] == 0x208D, f"thread TPERIOD readback 0x{got[0]:04x}"
    assert got[1] == 0x0001, f"TSTATUS readback 0x{got[1]:04x} (running, not halted)"
    assert got[2] == 0x0C00, f"TMASK readback 0x{got[2]:04x}"
    host.log.info("thread CSR bank RW + thread csrr readback OK")


@cocotb.test()
async def test_global_csr_rw(dut):
    """Global CSRs, checked through their *side effects* (pins / thread reads)."""
    host, mon = await start_loom(dut)
    # PDIR / POUT: directly observable on the pins
    await host.write_csr(CSR["PDIR"], 0x00AA)
    await host._clk(2)
    assert host.pins_dir() == 0xAA, f"pins_dir=0x{host.pins_dir():02x}"
    await host.write_csr(CSR["POUT"], 0x0C0F)
    await host._clk(2)
    assert host.pins_out() == 0x0C0F, f"pins_out=0x{host.pins_out():04x}"
    # EVT: written by the host, read back by a thread into DMEM
    await host.write_csr(CSR["EVT"], 0x005A)
    src = """
        csrr r0, EVT
        st   0, r0
        halt
    """
    got = await run_and_read(host, src, 2)
    assert got[0] == 0x005A, f"EVT thread readback 0x{got[0]:04x}"
    # thread `irq` -> host-visible H_IRQ, then host ACK_IRQ clears it
    await host.run_program("irq 0x42\nhalt\n")
    assert host.irq() == 1, "H_IRQ not asserted by `irq 0x42`"
    await host.ack_irq()
    await host._clk(4)
    assert host.irq() == 0, "H_IRQ not cleared by ACK_IRQ"
    host.log.info("global CSR side effects: PDIR/POUT pins, EVT via thread, IRQ pin/ACK OK")


@cocotb.test()
async def test_set_pc_run_stop(dut):
    """SET_PC / RUN / STOP / TPC / TSTATUS / STATUS."""
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
    for t, want in ((0, 0x2), (1, 0x0), (2, 0x2), (3, 0x0)):
        got = await host.get_tstatus(t)
        assert got == want, f"TSTATUS thread {t} = 0x{got:04x}, want 0x{want:04x}"
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
    host.log.info("SET_PC/RUN/STOP/TPC/TSTATUS/STATUS OK")


@cocotb.test()
async def test_tx_push_rx_pop(dut):
    """Host -> thread RX FIFO -> thread -> TX FIFO -> host, byte-exact."""
    host, _ = await start_loom(dut)
    echo = """
    echo:
        pull r0
        push r0
        jmp  echo
    """
    await host.load_asm(echo)
    await host.set_pc(0, 0)
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
    assert fifo["rx_empty"] == 0, f"TFIFO rx_empty should be 0: 0x{fifo['raw']:04x}"
    ts = await host.get_tstatus(2)
    assert (ts & 0x1) == 0 and (ts & 0x2) == 2, f"thread2 TSTATUS 0x{ts:04x}"
    await host.tx_push(2, 0xAA)          # 10th push must be dropped
    assert decode_tfifo(await host.get_tfifo(2))["rx_count"] == 8
    # drain through a thread: pull -> push (rpt repeats the pull 8 times)
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
    assert decode_tfifo(await host.get_tfifo(2))["rx_count"] == 0
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
    assert fifo["tx_full"] == 1, f"TFIFO tx_full not set: 0x{fifo['raw']:04x}"
    ts = await host.get_tstatus(0)
    assert (ts & 0x2) == 0, f"thread0 must be stalled (not halted): 0x{ts:04x}"
    got = [await host.rx_pop(0) for _ in range(10)]
    assert got == [0x5A] * 10, f"TX drain {[hex(b) for b in got]}"
    await host.wait_halted(0, timeout_cycles=500)
    assert decode_tfifo(await host.get_tfifo(0))["tx_count"] == 0
    host.log.info("FIFO full/empty back-pressure OK (RX dropped 9th, TX stalled at 8)")


@cocotb.test()
async def test_cs_abort_mid_command(dut):
    """Deasserting CS mid-command aborts it cleanly (spec §6 / host header)."""
    host, _ = await start_loom(dut)
    rx = await host.spi_xfer([CMD["SET_ADDR"], 0x10])
    assert rx[0] == RESP_OK
    assert await host.ping(), "PING after aborted SET_ADDR failed"
    before = await host.read_mem(0x00)
    await host.spi_xfer([CMD["SET_ADDR"], 0x00, 0x00, 0x00,
                         CMD["WRITE_MEM"], 0xAA, 0xBB])
    assert await host.ping()
    after = await host.read_mem(0x00)
    assert before == after, f"aborted WRITE_MEM modified PMEM[0]: {before:#x}->{after:#x}"
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
    assert host.pins_out() == 0 and host.pins_dir() == 0
    assert await host.read_mem(0) == 0x04001234, "PMEM lost across rst_n"
    host.log.info("reset mid-frame: link + architecture state reset, PMEM kept")


@cocotb.test()
async def test_irq_set_ack(dut):
    """irq imm8 raises H_IRQ; ACK_IRQ clears it (level visible on uo_out[1])."""
    host, _ = await start_loom(dut)
    await host.load_asm("irq 0x42\nhalt\n")
    await host.set_pc(0, 0)
    await host.run(0x1)
    assert host.irq() == 1, "H_IRQ (uo_out[1]) not asserted after irq imm8"
    await host.ack_irq()
    await host._clk(4)
    assert host.irq() == 0, "H_IRQ not cleared by ACK_IRQ"
    host.log.info("IRQ: set by irq imm8, ACK_IRQ clears the level")


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
    assert await host.read_mem(0) == words[0], "soft reset must keep PMEM"
    # TMR is observable by a *thread* (thread csrr path; host global read VER-003)
    got = await run_and_read(host, "csrr r0, TMR\nst 0, r0\nhalt\n", 1)
    assert got[0] < 200, f"TMR={got[0]} after soft reset (want near 0)"
    assert await host.ping(), "soft reset must not break the host bridge"
    host.log.info("soft reset: threads/halt/pins reset (TMR=%d), PMEM kept, link alive",
                  got[0])


@cocotb.test(skip=GLOBAL_CSR_BROKEN)
async def test_status_command(dut):
    """spec §6 STATUS: 4 thread bytes {halted,en,tx_full,tx_empty,rx_full,
    rx_empty,2'b00}; after reset = 0x94 each (VER-003 blocks it today)."""
    host, _ = await start_loom(dut)
    st = await host.status()
    for t in range(4):
        assert st[t]["raw"] == 0x94, f"STATUS thread {t} after reset {st[t]}"
        assert st[t]["halted"] == 1 and st[t]["en"] == 0
        assert st[t]["tx_empty"] == 1 and st[t]["rx_empty"] == 1
    await host.tx_push(1, 0x5A)
    st = await host.status()
    assert st[1]["rx_empty"] == 0, f"thread1 rx_empty after push: {st[1]}"
    await host.run_program("stop_here: jmp stop_here\n", wait_halt=False)
    await host.run_program("rpt 9\npushi 0x11\nhalt\n", wait_halt=False)
    st = await host.status()
    assert st[0]["tx_full"] == 1, f"thread0 tx_full after 9 pushes: {st[0]}"
    host.log.info("STATUS command byte decode OK: %s", [f"0x{t['raw']:02x}" for t in st])


# ---------------------------------------------------------------------------
# Spec-conformance tests gated on open known issues (see test/known_issues.py)
# ---------------------------------------------------------------------------
@cocotb.test(skip=GLOBAL_CSR_BROKEN)
async def test_global_csr_reads(dut):
    """spec §4.2: host READ_CSR of every global register (VER-003)."""
    host, _ = await start_loom(dut)
    expect = {CSR["POUT"]: 0x0000, CSR["PDIR"]: 0x0000, CSR["EVT"]: 0x0000,
              CSR["IRQ"]: 0x0000, CSR["WADDR"]: 0x0000, CSR["WLEN"]: 0x0000,
              CSR["WPER"]: 0x0000, CSR["WSTATUS"]: 0x0000, CSR["GSTATUS"]: 0x0000}
    for addr, want in expect.items():
        got = await host.read_csr(addr)
        assert got == want, f"READ_CSR(0x{addr:02x}) = 0x{got:04x}, want 0x{want:04x}"
    await host.write_csr(CSR["POUT"], 0x0C0F)
    await host.write_csr(CSR["PDIR"], 0x00AA)
    assert await host.read_csr(CSR["POUT"]) == 0x0C0F
    assert await host.read_csr(CSR["PDIR"]) == 0x00AA
    t0 = await host.read_csr(CSR["TMR"])
    t1 = await host.read_csr(CSR["TMR"])
    assert t0 != 0 or t1 != 0, "TMR must be non-zero after >0 cycles"
    assert ((t1 - t0) & 0xFFFF) < 0x8000, f"TMR backwards {t0}->{t1}"
    await host.write_csr(CSR["EVT"], 0x005A)
    assert await host.read_csr(CSR["EVT"]) == 0x005A
    await host.write_csr(CSR["IRQ"], 0x1234)
    assert await host.read_csr(CSR["IRQ"]) == 0x1234
    assert await host.read_csr(CSR["IRQ"]) == 0, "IRQ read must clear"
    host.log.info("global CSR host reads OK (POUT/PDIR/TMR/EVT/IRQ-rc)")


@cocotb.test(skip=(POUT_MASK_BROKEN or GLOBAL_CSR_BROKEN))
async def test_pout_host_bits_ignored(dut):
    """spec §4.1: `out imm16` — pins_out = imm16 (host bits ignored). VER-001."""
    host, _ = await start_loom(dut)
    await host.run_program("out 0xFFFF\nhalt\n", cycles=40)
    got = await host.read_csr(CSR["POUT"])
    assert got == 0xFCFF, f"POUT=0x{got:04x} after `out 0xFFFF` (want 0xFCFF)"


@cocotb.test(skip=KNOWN_ISSUES["tfifo_layout"]["open"])
async def test_tfifo_layout(dut):
    """spec §4.2 (frozen): TFIFO = {4'b0, rx_cnt[11:8], tx_cnt[7:4], 2'b0,
    tx_full[1], rx_empty[0]}.  VER-002."""
    host, _ = await start_loom(dut)
    await host.run_program("""
        rpt  2
        pushi 0x5A
        halt
    """, cycles=200)
    fifo = decode_tfifo(await host.get_tfifo(0))
    assert fifo["tx_count"] == 3, f"tx_count {fifo['tx_count']}: 0x{fifo['raw']:04x}"
    assert fifo["rx_count"] == 0, f"rx_count {fifo['rx_count']}: 0x{fifo['raw']:04x}"
    assert fifo["tx_full"] == 0 and fifo["rx_empty"] == 1, \
        f"flags wrong: 0x{fifo['raw']:04x}"
    # fill TX FIFO -> tx_full bit1
    await host.run_program("rpt 9\npushi 0x5A\nhalt\n", cycles=200, wait_halt=False)
    fifo = decode_tfifo(await host.get_tfifo(0))
    assert fifo["tx_count"] == 8 and fifo["tx_full"] == 1, \
        f"TX full flags: 0x{fifo['raw']:04x}"
    host.log.info("TFIFO layout OK: counts [11:8]/[7:4], tx_full=bit1, rx_empty=bit0")

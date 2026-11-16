"""Scratch probe: print raw MISO byte streams for framing debug (not part of CI)."""
import cocotb
from host import start_loom, CMD, CSR


@cocotb.test()
async def probe(dut):
    host, mon = await start_loom(dut)
    log = host.log
    async def st(tag):
        rx = await host.spi_xfer([0x0B, 0, 0, 0, 0])
        log.info("%-14s STATUS rx=%s", tag, " ".join(f"{b:02x}" for b in rx))
    await st("fresh")
    await st("again")
    for t in range(4):
        ts = await host.get_tstatus(t); tf = await host.get_tfifo(t)
        log.info("t%d TSTATUS=%04x TFIFO=%04x", t, ts, tf)
    log.info("tx_empty=%b rx_full=%b", host._uio_ext, 0)

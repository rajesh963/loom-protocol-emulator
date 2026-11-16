"""Scratch probe: print raw MISO byte streams for framing debug (not part of CI)."""
import cocotb
from host import start_loom, CMD, CSR


@cocotb.test()
async def probe(dut):
    host, mon = await start_loom(dut)
    log = host.log

    async def show(name, tx):
        rx = await host.spi_xfer(tx)
        log.info("%-16s tx=%s  rx=%s", name,
                 " ".join(f"{b:02x}" for b in tx),
                 " ".join(f"{b:02x}" for b in rx))

    await show("PING", [0x0D, 0x5A, 0x00])
    await show("READ_CSR POUT", [0x05, 0x10, 0x00, 0x00, 0x00])
    await show("READ_CSR TSTATUS0", [0x05, 0x42, 0x00, 0x00, 0x00])
    await show("SET_ADDR 40", [0x01, 0x40, 0x00, 0x00, 0x00])
    await show("WRITE 40", [0x02, 0xEF, 0xBE, 0xAD, 0xDE, 0x00])
    await show("READ_MEM", [0x03, 0x00, 0x00, 0x00, 0x00, 0x00])
    await show("READ_CSR TMR", [0x05, 0x12, 0x00, 0x00, 0x00])
    await show("STATUS", [0x0B, 0, 0, 0, 0, 0])
    log.info("pout=%04x status=%s", host.pins_out(), host.pins_dir())

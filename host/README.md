# LOOM host-side client (`host/`)

Transport-agnostic Python implementation of the LOOM SPI byte protocol
(`spec/ARCHITECTURE.md` §6) plus a pin-level SPI mode-0 bit-banger.

## Files

| File | Contents |
|---|---|
| `loom_host.py` | `Transport`, `ByteTransport` (command framing), `ModelTransport` (chip side over `sw/loom_model.py`), `ModelLink` (direct model API), `decode_status_byte()` |
| `spi_bitbang.py` | `PinIO`, `SPIBitBang` (exact SCK/MOSI/MISO sequence), `BitBangTransport` (use a bit-banged master as a `Transport`), `FakeChipSPI` (loopback test double) |

Run the self-tests (no hardware, no RTL needed):

```bash
python3 -m host.loom_host      # HOST_SELFTEST: PASS
python3 -m host.spi_bitbang    # BITBANG_SELFTEST: PASS
```

## Byte protocol in one paragraph

Every SPI transfer is full duplex: `[CMD][ARG...]`, multi-byte args LSB first.
The CMD transfer returns `0x80` (accepted / idle) or `0x81` (busy); ARG
transfers return `0x00`; read payloads appear on the transfers **after** the
command/args, LSB first. `READ_MEM` returns 4 bytes and auto-increments the word
pointer (PMEM `0x000..0x3FF`, DMEM `0x400..0x43F`); `READ_CSR` returns 2 bytes;
`RX_POP` returns 1 byte (`0x00` if empty); `STATUS` returns 4 bytes, one per
thread: bit7 `halt`, bit6 `en`, bit5 `tx_full`, bit4 `tx_empty`, bit3 `rx_full`,
bit2 `rx_empty`. `PING` is `0x0D`, arg `0x5A`, reply `0xA5`. Thread CSR banks
are addressed at `0x40 + 16*tid + n` (`0x00` TPERIOD … `0x08` TPC). `RUN`/`STOP`
write the core's private run/stop masks; `ACK_IRQ` clears the IRQ latch.

## Drive the golden model through the byte protocol

```python
from host.loom_host import ByteTransport, ModelTransport
from sw.loom_model import Loom, CSR_TPERIOD
from sw import loom_asm

prog = loom_asm.assemble(open("prog.asm").read())
chip = ModelTransport(Loom())          # the "chip"
host = ByteTransport(chip)             # the "host"
assert host.ping()
host.write_mem(0, prog.words)          # SET_ADDR + WRITE_MEM, auto-incrementing
host.set_pc(0, 0)
host.run(0xF)                          # release all four threads
chip.loom.run(500)                     # advance the model
print(host.status_flags())             # per-thread {halt, en, tx_full, ...}
print(host.pop_tx(0))                  # bytes the thread pushed
host.write_csr(0x40 + 16*0 + 0x08, 0)  # e.g. write thread 0's TPC
```

## Drive the model directly (protocol firmware / VIPs)

`ModelLink` is a duck-typed convenience layer with no SPI framing:

```python
from host.loom_host import ModelLink

link = ModelLink()                     # wraps a fresh Loom
link.load(prog.words); link.run_threads(0b0001)
link.set_pins_in(0x1234)
link.run_with_pins(10_000, lambda cycle, pout, dir8: my_far_end(cycle, pout, dir8))
byte = link.rx_pop(0)                  # thread TX FIFO -> host
link.tx_push(0, 0x41)                  # host -> thread RX FIFO
```

## Bit-banged SPI (MicroPython / GPIO)

`SPIBitBang` drives the exact pin sequence: CS active low, mode 0, MSB first,
MISO sampled on SCK rising edges, MOSI changed while SCK is low. Wrap it in
`BitBangTransport` and feed `ByteTransport`; each LOOM command is kept inside a
single CS window automatically:

```python
from host.spi_bitbang import PinIO, SPIBitBang, BitBangTransport
from host.loom_host import ByteTransport

class MyPins(PinIO):                 # implement set_cs/set_sck/set_mosi/read_miso
    ...

host = ByteTransport(BitBangTransport(SPIBitBang(MyPins())))
host.ping(); host.write_mem(0, words); host.run(1)
```

A chip-side loopback test double (`FakeChipSPI`) is included so this path is
regression-tested against the model without pins:
`python3 -m host.spi_bitbang`.

## Against the RTL (cocotb)

`ByteTransport` only needs a `Transport` with `xfer(byte) -> byte`. For the
cocotb host-bridge testbench implement a small adapter that drives
`ui_in[0:2]` (CS/SCK/MOSI) and samples `uo_out[0]` once per byte, then pass it
to `ByteTransport`. The framing is identical to `ModelTransport`, so a test can
run the same host script against RTL and model and diff the return bytes.

## Notes / limitations

* `TX_PUSH` into a full thread RX FIFO is silently dropped (the bridge accepts
  the command before the FIFO is consulted) — poll `TFIFO`/`STATUS` first.
* `RX_POP` of an empty TX FIFO returns `0x00` and sets
  `ByteTransport.last_rx_pop_empty`.
* `ByteTransport` raises `HostBusyError` (subclass of `HostProtocolError`) if the
  chip answers `0x81` to a command; retry after checking `STATUS`.
* `host/demo_uart.py` (a UART-to-SPI bridge demo) is intentionally not included;
  protocol helpers live in `sw/protocols.py` (firmware owner) and use
  `ModelLink` / `ByteTransport`.

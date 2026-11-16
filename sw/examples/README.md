# LOOM protocol firmware examples

Programmable protocol engines for the LOOM ASIC — **all timing comes from the
hardware event scheduler, not from instruction counting**. Each example is a
single relocatable `.asm` (< 128 instructions) that can run on any of the four
threads, plus the Python "other end of the wire" helpers in `sw/protocols.py`
and a runnable demo in `host/demos/`.

```
sw/examples/*.asm        firmware sources (this README describes each one)
sw/prog/build_examples.py builds sw/prog/*.hex|.lst|.sym + manifest.json
sw/examples/model_selftest.py  proves every example byte-exact on the golden model
sw/protocols.py         UartBus / SpiBus / I2cBus — far-end models + host helpers
host/demos/demo_all.py  four protocols running at once, decoded transcript + waves
```

## Quick start

```bash
cd chipchallenge/loom
../.venv/bin/python sw/prog/build_examples.py        # assemble all examples
../.venv/bin/python sw/examples/model_selftest.py -v # 36 checks, golden model
../.venv/bin/python host/demos/demo_all.py           # end-to-end demo
```

The golden-model self-test is the acceptance gate: every protocol is exercised
byte/frame-exact through real pin waveforms, including 9600/57600/115200 baud
variants, all four SPI modes, 100 kHz + 400 kHz I2C, a 450-cycle clock stretch,
a 2 MHz SPI slave clock, and all four threads running together.

## How firmware gets its timing

| primitive | use | why |
|---|---|---|
| `setp PERIOD` + `emit rN` | bit/edge generation | 12.4 fixed point with a hardware carry: drift-free, independent of instruction issue, other threads and FIFO stalls. This is the only timing reference for the wire. |
| `waitd D` | relative waits in polling loops | same 12.4 accumulator, so N waits are exactly N periods on average |
| `waitp mask|value` | react to an external edge | ≤ 4-cycle polling latency, only P[7:0] can be masked |
| `inat rd, t` | sample at an exact cycle | absolute (immediate) time — used for fixed schedules, not in these examples |
| `csrw TPC, rN` | computed jump | how `i2c_master.asm` / `i2c_target.asm` do CALL/RETURN without a return stack |

**Idle-thread discipline.** An `EVWAIT`/`WAITP` retry costs 2 cycles (50 % of the
issue slots) while an idle protocol thread waits. All host-driven examples
therefore idle in a `WAITD 256` + RX-FIFO-poll loop (~4 % of the slots), and the
UART receiver polls its input every 32 cycles. This is what lets four emulated
protocols share one 1-instruction/cycle datapath.

## Example inventory

| program | thread in demo | pins (P[n]) | size | status | measured rate |
|---|---|---|---|---|---|
| `uart_tx.asm` | t0 | TX=P[10]=uo[2] | 34 | model-verified | 115200 8N1, 520.8125 cyc/bit (−40 ppm) |
| `uart_rx.asm` | t1 | RX=P[11]=ui[3] | 32 | model-verified | 115200 8N1, full duplex with tx |
| `uart_loopback.asm` | — | RX=P[11], TX=P[10] | 41 | model-verified | half-duplex echo, needs inter-byte gap |
| `spi_master.asm` | t2 | SCK=P[0], MOSI=P[1], MISO=P[2], CS_N=P[3] | 49 | model-verified, modes 0–3 | ~0.97 MHz SCK (≈62 cyc/bit); 2 MHz with `QUARTER_CYCLES=0` |
| `spi_slave.asm` | — | MISO=P[0], SCK=P[4], MOSI=P[5], CS_N=P[6] | 31 | model-verified | 2 MHz external SCK tested clean |
| `i2c_master.asm` | t3 | SCL=P[4]=uio[4], SDA=P[5]=uio[5] | 125 | model-verified | 100 kHz (300-cyc phases), 400 kHz variant |
| `i2c_target.asm` | — | SCL=P[6]=uio[6], SDA=P[7]=uio[7] | 121 | model-verified | 100 kHz, register file = DMEM[0..7] |
| `ws2812.asm` | — | DATA=P[12]=uo[4] | 47 | model-verified | 800 kHz (T0H 24 / T1H 48 cyc), 50 us latch |
| `ps2_target.asm` | — | CLK=P[13]=uo[5], DATA=P[14]=uo[6] | 58 | model-verified | ~14.3 kHz clock, 11-bit frames + odd parity |
| `onewire.asm` | — | DQ=P[7]=uio[7] (open-drain) | 91 | model-verified | standard-speed, +25.0 C DS18B20 scratchpad |

Pin convention: `P[15:10]` = `uo_out[7:2]`, `P[15:11]` = `ui_in[7:3]` readable,
`P[7:0]` = the bidirectional open-drain `uio` pins (DIR=1 pulls low, DIR=0
releases), `P[9:8]` are the host link and never touched. `ui[2:0]`/`uo[0:1]` are
the host SPI bridge, so `ui[7:3]` are the only usable `uo`-side inputs.

All images are **relocatable** (PC-relative jumps only). The demo loads them at
`0x000/0x080/0x100/0x180` and sets each thread's PC; any other placement works.
The FF-fallback build has only 128 PMEM words — every program fits, but the
four-program demo needs the 1024-word SRAM build.

## UART — 115200 8N1, full duplex (`uart_tx.asm`, `uart_rx.asm`, `uart_loopback.asm`)

* **Bit clock**: `BAUD_DIV = round(60e6·16/115200) = 8333 = 0x208D` →
  520.8125 cycles/bit, −40 ppm. The 12.4 period register is at most 4095.9
  cycles, so slow rates use `CELLS = ceil(BAUD_DIV/65535)` event cells per bit
  (`9600 baud = 2 × 50000 = 6250 cycles/bit`, exact). Both TX and RX implement
  this, so `BAUD` is configurable via `.equ` for any rate.
* **TX event schedule**: 10 cells — start(0), 8 data LSB-first, stop(1). Each
  cell is one `emit`; the fractional carry makes the frame drift-free and the
  queue's back-pressure throttles the CPU automatically. Back-to-back frames
  chain one bit after the stop cell; after an idle gap the spec §3.1
  behind-schedule clamp restarts the chain at TMR+1.
* **RX**: gentle 32-cycle poll for the falling edge → `waitd` half-bit →
  mid-bit sample each cell → framing check on the stop bit → byte to the host TX
  FIFO. Issue-delay jitter is < 1 % of a bit and the mid-bit margin stays
  > 150 cycles even with all four threads runnable.
* **Loopback** is a half-duplex echo (1 char time to receive + 1 to transmit →
  50 % line rate); for true full duplex run `uart_tx` and `uart_rx` on two
  threads, which is what `demo_all.py` does.

## SPI master — modes 0–3, full duplex (`spi_master.asm`)

* `MODE` `.equ` selects CPOL/CPHA. SCK edges use `TOG` (works for both
  polarities); the only mode-dependent code is the MISO sample position
  (middle of the low phase before the leading edge for CPHA=0, middle of the
  high phase after it for CPHA=1), selected by a runtime CPHA flag.
* **Honest cycle budget**: this is bit-banged. Per bit the loop does 4 `WAITD`
  quarter-cells + ~30 instructions, so
  `SCK period = 4·QUARTER_CYCLES + N_instr`. `QUARTER_CYCLES=8` gives ≈62
  cycles/bit (~0.97 MHz) when the thread runs alone; with all four threads
  runnable the body costs 60–120 cycles and SCK falls to ~0.4–0.7 MHz with
  scheduler-dependent duty cycle. **10 MHz (3-cycle half period) is the pin
  unit's ceiling, not this firmware's**: it needs precomputed patterns via the
  wave player (stretch, MOSI-only). The CPU keeps up with the wire (30–60
  instructions per 60-cycle bit) — issue rate, not the event queue, is the limit.
* One byte per CS frame, MSB-first, MISO shifted in, received byte to the host
  TX FIFO; the drain loop empties the host FIFO without a doorbell.

## SPI slave — mode 0 (`spi_slave.asm`)

* Polls with `WAITP` (`CS low`, `SCK high`, `SCK low`); samples MOSI on the
  rising edge, presents MISO while SCK is low. One CS frame = one byte;
  response = previous MOSI + 1 (a deterministic register-read fixture).
* **Max slave clock**: each edge costs ≤4 cycles of poll latency + ~8
  instructions. A 16-cycle half period (2 MHz) is the practical edge; tested
  clean at 1 MHz and 2 MHz on the golden model. Since `WAITP`'s mask is only
  8 bits, the SCK/MOSI/CS pins must be on `P[7:0]` (they are).

## I2C master — 100/400 kHz, ACK/NAK, repeated START, clock stretch (`i2c_master.asm`)

* Open-drain on `uio[4]`/`uio[5]`: pin value always 0, `DIR` bit = 1 pulls low,
  0 releases to the external pull-up. SDA/SCL are read back through `pins_in`.
* `HALF_DIV = round(60e6·16/(2·I2C_HZ))`: 100 kHz → 300-cycle phases
  (0x12C0), 400 kHz → 75 cycles (0x04B0). Every time SCL is released the
  firmware `WAITP`s for it to actually go high, so **a target may stretch the
  clock indefinitely** — no timeout, no corruption (the model test stretches
  450 cycles on every ACK).
* Host job = 4 bytes (`cmd, dev, reg, data`, cmd bit0 = read). Write performs
  START/dev+W/reg/data/STOP; read performs START/dev+W/reg/Sr/dev+R/byte/NAK/
  STOP and returns the byte to the host TX FIFO. The 4 bus byte-loops are one
  `CSRW TPC` subroutine (position-independent call macro), which keeps the
  program at 125 words. **This example needs the RTL fix that makes a thread
  `CSRW TPC` redirect the fetch cleanly** (the golden model does; the current
  RTL leaves a stale instruction buffer — reported by the toolchain owner).

## I2C target — 100 kHz register file (`i2c_target.asm`)

* 7-bit address `0x50`; ACK on match, NAK + idle-bus resync otherwise (double-
  checks the idle condition after 320 cycles so a data "1" is not mistaken for
  STOP). Register file = `DMEM[0..7]`, first write byte sets the pointer, data
  auto-increments; reads return `DMEM[reg]` and auto-increment.
* A byte boundary is resolved by sampling SDA at the rising edge and polling
  while SCL is high: data is stable, a repeated START/STOP moves SDA. That is
  how a repeated START is told apart from a data byte.

## WS2812 / WS2812B — 800 kHz LED strip (`ws2812.asm`)

* Bit period 1.25 us = 75 cycles: T0H 0.40 us (24 cyc), T1H 0.80 us (48),
  T0L 0.85 us (51), T1L 0.45 us (27); latch = 50 us low.
* **Event schedule**: each edge is one `emit`, and the duration of a cell is
  the `setp` value in force when the *next* event is enqueued. The loop does
  `setp T0L; emit HIGH; setp T0H; emit LOW` (and the T1 variant), so the
  hardware scheduler produces exact pulse widths while the CPU only has to keep
  ahead (2 emits + ~8 instructions per 75-cycle bit — it does).
* Host pushes 3 bytes per LED in GRB order; `Ws2812` in `sw/protocols.py`
  decodes the strip waveform back into bytes and measured pulse widths.

## PS/2 device (`ps2_target.asm`)

* Device-to-host frames: start(0), 8 data LSB-first, odd parity, stop(1),
  11 clock pulses at ~14.3 kHz (35 us half period = 2100 cycles = 0x8340).
  Data is set, `waitd` 5 us, CLK pulled low 35 us and released 35 us; the host
  samples on the falling edge. Host pushes one scancode per byte; `Ps2Host`
  in `sw/protocols.py` decodes and checks parity.

## 1-Wire / DS18B20 (`onewire.asm`)

* Open-drain DQ on `uio[7]`. The device waits for a RESET pulse (>=480 us low),
  answers with a 30 us-delayed, 120 us PRESENCE pulse, receives ROM (0xCC skip)
  and function commands in write slots, and for 0xBE READ SCRATCHPAD shifts out
  9 bytes in read slots. Timing uses 20 us/15 us/40 us `waitd` cells and WAITP
  edge detection; `OneWireMaster` in `sw/protocols.py` is the host side
  (reset/presence, write/read slots, scratchpad read). The scratchpad is
  +25.0 C (`0x90 0x01 ...`); the 9th byte is a CRC placeholder (the demo does
  not verify it — a real CRC-8/MAXIM is ~30 extra instructions).

## Host helpers (`sw/protocols.py`)

`UartBus`, `SpiBus`, `I2cBus`, `Ws2812`, `Ps2Host`, `OneWireMaster` are both
(a) the **far end of the wire** for the model demo — each drives the chip's input pins and decodes its output pins cycle
by cycle — and (b) the application-side helpers used on real silicon through
`host/loom_host.py`. The `app_*` methods push/pop thread FIFO bytes via the host
SPI bridge, so the same host code runs against the model (`ModelLink`) or a real
board (`ByteTransport`); only the wire-level demo/recording is model-only.

```python
from sw import loom_asm, loom_model
from sw.protocols import UartBus, SpiBus, I2cBus, WireWorld
from host.loom_host import ModelLink

loom = loom_model.Loom(); link = ModelLink(loom); world = WireWorld(link)
uart = UartBus(world, tx_tid=0, rx_tid=1)        # far end + host helpers
uart.app_send(b"Hello")                          # host -> chip TX firmware
print(uart.wire_recv())                          # decoded from the TX pin
uart.wire_send(b"LOOM"); world.run(20000)        # far end -> chip RX
print(uart.app_recv())                           # chip RX firmware -> host
```

## Known limits / next steps

* UART at 115200 is the fast case; the 12.4 period limit means slow rates are
  split into `CELLS` cells (implemented), and the 4-thread RX margin was
  measured on the model, not silicon.
* SPI master full-duplex tops out around 1–2 MHz firmware bit-banged; 10 MHz
  needs the wave player with precomputed patterns.
* `i2c_master.asm`/`i2c_target.asm` depend on the clean `CSRW TPC` computed
  jump; until the RTL fix lands they are model-proven only.
* On-chip capture/record-replay is a stretch feature; `host/demos/
  demo_record_replay.py` shows the model-level record/replay flow that the
  capture engine will make on-chip once `loom_capture.v` lands.
* External rise times (I2C pull-ups, UART/SPI drivers) are the user's board
  responsibility; the firmware guarantees the 30–70 % point timing.

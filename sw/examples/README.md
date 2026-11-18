# LOOM protocol firmware examples

Programmable protocol engines for the LOOM ASIC — **all timing comes from the
hardware event scheduler, not from instruction counting**. Each example is a
single relocatable `.asm` (< 128 instructions) that can run on any of the four
threads, plus the Python "other end of the wire" helpers in `sw/protocols.py`
and a runnable demo in `host/demos/`.

**Shipped design point: 33.33 MHz** (`CLOCK_PERIOD` 30 ns, the frequency the
post-CTS resizer closes at; the competition sets no frequency target). Every
rate below is the real-time rate at 33.33 MHz; the cycle counts are what the
firmware and the tests actually use. Unless stated otherwise, rates are
**pin-trace measurements over a full protocol period** (UART: start-edge to
start-edge across bytes; I²C: rising-to-rising SCL, i.e. high + low phase —
*not* 2x the high phase), measured on the RTL at 30 ns with `verify`'s VIPs;
the golden-model numbers agree within the polled-release overhead noted per
protocol.

```
sw/examples/*.asm        firmware sources (this README describes each one)
sw/prog/build_examples.py builds sw/prog/*.hex|.lst|.sym + manifest.json
sw/examples/model_selftest.py  proves every example byte-exact on the golden model
sw/protocols.py         UartBus / SpiBus / I2cBus — far-end models + host helpers
host/{jtag,can,dmx,midi,ir_nec}.py  independent far-end decoders (protocol set 2)
host/demos/demo_all.py  four protocols running at once, decoded transcript + waves
```

## Quick start

```bash
cd chipchallenge/loom
../.venv/bin/python sw/prog/build_examples.py        # assemble all examples
../.venv/bin/python sw/examples/model_selftest.py -v # 161 checks, golden model
../.venv/bin/python host/demos/demo_all.py           # end-to-end demo
```

The golden-model self-test is the acceptance gate: every protocol is exercised
byte/frame-exact through real pin waveforms, including 9600/57600/115200 baud
variants, all four SPI modes, 100 kHz + 400 kHz I2C, a 450-cycle clock stretch,
SPI slave at 1 MHz (and 2 MHz as a model-only stress), and all four threads
running together.

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
| `uart_tx.asm` | t0 | TX=P[10]=uo[2] | 34 | model + RTL verified | 115200 8N1, 289.375 cyc/bit (divider −80 ppm) |
| `uart_rx.asm` | t1 | RX=P[11]=ui[3] | 32 | model-verified | 115200 8N1, full duplex with tx |
| `uart_loopback.asm` | — | RX=P[11], TX=P[10] | 41 | model-verified | half-duplex echo, needs inter-byte gap |
| `spi_master.asm` | t2 | SCK=P[0], MOSI=P[1], MISO=P[2], CS_N=P[3] | 49 | model-verified, modes 0–3 | ~1.03 MHz SCK (32 cyc/bit, `QUARTER_CYCLES=4`); 1.63 MHz ceiling |
| `spi_slave.asm` | — | MISO=P[0], SCK=P[4], MOSI=P[5], CS_N=P[6] | 31 | model-verified | 1 MHz external SCK clean (16-cyc half is the RTL edge) |
| `i2c_master.asm` | t3 | SCL=P[4]=uio[4], SDA=P[5]=uio[5] | 125 | model + RTL verified | nominal 100 kHz (RTL 92.5 kHz), nominal 400 kHz (RTL 344 kHz) |
| `i2c_target.asm` | — | SCL=P[6]=uio[6], SDA=P[7]=uio[7] | 121 | model + RTL verified | 100 kHz, register file = DMEM[0..7] |
| `ws2812.asm` | — | DATA=P[12]=uo[4] | 47 | model-verified | 800 kHz (T0H 13.3 / T1H 26.7 cyc), 50 us latch |
| `ps2_target.asm` | — | CLK=P[13]=uo[5], DATA=P[14]=uo[6] | 58 | model-verified | 14.28 kHz clock, 11-bit frames + odd parity |
| `onewire.asm` | — | DQ=P[7]=uio[7] (open-drain) | 91 | model-verified | standard-speed, +25.0 C DS18B20 scratchpad |
| `usb_ls_tx.asm` (stretch) | — | D+=P[0]=uio[0] | 96 | model-verified (TX only) | 1.4980 Mbit/s (−0.13 %), 22.25 cyc/bit |
| `eth_tx.asm` (stretch) | — | TXD=P[0]=uio[0] | 97 | model-verified | Manchester 2.083 Mbit/s (4.167 Mbaud), CRC-32 FCS in firmware |
| `usb_ls_rx.asm` (stretch) | — | D+=P[0]=uio[0], D−=P[1]=uio[1] | 357 | model-verified (SRAM-1024 only) | RX decode + ACK: 22.25 cyc/bit, CRC-5/16, EOP; enumeration not implemented |
| `jtag_tap.asm` | — | TCK=P[0], TMS=P[1], TDI=P[2], TDO=P[3] (all uio) | 115 | model-verified | ~0.96 MHz TCK (mean 34.8 cyc), IDCODE 0x14B5A5A5 IR+DR readback |
| `can_tx.asm` | — | TXD=P[10]=uo[2], RXD=P[11]=ui[3] | 79 | model-verified (TX) | 499.85 kbit/s (66.6875 cyc/bit), firmware CRC-15 + stuffing, ACK |
| `dmx_tx.asm` | — | DMX=P[10]=uo[2] | 35 | model-verified | 250.04 kbaud (133.3125 cyc/bit), 512-slot universe |
| `midi_tx.asm` | — | MIDI TX=P[10]=uo[2] | 16 | model-verified | 31250.6 baud (1066.6875 cyc/bit), 8N1 |
| `ir_nec_tx.asm` | — | IR=P[10]=uo[2] | 54 | model-verified | 38.00 kHz carrier (438.625-cyc half), 9/4.5 ms lead, 32 bits |

Pin convention: `P[15:10]` = `uo_out[7:2]`, `P[15:11]` = `ui_in[7:3]` readable,
`P[7:0]` = the bidirectional open-drain `uio` pins (DIR=1 pulls low, DIR=0
releases), `P[9:8]` are the host link and never touched. `ui[2:0]`/`uo[0:1]` are
the host SPI bridge, so `ui[7:3]` are the only usable `uo`-side inputs.

All images are **relocatable** (PC-relative jumps only). The demo loads them at
`0x000/0x080/0x100/0x180` and sets each thread's PC; any other placement works.
The FF-fallback build has only 128 PMEM words — every program fits, but the
four-program demo needs the 1024-word SRAM build.

## UART — 115200 8N1, full duplex (`uart_tx.asm`, `uart_rx.asm`, `uart_loopback.asm`)

* **Bit clock**: `BAUD_DIV = round(33.33e6·16/115200) = 4630 = 0x1216` →
  289.375 cycles/bit; the exact ideal is 289.3518, so the divider is **−80 ppm**
  (115,190.8 Bd vs 115,200 nominal). RTL pin-trace measurement over back-to-back
  frames: 289.35 cycles/bit → **115.20 kBd (+0.001 %)**, i.e. the quantisation
  is below measurement resolution. Well inside UART tolerance either way. The 12.4 period
  register is at most 4095.9 cycles, so slow rates use
  `CELLS = ceil(BAUD_DIV/65535)` event cells per bit
  (`9600 baud = 55556 = 3472.25 cycles/bit`, CELLS=1). Both TX and RX implement
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
  quarter-cells + ~16 instructions, so
  `SCK period = 4·QUARTER_CYCLES + N_instr`. Measured at 33.33 MHz, single
  thread (mode 0):

  | `QUARTER_CYCLES` | SCK period | SCK rate |
  |---|---|---|
  | 0 or 1 | 20.4 cyc | 1.63 MHz (ceiling; tight MISO margin) |
  | 2 | 24.4 cyc | 1.37 MHz |
  | **4 (shipped)** | **32.4 cyc** | **1.03 MHz** |
  | 8 | 48.4 cyc | 0.69 MHz (max margin) |

  With all four threads runnable the body costs 60–120 cycles and SCK falls to
  roughly a third of these rates with scheduler-dependent duty cycle. **10 MHz
  (3-cycle half period) is the pin unit's ceiling, not this firmware's**: it
  needs precomputed patterns via the wave player (stretch, MOSI-only). The CPU
  keeps up with the wire — issue rate, not the event queue, is the limit.
* One byte per CS frame, MSB-first, MISO shifted in, received byte to the host
  TX FIFO; the drain loop empties the host FIFO without a doorbell.

## SPI slave — mode 0 (`spi_slave.asm`)

* Polls with `WAITP` (`CS low`, `SCK high`, `SCK low`); samples MOSI on the
  rising edge, presents MISO while SCK is low. One CS frame = one byte;
  response = previous MOSI + 1 (a deterministic register-read fixture).
* **Max slave clock**: each edge costs ≤4 cycles of poll latency + ~8
  instructions, so a 16-cycle half period is the practical edge — that is
  ~1.04 MHz at 33.33 MHz, and 1 MHz is tested clean (the 60 MHz design point
  could do 2 MHz with the same cycle budget; the model also passes an 8-cycle
  2 MHz stress, but that is beyond the RTL-safe margin). Since `WAITP`'s mask is
  only 8 bits, the SCK/MOSI/CS pins must be on `P[7:0]` (they are).

## I2C master — 100/400 kHz, ACK/NAK, repeated START, clock stretch (`i2c_master.asm`)

* Open-drain on `uio[4]`/`uio[5]`: pin value always 0, `DIR` bit = 1 pulls low,
  0 releases to the external pull-up. SDA/SCL are read back through `pins_in`.
* `HALF_DIV = round(33.33e6·16/(2·I2C_HZ))`: 100 kHz → 166.6-cycle phases
  (0x0A6A), 400 kHz → 41.6 cycles (0x029A), i.e. a **nominal** 100/400 kHz.
  Measured full SCL period (rising-to-rising, high + low phase): **92.5 kHz /
  344.2 kHz on the RTL** (golden model 97.6 / 364 kHz). The difference is the
  *polled SCL-release overhead*: after `dclr SCL` the firmware `WAITP`s for the
  line to rise (2-4 cycles) and the low phase also carries the instruction
  issue, so the low phase is ~20 cycles longer than the high phase on RTL. I²C
  specifies **maximum** rates, so 92.5 kHz is a legal 100 kHz-mode controller;
  quoting 2x the high phase (169.1×2 = 338 cyc = 98.6 kHz) overstates it — use
  the full period. Every time SCL is released the firmware `WAITP`s for it to
  actually go high, so **a target may stretch the clock** — no timeout, no
  corruption (the model test stretches 450 cycles on every ACK; the cocotb RTL
  test at 400 kHz passes with legal stretching).
* Clock stretching is only ever *extending the low phase*: a target holds SCL
  low after it has been driven low and releases it later. A testbench that
  pulls SCL low while the line is **high** is not a stretch — it injects an
  extra falling edge (an extra clock), and any I2C controller will decode the
  resulting frame wrong. The RTL suite's 400 kHz capture showed 31 SCL rising
  edges for a 27-clock transaction exactly because of that; with low-phase-only
  stretching it decodes `0xA0 ACK, 0x00 ACK, 0x5A ACK, STOP` (28 rising edges,
  all data correct). See `known_issues.py` VER-006 for the VIP-side fix.
* Host job = 4 bytes (`cmd, dev, reg, data`, cmd bit0 = read). Write performs
  START/dev+W/reg/data/STOP; read performs START/dev+W/reg/Sr/dev+R/byte/NAK/
  STOP and returns the byte to the host TX FIFO. The 4 bus byte-loops are one
  `CSRW TPC` subroutine (position-independent call macro), which keeps the
  program at 125 words. A 3-byte write is ~10.4 kcycles ≈ 313 us on the wire at
  the 100 kHz setting. This relies on a thread `CSRW TPC` redirecting the
  fetch; the RTL now invalidates the prefetched instruction (the earlier
  stale-word hazard is fixed) and the example passes in cocotb.

## I2C target — 100 kHz register file (`i2c_target.asm`)

* 7-bit address `0x50`; ACK on match, NAK + idle-bus resync otherwise (double-
  checks the idle condition after a 320-cycle `waitd` so a data "1" is not
  mistaken for STOP). At 33.33 MHz/100 kHz the SCL high phase is 166.7 cycles,
  so the 320-cycle resync has 1.92x margin and does not need rescaling.
  Register file = `DMEM[0..7]`, first write byte sets the pointer, data
  auto-increments; reads return `DMEM[reg]` and auto-increment.
* A byte boundary is resolved by sampling SDA at the rising edge and polling
  while SCL is high: data is stable, a repeated START/STOP moves SDA. That is
  how a repeated START is told apart from a data byte. The poll samples SCL
  and SDA in **one atomic `in`** and checks **SCL first**: as soon as SCL is low
  the byte continues (a fast master may change SDA on the falling edge itself,
  i.e. with zero hold time — the RTL VIP does exactly that); only an SDA move
  while SCL is *still high* is a START/STOP. (Two real bugs were found here on
  the RTL: the old SDA-first order abandoned the data byte and NAKed it, and a
  1-instruction sampling skew made a normal falling-edge SDA change look like a
  START at short bit periods.)

## WS2812 / WS2812B — 800 kHz LED strip (`ws2812.asm`)

* Bit period 1.25 us ≈ 41.7 cycles at 33.33 MHz: T0H 0.40 us (0x00D5 =
  13.3 cyc), T1H 0.80 us (0x01AB = 26.7), T0L 0.85 us (0x01C5 = 28.3), T1L
  0.45 us (0x00F0 = 15.0); latch = 50 us low (0x682B). Measured high pulses
  13/14 and 26/27 cycles (fractional carry), i.e. 0.39/0.81 us — inside the
  WS2812 T0H<=0.5 us / T1H>=0.65 us windows.
* **Event schedule**: each edge is one `emit`, and the duration of a cell is
  the `setp` value in force when the *next* event is enqueued. The loop does
  `setp T0L; emit HIGH; setp T0H; emit LOW` (and the T1 variant), so the
  hardware scheduler produces exact pulse widths while the CPU only has to keep
  ahead (2 emits + ~8 instructions per 41.7-cycle bit — it does).
* Host pushes 3 bytes per LED in GRB order; `Ws2812` in `sw/protocols.py`
  decodes the strip waveform back into bytes and measured pulse widths.

## PS/2 device (`ps2_target.asm`)

* Device-to-host frames: start(0), 8 data LSB-first, odd parity, stop(1),
  11 clock pulses at 14.28 kHz (35 us half period = 0x48EB = 1166.7 cycles).
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

## USB 1.1 low-speed device RX + ACK — stretch (`usb_ls_rx.asm`)

Device-side receive path for the USB LS stretch goal.
**Rung status: (1) RX decode ✅ (golden-model verified), (2) ACK response ✅ but
functional-only (measured latency far outside the USB window), (3) enumeration
state machine ❌ not implemented — documented boundary below.**

* **Pins**: D+ = `P[0] = uio[0]`, D− = `P[1] = uio[1]`; line-state value
  `K=1, J=2 (idle), SE0=0`. The pair is high-Z (DIR=0) while receiving and both
  lines are driven during the ACK.
* **Receive pipeline**: poll the J→K SYNC edge, align the sample grid with a
  measured fixed offset (valid window 6–12 cycles; 9 chosen), then take 104
  differential samples on an exact **22.25-cycle grid** (`WAITD 7.25` + 15
  fixed instructions; branchy work padded with NOPs). Post-processing (untimed)
  validates the SYNC state pattern, NRZI-decodes, bit-unstuffs, checks
  PID+complement, **CRC-5** for tokens / **CRC-16/USB** for DATA0, finds the
  EOP as the first double-SE0 sample pair, and pushes
  `[status, pid, addr|endp<<7, crc_ok, eop_ok]` to the host TX FIFO.
* **Measured (golden model)**: host bit period recovered **22.249–22.255 cyc**
  (1.4981–1.4982 Mbit/s); SETUP token addr0/ep0 → PID `0x2D`, ADDR/ENDP 0,
  CRC-5 `0x02` ok, EOP at sample 32; DATA0 `80 06 00 01 00 00 40 00` (8-byte
  GET_DESCRIPTOR request) → payload byte-exact, CRC-16 `0x94DD` ok, EOP at
  sample 96. Decodes at **nominal, +0.25 % and −0.25 % host clock drift**.
* **ACK response**: after a valid DATA0 the firmware drives the pair with
  SYNC + PID `0xD2` + EOP (18 cells at 22.25 cycles; the host-side reference
  decoder reads `ack pid=0xD2, EOP ok, 22.255 cyc/bit`).
* **Measured ACK-latency ceiling** (golden model, PC-breakpoint breakdown, the
  DATA0 packet above; 22.25 cycles/bit, USB LS ACK window = 7.5 bit times =
  167 cycles):
  | segment | cycles | bit times |
  |---|---|---|
  | sampler (per bit) | 16 instr + WAITD 7.25 = **22.25** | 1.0 |
  | host EOP → sample window end (fixed 104 samples) | 198 | 8.9 |
  | NRZI + unstuff + byte assembly (96 states) | 2505 | 112.6 |
  | PID + CRC-16 over 8 bytes | 564 | 25.3 |
  | EOP double-SE0 scan (104 states) | 1155 | 51.9 |
  | publish + ACK setup/emit | 23 | 1.0 |
  | **total EOP → first ACK cell** | **4445** | **199.8** |
  This is an **architectural ceiling, not a tuning problem**: decoding after
  capture costs hundreds of cycles, and wire-speed ACK would require inline
  per-bit decode = sampler 16 + NRZI/unstuff/accumulate ~10 + CRC-16 ~8
  instructions/bit = ~34 > 22.25 at IPC 1 (≈1.5× over; two threads share the
  single IPC, so they cannot help). Even an idealised 8-instruction sampler
  gives ~26 > 22.25 (≈17 % over). **Revision-B option (not built):** a small
  hardware LS receiver FSM (edge detect + DPLL bit sampler + NRZI/stuff/
  CRC-16 + EOP + byte FIFO/interrupt) would let firmware ACK in a few bit
  times; firmware-only wire-speed LS RX is not feasible on this core.
* **DMEM map (32/32 words used)**: `[0..12]` 104 sampled states (2 bits each),
  `[13]` PID / `[14..21]` payload or token fields, `[22..23]` received CRC-16,
  `[24]` status, `[25]` pid, `[26]` addr|endp<<7, `[27]` crc_ok, `[28]` eop_ok,
  `[29]` sync_ok, `[30]` byte count, `[31]` EOP sample index.
* **Size**: 357 instructions → **SRAM-1024 build only**; the 128-word FF
  fallback cannot hold it. RX is *code-size-bound, not timing-bound*: the
  sampler costs 16 instructions/bit at IPC 1 (72 % of the 22.25-cycle budget).
* **Enumeration boundary**: the enumeration state machine (GET_DESCRIPTOR
  DEVICE response, SET_ADDRESS) is designed but not implemented in this pass.
  DMEM analysis: the 18-byte device descriptor fits by reusing the dead sample
  region `[0..8]` at response time (copied from PMEM constants), so
  "TX+RX+ACK + device descriptor + SET_ADDRESS" is achievable; a full config
  descriptor (or HID report descriptor) does **not** fit in 32 words. Config
  descriptors would return STALL.

## Ethernet Manchester frame transmitter — stretch (`eth_tx.asm`)

The Ethernet stretch goal: transmit a **complete 64-byte minimum Ethernet frame**
with IEEE 802.3 Manchester line coding and a **CRC-32 FCS computed by the
firmware itself**. This is a 10 Mbit/s-*class* framing demo, not 10BASE-T
compliance — the honest ceiling is quantified below.

* **Pin**: `TXD = P[0] = uio[0]` (push-pull, DIR bit 0). Manchester: 0 → all
  edge one way (`1,0`), 1 → the other (`0,1`); bytes/bits LSB-first.
* **Frame**: 7 × `0xAA` preamble → `0xD5` SFD → 64 frame bytes
  (dst 6 + src 6 + ethertype 2 + payload 46 + FCS 4) → IPG → idle.
  The host preloads the 60-byte frame into DMEM; the firmware computes
  CRC-32/802.3 (reflected 0xEDB88320, init/final-XOR 0xFFFFFFFF) and writes the
  little-endian FCS into the last two DMEM words, then streams all 64 bytes.
  **DMEM footprint: 32/32 words** — exactly the frame buffer, state in registers.
* **Timing**: `setp 0x0080` = **8-cycle Manchester cells = 16-cycle bits =
  4.167 Mbaud = 2.083 Mbit/s**; every cell is one `emit`, so all edges are on
  the exact event grid. Transmit loop ≈ 9 instructions/bit against 16
  cycles/bit (56 % IPC). Wire time: (56+8+512) bits × 16 = 9216 cycles = 276 µs
  per frame; IPG 1536 cycles.
* **Model evidence** (`model_selftest.py`, 11 checks): the decoded frame is
  **byte-exact**, preamble/SFD lock, FCS 0xF57743B9 ok (host preloads the FCS
  words as zero, so the match proves the firmware computed it), 8.000-cycle cells
  / 16.00-cycle bits, **max edge residual 0.00 cycle**, 2.0833 Mbit/s.
  `host/eth.py` is the independent receiver (clock recovery, Manchester decode,
  body+FCS check, `python -m host.eth` passes).
* **Measured firmware ceiling** (same `eth_tx.asm`, only `CELL_DIV` changed):
  decode still succeeds at 5.5 cycles/cell = **3.03 Mbit/s**; it fails at 5.0
  cycles/cell (3.33 Mbit/s, edges fall behind the grid). The loop is
  ~4.5 instructions/cell, so the IPC bound is 33.33/4.5 = 7.4 M cells/s =
  3.7 Mbit/s; scheduling slack puts the practical limit at ~3.0 Mbit/s. The
  shipped 8-cycle point keeps a comfortable margin.
* **10BASE-T (20 Mbaud) verdict — not feasible, quantified**: a true 10BASE-T
  cell is 50 ns = **1.6667 cycles** at 33.33 MHz. One cell needs at least an
  `emit` plus level extraction (≥4 instructions for a data-bearing cell), i.e.
  20 M cells/s × 4 = **80 M instructions/s against a 33.33 M instr/s chip =
  2.4× over IPC**; four threads do **not** multiply IPC (work-conserving
  round-robin shares one instruction/cycle), so 4-thread scheduling only adds
  merge/ordering risk. Even a degenerate 2-instruction toggler (no frame data)
  would top out at 33.33/2 = 16.7 M cells/s = 8.3 Mbaud, and pin edges are
  cycle-quantised: ±0.5 cycle (±15 ns) plus floor-rounding up to 0.69 cycle
  (±23 ns) against a 50 ns cell and a 10BASE-T TX jitter budget of a few ns
  (±100 ppm clock). Conclusion: ship the 2.083 Mbit/s Manchester+CRC-32 frame
  demo with this ceiling documented; 10BASE-T is physically out of reach for
  cycle-quantised firmware edges.

## USB 1.1 low-speed device transmitter — stretch (`usb_ls_tx.asm`)

The competition FAQ lists low-speed USB as a stretch goal and rewards protocols
where the chip does the line coding, CRC and framing itself. This is a
**device→host, transmit-only prototype, proven on the golden model** (not yet in
the RTL suite and not silicon-proven).

* **Pin**: single-ended D+ on `P[0] = uio[0]` (push-pull, DIR bit 0). A real
  board needs the other half of the pair: D− tied to the LS idle level (high)
  and a 1.5 kΩ pull-up on D− (external parts are allowed by the FAQ); the chip
  drives/monitors only D+, which is why the far-end decoder can be single-ended.
* **Frame**: `SYNC (00000001)` → `PID = 0xC3 (DATA0)` → 4 host-supplied data
  bytes → `CRC16` → `EOP (SE0, 2 cell times)` → idle J.
* **Coding**: NRZI (0 toggles D+, 1 holds), bit stuffing after six consecutive
  1s (SYNC never stuffed), USB data CRC16 = reflected poly 0x8005 (0xA001), init
  0xFFFF, final XOR 0xFFFF, computed over the **data field only** and sent
  LSB-first. Validated against the published CRC-16/USB check value
  (`crc16_usb(b"123456789") == 0xB4C8`). The decoder independently checks
  SYNC, PID+complement, stuffing, CRC and EOP.
* **Timing**: one LS bit at the shipped 33.33 MHz is 22.2222 cycles; the event
  scheduler period `setp 0x0164` = 22.25 cycles gives **1.4981 Mbit/s
  (−0.125 %, inside the LS ±0.25 % budget)** and the 12.4 carry keeps every bit
  boundary drift-free. Budget: ~12–14 instructions/bit (worst case 18 with a
  stuff bit) against 22.25 cycles/bit at IPC 1 — every edge is an `emit`, the
  CPU only keeps the 4-deep queue fed. A packet is ≤ 8 + 56 + ≤8 = ≤72 encoded
  bits ≈ 1602 cycles (48 µs), EOP 44.5 cycles, 100 µs idle between packets.
* **Model evidence** (`model_selftest.py`, 13 checks): two host-controlled
  packets decode with SYNC/PID+complement/EOP ok; payloads `FF FF FF FF`
  (**6 stuffed bits**, CRC 0x4FFE) and `LOOM` (0 stuffed, CRC 0x992C); every NRZI
  edge sits on the 22.25-cycle event grid (max residual 0.50 cycle = integer
  cycle quantisation, i.e. no CPU/issue jitter); measured **22.249 cycles/bit →
  1.4982 Mbit/s (−0.122 %)**. `host/usb_ls.py` is the receiver: NRZI decode,
  unstuffing, PID/CRC16/EOP checks and bit-period recovery from the 7 SYNC
  edges (the same information a USB DPLL locks to).
* **Honest verdict**: TX-only, single-ended, model-proven. A real LS device also
  needs the differential transceiver + 1.5 kΩ on D−, a receive path with DPLL
  sampling the far end's NRZI stream (the same logic fits a thread at ~10–14
  instructions/bit vs 22.25 available), and host SOF/EOF enumeration — follow-on
  work, not claimed. What *is* demonstrated is the judged axis: line coding, bit
  stuffing, protocol framing and CRC are done by the chip in firmware.

## Protocol set 2 — JTAG, CAN 2.0A, DMX512, MIDI, NEC IR

Five more pin-level protocol programs, each with an **independent host
decoder** in `host/` that rebuilds the protocol from the pin trace only (the
firmware's captured/ACK bytes are cross-checked where the program reports
them). All five fit the 128-word FF fallback build (16–115 words). Each decoder
also runs standalone, with a synthetic-trace self-check plus the golden-model
frame, as `python3 -m host.{jtag,can,dmx,midi,ir_nec}`.

### JTAG TAP controller — bit-banged (`jtag_tap.asm`, `host/jtag.py`)

* **Pins**: TCK=P[0]=uio[0], TMS=P[1]=uio[1], TDI=P[2]=uio[2] (outputs),
  TDO=P[3]=uio[3] (input; DIR bit 3 stays 0). No external parts beyond the DUT;
  `host/jtag.py` models a 4-bit-IR TAP (IDCODE 0x14B5A5A5 in IR 0x2, BYPASS
  0xF) as the far end.
* **Program (115 words)**: 5 x TMS=1 Test-Logic-Reset -> RTI -> Select-DR/IR ->
  Capture-IR -> Shift-IR (IDCODE instruction 0x2; TDO = Capture-IR status
  0b0001) -> Update-IR -> SelectDR/CaptureDR/ShiftDR (`DR_BITS` bits, default 8;
  32 = full IDCODE readback) -> Update-DR -> halt. TDO is sampled at every TCK
  rising edge into DMEM; the 4+`DR_BITS` captured bits are pushed to the host
  after the transaction, so host FIFO latency can never stretch TCK.
* **Timing**: bit-banged with the 12.4 `waitd` accumulator: TCK high phase is
  exactly 6 cycles on every pulse, TCK period 31..39 cycles (mean 34.8 ->
  **959 kHz**), the few cycles of variation are instruction-issue padding in the
  low phase (JTAG has no maximum TCK rate; setup/hold around the rising edge is
  what matters).
* **Evidence** (`model_selftest.py`): the decoder reconstructs the **exact TAP
  state path** from the pin trace, verifies IR shifted in/latched = 0x2,
  Capture-IR TDO = 0b0001, DR capture = IDCODE, the 8-bit and 32-bit TDO
  readbacks, and cross-checks every firmware-captured bit against the decoder's
  sample.

### CAN 2.0A data frame — firmware CRC-15 + stuffing + ACK (`can_tx.asm`, `host/can.py`)

* **Pins**: TXD=P[10]=uo[2] (recessive 1 / dominant 0), RXD=P[11]=ui[3]. A real
  board needs a CAN transceiver (external parts are allowed by the FAQ); the
  chip drives the transmitter side and samples the ACK slot on RXD.
* **Program (79 words)**: the host writes the frame's SOF..data **bit stream**
  into the TX mailbox (DMEM[0]=bit count, DMEM[1..]=16-bit packed words, LSB
  first) — the way a CAN controller's TX buffer holds the frame — and the
  firmware does the link layer: ISO 11898-1 CRC-15 (generator 0x4599, init 0;
  catalogue check value over "123456789" = **0x059E**) bit by bit, appends the
  15 CRC bits, inserts a complemented stuff bit after five identical bits over
  SOF..CRC sequence, then CRC delimiter, ACK slot (recessive; RXD polled for
  ~1000 cycles and the ACK result pushed to the host), ACK delimiter, 7 x EOF,
  3 x IFS.
* **Timing**: `setp 0x042B` = 66.6875 cycles/bit -> **499.85 kbit/s**
  (-0.03 %); every bit and every stuff bit is one `emit`, so all edges sit on
  the event grid.
* **Evidence** (`model_selftest.py`): golden-model frames 0x123/DLC8
  `"LOOMCAN!"`, 0x7FF/DLC0 and 0x0AA/DLC2 decode **byte-exact** with matching
  stuffing positions (1, 3 and 2 stuff bits), the independent CRC-15 remainder
  over frame+CRC is 0, the far end drives the ACK slot and the firmware reports
  it seen, 66.69 cycles/bit -> 499.8 kbit/s, edge residual <= 0.7 cycles.
  (Transmit only: bus arbitration and RX are not implemented.)

### DMX512 — 250 kbaud, full 512-slot universe (`dmx_tx.asm`, `host/dmx.py`)

* **Pins**: DMX data = P[10]=uo[2], UART 8N2. A real board needs an RS-485
  transceiver (external parts allowed).
* **Program (35 words)**: BREAK held low for 4000 cycles = 120 us (spec
  >= 88 us), MAB high for 400 cycles = 12.5 us (spec >= 8 us), the fixed 0x00
  start-code slot, then 512 data slots pulled from the host RX FIFO, each 11 bit
  cells (start 0, 8 data LSB-first, two stop 1).
* **Timing**: `setp 0x0855` = 133.3125 cycles/bit -> **250.04 kbaud**
  (+0.015 %), one `emit` per bit cell, drift-free over the 513-slot frame
  (~756 kcycles = 22.7 ms).
* **Evidence** (`model_selftest.py`): full-frame decode with BREAK 4004 cycles
  = 120.1 us, MAB 415 cycles = 12.5 us, start code 0x00, all 512 slot bytes
  byte-exact, every slot's start/2-stop framing valid, 133.3125 cycles/bit ->
  250.04 kbaud, slot-start edge residual <= 0.63 cycles.

### MIDI 1.0 — 31250 baud 8N1 (`midi_tx.asm`, `host/midi.py`)

* **Pins**: MIDI TX = P[10]=uo[2]; a real board adds the MIDI current-loop
  output stage.
* **Program (16 words)**: the host pushes raw MIDI bytes; the firmware
  serialises them back-to-back as 8N1 frames (start 0, 8 data LSB-first, stop
  1), blocking on a FIFO pull when idle and chaining frames on the event grid.
* **Timing**: `setp 0x42AB` = 1066.6875 cycles/bit -> **31250.6 baud**
  (+0.002 %).
* **Evidence** (`model_selftest.py`): Note On 0x90 0x3C 0x64 + Note Off
  0x80 0x3C 0x00 + Program Change 0xC0 0x05 -> 8 bytes byte-exact and
  3 messages assembled (1- and 2-data-byte statuses), 1066.686 cycles/bit ->
  31.249 kbaud, start-edge residual <= 0.71 cycles.

### NEC IR remote — scheduler-synthesised 38 kHz carrier (`ir_nec_tx.asm`, `host/ir_nec.py`)

* **Pins**: IR LED gate = P[10]=uo[2] (high during a carrier burst). A real
  board adds the IR LED + driver transistor.
* **Program (54 words)**: 9 ms carrier MARK, 4.5 ms space, 32 pulse-distance
  bits (address/~address/command/~command, LSB first; each bit = 562.5 us mark
  + 562.5 us space for 0 or 1687.5 us for 1), final 562.5 us mark. The
  **carrier itself is event-scheduler output**: alternating high/low `emit`s
  one half period apart, `setp 0x1B6A` = 438.625 cycles -> **38.00 kHz**
  (-0.01 %); spaces are runs of low events chunked into <= 4095.9375-cycle
  periods (the 12.4 register limit). Host preloads DMEM[0..1] with the 32
  payload bits.
* **Evidence** (`model_selftest.py`): golden-model decode with lead mark
  299581 cycles = 8.987 ms, lead space 150000 cycles = 4.500 ms, 34 marks /
  33 spaces, addr=0x04 cmd=0x08 with correct complements, 16 ones / 16 zeros,
  measured carrier **37.998 kHz** at 50.8 % duty with half-period jitter
  <= 0.63 cycles, bit marks 18860..18861 cycles = 562.4 us. The frame itself
  is 2.27 Mcycles = 68 ms (the CPU feeds the queue and is idle > 99 % of it).

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

loom = loom_model.Loom(); link = ModelLink(loom)
world = WireWorld(link, clock_hz=33_333_333)   # shipped design point
uart = UartBus(world, tx_tid=0, rx_tid=1)        # far end + host helpers
uart.app_send(b"Hello")                          # host -> chip TX firmware
print(uart.wire_recv())                          # decoded from the TX pin
uart.wire_send(b"LOOM"); world.run(20000)        # far end -> chip RX
print(uart.app_recv())                           # chip RX firmware -> host
```

## Target clock (shipped: 33.33 MHz)

The whole firmware set is swept to **33.33 MHz** (`CLOCK_PERIOD` 30 ns, the
hardening design point). If the target clock ever changes again, the examples
fall into two classes:

| kind | examples | action when `CLOCK_HZ` changes |
|---|---|---|
| derived from `.equ CLOCK_HZ` | `uart_tx/rx/loopback` (`BAUD_DIV`), `i2c_master` (`HALF_DIV`), `spi_master` (`QUARTER_CYCLES` is a chosen literal; `CLOCK_HZ` documents the rate) | edit the one `.equ CLOCK_HZ, 33333333` line per file; the timing re-derives |
| absolute time encoded in cycles | `ws2812` (T0H/T1H in cycles), `ps2_target` (35 us/5 us/50 us), `onewire` (20 us/15 us/40 us slot cells) | rescale every `.equ` by `new_hz/33.33e6` (these are 12.4 fixed-point cycle counts, not register values) |
| fixed cycles as robustness margin | `i2c_target` (`waitd 0x1400` = 320 cycles idle-bus resync) | re-check it still exceeds the 100 kHz SCL high phase: `CLOCK_HZ/(2*I2C_HZ)` cycles (at 33.33 MHz: 320 vs 166.7, 1.92x) |
| clock-independent | `spi_slave`, `i2c_target` bit loops (edge-polled; external master owns the clock) | none (max external clock margins in the tables scale with the clock) |

All UART baud rates keep their nominal value automatically (`BAUD_DIV` is
computed from `CLOCK_HZ`), and the 12.4 fixed-point period register limit
(`<=4095.94` cycles per cell, `CELLS` splits slower rates) is unchanged.

`sw/examples/model_selftest.py` and `host/demos/*` take their clock from
`CLOCK_HZ` (and the manifest); the far-end helpers derive every microsecond
constant from `world.clock_hz`, so a future change is a one-line edit plus
re-running the 161 checks and the RTL protocol suite.

## Known limits / next steps

* UART at 115200 is the fast case; the 12.4 period limit means slow rates are
  split into `CELLS` cells (implemented), and the 4-thread RX margin was
  measured on the model, not silicon.
* SPI master full-duplex is ~1.03 MHz shipped (32 cyc/bit at 33.33 MHz),
  1.63 MHz at the bit-banged ceiling; 10 MHz needs the wave player with
  precomputed patterns.
* `i2c_master.asm`/`i2c_target.asm` use the `CSRW TPC` computed jump; the RTL
  now invalidates the prefetched instruction, and both pass in cocotb (100 kHz
  controller + clock stretching + target register write/read; 400 kHz passes
  once the VIP emits legal low-phase-only stretching — see VER-006).
* On-chip capture/record-replay is a stretch feature; `host/demos/
  demo_record_replay.py` shows the model-level record/replay flow that the
  capture engine will make on-chip once `loom_capture.v` lands.
* `usb_ls_tx.asm` is a **stretch prototype**: device→host TX only, single-ended
  D+ (external D− tie + 1.5 kΩ are board parts), golden-model-proven; RX/DPLL
  and host enumeration are not implemented.
* `usb_ls_rx.asm` is **RX decode + ACK only** (golden-model verified): the
  ACK is symbol-correct but ~198 bit times late (not USB real-time compliant),
  and the enumeration state machine is not implemented; it needs the SRAM-1024
  build (357 words) and must run single-thread/host-quiet while sampling.
* `eth_tx.asm` is a **10 Mbit/s-class Manchester framing demo, not 10BASE-T**:
  2.083 Mbit/s measured, ~3.0 Mbit/s practical firmware ceiling; true 20 Mbaud
  needs 2.4× the chip's instruction throughput and ns-class edge jitter (see the
  Ethernet section for the numbers).
* External rise times (I2C pull-ups, UART/SPI drivers) are the user's board
  responsibility; the firmware guarantees the 30–70 % point timing.

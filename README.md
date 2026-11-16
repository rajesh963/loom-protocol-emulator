![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# LOOM: a programmable protocol loom

**LOOM is a protocol emulator**: four programmable threads, a hardware event scheduler, and exact
timing. A thread does not toggle pins — it enqueues `(time, pin value)` events into a 4-deep hardware
queue, and the scheduler applies each one on the exact 60 MHz cycle it is due. The instruction stream
can branch, stall on a host FIFO, or wait for another protocol without moving an edge, and four
threads run four protocols concurrently on 19 pins. Nothing is hard-wired: a protocol is a program
loaded over SPI after the chip is fabricated, so the same silicon is a UART, an SPI controller *and*
target, an I2C controller, a PS/2 host, a 1-Wire master, a WS2812 driver — or something invented next
year. Discrete logic blocks would have needed a mask; LOOM needs an assembler.

```
                          ┌──────────────────────────────────────────────────────┐
                          │  tt_um_rajesh963_loom      IHP CMOS5L 8x4            │
                          │  60 MHz · 19 protocol pins · ~1.0 mm²                │
  ┌──────────┐  SPI      ├──────────────────────────────────────────────────────┤
  │ host MCU │  mode 0    │  ┌─────────────┐   ┌────────────┐                    │
  │ (RP2350) │  ≤10 MHz   │  │ host bridge │   │ 4 threads  │                    │
  │  loads   │ ─────────▶ │  │ 13 commands │──▶│ 1 per cycle│                    │
  │  program │ ◀───────── │  │ 8×8 FIFOs   │   │ 4 × R0-R7  │                    │
  │  streams │ MISO/IRQ   │  └──────┬──────┘   │ PERIOD 12.4│                    │
  └──────────┘            │         │          └──────┬─────┘                    │
                          │         │   ┌───────────────┘                        │
                          │         ▼   ▼                                        │
                          │  ┌──────────────┐  ┌──────────────┐   ┌────────┐     │
                          │  │ PMEM 1024×32 │  │ 4× event     │   │ wave   │     │
                          │  │ DMEM 64×16   │  │ schedulers,  │   │ player │     │
                          │  └──────────────┘  │ depth 4 each │   └────────┘     │
                          │                    └──────┬───────┘                  │
                          │   priority: t0 > t1 > t2 > t3 > OUT, wave overrides  │
                          │                    ▼                                 │
                          │  19 pins: ui[7:3] in · uo[7:2] out · uio[7:0] bidir  │
                          └──────────────────────────────────────────────────────┘
```

Read [`docs/writeup.md`](docs/writeup.md) for the argument, [`docs/architecture.md`](docs/architecture.md)
for how it works, [`docs/isa.md`](docs/isa.md) for the instruction set, and
[`docs/examples.md`](docs/examples.md) for protocols with verified code.

## The competition

This is an entry to the [Jane Street × Tiny Tapeout protocol emulator ASIC
competition](https://blog.janestreet.com/protocol-emulator-asic-competition/): design an
open-source, general-purpose, *reprogrammable* protocol emulator, harden it through the Tiny Tapeout
flow on IHP 130 nm CMOS5L, maximum 8×4 tiles, and submit by **2027-01-18** for the March 2027
shuttle. Start with UART, SPI and I2C; stretch goals are low-speed USB and 10 Mbit Ethernet. The
judging is explicitly about *flexibility* — can the chip be reprogrammed to support protocols it was
not designed for — plus how the design was verified and how well the write-up explains it.

## Why this is not another PIO or PRU

Both RP2040 PIO and TI PRU make a pin transition *be* an instruction. LOOM makes the pin transition a
*scheduled event* and lets the CPU get on with the program:

| | RP2040 PIO | TI PRU | **LOOM** |
|---|---|---|---|
| timing source | SM instruction issue, 16.8-divided SM clock | instruction issue at 200 MHz | hardware comparator against a 16-bit cycle counter |
| fractional baud | yes (16.8 divider — average rate exact) | no (cycle-counted loops) | yes — 12.4 per-thread period, drift-free carry, per **edge** |
| edge-triggered events | no | no | **yes** — `emite` fires on a pin-pattern match in hardware |
| timed sampling | no | no | **yes** — `inat` latches pins on a chosen cycle |
| wave player | no (DMA→FIFO is host-paced) | no | **yes** — DMEM patterns at a 12.4 period, 60 M patterns/s |
| host streaming | 4-deep FIFOs + DMA | shared memory / RPMsg | 8-byte FIFOs per thread, SPI bridge with IRQ, no DMA needed |
| program size | 32 words shared per 4 SMs, per block | 8 KB IRAM | 1024 words, relocatable, 4 threads |
| what it is worst at | code-shaped timing | code-shaped timing + clock rate | small memory, no interrupts |

The defensible claim is not raw toggle rate — a 133 MHz PIO wins that — it is that **timing belongs
to the schedule, not to the code**: adding a feature, restructuring a loop, loading another protocol
on another thread, or a slow host cannot move an edge. See
[`docs/writeup.md` §9](docs/writeup.md) for the honest limitations.

## Features

* **4 threads**, one instruction/cycle shared issue slot, work-conserving round robin; a stalled
  thread never blocks the others and never affects an edge.
* **Event scheduler per thread**, depth 4, four kinds: periodic (`emit`), relative (`emitd`),
  absolute (`emita`), edge-triggered (`emite`).
* **12.4 fixed-point periods** (1/16 cycle = 1.042 ns at 60 MHz) with a carry accumulator —
  UART 115200 is a +0.0046 % baud error that does not drift.
* **Timed sampling** (`inat`/`waitu`/`waitd`) with a fractional-delay chain that cannot drift.
* **Wave player**: 1–64 patterns of 16 pin bits, 12.4 period, loop/one-shot, up to 60 M patterns/s.
* **19 protocol pins** — 14 drivable, 13 readable, 8 true bidirectional with per-pin `DIR` for
  open-drain (I2C/PS/2/1-Wire without external buffers).
* **SPI host bridge**: mode 0, ≤ 10 MHz, 13 commands, 24-bit address space, level IRQ,
  per-thread 8-byte FIFOs.
* **Reset-complete, single clock, synthesizable Verilog-2005** with no vendor primitives except the
  optional IHP SRAM macro.
* **Two build variants**: 128-word FF PMEM (zero flow risk) and 1024-word SRAM PMEM (flagship).

## Pinout

The 24 Tiny Tapeout pads: 3 inputs and 2 outputs are the host SPI link, leaving 19 protocol pins.
Names are the example firmware assignment; **every protocol function can be moved to any protocol
pin by software**.

| Pad | Function | Pad | Function |
|---|---|---|---|
| `ui[0]` | `H_CS_N` — host SPI select | `uo[0]` | `H_MISO` — host SPI out |
| `ui[1]` | `H_SCK` — host SPI clock | `uo[1]` | `H_IRQ` — interrupt (level) |
| `ui[2]` | `H_MOSI` — host SPI in | `uo[2]` | `UART_TX` |
| `ui[3]` | `UART_RX` | `uo[3]` | `SPI_CS_N` |
| `ui[4]` | `SPI_CS_IN` (SPI target select) | `uo[4]` | `WS2812_DATA` |
| `ui[5]` | `TRIG_IN` (spare input) | `uo[5]` | `SPARE_OUT` |
| `ui[6]` | `WAVE_IN0` (10BASE-T RX+) | `uo[6]` | `WAVE_OUT0` |
| `ui[7]` | `WAVE_IN1` (10BASE-T RX-) | `uo[7]` | `WAVE_OUT1` |
| `uio[0]` | `I2C_SCL` (open drain) | `uio[4]` | `ONEWIRE` (open drain) |
| `uio[1]` | `I2C_SDA` (open drain) | `uio[5]` | `SPI_SCK` |
| `uio[2]` | `PS2_CLK` (open drain) | `uio[6]` | `SPI_MOSI` |
| `uio[3]` | `PS2_DAT` (open drain) | `uio[7]` | `SPI_MISO` |

`clk` = 60 MHz target (10–66 MHz accepted), `rst_n` asynchronous active low, `ena` ignored. The full
pad map, the instruction-level pin vector and the in/out overlap are in
[`docs/architecture.md` §5](docs/architecture.md#5-pins) and [`docs/images/pinout.svg`](docs/images/pinout.svg).

## Repository layout

```
loom/
├── rtl/            synthesisable Verilog-2005 (the design)
│   ├── loom_core.v        4 threads, scheduler, ALU, event queues, CSRs
│   ├── loom_mem.v         PMEM (FF or IHP SRAM) + DMEM
│   ├── loom_host.v        SPI slave host bridge, 13-command FSM, IRQ latch
│   ├── loom_wave.v        wave player
│   ├── loom_capture.v     capture engine (stretch: unit-verified, silicon integration rev B)
│   ├── loom_fifo.v        FIFO primitive (unit-test helper)
│   ├── loom_bus.vh        FROZEN: opcodes, CSRs, host commands
│   └── tt_um_rajesh963_loom.v   top level, TT pin mapping
├── src/            files compiled by the Tiny Tapeout flow (mirror of rtl/ + config.json)
├── sw/             assembler (loom_asm.py), golden ISA model, ISA tables (isa.py), program gen
├── host/           Python host-protocol client + MicroPython/PIO reference snippets  [in progress]
├── test/           iverilog/cocotb benches: unit_core, unit_host, unit_wave, integration, VIPs
├── formal/         bounded proofs (yosys write_smt2 + z3)                             [in progress]
├── tools/          sync_src.sh, area_synth.sh, helpers                               [area script in progress]
├── docs/           datasheet, architecture, ISA, host protocol, examples, verification, write-up
├── info.yaml       Tiny Tapeout project metadata (8×4, 60 MHz, source_files, pinout)
└── spec/           the frozen architecture/interface spec and ownership map
```

## Quickstart

Requires `iverilog` (Icarus 12) and Python 3 (stdlib only for the toolchain; `../.venv/bin/python`
has cocotb/yosys for the fuller flow).

```sh
# 1. ISA tables and the assembler's self-test
python3 -m sw.isa

# 2. assemble any protocol example (sources in docs/examples_src/, more in sw/examples/)
python3 -m sw.loom_asm docs/examples_src/uart_tx.asm -o uart_tx.hex \
        --format hex32 --rom-words 1024 --fill 0x02000000 --lst uart_tx.lst --sym uart_tx.sym

# 3. run the self-checking RTL benches
bash test/unit_core/run.sh        # two concurrent periodic streams, exact intervals
bash test/unit_host/run.sh        # all 13 host commands at 10 MHz and 100 kHz SCK
bash test/unit_wave/run.sh        # wave player pattern walk / loop / done
bash test/integration/run.sh      # host loads PMEM, runs a thread, 16-cycle square wave

# 4. drive the chip from a host: the reference client lives in host/ (in progress) and
#    speaks the byte protocol specified in docs/host-protocol.md §1-§4; test/host.py is the
#    cocotb-side driver used by the benches above.
```

Protocols are demonstrated by assembling and running a program: `docs/examples_src/uart_tx.asm`
plus the host loader gives a 115200-baud frame on `uo[5]` (the integration bench already does the
equivalent end-to-end run with a 16-cycle square wave).

The end-to-end story — load a program, start a thread, watch a protocol appear on a pin — is in
[`docs/host-protocol.md` §5](docs/host-protocol.md#5-captured-wire-traces).

## Verification

Landed RTL, `make` from `test/` (Icarus Verilog 12 + cocotb): **59 tests, 59 PASS, 0 FAIL, 0 SKIP** —
154 s on the SRAM tape-out configuration (`make SRAM_SIM=1024`), 157 s on the FF regression
configuration — and the `test` GitHub Actions workflow reproduces that matrix on every push, so these
claims are CI-verifiable. Two independent implementations — hand-written RTL and a
Python golden model written from the spec — agree on the exact cycle of every pin transition and on
the full architectural state for 100/100 random programs. The two I²C gates that were skipped during
bring-up are now closed: VER-007 was a real firmware bug (SCL-first byte boundary) and VER-006 a VIP
artifact (illegal clock-stretch injection).

| Suite | Result | What it proves |
|---|---|---|
| host link | **PASS** | all 13 commands, global-CSR read matrix, FIFOs, IRQ/ACK, CS-abort, STATUS |
| ISA | **22/22 PASS** | every mnemonic; coverage meta-test covers all 59 tests |
| timing | **7/7 PASS** | drift-free fractional stream, UART 115200 @ 60 MHz, `waitu` jitter-free, `inat` exact-cycle sample, `emite` latency, 4 concurrent periods, queue back-pressure (`max_eq = 4`) |
| differential co-sim | **PASS** | 100/100 random programs cycle-exact on the full `(pins_out, DIR)` trace **and** architectural state; 12/12 pull/reactive seeds; 6/6 hand-written; 0 artefacts |
| protocol VIPs | **8/8 PASS** | UART TX/RX, SPI modes 0–3, SPI target, I2C 100 kHz + 400 kHz + clock stretching, I2C target |
| formal | **3/3 PASS** | bounded proofs, `yosys write_smt2` + `yosys-smtbmc -s z3`: FIFO/queue k=8, core reset+event k=6, host command ordering k=6 |
| gate-level | pending | awaits the attempt-4 netlist |
| sign-off (`gds`/`precheck`) | **attempt 4 running** | area solved (622 401 µm² = 51.5 %, −61 %); attempt 3 failed a GPL routability-mode check, fixed by one config line — see [flow status](docs/FLOW_STATUS.md) |

The bring-up caught **sixteen real deviations** worth reading as evidence the methodology works,
including a host global-CSR read-path bug (VER-003), unmasked `pins_out[9:8]` (VER-001), a three-way
a computed-jump/`pc+1` bug where the golden model was right and the RTL wrong (VER-000), a `TFIFO`
layout disagreement (VER-002), an undriven DMEM address (VER-004), an `INAT` register-bank
truncation (VER-013) and peripheral event-chain pollution (VER-014) — all closed with regressions.
Detail in [`docs/verification.md` §9](docs/verification.md#9-results), including how the two I²C gates
closed and why one was a design bug and one a test bug.

**Flow status, reported as measured — the area problem is solved.** Three hardening attempts on the
FF (flop-array) build failed on area (1 147 604 / 1 190 170 / 1 223 970 µm² against an 8×4 budget of
≤ ~750 k µm²), and the probes were informative: relaxing `MAX_FANOUT_CONSTRAINT` and dropping the
clock to 25 MHz both made it *worse*, localising the 305 304 µm² of buffers (27 % of the design) to
ABC's SCL sequential-mapping pass — constraint knobs are a dead end, structure is the lever.
**Attempt 3 (SRAM 1024×32 macro + 32-word DMEM) measured 622 401 µm² = 51.5 % area ratio / 44.7 %
flow utilisation — a −61 % area reduction** — and failed only a placement-*mode* check
(`[GRT-0010]` inside GPL's routability-driven mode), which **attempt 4 fixes with one config line
(`PL_ROUTABILITY_DRIVEN: false`) and is running now**. Both build configurations pass the full
**59/59 simulation matrix (0 skips)**. Nothing is marked passing until GDS + precheck + gate-level
all pass on one commit. Canonical log: [`docs/FLOW_STATUS.md`](docs/FLOW_STATUS.md); tables in
[`docs/results.md`](docs/results.md).

## Documentation

| Document | Contents |
|---|---|
| [`docs/info.md`](docs/info.md) | Tiny Tapeout datasheet: how it works, how to test, pinout, CSR summary |
| [`docs/writeup.md`](docs/writeup.md) | the competition write-up: problem, insight, timing math, results, comparison, AI workflow |
| [`docs/architecture.md`](docs/architecture.md) | beginner-friendly architecture: scheduler semantics, memory, area strategy |
| [`docs/isa.md`](docs/isa.md) | full ISA reference, CSR map, assembler syntax, worked UART tutorial |
| [`docs/host-protocol.md`](docs/host-protocol.md) | byte-level SPI protocol with captured wire traces |
| [`docs/examples.md`](docs/examples.md) | per-protocol walkthroughs with timing budgets and verified code |
| [`docs/verification.md`](docs/verification.md) | verification methodology and results tables |
| [`docs/results.md`](docs/results.md) | area, timing, sign-off |
| [`docs/FLOW_STATUS.md`](docs/FLOW_STATUS.md) | canonical LibreLane attempt log (what failed, measured numbers, diagnosis, fix) |
| [`docs/SUBMISSION_CHECKLIST.md`](docs/SUBMISSION_CHECKLIST.md) | what must be true before submitting |
| [`spec/ARCHITECTURE.md`](spec/ARCHITECTURE.md) | the frozen architecture and interface specification |
| [`docs/images/`](docs/images/) | hand-written SVG diagrams (block, pinout, timing, event queue) |

## How we used AI tools

The competition asks for this to be documented, so: the design was built by an **AI agent team**
(DeepSeek Harness) under human direction, starting from a human-frozen specification
(`spec/ARCHITECTURE.md` + `rtl/loom_bus.vh`) that every artefact — RTL, assembler, ISA model, host
client, tests, this README — was generated against by separate agents with per-file ownership
(`spec/OWNERS.md`). AI did the broad, mechanical layers well: testbench scaffolding, the assembler
and relocation records, the golden model, the 13-command host client, hand-written SVG diagrams and
first drafts of every document. It also produced confidently wrong details — a 6-bit opcode field
that silently turned `jmp` into `nop`, CSR bit diagrams wider than a 16-bit read, an ambiguous
`waitp` encoding — all caught by cross-checking artefacts against each other and fixing the source
of truth, never the symptom. The discipline that made the collaboration productive is the same one
that makes the verification credible: derive documents from machine-readable sources, execute every
example before publishing it, and never let a claim exist without an artefact. The full account is
in [`docs/writeup.md` §10](docs/writeup.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE). The design is open source so others can build on it
(the competition requires this). Tiny Tapeout tooling and the IHP PDK carry their own licenses.

## Links

* [Tiny Tapeout](https://tinytapeout.com/) · [protocol emulator competition](https://blog.janestreet.com/protocol-emulator-asic-competition/) · [IHP CMOS5L](https://www.ihp-microelectronics.com/)
* [`docs/writeup.md`](docs/writeup.md) — start here if you read one document.

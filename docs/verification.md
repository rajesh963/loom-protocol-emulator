# LOOM verification

**What this is.** How LOOM is proven, layer by layer: a golden ISA model written from the
specification rather than from the RTL, differential co-simulation of random programs, protocol
VIP conformance with ASCII waveform dumps, bounded formal proofs, gate-level simulation of the
hardened netlist, and a coverage matrix. It names the exact command that reproduces each layer and
keeps a results table whose cells are honestly marked TBD until the artefact is green in CI. This
document is a judged artefact: the *method* is the claim, the TBDs are placeholders that will be
filled with real numbers before the 2027-01-18 deadline.

The design is small enough to verify exhaustively-ish but subtle enough that "it simulates" proves
nothing: timing bugs are off-by-one-cycle, queue bugs are occupancy races, and the host bridge is a
clock-domain crossing. Each of those has its own layer below.

---

## 1. The pyramid

```
                    ┌───────────────────────────────────────┐
   coverage  ──────▶│ L6  coverage matrix: opcode × CSR × th │
                    │     × pin, 4 concurrent threads       │
                    ├───────────────────────────────────────┤
                    │ L5  gate-level sim of hardened netlist │
                    │     (LibreLane GL netlist, 60 MHz)     │
                    ├───────────────────────────────────────┤
                    │ L4  formal: bounded proofs (yosys SMT2 │
                    │     + z3)                             │
                    ├───────────────────────────────────────┤
                    │ L3  protocol VIP conformance + ASCII   │
                    │     waveform dumps (cocotb)           │
                    ├───────────────────────────────────────┤
                    │ L2  differential RTL vs golden model   │
                    │     on random programs                │
                    ├───────────────────────────────────────┤
                    │ L1  unit tests: ISA, FIFO, host FSM,   │
                    │     wave player, assembler            │
                    └───────────────────────────────────────┘
```

Two rules hold the pyramid together:

1. **Model first, from the spec.** `sw/loom_model.py` is written from `spec/ARCHITECTURE.md` and
   `sw/isa.py`, not from `rtl/`. If the model and the RTL disagree, the disagreement is a design
   decision to be made deliberately — never "fix the model to match the RTL".
2. **A layer is either green in CI or marked TBD here.** No prose claims a pass that has no
   artefact behind it.

---

## 2. L1 — unit tests

| Unit | What it proves | Location |
|---|---|---|
| ISA encode/decode | every mnemonic's field mapping round-trips; the assembler and disassembler agree | `sw/isa.py` self-test, `test/unit_sw/` |
| All-op smoke program | each opcode executes and writes the documented architectural state | `test/unit_sw/all_ops.asm` |
| ALU / flags | `Z`/`C` semantics of `add/sub/cmp/and/…` including 16-bit wrap | `test/unit_core/` |
| FIFO | full/empty/count, simultaneous read+write, fall-through read | `test/unit_host/tb_fifo.v` |
| Host bridge FSM | every command's byte count, MISO during each transfer, CS abort | `test/unit_host/tb_host.v` |
| Wave player | pattern walk, loop/one-shot, `done`, fractional period, DMEM read timing | `test/unit_wave/tb_wave.v` |
| Event queue | enqueue/fire races, depth-4 wrap, same-cycle enqueue+fire occupancy | `test/unit_core/` |

### 2.1 What is green right now (RTL, Icarus Verilog 12)

These runs exist and pass in the repo today (Icarus Verilog 12); they are the first rows of the
results table in §9 and the seed of everything above them.

| Bench | Command | What it proves |
|---|---|---|
| `CORE_TB` | `test/unit_core/run.sh` | two threads running concurrently produce **exact 16-cycle and 8-cycle edge streams** through event-queue back-pressure — i.e. the periodic scheduler keeps its promise while the queue fills and drains |
| `ITEST` | `test/integration/run.sh` | a bit-banged SPI host loads **7 instructions** into PMEM, reads them back, sets `PC` and starts a thread; the event scheduler then emits a square wave on `uo_out[2]` with **25 edges, every interval exactly 16 cycles, 0 errors** |
| `HOST_TB` | `test/unit_host/run.sh` | all 13 commands at 10 MHz and at 100 kHz SCK: byte framing, MISO per transfer, CS-abort, IRQ latch; the trace in [`host-protocol.md`](host-protocol.md) §5 is its output |
| `WAVE_TB` / `CAPTURE_TB` | `test/unit_wave/run.sh` | wave player pattern walk, loop/one-shot, fractional period, `done`; capture engine (stretch) with 209 checks |

Reproduce:

```sh
python3 -m sw.isa                                   # ISA self-test + tables
bash test/unit_core/run.sh                          # CORE_TB
bash test/integration/run.sh                        # ITEST
bash test/unit_host/run.sh                          # HOST_TB (regenerates build/tb_host.log)
iverilog -g2012 -I rtl -o /dev/null rtl/*.v                # lint: no implicit nets
```

## 3. L2 — differential co-simulation on random programs

The random program generator (toolchain-owned `sw/gen_program.py`; the seed generator used today is
`test/unit_core/gen_prog.py`) produces constrained-random programs (all opcodes, biased toward the
interaction cases that break timing engines: `emit`/`emitd`/`emita` interleaved, queue-full
back-pressure, `waitu`/`inat` with wrap-around absolute times, FIFO stalls, `rpt` over
multi-cycle instructions). Each program runs twice — RTL (Icarus) and `sw/loom_model.py` — and the
harness compares, at every retirement boundary and every clock edge of interest:

* exact **pin transition list** `(cycle, pins_out, pins_dir)` — the waveform, not a checksum of it;
* `TMR`, and the per-thread `PC`, `R0..R7`, `Z`, `C`, `stall`, `NEXTT`, `PFRAC`, queue occupancy;
* FIFO contents and counts;
* the final CSR image.

The comparison is on *cycle numbers of transitions*, so a one-cycle scheduling error is a failure.
This layer is the workhorse: it catches integer-width and priority bugs that unit tests cannot.

```sh
python3 -m sw.gen_program --seed 1 --n 200 --out test/random/
python3 -m pytest test -k differential --seed 1        # intended entry point (verify owner)
```

## 4. L3 — protocol VIP conformance

Independent cocotb reference models (VIPs) for UART, SPI (all four modes) and I2C check the
protocol rules rather than the implementation:

| Protocol | Checks |
|---|---|
| UART | bit-centre sampling within ±2 % over 10 000 frames, start/stop framing, idle level, back-to-back frames, break detection, 9600…921600 Bd, both directions |
| SPI controller | CPOL/CPHA for modes 0–3, MOSI setup/hold vs SCK, MSB-first, CS setup/hold, 8/16-bit transfers, 1–10 MHz SCK |
| SPI target | responds at the right edge, MISO tri-states off-select, no spurious clocks |
| I2C controller | START/STOP conditions, 9th-clock ACK sampling, clock at 100 ± 5 kHz / 400 ± 20 kHz, no SDA transition while SCL high except START/STOP |
| 1-Wire | reset 480 µs, presence sampled in its window, write-1/0 slot widths, recovery times |
| WS2812 | T0H/T1H/period and reset-low width within ±150 ns |
| PS/2 | samples on falling edges, parity/stop acceptance |

**ASCII waveform dumps** are the debugging tool that makes failures reviewable in a pull request
rather than a GUI session. Every VIP failure prints the surrounding window as text, so a reviewer
can see the bug in the diff without opening GTKWave — an idea borrowed from Jane Street's own
[average of ASCII waveform testing](https://blog.janestreet.com/using-ascii-waveforms-to-test-hardware-designs/):

```
   cycle  ...  520   1041  1562  2083  2604  3125  3646  4167  4688  5209
   TMR    ...  520   1041  1562  2083  2604  3125  3646  4167  4688  5209
   UART_TX     1     0     1     0     1     1     0     1     0     1
   expected    1     0     1     0     1     1     0     1     0     1     PASS
   error[cyc]  0     0     0     0     0     0     0     0     0     0
   bit cell     stop  b0    b1    b2    b3    b4    b5    b6    b7    stop
```

A failing trace prints `!` at the first divergent cycle and the ±3 neighbouring cycles, so the
reader can distinguish "one cycle late" from "wrong bit" instantly.

## 5. L4 — formal properties

Bounded model checking with yosys `write_smt2` + z3 (`formal/`, owner: verify). Properties, with the
bound that makes them meaningful:

| # | Property | Why it matters |
|---|---|---|
| F1 | **Reset completeness**: after `rst_n` low for ≥ 2 cycles, every architectural flop has a defined value and no X | the ASIC has no FPGA initial values |
| F2 | **Event queue never drops an armed event**: an event enqueued is eventually fired or is still queued, never silently lost | the core timing promise |
| F3 | **Event fires at its scheduled time**: `kind=time` events fire in the cycle `TMR == t`, for all bounded schedules | bit-exactness |
| F4 | **FIFO discipline**: `push` when full and `pull` when empty stall; `count` never underflows or exceeds 8; head/tail point only into the FIFO | no byte loss |
| F5 | **No out-of-bounds host write**: `SET_ADDR`+`WRITE_MEM` cannot write outside the 1024-word PMEM / 64-word DMEM map | a hostile host cannot corrupt the core |
| F6 | **`pins_out` changes only** on a scheduled event fire, an explicit pin op, a CSR `POUT` write, or an active wave-player pattern | no spurious edges |
| F7 | **IRQ is never spurious**: `H_IRQ` high implies CSR `IRQ != 0`; it clears only on `ACK_IRQ` | host wake-up correctness |
| F8 | **Scheduler priority**: two threads scheduling the same pin in one cycle resolve to `t0` | documented determinism |
| F9 | **WCTRL start is a one-shot**: writing `start=1` starts exactly one burst; `loop`/`oneshot` are mutually exclusive | wave player control |

```sh
tools/formal.sh                 # intended: yosys write_smt2 -> z3 for F1..F9 (verify owner)
```

## 6. L5 — gate-level simulation

The Tiny Tapeout flow's `gl_test` job runs the cocotb testbench against the **hardened netlist**
(`GATES=yes`, `gate_level_netlist.v` from the LibreLane run) with the PDK cell models and the same
protocol suite. This catches things RTL simulation cannot: inferred latches, clock-tree effects on
reset, and the occasional tool mis-elaboration. The gate-level run is the last line of defence
before tapeout and its result is a required submission artefact.

```sh
make -C test GATES=yes              # after the GDS action copies in gate_level_netlist.v
```

## 7. L6 — coverage

The coverage model is a cross-product, closed in Python from the same random programs plus the
protocol suite:

| Dimension | Bins |
|---|---|
| opcode | all 55 defined opcodes executed ≥ 1 (and the undefined ones retire as `nop`) |
| CSR | every CSR read and written, per-thread bank and global, host and thread side |
| thread | each thread active alone, and all 4 concurrently with interleaved protocols |
| pin | each of the 19 protocol pins driven and read (including `uio` open-drain release) |
| event kind | `time` (`emit`/`emitd`/`emita`) and `edge` (`emite`) each fired |
| back-pressure | queue-full stall, TX-full stall, RX-empty stall each hit |
| host command | all 13 commands, including CS-abort mid-command |
| wrap | an event scheduled across the `TMR` 0xFFFF→0x0000 wrap |

Coverage is reported as (hits / bins) per dimension and as a cross-coverage percentage; the table in
§9 has the cells to fill.

---

## 8. What we deliberately do not claim

* **No post-silicon results.** Nothing here is measured silicon data; results tables distinguish
  simulation/formal/flow from hardware.
* **No protocol-completeness claims.** "UART" means 8N1 with configurable baud; framing-error
  *recovery* is firmware's job, not the hardware's.
* **No analog/electrical verification.** IO drive strength, pad timing and signal integrity are the
  Tiny Tapeout pad ring's and the board's; we only verify digital behaviour at the pin vector.
* **The host link is verified at ≤ 10 MHz SCK.** Above `clk/6` the synchroniser can miss edges; that
  is documented as a host requirement, not defended against.
* **Stretch features** (capture/record-replay, USB LS, 10BASE-T) are verified only if they ship;
  their rows stay TBD until then.

---

## 9. Results

> Filled in from CI and the hardened build before submission; every cell is a link to the artefact
> that produced it. Git revision: `TBD`.

### 9.1 Layer status

| Layer | Suite | Cases | Pass | Fail | Artefact |
|---|---|---|---|---|---|
| L1 unit | ISA + assembler | TBD | TBD | TBD | `test/unit_sw/` |
| L1 unit | core: two concurrent periodic streams + queue back-pressure | PASS | PASS | 0 | `test/unit_core/run.sh` |
| L1 unit | FIFO + host FSM (13 commands, 10 MHz & 100 kHz SCK) | PASS | PASS | 0 | `test/unit_host/build/tb_host.log` |
| L1 unit | wave player + capture | PASS | PASS | 0 | `test/unit_wave/` |
| L1 integration | host loads PMEM, sets PC, runs thread → 25-edge square wave, all intervals 16 cycles | PASS | PASS | 0 | `test/integration/` |
| L2 differential | random programs × 4 threads | TBD | TBD | TBD | `test/random/` |
| L3 protocol VIP | UART/SPI/I2C/1-Wire/WS2812/PS-2 | TBD | TBD | TBD | `test/integration/` |
| L4 formal | F1–F9 bounded proofs | 9 | TBD | TBD | `formal/` |
| L5 gate-level | protocol suite on hardened netlist | TBD | TBD | TBD | `test/results.xml` |
| L6 coverage | dimension + cross coverage | TBD | TBD | TBD | `test/coverage.json` |

### 9.2 Coverage

| Dimension | Bins | Hit | % |
|---|---|---|---|
| opcode | TBD | TBD | TBD |
| CSR | TBD | TBD | TBD |
| thread concurrency | 4 | TBD | TBD |
| protocol pins | 19 | TBD | TBD |
| event kinds | 2 | TBD | TBD |
| host commands | 13 | TBD | TBD |
| TMR wrap | 1 | TBD | TBD |
| **cross coverage** | TBD | TBD | TBD |

### 9.3 Open issues at submission

| # | Issue | Severity | Owner |
|---|---|---|---|
| 1 | TBD | TBD | TBD |

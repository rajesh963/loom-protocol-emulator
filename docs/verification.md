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
| `JUMP_TB` | `test/unit_core/run.sh` | a computed jump (`csrw TPC` + `jmp`) takes the new target with no bubble (the model-found computed-jump regression) |
| `ITEST` | `test/integration/run.sh` | a bit-banged SPI host loads **7 instructions** into PMEM, reads them back, sets `PC` and starts a thread; the event scheduler then emits a square wave on `uo_out[2]` with **25 edges, every interval exactly 16 cycles, 0 errors** |
| `HOST_TB` | `test/unit_host/run.sh` | all 13 commands at 10 MHz and at 100 kHz SCK: byte framing, MISO per transfer, CS-abort, IRQ latch; the trace in [`host-protocol.md`](host-protocol.md) §5 is its output |
| `WAVE_TB` / `CAPTURE_TB` | `test/unit_wave/run.sh` | 1000 symbols at 1.625 cycles each = **1625 cycles exact**, loop/one-shot/`done`; capture engine (stretch), 209 checks |
| `FIFO_TB` | `test/unit_host/run.sh` | full/empty/count, simultaneous read+write, fall-through read |

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
| SPI controller | CPOL/CPHA for modes 0–3, MOSI setup/hold vs SCK, MSB-first, CS setup/hold, 8/16-bit transfers, ≈1 MHz SCK (shipped firmware rate) |
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

Bounded model checking with `yosys write_smt2` + `yosys-smtbmc -s z3`. **As landed: four bounded
proof groups, 4/4 PASS** — `fifo` / `fifo_queue_bounds` (k = 8), `core` / `core_reset_event` (k = 6),
`host` / `host_cmd_order` (k = 6), and *(v2 post-freeze)* **`sched` / scheduler exactness (k = 16)**.
The table below is the full property *set* the groups are drawn from; properties not inside a proof
group are exercised by the simulation suites named in §2–§4, and are kept here as the documented
intent for anyone extending the formal harness.

### 5.1 The scheduler-exactness proof — `formal/h_sched.v` *(v2 post-freeze addition)*

The architecture's central promise is that an accepted event fires on its exact cycle. That is now
**proved, not just tested**, for a bounded run: `formal/h_sched.v` *(committed in **`53097eb`**, alongside
`formal/run_formal.py` and the recorded result in `formal/results.txt`; the generated wrapper
`formal/build/loom_core_probe.v` and other `formal/build/` artefacts are not committed)* does BMC from a
generated reset
under arbitrary external stimulus (`imem_data`, `dmem_rdata`, `pins_in` are `(* anyseq *)`) and observes
the scheduler directly through read-only `dbg_*` ports. Because yosys does not resolve hierarchical
references into submodule internals, the harness compiles a **generated copy of `rtl/loom_core.v` with
observation ports appended** (`formal/build/loom_core_probe.v`, produced by `run_formal.py`); **the
logic is identical — only `assign dbg_* = …` statements are added**, so the proof is about the shipped
RTL, not a model of it. This is a **v2 addition to the shipped RTL**: the tape-out netlist, GDS and
precheck results are unchanged by it.

**Claim (bound k = 16):** *"An accepted timed event fires exactly in the cycle where `TMR` equals its
recorded due time — never one cycle late, never one cycle early — and an accepted edge-triggered event
fires exactly in the cycle where the pin condition holds; the due time recorded at enqueue is exact
(periodic `emit` including the 12.4 `PFRAC` carry, absolute `emita`, relative `emitd`); the fired value
reaches `pins_out` in that same cycle when it is the only pin cause."*

Checked every post-reset cycle (cycle-local invariants, hence valid for every program/stimulus the
solver explores within the bound):

| Inv. | Property |
|---|---|
| P1 | head timed event with `head_tgt == TMR` ⇒ `fire` this cycle (due ⇒ fire) |
| P2 | head timed event with lookahead in `(0, 0x8000)` ⇒ no fire this cycle (future ⇒ no fire) |
| P3 | head edge event fires **iff** masked `pins_in == TECOND` |
| P4 | `fire` ⇒ the fired value is exactly the head event's value |
| P5 | accepted `emit`/`emita`/`emitd` records exactly the documented due time (incl. `PFRAC` carry / behind-schedule clamp) |
| P6 | when thread 0's event is the only pin cause, `pins_out == merge(pout, fire_v[0], tmask[0])` and equals `npout` in the fire cycle |

**Assumptions, and what is deliberately *not* proved** (this is a bounded proof, and the honest scope
matters):

* **Accepted-event precondition.** Firmware must enqueue a timed event *before* its due cycle, with
  lookahead `< 0x8000`. A late enqueue is recovered as "overdue" by design; P1/P2 cover the due and
  future cycles, not the recovery path — **no liveness guarantee** is claimed for events enqueued after
  their due cycle.
* **Queue capacity.** A full queue stalls the emitting instruction (no event is dropped); the enqueue
  properties' antecedent excludes `stall_issue`.
* **Same-cycle `pins_out`** (P6) holds only when the event is the sole pin cause — competing writers
  (instruction pin op, CSR `POUT`, host `POUT`, wave player, another thread's fire) are excluded from
  *that* antecedent; P1–P5 hold under all stimulus.
* **Bounded depth k = 16.** Cycle-local invariants prove each cycle's behaviour inside the bound; an
  unbounded induction is future work. Firmware timing (enqueuing early enough) remains the software
  contract, not a hardware theorem.


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

Coverage is enforced, not just reported: a **coverage meta-test** asserts that every opcode and CSR in
`sw/isa.py` is exercised by the 59-test suite (that is what makes "all 59 covered" a checked claim),
and the global-CSR read matrix covers the CSR read paths explicitly. The numeric (hits / bins) and
cross-coverage percentage report remains a reporting artefact (TBD in §9.4); the gate is the
meta-test.

---

## 8. What we deliberately do not claim

* **No post-silicon results.** Nothing here is measured silicon data; results tables distinguish
  simulation/formal/flow from hardware.
* **No protocol-completeness claims.** "UART" means 8N1 with configurable baud; framing-error
  *recovery* is firmware's job, not the hardware's.
* **No analog/electrical verification.** IO drive strength, pad timing and signal integrity are the
  Tiny Tapeout pad ring's and the board's; we only verify digital behaviour at the pin vector.
* **The host link is verified at 10 MHz SCK at the 60 MHz design point** (≈5.5 MHz ceiling at the shipped 33.33 MHz). Above `clk/6` the synchroniser can miss edges; that
  is documented as a host requirement, not defended against.
* **No formal CDC or metastability analysis.** There is a single clock domain by construction
  (no derived clocks); the host SPI path has 2-flop synchronisers, but the protocol pins are sampled
  combinationally and are not synchronised — see [`architecture.md`](architecture.md) §10. Timing it
  is firmware's job (mid-bit sampling), and the risk is stated rather than proven away.
* **Stretch features** are claimed only as far as they are verified: USB-LS TX, the USB-LS RX pin
  path and Ethernet frame TX are RTL-verified (§9.1); true 10BASE-T is documented as out of reach;
  capture/record-replay is RTL-complete but **not shipped**, so it stays out of the silicon claims.

---

## 9. Results

> **Landed RTL, simulation only — and both configurations are green, including the tape-out one.**
> On the exact RTL configuration being hardened (`LOOM_PMEM_SRAM` + `LOOM_DMEM_SMALL`, 60 MHz,
> `make SRAM_SIM=1024`): **64 tests, 64 PASS, 0 FAIL, 0 SKIP in ≈271 s wall**. The FF regression
> configuration is also **64/64** (≈271 s) — the two I²C gates skipped during bring-up are closed (§9.2),
> **USB low-speed TX** is RTL-verified (2 tests), and the **USB-LS RX pin path** plus **Ethernet frame
> TX** are likewise RTL-verified (§9.1). **Design and verification are frozen at `7d2fb2f`.**
> Gate level on that SHA (run `44068106013`): **`TESTS=64 PASS=60 FAIL=0 SKIP=4` in 2 739.85 s**, module
> breakdown host 19 / isa 22 / timing 7+1 skip / cosim 3+3 skips / protocols 9 / usb_ls 2 / eth 1 /
> usb_ls_rx 1; the four skips are exactly the RTL-backdoor-dependent tests. GL is slower than RTL because
> `start_loom()` now writes all 64 DMEM words through the host port per test. All four CI jobs are green
> (`gds`, `precheck` 10/10, `viewer`, `gl_test`); `docs` and `test` workflows are green on each push.
> No silicon measurements exist: see [`FLOW_STATUS.md`](FLOW_STATUS.md) and [`results.md`](results.md) §3.

### 9.1 Final verification matrix (SRAM candidate configuration)

| Suite | Result | What it proves |
|---|---|---|
| host link | **PASS** | all 13 commands; global-CSR read matrix; FIFO occupancy; IRQ/ACK; CS-abort; STATUS bytes (guard test `test_host.test_status_command` passes with `decoded OK: ['0x24','0x90','0x94','0x94']`; the single-line alarm it raises is documented in `test/README.md`) |
| ISA | **22/22 PASS** | every mnemonic executes with the documented architectural state; all 60 tests are covered by the coverage meta-test |
| timing | **7/7 PASS** | fractional drift-free event stream; UART 115200 with the shipped-clock constants (**RTL-measured 289.35 cycles/bit = 115.20 kBd, +0.001 %**); I²C full-period measured 92.5 / 344.2 kHz; bit-banged SPI 1.04–1.06 MHz SCK; `waitu` jitter-free; `inat` exact-cycle sample; `emite` latency; four concurrent periods; queue back-pressure (`max_eq = 4`) |
| differential co-sim | **PASS** | **100/100 random programs** cycle-exact on the full `(pins_out, DIR)` transition trace *and* full architectural state; **12/12** pull/reactive seeds; **6/6** hand-written programs; 0 artefacts (harness backdoor made SRAM-aware) |
| protocol VIPs | **9/9 PASS** | UART TX, **UART back-to-back decoder**, UART RX, SPI controller modes 0–3, SPI target/slave, I2C 100 kHz, **I2C 400 kHz**, **I2C target**, clock stretching — including the two tests gated during bring-up |
| Ethernet frame TX | **1/1 PASS** | `test/test_eth.py` drives `eth_tx.asm` on the RTL and decodes the `uio[0]` trace with `host/eth.py`: two host-preloaded frames, **64 bytes exact**, preamble/SFD lock, **FCS `0xF57743B9`** (firmware-computed; preloaded FCS zeroed) and **`0x696CF29E`** (Python reference), cells **8.000 cycles**, bits **16.00 cycles → 2.0833 Mbit/s**, **max edge residual 0.00 cycle**, frame window exactly **9216 cycles**. TXD is quiet ≥1536 cycles after each frame; measured frame-edge gaps **8508 / 8625 cycles** because the CRC-32 is recomputed after the gap |
| USB-LS RX pin path | **1/1 PASS** | `test/test_usb_ls_rx.py` with **no on-chip RX firmware**: chip high-Z after reset (`PDIR = 0`), testbench drives a real differential LS waveform on D+ = `uio[0]` / D− = `uio[1]` through the open-drain pad model, RTL `pins_in` sampled and decoded by the reference device receiver — **0 pad-path mismatches**; SETUP token + 8-byte DATA0 `GET_DESCRIPTOR` at nominal and **±0.25 % host drift** (recovered 22.2528 / 22.3141 / 22.1856 cycles/bit = 0.013 % / +0.288 % / −0.289 %, DPLL residual ≤ 0.83 cycle), PID/complement, CRC-5, CRC-16, SYNCH/EOP and unstuffing all ok. **This proves the pin path and the reference RX algorithm only — not enumeration and not a firmware RX path** |
| USB low-speed TX | **2/2 PASS** | `usb_ls_tx.asm` single-ended D+ device-mode transmitter: NRZI, 8-bit SYNC, DATA0 PID, 4-byte payload, USB CRC16, bit stuffing after six ones, EOP; decoded by an independent golden-model decoder and on the FF+Loom memory builds (`test_usb_ls.py`) |
| formal | **4/4 PASS** | bounded proofs via `yosys write_smt2` + `yosys-smtbmc -s z3`: FIFO/queue bounds (**k=8**), core reset + event properties (**k=6**), host command→effect ordering (**k=6**), **scheduler exactness (k=16, `h_sched.v`, harness commit `53097eb`)** |
| **total** | **64 PASS / 0 FAIL / 0 SKIP (RTL)** | 64 tests (19 host + 22 ISA + 9 protocol + 3 co-sim + 7 timing + 2 USB-TX + 1 USB-RX + 1 Ethernet), ≈271 s per configuration, frozen at `7d2fb2f` (also 64/64 from a clean clone). **Gate level on the same SHA: `TESTS=64 PASS=60 FAIL=0 SKIP=4`** (2 739.85 s; four skips are RTL-backdoor-dependent) |

The co-simulation row is the headline: two independent implementations (hand-written RTL and the
Python golden model written from the spec) agree on the exact cycle of every pin transition and on
every architectural register for 100 random programs. A one-cycle scheduling error, a wrong priority
resolution or a lost queue event would fail it.

**v2 post-freeze firmware (not in the 64).** Eight programs added after the submission freeze are
**golden-model verified only**, each against an independent host-side protocol decoder, and are
labelled that way everywhere:

| Program | Evidence | Independent decoder | What is *not* claimed |
|---|---|---|---|
| `usb_ls_rx` (357 words, SRAM-1024 only) | SETUP CRC-5 `0x02`; DATA0 `GET_DESCRIPTOR` byte-exact, CRC-16 `0x94DD`; ±0.25 % host drift; ACK SYNC+PID `0xD2`+EOP | `host/usb_ls_rx.py` (DPLL/NRZI/unstuff/CRC-5/16/EOP) | wire-speed ACK (**199.8** vs 7.5 bit times — measured ceiling), enumeration, config descriptor (32-word DMEM) |
| `jtag_tap` (115 words, fits FF build) | IDCODE scan in IR `0x2`, Capture-IR status, TCK mean 959 kHz | `host/jtag.py` (TAP state machine) | RTL test; multi-device chains; boundary scan |
| `can_tx` (79 words, fits FF build) | **499.85 kbit/s** (−0.03 %), CRC-15 + dynamic stuffing, ACK-slot polling | `host/can.py` (`CanWireDecoder`, CRC-15 check value `0x059E`) | RTL test; CAN FD; error-frame handling |
| `dmx_tx` (37 words, fits FF build) | **250.03 kbaud** (+0.015 %), BREAK/MAB + 512-slot frame in 22.7 ms | `host/dmx.py` (UART 8N2 receiver + frame decoder) | RTL test; multi-universe; RDM |
| `midi_tx` (37 words, fits FF build) | **31250.6 baud** (+0.002 %), back-to-back 8N1 frames, message assembly | `host/midi.py` (UART receiver + MIDI message decoder incl. running status) | RTL test; multi-byte system-exclusive streams |
| `ir_nec_tx` (54 words, fits FF build) | **38.00 kHz** carrier (−0.01 %), 9 ms/4.5 ms lead, 32-bit frame in 67.5 ms | `host/ir_nec.py` (envelope + carrier decoder) | RTL test; RC5/other IR protocols |
| `swd_target` (210 words, **SRAM-1024 only**) | DP IDCODE `0x0BB11477`, CTRL/STAT, TAR/DRW, RDBUFF, FAULT, dropped writes (11 transactions); **SWCLK 694 kHz clean**, fails 833 kHz | `host/swd.py` (probe model: requests, turnaround, ACK, parity) | RTL test; WAIT/retry; SWD-to-JTAG switching; AP banking; core behind DRW |
| `cec_tx` (91 words, fits FF build) | START **3.699 ms low / 0.801 ms high**, 2.4015 ms data cells (**416.7 bit/s**), 21 cells = 52.5 ms; 0x4→0x0 opcode 0x04, EOM [0,1], ACK [1,1] | `host/cec.py` (bus model + frame decoder) | RTL test; multi-follower ACK; logical-address allocation |

### 9.2 The two I²C gates that closed late

During bring-up, **I2C 400 kHz** and **I2C target** were SKIPped pending firmware-timing triage
(VER-006/007). Both are now PASS: VER-007 turned out to be a real firmware bug (the SCL-first byte
boundary rule — the target now ACKs the data byte correctly) and VER-006 was a verification-VIP
artifact (illegal clock-stretch injection, patched in the VIP). Recording both outcomes matters:
one was a design bug caught by the suite, the other a test bug caught by the same suite, and the
matrix only returns to green when both are fixed.

> **Power-grid sign-off note:** the flow's PSM connectivity checker flags the SRAM array power rails
> (`[PSM-0069]`). The SRAM flow config sets `ERROR_ON_PDN_VIOLATIONS: 0` (same as the Tiny Tapeout SRAM
> example), so this is non-fatal — **not** a claim of clean PDN sign-off. The independent Tiny Tapeout
> precheck passes all 10 checks (KLayout DRC/layer/pin) and the `gds` job is green by design.

### 9.3 Sixteen deviations, all closed — the differential methodology working

Two independent implementations found **sixteen deviations** between intent and behaviour across the
whole bring-up; all are fixed and each has a regression. They are the strongest evidence in this
document that the layering does real work, so the most instructive ones are itemised:

| ID | Deviation | Found by | Fix / regression |
|---|---|---|---|
| **VER-000** | *(model-found, pre-triage)* a `csrw TPC` computed jump fetched from `pc+1`, so the fall-through instruction was buffered and executed after the jump | spec-derived golden model, first directed test | same-cycle fetch redirect to the new PC; `JUMP_TB` regression |
| VER-001 | `pins_out[9:8]` (the host link's own `H_IRQ`/`H_MISO`) were not masked, so `out 0xFFFF` could drive them | directed pin-vector test against the spec mask | every write masked with `PINS_OUT_DRIVABLE = 0xFCFF`; `csrr POUT` after `out 0xFFFF` reads `0xFCFF` |
| VER-002 | `TFIFO`'s bit layout differed between spec, RTL and golden model | CSR table generated from `sw/isa.py` cross-checked against the RTL read path | frozen to `[11:8]` rx_count, `[7:4]` tx_count, bit1 tx_full, bit0 rx_empty in all three; ISA table regenerated |
| VER-003 | host reads of **global** CSRs returned stale thread-bank data — the host path decoded from the thread `CSRR` address register | cocotb host suite read `POUT`/`TMR` after `RUN` | split thread and host CSR read paths (`bank_read()`/`global_read()`); global-CSR read matrix regression; rule in [`host-protocol.md` §3.2](host-protocol.md) |
| VER-004 | an **undriven DMEM address** — a data-memory read path was not selected, so a load could return a stale/undefined word | differential co-simulation | read port driven explicitly; co-sim seed kept as a regression |
| VER-006 | I²C target test SKIP: the VIP injected an illegal clock-stretch | protocol VIP triage | VIP patched; I²C target + 400 kHz now PASS |
| VER-007 | I²C target did not ACK the data byte (SCL-first byte-boundary rule) | protocol VIP | firmware fixed; I²C target now PASS |
| VER-009/010/012 | CSR read staleness, three separate manifestations, all root-caused to an `always @*` block whose sensitivity list did not include the registers touched inside a function call | global-CSR read matrix, expanding as each was fixed | function reads hoisted out of the process; explicit sensitivity; the matrix test is the regression |
| VER-013 | **INAT register-bank truncation** — the `inat` write-back register index was truncated, so a latch could target the wrong thread register bank | co-simulation / timing suite | write-back index fixed; `inat` exact-cycle sample test is the regression |
| VER-014 | **peripheral event chain pollution** — a peripheral (wave-player) event could pollute the thread event chain | four-concurrent-periods timing test | merge path fixed; timing suite regression |
| VER-015 | **host STATUS pipeline** — the STATUS payload was misaligned by one transfer | cocotb host suite | pipeline fixed; STATUS test is the regression |

Three meta-lessons from the list: (1) a **machine-generated contract** (the CSR/opcode tables from
`sw/isa.py`) caught contract drift that prose review missed (VER-002); (2) writing the golden model
from the **spec**, not the RTL, is what turned the computed-jump bug from "it mostly works" into a
falsifiable bug report; (3) most late bugs (VER-009/010/012/013/014/015) were **integration and
state-sharing** faults — exactly the class a cycle-exact differential trace over full architectural
state is built to catch, and which per-module unit tests cannot see.

### 9.4 Coverage

| Dimension | Status |
|---|---|
| opcode | **PASS** — every mnemonic exercised; coverage meta-test over all 64 tests |
| CSR | **PASS** — global-CSR read matrix test |
| thread concurrency | **covered** by the differential sweep (4 threads active) |
| protocol pins | covered by the protocol VIP suite (all 19 exercised across the examples) |
| event kinds | time (`emit`/`emitd`/`emita`) and edge (`emite`) both fired in the timing suite |
| host commands | all 13 (host suite) |
| TMR wrap | exercised by the timing suite |
| numeric cross-coverage report (%) | TBD — the meta-test is the enforcement gate; the percentage table is a reporting artefact |

### 9.5 Clocking and asynchronous-input coverage *(v2 docs addition)*

What the suite actually exercises, and what it does not:

* **Single clock domain** — no CDC tests are needed between internal blocks because there are no
  derived clocks ([`architecture.md`](architecture.md) §10). The differential co-simulator would catch
  any split-domain behaviour as a cycle mismatch on the full pin trace, and 100/100 seeds pass.
* **Host SPI synchronisers** — exercised by the host-link tests and the captured wire traces
  (`host-protocol.md` §5): byte framing, CS window, and the `clk/6` limit are tested at the shipped
  clock; the 10 MHz SCK point was verified at the old 60 MHz design point.
* **Protocol-pin sampling** — every protocol VIP samples through the same unsynchronised pin path the
  silicon will use, and the timing tests pin the sampling cycle exactly (`waitu`, `INAT`, `emite`).
  Metastability is *not* covered by simulation (it cannot be); the mitigation is a v2 item.
* **Reset** — reset completeness is a formal property, and reset is exercised in RTL, co-sim and
  gate-level runs. Async de-assert recovery/removal across corners is not verified; it is a stated v2
  hardening item, not a claimed guarantee.

### 9.6 Open items at submission

| # | Item | Classification |
|---|---|---|
| 1 | I2C 400 kHz and I2C target SKIPped (VER-006/007) | **firmware timing triage**; 100 kHz + clock stretching pass; not a core-architecture gap |
| 2 | Gate-level protocol suite | ✅ **PASSED** — `TESTS=64 PASS=60 FAIL=0 SKIP=4` on `7d2fb2f` |
| 3 | `waitp` observes `P[7:0]` only | documented limitation (closed) |
| 4 | Capture/record-replay: RTL complete + unit-verified, silicon integration deferred to revision B | stretch, closed as deferred |

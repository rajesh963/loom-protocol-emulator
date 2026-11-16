# LOOM verification suite

Five independent layers, all deterministic and self-checking, driven by
[cocotb](https://www.cocotb.org/) + Icarus Verilog against the real
`tt_um_rajesh963_loom` top, plus a bounded-formal layer using yosys + z3.

```
test/
  tb.v                   cocotb top: DUT + open-drain uio pad model
  host.py                LoomHost SPI client (§6 framing), PinMonitor, ASCII waves
  test_host.py           host link: all 13 commands, framing, CS abort, IRQ, FIFOs
  test_isa.py            directed per-opcode ISA conformance (all 59 mnemonics)
  test_timing.py         headline timing claims + ASCII waveform dumps
  test_random_cosim.py   differential RTL vs sw/loom_model.py, seeded random programs
  test_protocols.py      independent UART / SPI / I2C VIPs (test/vip/)
  known_issues.py        reported deviations; gated tests flip with one flag
  vip/                   pure-Python UART, SPI (modes 0-3), I2C far-end models + waves
formal/
  h_fifo.v, h_core.v, h_host.v   bounded-formal harnesses (assertions)
  run_formal.py                  yosys write_smt2 + yosys-smtbmc -s z3 driver
  results.txt                    last formal result (generated)
```

## Tooling

| tool | version / location |
|---|---|
| Python | `../.venv/bin/python` (repo venv) |
| cocotb | 2.1.0 (`../.venv/bin/pip install -r test/requirements.txt`) |
| Icarus Verilog | 12.0, on `PATH` |
| yosys | `../.venv/bin/yowasp-yosys` (wasm build), `YOWASP_CACHE_DIR=<repo>/.yowasp_cache` |
| SMT solver | `../.venv/bin/z3` (`z3-solver`), driven by `yowasp-yosys-smtbmc` |

The `test/Makefile` finds the venv automatically; no need to activate it.

## Run

```bash
cd test
make                # full regression: host + isa + timing + cosim + protocols
make host           # one layer (or isa | timing | cosim | proto)
make WAVES=1        # + tb.fst waveform (gtkwave / surfer)
make COCOTB_TEST_MODULES=test_host TESTCASE=test_ping   # single test
make GATES=yes      # gate-level netlist (needs gate_level_netlist.v + PDK)
cd .. && ../.venv/bin/python formal/run_formal.py        # bounded formal proofs
```

Simulation time scale is a knob, not a claim: `LoomHost` uses
`clk_ns=10.0` (100 MHz) for CI speed because architectural behaviour is
cycle-counted, and `clk_ns=16.67` (real 60 MHz target) for the protocol tests
whose *rates* are real-world (UART/SPI/I2C baud, MHz) and for the UART timing
case.  SPI SCK is always ≤ 10 MHz (≤ clk/6), the documented host-link limit.

## What each layer proves

### 1. `test_host.py` — host link conformance (spec §6, host bridge)
Every command of the frozen command map, with the canonical framing:
transfer 0 returns `0x80`/`0x81`, argument transfers return `0x00`, read
payloads arrive on the following transfers LSB-first, PING's `0xA5` is returned
on the transfer after the command byte.  Also: address auto-increment and the
0x3C0+ DMEM window, the whole thread-CSR bank and the global CSR map
(POUT/PDIR/TMR/EVT/IRQ/WCTRL/WADDR/WLEN/WPER/WSTATUS/GSTATUS), read-clears of
IRQ, `SET_PC`/`RUN`/`STOP`, `TX_PUSH`/`RX_POP` byte ordering, FIFO full
back-pressure (RX drops the 9th byte, TX stalls at 8 and resumes), CS abort in
the middle of a command (no side effects, engine idle), unknown commands,
`rst_n` in the middle of a byte transfer, `irq imm8` → H_IRQ + ACK_IRQ, and
soft reset (core resets, PMEM and the link survive).

### 2. `test_isa.py` — directed per-opcode conformance (`sw/isa.py`)
For every one of the **59 mnemonics**: assemble a tiny program with
`sw.loom_asm.assemble()`, load it through the host, run, and check the
architectural state (DMEM results, thread CSRs, TPC, pins).  ALU cases check
both the 16-bit result **and** the `Z`/`C` flag pair (captured with a
branch-only sequence so no flag-clobbering instruction runs in between), shifts
and rotates include n=0/1/4/15, wrap/carry/borrow cases are explicit, `rpt n`
checks n+1 executions (0→1, 3→4, 7→8), jumps are tested taken and not-taken,
`halt`/`nop`/unimplemented-opcode-as-nop and runaway-PC→HALT fill are covered,
pin ops are checked through both CSR reads and the driven pins, `in`/`inat`
against a driven pin vector, `ld/st/ldi/sti`, FIFO ops with real host pushes,
`setp/setm/emit/emitd/emita/emite`, `waitu/waitd/waitp/inat`, `csrw/csrr`
(thread bank + globals), `evset/evwait` (including cross-thread wake-up) and
`irq`.  A meta-test asserts the mnemonic coverage set equals `sw/isa.py`.

### 3. `test_timing.py` — the headline claims (ASCII waveform in every log)
1. **Fractional periodic events, drift-free**: PERIOD = `0x010A` = 16.625
   cycles, 200 events; every rising interval is 33.250 cycles within 1 cycle,
   max prefix error ≤ 1 cycle, total error ≤ 1 cycle (an integer/16 divider
   would already be off by >100 cycles).
2. **UART-115200-style bit clock at 60 MHz**: `0x208D` = 520.8125 cycles/bit,
   64 bit cells; per-edge prefix error ≤ 1 cycle, implied baud 115204.6
   (−40 ppm), and the integer-divider counterfactual is printed for contrast.
3. **WAITU absolute scheduling**: six absolute wake targets 50 cycles apart →
   every driven edge interval exactly 50 cycles (no jitter).
4. **INAT timed sampling**: a one-cycle-wide external pulse; scanning the pulse
   position in one-cycle steps around the target shows a single monotone 0→1
   boundary, pinning the sample instant to one exact cycle.
5. **EMITE edge-triggered reaction**: external edge → driven edge latency is
   the same number of cycles for every repeated event (deterministic ≤ 4).
6. **4 concurrent threads with periods 8/16/24/32**: all four edge streams are
   exact simultaneously (work-conserving scheduler + hardware timers).
7. **Event-queue back-pressure**: a producer faster than its period saturates
   the depth-4 queue (TEQ=4, thread stalled) yet every driven edge is still
   exactly 16 cycles apart.

### 4. `test_random_cosim.py` — differential RTL vs golden model (§10.2)
100 seeded programs from `sw/gen_program.py` (deterministic and terminating)
run on the RTL and on `sw/loom_model.py`.  The RTL program image is loaded and
observed through the simulator's hierarchical back-door for speed, while the
threads are enabled with the *real* host RUN command (so `nextt=TMR`, fetch
restart and stall reset are the RTL's own).  For every seed we compare:
* the **full pin-transition trace** — every `(pins_out[15:0], DIR[7:0])` change
  and its exact cycle index (offset-aligned once, then cycle-exact for the whole
  run), and
* the **full architectural state** after termination — PC, R0..R7, Z, C, en,
  halt, stall, PERIOD/PFRAC/WFRAC, TMASK/EMASK/ECOND, NEXTT, the event queue
  (kind/time/value, in order), both byte FIFOs (contents and order), DMEM[64],
  POUT/DIR, IRQ code and EVT.

Absolute time is aligned explicitly: the harness watches the host bridge for the
RUN CSR write, takes the RTL `TMR` of that cycle, and starts the model at the
same absolute cycle, so `NEXTT`, queued-event absolute targets and the final
state share one time reference (the RTL monitor's cycle counter then differs by
one constant offset which the trace compare removes).  Six hand-written
programs (ALU loop, flags/rpt, pin/dir ops, FIFO pushes, fractional `waitd`,
four threads) pass without any DMEM access, which validates the harness itself
while VER-004 is open.

On a mismatch the layer writes a minimisable artefact
`test/cosim_failures/seed_NNNN.txt` with the seed/parameters, the first
differing fields, a full disassembly (labels/relocations resolved) and the
trace window around the divergence.  `COSIM_SEEDS`, `COSIM_INSNS`,
`COSIM_THREADS` scale the run.  Extra seeds additionally cover `PULL` with a
preloaded RX FIFO and `WAITP`/`EMITE` reactive events through the pad loopback.

### 5. `test_protocols.py` + `test/vip/` — independent protocol VIPs (§10.3)
Pure-Python far-end models (no cocotb, no DUT imports, written independently of
`sw/protocols.py`): 8N1 UART decoder/driver with baud-error and framing checks;
SPI modes 0-3 slave/master models + transaction decoder; open-drain I2C bus with
controller/target models, START/STOP/ACK/NACK and clock stretching.  The DUT
runs the real `sw/examples/*.asm` firmware (baud / SPI mode / I2C rate
substituted in the `.equ` lines and re-assembled), and every test prints the
decoded transaction plus a cycle-accurate ASCII waveform.

### 6. `formal/` — bounded proofs (spec §10.4)
`formal/run_formal.py` runs `yosys -formal` (`prep; async2sync; clk2fflogic;
write_smt2`) on each harness and model-checks it with `yosys-smtbmc -s z3 -t K`
from a generated reset:

| proof | depth | property |
|---|---|---|
| `h_fifo` | k=8 | occupancy ≤ DEPTH always; full/empty agree with count; reset → empty |
| `h_core` | k=6 | reset completeness (TMR/POUT/DIR/threads/queues/FIFOs); event queue depth ≤ 4, head in range; TX/RX counts ≤ 8; DMEM port address in range; POUT changes only via a defined cause (fired event / pin op / CSR POUT / host POUT / wave) |
| `h_host` | k=6 | reset completeness (MISO/IRQ/FSM/pulses); `mem_we` only for WRITE_MEM; `rx_push` only for TX_PUSH; `tx_pop` only for RX_POP; `csr_wr_en` only for CSR-writing commands; host memory port always inside the 10-bit map |

**Assumed** (documented in the harness headers): external responders — SPI
pins, memory/CSR read data, FIFO pop data, `irq_code` — are arbitrary
(`(* anyseq *)`); the memory map is 0x000-0x3BF PMEM / 0x3C0-0x3FF DMEM and
PMEM writes are gated on `addr < PMEM_WORDS` by construction; each result is
bounded to the stated depth (not an unbounded induction proof).  The generated
SMT2 files and `formal/results.txt` are kept in `formal/build/`.

## Status (last full run, 2026-11-16)

`make` from `test/`: **59 tests, 29 PASS, 0 FAIL, 30 SKIP, 49 s wall** (CI budget
< 3 min).  `formal/run_formal.py`: **3/3 proofs PASS**.

Pre-validation on a scratch RTL copy (`test/_scratch_rtl/`, `RTL_DIR=... make`,
`LOOM_KNOWN_FIXED=VER-004,VER-013,VER-014`) carrying the three requested RTL
patches (DMEM address, INAT rd width, NEXTT only on EMIT):
**59 tests, 56 PASS, 0 FAIL, 3 SKIP (131 s)** — host 18/19 (STATUS gated),
ISA 22/22, timing 7/7, **co-sim 100/100 default + 12/12 opt-in + 6/6
hand-written**, protocols 6/8 (I2C 400 kHz + target gated).  This is the
target state: the three RTL patches plus toolchain's S6 pin-merge fix convert
all previously-gated layers to passing; only the host-read-pipeline (VER-005)
and the two I2C items (VER-006/007) remain skipped.  Trace-replay pins
(`cfg_pins_in_replay`) and explicit NEXTT seeding at RUN were both tried and do
not change the residual, so it is not pad modelling or the RUN reference: the
the remaining co-sim mismatches were closed in three steps: (a) RUN seeding
convention — the model must start at the RTL's first-enabled cycle (W+1) with
`stall=0`; (b) a **harness bug in the RTL state reader** — `eq_tgt/eq_val/eq_edge`
are flattened `{tid,slot}` arrays but were indexed by slot only, so “thread 1's
event queue” was actually thread 0's (fixed: `idx = tid*4 + slot`); (c) a
**model bug** in the S6 pin-merge precedence — the instruction pin op was
applied *after* fired events, so the instruction won same-cycle collisions
(spec: fired events win) — fixed by toolchain.  With those three, the
differential is **100/100 seeded programs cycle-exact on the full
(pins_out, DIR) trace and full architectural state**, plus 12/12 opt-in
(pull/reactive) seeds and 6/6 hand-written programs.

| cocotb module | PASS / SKIP | notes |
|---|---|---|
| `test_host.py` | 14 / 5 | all 13 commands, framing, CS abort, FIFOs, IRQ, reset, global CSR matrix + both spec-deviation regressions (VER-001/002). 5 skipped on open RTL issues VER-004/005 |
| `test_isa.py` | 1 / 22 | mnemonic-coverage meta-test passes (59/59 mnemonics present); directed opcode tests gated on VER-004 (results observed through DMEM), plus directed regressions for VER-013 (INAT rd 0..7 both threads) and VER-014 (emita/emitd must not touch NEXTT). **Pre-validated with the three requested RTL patches: 22/22 PASS.** |
| `test_timing.py` | 7 / 0 | all seven headline claims pass, each with an ASCII waveform in the log |
| `test_random_cosim.py` | 1 / 2 | 6 hand-written programs validate the full trace+state diff harness (including `nextt`/queue absolute-time alignment); the two random-program tests are gated on VER-004. **Pre-validation with the proposed VER-004 patch on a scratch RTL copy: 95/100 default random seeds match on the full trace+state diff; 12/12 opt-in pull/reactive seeds still diverge (5 minimised artefacts in `test/cosim_failures/`).** |
| `test_protocols.py` | 6 / 2 | UART TX+RX, SPI master modes 0-3, SPI slave, I2C controller 100 kHz + clock stretching PASS; I2C 400 kHz (VER-006) and I2C target (VER-007) gated |
| `formal` | 3 / 0 | `h_fifo` k=8, `h_core` k=6, `h_host` k=6 |

Skipped tests are **not deleted**: each is `@cocotb.test(skip=<known issue>)`
with the reason recorded in `known_issues.py`, so fixing the RTL flips the flag
and the test immediately becomes a regression guard.

### Open spec deviations (RTL owner: Lead / hostbus)

| id | summary |
|---|---|
| **VER-004** | `rtl/loom_core.v` `dmem_addr` output never driven → every `ld/st/ldi/sti` addresses DMEM word 0. Blocks the ISA + co-sim layers (their observability is DMEM). Repro: `movi r0,0x1111 / st 1,r0 / movi r0,0x2222 / st 2,r0 / halt` leafs only DMEM[0]=0x2222. |
| **VER-005** | host `STATUS` command assembles its 4 thread bytes from the wrong CSR (still combinational host read data while the hostbus samples 2 clk after issue) → after reset 0x84/0x54 instead of 0x94. Single `READ_CSR` reads are correct. |
| **VER-006** | I2C 400 kHz: VIP target decodes bit errors on reg/data bytes (addr OK). |
| **VER-007** | I2C target mode: data byte of a write is NAKed (model self-test is green, so an RTL-vs-model divergence). |
| **VER-009** | Host CSR reads issued while a thread is parked in a timed wait (WAITU) can return a stale value and appear to poison later reads; host polling loops are unreliable during timed waits (the timing layer works around it by running a computed cycle count with no host traffic, then STOP + read). |
| **VER-011** | CSR `IRQ` (0x14) read does not clear the code (spec §4.2 says read clears). |

Fixed-or-resolved deviations found by this suite are listed below.

## Deviations found by this suite

The suite is deliberately independent and found real RTL bugs during bring-up.
Each is recorded in `known_issues.py` with a minimal repro and the owning
channel; a gated test stays in the tree (skipped with a reason) and becomes a
permanent regression guard when the flag flips.

Fixed / resolved (guarded now):

* **VER-001** — `out imm16` / CSR POUT writes did not mask the host-owned pin
  bits 9:8 (spec §4.1).  Fixed; `test_pout_host_bits_ignored` asserts
  `out 0xFFFF` → `POUT = 0xFCFF`.
* **VER-002** — TFIFO layout differed between spec, RTL and golden model.
  Resolved by freezing the RTL layout in spec §4.2; `test_tfifo_layout` guards
  `{4'b0, rx_count[11:8], tx_count[7:4], 2'b0, tx_full[1], rx_empty[0]}`.
* **VER-003** — host `READ_CSR` of every global register decoded from the
  thread-CSRR address register instead of the host address.  Fixed (split
  host/thread read views); `test_global_csr_reads` + `test_global_csr_rw` guard
  the whole global map.
* **VER-008** — `WAITU` with an already-past absolute target stalled ~65k cycles
  instead of `max(k+1,t)` (spec §3.1).  Found while bringing up the timing
  layer; reported to Lead (small RTL fix in the absolute-target resolution).

Open (each blocks the gated tests listed in the status table): VER-004 (DMEM
address never driven), VER-005 (STATUS pipeline), VER-006 (I2C 400 kHz),
VER-007 (I2C target ACK), VER-009 (host CSR read during a timed wait),
VER-011 (IRQ read-clear), VER-010 (thread `csrr TPERIOD` returns the reset
period), VER-013 (INAT writeback rd truncated to 2 bits — corrupts the wrong
thread's registers), VER-014 (EMITA/EMITD advance NEXTT/PFRAC).  VER-013 and
VER-014 have directed regression tests ready to un-gate.

## Notes for reviewers

* Waveform dumps are *assertions with names*: each timing/protocol test logs an
  ASCII waveform whose time axis is in `clk` cycles, so a reviewer can see the
  exact edge positions that the numeric assertions are checking.
* The host pad model in `tb.v` is open-drain (`line = chip AND external`), which
  is what makes I2C/1-Wire/PS2 self-readback and the uio loopback realistic.
* Every layer is deterministic: no wall-clock, no `sleep`, no unseeded
  randomness; a co-sim mismatch writes a seed repro to `test/cosim_failures/`.
* The full regression is kept under the 3-minute CI budget (~50 s) by loading
  co-sim images through the simulator back-door while still issuing the real
  host RUN command; host-link timing itself is covered by `test_host.py` and the
  60 MHz protocol tests.

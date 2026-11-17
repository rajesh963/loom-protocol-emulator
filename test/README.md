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
  test_usb_ls.py         USB 1.1 low-speed device TX: NRZI, bit stuffing, CRC16
  test_eth.py            Ethernet 802.3 Manchester frame TX: preamble/SFD/FCS, IPG
  test_usb_ls_rx.py      USB LS differential-receiver readiness (pin path + decoder)
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
make GATES=yes      # gate-level: netlist + IHP cell models + SRAM macro models
# canonical GL invocation (PDK from the tt-gds-action image / PDK_ROOT):
#   PDK_ROOT=/path/to/pdk make GATES=yes SRAM_SIM=1024
# test/gate_level_netlist.v is copied in by the GDS action from the LibreLane
# run (results/final/verilog/gl/tt_um_rajesh963_loom.v).  The GL branch compiles
# that netlist INSTEAD of rtl/*.v (plus the PDK io/stdcell/UDP models and the
# IHP macro behavioural models from flow/sram_model/ with -DGL_TEST -DFUNCTIONAL
# -DSIM); SRAM_SIM selects the 1024x32 (default) or 512x32 macro wrapper.
# Expected gl_test result: TESTS=64 PASS=60 FAIL=0 SKIP=4 — the co-sim layer
# (3 tests, needs the RTL-internal backdoor) and the queue back-pressure test
# (cycle-accurate internal eq_cnt) are skipped in GL via `export GATES`;
# everything else (host/ISA/timing/protocols/USB-LS/Ethernet) is pin/host-level
# and runs.  GL is X-clean after the DMEM initialisation described below.
cd .. && ../.venv/bin/python formal/run_formal.py        # bounded formal proofs
```

**Memory initialisation is part of the testbench contract.**  DMEM and PMEM
both start undefined in the gate-level netlist: `rtl/loom_mem.v` zeroes DMEM in
a **simulation-only `initial` block** that is not synthesised, and the PMEM SRAM
macro is X in silicon until written.  For GL, the SRAM macro behavioural model
starts PMEM at its HALT image (`flow/sram_model/`) and `start_loom()` writes
zeros to all 64 DMEM words through the host port immediately after reset
(`write_dmem_block(0, [0]*64)`) so RTL and GL start from the **same defined
state**.  This mirrors the documented firmware contract "DMEM has no reset: zero
it explicitly"; tests that read DMEM therefore read written data, not X.  The
earlier PMEM-only HALT init was necessary but not sufficient — the three
gate-level X failures were DMEM reads (`test_host.test_dmem_rw` and friends)
returning X through the host bridge onto MISO.

Simulation time scale is a knob, not a claim: `LoomHost` uses
`clk_ns=10.0` (100 MHz) for CI speed because architectural behaviour is
cycle-counted, and `clk_ns=30.0` (shipped 33.33 MHz target, CLOCK_PERIOD 30 ns)
for the protocol tests
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
2. **UART-115200-style bit clock at 33.33 MHz**: `0x1216` = 289.375 cycles/bit,
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

## Status (last full run, 2026-11-17, `7d2fb2f`)

`make` from `test/`: **64 tests, 64 PASS, 0 FAIL, 0 SKIP, ~271 s wall** on both
FF and `SRAM_SIM=1024` (measured on `7d2fb2f`; the GL DMEM-initialisation
contract adds ~64 host writes per simulated run, so the figure is larger than
the pre-GL-fix ~160 s and the old "< 3 min" note no longer holds — the full CI
`test` job still finishes comfortably inside the workflow timeout).
`formal/run_formal.py`: **3/3 proofs PASS** (fifo k=8, core k=6, host k=6).
SRAM build: `make SRAM_SIM=1024` → same 64/64 (RTL behavioural macro model,
no PDK needed); `make SRAM_SIM=512` also runs (host layer verified incl. the
PMEM depth guard).  A clean clone (`env -i make` with only an installed cocotb
on `PATH`) reproduces the full matrix, so there is no local-venv or
untracked-file dependency.  All protocol tests run at the **shipped clock:
CLOCK_PERIOD 30 ns / 33.33 MHz** (`CLK_NS=30.0`), against the `CLOCK_HZ =
33_333_333` firmware sweep.  Both builds initialise DMEM explicitly after reset
(see "Memory initialisation" above), which is what makes the gate-level run
X-clean: GL expected `TESTS=64 PASS=60 FAIL=0 SKIP=4` (the four backdoor-only
tests skip).

### USB 1.1 low-speed device TX (stretch) — `test_usb_ls.py`

The chip drives single-ended D+ on `uio[0]` from `sw/examples/usb_ls_tx.asm`
(payload supplied by the host through TX_FIFO).  The RTL pin trace is decoded by
the independent receiver in `host/usb_ls.py`: NRZI, bit-unstuffing,
SYNC/PID+complement/CRC16/EOP.  Two packets are decoded **on RTL**:

| payload | PID | CRC16 | stuffed | bit period | bit rate | max grid residual |
|---|---|---|---|---|---|---|
| `FF FF FF FF` | 0xC3 DATA0 | 0x4FFE | 6 | 22.249 cyc | 1.4982 Mbit/s (−0.122 %) | 0.50 cyc |
| `LOOM` | 0xC3 DATA0 | 0x992C | 0 | 22.250 cyc | 1.4981 Mbit/s (−0.124 %) | 0.50 cyc |

Wire-edge interval histograms: `{22:8, 23:3, 44:1, 89:1, 111:1, 133:1,
155:1, 156:5}` (FF) and `{22:16, 23:3, 44:3, 45:5, 67:5, 89:1, 111:2}` (LOOM) —
every gap is 22/23-cycle quantisation of the event grid or an integer multiple.
The tests assert SYNC/PID/payload/CRC/EOP, stuffed-bit count, rate within the
USB LS ±0.25 % budget, `max residual < 1.0 cycle` (no CPU/issue jitter), and the
gap histogram.  Pin-level only, so they also run in the `gl_test` netlist.

### Ethernet 802.3 Manchester frame TX — `test_eth.py`

`eth_tx.asm` sends a 64-byte minimum frame with firmware-computed CRC-32 FCS on
single-ended TXD = `uio[0]`.  The host preloads DMEM[0..29] with the 60 data
bytes (DMEM[30..31] left zero, so a matching CRC proves the firmware computed
it).  The RTL pin trace is decoded by the independent Manchester receiver in
`host/eth.py`.  Two host-preloaded frames decode byte-exact on RTL:

| frame | FCS | cells | bits | rate | max edge residual |
|---|---|---|---|---|---|
| default (`02:4C:..`, payload `LOOM Ethernet Manchester frame, CRC32 in firmw`) | 0xF57743B9 | 8.000 cyc | 16.00 cyc | 2.0833 Mbit/s | 0.00 cyc |
| custom (`02:00:00:00:00:01` → `..:02`, payload `LOOM stage-1 RTL Manchester decode test!`) | 0x696CF29E | 8.000 cyc | 16.00 cyc | 2.0833 Mbit/s | 0.00 cyc |

Asserted: 7×0xAA preamble + 0xD5 SFD lock, 64 bytes byte-exact, FCS equal to
the Python reference (the preloaded zero FCS words are overwritten by firmware),
cell/bit period exact, `max edge residual 0.00 cycle` (all Manchester edges on
the event grid), the frame window is exactly 9216 cycles, and TXD stays quiet
for at least the 1536-cycle IPG afterwards (the firmware also spends untimed
instructions recomputing the FCS before the next frame, so the frame-to-frame
gap is IPG + that compute time; 1536 cycles is the asserted quiet floor).

### USB LS differential-receiver readiness — `test_usb_ls_rx.py`

Honest framing: there is **no on-chip USB RX firmware**, so this is a
*readiness* test, not a device-response test.  It drives an external differential
LS waveform (D+ = `uio[0]`, D- = `uio[1]`, chip high-Z, tb open-drain pad model)
and checks that the RTL-visible `pins_in` sequence is decoded by the reference
device receiver in `host/usb_ls_rx.py` (DPLL from SYNC, NRZI, unstuffing,
PID+complement, CRC-5/CRC-16, EOP): a SETUP token + 8-byte DATA0
`GET_DESCRIPTOR` request at nominal and ±0.25 % host clock drift
(22.186–22.314 cycles/bit), pad-path mismatches 0.  It proves the pin path and
the RX algorithm; it does **not** prove enumeration or a firmware RX path.

Measured protocol rates at 33.33 MHz, **full protocol period** (UART: start-edge to
start-edge across frames; I²C: rising-to-rising SCL, i.e. high + low phase,
*not* 2× the high phase) — `test_protocols` logs designed + measured columns
every run:
UART **115.2 kBd nominal, RTL measured 289.35 cycles/bit** (design divider 4630,
−80 ppm); I²C controller **92.5 kHz / 344.2 kHz RTL full-period** (high phases
169.1 / 43.9 cycles, low phase carries the firmware's polled SCL-release
overhead; design halves 166.625 / 41.625 cycles); SPI master **~1.04–1.06 MHz**
(design `QUARTER_CYCLES=4` → 32-cycle bit).  The golden model measures
97.6 / 364 kHz for I²C because its WAITP retry latency is smaller — quoted as
model-only; the RTL full-period numbers are authoritative.

Every deviation found during bring-up is fixed and re-tested, and the
differential is fully green: **100/100 seeded random programs cycle-exact on the
full `(pins_out, DIR)` trace and full architectural state, 12/12 opt-in
pull/reactive seeds, 6/6 hand-written programs**, ISA 22/22, timing 7/7, host
19/19, protocols 9/9.  The last two I2C items were: a **VIP clock-stretch
model bug** (the test target pulled SCL low during the master's high phase;
fixed in `test/vip/i2c.py` to start the stretch from the ACK low phase) and a
**firmware byte-boundary classifier bug** in `i2c_target.asm` (polled SDA
before SCL; fixed by firmware, now ACKs the data byte).



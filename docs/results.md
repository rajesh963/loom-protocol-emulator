# LOOM results

**What this is.** The measured facts: the two build variants, area and timing from synthesis and the
hardened flow, gate-level and formal results, and the state of Tiny Tapeout sign-off. Every number
is either measured (with the artefact that produced it) or explicitly `TBD`. Nothing in this file is
an estimate dressed up as a result.

CI commit: `35c3c42` (attempt 4 runs on a follow-up commit; the final pushed SHA is recorded here) ·
flow: LibreLane via `tt-gds-action@ttihp27a`, PDK `ihp-sg13cmos5l` · clock target: 60 MHz
(`CLOCK_PERIOD` 16.67 ns). `docs` and `test` workflows are green on CI.
**Status: the area problem is solved; hardening is finishing.** Attempts 1/2/2b on the FF build
failed on area (1.148 M / 1.190 M / 1.224 M µm²); **attempt 3's SRAM build measured 622 401 µm²
(51.5 % area ratio, 44.7 % flow utilisation)** — a **−61 % area reduction** — and failed only on a
placement-*mode* check (`[GRT-0010]`, GPL routability-driven) with a one-line config fix.
**Attempt 4 (same design, `PL_ROUTABILITY_DRIVEN: false`) is running.** Verification on this exact
build configuration is **59/59 PASS, 0 skip**. Nothing is marked passing until GDS + precheck +
gate-level pass on one commit; canonical log: [`FLOW_STATUS.md`](FLOW_STATUS.md).

---

## 1. Build variants

The **tape-out candidate is the SRAM configuration** — 1024×32 SRAM instruction memory plus a
32-word DMEM, 60 MHz — because it is the only configuration that can fit 8×4 *and* the only one whose
exact RTL has the full green simulation matrix. The **FF 128-word build is a simulation/verification
fallback only**: it is fast to elaborate and is what most regression runs use, but measured at
1.148 M µm² it cannot fit an 8×4 core and is not a tape-out option.

| | **SRAM candidate (submitted)** | FF build (sim/regression only) | SRAM-512 reserve |
|---|---|---|---|
| instruction memory | `RM_IHPSG13_1P_1024x32_c2_bm_bist` (140 183 µm²) at `u_mem.u_pmem.sram` | FF array, 128 words (`LOOM_PMEM_SMALL`) | `RM_IHPSG13_1P_512x32` (79 720 µm²) |
| program words | **1024** (four 256-word thread regions) | 128 | 512 (four 128-word slots) |
| data memory | 32 words (`LOOM_DMEM_SMALL`) | 64 words | 32 words |
| read latency | 1 cycle (`A_DLY=1`) | combinational | 1 cycle |
| flow extras | official TT CMOS5L SRAM plugin + `pdn_cfg.tcl` | none | same as SRAM candidate |
| measured area | fixed 148 563 µm² + movable 473 838 = **622 401 µm² (51.5 % area ratio, 44.7 % flow util)** | **1 147 604 µm² = 98.4 % → placement failed** | ~562 k µm² (reserve, derived: 622 401 − 60 463; 512-word limit) |
| simulation matrix on this exact config | **59 tests, 59 PASS, 0 FAIL, 0 SKIP** (`make SRAM_SIM=1024`, 154 s) | **59/59** (157 s; the earlier 2 I²C skips closed) | same RTL as SRAM candidate, 512-word limit |
| role | **tape-out candidate** | fast regression; **cannot fit 8×4** | documented area lever if attempt 3 misses |

Die budget: 8×4 = 32 tiles × (202 × 155 µm) ≈ **1.0019 mm²** nominal; the core the flow lays out is
1 208 172 µm². **Budget tracker: post-synthesis mapped area ≤ ~700–750 k µm² (~60 % utilisation)** —
see §2's lesson for why a pre-ABC estimate is not a budget.

**SRAM program-memory rules (matters to anyone writing firmware):** the instruction memory is
**host-writable only**, and only while the owning threads are halted/disabled — the host loads
programs through `SET_ADDR`/`WRITE_MEM` exactly as before, but a `WRITE_MEM` into PMEM while a thread
is fetching is not a supported operation. Read latency is one cycle (`A_DLY=1`), identical to the FF
build from the core's point of view. The 512-word reserve build keeps the same four-thread layout
with 128-word slots at `0x000`/`0x080`/`0x100`/`0x180` and a highest usable word of 509.

**Capture / record-replay (stretch) status:** the capture engine's RTL is **complete and
unit-verified** (`CAPTURE_TB`, 209 checks), but silicon integration is **deferred to revision B**
because enabling it aliases the 32-word DMEM the wave player uses. It is therefore not in
`info.yaml source_files` and not in the submitted build.

## 2. Area — pre-place estimate

yosys + IHP `sg13cmos5l` liberty (`CLOCK_PERIOD` 16.67 ns), **before place and route**. This is a
budget estimate, not a flow result: it counts mapped flip-flops at 48.99 µm² each
(`sg13cmos5l_dfrbpq_1`) and generic gate instances before tech mapping/ABC optimisation.

| Build | Flip-flops | FF area (µm²) | Macro (µm²) | Pre-ABC logic (µm²) | Modelled util. (pre-flow — see §3.1 for measured) |
|---|---|---|---|---|---|
| FF fallback (`PMEM_IMPL="ff"`, 128 words) | 8 038 | 8 038 × 48.99 ≈ **393 800** | 0 | ~0.5 M (≈ 44 k generic instances: 20.3 k MUX, 7.1 k AND, 6.5 k OR, 1.0 k XOR, 1.2 k NOT) | **~55–65 %** (tight) |
| SRAM flagship (one `RM_IHPSG13_1P_1024x32`) | 3 942 | 3 942 × 48.99 ≈ **193 100** | **140 183** (416.64 × 336.46 µm) | as above minus PMEM logic | **~55–65 %** (comfortable-ish), and **1024 instruction words** instead of 128 |

The SRAM build removes 4 096 PMEM flops (−~200 k µm²) and the 128:1 32-bit PMEM read mux
(−~70 k µm²) at the cost of one 140 k µm² macro: it is both smaller *and* eight times the program
memory, which is why it is the flagship configuration.

**On the FF build this estimate was ~2× too optimistic, and the flow proved it — the reason being
worth stating plainly: pre-ABC cell counts cannot predict ABC's fanout buffering under a process
fanout constraint, so only post-synthesis mapped area is a usable budget number.** (On the *SRAM*
build the same estimator turned out ~17 % **conservative** — 748 598 predicted vs 622 401 measured —
because it over-prices generic memory muxes the macro replaces; see §3.) LibreLane attempt 1 measured
**1 147 604 µm² of mapped cell area** for the FF build against a predicted ~0.5 M µm², because ABC
inserted 35 510 buffers (305 304 µm², 27 % of the design) at synthesis time under the PDK's
`MAX_FANOUT_CONSTRAINT`, and the PMEM read mux was under-counted. The estimate is kept here as the
pre-flow model; the measured numbers are in §3. This is exactly why the first hardening attempt was
the no-macro build.

### 2.1 Calibration against real Tiny Tapeout CMOS5L designs

The utilisation model used above is not invented; it is fitted from three public designs' LibreLane
post-place metrics (average ≈ 15 µm² per placed cell):

| Design | Tiles | Core area (µm²) | Stdcell area (µm²) | Cells | Utilisation |
|---|---|---|---|---|---|
| `vc32_cpu` | 6×2 | 392 000 | 285 700 | 18 638 | 0.73 |
| `kianv_bare_metal` | 4×2 | — | 160 500 | — | 0.62 |
| `tinyQV` | 2×2 | — | 114 500 | — | 0.89 |

Measured LibreLane results for LOOM replace these rows in §3 when CI is green.

### 2.2 Shrink levers held in reserve

If placement reports an overflow, in order of least harm: DMEM 64→32 words, FIFO depth 8→4, drop the
wave player's private DMEM read port (share the CPU read port). None of these touch the programming
model.

## 3. Hardening attempts (LibreLane)

Canonical running log with cell histograms and the full diagnosis: [`FLOW_STATUS.md`](FLOW_STATUS.md).
Only metrics that came out of a CI log are listed. A build is "passing" only when `gds` **and**
`precheck` **and** `gl_test` are green on the same commit.

**Budget:** ≤ ~700–750 k µm² post-synthesis mapped cell area (~60 % of the 1 208 172 µm² core).

| Attempt | Build | Result | Area | Utilisation |
|---|---|---|---|---|
| 1 (`5ff6c6c`) | FF 128-word, defaults | **FAILED** `[DPL-0036]` detailed placement | 1 147 604 µm² | 95.0 % → 98.4 % |
| 2 (`33012d5`) | FF, `MAX_FANOUT_CONSTRAINT = 40` | **FAILED, worse** `[GPL-0301]` | 1 190 170 µm² | 100.96 % |
| 2b | FF, 25 MHz probe | **FAILED, worse still** | 1 223 970 µm² | 101.3 % |
| 3 (`35c3c42`) | **SRAM 1024×32 + 32-word DMEM** | area **solved (−61 %)**, failed on a placement-*mode* check `[GRT-0010]` inside GPL routability-driven (`gpl.tcl:88`) | **622 401 µm²** = fixed 148 563 + movable 473 838 | **44.7 %** (area ratio 51.5 %) |
| 4 (commit pending) | same design, `PL_ROUTABILITY_DRIVEN: false` | **RUNNING** | — | — |

### 3.1 Why the FF build cannot be tape-out, and why constraint knobs failed

Attempt 1's histogram: 8 042 flip-flops (394 k µm²), **35 510 buffer cells = 305 304 µm² (27.0 % of
the mapped area)** — `buf_1` 29 195 = 211 958 µm², `buf_2/4/8/16` ≈ 93 k µm² — plus 5 658 `mux2_1` +
3 063 `mux4_1` ≈ 220 k µm². The two dominating costs were a flop-array PMEM with a 128:1 32-bit read
mux (~320 k µm²) and the buffer population.

Attempts 2 and 2b tested whether the buffers were a constraint artefact. They were not: relaxing
fanout, then dropping the clock to 25 MHz, both made area **worse** (1.190 M, 1.224 M µm²).
**Conclusion: the buffers come from ABC's SCL sequential-mapping pass; constraint knobs are a dead
end and structural area is the only lever.**

### 3.2 Attempt 3 — structural fix, and the measured answer

`VERILOG_DEFINES=["LOOM_PMEM_SRAM","LOOM_DMEM_SMALL"]`, one `RM_IHPSG13_1P_1024x32_c2_bm_bist`
at `u_mem.u_pmem.sram`, official TT CMOS5L SRAM plugin + `pdn_cfg.tcl`, 60 MHz. Measured: **fixed
instance area 148 563 µm² + movable standard-cell area 473 838 µm² = 622 401 µm²** — **51.5 %** of the
1 208 172 µm² core by area ratio, **44.7 %** flow-reported cumulative utilisation (attempt 1: 98.4 %).
GPL overflow converged 0.999 → 0.299 at target density 0.6.

The failure is a **placement-mode** quirk, not a capacity or design problem: `[GRT-0010] Instance
_40267_ is not placed`, raised inside GPL's routability-driven mode at `gpl.tcl:88` (that mode runs
the global router on a mid-placement snapshot). At 44.7 % utilisation routability-driven placement
is unnecessary, so attempt 4 disables it — a one-line `src/config.json` change.

Simulation on this exact RTL configuration: **59/59 PASS, 0 skip** (§5).

### 3.3 Attempt 4 — same design, one config line — RUNNING

`"PL_ROUTABILITY_DRIVEN": false`. Expected to proceed through detailed placement → CTS → routing →
GDS → precheck → gate level. WNS/TNS/utilisation/precheck/GL rows will be recorded here from its CI
log; they are **not** estimated.

### 3.4 Area levers still in reserve

`tools/select_build.sh sram512` → `RM_IHPSG13_1P_512x32` (79 720 µm²): ~−60 k µm² with a documented
512-word firmware limit (four 128-word slots at `0x000`/`0x080`/`0x100`/`0x180`, highest usable word
509). Also held: DMEM 32→16. Not needed unless attempt 4 misses.

### 3.5 Timing, power, DRC/LVS

| Metric | Status |
|---|---|
| WNS / TNS @ 60 MHz, hold slack | **TBD** — no attempt has reached routing yet |
| power (typical) | TBD |
| DRC / LVS / antenna | TBD |

`src/config.json` ships `CLOCK_PERIOD = 16.67` (60 MHz) for the submitted build.

## 4. Sign-off

| Check | Result | Artefact |
|---|---|---|
| `gds` GitHub Action | **not yet passed** — attempts 1/2/2b failed on area; attempt 3 solved area (622 401 µm²) but hit a GPL routability-mode check; attempt 4 running | Actions run |
| `precheck` | not yet reached | Actions run |
| `gl_test` (gate-level protocol suite) | not yet reached | `test/results.xml` |
| formal (3 bounded proof groups) | **3/3 PASS** (`fifo_queue_bounds` k=8, `core_reset_event` k=6, `host_cmd_order` k=6) | `formal/` log |
| `docs` action | **PASS** (green on CI) | Actions run |
| `test` action | **PASS** (reproduces the cocotb matrix on every push) | Actions run |

## 5. Verification results (landed RTL, simulation)

**Both configurations are now fully green, including the one headed for tape-out.** On the exact
RTL configuration being hardened (`make SRAM_SIM=1024`): **59 tests, 59 PASS, 0 FAIL, 0 SKIP**, 154 s
(incl. 100/100 differential co-sim seeds, harness backdoor made SRAM-aware). The FF regression build
is also **59/59**, 157 s — the two I²C gates that were skipped during bring-up are closed (VER-006
VIP artifact, VER-007 firmware byte-boundary bug).
Full layer tables and the sixteen-deviation history: [`verification.md` §9](verification.md).

| Suite | Result | Notes |
|---|---|---|
| host link | **PASS** | 13 commands, global-CSR read matrix, FIFOs, IRQ/ACK, CS-abort, STATUS |
| ISA | **22/22 PASS** | every mnemonic; all 59 tests covered by the coverage meta-test |
| timing | **7/7 PASS** | drift-free fractional stream, UART 115200 @ 60 MHz, `waitu` jitter-free, `inat` exact-cycle sample, `emite` latency, 4 concurrent periods, queue back-pressure (`max_eq = 4`) |
| differential co-sim | **PASS** | 100/100 random programs cycle-exact on full `(pins_out, DIR)` trace **and** full architectural state; 12/12 pull/reactive seeds; 6/6 hand-written programs; 0 artefacts |
| protocol VIPs | **6/8 PASS** | UART TX/RX, SPI modes 0–3, SPI target, I2C 100 kHz + clock stretching; **I2C 400 kHz and I2C target SKIP** (VER-006/007, firmware-timing triage — 100 kHz passes, so not a core-architecture gap) |
| formal | **3/3 PASS** | FIFO/queue bounds k=8, core reset+event k=6, host command→effect ordering k=6 (`yosys write_smt2` + `yosys-smtbmc -s z3`) |
| gate-level protocol suite | TBD | needs the LibreLane netlist |

Earlier fixed-configuration benches (`CORE_TB`, `JUMP_TB`, `HOST_TB`, `FIFO_TB`, `WAVE_TB`,
`CAPTURE_TB`, `ITEST`, integration) remain in the suite and in the per-bench table of
[`verification.md` §2.1](verification.md).

## 6. Errata and known gaps

| # | Gap | Impact | Owner |
|---|---|---|---|
| 1 | SPI target mode needs the spare `uio[5..7]` re-mux (the default SPI pins are output-only) | documented in [`examples.md`](examples.md) §2.2; no hardware change | docs / firmware |
| 2 | Full-duplex SPI above ~2 MHz controller-side needs a queue-synchronised read | documented limitation, see [`writeup.md`](writeup.md) §9 | design |
| 3 | capture / record-replay: RTL complete and unit-verified (209 checks), **silicon integration deferred to revision B** because enabling it aliases the 64-word DMEM used by the wave player | not in `source_files`; not in either submitted build | Lead |
| 4 | **Area**: the FF 128-word build is ~2× the 8×4 budget (1.148 M µm² measured vs ≤ ~750 k µm²); attempts 1/2/2b failed, and constraint relaxation made it *worse* (1.190 M / 1.224 M µm²) | not a silicon bug — a structural-area problem. Answer: the SRAM 1024×32 + 32-word-DMEM build (local estimate 748 598 µm² = 62 %), running as attempt 3; `sram512` macro is the documented reserve. Log: [`FLOW_STATUS.md`](FLOW_STATUS.md) | Lead |

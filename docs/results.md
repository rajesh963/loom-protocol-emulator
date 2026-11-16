# LOOM results

**What this is.** The measured facts: the two build variants, area and timing from synthesis and the
hardened flow, gate-level and formal results, and the state of Tiny Tapeout sign-off. Every number
is either measured (with the artefact that produced it) or explicitly `TBD`. Nothing in this file is
an estimate dressed up as a result.

CI commit: `5ff6c6c29cc7` (the final pushed SHA is recorded here when the repo goes public) · flow:
LibreLane via `tt-gds-action@ttihp27a`, PDK `ihp-sg13cmos5l` · clock target: 60 MHz
(`CLOCK_PERIOD` 16.67 ns). `docs` and `test` workflows are green on CI; the `test` workflow
reproduces the full cocotb matrix (57 PASS / 2 SKIP) on every push.
**Status: pre-place-and-route for §2; the first LibreLane run (FF build) is in progress.** §2 is a
yosys/IHP-liberty *estimate*; §3–§4 stay TBD until that run and precheck exist — they are not
estimated here.

---

## 1. Build variants

Two configurations are built in CI. The FF build guarantees that an SRAM-macro problem can never
block the tapeout; the SRAM build is the flagship that makes the full 1024-word program suite fit.

| | `PMEM_IMPL="ff"` fallback | `PMEM_IMPL="sram"` flagship |
|---|---|---|
| program memory | FF array, 128 words (`PMEM_WORDS` parameter) | `RM_IHPSG13_1P_1024x32` 1-port macro, 1024 words |
| macro area | 0 (flops) | 140 183 µm² (416.64 × 336.46 µm) = **~14 % of an 8×4 die** |
| macro ports | — | `A_CLK, A_MEN, A_WEN, A_REN, A_ADDR[9:0], A_DIN[31:0], A_DLY, A_DOUT[31:0], A_BM[31:0]` |
| read latency | 1 cycle | 1 cycle (`A_DLY=1` pipeline) |
| flow extras | none | `pdn_cfg.tcl` power grid + `Project.ExtendPowerStripes` LibreLane plugin, per the Tiny Tapeout reference `ttihp-cmos5l-sram-example` |
| purpose | zero-flow-risk tapeout, simulation, unit benches — **the first hardening run** | full protocol suite in one 1024-word image — **a config switch after the flow is green** |
| flow status | `TBD` (LibreLane in progress on CI `5ff6c6c29cc7`) | `TBD` (flips after FF flow is green) |

Die budget: 8×4 = 32 tiles × (202 × 155 µm) ≈ **1.0019 mm²** of nominal area, with a rule of thumb of
about 1K logic cells per tile (~32K cells for 8×4). The SRAM macro's 140 183 µm² is 11–14 % of that
area depending on utilisation, which is why the macro is the main area decision in the design.

**How the CI is actually run — and why.** The **first hardening run is the FF 128-word build**
(`VERILOG_DEFINES=["LOOM_PMEM_SMALL"]`, no SRAM macro) — deliberately, to de-risk the flow: it
exercises the whole LibreLane path (synthesis → floorplan → placement → CTS → routing → GDS →
precheck → gate-level sim) with no macro, no power-grid plugin and no `pdn_cfg.tcl`. The **SRAM
1024-word flagship is a config switch** (`PMEM_IMPL="sram"`, one `RM_IHPSG13_1P_1024x32`) flipped
only after that flow is green, so a macro/power-grid problem cannot block the tapeout or invalidate
a good run. Both configurations come from the same RTL: only `PMEM_IMPL`/`PMEM_WORDS` and the SRAM
flow's `pdn_cfg.tcl` / `Project.ExtendPowerStripes` differ. The submitted build is whichever is green
at submission time, with the FF build as the guaranteed fallback.

**Capture / record-replay (stretch) status:** the capture engine's RTL is **complete and
unit-verified** (`CAPTURE_TB`, 209 checks), but silicon integration is **deferred to revision B**
because enabling it aliases the 64-word DMEM used by the wave player. It is therefore not in
`info.yaml source_files` and not in either submitted build; it is the cheapest future feature to
enable (one parameter, one DMEM budget decision) and the reason the record-and-replay story in
[`writeup.md` §8](writeup.md) is credible rather than hypothetical.

## 2. Area — pre-place estimate

yosys + IHP `sg13cmos5l` liberty (`CLOCK_PERIOD` 16.67 ns), **before place and route**. This is a
budget estimate, not a flow result: it counts mapped flip-flops at 48.99 µm² each
(`sg13cmos5l_dfrbpq_1`) and generic gate instances before tech mapping/ABC optimisation.

| Build | Flip-flops | FF area (µm²) | Macro (µm²) | Pre-ABC logic (µm²) | Expected util. of 8×4 core |
|---|---|---|---|---|---|
| FF fallback (`PMEM_IMPL="ff"`, 128 words) | 8 038 | 8 038 × 48.99 ≈ **393 800** | 0 | ~0.5 M (≈ 44 k generic instances: 20.3 k MUX, 7.1 k AND, 6.5 k OR, 1.0 k XOR, 1.2 k NOT) | **~55–65 %** (tight) |
| SRAM flagship (one `RM_IHPSG13_1P_1024x32`) | 3 942 | 3 942 × 48.99 ≈ **193 100** | **140 183** (416.64 × 336.46 µm) | as above minus PMEM logic | **~55–65 %** (comfortable-ish), and **1024 instruction words** instead of 128 |

The SRAM build removes 4 096 PMEM flops (−~200 k µm²) and the 128:1 32-bit PMEM read mux
(−~70 k µm²) at the cost of one 140 k µm² macro: it is both smaller *and* eight times the program
memory, which is why it is the flagship configuration.

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

## 3. Area and timing (hardened LibreLane run)

| Metric | FF build | SRAM build |
|---|---|---|
| die area / utilization | TBD | TBD |
| instance count | TBD | TBD |
| worst negative slack (WNS) @ 60 MHz | TBD | TBD |
| total negative slack (TNS) | TBD | TBD |
| worst hold slack | TBD | TBD |
| power (typical) | TBD | TBD |
| DRC / LVS / antenna | TBD | TBD |

`src/config.json` currently ships `CLOCK_PERIOD = 20` (50 MHz); it is set to **16.67 ns (60 MHz)**
for the submitted build. Both are flow configuration, owned by the Lead; the RTL is unchanged.

## 4. Sign-off

| Check | Result | Artefact |
|---|---|---|
| `gds` GitHub Action | TBD | Actions run |
| `precheck` | TBD | Actions run |
| `gl_test` (gate-level protocol suite) | TBD | `test/results.xml` |
| formal F1–F9 (bounded, yosys SMT2 + z3) | TBD | `formal/` log |
| `docs` action | TBD | Actions run |

## 5. Verification results (landed RTL, simulation)

`make` from `test/` (Icarus Verilog 12 + cocotb): **59 tests, 57 PASS, 0 FAIL, 2 SKIP, 147 s wall**;
also reproduced by the `test` GitHub Actions workflow on every push. Full layer table, skips and the
sixteen-deviation history: [`verification.md` §9](verification.md).

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

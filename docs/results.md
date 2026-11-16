# LOOM results

**What this is.** The measured facts: the two build variants, area and timing from synthesis and the
hardened flow, gate-level and formal results, and the state of Tiny Tapeout sign-off. Every number
is either measured (with the artefact that produced it) or explicitly `TBD`. Nothing in this file is
an estimate dressed up as a result.

Git revision of the last results update: `TBD` · flow: LibreLane via
`tt-gds-action@ttihp27a`, PDK `ihp-sg13cmos5l` · clock target: 60 MHz (`CLOCK_PERIOD` 16.67 ns).

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
| purpose | zero-flow-risk tapeout, simulation, unit benches | full protocol suite in one 1024-word image |
| flow status | `TBD` (CI) | `TBD` (CI) |

Die budget: 8×4 = 32 tiles × (202 × 155 µm) ≈ **1.0019 mm²** of nominal area, with a rule of thumb of
about 1K logic cells per tile (~32K cells for 8×4). The SRAM macro's 140 183 µm² is 11–14 % of that
area depending on utilisation, which is why the macro is the main area decision in the design.

**Current submission plan:** submit the **SRAM flagship** build if its CI is green (it is the
configuration that runs the full 1024-word protocol suite), and fall back to the **FF build** if the
SRAM flow has any problem — the FF build exists precisely so that a macro issue can never block the
tapeout. Both configurations are built from the same RTL with no source differences other than
`PMEM_IMPL`/`PMEM_WORDS` (and the SRAM flow's `pdn_cfg.tcl` / `Project.ExtendPowerStripes`).

**Capture / record-replay (stretch) status:** the capture engine's RTL is **complete and
unit-verified** (`CAPTURE_TB`, 209 checks), but silicon integration is **deferred to revision B**
because enabling it aliases the 64-word DMEM used by the wave player. It is therefore not in
`info.yaml source_files` and not in either submitted build; it is the cheapest future feature to
enable (one parameter, one DMEM budget decision) and the reason the record-and-replay story in
[`writeup.md` §8](writeup.md) is credible rather than hypothetical.

## 2. Area (synthesis)

Measured with yosys + the IHP liberty file (`tools/area_synth.sh`, in progress); "cells" excludes
the macro.

| Build | Cells | Cell area (µm²) | Macro area (µm²) | Total logic+macro (µm²) | % of 8×4 die |
|---|---|---|---|---|---|
| FF fallback (128-word PMEM) | TBD | TBD | 0 | TBD | TBD |
| SRAM flagship (1024-word PMEM) | TBD | TBD | 140 183 | TBD | TBD |

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

## 5. Verification results (current)

| Layer | Result |
|---|---|
| `CORE_TB` — two concurrent periodic streams, exact 16-cycle and 8-cycle intervals, through event-queue back-pressure | **PASS** (Icarus Verilog 12) |
| `ITEST` — SPI host load/readback/run; 25 edges on `uo_out[2]`, every interval exactly 16 cycles, 0 errors | **PASS** |
| `HOST_TB` — all 13 host commands at 10 MHz and 100 kHz SCK, CS-abort, IRQ latch | **PASS** |
| `WAVE_TB` / `CAPTURE_TB` — wave player pattern walk / loop / done; capture engine 209 checks | **PASS** |
| ISA assembler/disassembler round-trip, all-op smoke program | TBD |
| Differential co-simulation vs golden model | TBD |
| Protocol VIP conformance | TBD |
| Gate-level protocol suite | TBD |
| Coverage matrix | TBD |

Full methodology and the tables to be filled: [`verification.md`](verification.md).

## 6. Errata and known gaps

| # | Gap | Impact | Owner |
|---|---|---|---|
| 1 | SPI target mode needs the spare `uio[5..7]` re-mux (the default SPI pins are output-only) | documented in [`examples.md`](examples.md) §2.2; no hardware change | docs / firmware |
| 2 | Full-duplex SPI above ~2 MHz controller-side needs a queue-synchronised read | documented limitation, see [`writeup.md`](writeup.md) §9 | design |
| 3 | capture / record-replay: RTL complete and unit-verified (209 checks), **silicon integration deferred to revision B** because enabling it aliases the 64-word DMEM used by the wave player | not in `source_files`; not in either submitted build | Lead |

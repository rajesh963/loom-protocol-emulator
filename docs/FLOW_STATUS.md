# LibreLane flow status (running log)

**What this is.** The canonical log of every hardening attempt on this design: what was built, how
far the flow got, the measured numbers, the diagnosis, and the corrective action. It is linked from
[`results.md` §3](results.md) and it is deliberately blunt — a flow that fails at placement before
tape-out is the flow *working*, and the numbers are only useful if they are recorded exactly as they
came out of the CI log. Nothing in this file is marked passing until the step actually passes.

Target: 8×4 tiles, IHP CMOS5L, 60 MHz (`CLOCK_PERIOD` 16.67 ns), flow `tt-gds-action@ttihp27a`.
Area budget for an 8×4 core: **≤ ~700–750 k µm² mapped cell area (~60 % effective utilisation)**.

---

## Attempt 1 — 2026-11-16 · FF 128-word build · commit `5ff6c6c`

**Result: FAILED at detailed placement (congestion).** This was *intentionally* the
smallest-risk build: `VERILOG_DEFINES=["LOOM_PMEM_SMALL"]`, instruction memory as a flip-flop array,
no SRAM macro, no power-grid plugin, no `pdn_cfg.tcl`. Its job was to exercise the whole LibreLane
path with zero macro risk — and it did: it told us the design is **~2× over the area budget** and
exactly where the area is. That is the flow doing its job before tape-out, not a surprise.

| Metric | Value |
|---|---|
| Die | 1724.16 × 710.64 µm |
| Core area | **1 208 172 µm²** |
| Mapped cell area after synthesis + ABC | **1 147 604 µm²** |
| Effective utilisation at floorplan | **95.0 %** |
| Post-resizer utilisation | **98.4 %** |
| Global-placement routing overflow | 2 125.6 (8 917 tiles = **38.1 %**) |
| Failing step | `OpenRoad.RepairDesignPostGPL` → `[DPL-0036] Detailed placement failed` (451 instances) |
| Hit the failing step? | no precheck, no gate-level sim, no GDS — the run stopped at placement |

Cell histogram (from the same log, the part that points at the fix):

| Class | Count | Area | Share |
|---|---|---|---|
| flip-flops | 8 042 | 394 k µm² | 34 % |
| buffer cells (all types, incl. `buf_1`) | **35 510** | **305 304 µm²** | **27 %** |
| of which `buf_1` | 29 195 | 212 k µm² | 18 % |
| `mux2_1` + `mux4_1` | 5 658 + 3 063 | ≈ 220 k µm² | 19 % |

**Diagnosis.** At 1.148 M µm² the design needs ~60 % of its current area before placement can
succeed at any target density: the floorplan was already 95 % full and GPL overflowed 38 % of tiles.
The two dominant, fixable costs are (a) **PMEM as a flop array plus the 128:1 32-bit read mux**
(~320 k µm²) and (b) **ABC's synthesis-time fanout buffering** (305 k µm², 27 % of the design)
inserted because the PDK's `MAX_FANOUT_CONSTRAINT` is aggressive for a 130 nm process at 60 MHz.

## Corrective actions (in flight)

| # | Action | Rationale | Status |
|---|---|---|---|
| 1 | Raise `MAX_FANOUT_CONSTRAINT` to 40 at pdk+scl scope in `src/config.json` (commit `33012d5`) | 60 MHz on 130 nm has ample slack; let OpenROAD repair real violations post-placement instead of ABC buffer everything up front. Removes most of the 305 k µm² buffer cost | **pushed; fresh FF-build run in progress (attempt 2)** |
| 2 | Replace the 4 096-flop PMEM array + read mux with one `RM_IHPSG13_1P_1024x32` SRAM macro (140 183 µm²) | ~−180 k µm² and **1024 instruction words** instead of 128 — the flagship configuration | in implementation |
| 3 | Shrink DMEM 64→32 words (two 32:1 read muxes) and TX/RX FIFOs 8→4 | ~−110 k µm²; documented shrink levers that do not touch the programming model | held in reserve |
| 4 | If still over after 1–3: 4→2 threads or 32→24-bit instruction width | last-resort architectural cuts | not planned unless required |

## Attempt 2 — FF build with relaxed fanout · commit `33012d5`

**Status: running.** Expected to recover a large part of the 305 k µm² buffer cost; the measured
result will replace this paragraph, with the same table shape as Attempt 1 (die/core, mapped area,
floorplan + post-resizer utilisation, GPL overflow, failing step, cell histogram) plus WNS/TNS if it
reaches routing. No number is predicted here — only recorded.

## Attempt 3 — SRAM 1024-word flagship

**Status: not started.** Config switch (`PMEM_IMPL="sram"`, one `RM_IHPSG13_1P_1024x32`) plus the
CMOS5L SRAM flow setup (`pdn_cfg.tcl`, `Project.ExtendPowerStripes`). This is the configuration that
runs the full protocol suite from a single 1024-word image.

## How to read this log

* An attempt is only "passing" when `gds` **and** `precheck` **and** `gl_test` are green on the same
  commit; anything less is recorded as a failure or in-progress.
* Every number above is copied from the CI log of the commit named in the heading — no estimates,
  no extrapolations. Estimates live in [`results.md` §2](results.md) and are labelled as such.
* The submission build is whichever attempt is green at submission time; the FF build is the
  guaranteed fallback, so a macro or power-grid problem can never block the tapeout.

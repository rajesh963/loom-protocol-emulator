# LibreLane flow status (running log)

**What this is.** The canonical log of every hardening attempt on this design: what was built, how
far the flow got, the measured numbers, the diagnosis, and the corrective action. Linked from
[`results.md` §3](results.md). It is deliberately blunt — a flow that fails at placement before
tape-out is the flow *working*, and the numbers are only useful if they are recorded exactly as they
came out of the CI log.

Target: 8×4 tiles, IHP CMOS5L, 60 MHz (`CLOCK_PERIOD` 16.67 ns), flow `tt-gds-action@ttihp27a`.
**Area budget: ≤ ~700–750 k µm² mapped cell area (~60 % effective utilisation of the ~1.21 M µm²
core).** The budget tracker is now the *post-synthesis mapped area* measured by the flow, not a
pre-ABC estimate (see the lesson at the end).

---

## Attempt 1 — FF 128-word build · commit `5ff6c6c` — **FAILED at detailed placement**

Intentionally the smallest-risk build: `VERILOG_DEFINES=["LOOM_PMEM_SMALL"]`, instruction memory as a
flip-flop array, no SRAM macro, no power-grid plugin, no `pdn_cfg.tcl`. Its job was to walk the whole
LibreLane chain with zero macro risk — and it did: it told us the design is ~2× over the area budget
and exactly where the area is.

| Metric | Value |
|---|---|
| Die / core | 1724.16 × 710.64 µm / **1 208 172 µm²** |
| Mapped cell area after synthesis + ABC | **1 147 604 µm²** |
| Effective utilisation at floorplan / post-resizer | **95.0 % / 98.4 %** |
| Global-placement routing overflow | 2 125.6 (8 917 tiles = **38.1 %**) |
| Failing step | `OpenRoad.RepairDesignPostGPL` → `[DPL-0036] Detailed placement failed` (451 instances) |

Cell histogram — the part that points at the fix:

| Class | Count | Area | Share of 1 147 604 µm² |
|---|---|---|---|
| flip-flops | 8 042 | 394 k µm² | 34 % |
| **buffer cells (all types)** | **35 510** | **305 304 µm²** | **27.0 %** |
| — of which `buf_1` | 29 195 | 211 958 µm² (7.26 µm² each) | 18.5 % |
| — `buf_2` / `buf_4` / `buf_8` / `buf_16` | 2 975 / 1 892 / 1 227 / 221 | ≈ 93 k µm² | 8.1 % |
| `mux2_1` + `mux4_1` | 5 658 + 3 063 | ≈ 220 k µm² | 19 % |

## Attempt 2 — FF build, `MAX_FANOUT_CONSTRAINT = 40` · commit `33012d5` — **FAILED, worse**

Hypothesis: the 29 k `buf_1` cells came from synthesis-time fanout buffering under the PDK default
constraint (believed 6), so relaxing it to 40 at `pdk::ihp-sg13cmos5l` +
`scl::sg13cmos5l_stdcell` scope would recover area, with OpenROAD repairing genuine violations
post-placement.

**Result: mapped area 1 190 170 µm², GPL utilisation 100.96 %, `[GPL-0301]` — i.e. worse than
attempt 1.** Reverted.

## Attempt 2b (25 MHz probe) — `CLOCK_PERIOD = 40 ns` — **FAILED, worse still**

Testing whether the buffers were a timing-constraint artefact: **mapped area 1 223 970 µm²,
GPL utilisation 101.3 %.** Reverted.

**Conclusion (a genuine methodology finding):** the ~30 k buffer cells are produced by the ABC
**SCL sequential-mapping pass**, not by the fanout or timing constraints. Loosening constraints does
not recover the area and perturbs the mapping adversely. **Constraint knobs are a dead end;
structural area is the only lever.** That is why attempt 3 is architectural rather than
configuration.

---

## Attempt 3 — SRAM 1024×32 instruction memory + 32-word DMEM, 60 MHz · commit `35c3c42` — **AREA SOLVED, failed on a placement-mode check**

| | |
|---|---|
| configuration | `VERILOG_DEFINES = ["LOOM_PMEM_SRAM","LOOM_DMEM_SMALL"]`, `CLOCK_PERIOD 16.67` |
| macro | `RM_IHPSG13_1P_1024x32_c2_bm_bist` at `u_mem.u_pmem.sram` |
| flow extras | official Tiny Tapeout CMOS5L SRAM flow plugin + `pdn_cfg.tcl` |
| **fixed (macro) instance area** | **148 563 µm²** |
| **movable standard-cell area** | **473 838 µm²** |
| **total placed area** | **622 401 µm²** — **51.5 %** of the 1 208 172 µm² core by simple area ratio |
| flow-reported cumulative utilisation | **44.7 %** — *movable-cell area relative to the free (non-macro) core area*: 473 838 / (1 208 172 − 148 563) = 44.72 %, which is what OpenROAD's placer reports. The raw cumulative figure **51.5 %** counts the macro too: 622 401 / 1 208 172. Both are kept, labelled (attempt 1: 98.4 %) |
| placement target density / GPL overflow | target 0.6; overflow converged 0.999 → **0.299** |
| failing step | `OpenRoad.GlobalPlacement` (`gpl.tcl:88`) — **`[GRT-0010] Instance _40267_ is not placed`**, raised inside GPL's *routability-driven* mode (the routability check runs the global router on a mid-placement snapshot) |
| reached GDS/precheck/GL? | **no** |
| simulation on this exact RTL | **59/59 PASS, 0 fail, 0 skip** (`make SRAM_SIM=1024`, 154 s) |

**Reading:** the area problem is solved — **−46 % area versus attempt 1** (622 401 / 1 147 604 = 54.2 %), 44.7 % movable-relative utilisation, and the
failure is a placement-*mode* quirk with a one-line configuration fix, not a design or capacity
problem. **Estimator note:** the local pre-flow estimator predicted 748 598 µm² (62 %); the flow
measured 622 401 µm² (51.5 %), i.e. the estimator was *conservative* by ~17 % on the SRAM build — it
over-prices generic memory muxes that the macro replaced. Honest calibration: the pre-ABC estimator
was pessimistic here and optimistic on the FF build, so only post-synthesis mapped area is a budget.

## Attempt 4 — same design, routability-driven placement disabled · commit `5732b23` — **RUNNING**

Identical RTL and SRAM configuration; the only change is `"PL_ROUTABILITY_DRIVEN": false` in
`src/config.json` (LibreLane/OpenROAD `GlobalPlacement` variable, default `true`) — commit
`5732b23` ("flow: disable routability-driven global placement (attempt 4)"), full SHA
`5732b23250ecb23b1e7c1b8f4a447701f8fa9b36`, CI run `43905211454`. At 44.7 %
utilisation routability-driven placement is unnecessary, and it is the only step that failed in
attempt 3. **Prediction: attempt 4 proceeds through detailed placement → CTS → routing → GDS →
precheck → gate-level simulation.** WNS/TNS/utilisation/precheck/GL rows will be recorded here from
its CI log.

## Area lever still in reserve — `RM_IHPSG13_1P_512x32`

`tools/select_build.sh sram512` swaps to a 512×32 macro (79 720 µm²): **561 938 µm² = 46.5 % of the
1 208 172 µm² core** by the raw-ratio convention (622 401 − (140 183 − 79 720)), with a documented
512-word firmware limit (four 128-word slots at `0x000`/`0x080`/`0x100`/`0x180`; highest used word
509). Not needed unless attempt 4 misses.

## How to read this log

* An attempt is only "passing" when `gds` **and** `precheck` **and** `gl_test` are green on the same
  commit; anything less is recorded as a failure or in-progress.
* Every number above is copied from the CI log of the commit named in the heading — no estimates,
  no extrapolations. Estimates say so explicitly and live in [`results.md` §2](results.md).
* **Budget lesson:** pre-ABC cell counts cannot predict ABC's fanout buffering under a process
  fanout constraint, so only **post-synthesis mapped area** is a usable budget number. That is the
  number attempt 1 produced, and it is now the tracker every attempt is judged against.
* The submitted build is the SRAM configuration (`35c3c42` onward); the FF build exists for fast
  regression and cannot be a tape-out fallback (1.147 M µm² measured — 98.4 %).
* Attempt 3 also settled the estimator question: on the SRAM build the pre-flow estimate was
  **conservative** (748 598 predicted vs 622 401 measured), the opposite of the FF build — the
  estimator over-prices generic memory muxes that the macro replaces.

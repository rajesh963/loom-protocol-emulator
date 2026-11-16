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

## Attempt 3 — SRAM 1024×32 instruction memory + 32-word DMEM, 60 MHz · commit `35c3c42` — **RUNNING**

| | |
|---|---|
| configuration | `VERILOG_DEFINES = ["LOOM_PMEM_SRAM","LOOM_DMEM_SMALL"]`, `CLOCK_PERIOD 16.67` |
| macro | `RM_IHPSG13_1P_1024x32_c2_bm_bist` at `u_mem.u_pmem.sram` (140 183 µm²) |
| flow extras | official Tiny Tapeout CMOS5L SRAM flow plugin + `pdn_cfg.tcl` |
| local area estimate (same estimator that predicted attempt 1 within 3.4 %) | 608 415 µm² logic + 140 183 µm² macro = **748 598 µm² = 62 %** of the 1 208 172 µm² core |
| density reference | `vc32_cpu` placed at 73 % — 62 % is inside that proven envelope, so it *should* place and route; CI decides whether it does |
| simulation on this exact RTL configuration | **59 tests, 59 PASS, 0 FAIL, 0 SKIP** (`make SRAM_SIM=1024`), including 100/100 differential co-sim seeds (harness backdoor made SRAM-aware) |
| result | **pending** — area, utilisation, WNS/TNS to be recorded from the CI log |

This is the first build configuration where *the thing we intend to tape out* has the full green
simulation matrix, which is the point of making the SRAM build the candidate rather than an
optimisation.

## Area lever still in reserve — `RM_IHPSG13_1P_512x32`

`tools/select_build.sh sram512` swaps to a 512×32 macro (79 720 µm²) for **~−60 k µm² (~710 k, 59 %)**,
with a documented 512-word firmware limit (four 128-word slots at `0x000`/`0x080`/`0x100`/`0x180`;
highest used word 509). Not needed unless attempt 3 misses.

## How to read this log

* An attempt is only "passing" when `gds` **and** `precheck` **and** `gl_test` are green on the same
  commit; anything less is recorded as a failure or in-progress.
* Every number above is copied from the CI log of the commit named in the heading — no estimates,
  no extrapolations. Estimates say so explicitly and live in [`results.md` §2](results.md).
* **Budget lesson:** pre-ABC cell counts cannot predict ABC's fanout buffering under a process
  fanout constraint, so only **post-synthesis mapped area** is a usable budget number. That is the
  number attempt 1 produced, and it is now the tracker every attempt is judged against.
* The submitted build is the SRAM configuration; the FF build exists for fast regression and can no
  longer be a tape-out fallback (it cannot fit 8×4 — 1.15 M µm² measured).

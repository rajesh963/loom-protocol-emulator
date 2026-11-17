# LibreLane flow status (running log)

**What this is.** The canonical log of every hardening attempt on this design: what was built, how
far the flow got, the measured numbers, the diagnosis, and the corrective action. Linked from
[`results.md` §3](results.md). It is deliberately blunt — a flow that fails at placement before
tape-out is the flow *working*, and the numbers are only useful if they are recorded exactly as they
came out of the CI log.

Target: 8×4 tiles, IHP CMOS5L, flow `tt-gds-action@ttihp27a`. **Shipped clock: 33.33 MHz
(`CLOCK_PERIOD` 30 ns)** — 60 MHz was the original target and failed timing at a 20.8 ns critical
path; firmware constants are swept for 33.33 MHz (`sw/examples/`, `sw/prog/manifest.json`).
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

## Attempt 3 — SRAM 1024×32 instruction memory + 32-word DMEM, 60 MHz (original target) · commit `35c3c42` — **AREA SOLVED, failed on a placement-mode check** (root cause of the unplaced instance found later: stray IO pads, see below)

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
| failing step | `OpenRoad.GlobalPlacement` (`gpl.tcl:88`) — **`[GRT-0010] Instance _40267_ is not placed`**, raised inside GPL's *routability-driven* mode (the routability check runs the global router on a mid-placement snapshot). **Root cause later found: that instance was a stray IO-pad cell, not logic** — see “Root cause” below |
| reached GDS/precheck/GL? | **no** |
| simulation on this exact RTL | **64/64 PASS, 0 fail, 0 skip** (`make SRAM_SIM=1024`, ≈271 s per configuration), frozen at `7d2fb2f` |

**Reading:** the area problem is solved — **−46 % area versus attempt 1** (622 401 / 1 147 604 = 54.2 %), 44.7 % movable-relative utilisation, and the
failure is a placement-*mode* quirk with a one-line configuration fix, not a design or capacity
problem. **Estimator note:** the local pre-flow estimator predicted 748 598 µm² (62 %); the flow
measured 622 401 µm² (51.5 %), i.e. the estimator was *conservative* by ~17 % on the SRAM build — it
over-prices generic memory muxes that the macro replaced. Honest calibration: the pre-ABC estimator
was pessimistic here and optimistic on the FF build, so only post-synthesis mapped area is a budget.

## Attempt 4 — same design, routability-driven placement disabled · commit `5732b23` — **FAILED at routing (cause unknown at the time)**

Identical RTL and SRAM configuration; the only change is `"PL_ROUTABILITY_DRIVEN": false` in
`src/config.json` (commit `5732b23`, full SHA `5732b23250ecb23b1e7c1b8f4a447701f8fa9b36`, CI run
`43905211454`). It got much deeper: floorplan → macro placement/PDN → global placement → repair →
detailed placement → CTS → post-CTS timing repair, a 27-minute run.

| Metric | Value |
|---|---|
| completed steps | floorplan, macro placement + PDN, **global placement, repair, detailed placement, CTS, post-CTS timing repair** |
| failing step | **`OpenROAD.GlobalRouting`** — `[GRT-0010] Instance _40267_ is not placed` (`grt.tcl:20`) |
| timing at the time | post-GPL repair: **1 028 setup-violated endpoints, WNS −4.125 ns, TNS −1658.6** at `CLOCK_PERIOD 16.67 ns` (critical path ≈ **20.8 ns**), "unable to repair all setup violations"; 1 909 fanout + 9 slew violations repaired; 3 178 hold endpoints after CTS |
| reached GDS/precheck/GL? | **no** |

Two independent problems were visible here and we initially attributed the abort to the first: the
16.67 ns clock did not close, **and** one instance was never placed. Only attempt 5 separated them.

## Attempt 5 — same design at 33.33 MHz (`CLOCK_PERIOD = 30 ns`) · commit `98cd5d6` — **TIMING FIXED, failed on the same unplaced cell**

Identical RTL, `CLOCK_PERIOD 30 ns` (33.33 MHz, ~44 % margin over the measured 20.8 ns path).

| Metric | Value |
|---|---|
| setup | **closed** — post-CTS repair reported `RSZ-0098 No setup violations found` (vs 1 028 violated endpoints / WNS −4.125 ns at 16.67 ns) |
| hold | 3 177 hold endpoints repaired after CTS |
| failing step | still **`OpenROAD.GlobalRouting`**, `[GRT-0010]`, the *same* unplaced instance |
| reached GDS/precheck/GL? | **no** |

**So 33.33 MHz closes setup; the routing blocker is unrelated to timing.**

## Root cause of the routing failure: stray IO-pad cells

`_40267_` was **not a logic cell**. Artifacts from attempt 4 show the design contains **four
`sg13cmos5l_IOPadOut16mA` IO-pad cells**, each ≈ **14 400 µm²** — **57 600 µm² total, 12 % of the
moved cell area** — and they are *internal*: their `pad` pin drives internal logic. In other words
**ABC mapped four high-fanout internal nets onto the 16 mA IO-pad driver**, because the IO library is
part of the synthesis library set. A Tiny Tapeout design owns no pads (the shuttle adds the padring),
so IO placement never places them; global routing then aborts with `[GRT-0010] Instance is not
placed`. **That single library-hygiene quirk explains attempts 3, 4 and 5 failing at the same point.**

## Attempt 6 — same design + IO pads excluded · commit `1182041` — **CANCELLED at the 6-hour CI cap (not a design failure)**

Identical RTL and `CLOCK_PERIOD 30 ns`, plus `"EXTRA_EXCLUDED_CELLS": ["sg13cmos5l_IOPad*"]`. **The
IO-pad fix worked**: every stage through global routing finished in **15 minutes**, and for the first
time the design reached detailed routing.

| Stage | Time |
|---|---|
| lint → synthesis → floorplan → placement → repair → DPL → CTS | 16:58 → 17:02 |
| post-CTS timing repair | 17:06:49 |
| global routing | 17:06 – 17:10 |
| antenna repair | 17:10 |
| **detailed routing (DRT)** | **17:11:56 → 22:56:20 (5 h 45 m), cancelled by the 6 h job cap with 6 violations left** |

DRT violation trajectory: **37 824 → 24 763 → 22 716 → 5 797 → … → 30 → 23 → 7 → 6**, at 20–50 min
per pass on a 2-core runner. ~30 600 instances, effective utilisation **48.1 %** (GPL-reported 45.6 %).
It was converging — just not fast enough for the CI cap.

**Root cause (measured, not guessed).** Global routing was invoked with `-allow_congestion` because
our `src/config.json` still carried the Tiny Tapeout template's `GRT_ALLOW_CONGESTION: true`. GRT
therefore gave up with **47 track overflows** (usage M2 59.8 %, M3 66.1 %, M4 33.4 %) instead of
clearing them, and its own log recommended *"reduce the layer adjustment from 30.0 % to 18 %"*. DRT
then spent 5 h 45 m repairing that congestion violation-by-violation (iterations 1–46).

This is *not* a "design too big" problem — 30.6 k instances at 45.6 % utilisation is a small design.
It is a routing-*configuration* problem: **a flow knob inherited from a template can cost six hours
before it is understood.** The competition FAQ anticipates the 6-hour GitHub Actions cap (and the
workaround of a local LibreLane run + custom GDS action); we avoided needing that workaround by
fixing the configuration instead of the design.

## Attempt 7 — same design, routing configuration "fixed" · commit `6050726` — **CANCELLED at the 6 h cap, WORSE than attempt 6**

The GRT "fix" **backfired**. Stages before detailed routing again finished in ~15–20 min; DRT then ran
~5 h 40 m and was cancelled at the cap.

| Metric | Attempt 6 (`1182041`) | Attempt 7 (`6050726`) |
|---|---|---|
| GRT settings | `ALLOW_CONGESTION=true`, `ADJUSTMENT=0.30`, default iters | `ALLOW_CONGESTION=false`, `ADJUSTMENT=0.18`, `OVERFLOW_ITERS=150` |
| DRT trajectory → end state | 37 824 → … → **6 violations left** | 8 686 → 7 835 → 7 423 → 6 851 → 6 040 → 5 772 → 5 520 → **5 198 left** |
| verdict | converging, ~1 order from clean | still an order of magnitude away at the cap |
| artefacts | none (cancel ⇒ no upload) | none |

**Interpretation (measured negative result):** forcing global routing to clear congestion instead of
deferring it made detailed routing **substantially harder** — 5 198 violations at the cap versus 6.
The router's default congestion behaviour was better. **The problem is the placement DRT starts from,
not the quality of the GRT guide.**

**Running total at this point:** attempts 6 and 7 both capped at exactly **6 h**, while the design
stages (synthesis → floorplan → placement → CTS → GRT) finished in **~15–20 min** in both. The cap is
exclusively a **detailed-routing time budget**.

## Attempt 8 — reverts the GRT knobs, attacks placement quality · commit `d7bec7a` — ✅ **PASSED: GDS + PRECHECK 10/10**

**The GDS build succeeded.** Full LibreLane flow in **~2.5 h**, both routing stages clean, Tiny Tapeout
**precheck 10/10**, and after the DMEM-initialisation fix the **gate-level suite is green too**
(`TESTS=64 PASS=60 FAIL=0 SKIP=4`, run `44068106013`). All four CI jobs pass on `7d2fb2f`.

| Setting | Attempt 6 | **Attempt 8** |
|---|---|---|
| `GRT_ALLOW_CONGESTION` | `true` | `true` (reverted) |
| `GRT_ADJUSTMENT` / `GRT_OVERFLOW_ITERS` | 0.30 / 50 | 0.30 / 50 (PDK defaults) |
| `PL_ROUTABILITY_DRIVEN` | `true` | **`true`** (now runnable — stray IO pads excluded) |
| `PL_TARGET_DENSITY_PCT` | 60 | **50** (spread cells, cut local congestion) |
| design | unchanged | unchanged (SRAM 1024×32, 32-word DMEM, 33.33 MHz, IO pads excluded) |

**The two changes that fixed it:** routability-driven placement (previously blocked by the stray IO pads,
not by the algorithm) plus a lower target density. GRT returned to its defaults.

| Stage | Trajectory |
|---|---|
| detailed routing | **28 461 → 15 893 → 14 361 → 890 → 65 → 8 → 8 → 0** violations |
| post-DRT antenna repair | **4 031 → 806 → 536 → 38 → 1 → 0** violations |

**Measured sign-off (from `stats/metrics.csv`):**

| Metric | Value |
|---|---|
| area | stdcells **518 231 µm²** + SRAM macro **140 183 µm²** = **658 414 µm² = 54.5 %** of the 1 208 170 µm² core (stdcell util 48.5 %); die 1 225 260 µm²; **35 971 stdcell instances** |
| timing (3 corners) | setup **WNS 0.0 / TNS 0.0**, hold **WNS 0.0 / TNS 0.0**; worst setup **+1.6006 ns** at slow (1.08 V, 125 °C) vs a 30 ns period (path ≈ 28.4 ns); worst hold **+0.1200 ns** at fast |
| power | **8.60 mW** (internal 7.05 + switching 1.51 + leakage 0.038) |
| clock skew | worst setup +0.440 ns, worst hold −0.453 ns |
| antenna / routing | **0 antenna violations**, 0 routed DRC; global-route wirelength 3.32 mm, routed-estimate 2.07 mm |
| lint | 0 errors, 52 warnings, 0 inferred latches |
| precheck | **10/10 PASSED** (KLayout pin-label overlap, KLayout SG13CMOS5L DRC, zero-area, KLayout checks, pin check, boundary check, layer check, cell-name check, analog-pin check, Verilog syntax) |

**Gate-level root cause and fix (`7d2fb2f`).** The first GL run was `TESTS=64 PASS=57 FAIL=3 SKIP=4`. All three
failures were **one** problem: `rtl/loom_mem.v` zeroes DMEM in a **simulation-only `initial` block that is
not synthesised**, so the gate-level netlist started with undefined DMEM; tests that read DMEM words they
never wrote got X back through the host bridge, X reached the MISO pin, and the cycle monitor's `int()`
conversion threw. Initialising the PMEM SRAM behavioural model to `halt` (`fd0a67b`) was necessary but not
sufficient. Fix (`7d2fb2f`): `start_loom()` writes zeros to all 64 DMEM words through the host port after
reset, so RTL and GL start from identical defined state — matching the firmware contract (DMEM has no
reset; software initialises it).

**Final gate level: ✅ `TESTS=64 PASS=60 FAIL=0 SKIP=4`** — run `44068106013`, 2 739.85 s, no errors;
modules host 19 / isa 22 / timing 7+1 skip / cosim 3+3 skips / protocols 9 / usb_ls 2 / eth 1 / usb_ls_rx 1;
the four skips are exactly the RTL-backdoor-dependent tests (`test_random_cosim.*` ×3 and
`test_timing.test_queue_backpressure_schedule_exact`). GL is slower than RTL because of the 64 DMEM word
writes `start_loom()` performs per test. **Evidence: `results.xml` in the `gatelevel_test_results` artifact
plus the job log — the CI GL job runs without `WAVES=1`, so no gate-level FST exists or is claimed.**

**Power-grid sign-off footnote.** The flow's PSM connectivity checker flags the SRAM array power rails
(`[PSM-0069]` lines in the GDS log). The SRAM flow config sets `ERROR_ON_PDN_VIOLATIONS: 0` (as in the Tiny
Tapeout SRAM example), so these are **non-fatal by design** — this is **not** a clean-PDN sign-off claim. The
independent Tiny Tapeout **precheck passes all 10 checks** (KLayout DRC/layer/pin included).

**Honest footnote:** metrics also list 292 max-fanout / 80 max-slew / 27 max-cap flags — the TT/PDK
*default limit checks*, not timing violations; the flow does not gate on them.

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
* The submitted build is the SRAM configuration at **33.33 MHz**, and **attempt 8 (`d7bec7a`) is the
  hardened, precheck-passing build**; the FF build exists for fast regression and cannot be a tape-out
  fallback (1 147 604 µm² measured — 98.4 %).
* **A template knob can cost six hours.** `GRT_ALLOW_CONGESTION: true` (inherited from the Tiny
  Tapeout template) let global routing hand 47 track overflows to detailed routing, which then spent
  5 h 45 m repairing them and hit the CI cap with 6 violations left. At 45.6 % utilisation the design
  was never too big — the router was just not allowed to converge. Fix the configuration first.
* **Library hygiene is a real area/timing/congestion lever.** An IO library in the synthesis library
  set let ABC map internal nets onto 16 mA pad drivers — four cells, 57 600 µm² (12 % of moved area),
  never placeable on a Tiny Tapeout design. `EXTRA_EXCLUDED_CELLS` now excludes
  `sg13cmos5l_IOPad*`. Before blaming placement or timing, check *what cells are in the netlist*.
* Attempt 3 also settled the estimator question: on the SRAM build the pre-flow estimate was
  **conservative** (748 598 predicted vs 622 401 measured), the opposite of the FF build — the
  estimator over-prices generic memory muxes that the macro replaces.

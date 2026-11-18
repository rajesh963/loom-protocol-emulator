# LOOM results

**What this is.** The measured facts: the two build variants, the area estimate and its correction, the
full hardening-attempt history, the **final GDS build with precheck and timing closure**, the
verification matrix, and the honest remaining gaps. Every number is either measured (with the artefact
that produced it) or explicitly `TBD`. Nothing here is an estimate dressed up as a result, and nothing
is a silicon measurement — no LOOM silicon exists yet.

**Submission build:** SRAM `RM_IHPSG13_1P_1024x32` instruction memory + 32-word DMEM, **33.33 MHz**
(`CLOCK_PERIOD` 30 ns), design commit **`d7bec7a`** (attempt 8); submitted tree **`352e473`** (docs/test/
hygiene only since the design commit).

**FINAL: all four CI jobs GREEN on `7d2fb2f` (run `44068106013`) — `gds` ✅ · `precheck` ✅ ·
`viewer` ✅ · `gl_test` ✅ with `TESTS=64 PASS=60 FAIL=0 SKIP=4` (2 739.85 s)** — module breakdown
host 19, isa 22, timing 7 + 1 skip, cosim 3 + 3 skips, protocols 9, usb_ls 2, eth 1, usb_ls_rx 1; the
four skips are exactly the RTL-backdoor-dependent tests (`test_random_cosim.*` ×3 and
`test_timing.test_queue_backpressure_schedule_exact`). CI evidence is `results.xml` from the
`gatelevel_test_results` artifact plus the job log — **the CI gate-level job does not run with
`WAVES=1`, so no gate-level FST is produced or claimed**. **Design and verification are frozen at
`7d2fb2f`; the submission tree is git tag `submission-1`** (docs/hygiene commits only after the design
freeze).
**GDS BUILD PASSED · PRECHECK 10/10 PASSED · DRC clean · timing closed at all three corners · 8.60 mW.**
Gate-level: the first run was `57/60`, and the three failures were **one** root cause — DMEM is zeroed by
a **simulation-only `initial` block** in `rtl/loom_mem.v` that is not synthesised, so the gate-level
netlist starts with undefined DMEM; tests reading DMEM words they never wrote got X through the host
bridge, X reached MISO, and the cycle monitor's `int()` threw. Fixed in `7d2fb2f` (`start_loom()` writes
zeros to all 64 DMEM words through the host port after reset, so RTL and GL start from the same defined
state — mirroring the firmware contract). **Final GL result: `TESTS=64 PASS=60 FAIL=0 SKIP=4` (2 740 s)**
— the four skips are RTL-backdoor-dependent tests, and the longer wall time versus RTL is the 64 DMEM
word writes `start_loom()` now performs per test.

RTL simulation verification is frozen at `7d2fb2f`: **64/64 PASS on both memory builds, 0 skips,
64/64 from a clean clone**, with `test` and `docs` workflows green on the fix commit `7d2fb2f`; the tape-out facts above are unchanged since `d7bec7a` (only test/sim/host files changed).

---

## 1. Build variants

The **tape-out candidate is the SRAM configuration** — 1024×32 SRAM instruction memory plus a 32-word
DMEM, **33.33 MHz shipped** — because it is the only configuration that can fit 8×4 *and* the only one
whose exact RTL has the full green simulation matrix. The FF 128-word build is a simulation/regression
configuration: fast to elaborate, used for most unit runs, but measured at 1.147 604 µm² it cannot fit an
8×4 core.

| | **SRAM candidate (submitted)** | FF build (sim/regression only) | SRAM-512 reserve |
|---|---|---|---|
| instruction memory | `RM_IHPSG13_1P_1024x32_c2_bm_bist` (140 183 µm²) at `u_mem.u_pmem.sram` | FF array, 128 words (`LOOM_PMEM_SMALL`) | `RM_IHPSG13_1P_512x32` (79 720 µm²) |
| program words | **1024** | 128 | 512 (four 128-word slots) |
| data memory | 32 words (`LOOM_DMEM_SMALL`) | 64 words | 32 words |
| measured area | stdcells **518 231 µm²** + macro **140 183 µm²** = **658 414 µm² = 54.5 %** of the 1 208 170 µm² core (48.5 % stdcell util; die 1 225 260 µm²; 35 971 stdcell instances) | **1 147 604 µm² = 98.4 % → placement failed** | 561 938 µm² = 46.5 % raw ratio |
| simulation matrix | **64/64 PASS, 0 skip** | 64/64 PASS | same RTL as candidate |
| flow status | **GDS + precheck 10/10 PASSED** (§4) | area experiments only; cannot fit | not needed |

Die budget: 8×4 = 32 tiles ≈ **1.0019 mm²** nominal; the core is 1 208 170 µm² and the SRAM macro is
~11.6 % of the die. **Budget tracker: post-synthesis mapped area ≤ ~700–750 k µm² (~60 % utilisation)** —
see §2 for why a pre-ABC estimate is not a budget.

**SRAM program-memory rules:** host-writable only, and only while the owning threads are halted or
disabled (load with `SET_ADDR`/`WRITE_MEM`, then `SET_PC`/`RUN`); read latency one cycle (`A_DLY=1`).
The 512-word reserve build keeps four 128-word slots at `0x000`/`0x080`/`0x100`/`0x180` (highest usable
word 509).

**Capture / record-replay (stretch):** RTL complete and unit-verified (`CAPTURE_TB`, 209 checks);
silicon integration deferred to revision B because enabling it aliases the wave-player DMEM.

## 2. Area — pre-place estimate (and what the flow actually measured)

yosys + IHP `sg13cmos5l` liberty, before place and route — kept because it is the calibration lesson:

| Build | Flip-flops | Modelled area | Reality |
|---|---|---|---|
| FF (`PMEM_IMPL=ff`, 128 words) | 8 038 | ~0.5 M µm² pre-ABC | **1 147 604 µm² measured** — ABC's fanout buffering and the 128:1 read mux were under-counted |
| SRAM 1024×32 + 32-word DMEM (candidate) | 3 942 | 608 415 µm² logic + 140 183 µm² macro = 748 598 µm² (62 %) | **658 414 µm² measured (54.5 %)** — the estimator was ~14 % *conservative* here because the macro replaced the muxes it over-prices |

**The budget lesson:** pre-ABC cell counts cannot predict ABC's fanout buffering under a process fanout
constraint, so only **post-synthesis mapped area** is a usable budget number. The pre-flow estimate was
~2× optimistic on the FF build and ~14 % pessimistic on the SRAM build — directional errors in both
directions, which is why the tracker is the measured number.

## 3. Hardening attempts (LibreLane)

Canonical running log with cell histograms and the full diagnosis: [`FLOW_STATUS.md`](FLOW_STATUS.md).
Only metrics that came out of a CI log are listed. A build is "passing" only when `gds` **and**
`precheck` **and** `gl_test` are green on the same commit.

| Attempt | Build | Result | Area / notes |
|---|---|---|---|
| 1 (`5ff6c6c`) | FF 128-word, defaults | **FAILED** `[DPL-0036]` detailed placement | 1 147 604 µm², 95.0 → 98.4 %; 35 510 buffer cells (305 304 µm², 27 %) |
| 2 (`33012d5`) | FF, `MAX_FANOUT_CONSTRAINT = 40` | **FAILED, worse** `[GPL-0301]` | 1 190 170 µm², 100.96 % |
| 2b | FF, 25 MHz probe | **FAILED, worse still** | 1 223 970 µm², 101.3 % |
| 3 (`35c3c42`) | **SRAM 1024×32 + 32-word DMEM** | area solved; failed a GPL routability-mode check `[GRT-0010]` (root cause later: stray IO pads) | 622 401 µm² = −46 %, 44.7 % movable-relative |
| 4 (`5732b23`) | `PL_ROUTABILITY_DRIVEN: false` | reached global routing; blocked by an unplaced instance; 16.67 ns failed setup (**WNS −4.125 ns, TNS −1658.6**, path ≈ 20.8 ns) | 622 401 µm² |
| 5 (`98cd5d6`) | **`CLOCK_PERIOD 30 ns` = 33.33 MHz** | **setup closed** (`RSZ-0098`), 3 177 hold endpoints repaired; still `[GRT-0010]` on the same unplaced cell | 622 401 µm² |
| 6 (`1182041`) | IO pads excluded (`EXTRA_EXCLUDED_CELLS`) | **CANCELLED at 6 h cap**: all stages through GRT in 15 min, DRT 5 h 45 m, **6 violations left** (37 824 → … → 6) | ~30 600 inst, 48.1 % eff. |
| 7 (`6050726`) | GRT forced to clear congestion (`ALLOW_CONGESTION:false`, `ADJUSTMENT:0.18`) | **CANCELLED at 6 h, WORSE**: 8 686 → … → **5 198 left** — measured negative result; no artefacts | ~30 600 inst |
| 8 (`d7bec7a`) | GRT knobs reverted; **`PL_ROUTABILITY_DRIVEN: true`**, **`PL_TARGET_DENSITY_PCT: 50`** | **PASSED — GDS + precheck 10/10** in ~2.5 h | 658 414 µm² = 54.5 %, 35 971 inst |

### 3.1 Why the FF build cannot be tape-out, and why constraint knobs failed

Attempt 1's histogram: 8 042 flip-flops (394 k µm²), **35 510 buffer cells = 305 304 µm² (27.0 % of
the mapped area)** — `buf_1` 29 195 = 211 958 µm², `buf_2/4/8/16` ≈ 93 k µm² — plus 5 658 `mux2_1` +
3 063 `mux4_1` ≈ 220 k µm². Attempts 2 and 2b tested whether the buffers were a constraint artefact:
relaxing fanout, then dropping the clock to 25 MHz, both made area **worse**. **The buffers come from
ABC's SCL sequential-mapping pass; constraint knobs are a dead end and structure is the only lever.**

### 3.2 Attempt 3 — the structural fix

One `RM_IHPSG13_1P_1024x32` macro + 32-word DMEM: fixed instance area 148 563 µm² + movable standard
cells 473 838 µm² = **622 401 µm²** (51.5 % raw area ratio, 44.7 % movable-relative flow utilisation), a
**−46 %** area reduction versus attempt 1, with GPL overflow converging 0.999 → 0.299.

### 3.3 Attempts 4–5 — two problems, and the routing blocker was not timing

Attempt 4 reached global routing but was blocked by an unplaced instance while 16.67 ns also failed
setup (1 028 violated endpoints, WNS −4.125 ns). Attempt 5 separated the two by running the same design
at 33.33 MHz: post-CTS repair reported **`RSZ-0098 No setup violations found`** and 3 177 hold endpoints
were repaired — and it still aborted on the same cell. **So timing was never the routing blocker.**

### 3.4 Root cause: stray IO-pad cells (library hygiene)

`_40267_` was not logic: the netlist contained **four `sg13cmos5l_IOPadOut16mA` IO-pad cells**,
≈ 14 400 µm² each (**57 600 µm² total, 12 % of moved cell area**), their `pad` pin driving *internal*
logic. ABC had mapped four high-fanout internal nets onto the 16 mA pad driver because the IO library
was in the synthesis library set. Tiny Tapeout designs own no pads, so IO placement never placed them
and routing aborted. Fixed by `"EXTRA_EXCLUDED_CELLS": ["sg13cmos5l_IOPad*"]` (attempt 6).

### 3.5 Attempts 6–8 — the 6-hour cap is a DRT budget, and the fix was placement

Attempt 6 reached detailed routing in **15 minutes** and then spent 5 h 45 m in DRT, capped with **6
violations left**. Attempt 7 tried the obvious-looking router fix and **made DRT worse** (5 198 left at
the cap): the router's default congestion behaviour was better, and the real problem is the placement DRT
inherits, not the guide quality. Attempt 8 therefore attacked placement — GRT knobs reverted,
**routability-driven placement on** (finally runnable now the stray pads are excluded) and target density
**60 → 50 %** — and **converged**: DRT `28 461 → 15 893 → 14 361 → 890 → 65 → 8 → 8 → 0`, then post-DRT
antenna repairs `4 031 → 806 → 536 → 38 → 1 → 0`. Total flow time ~2.5 h.

## 4. Final build — attempt 8, commit `d7bec7a`

**GDS build and precheck PASSED.** Design: SRAM 1024×32 instruction memory, 32-word DMEM, 33.33 MHz,
IO pads excluded. Numbers from `stats/metrics.csv` of the hardened run:

| Metric | Value |
|---|---|
| **Area** | stdcells **518 231 µm²** + SRAM macro **140 183 µm²** = **658 414 µm² = 54.5 %** of the 1 208 170 µm² core; stdcell utilisation 48.5 %; die 1 225 260 µm² (1724.16 × 710.64 µm); **35 971 stdcell instances** (+fill) |
| **DRC / antenna** | **0 antenna violations**, 0 routed DRC violations; global-route wirelength 3.32 mm, routed-estimate 2.07 mm |
| **Timing (all three corners)** | setup **WNS 0.0 / TNS 0.0**, hold **WNS 0.0 / TNS 0.0**; worst setup slack **+1.6006 ns** at the slow corner (1.08 V, 125 °C) against a 30 ns period (critical path ≈ 28.4 ns); worst hold **+0.1200 ns** at the fast corner |
| **Power** | **8.60 mW** total — internal 7.05 + switching 1.51 + leakage 0.038 mW |
| **Clock skew** | worst setup +0.440 ns, worst hold −0.453 ns |
| **Lint** | 0 errors, 52 warnings, 0 inferred latches |
| **Precheck** | **10/10 checks passed** — KLayout pin-label overlap, KLayout SG13CMOS5L DRC, zero-area, KLayout checks, pin check, boundary check, layer check, cell-name check, analog-pin check, Verilog syntax |
| **Gate level** | ✅ **`TESTS=64 PASS=60 FAIL=0 SKIP=4`** on `7d2fb2f` (run `44068106013`, 2 740 s), no errors. First run was `57/60`; all three failures were **one** cause: `rtl/loom_mem.v` zeroes DMEM in a **simulation-only `initial` block that is not synthesised**, so the GL netlist started with undefined DMEM, tests read unwritten DMEM words as X through the host bridge, X reached MISO and the cycle monitor's `int()` threw (initialising the PMEM SRAM model to `halt` was necessary but not sufficient). Fixed in `7d2fb2f`: `start_loom()` writes zeros to all 64 DMEM words over the host port after reset — RTL and GL now start from identical defined state, mirroring the firmware contract. The four skips are RTL-backdoor-dependent tests; GL is slower than RTL (2 740 s) because of those 64 DMEM writes per test |

Honest footnote: the metrics also list 292 max-fanout / 80 max-slew / 27 max-cap flags — these are the
TT/PDK *default limit checks*, not timing violations, and the flow does not gate on them (they are
reported here rather than hidden).

**Explore the result yourself:** 3D GDS viewer <https://gds-viewer.tinytapeout.com/?pdk=ihp-sg13cmos5l&model=https%3A%2F%2Frajesh963.github.io%2Floom-protocol-emulator%2Ftinytapeout.oas> · interactive RTL module explorer
<https://rajesh963.github.io/loom-protocol-emulator/modules/> · landing page <https://rajesh963.github.io/loom-protocol-emulator/>. The explorer is self-contained in `viewer/` (no build step),
shows nested per-thread internals with `specRef` links into the docs, and is SiliconPulse-compatible.

## 5. Sign-off

| Check | Result | Artefact |
|---|---|---|
| `gds` GitHub Action | **PASSED** — attempt 8 (`d7bec7a`), ~2.5 h, DRT 0 violations at both routing stages | Actions run |
| `precheck` | **PASSED 10/10** | Actions run |
| `gl_test` (gate-level suite) | ✅ **PASSED — `TESTS=64 PASS=60 FAIL=0 SKIP=4`**, run `44068106013` on `7d2fb2f`, 2 740 s, no errors (four skips are RTL-backdoor-dependent) | Actions run `44068106013` |
| formal (4 bounded proof groups) | **4/4 PASS** (`fifo` k=8, `core` k=6, `host` k=6, **`sched` k=16 — scheduler exactness**, v2 post-freeze) | `formal/` log |
| `docs` action | **PASS** | Actions run |
| `test` action | **PASS** — reproduces 64/64 on every push | Actions run |

Sign-off footnote: the flow's **PSM connectivity checker flags the SRAM array power rails** (`[PSM-0069]`
lines in the GDS log). The SRAM flow config sets `ERROR_ON_PDN_VIOLATIONS: 0` (as in the Tiny Tapeout SRAM
example), so these are non-fatal — this is **not** a claim of clean PDN sign-off. The independent Tiny
Tapeout **precheck passes all 10 checks**, including KLayout DRC/layer/pin, and the `gds` job is green by
design.

No silicon measurements exist; the frequency, power and areas above are flow results for the hardened
netlist at 33.33 MHz, VDD 1.2 V nominal.

## 6. Verification results (landed RTL, simulation)

**Both configurations are fully green, including the one that tape-out uses.** On the exact RTL
configuration being hardened (`make SRAM_SIM=1024`): **64 tests, 64 PASS, 0 FAIL, 0 SKIP**, ≈271 s per configuration, including 100/100 differential co-sim seeds, 2 USB-LS TX tests, the USB-LS RX
pin-path test and the Ethernet frame TX test. Frozen at `7d2fb2f`; a clean clone reproduces 64/64.

| Suite | Result | Notes |
|---|---|---|
| host link | **PASS** | 13 commands, global-CSR read matrix, FIFOs, IRQ/ACK, CS-abort, STATUS |
| ISA | **22/22 PASS** | every mnemonic; coverage meta-test over all 64 tests |
| timing | **7/7 PASS** | drift-free fractional stream, UART 115200 at the shipped clock (**RTL 289.35 cyc/bit = 115.20 kBd, +0.001 %**), I²C full-period 92.5/344.2 kHz, bit-banged SPI 1.04–1.06 MHz, `waitu` jitter-free, `inat` exact-cycle, `emite` latency, four concurrent periods, queue back-pressure (`max_eq = 4`) |
| differential co-sim | **PASS** | 100/100 random programs cycle-exact on the full `(pins_out, DIR)` trace **and** architectural state; 12/12 pull/reactive seeds; 6/6 hand-written; 0 artefacts |
| protocol VIPs | **9/9 PASS** | UART TX/RX/back-to-back decoder, SPI modes 0–3, SPI target, I²C 100 kHz/400 kHz/target, clock stretching |
| USB-LS TX / RX pin path | **2/2 + 1/1 PASS** | TX packet decoded independently; RX proves the differential pad path + reference decoder (0 mismatches, ±0.25 % drift) — not enumeration, no on-chip RX firmware |
| Ethernet frame TX | **1/1 PASS** | two host-preloaded frames, FCS `0xF57743B9`/`0x696CF29E`, cells 8.000 cycles, 2.0833 Mbit/s, 0.00-cycle edge residual |
| formal | **4/4 PASS** | bounded proofs via `yosys write_smt2` + `yosys-smtbmc -s z3`: FIFO/queue k=8, core k=6, host k=6, **scheduler exactness k=16** |
| gate-level | ✅ **`TESTS=64 PASS=60 FAIL=0 SKIP=4`** (2 739.85 s, no errors) on `7d2fb2f`, module breakdown host 19 / isa 22 / timing 7+1 skip / cosim 3+3 skips / protocols 9 / usb_ls 2 / eth 1 / usb_ls_rx 1. Skips = the four RTL-backdoor tests. | First run was `57/60`; one root cause: `rtl/loom_mem.v` zeroes DMEM in a **simulation-only `initial`** that is not synthesised, so GL started with undefined DMEM, tests read unwritten words as X through the host bridge, X reached MISO and the cycle monitor's `int()` threw (the PMEM `halt` init was necessary but not sufficient). Fixed (`7d2fb2f`) by `start_loom()` writing zeros to all 64 DMEM words over the host port — the firmware contract. Evidence: `results.xml` artifact + job log; **no GL FST is produced** (`WAVES=1` not enabled in CI). |
| coverage | **PASS (meta-test gate)** | every opcode/CSR exercised; numeric cross-coverage report is reporting-only |

## 7. Errata and known gaps

| # | Gap | Impact | Owner |
|---|---|---|---|
| 1 | SPI target mode needs the spare `uio[5..7]` re-mux (default SPI pins are output-only) | documented in [`examples.md`](examples.md) §2.2; no hardware change | docs / firmware |
| 2 | Full-duplex SPI above ~2 MHz controller-side needs a queue-synchronised read | documented limitation, see [`writeup.md`](writeup.md) §9 | design |
| 3 | capture / record-replay: RTL complete and unit-verified (209 checks), **silicon integration deferred to revision B** because enabling it aliases the 32-word DMEM used by the wave player | not in `source_files`; not in the submitted build | Lead |
| 4 | **True 10BASE-T not achievable**: 20 Mbaud = 1.6667 cycles/cell needs ≥4 instr/cell = 80 M instr/s vs 33.33 M instr/s IPC (2.4× over); measured same-code ceiling 3.03 Mbit/s; ±15–23 ns edge quantisation vs a few-ns TX jitter budget; 1024 half-cells/frame exceed the 32-word DMEM | documented, not a defect — revision-B item. Ethernet *frame TX* at 2.0833 Mbit/s with firmware CRC-32 is RTL-verified instead (§6) | Lead |
| 5 | **None — all four CI jobs are green.** GDS + precheck 10/10, gate-level `TESTS=64 PASS=60 FAIL=0 SKIP=4` on `7d2fb2f` (run `44068106013`); the viewer-only descendants `2805c5e` (run `44123912681`) and `27bd040` (run `44126150927`) are also all-green (GL `PASS=60 SKIP=4`), and later commits are docs-only | design/verification frozen at `7d2fb2f`. **No silicon measurements exist** | Lead |
| 6 | The 6-hour CI cap remains a hard limit: attempts 6/7 hit it (design stages are only ~15–20 min; the cap is a DRT budget). Attempt 8 converged in ~2.5 h, so the submitted build is inside it | FAQ fallback (local LibreLane + custom GDS action) not needed | Lead |
| 7 | **Wire-speed USB LS ACK is not achievable in firmware** *(v2 post-freeze)*: measured EOP→first ACK cell = **4 445 cycles = 199.8 bit times** against USB LS's 7.5; decode-during-reception needs ≈34 instructions/bit vs 22.25 available (≈1.5× over single-issue IPC) | architectural ceiling, not a bug (§10.2) — RX decode + ACK is functional-only and golden-model verified; enumeration not implemented (32-word DMEM also bounds descriptors). Revision-B option: hardware LS RX FSM | Lead |

## 8. How LOOM compares — summary *(v2 docs addition — post-freeze; design unchanged)*

Full discussion and links: [`writeup.md`](writeup.md) §10. All compared entries are open-source,
pre-silicon public projects; descriptions are summary-level from their public READMEs at the time of
writing (Nov 2026). **No row is a silicon measurement.**

| Entry | Tiles | Architecture family | Evidence level (as published) | Where it leads LOOM today |
|---|---|---|---|---|
| escapement | 8×4 | 4 contexts, absolute-tick scheduling | **formal no-late-edge guarantee on the netlist** + RTL | firmware breadth (CAN/JTAG/SWD/USB/10BASE-T), capture/bus monitor, formal exactness theorem |
| Polyglot | 6×4 | PIO-like cores + programmable line coder | RTL | USB LS RX, CAN 2.0A, true 10BASE-T framing |
| wirelingo | 8×4 | 3 cores @ 48 MHz, 3 × 256-word SRAM | **formal equivalence** + model/RTL/formal/gate/FPGA labelling | evidence discipline; clock rate |
| sophos | 6×4 | one SM, 32×16 IMEM, 32-slot capture | flow metrics (11.6 k stdcells, 22.2 %, 40 MHz) | area efficiency, 40 MHz, JTAG |
| Chameleo | — | Hardcaml-generated RTL + OCaml golden model | model-based generation | host-language golden model |
| protocol-emulator (WilliamZhang20) | — | bytecode engine, 2 action lanes | RTL | action-lane parallelism, JTAG |
| **LOOM** | 8×4 | 4 symmetric threads + hardware event scheduler (absolute/relative/edge events, 1/16-cycle fractional periods) | **RTL 64/64 both builds · 100/100 co-sim of pins + state · 4 formal proofs (incl. scheduler exactness k=16) · GL 60/4 · GDS+precheck 10/10** + 18 firmware programs / 13 families (6 v2 programs model-verified) | *deliberately* one uniform machine: no per-protocol datapaths |

LOOM's differentiators are architectural (single scheduler, four symmetric threads, deterministic
cross-thread pin merge) and methodological (spec-derived golden model + state-level differential
co-simulation, interactive module explorer, failed-attempt log in the write-up). Where others lead —
USB RX/enumeration firmware, CAN, JTAG/SWD, true 10BASE-T — the v2 plan closes that gap in firmware
with zero new cells (§9).

## 9. Area headroom and v2 roadmap *(v2 docs addition — post-freeze; design unchanged)*

**Where the area went (measured, attempt 8):** stdcells **518 231 µm²**, SRAM macro **140 183 µm²**,
total **658 414 µm² = 54.5 %** of the 1 208 170 µm² core; 35 971 stdcell instances; fill occupies the
rest of the core. Tiny Tapeout guidance is roughly 1 k cells per tile (≈32 k cells for 8×4), so the
design sits slightly **above** the guidance yet **area was not the binding constraint** — detailed
routing time was. DRT took **2.5 h** and only converged with routability-driven placement and 50 %
target density; the hardened die dissipates **8.60 mW**.

**Therefore the v2 plan is firmware-first:**

| Item | Cells | Re-harden? | Note |
|---|---|---|---|
| USB RX / enumeration firmware | **0** | no | algorithm + oracle already in repo (`host/usb_ls_rx.py`); code-size-bound (~150–200 instr), fits SRAM-1024 |
| JTAG, SWD, CAN, DMX512, MIDI, IR firmware | **0** | no | same datapath, new programs; each demonstrated the same way (VIP + golden model) |
| 2-FF input synchronisers (24 pins) | ~1 k µm² | yes (v2) | closes the unsynchronised-protocol-pin limitation (architecture §10); does not change protocol timing (mid-bit sampling, ≥8 cycles margin) |
| reset synchroniser | ~100 µm² | yes (v2) | async de-assert recovery/removal hardening |
| capture / record-replay | 0 (RTL exists, verified) | yes (v2) | currently gated off because it aliases the DMEM port used by the wave player |
| scheduler-exactness formal proof | 0 | no | the property this architecture should prove next (no late edge across thread interleavings) |

The design is frozen and shipped; no second GDS is promised. A v2 harden happens only if the firmware
work shows it is worth another tape-out, and it would ship on its own green pipeline.

## 10. Measured architectural ceilings *(v2 docs addition — post-freeze; design unchanged)*

The strongest "we measured our limits" artefacts in the project are the two cases where the firmware
work ran into the microarchitecture. Both are reproducible measurements, not tuning failures, and both
are revision-B hardware items rather than open bugs.

### 10.1 True 10BASE-T — 2.4× over single-issue IPC

| Quantity | 10BASE-T needs | This build provides | Verdict |
|---|---|---|---|
| line rate | 20 Mbaud = **1.6667 cycles/cell** at 33.33 MHz | data-bearing Manchester cell ≥4 instructions → **80 M instr/s** vs **33.33 M instr/s** IPC | **2.4× over** |
| threads | — | 4 threads share one issue slot: concurrency, not IPC | does not help |
| measured ceiling, same code | — | decodes at **5.5 cycles/cell = 3.03 Mbit/s**; fails at 5.0 cycles/cell (3.33 Mbit/s) | ~3 Mbit/s is the wall |
| TX jitter | a few ns at ±100 ppm | cycle-quantised edges: **±15–23 ns on a 50 ns cell** | unattainable |
| pattern buffer | 1024 half-cells/frame | wave player ≤64 patterns in a **32-word DMEM** | does not fit |

Achieved instead: **Ethernet frame TX at 2.0833 Mbit/s** with Manchester line coding and CRC-32 FCS in
firmware, RTL-verified (`examples.md` §11, `test/test_eth.py`).

### 10.2 USB low-speed wire-speed ACK — 1.5× over IPC

| Quantity | Measured | USB LS requirement |
|---|---|---|
| sampler cost | **22.25 cycles/bit = 22.25 of 22.25 available (72 % of IPC, zero issue slack)** | — |
| EOP → end of fixed 104-sample window | 198 cycles (8.9 bit times) | — |
| NRZI + unstuff + assembly | 2 505 cycles (112.6 bit times) | — |
| PID + CRC-16 (8 bytes) | 564 cycles (25.3 bit times) | — |
| EOP double-SE0 scan | 1 155 cycles (51.9 bit times) | — |
| publish + ACK setup/emit | 23 cycles (1.0 bit times) | — |
| **EOP → first ACK cell** | **4 445 cycles = 199.8 bit times** | **7.5 bit times (167 cycles)** |
| decode-during-reception cost | ≈34 instructions/bit | 22.25 available → **≈1.5× over IPC** |
| idealised 8-instruction sampler | ≈26 instructions/bit | still over |

Achieved instead: **USB-LS RX decode + ACK, golden-model verified** (357 words → SRAM-1024 only; DMEM
32/32; SETUP CRC-5 `0x02`, DATA0 `GET_DESCRIPTOR` byte-exact, CRC-16 `0x94DD`, ±0.25 % host drift; ACK
is symbol-correct — SYNC + PID `0xD2` + EOP, 18 cells — but functional-only). RX is single-thread and
host-quiet while sampling; the 104-sample window is 2 314 cycles, not a rate. Enumeration is not
implemented, and a config descriptor does not fit in 32 DMEM words (a device descriptor would, by
reusing the dead sample region at response time); a device with no config descriptor must STALL. Full
breakdown: [`examples.md`](examples.md) §8.1.

The pair is deliberate: **the same `emit`/`waitd` machinery that makes UART/I²C/SPI bit-exact is what
tells us, quantitatively, where a 33.33 MHz single-issue datapath stops** — and both ceilings were
found by writing the firmware, not by guessing.


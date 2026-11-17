# LOOM results

**What this is.** The measured facts: the two build variants, the area estimate and its correction, the
full hardening-attempt history, the **final GDS build with precheck and timing closure**, the
verification matrix, and the honest remaining gaps. Every number is either measured (with the artefact
that produced it) or explicitly `TBD`. Nothing here is an estimate dressed up as a result, and nothing
is a silicon measurement — no LOOM silicon exists yet.

**Submission build:** SRAM `RM_IHPSG13_1P_1024x32` instruction memory + 32-word DMEM, **33.33 MHz**
(`CLOCK_PERIOD` 30 ns), commit **`d7bec7a`** (attempt 8).
**GDS BUILD PASSED · PRECHECK 10/10 PASSED · DRC clean · timing closed at all three corners · 8.60 mW.**
Gate-level: the first run was `57/60`, and the three failures were **one** root cause — DMEM is zeroed by
a **simulation-only `initial` block** in `rtl/loom_mem.v` that is not synthesised, so the gate-level
netlist starts with undefined DMEM; tests reading DMEM words they never wrote got X through the host
bridge, X reached MISO, and the cycle monitor's `int()` threw. Fixed in `7d2fb2f` (`start_loom()` writes
zeros to all 64 DMEM words through the host port after reset, so RTL and GL start from the same defined
state — mirroring the firmware contract). **Expected final GL: `TESTS=64 PASS=60 FAIL=0 SKIP=4`** (the
four skips are RTL-backdoor-dependent tests); the count is published only when the run reports. RTL verification is frozen at `4164c42`: **64/64 PASS on both memory
builds, 0 skips, 64/64 from a clean clone.**

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
| **Gate level** | first run `TESTS=64 PASS=57 FAIL=3 SKIP=4`; all three failures were **one** cause: `rtl/loom_mem.v` zeroes DMEM in a **simulation-only `initial` block that is not synthesised**, so the GL netlist starts with undefined DMEM, the tests read unwritten DMEM words as X through the host bridge, X reached MISO and the cycle monitor's `int()` threw (initialising the PMEM SRAM model to `halt` was necessary but not sufficient). Fixed in `7d2fb2f`: `start_loom()` writes zeros to all 64 DMEM words over the host port after reset — RTL and GL now start from identical defined state, mirroring the firmware contract that DMEM has no reset. **Final run in progress; expected `TESTS=64 PASS=60 FAIL=0 SKIP=4`** (skips are RTL-backdoor-dependent tests) |

Honest footnote: the metrics also list 292 max-fanout / 80 max-slew / 27 max-cap flags — these are the
TT/PDK *default limit checks*, not timing violations, and the flow does not gate on them (they are
reported here rather than hidden).

## 5. Sign-off

| Check | Result | Artefact |
|---|---|---|
| `gds` GitHub Action | **PASSED** — attempt 8 (`d7bec7a`), ~2.5 h, DRT 0 violations at both routing stages | Actions run |
| `precheck` | **PASSED 10/10** | Actions run |
| `gl_test` (gate-level suite) | **57/60 first run → one root cause found (unsynthesised DMEM `initial`) → fixed in `7d2fb2f`; final run in progress** (expected `TESTS=64 PASS=60 FAIL=0 SKIP=4`) | `test/results.xml` |
| formal (3 bounded proof groups) | **3/3 PASS** (`fifo_queue_bounds` k=8, `core_reset_event` k=6, `host_cmd_order` k=6) | `formal/` log |
| `docs` action | **PASS** | Actions run |
| `test` action | **PASS** — reproduces 64/64 on every push | Actions run |

No silicon measurements exist; the frequency, power and areas above are flow results for the hardened
netlist at 33.33 MHz, VDD 1.2 V nominal.

## 6. Verification results (landed RTL, simulation)

**Both configurations are fully green, including the one that tape-out uses.** On the exact RTL
configuration being hardened (`make SRAM_SIM=1024`): **64 tests, 64 PASS, 0 FAIL, 0 SKIP**, ≈271 s per configuration, including 100/100 differential co-sim seeds, 2 USB-LS TX tests, the USB-LS RX
pin-path test and the Ethernet frame TX test. Frozen at `4164c42`; a clean clone reproduces 64/64.

| Suite | Result | Notes |
|---|---|---|
| host link | **PASS** | 13 commands, global-CSR read matrix, FIFOs, IRQ/ACK, CS-abort, STATUS |
| ISA | **22/22 PASS** | every mnemonic; coverage meta-test over all 64 tests |
| timing | **7/7 PASS** | drift-free fractional stream, UART 115200 at the shipped clock (**RTL 289.35 cyc/bit = 115.20 kBd, +0.001 %**), I²C full-period 92.5/344.2 kHz, bit-banged SPI 1.04–1.06 MHz, `waitu` jitter-free, `inat` exact-cycle, `emite` latency, four concurrent periods, queue back-pressure (`max_eq = 4`) |
| differential co-sim | **PASS** | 100/100 random programs cycle-exact on the full `(pins_out, DIR)` trace **and** architectural state; 12/12 pull/reactive seeds; 6/6 hand-written; 0 artefacts |
| protocol VIPs | **9/9 PASS** | UART TX/RX/back-to-back decoder, SPI modes 0–3, SPI target, I²C 100 kHz/400 kHz/target, clock stretching |
| USB-LS TX / RX pin path | **2/2 + 1/1 PASS** | TX packet decoded independently; RX proves the differential pad path + reference decoder (0 mismatches, ±0.25 % drift) — not enumeration, no on-chip RX firmware |
| Ethernet frame TX | **1/1 PASS** | two host-preloaded frames, FCS `0xF57743B9`/`0x696CF29E`, cells 8.000 cycles, 2.0833 Mbit/s, 0.00-cycle edge residual |
| formal | **3/3 PASS** | bounded proofs via `yosys write_smt2` + `yosys-smtbmc -s z3` |
| gate-level | **57/60 first run → fixed (`7d2fb2f`) → final re-run in progress** | expected `TESTS=64 PASS=60 FAIL=0 SKIP=4`; root cause: `rtl/loom_mem.v` zeroes DMEM in a **simulation-only `initial` block that is not synthesised**, so the gate-level netlist starts with undefined DMEM; the tests that read DMEM words they never wrote got X back through the host bridge, X reached the MISO pin, and the cycle monitor's `int()` conversion threw. (Initialising the PMEM SRAM model to `halt` was necessary but not sufficient.) Fix (`7d2fb2f`): `start_loom()` writes zeros to all 64 DMEM words through the host port after reset, so RTL and GL start from the same defined state — which mirrors the firmware contract (DMEM has no reset; software initialises it). |
| coverage | **PASS (meta-test gate)** | every opcode/CSR exercised; numeric cross-coverage report is reporting-only |

## 7. Errata and known gaps

| # | Gap | Impact | Owner |
|---|---|---|---|
| 1 | SPI target mode needs the spare `uio[5..7]` re-mux (default SPI pins are output-only) | documented in [`examples.md`](examples.md) §2.2; no hardware change | docs / firmware |
| 2 | Full-duplex SPI above ~2 MHz controller-side needs a queue-synchronised read | documented limitation, see [`writeup.md`](writeup.md) §9 | design |
| 3 | capture / record-replay: RTL complete and unit-verified (209 checks), **silicon integration deferred to revision B** because enabling it aliases the 32-word DMEM used by the wave player | not in `source_files`; not in the submitted build | Lead |
| 4 | **True 10BASE-T not achievable**: 20 Mbaud = 1.6667 cycles/cell needs ≥4 instr/cell = 80 M instr/s vs 33.33 M instr/s IPC (2.4× over); measured same-code ceiling 3.03 Mbit/s; ±15–23 ns edge quantisation vs a few-ns TX jitter budget; 1024 half-cells/frame exceed the 32-word DMEM | documented, not a defect — revision-B item. Ethernet *frame TX* at 2.0833 Mbit/s with firmware CRC-32 is RTL-verified instead (§6) | Lead |
| 5 | **GDS + precheck passed; gate-level was 57/60, one root cause (DMEM zeroed by an unsynthesised `initial` block), fixed in `7d2fb2f`** | final `gl_test` count + SHA filled in when the run reports (expected `TESTS=64 PASS=60 FAIL=0 SKIP=4`); **no silicon measurements exist** | Lead |
| 6 | The 6-hour CI cap remains a hard limit: attempts 6/7 hit it (design stages are only ~15–20 min; the cap is a DRT budget). Attempt 8 converged in ~2.5 h, so the submitted build is inside it | FAQ fallback (local LibreLane + custom GDS action) not needed | Lead |

# LOOM project status — 2026-11-16

## Verified working (RTL simulation, Icarus Verilog 12)
- `rtl/loom_core.v` — 4-thread work-conserving scheduler, 32-bit ISA (7-bit opcode),
  per-thread periodic event queues (12.4 fixed point, drift-free), edge-triggered events,
  timed sampling (INAT), WAITD/WAITU/WAITP, RPT, FIFOs, CSR map, host-contention retry.
  - `test/unit_core/run.sh` → **CORE_TB: PASS** (two concurrent threads: exact 16-cycle and
    8-cycle edge streams through event-queue back-pressure).
- `rtl/loom_host.v` — SPI mode-0 slave + full command engine (all 13 commands), IRQ latch.
  - `test/unit_host/run.sh` (owner: hostbus) — see their report.
- `rtl/loom_wave.v`, `rtl/loom_capture.v` (owner: wavecapt) — see their report.
- `rtl/loom_mem.v` — PMEM 1024x32 (1-cycle sync read), DMEM 64x16, host load/readback.
- `rtl/tt_um_rajesh963_loom.v` — Tiny Tapeout top, 8x4 target, host link on ui[2:0]/uo[0..1].
- `test/integration/run.sh` → **ITEST: PASS**: bit-banged SPI loads 7 instructions, reads them
  back, sets PC, RUNs a thread; event scheduler emits a square wave on `uo_out[2]` with
  **zero** cycle-period errors (25 edges, all exactly 16 cycles apart).
- `sw/isa.py` (single source of truth, parses `loom_bus.vh`), `sw/loom_asm.py` (2-pass
  assembler), `sw/loom_model.py` (golden cycle model, being finalised against spec §3.1).

## Frozen contracts
- `spec/ARCHITECTURE.md` (incl. §3.1 normative cycle model), `spec/OWNERS.md`, `rtl/loom_bus.vh`.
- Instruction: `opcode[31:25] rd[24:22] rs[21:19] mode[18:16] imm16[15:0]`.
- Host memory map (10-bit): PMEM 0x000-0x3BF, DMEM 0x3C0-0x3FF.
- Reset/unmapped-PMEM word: `0x0200_0000` (HALT).

## Next steps (in order)
1. Golden model vs RTL differential co-simulation (random programs) — `verify` teammate.
2. cocotb protocol VIPs: UART, SPI (modes 0-3, master+slave), I2C (100/400k), PS/2, 1-Wire.
3. Firmware in `sw/examples/` for each protocol + host client demos.
4. yosys + IHP-liberty area report (FF build), then SRAM 1024x32 build decision (research pending).
5. Push to GitHub public repo → `gds` + `precheck` + `gl_test` CI runs → iterate to green.
6. docs results tables filled from CI artefacts; write-up polish; submission checklist.

## 2026-11-16 late update
- Host bridge (hostbus) **DONE**: HOST_TB/FIFO_TB PASS at 10 MHz SCK and 100 kHz SCK; max SCK = clk/6
  (10 MHz at 60 MHz); CS abort / long CS-low / inter-byte pauses tested. TFIFO layout harmonised
  with core: `{4'b0, rx_count[3:0], tx_count[3:0], 2'b0, tx_full, rx_empty}` (counts [11:8]/[7:4]).
- Wave player + capture (wavecapt) **DONE**: WAVE_TB PASS (96 checks; 1000 symbols @ 1.625 cycles =
  1625 cycles exact), CAPTURE_TB PASS (209 checks). Wave player wired in top. Capture remains
  `LOOM_ENABLE_CAPTURE`-gated (needs cap_mask/cap_value CSRs + TMR export before enabling).
- First yosys generic-cell count (FF-small build, 128-word PMEM): ~8.1k FFs, ~36k combinational
  cells before technology mapping. Rough area: FFs ~395k um^2, logic ~200-280k um^2 → FF-small is
  ~55-65% of the 1.0 mm^2 die (tight). SRAM build removes ~4k PMEM FFs and adds one
  140k um^2 macro → ~45-50% of die, comfortable, and gives 1024 instruction words.
  ACTION: flagship = SRAM 1024x32; FF fallback config only if LibreLane struggles.
- SRAM research (official route, `research/sram-recipe.md`): Tiny Tapeout now publishes
  github.com/TinyTapeout/ttihp-cmos5l-sram-example (tt-gds-action@ttihp27a, 256x16) with the
  pdn_cfg.tcl + ExtendPowerStripes plugin needed for macro power; macro port list and config
  delta documented. Demo board map: ui=GPIO17-24, uio=GPIO25-32, uo=GPIO33-40, clk=GPIO16,
  rst=GPIO14; host link can be driven by PIO/bit-bang (chip MISO is uo_out[0]=GPIO33).
- Firmware (`firmware`) and verification (`verify`) workstreams started.
- **Computed-jump RTL bug found by the golden model and fixed**: `csrw TPC` now redirects the
  same-cycle fetch to the new PC (no bubble, correct instruction). New regression test
  `test/unit_core/tb_jump.v` (program: movi r0,6; csrw TPC,r0; [skipped fall-through]; target
  block drives pins 0x0A00) → **JUMP_TB: PASS**. Spec §3.1 S4 updated. This is exactly the kind of
  RTL-vs-model divergence the co-simulation layer is meant to catch — worth citing in the write-up.
- **Docs workstream DONE** (19 files): README, info.yaml (validated: 8x4, 60 MHz, 6 source files,
  all 24 pins labelled), docs/{info,architecture,isa,host-protocol,examples,verification,results,
  writeup,SUBMISSION_CHECKLIST}.md, 4 SVG diagrams (48 KB total), 9 example asm sketches.
  Doc set is ~158 KB of markdown (fine for judges; text is cheap).
- **CI prep**: `.github/workflows/gds.yaml` now runs `tools/sync_src.sh` before `tt-gds-action@ttihp27a`;
  `src/` contains exactly the 6 files listed in info.yaml (template `project.v` removed);
  `.gitignore` covers venv/cache/sim artefacts. `src/config.json` CLOCK_PERIOD = 16.67 ns (60 MHz).
- Remaining before CI: create public GitHub repo (awaiting Rajesh's go-ahead), then iterate
  gds → precheck → gl_test to green, then fill results tables in docs/results.md + verification.md.
- **VER-003 (critical) found by the cocotb host suite and FIXED**: host reads of global CSRs
  (POUT/PDIR/TMR/EVT/IRQ/W*/CAP*/GSTATUS) were decoded from the *thread* CSRR address register,
  so the host saw the last thread-CSRR target instead of the global register. Fixed by splitting
  into two read ports (thread bank/global via `csr_rd_addr`, host via `host_csrr_addr`, functions
  `bank_read(t,a)`/`global_read(a)`). Regression added to `test/integration` (host POUT=0000,
  TMR=0x04A0 after reset) — ITEST PASS. Also VER-001 (pout host-bit mask) and VER-002 (TFIFO
  layout) closed the same day; three real deviations found by verification in the first bring-up.
- **Area calibration** (real ttihp designs, post-place LibreLane metrics in `research/metrics/`):
  vc32_cpu 6x2 tiles → 392k um^2 core, 285.7k stdcell, 18.6k cells, util 0.73; kianv 4x2 → 160.5k
  stdcell, util 0.62; tinyQV 2x2 → 114.5k stdcell, util 0.89. Average ~15 um^2 per placed cell.
  Our mapped estimate: 8038 FFs (~394k um^2) + ~40k gate instances for the FF-small build; the
  SRAM flagship replaces 4096 PMEM FFs and the 128:1 PMEM read mux (~70k um^2) with one 140k um^2
  macro → expect ~55-65% utilisation of an 8x4 core (~1.0 mm^2), i.e. it should fit but with no
  room for extras. Cheap shrink levers if CI says otherwise: DMEM 64→32 words, FIFO depth 8→4,
  drop the wave player's private DMEM read port (share with CPU).
- **GitHub repo created (PRIVATE as requested): https://github.com/rajesh963/loom-protocol-emulator**
  pushed `master`; workflows `gds`, `docs`, `test` triggered on push (2026-11-16 15:25 UTC).
  `src/config.json` sets `VERILOG_DEFINES=["LOOM_PMEM_SMALL"]` for the first hardening run
  (FF 128-word build, no SRAM macro) — the flow/area risk is isolated from the macro integration.
  Plan: get FF-small GDS+precheck green first, then switch to the SRAM `1024x32` flagship and
  re-run; flip the repo public before submission (user decision).
- **RTL bug-fix batch (all landed + committed `beeb878`/`d468351`)**: VER-004 DMEM address,
  VER-008 past-target waits, VER-009/010/012 CSR-read staleness (root cause: `always @*` does not
  track regs read inside a function in Icarus — replaced with explicit case statements), VER-013
  INAT rd truncation, VER-014 emita/emitd polluting NEXTT. Soft reset keeps TMR running (spec).
  Local suites after the batch: unit_core PASS, unit_host PASS, unit_wave PASS, integration PASS,
  SW_TESTS PASS; cocotb 28 PASS / 1 FAIL (TEQ sampling artefact, verify to fix) / 30 SKIP
  (known-issue flags still gated — flipping them is verify's next step).
- **Verification suite: 57 PASS / 0 FAIL / 2 SKIP locally** (the 2 skips are I2C 400 kHz sampling
  and I2C-target data NAK, still gated for triage). Includes the 100-seed differential co-sim
  (RTL vs golden model, full trace + architectural state) — the headline verification claim — plus
  22/22 ISA opcode tests, timing 7/7, UART/SPI/I2C protocol VIPs, and 3/3 formal proofs.
- Final RTL batch: hostbridge STATUS capture now samples 1 clk after issue (combinational host read
  port); TPC writes invalidate the prefetched instruction buffer.
- CI: docs workflow PASS; test workflow expected green on next push.

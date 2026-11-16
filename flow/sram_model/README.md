# IHP SRAM macro behavioural models (gate-level simulation only)

Unmodified copies from IHP-Open-PDK, commit `89d42ee6d2fd9b50b86a1aada6fee6673c3e6008`
(`ihp-sg13g2/libs.ref/sg13g2_sram/verilog/`, which the cmos5l PDK links to as
`sg13cmos5l_sram`).  Apache-2.0, (c) IHP PDK Authors.

* `RM_IHPSG13_1P_1024x32_c2_bm_bist.v` — macro wrapper (used by the 1024-word build)
* `RM_IHPSG13_1P_512x32_c2_bm_bist.v`  — macro wrapper (used by the 512-word build)
* `RM_IHPSG13_1P_core_behavioral_bm_bist.v` — shared bit-cell model

They are needed only for **gate-level** simulation (`make GATES=yes`), where the
hardened netlist keeps the macro as a black box: `test/Makefile` adds the right
wrapper + the core model and defines `-DSIM -DFUNCTIONAL` in that branch, and
`src/sram_bb.v` is skipped because it is guarded by `` `ifndef SIM ``.

RTL simulation does not use these files: it defines `LOOM_SIM` and uses
`rtl/loom_sram_model.v` instead, so no PDK is required for `make`/CI test runs.

#!/usr/bin/env bash
# Mirror the canonical RTL (rtl/) into src/, which is where Tiny Tapeout's
# LibreLane flow looks for sources (info.yaml source_files are src/ basenames).
# Run this before any GDS build; CI runs it automatically.
#
# Two SRAM-flow files are NOT generated from rtl/ and live in src/ directly
# (committed, exactly like the official ttihp-cmos5l-sram-example):
#   src/sram_bb.v    (* blackbox *) declaration of the selected IHP SRAM macro
#   src/pdn_cfg.tcl  PDN config referenced by "PDN_CFG": "dir::pdn_cfg.tcl"
# The LibreLane plugin files (librelane_plugin_sram_pdn.py, odb_stripes.py)
# are committed at the repository root, where LibreLane auto-imports them.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -f src/loom_*.v src/loom_bus.vh src/tt_um_rajesh963_loom.v
cp rtl/loom_bus.vh rtl/loom_core.v rtl/loom_mem.v rtl/loom_host.v \
   rtl/loom_wave.v rtl/loom_pmem_sram.v rtl/loom_sram_model.v \
   rtl/tt_um_rajesh963_loom.v src/
echo "synced: $(ls src/*.v src/*.vh src/*.tcl | tr '\n' ' ')"

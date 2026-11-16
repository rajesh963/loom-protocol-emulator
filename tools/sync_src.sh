#!/usr/bin/env bash
# Mirror the canonical RTL (rtl/) into src/, which is where Tiny Tapeout's
# LibreLane flow looks for sources (info.yaml source_files are src/ basenames).
# Run this before any GDS build; CI runs it automatically.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -f src/loom_*.v src/loom_bus.vh src/tt_um_rajesh963_loom.v
cp rtl/loom_bus.vh rtl/loom_core.v rtl/loom_mem.v rtl/loom_host.v \
   rtl/loom_wave.v rtl/tt_um_rajesh963_loom.v src/
echo "synced: $(ls src/*.v src/*.vh | tr '\n' ' ')"

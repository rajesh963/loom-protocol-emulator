#!/usr/bin/env bash
# =============================================================================
# tools/select_build.sh — choose the LOOM program-memory build flavour
#
# Usage:
#   tools/select_build.sh sram      # IHP RM_IHPSG13_1P_1024x32 (140 183 um^2)
#   tools/select_build.sh sram512   # IHP RM_IHPSG13_1P_512x32  ( 79 720 um^2)
#   tools/select_build.sh ff        # 128-word FF-array fallback (area-safe)
#   tools/select_build.sh status    # show the currently selected build
#
# The SRAM variants merge flow/config_sram.json (or config_sram512.json) over
# the CURRENT src/config.json, so locally maintained keys such as CLOCK_PERIOD,
# PL_TARGET_DENSITY_PCT and the pdk:: max-fanout block are preserved; `ff`
# removes exactly the SRAM keys again.  `src/` is re-synced from rtl/ at the
# end (same script CI runs before the GDS action).
#
# After switching, the committed GDS build is whatever src/config.json says;
# the TinyTapeout gds workflow hardens it and runs precheck + gl_test.
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PYTHON="${PYTHON:-python3}"
CONFIG="src/config.json"

usage() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

current_build() {
  "$PYTHON" - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
defs = cfg.get("VERILOG_DEFINES", [])
macros = list(cfg.get("MACROS", {}).keys())
if "LOOM_PMEM_SRAM" not in defs:
    build = "ff (FF-array PMEM, %s)" % ("128-word -DLOOM_PMEM_SMALL"
                                        if "LOOM_PMEM_SMALL" in defs else "1024-word")
else:
    build = "sram (%s)" % (macros[0] if macros else "no macro declared!")
print(build)
print("VERILOG_DEFINES =", defs)
if macros:
    print("MACROS =", macros)
PY
}

case "${1:-}" in
  sram|sram512)
    "$PYTHON" flow/merge_config.py "$CONFIG" "$1" -o "$CONFIG"
    echo "selected build: $(current_build | head -1)"
    bash tools/sync_src.sh >/dev/null
    echo "src/ re-synced; src/config.json now carries the $1 flow keys."
    echo "next: rebuild GDS (CI gds workflow uses exactly src/config.json)."
    ;;
  ff)
    "$PYTHON" flow/merge_config.py "$CONFIG" ff -o "$CONFIG"
    echo "selected build: $(current_build | head -1)"
    bash tools/sync_src.sh >/dev/null
    echo "src/ re-synced."
    ;;
  status)
    current_build
    ;;
  *)
    usage
    exit 2
    ;;
esac

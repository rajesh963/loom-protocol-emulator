#!/usr/bin/env bash
# =============================================================================
# tools/area_synth.sh — LOOM standard-cell area estimation
#
# Synthesises the RTL with yosys against the IHP SG13CMOS5L typical-corner
# liberty file and prints the estimated area in um^2.
#
# Usage:
#   tools/area_synth.sh [-t TOP] [-l LIBERTY] [-P NAME=VALUE]... [--abc]
#                       [--rtl-dir DIR] [--keep-log] [-q]
#
#   -t, --top TOP        top module (default: first `tt_um_*` in rtl/*.v, else
#                        `loom_core`, else the first module found)
#   -l, --liberty PATH   liberty file (default ../research/sg13cmos5l_stdcell_typ_1p20V_25C.lib)
#   -P, --param K=V      override a top-level parameter (repeatable; uses
#                        `chparam`, e.g. -P PMEM_IMPL=ff -P NUM_THREADS=2)
#       --abc            use a real ABC-mapped synthesis (requires an
#                        ABC-enabled *native* yosys; see note below)
#       --rtl-dir DIR    RTL directory (default <repo>/rtl)
#       --keep-log       keep the yosys log next to the output
#   -q, --quiet          only the machine-readable results
#
# Output (stdout):
#   AREA_UM2: <float>          total cell area in um^2
#   AREA_MODE: abc|gate_estimate
#   plus a per-cell-type/breakdown table.
#
# ENVIRONMENT / important note on yosys:
#   This repo's Python venv ships YoWASP yosys (`../.venv/bin/yowasp-yosys`).
#   YoWASP builds yosys to WebAssembly and does NOT include the external ABC
#   binary, so `abc -liberty` (and `synth`'s internal ABC step) cannot run in
#   this environment.  The script therefore defaults to:
#       synth -top TOP -flatten -noabc ; stat -json
#   and prices the resulting generic-gate netlist with tools/area_estimate.py
#   using cell areas parsed from the liberty file (an estimate, typically within
#   ~+10..20% of an ABC-mapped result).
#   If you have an ABC-enabled native yosys, run with `--abc` (or set
#   YOSYS=/path/to/native-yosys and pass --abc) to get the real mapped area from
#   `abc -liberty ...; stat` ("Chip area for module ...").
#
# Examples:
#   tools/area_synth.sh                                  # auto top, estimate
#   tools/area_synth.sh -t loom_core                     # explicit top
#   tools/area_synth.sh -t loom_core -P NUM_THREADS=2
#   tools/area_synth.sh -t tt_um_rajesh963_loom -P PMEM_IMPL=ff --abc
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON="${PYTHON:-$REPO/../.venv/bin/python}"
YOSYS="${YOSYS:-$REPO/../.venv/bin/yowasp-yosys}"
LIBERTY="${LIBERTY:-$REPO/../research/sg13cmos5l_stdcell_typ_1p20V_25C.lib}"
RTL_DIR="$REPO/rtl"
TOP=""
ABC_MODE=0
KEEP_LOG=0
QUIET=0
PARAMS=()

usage() { sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--top)       TOP="${2:?--top needs a module}"; shift 2 ;;
    -l|--liberty)   LIBERTY="${2:?--liberty needs a path}"; shift 2 ;;
    -P|--param)     PARAMS+=("${2:?--param needs NAME=VALUE}"); shift 2 ;;
    --abc)          ABC_MODE=1; shift ;;
    --rtl-dir)      RTL_DIR="${2:?--rtl-dir needs a path}"; shift 2 ;;
    --keep-log)     KEEP_LOG=1; shift ;;
    -q|--quiet)     QUIET=1; shift ;;
    -h|--help)      usage 0 ;;
    *) echo "area_synth: unknown argument '$1' (try --help)" >&2; exit 2 ;;
  esac
done

[[ -x "$YOSYS" ]]  || { echo "area_synth: yosys not found/executable: $YOSYS" >&2; exit 1; }
[[ -x "$PYTHON" ]] || { echo "area_synth: python not found/executable: $PYTHON" >&2; exit 1; }
[[ -f "$LIBERTY" ]] || { echo "area_synth: liberty file not found: $LIBERTY" >&2; exit 1; }
[[ -d "$RTL_DIR" ]] || { echo "area_synth: RTL dir not found: $RTL_DIR" >&2; exit 1; }

shopt -s nullglob
RTL_FILES=("$RTL_DIR"/*.v)
if [[ ${#RTL_FILES[@]} -eq 0 ]]; then
  echo "area_synth: no rtl/*.v files in $RTL_DIR (nothing to synthesise yet)" >&2
  exit 1
fi

# ---- pick a top module if none was given -----------------------------------
find_modules() {
  grep -hoE '^[[:space:]]*module[[:space:]]+[A-Za-z_][A-Za-z0-9_]*' "$@" \
    | awk '{print $2}' | sort -u
}
MODULES=($(find_modules "${RTL_FILES[@]}"))
if [[ -z "$TOP" ]]; then
  TOP="$(printf '%s\n' "${MODULES[@]}" | grep -m1 '^tt_um_' || true)"
  if [[ -z "$TOP" ]] && printf '%s\n' "${MODULES[@]}" | grep -qx 'loom_core'; then
    TOP="loom_core"
  fi
  if [[ -z "$TOP" ]]; then
    TOP="${MODULES[0]}"
  fi
fi
if ! printf '%s\n' "${MODULES[@]}" | grep -qx "$TOP"; then
  echo "area_synth: top module '$TOP' not found in $RTL_DIR (modules: ${MODULES[*]})" >&2
  exit 1
fi

# ---- build the yosys command ----------------------------------------------
CHPARAMS=""
for kv in "${PARAMS[@]:-}"; do
  [[ -z "$kv" ]] && continue
  if [[ "$kv" != *=* ]]; then
    echo "area_synth: --param expects NAME=VALUE, got '$kv'" >&2; exit 2
  fi
  CHPARAMS+="chparam -set ${kv%%=*} ${kv#*=} $TOP; "
done

WORK="$REPO/.area_synth"
mkdir -p "$WORK"
LOG="$WORK/yosys_${TOP}.log"
STAT="$WORK/stat_${TOP}.json"

READ="read_verilog -I $RTL_DIR ${RTL_FILES[*]}; "

export YOWASP_CACHE_DIR="${YOWASP_CACHE_DIR:-$REPO/.yowasp_cache}"
export YOSYS_ABC="${YOSYS_ABC:-}"     # unused by wasm yosys; documented for native

run_yosys() {
  local script="$1"
  "$YOSYS" -p "$script" >"$LOG" 2>&1 || {
    echo "area_synth: yosys failed for top '$TOP' (log: $LOG)" >&2
    tail -n 40 "$LOG" >&2
    exit 1
  }
}

if [[ "$ABC_MODE" -eq 1 ]]; then
  # Real mapped area.  Requires yosys-abc next to the yosys binary / on PATH.
  SCRIPT="${READ}${CHPARAMS}hierarchy -check -top $TOP; synth -top $TOP -flatten; "
  SCRIPT+="dfflibmap -liberty $LIBERTY; abc -liberty $LIBERTY; opt_clean; "
  SCRIPT+="tee -o $STAT stat"
  run_yosys "$SCRIPT"
  AREA="$(grep -oE "Chip area for (top )?module '[^']*': [0-9.]+" "$LOG" "$STAT" 2>/dev/null \
          | tail -1 | grep -oE '[0-9.]+$' || true)"
  if [[ -z "$AREA" ]]; then
    # stat output format: "Chip area for module '\top': 1234.5"
    AREA="$(grep -oE "Chip area for module '[^']*':[[:space:]]*[0-9.]+" "$WORK"/stat_* 2>/dev/null \
            | tail -1 | grep -oE '[0-9.]+$' || true)"
  fi
  if [[ -z "$AREA" ]]; then
    echo "area_synth: ABC mode produced no 'Chip area' line; does '$YOSYS' have ABC?" >&2
    tail -n 20 "$LOG" >&2
    exit 1
  fi
  if [[ "$QUIET" -eq 0 ]]; then
    echo "LOOM_AREA: top=$TOP liberty=$LIBERTY mode=abc"
  fi
  echo "AREA_UM2: $AREA"
  echo "AREA_MODE: abc"
  rm -f "$WORK"/stat_* 2>/dev/null || true
  [[ "$KEEP_LOG" -eq 1 ]] || rm -f "$LOG"
  exit 0
fi

# ---- default: generic-gate synthesis + liberty-priced estimate -------------
SCRIPT="${READ}${CHPARAMS}hierarchy -check -top $TOP; "
SCRIPT+="synth -top $TOP -flatten -noabc; tee -o $STAT stat -json"
run_yosys "$SCRIPT"

if [[ "$QUIET" -eq 0 ]]; then
  echo "LOOM_AREA: top=$TOP liberty=$LIBERTY mode=gate_estimate"
fi
"$PYTHON" "$REPO/tools/area_estimate.py" --liberty "$LIBERTY" --stat-json "$STAT" --top "$TOP"

if [[ "$KEEP_LOG" -eq 1 ]]; then
  echo "LOOM_AREA: log retained at $LOG" >&2
else
  rm -f "$LOG"
fi
rm -f "$STAT"

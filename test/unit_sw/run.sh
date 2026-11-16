#!/usr/bin/env bash
# =============================================================================
# test/unit_sw/run.sh — software/toolchain test gate (owner: toolchain)
#
# Runs everything that does NOT need the RTL simulator:
#   * sw/isa.py self-test (frozen-header parse, encode/decode)
#   * sw/loom_model.py self-test (Lead's periodic-emit acceptance target)
#   * host/loom_host.py + host/spi_bitbang.py self-tests
#   * assembler / ISA / model / host / generator unit tests
#   * tools/area_synth.sh smoke test (skips if yosys/liberty are unavailable)
#
# Usage:
#   bash test/unit_sw/run.sh            # quiet-ish, prints SW_TESTS: PASS/FAIL
#   PYTHON=/path/to/python bash test/unit_sw/run.sh
#
# Exit status: 0 = all pass, non-zero = at least one failure.
# Always prints the final line "SW_TESTS: PASS" or "SW_TESTS: FAIL".
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PYTHON="${PYTHON:-$REPO/../.venv/bin/python}"

cd "$REPO" || { echo "SW_TESTS: FAIL (cannot cd to $REPO)"; exit 1; }
mkdir -p "$HERE/_artifacts"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

if [[ ! -x "$PYTHON" ]]; then
  echo "SW_TESTS: FAIL (python not found/executable: $PYTHON)"
  exit 1
fi

FAILED=0
FAILED_NAMES=""
PASSED=0

run() {
  local label="$1"; shift
  echo "=== $label"
  if "$@"; then
    PASSED=$((PASSED + 1))
  else
    echo "!!! FAILED: $label"
    FAILED=$((FAILED + 1))
    FAILED_NAMES+=" $label"
  fi
}

# --- module self-tests ------------------------------------------------------
run "sw/isa.py"            "$PYTHON" -m sw.isa
run "sw/loom_model.py"     "$PYTHON" -m sw.loom_model
run "host/loom_host.py"    "$PYTHON" -m host.loom_host
run "host/spi_bitbang.py"  "$PYTHON" -m host.spi_bitbang

# --- unit test files --------------------------------------------------------
run "test_isa.py"   "$PYTHON" "$HERE/test_isa.py"
run "test_asm.py"   "$PYTHON" "$HERE/test_asm.py"
run "test_model.py" "$PYTHON" "$HERE/test_model.py"
run "test_host.py"  "$PYTHON" "$HERE/test_host.py"
run "test_gen.py"   "$PYTHON" "$HERE/test_gen.py"
run "test_tools.py" "$PYTHON" "$HERE/test_tools.py"

# --- acceptance example programs -------------------------------------------
for asm in "$HERE"/*.asm; do
  [[ -e "$asm" ]] || continue
  base="$(basename "$asm" .asm)"
  run "assemble $base.asm" \
    "$PYTHON" -m sw.loom_asm "$asm" -o "$HERE/_artifacts/${base}.hex" \
    --lst "$HERE/_artifacts/${base}.lst" --sym "$HERE/_artifacts/${base}.sym" -q
done

echo
echo "SW_TESTS: $PASSED test groups passed, $FAILED failed"
if [[ "$FAILED" -ne 0 ]]; then
  echo "SW_TESTS: FAIL$FAILED_NAMES"
  exit 1
fi
echo "SW_TESTS: PASS"
exit 0

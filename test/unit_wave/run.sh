#!/bin/sh
# ============================================================================
#  test/unit_wave/run.sh - build + run the wave-player and capture unit tests
#  Usage: ./run.sh          (from any directory)
#  Exit status: 0 only if the RTL lints clean and both testbenches print PASS.
# ============================================================================
set -e
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
BUILD="build"
IVFLAGS="-g2012 -Wall -Wno-timescale"

rm -rf "$BUILD"
mkdir -p "$BUILD"

echo "== lint: rtl/loom_wave.v + rtl/loom_capture.v =="
iverilog -g2012 -Wall -o "$BUILD/lint" \
    "$ROOT/rtl/loom_wave.v" "$ROOT/rtl/loom_capture.v"
echo "   lint clean"

echo "== tb_wave =="
iverilog $IVFLAGS -o "$BUILD/tb_wave" tb_wave.v "$ROOT/rtl/loom_wave.v"
vvp "$BUILD/tb_wave" | tee "$BUILD/tb_wave.log"

echo "== tb_capture =="
iverilog $IVFLAGS -o "$BUILD/tb_capture" tb_capture.v "$ROOT/rtl/loom_capture.v"
vvp "$BUILD/tb_capture" | tee "$BUILD/tb_capture.log"

# Explicit PASS checks (pipe/tee would otherwise hide a non-zero vvp status)
grep -q "^WAVE_TB: PASS" "$BUILD/tb_wave.log"
grep -q "^CAPTURE_TB: PASS" "$BUILD/tb_capture.log"
echo "ALL UNIT_WAVE TESTS PASSED"

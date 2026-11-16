#!/usr/bin/env bash
# ============================================================================
#  test/unit_host/run.sh -- host-bridge (SPI slave + command engine) unit tests
#
#  Runs, with Icarus Verilog 12:
#    1. a warning-clean compile of rtl/loom_host.v + rtl/loom_fifo.v
#    2. tb_fifo.v : loom_fifo DEPTH=8 and DEPTH=2 (full/empty/count/wrap)
#    3. tb_host.v : bit-banged SPI mode-0 frames at 10 MHz SCK / 60 MHz clk
#                   against stub memory / CSR / thread-FIFO models
#
#  Exit status: 0 only if every testbench reports PASS, non-zero otherwise.
#  Add "+trace" to the vvp invocation (see below) for a per-transfer wire dump.
# ============================================================================
set -u
cd "$(dirname "$0")/../.." || exit 1

BUILD=test/unit_host/build
mkdir -p "$BUILD"
fail=0

echo "== 1/3 warning-clean RTL compile =="
if ! iverilog -g2012 -Wall -o /dev/null rtl/loom_host.v rtl/loom_fifo.v; then
    echo "HOST_TB: FAIL (RTL compile)"
    exit 1
fi

echo "== 2/3 tb_fifo (DEPTH=8 / DEPTH=2) =="
iverilog -g2012 -Wall -o "$BUILD/tb_fifo.vvp" \
    test/unit_host/tb_fifo.v rtl/loom_fifo.v || { echo "HOST_TB: FAIL (tb_fifo compile)"; exit 1; }
if ! vvp "$BUILD/tb_fifo.vvp" | tee "$BUILD/tb_fifo.log" | grep -q "FIFO_TB: PASS"; then
    echo "FIFO_TB: FAIL"
    fail=1
else
    grep -o "FIFO_TB: PASS" "$BUILD/tb_fifo.log"
fi

echo "== 3/3 tb_host (SPI mode 0, 60 MHz clk, 10 MHz and 100 kHz SCK) =="
iverilog -g2012 -Wall -o "$BUILD/tb_host.vvp" \
    test/unit_host/tb_host.v rtl/loom_host.v rtl/loom_fifo.v || { echo "HOST_TB: FAIL (tb_host compile)"; exit 1; }

# 10 MHz SCK (max rated): full suite, with the per-transfer wire trace dumped
if ! vvp "$BUILD/tb_host.vvp" +trace | tee "$BUILD/tb_host.log" | grep -q "HOST_TB: PASS"; then
    echo "HOST_TB: FAIL (SCK 10 MHz)"
    fail=1
else
    grep -o "HOST_TB: PASS" "$BUILD/tb_host.log"
fi

# 100 kHz SCK: same suite, proves no SCK-rate dependence
if ! vvp "$BUILD/tb_host.vvp" +bitns=10000 | tee "$BUILD/tb_host_slow.log" | grep -q "HOST_TB: PASS"; then
    echo "HOST_TB: FAIL (SCK 100 kHz)"
    fail=1
else
    echo "HOST_TB: PASS (SCK 100 kHz)"
fi

if [ "$fail" -ne 0 ]; then
    echo "unit_host: FAIL"
    exit 1
fi
echo "unit_host: PASS (fifo + host bridge)"
exit 0

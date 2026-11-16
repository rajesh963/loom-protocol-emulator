#!/usr/bin/env bash
# End-to-end integration test: bit-bangs the SPI host link to load a program and
# run a thread, then checks the hardware event scheduler produces an exact
# 16-cycle square wave on uo_out[2] (protocol pin P[10]).
set -euo pipefail
cd "$(dirname "$0")"
python3 gen_itest.py
iverilog -g2012 -I ../../rtl -o sim_top tb_top.v \
    ../../rtl/loom_core.v ../../rtl/loom_mem.v ../../rtl/loom_host.v \
    ../../rtl/loom_wave.v ../../rtl/tt_um_rajesh963_loom.v 2>&1 \
    | grep -vE "truncated|sensitive to all" || true
vvp -n sim_top > itest.log 2>&1 || true
grep -E "PING|readback|edges|ITEST|ERROR" itest.log
grep -q "ITEST: PASS" itest.log
echo "ITEST: PASS (script)"

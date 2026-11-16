#!/usr/bin/env bash
# Core unit tests: concurrent periodic events + computed-jump regression.
set -euo pipefail
cd "$(dirname "$0")"
PY=../../../.venv/bin/python
$PY gen_prog.py
iverilog -g2012 -I ../../rtl -o sim_core tb_core.v ../../rtl/loom_core.v 2>/dev/null
vvp -n sim_core > core_sim.log 2>&1 || true
grep -E "CORE_TB|ERROR" core_sim.log
grep -q "CORE_TB: PASS" core_sim.log
iverilog -g2012 -I ../../rtl -o sim_jump tb_jump.v ../../rtl/loom_core.v 2>/dev/null
vvp -n sim_jump > jump_sim.log 2>&1 || true
grep -E "JUMP_TB|ERROR" jump_sim.log
grep -q "JUMP_TB: PASS" jump_sim.log
echo "unit_core: PASS (events + computed jump)"

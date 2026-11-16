#!/usr/bin/env python3
"""Smoke test: tools/area_synth.sh (owner: toolchain).

The full RTL area run is slow-ish, so this test synthesises the small
`loom_fifo` module and checks the machine-readable output.  If yosys or the
liberty file is unavailable the test SKIPS (does not fail).

Run:  ../.venv/bin/python test/unit_sw/test_tools.py    (or via run.sh)
Prints TOOLS_TESTS: PASS and exits 0 on success.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "area_synth.sh"
YOSYS = REPO.parent / ".venv" / "bin" / "yowasp-yosys"
LIBERTY = REPO.parent / "research" / "sg13cmos5l_stdcell_typ_1p20V_25C.lib"

errors: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        errors.append(name)


if not SCRIPT.exists():
    check("tools/area_synth.sh exists", False)
elif not (YOSYS.exists() and LIBERTY.exists()):
    print("SKIP tools tests: yosys or liberty file not available")
else:
    proc = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
    check("area_synth --help", proc.returncode == 0
          and "AREA_UM2" in proc.stdout and "abc" in proc.stdout.lower())

    proc = subprocess.run([str(SCRIPT), "-t", "loom_fifo", "-q"],
                          capture_output=True, text=True, timeout=300)
    out = proc.stdout
    m = re.search(r"^AREA_UM2:\s*([0-9.]+)", out, re.M)
    check("area_synth runs (loom_fifo)", proc.returncode == 0,
          proc.stderr.strip()[-200:])
    check("area_synth prints AREA_UM2", m is not None, out[-200:])
    if m:
        area = float(m.group(1))
        check("area is positive and plausible", 1000.0 < area < 200000.0, f"{area:.1f} um^2")
    check("area_synth prints AREA_MODE", re.search(r"^AREA_MODE:\s*(gate_estimate|abc)",
                                                   out, re.M) is not None)
    check("area breakdown table", "sg13cmos5l_" in out and "count" in out)

    proc = subprocess.run([str(SCRIPT), "-t", "no_such_module", "-q"],
                          capture_output=True, text=True, timeout=60)
    check("area_synth rejects unknown top", proc.returncode != 0
          and "not found" in (proc.stderr + proc.stdout))

    # parameter override path (must at least run; NUM_THREADS=2 on loom_core)
    proc = subprocess.run([str(SCRIPT), "-t", "loom_core", "-P", "NUM_THREADS=2", "-q"],
                          capture_output=True, text=True, timeout=300)
    check("area_synth parameter override runs", proc.returncode == 0
          and re.search(r"^AREA_UM2:", proc.stdout, re.M) is not None,
          proc.stderr.strip()[-200:])

    # python helper is importable and rejects a bogus liberty
    sys.path.insert(0, str(REPO / "tools"))
    import area_estimate  # noqa: E402
    check("area_estimate map_cell covers core cells",
          area_estimate.map_cell("$_AND_") is not None
          and area_estimate.map_cell("$_DFFE_PN0P_") is not None)

    # per-instruction model retirement trace (co-sim debugging aid)
    trace_tool = REPO / "tools" / "model_retire_trace.py"
    proc = subprocess.run([sys.executable, str(trace_tool), "--seed", "2",
                           "--n-insns", "40", "--threads", "2",
                           "--tmr-at-run", "152", "--stall", "2",
                           "--limit", "3"],
                          capture_output=True, text=True, timeout=120)
    check("model_retire_trace.py runs", proc.returncode == 0,
          proc.stderr.strip()[-200:])
    check("model_retire_trace.py output", "# cycle tmr tid pc word mnemonic" in proc.stdout
          and "movi" in proc.stdout and "words_sha256=" in proc.stdout)

print("TOOLS_TESTS:", "PASS" if not errors else f"FAIL ({len(errors)} failures)")
sys.exit(0 if not errors else 1)

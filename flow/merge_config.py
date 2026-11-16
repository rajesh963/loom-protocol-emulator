#!/usr/bin/env python3
"""Merge / revert the LOOM SRAM flow keys in src/config.json.

The SRAM flow additions are stored as self-contained DELTA files:

    flow/config_sram.json      RM_IHPSG13_1P_1024x32 (140 183 um^2, 1024 words)
    flow/config_sram512.json   RM_IHPSG13_1P_512x32  ( 79 720 um^2,  512 words)

Every key in a delta (meta/MACROS/pdn/FP_PDN_*/VERILOG_DEFINES/...) is
independent of the base config, so a shallow top-level merge is correct and
keeps the base keys (CLOCK_PERIOD, PL_TARGET_DENSITY_PCT, the pdk:: max-fanout
block, ...) untouched.  Selecting "ff" removes exactly the delta keys again, so
the FF fallback build is byte-identical to the pre-SRAM config.

Usage:
    flow/merge_config.py <base.json> <ff|sram|sram512> [-o out.json]
Prints the merged JSON to stdout when -o is not given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DELTAS = {
    "sram": os.path.join(HERE, "config_sram.json"),
    "sram512": os.path.join(HERE, "config_sram512.json"),
}
FF_DEFINES = ["LOOM_PMEM_SMALL"]  # 128-word area-safe FF build
#: program-memory defines owned by this script; any OTHER build define that is
#: already in the config (e.g. -DLOOM_DMEM_SMALL, -DLOOM_ENABLE_CAPTURE) is
#: preserved across a build switch, since those are independent area levers.
PMEM_DEFINES = {"LOOM_PMEM_SMALL", "LOOM_PMEM_SRAM", "LOOM_PMEM_512"}
#: keys whose pre-SRAM values must be restored when switching back to `ff`
#: (everything else the SRAM delta adds is simply removed)
FF_RESTORE = {
    "//": "Only export pin area in LEF (without any connected nets)",
    "FP_PDN_VPITCH": 50.0,
    "FP_PDN_VWIDTH": 2.1,
    "FP_PDN_MULTILAYER": 0,
    "RUN_KLAYOUT_DRC": 0,
}
#: keys a previous SRAM merge may have introduced and that must be dropped
FF_DROP = {"KLAYOUT_DRC_RUNSET"}


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def delta_keys() -> set[str]:
    """Union of all top-level keys any SRAM delta can add."""
    keys: set[str] = set()
    for path in DELTAS.values():
        keys |= set(load(path).keys())
    keys.discard("//")          # shared comment key; keep the base one
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("base", help="src/config.json (read)")
    ap.add_argument("mode", choices=["ff", "sram", "sram512"])
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.base):
        raise SystemExit(f"merge_config: no such file: {args.base}")
    base = load(args.base)

    old_defines = list(base.get("VERILOG_DEFINES", []))
    extra_defines = [d for d in old_defines if d not in PMEM_DEFINES]

    if args.mode == "ff":
        for key in delta_keys() | FF_DROP:
            base.pop(key, None)
        base.update(FF_RESTORE)
        new_defines = FF_DEFINES
    else:
        delta = load(DELTAS[args.mode])
        base.update(delta)   # shallow: delta keys are self-contained
        new_defines = list(delta["VERILOG_DEFINES"])
    for define in extra_defines:
        if define not in new_defines:
            new_defines.append(define)
    base["VERILOG_DEFINES"] = new_defines

    text = json.dumps(base, indent=2) + "\n"
    if args.out:
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, args.out)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

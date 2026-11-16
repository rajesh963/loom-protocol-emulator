#!/usr/bin/env python3
"""Standard-cell area estimator for `tools/area_synth.sh` (owner: toolchain).

YoWASP's yosys (the only yosys available in this environment) does **not** ship
the ABC mapper, so `abc -liberty` cannot be used here.  This helper provides the
fallback area number: it takes a `stat -json` report produced after
`synth -top ... -flatten -noabc` (yosys generic-gate netlist) and prices every
generic cell with the area of the cheapest IHP SG13CMOS5L standard cell that
implements it, read from the liberty file itself.

It is an **estimate**: generic gates are all 2-input and the synthesis flow is
not ABC-optimised, so expect the number to be within roughly +10..20% of an
ABC-mapped synthesis of the same RTL.  When an ABC-enabled native yosys is
available, `tools/area_synth.sh --abc` uses the real mapped `stat` area instead.

Usage:
    python3 tools/area_estimate.py --liberty PATH --stat-json PATH [--top NAME]

Prints a per-cell-type breakdown and a final machine-readable line:
    AREA_UM2: <float>
    AREA_MODE: gate_estimate
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Generic yosys cell -> cheapest IHP implementation.  yosys emits a family of
# names (e.g. $_DFF_PN0_, $_DFFE_PN1P_), so most mapping is by prefix; the exact
# table below is only for the common combinational cells.
CELL_MAP: dict[str, str] = {
    "$_AND_": "sg13cmos5l_and2_1",
    "$_NAND_": "sg13cmos5l_nand2_1",
    "$_OR_": "sg13cmos5l_or2_1",
    "$_NOR_": "sg13cmos5l_nor2_1",
    "$_XOR_": "sg13cmos5l_xor2_1",
    "$_XNOR_": "sg13cmos5l_xnor2_1",
    "$_ANDNOT_": "sg13cmos5l_and2_1",
    "$_ORNOT_": "sg13cmos5l_or2_1",
    "$_NOT_": "sg13cmos5l_inv_1",
    "$_BUF_": "sg13cmos5l_buf_1",
    "$_MUX_": "sg13cmos5l_mux2_1",
    "$_NMUX_": "sg13cmos5l_mux2_1",
    "$_AOI3_": "sg13cmos5l_a21oi_1",
    "$_AOI4_": "sg13cmos5l_a22oi_1",
    "$_OAI3_": "sg13cmos5l_o21ai_1",
    "$_OAI4_": "sg13cmos5l_a22oi_1",
    "$_DLATCH_": "sg13cmos5l_dlhq_1",
    "$_DLATCH_P_": "sg13cmos5l_dlhq_1",
    "$_SR_": "sg13cmos5l_nor2_1",
}

#: cells needing an extra enable/select mux on top of the base flip-flop
EXTRA_MUX: frozenset[str] = frozenset()
MUX_AREA_KEY = "sg13cmos5l_mux2_1"


def map_cell(cell: str) -> tuple[str, bool] | None:
    """Return (liberty cell, needs_extra_mux) for a generic yosys cell type."""
    if cell in CELL_MAP:
        return CELL_MAP[cell], False
    if cell.startswith("$_DFFE") or cell.startswith("$_SDFFE") \
            or cell.startswith("$_SDFFCE"):
        return "sg13cmos5l_dfrbp_1" if "$_DFFE" in cell else "sg13cmos5l_sdfrbp_1", True
    if cell.startswith("$_SDFF"):
        return "sg13cmos5l_sdfrbp_1", False
    if cell.startswith("$_DFF"):
        return "sg13cmos5l_dfrbp_1", False
    if cell.startswith("$_DLATCH"):
        return "sg13cmos5l_dlhq_1", False
    return None


def parse_liberty_areas(path: str) -> dict[str, float]:
    areas: dict[str, float] = {}
    cur: str | None = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.search(r'\bcell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)', line)
            if m:
                cur = m.group(1)
            m = re.search(r'\barea\s*:\s*([0-9.]+)', line)
            if m and cur:
                areas[cur] = float(m.group(1))
    if not areas:
        raise SystemExit(f"area_estimate: no cell areas found in {path}")
    return areas


def pick_area(areas: dict[str, float], cell: str) -> float:
    """Area of the mapped liberty cell (prefer the cheapest variant)."""
    if cell in areas:
        return areas[cell]
    # prefer the _1 drive variant
    cand = [(a, n) for n, a in areas.items() if n.startswith(cell)]
    if cand:
        cand.sort()
        return cand[0][0]
    return 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--liberty", required=True)
    ap.add_argument("--stat-json", required=True)
    ap.add_argument("--top", default=None)
    args = ap.parse_args(argv)

    areas = parse_liberty_areas(args.liberty)
    with open(args.stat_json, "r", encoding="utf-8") as fh:
        stat = json.load(fh)
    modules = stat.get("modules", {})
    if not modules:
        raise SystemExit("area_estimate: no modules in stat JSON")
    top = args.top
    if top is None:
        top = next(iter(modules))
    key = top if top in modules else "\\" + top if ("\\" + top) in modules else None
    if key is None:
        # fall back to the largest module in the report
        key = max(modules, key=lambda k: modules[k].get("num_cells", 0))
    counts = modules[key].get("num_cells_by_type", {})

    total = 0.0
    total_cells = 0
    unknown: list[str] = []
    rows: list[tuple[str, str, int, float, float]] = []
    for cell, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        mapped = map_cell(cell)
        if mapped is None:
            unknown.append(cell)
            continue
        liberty_cell, needs_mux = mapped
        area = pick_area(areas, liberty_cell)
        if needs_mux:
            area += pick_area(areas, MUX_AREA_KEY)
        rows.append((cell, liberty_cell, count, area, count * area))
        total += count * area
        total_cells += count
    # unknown generic cells: price them as a 2-input NAND so the total is not
    # silently low; report them explicitly.
    fallback_unit = pick_area(areas, "sg13cmos5l_nand2_1")
    for cell in unknown:
        count = counts[cell]
        rows.append((cell, "sg13cmos5l_nand2_1", count, fallback_unit,
                     count * fallback_unit))
        total += count * fallback_unit
        total_cells += count

    print(f"LOOM_AREA: liberty={args.liberty}")
    print(f"LOOM_AREA: top={top.lstrip(chr(92))} generic_cells={total_cells}")
    print(f"{'generic cell':<22s} {'mapped stdcell':<26s} {'count':>7s} "
          f"{'area/cell':>10s} {'area':>12s}")
    for cell, liberty_cell, count, unit, area in rows:
        print(f"{cell:<22s} {liberty_cell:<26s} {count:7d} {unit:10.3f} {area:12.2f}")
    if unknown:
        print("LOOM_AREA_WARNING: unknown generic cells priced as NAND2: "
              + ", ".join(sorted(unknown)))
    print(f"AREA_UM2: {total:.2f}")
    print("AREA_MODE: gate_estimate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

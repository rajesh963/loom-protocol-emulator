#!/usr/bin/env python3
"""Bounded formal verification driver for LOOM (spec §10.4).

Pipeline (all open-source, offline):
    1. `yosys -p "read_verilog -formal ...; prep -top h_*; async2sync;
                  clk2fflogic; write_smt2 -wires ..."`  -> one SMT2 transition
       system per harness module in `formal/`,
    2. `yosys-smtbmc -s z3 -t K <smt2>`  -> bounded model check, K clock steps,
       with the z3 SMT solver installed in the project venv.

Each harness has an internal reset generator (a counter with initial value 0, so
`smtbmc` constrains state 0) and checks its assertions only after reset is
released; the proofs therefore start from a real post-reset state.

Scope (what is *proven*, not just simulated):
    h_fifo  -- occupancy never exceeds DEPTH, full/empty agree with the count,
               reset leaves the FIFO empty.
    h_core  -- reset completeness (TMR/POUT/DIR/threads/queues/FIFOs), event
               queue depth <= 4 + head pointer in range, TX/RX FIFO counts never
               exceed 8, DMEM port address in range, and POUT only changes
               through a defined cause.
    h_host  -- reset completeness (MISO/IRQ/FSM/pulses), command/effect
               ordering (mem_we only for WRITE_MEM, rx_push only for TX_PUSH,
               tx_pop only for RX_POP, csr_wr_en only for CSR-writing commands),
               host memory port always inside the 10-bit map.

Assumptions (documented, not proven): external responders (SPI pins, memory and
CSR read data, FIFO pop data, irq_code) are arbitrary (`(* anyseq *)`); the
memory decode maps 0x000-0x3BF to PMEM and 0x3C0-0x3FF to DMEM by construction
and gates PMEM writes on addr < PMEM_WORDS; bounded depth K is stated per proof.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORMAL = ROOT / "formal"
RTL = ROOT / "rtl"
BUILD = FORMAL / "build"
VENV_BIN = ROOT.parent / ".venv" / "bin"
YOSYS = VENV_BIN / "yowasp-yosys"
SMTBMC = VENV_BIN / "yowasp-yosys-smtbmc"
Z3_BIN = VENV_BIN / "z3"
YOWASP_CACHE = ROOT / ".yowasp_cache"

PROOFS = [
    # name,    harness,      depth, rtl sources
    ("fifo", "h_fifo", 8, ["loom_fifo.v"]),
    ("core", "h_core", 6, ["loom_core.v"]),
    ("host", "h_host", 6, ["loom_host.v", "loom_fifo.v"]),
]


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, **kw)


def generate_smt2(name: str, harness: str, sources: list[str]) -> pathlib.Path:
    BUILD.mkdir(exist_ok=True)
    smt2 = BUILD / f"{name}.smt2"
    verilog = " ".join(str(RTL / s) for s in sources) + " " + str(FORMAL / f"{harness}.v")
    script = (
        f"read_verilog -formal -I {RTL} {verilog}; "
        f"prep -top {harness}; async2sync; clk2fflogic; "
        f"write_smt2 -wires {smt2}"
    )
    env = dict(os.environ, YOWASP_CACHE_DIR=str(YOWASP_CACHE), PATH=f"{VENV_BIN}:{os.environ.get('PATH','')}")
    res = run([str(YOSYS), "-q", "-p", script], env=env)
    if res.returncode != 0 or not smt2.exists():
        raise RuntimeError(f"yosys failed for {name}:\n{res.stdout}\n{res.stderr}")
    return smt2


def bmc(smt2: pathlib.Path, depth: int) -> tuple[bool, str]:
    env = dict(os.environ, YOWASP_CACHE_DIR=str(YOWASP_CACHE), PATH=f"{VENV_BIN}:{os.environ.get('PATH','')}")
    res = run([str(SMTBMC), "-s", "z3", "-t", str(depth), str(smt2)], env=env)
    out = res.stdout + res.stderr
    ok = "Status: PASSED" in out and res.returncode == 0
    return ok, out


def main(argv: list[str]) -> int:
    if not YOSYS.exists():
        print(f"error: {YOSYS} not found", file=sys.stderr)
        return 2
    if not Z3_BIN.exists():
        print(f"warning: {Z3_BIN} not found; smtbmc will look for z3 on PATH",
              file=sys.stderr)
    BUILD.mkdir(exist_ok=True)
    results = []
    for name, harness, depth, sources in PROOFS:
        print(f"== {name}: generating SMT2 ({harness}, sources={sources})")
        try:
            smt2 = generate_smt2(name, harness, sources)
        except RuntimeError as exc:
            print(str(exc))
            results.append((name, False, "yosys error"))
            continue
        print(f"== {name}: BMC with z3, depth k={depth}")
        ok, out = bmc(smt2, depth)
        if ok:
            results.append((name, True, f"k={depth} SAT-free (PASSED)"))
            print(f"   PASS  ({name}.smt2, k={depth})")
        else:
            tail = "\n".join(out.strip().splitlines()[-12:])
            results.append((name, False, tail))
            print(f"   FAIL\n{tail}")
    print("\n===== formal results =====")
    for name, ok, detail in results:
        print(f"  {name:6s} {'PASS' if ok else 'FAIL'}  {detail.splitlines()[0]}")
    (FORMAL / "results.txt").write_text(
        "\n".join(f"{name} {'PASS' if ok else 'FAIL'} {detail}"
                  for name, ok, detail in results) + "\n", encoding="utf-8")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"\nformal: FAIL ({', '.join(failed)})")
        return 1
    print("\nformal: PASS (all bounded proofs)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

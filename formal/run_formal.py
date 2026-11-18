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
    h_sched -- scheduler exactness: an accepted timed event fires exactly in
               the cycle where TMR == its due time (never late/early), an edge
               event fires iff the masked pin condition holds, enqueue due
               times are exact (incl. the 12.4 PFRAC carry and behind-schedule
               clamp), and thread 0's fired value reaches pins_out in the fire
               cycle.  Assumes firmware enqueues before the due cycle and the
               queue is not full (a full queue stalls, it does not drop).

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
    # scheduler exactness: accepted timed events fire exactly on their due
    # cycle (never late/early), edge events fire iff the pin condition holds,
    # enqueue due times are exact (incl. 12.4 PFRAC carry / behind clamp), and
    # thread 0's fired value reaches pins_out in the same cycle.
    ("sched", "h_sched", 16, ["loom_core.v"]),   # instrumented copy
]




# ---------------------------------------------------------------------------
# Formal instrumentation for the scheduler proof: yosys does not resolve
# hierarchical references into submodule wire/regs (it creates undriven wires),
# so the sched proof compiles a *generated read-only observation wrapper*
# around rtl/loom_core.v: same logic, extra output ports that expose the
# internal scheduler signals.  No logic is changed (only `assign`s are added).
# ---------------------------------------------------------------------------
CORE_DBG_PORTS = [
    ("dbg_tmr", 16), ("dbg_fire", 4), ("dbg_fire_v", 64),
    ("dbg_pout", 16), ("dbg_npout", 16),
    ("dbg_sel", 2), ("dbg_opc", 7), ("dbg_imm", 16), ("dbg_src_a", 16),
    ("dbg_do_issue", 1), ("dbg_stall_issue", 1), ("dbg_pin_op", 1),
    ("dbg_csr_wr", 1), ("dbg_csr_wr_addr", 8), ("dbg_csr_wr_val", 16),
    ("dbg_ev_enq_t", 1), ("dbg_ev_enq_edge", 1),
    ("dbg_ev_enq_val", 16), ("dbg_ev_enq_time", 16),
    ("dbg_eq_head", 12), ("dbg_eq_cnt", 12),
    ("dbg_eq_tgt", 256), ("dbg_eq_val", 256),
    ("dbg_eq_edge", 16), ("dbg_eq_vali", 16),
    ("dbg_period", 64), ("dbg_nextt", 64), ("dbg_pfrac", 16),
    ("dbg_tmask", 64), ("dbg_emask", 64), ("dbg_econd", 64),
]

CORE_DBG_ASSIGNS = """
  // ---- generated formal observation ports (read-only, logic unchanged) ----
  assign dbg_tmr  = tmr;
  assign dbg_fire = {fire[3], fire[2], fire[1], fire[0]};
  assign dbg_fire_v = {fire_v[3], fire_v[2], fire_v[1], fire_v[0]};
  assign dbg_pout = pout;
  assign dbg_npout = npout;
  assign dbg_sel = sel;
  assign dbg_opc = opc;
  assign dbg_imm = imm;
  assign dbg_src_a = src_a;
  assign dbg_do_issue = do_issue;
  assign dbg_stall_issue = stall_issue;
  assign dbg_pin_op = pin_op;
  assign dbg_csr_wr = csr_wr;
  assign dbg_csr_wr_addr = csr_wr_addr;
  assign dbg_csr_wr_val = csr_wr_val;
  assign dbg_ev_enq_t = ev_enq_t;
  assign dbg_ev_enq_edge = ev_enq_edge;
  assign dbg_ev_enq_val = ev_enq_val;
  assign dbg_ev_enq_time = ev_enq_time;
  genvar dgi;
  generate for (dgi = 0; dgi < 4; dgi = dgi + 1) begin : dbg_unpack
    assign dbg_eq_head[dgi*3 +: 3]  = eq_head[dgi];
    assign dbg_eq_cnt[dgi*3 +: 3]   = eq_cnt[dgi];
    assign dbg_period[dgi*16 +: 16] = period[dgi];
    assign dbg_nextt[dgi*16 +: 16]  = nextt[dgi];
    assign dbg_pfrac[dgi*4 +: 4]    = pfrac[dgi];
    assign dbg_tmask[dgi*16 +: 16]  = tmask[dgi];
    assign dbg_emask[dgi*16 +: 16]  = emask[dgi];
    assign dbg_econd[dgi*16 +: 16]  = econd[dgi];
  end endgenerate
  genvar dgj;
  generate for (dgj = 0; dgj < 16; dgj = dgj + 1) begin : dbg_eq
    assign dbg_eq_tgt[dgj*16 +: 16]  = eq_tgt[dgj];
    assign dbg_eq_val[dgj*16 +: 16]  = eq_val[dgj];
    assign dbg_eq_edge[dgj]          = eq_edge[dgj];
    assign dbg_eq_vali[dgj]          = eq_val_i[dgj];
  end endgenerate
"""


def instrument_core() -> pathlib.Path:
    """Write a formal-only copy of rtl/loom_core.v with observation ports."""
    BUILD.mkdir(exist_ok=True)
    out = BUILD / "loom_core_probe.v"
    txt = (RTL / "loom_core.v").read_text(encoding="utf-8")
    txt = txt.replace("module loom_core #(", "module loom_core_probe #(", 1)
    if "module loom_core_probe" not in txt:
        raise RuntimeError("could not rename loom_core module for instrumentation")
    ports = "".join(f"    output wire [{w-1}:0] {n},\n"
                    for n, w in CORE_DBG_PORTS)
    # insert the observation ports just before the end of the port list
    head, sep, tail = txt.partition(");\n")
    if not sep:
        raise RuntimeError("could not find end of loom_core port list")
    txt = head + ",\n" + ports.rstrip(",\n") + "\n" + sep + tail
    idx = txt.rfind("endmodule")
    txt = txt[:idx] + CORE_DBG_ASSIGNS + "\n" + txt[idx:]
    out.write_text(txt, encoding="utf-8")
    return out


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, **kw)


def generate_smt2(name: str, harness: str, sources: list[str]) -> pathlib.Path:
    BUILD.mkdir(exist_ok=True)
    smt2 = BUILD / f"{name}.smt2"
    if name == "sched":
        # generated observation wrapper instead of the plain core (see above)
        probe = instrument_core()
        verilog = str(probe) + " " + str(FORMAL / f"{harness}.v")
    else:
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

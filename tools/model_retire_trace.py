#!/usr/bin/env python3
"""Emit a per-instruction model retirement trace for RTL-vs-model debugging.

Usage:
    python3 tools/model_retire_trace.py --seed 2 --n-insns 60 --threads 2 \
        [--tmr-at-run 152] [--stall 2] [--cycles 4000] [--limit 0] \
        [--allow-pull] [--allow-reactive]

This uses `sw.gen_program.gen()` for the program (identical to what the
co-simulation harness loads) and prints one line per retired instruction:

    cycle tmr tid pc word mnemonic wb=<rd>:<data> stall=<n> nextt=<n> ...

Generate the RTL side with the equivalent VPI/retirement monitor (pc, sel,
opcode, wb_data, dmem_we/addr/data) and diff the two files line by line; the
first differing line is the first instruction where the two models disagree.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sw import isa  # noqa: E402
from sw.gen_program import gen  # noqa: E402
from sw.loom_model import Loom  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="model retirement trace (co-sim debug)")
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--n-insns", type=int, default=60)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--tmr-at-run", type=int, default=0,
                    help="RTL TMR at the RUN CSR-write cycle (absolute-time align)")
    ap.add_argument("--stall", type=int, default=0,
                    help="initial stall applied to released threads (harness align)")
    ap.add_argument("--nextt-at-run", type=int, default=None,
                    help="override NEXTT at RUN (RTL nextt sampled at RUN commit)")
    ap.add_argument("--cycles", type=int, default=0,
                    help="max cycles (default: meta['cycles_bound']+4000)")
    ap.add_argument("--limit", type=int, default=0, help="print at most N lines")
    ap.add_argument("--allow-pull", action="store_true")
    ap.add_argument("--allow-reactive", action="store_true")
    args = ap.parse_args(argv)

    words, meta = gen(args.seed, args.n_insns, args.threads,
                      allow_pull=args.allow_pull,
                      allow_reactive=args.allow_reactive)
    m = Loom()
    m.load(words)
    m.cycle = args.tmr_at_run
    for th in meta["threads"]:
        for b in meta["preload_rx"][th["tid"]]:
            m.push_rx(th["tid"], b)
        m.set_pc(th["tid"], th["start_pc"])
    m.run_threads(meta["thread_mask"], nextt=args.nextt_at_run)
    if args.stall:
        for th in m.threads:
            if th.en:
                th.stall = args.stall
    m.retire_log = []
    m.inat_log = []
    m.event_enq_log = []
    m.event_fire_log = []
    cycles = args.cycles or (meta["cycles_bound"] + 4000)
    m.run(cycles)

    print(f"# seed={args.seed} n_insns={args.n_insns} threads={args.threads} "
          f"tmr_at_run={args.tmr_at_run} stall={args.stall} "
          f"cycles_bound={meta['cycles_bound']} words_sha256={meta['words_sha256']}")
    print("# cycle tmr tid pc word mnemonic wb stall nextt")
    shown = 0
    for r in m.retire_log:
        wb = f"{r['wb_rd']}:{r['wb_data']:04x}" if r["wb_en"] else "-"
        print(f"{r['cycle']:6d} {r['tmr']:5d} {r['tid']} {r['insn_pc']:4d} "
              f"{r['word']:08x} {r['mnemonic']:<6s} wb={wb:<9s} "
              f"stall={r['stall_set']:5d} nextt={r['nextt']:5d}")
        shown += 1
        if args.limit and shown >= args.limit:
            break
    print("# event enqueues (cycle tmr tid kind target val)")
    for e in m.event_enq_log:
        kind = "time" if e["kind"] == 0 else "edge"
        print(f"{e['cycle']:6d} {e['tmr']:5d} {e['tid']} {kind} "
              f"target={e['target']:5d} val=0x{e['val']:04x}")
    print("# event fires (cycle tmr tid kind target val)")
    for e in m.event_fire_log:
        kind = "time" if e["kind"] == 0 else "edge"
        print(f"{e['cycle']:6d} {e['tmr']:5d} {e['tid']} {kind} "
              f"target={e['target']:5d} val=0x{e['val']:04x}")
    print("# inat writebacks (absolute-cycle timing)")
    for e in m.inat_log:
        print(f"{e['cycle']:6d} {e['tmr']:5d} {e['tid']} INAT r{e['rd']} "
              f"data=0x{e['data']:04x}")
    if not args.limit:
        for th in m.threads:
            print(f"# t{th.tid}: halted={th.halt} pc={th.pc} r={th.r} z={th.z} "
                  f"c={th.c} nextt={th.nextt} period={th.period} eq={th.eq} "
                  f"tx={list(th.tx)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

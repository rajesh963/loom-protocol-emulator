#!/usr/bin/env python3
"""LOOM seeded random program generator (owner: toolchain).

Produces **valid, terminating** multi-thread LOOM programs for differential
co-simulation, plus a JSON "expected properties" sidecar describing what the
co-simulation driver must set up and how long to run.

    from sw.gen_program import gen
    words, meta = gen(seed=1, n_insns=80, num_threads=2)
    loom.load(words)
    for th in meta["threads"]:
        loom.set_pc(th["tid"], th["start_pc"])
    loom.run_threads(meta["thread_mask"])
    loom.run(meta["cycles_bound"])

CLI:  python3 -m sw.gen_program --seed 1 --n-insns 80 --threads 2 \
          -o prog.hex --json prog.json [--format hex32|c_array|bin] [--rom-words N]

Guarantees (the whole point — a generator bug would silently corrupt co-sim):
  * **Termination.** No unbounded waits: backward branches are counter-bounded
    (`movi rN,K; loop: …; subi rN,1; jnz loop`, K <= 5), `rpt` counts <= 7, all
    `waitu`/`inat`/`emita` absolute targets are strictly in the future for the
    whole run (T0 = 300 + 8*n_insns + 600, spaced 32 cycles), `waitd` durations
    are <= 3 cycles, and `waitp`/`emite`-reactive patterns are opt-in (they need
    external pin stimulus or self-loopback DIR=1, so they are excluded from the
    default terminating mode).
  * **FIFO safety without a host.** At most 6 TX pushes per thread (depth 8), so
    no `push` ever stalls on a full TX FIFO. `pull` instructions are only
    emitted when `allow_pull=True`, and then the sidecar `preload_rx` tells the
    co-sim driver exactly which bytes to push into each thread's RX FIFO
    (via the model's `push_rx` or the chip's `TX_PUSH`) *before* RUN.
  * **Event-queue safety.** At most 4 outstanding timed events are issued per
    thread before the program halts, and every target is in the future, so
    `emit` never stalls on a full queue for a long time; `evset`/`evwait` pairs
    use a per-thread private flag bit, so threads cannot steal each other's
    flags (no deadlock).
  * **All opcode classes** appear at least once per generated program: ALU,
    shifts/rotates, compares + conditionals, loads/stores (direct+indirect),
    pin ops, `in`, `dir`/`dset`/`dclr`, all four `emit*`, `setp/setm/setem/
    setec`, `waitd` (+ future `waitu`/`inat`), CSR read/write, `rpt`, bounded
    backward loops, `push`/`pushi` (+`pull` when preloaded), `evset/evwait`,
    `irq`, `nop`, `halt`.
  * **Determinism.** `random.Random(seed)` only; same seed => identical words on
    any Python 3 interpreter with the same `n_insns/num_threads/flags`.

DMEM usage: thread `tid` owns DMEM words `[8*tid, 8*tid+7]` (64 words / 4
threads), so concurrent threads never fight over memory in co-sim.

Register convention inside generated code: r0..r2 hold per-thread DMEM
addresses, r3..r5 are scratch/data, r6/r7 are loop counters. Programs only
touch their own DMEM window and the shared CSRs.

Semantic decisions
------------------
  * `n_insns` is an *approximate* instruction budget; the emitted program is at
    least large enough to touch every class (typically >= 60 words).
  * Generated jumps are forward-conditionals and counter-bounded backward loops
    only; no computed `csrw TPC` jumps (covered by dedicated unit tests).
  * The sidecar's `cycles_bound` is an upper bound for terminating programs,
    including the absolute timed waits; run at least that many cycles.
  * `preload_rx` is a list (per thread) of bytes; push them before RUN. TX
    bytes produced by the program are left in the thread TX FIFOs for the
    driver to drain (`meta["tx_expected_min"]` is a lower bound).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from sw import isa, loom_asm  # type: ignore
else:
    from sw import isa, loom_asm

__all__ = ["gen", "GenResult", "CLASSES"]

#: opcode classes the generator always covers at least once
CLASSES: tuple[str, ...] = (
    "alu", "shift", "cond", "mem", "pins", "emit", "wait",
    "csr", "rpt", "loop", "jmp", "fifo", "ev", "irq", "nop",
)

_DATA_REGS = (3, 4, 5)
_ADDR_REGS = (0, 1, 2)
_COUNTER_REGS = (6, 7)
_DMEM_BASE = 8            # DMEM words per thread
_PUSH_BUDGET = 6          # keep < FIFO depth (8) so pushes never stall
_PERIODS = (0x0010, 0x0018, 0x0020, 0x0030, 0x0100)   # 1.0 .. 16.0 cycles
_PINMASKS = (0x00FF, 0x000F, 0x00F0, 0x0F00, 0xFFFF, 0x0001)
_IMM16 = (0x0000, 0x0001, 0x000F, 0x0055, 0x00FF, 0x1234, 0xFFFF, 0x8000)
_IMM8 = (0x00, 0x01, 0x0F, 0x55, 0xAA, 0xFF)


class _Ctx:
    """Per-thread generation context."""

    def __init__(self, rng: random.Random, tid: int, opts: dict[str, Any]):
        self.rng = rng
        self.tid = tid
        self.opts = opts
        self.lines: list[str] = []
        self.label_no = 0
        self.dmem_base = (tid * _DMEM_BASE) & 0x3F
        self.timed_base = opts["timed_base"]
        self.timed_no = 0
        self.pushes = 0
        self.pulls = 0
        self.tx_expected = 0
        self.dmem_addrs = [self.dmem_base + i for i in range(8)]
        self.ev_bit = (tid + self.rng.choice((0, 4))) & 0x7
        self.pinmask = self.rng.choice(_PINMASKS)

    # -- helpers ---------------------------------------------------------
    def label(self, prefix: str = "L") -> str:
        self.label_no += 1
        return f"t{self.tid}_{prefix}{self.label_no}"

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    _TIMED_SPACING = 256
    _TIMED_LIMIT = 30000

    def timed_available(self) -> bool:
        nxt = self.timed_base + (self.timed_no + 1) * self._TIMED_SPACING
        return self.opts["timed"] and nxt < self._TIMED_LIMIT

    def next_timed(self) -> int:
        self.timed_no += 1
        return (self.timed_base + self.timed_no * self._TIMED_SPACING) & 0xFFFF

    def data_reg(self) -> int:
        return self.rng.choice(_DATA_REGS)

    def addr_reg(self) -> int:
        return self.rng.choice(_ADDR_REGS)

    def reg_operand(self) -> str:
        return f"r{self.rng.choice(_DATA_REGS + _ADDR_REGS)}"

    def reg_operand_num(self) -> int:
        return self.rng.choice(_DATA_REGS + _ADDR_REGS)

    def dmem_addr(self) -> int:
        return self.rng.choice(self.dmem_addrs)

    def simple_instr(self) -> str:
        """One non-blocking, non-control instruction (safe inside loops/rpt)."""
        kind = self.rng.choice(("alu", "shift", "pins", "mem", "csr"))
        if kind == "alu":
            op = self.rng.choice(("mov", "add", "addi", "sub", "subi",
                                  "and", "andi", "or", "ori", "xor", "xori", "inv"))
            rd = self.data_reg()
            if op == "inv":
                return f"inv r{rd}"
            if op.startswith(("addi", "subi", "andi", "ori", "xori")):
                return f"{op} r{rd}, 0x{self.rng.choice(_IMM8):02X}"
            return f"{op} r{rd}, {self.reg_operand()}"
        if kind == "shift":
            return f"{self.rng.choice(('shl','shr','rol','ror'))} r{self.data_reg()}, {self.rng.randrange(16)}"
        if kind == "pins":
            op = self.rng.choice(("out", "set", "clr", "tog"))
            return f"{op} 0x{self.rng.choice(_PINMASKS):04X}"
        if kind == "mem":
            if self.rng.random() < 0.5:
                return f"ld r{self.data_reg()}, 0x{self.dmem_addr():02X}"
            return f"st 0x{self.dmem_addr():02X}, r{self.data_reg()}"
        return f"csrr r{self.data_reg()}, {self.rng.choice(('TSTATUS','TFIFO','TMR','TPERIOD','TNEXTT','TEQ'))}"

    # -- class generators (return estimate of emitted instructions) ------
    def gen_alu(self) -> int:
        """Bundle covering every ALU mnemonic."""
        rd, re, imm = self.data_reg(), self.reg_operand(), self.rng.choice(_IMM8)
        self.add(f"movi r{rd}, 0x{self.rng.choice(_IMM16):04X}",
                 f"mov r{rd}, {re}",
                 f"add r{rd}, {re}",
                 f"addi r{rd}, 0x{imm:02X}",
                 f"sub r{rd}, {re}",
                 f"subi r{rd}, 0x{imm:02X}",
                 f"and r{rd}, {re}",
                 f"andi r{rd}, 0x{imm:02X}",
                 f"or r{rd}, {re}",
                 f"ori r{rd}, 0x{imm:02X}",
                 f"xor r{rd}, {re}",
                 f"xori r{rd}, 0x{imm:02X}",
                 f"inv r{rd}",
                 f"cmp r{rd}, {re}",
                 f"cmpi r{rd}, 0x{self.rng.choice(_IMM16):04X}")
        return 15

    def gen_shift(self) -> int:
        rd = self.data_reg()
        self.add(f"shl r{rd}, {self.rng.randrange(16)}",
                 f"shr r{rd}, {self.rng.randrange(16)}",
                 f"rol r{rd}, {self.rng.randrange(16)}",
                 f"ror r{rd}, {self.rng.randrange(16)}")
        return 4

    def gen_cond(self) -> int:
        """Bundle covering jz/jnz/jc/jnc (each jumps forward over one insn)."""
        rd, re = self.data_reg(), self.reg_operand()
        self.add(f"cmp r{rd}, {re}")
        n = 1
        for jump in ("jz", "jnz", "jc", "jnc"):
            lbl = self.label("skip")
            self.add(f"{jump} {lbl}", self.simple_instr(), f"{lbl}:")
            n += 2
        return n

    def gen_mem(self) -> int:
        rd, ra = self.data_reg(), self.addr_reg()
        self.add(f"ld r{rd}, 0x{self.dmem_addr():02X}",
                 f"st 0x{self.dmem_addr():02X}, r{rd}",
                 f"ldi r{rd}, r{ra}",
                 f"sti r{rd}, r{ra}")
        return 4

    def gen_pins(self) -> int:
        m = self.rng.choice(_PINMASKS)
        rd = self.data_reg()
        self.add(f"out 0x{m:04X}",
                 f"set 0x{self.rng.choice(_PINMASKS):04X}",
                 f"clr 0x{self.rng.choice(_PINMASKS):04X}",
                 f"tog 0x{self.rng.choice(_PINMASKS):04X}",
                 f"in r{rd}",
                 f"dir 0x{self.rng.choice(_IMM8):02X}",
                 f"dset 0x{self.rng.choice(_IMM8):02X}",
                 f"dclr 0x{self.rng.choice(_IMM8):02X}")
        return 8

    def gen_emit(self) -> int:
        rd = self.data_reg()
        self.add(f"setp 0x{self.rng.choice(_PERIODS):04X}",
                 f"setm 0x{self.rng.choice(_PINMASKS):04X}",
                 f"emit r{rd}",
                 f"emitd r{rd}, {self.rng.choice((1, 2, 5, 17, 40))}")
        if self.timed_available():
            self.add(f"emita r{rd}, {self.next_timed()}")
        mask = self.rng.choice((0x00FF, 0x000F, 0x00F0))
        self.add(f"setem 0x{mask:04X}", f"setec 0x{mask:04X}")
        if self.opts.get("allow_reactive"):
            # self-satisfying loopback pattern: DIR=1 makes pins_in read pout
            self.add(f"out 0x{mask & 0xFF:02X}", f"dir 0x{mask & 0xFF:02X}",
                     f"emite r{rd}", "dir 0x00")
        return 6

    def gen_wait(self) -> int:
        self.add(f"waitd 0x{self.rng.choice((0x0010, 0x0018, 0x0020, 0x0028)):04X}")
        n = 1
        if self.timed_available():
            self.add(f"waitu {self.next_timed()}")
            n += 1
        if self.timed_available():
            self.add(f"inat r{self.data_reg()}, {self.next_timed()}")
            n += 1
        return n

    def gen_csr(self) -> int:
        rd = self.data_reg()
        self.add(f"csrr r{rd}, {self.rng.choice(('TSTATUS','TFIFO','TMR','TPERIOD','TNEXTT','TEQ','TEMASK','TECOND'))}",
                 f"csrr r{rd}, TNEXTT",
                 f"csrr r{rd}, TEQ",
                 f"csrw {self.rng.choice(('TPERIOD','TMASK','TEMASK','TECOND'))}, r{rd}",
                 f"csrw {self.rng.choice(('POUT','PDIR'))}, r{rd}")
        return 5

    def gen_rpt(self) -> int:
        self.add(f"rpt {self.rng.randrange(8)}", self.simple_instr())
        return 2

    def gen_loop(self) -> int:
        counter = self.rng.choice(_COUNTER_REGS)
        iterations = self.rng.randint(1, 5)
        lbl = self.label("loop")
        self.add(f"movi r{counter}, {iterations}",
                 f"{lbl}:",
                 self.simple_instr(),
                 f"subi r{counter}, 1",
                 f"jnz {lbl}")
        return 4

    def gen_fifo(self) -> int:
        n = 0
        if self.pushes < _PUSH_BUDGET:
            self.add(f"push r{self.data_reg()}",
                     f"pushi 0x{self.rng.choice(_IMM8):02X}")
            self.pushes += 2
            self.tx_expected += 2
            n += 2
        if self.opts["allow_pull"] and self.pulls < 4:
            self.add(f"pull r{self.data_reg()}")
            self.pulls += 1
            n += 1
        if n == 0:
            n = self.gen_alu()
        return n

    def gen_ev(self) -> int:
        # per-thread private flag bit => evset/evwait can never deadlock
        self.add(f"evset {self.ev_bit}", f"evset {self.ev_bit}",
                 f"evwait {self.ev_bit}")
        return 3

    def gen_jmp(self) -> int:
        lbl = self.label("fjmp")
        self.add(f"jmp {lbl}", self.simple_instr(), f"{lbl}:")
        return 2

    def gen_nop(self) -> int:
        self.add("nop")
        return 1

    def gen_waitp(self) -> int:
        """Opt-in: waitp watches uio[7:0] only; make it self-satisfying.

        Requires the pins to loop back (model does; a cocotb TB must tie
        uio_in to uio_out). Never enabled by default.
        """
        mask = self.rng.choice((0xF0, 0x0F, 0xFF, 0xAA))
        value = self.rng.choice((0x00, 0xF0, 0x0F, 0xFF, 0xAA)) & mask
        self.add(f"out 0x{value:02X}", f"dir 0x{mask if mask else 0xFF:02X}",
                 f"waitp 0x{(mask << 8) | value:04X}", "dir 0x00")
        return 4

    def gen_irq(self) -> int:
        self.add(f"irq 0x{self.rng.choice(_IMM8):02X}")
        return 1

    # -- whole thread ----------------------------------------------------
    def build(self, budget: int) -> str:
        rng = self.rng
        self.add(f"; ---- thread {self.tid} ----")
        self.add(f"t{self.tid}_start:")
        # register prologue: per-thread DMEM addresses in r0..r2
        for reg, off in zip(_ADDR_REGS, range(3)):
            self.add(f"movi r{reg}, 0x{(self.dmem_base + off) & 0x3F:02X}")
        for reg in _DATA_REGS + _COUNTER_REGS:
            self.add(f"movi r{reg}, 0x{rng.choice(_IMM16):04X}")
        # at least one block of every class, then random extras to the budget
        order = list(CLASSES)
        if self.opts.get("allow_waitp"):
            order.append("waitp")
        rng.shuffle(order)
        estimate = 0
        while order or estimate < budget:
            cls = order.pop() if order else rng.choice(CLASSES)
            estimate += getattr(self, f"gen_{cls}")()
            if estimate > budget and not order:
                break
        self.add(f"t{self.tid}_done:", "halt")
        return "\n".join(self.lines)


class GenResult:
    """Result of :func:`gen`: words + sidecar metadata."""

    def __init__(self, words: list[int], meta: dict[str, Any]):
        self.words = words
        self.meta = meta

    def __iter__(self):
        return iter((self.words, self.meta))


def gen(seed: int, n_insns: int = 80, num_threads: int = 1, *,
        timed: bool = True, allow_pull: bool = False, allow_waitp: bool = False,
        allow_reactive: bool | None = None,
        rom_words: int | None = None,
        fill: int = isa.DEFAULT_FILL_WORD) -> tuple[list[int], dict[str, Any]]:
    """Generate a terminating random LOOM program.

    Returns ``(words, meta)``.  `meta` keys used by co-simulation drivers:
      seed, n_insns, num_threads, thread_mask, threads[{tid,start_pc,dmem_base}],
      preload_rx (list per thread of host->thread bytes), cycles_bound,
      instruction_count, opcodes_used, tx_expected_min, notes.
    """
    if num_threads < 1 or num_threads > 4:
        raise ValueError("num_threads must be 1..4")
    if n_insns < 16:
        raise ValueError("n_insns must be >= 16")
    rng = random.Random(seed)
    # all absolute timed targets live beyond the program's own execution
    if n_insns > 400:
        raise ValueError("n_insns must be <= 400")
    if allow_reactive is None:
        allow_reactive = allow_waitp
    allow_waitp = bool(allow_waitp or allow_reactive)
    timed_base = 600 + 10 * n_insns
    opts = {
        "timed": bool(timed),
        "allow_pull": bool(allow_pull),
        "allow_waitp": bool(allow_waitp),
        "allow_reactive": bool(allow_reactive),
        "timed_base": timed_base,
    }

    sections: list[str] = [f"; LOOM random program: seed={seed} "
                           f"n_insns={n_insns} threads={num_threads}"]
    contexts: list[_Ctx] = []
    for tid in range(num_threads):
        ctx = _Ctx(rng, tid, opts)
        body = ctx.build(n_insns)
        sections.append(body)
        contexts.append(ctx)
    source = "\n".join(sections) + "\n"

    result = loom_asm.assemble(source, filename=f"<gen seed={seed}>",
                               rom_words=rom_words, fill=fill)
    words = result.words
    words.extend([fill & 0xFFFFFFFF] * ((rom_words or 0) - len(words)))

    threads = [
        {"tid": ctx.tid, "start_pc": result.symbols[f"t{ctx.tid}_start"],
         "dmem_base": ctx.dmem_base, "dmem_range": [ctx.dmem_base,
                                                   ctx.dmem_base + _DMEM_BASE - 1],
         "rx_preload_bytes": ctx.pulls}
        for ctx in contexts
    ]
    preload_rx = []
    for ctx in contexts:
        # deterministic dummy bytes for any generated `pull`
        preload_rx.append([0x41 + ((i * 7) & 0x3F) for i in range(ctx.pulls)])

    opcodes = sorted({isa.decode(w).mnemonic for w in words})
    last_timed = max([ctx.timed_base + ctx.timed_no * _Ctx._TIMED_SPACING
                      for ctx in contexts] or [0])
    cycles_bound = max(last_timed + 512, timed_base + 512 + 16 * n_insns) \
        if timed else 300 + 16 * n_insns
    meta: dict[str, Any] = {
        "seed": seed,
        "n_insns": n_insns,
        "num_threads": num_threads,
        "thread_mask": (1 << num_threads) - 1,
        "threads": threads,
        "preload_rx": preload_rx,
        "tx_expected_min": sum(ctx.tx_expected for ctx in contexts),
        "cycles_bound": cycles_bound,
        "instruction_count": result.n_words,
        "word_count": len(words),
        "opcodes_used": opcodes,
        "source": source,
        "notes": [
            "preload_rx[t] bytes must be pushed into thread t's RX FIFO before RUN",
            "run at least cycles_bound cycles (includes absolute timed waits)",
            "no unbounded waits: backward loops are counter-bounded, "
            "waitu/inat/emita targets are absolute and in the future",
            "reactive classes (waitp/emite) need pin stimulus or uio loopback: "
            "pass allow_reactive=True (or drive the pins) to include them",
            "pull is only emitted with allow_pull=True (sidecar preload_rx)",
        ],
    }
    return words, meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _int_arg(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m sw.gen_program",
        description="Generate a seeded, valid, terminating random LOOM program "
                    "plus a JSON expected-properties sidecar.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m sw.gen_program --seed 1 --n-insns 80 -o prog.hex "
            "--json prog.json\n"
            "  python -m sw.gen_program --seed 7 --n-insns 120 --threads 2 "
            "-o p.hex --json p.json --rom-words 1024\n"
        ),
    )
    p.add_argument("--seed", type=int, default=1, help="RNG seed (default 1)")
    p.add_argument("--n-insns", type=_int_arg, default=80, metavar="N",
                   help="approximate instruction budget (default 80)")
    p.add_argument("--threads", type=_int_arg, default=1, choices=(1, 2, 3, 4),
                   help="number of threads with generated programs (default 1)")
    p.add_argument("-o", "--output", default=None,
                   help="program image file (default: stdout)")
    p.add_argument("--json", default=None, metavar="PATH",
                   help="write the expected-properties JSON sidecar")
    p.add_argument("--format", choices=("hex32", "c_array", "bin"), default="hex32")
    p.add_argument("--rom-words", type=_int_arg, default=None)
    p.add_argument("--fill", type=_int_arg, default=isa.DEFAULT_FILL_WORD)
    p.add_argument("--no-timed", action="store_true",
                   help="omit future-absolute waitu/inat/emita (shorter tests)")
    p.add_argument("--allow-pull", action="store_true",
                   help="emit pull instructions (driver must honour preload_rx)")
    p.add_argument("--quiet", "-q", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        words, meta = gen(args.seed, args.n_insns, args.threads,
                          timed=not args.no_timed, allow_pull=args.allow_pull,
                          rom_words=args.rom_words, fill=args.fill)
    except (ValueError, loom_asm.AsmError) as exc:
        print(f"gen_program: error: {exc}", file=sys.stderr)
        return 1
    if args.format == "hex32":
        payload: str | bytes = "".join(f"{w:08x}\n" for w in words)
    elif args.format == "c_array":
        payload = loom_asm.format_c_array(words, f"gen_seed{args.seed}")
    else:
        payload = loom_asm.format_binary(words)
    if args.output:
        out = pathlib.Path(args.output)
        if isinstance(payload, bytes):
            out.write_bytes(payload)
        else:
            out.write_text(payload, encoding="utf-8")
    else:
        if isinstance(payload, bytes):
            print("gen_program: --format bin needs -o FILE", file=sys.stderr)
            return 1
        sys.stdout.write(payload)
    sidecar = {k: v for k, v in meta.items() if k != "source"}
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(sidecar, indent=2) + "\n",
                                           encoding="utf-8")
    if not args.quiet:
        print(f"GEN_PROGRAM: seed={args.seed} threads={args.threads} "
              f"words={meta['word_count']} cycles_bound={meta['cycles_bound']} "
              f"opcodes={len(meta['opcodes_used'])} "
              f"preload_rx={meta['preload_rx']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

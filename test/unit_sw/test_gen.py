#!/usr/bin/env python3
"""Unit tests: sw/gen_program.py random generator (owner: toolchain).

Run:  ../.venv/bin/python test/unit_sw/test_gen.py    (or via run.sh)
Prints GEN_TESTS: PASS and exits 0 on success.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sw import isa, loom_asm  # noqa: E402
from sw.gen_program import gen  # noqa: E402
from sw.loom_model import Loom  # noqa: E402

errors: list[str] = []
ART = pathlib.Path(__file__).resolve().parent / "_artifacts"
ART.mkdir(exist_ok=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        errors.append(name)


def execute(words: list[int], meta: dict) -> Loom:
    m = Loom()
    m.load(words)
    for th in meta["threads"]:
        for b in meta["preload_rx"][th["tid"]]:
            check(f"push_rx t{th['tid']}", m.push_rx(th["tid"], b))
        m.set_pc(th["tid"], th["start_pc"])
    m.run_threads(meta["thread_mask"])
    m.run(meta["cycles_bound"])
    return m


# ---------------------------------------------------------------------------
# 1. API shape + determinism
# ---------------------------------------------------------------------------
w1, m1 = gen(seed=1, n_insns=60, num_threads=2)
w2, m2 = gen(seed=1, n_insns=60, num_threads=2)
w3, m3 = gen(seed=2, n_insns=60, num_threads=2)
check("gen returns (words, meta)", isinstance(w1, list) and isinstance(m1, dict))
check("deterministic for a seed", w1 == w2 and m1 == m2)
check("different seeds differ", w1 != w3)
meta_keys = {"seed", "n_insns", "num_threads", "thread_mask", "threads",
             "preload_rx", "cycles_bound", "instruction_count", "word_count",
             "opcodes_used", "tx_expected_min"}
check("meta has required keys", meta_keys <= set(m1), f"{meta_keys - set(m1)}")
check("thread starts are valid PCs",
      all(0 <= t["start_pc"] < len(w1) for t in m1["threads"]))
check("thread mask", m1["thread_mask"] == 0b11)
check("cycles_bound positive", m1["cycles_bound"] > 1000)
check("words are 32-bit", all(0 <= w <= 0xFFFFFFFF for w in w1))
import hashlib
sha = hashlib.sha256(b"".join(int(w).to_bytes(4, "little") for w in w1)).hexdigest()
check("words_sha256 present and correct", m1.get("words_sha256") == sha,
      f"{m1.get('words_sha256')}")
check("words_sha256 deterministic", m2.get("words_sha256") == sha)

# ---------------------------------------------------------------------------
# 2. validity + termination across seeds/thread counts
# ---------------------------------------------------------------------------
need_default = (
    "nop", "halt", "movi", "mov", "add", "addi", "sub", "subi", "and", "andi",
    "or", "ori", "xor", "xori", "inv", "shl", "shr", "rol", "ror", "cmp",
    "cmpi", "csrw", "csrr", "out", "set", "clr", "tog", "in", "dir", "dset",
    "dclr", "emit", "emita", "emitd", "setp", "setm", "setem", "setec",
    "waitd", "waitu", "inat", "ld", "st", "ldi", "sti", "push", "pushi",
    "jmp", "jz", "jnz", "jc", "jnc", "rpt", "evset", "evwait", "irq",
)
coverage_union: set[str] = set()
for seed in (1, 2, 3, 7, 42, 123, 999, 31337):
    for nts in (1, 2, 4):
        words, meta = gen(seed, 80, nts)
        coverage_union |= set(meta["opcodes_used"])
        m = execute(words, meta)
        halted = all(m.threads[t].halt for t in range(nts))
        check(f"terminates seed={seed} threads={nts}", halted,
              f"cycle={m.cycle} bound={meta['cycles_bound']}")
        check(f"no unbounded waits seed={seed} threads={nts}",
              m.cycle <= meta["cycles_bound"])
check("default programs cover all non-reactive mnemonics",
      set(need_default) <= coverage_union,
      f"missing {sorted(set(need_default) - coverage_union)}")
check("halted threads remain enabled (halt != stop)",
      all(m.threads[t].en for t in range(nts)))

# ---------------------------------------------------------------------------
# 3. opt-in coverage: pull + reactive classes + all 59 mnemonics
# ---------------------------------------------------------------------------
words, meta = gen(11, 100, num_threads=2, timed=True, allow_pull=True,
                  allow_reactive=True)
allops = set(meta["opcodes_used"])
missing = set(isa.INSTRUCTIONS) - allops
check("opt-in program covers all 59 mnemonics", not missing, f"missing {sorted(missing)}")
m = execute(words, meta)
check("opt-in program terminates", all(m.threads[t].halt for t in range(2)))
check("preload_rx matches pulls",
      sum(len(x) for x in meta["preload_rx"])
      == sum(t["rx_preload_bytes"] for t in meta["threads"]))

# ---------------------------------------------------------------------------
# 4. FIFO safety: TX pushes must not exceed depth 8 per thread
# ---------------------------------------------------------------------------
for seed in (1, 5, 9, 17):
    words, meta = gen(seed, 120, num_threads=2)
    m = execute(words, meta)
    check(f"tx fifo never overflows seed={seed}",
          all(m.tx_count(t) <= 8 for t in range(2)))
check("tx_expected_min is a lower bound",
      meta["tx_expected_min"] >= 0)

# ---------------------------------------------------------------------------
# 4b. opt-in combinations must still terminate within the sidecar bound
#     (regression: verify seed 101 pull+reactive exceeded cycles_bound because
#     a masked WAITP could be clobbered by another thread's OUT/DIR)
# ---------------------------------------------------------------------------
for seed in (101, 202, 303):
    for nts in (1, 2):
        words, meta = gen(seed, 40, nts, allow_pull=True,
                          allow_reactive=True)
        m = execute(words, meta)
        check(f"combined pull+reactive terminates seed={seed} t={nts}",
              all(m.threads[t].halt for t in range(nts)),
              f"cycle={m.cycle} bound={meta['cycles_bound']}")
# reactive classes are emitted in always-satisfied form (no external pins)
words, meta = gen(7, 60, 2, allow_reactive=True)
waitps = [w for w in words if isa.decode(w).mnemonic == "waitp"]
check("reactive program uses waitp", len(waitps) >= 1)
check("generated waitp uses mask 0 (always satisfied)",
      all(isa.decode(w).imm16 == 0 for w in waitps), f"{[hex(w) for w in waitps]}")
# the same bound must hold for a few larger/non-default sizes
for seed in (5, 55):
    words, meta = gen(seed, 120, 2, allow_pull=True, allow_reactive=True)
    m = execute(words, meta)
    check(f"larger combined program terminates seed={seed}",
          all(m.threads[t].halt for t in range(2)))

# ---------------------------------------------------------------------------
# 5. CLI: words + JSON sidecar
# ---------------------------------------------------------------------------
hex_path = ART / "gen.hex"
json_path = ART / "gen.json"
proc = subprocess.run(
    [sys.executable, "-m", "sw.gen_program", "--seed", "5", "--n-insns", "60",
     "--threads", "2", "-o", str(hex_path), "--json", str(json_path),
     "--rom-words", "256", "-q"],
    cwd=REPO, capture_output=True, text=True)
check("gen CLI success", proc.returncode == 0, proc.stderr[:200])
if proc.returncode == 0:
    words_cli = [int(line, 16) for line in hex_path.read_text().splitlines()]
    sidecar = json.loads(json_path.read_text())
    check("gen CLI hex image", len(words_cli) == 256 and sidecar["word_count"] == 256)
    check("gen CLI sidecar", sidecar["seed"] == 5 and "threads" in sidecar
          and "preload_rx" in sidecar and "cycles_bound" in sidecar)
    check("gen CLI image runs", all(
        t.halt for t in execute(words_cli, sidecar).threads))
    check("gen CLI padded with halt", words_cli[-1] == isa.HALT_WORD)
# c_array / bin formats
for fmt in ("c_array", "bin"):
    outp = ART / f"gen.{fmt}"
    proc = subprocess.run(
        [sys.executable, "-m", "sw.gen_program", "--seed", "6", "--n-insns", "40",
         "-o", str(outp), "--format", fmt, "-q"],
        cwd=REPO, capture_output=True, text=True)
    check(f"gen CLI --format {fmt}", proc.returncode == 0 and outp.exists())

# invalid parameters
proc = subprocess.run(
    [sys.executable, "-m", "sw.gen_program", "--seed", "1", "--n-insns", "5", "-q"],
    cwd=REPO, capture_output=True, text=True)
check("gen CLI rejects tiny n_insns", proc.returncode != 0)
proc = subprocess.run(
    [sys.executable, "-m", "sw.gen_program", "--seed", "1", "--threads", "5", "-q"],
    cwd=REPO, capture_output=True, text=True)
check("gen CLI rejects >4 threads", proc.returncode != 0)

# ---------------------------------------------------------------------------
# 6. generated programs assemble cleanly through the public assembler path too
# ---------------------------------------------------------------------------
words, meta = gen(21, 50, 1)
res = loom_asm.assemble(meta["source"], filename="<regen>")
check("meta['source'] re-assembles to identical words", res.words == words)

print("GEN_TESTS:", "PASS" if not errors else f"FAIL ({len(errors)} failures)")
sys.exit(0 if not errors else 1)

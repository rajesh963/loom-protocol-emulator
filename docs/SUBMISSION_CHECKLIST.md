# Submission checklist

**What this is.** The competition's submission requirements translated into a checklist with an
owner and a status per line, plus the command list that has to pass before the submit button is
pressed. Source of requirements: the
[Jane Street competition announcement and FAQ](https://blog.janestreet.com/protocol-emulator-asic-competition/)
("What do I submit?", "What sign-off do I need?", "How will entries be judged?") and the Tiny
Tapeout IHP Verilog template. Status values: **done**, **in progress**, **blocked**, **TBD**.

Deadline: **2027-01-18**. Shuttle: March 2027, IHP CMOS5L, 8×4 tiles.

---

## 1. Repository and sign-off

| # | Requirement | Evidence / artefact | Owner | Status |
|---|---|---|---|---|
| 1 | Public repository with RTL | this repo, Apache-2.0 (`LICENSE`) | lead | TBD (make public at submission) |
| 2 | `info.yaml` complete and valid: 8×4, 60 MHz, `tt_um_rajesh963_loom`, source_files, full 24-pin pinout, `yaml_version: 6` | [`info.yaml`](../info.yaml) | docs | **done** |
| 3 | `src/` contains exactly the build sources (`rtl/` mirrored) | `tools/sync_src.sh` | lead | **done** (re-run before submit) |
| 4 | RTL lint clean (`iverilog -g2012 -I rtl -o /dev/null rtl/*.v`) | CI / local | lead | TBD |
| 5 | `gds` GitHub Action green (LibreLane, `tt-gds-action@ttihp27a`, `ihp-sg13cmos5l`) | Actions run | lead | TBD |
| 6 | `precheck` green | Actions run | lead | TBD |
| 7 | `gl_test` green (gate-level protocol suite) | Actions run + `test/results.xml` | verify | TBD |
| 8 | Both build variants buildable (FF fallback and SRAM flagship) | CI matrix | lead | TBD |
| 9 | No `TODO`/`FIXME` left in delivered RTL or firmware | `grep -rn TODO rtl/ sw/` | all | TBD |
| 9b | `test` workflow reproduces the full cocotb matrix on GitHub (57 PASS / 2 SKIP) | Actions run | verify | **done** (green on `5ff6c6c29cc7`) |
| 9c | `docs` workflow green (datasheet builds from `docs/info.md`) | Actions run | docs | **done** (green) |

## 2. Documentation (the write-up is weighted heavily)

| # | Requirement | Evidence | Owner | Status |
|---|---|---|---|---|
| 10 | Architecture documentation | [`architecture.md`](architecture.md) | docs | **done** |
| 11 | Instruction-set documentation | [`isa.md`](isa.md) (tables generated from `sw/isa.py`) | docs | **done** |
| 12 | Tiny Tapeout datasheet (how it works / how to test / external hardware) | [`info.md`](info.md) | docs | **done** |
| 13 | Host protocol documentation, including how a program is loaded | [`host-protocol.md`](host-protocol.md) | docs | **done** |
| 14 | Protocol examples with timing budgets | [`examples.md`](examples.md) + [`examples_src/`](examples_src/) | docs / firmware | **done** (sketches assemble) |
| 15 | Verification write-up | [`verification.md`](verification.md) | docs / verify | **done** (results TBD) |
| 16 | Competition write-up (problem, insight, timing, results, comparison, AI usage) | [`writeup.md`](writeup.md) | docs | **done** (results tables TBD) |
| 17 | Results: area, timing, GDS, gate-level, formal | [`results.md`](results.md) | lead + docs | **in progress** |
| 18 | README front page: pitch, comparison, pinout, layout, quickstart, verification summary | [`README.md`](../README.md) | docs | **done** |
| 19 | AI-tool usage described | [`writeup.md` §10](writeup.md), README | docs | **done** |
| 20 | Diagrams: block, pinout, timing, event queue (SVG, < 512 KB each, < 1 MB total) | [`images/`](images/) — 48 KB total | docs | **done** |
| 21 | Every internal link resolves | smoke test §4 | docs | TBD (re-run at submit) |

## 3. Toolchain, examples and host

| # | Requirement | Evidence | Owner | Status |
|---|---|---|---|---|
| 22 | Assembler with relocations and a CLI | `sw/loom_asm.py` (`python3 -m sw.loom_asm`) | toolchain | **done** |
| 23 | Golden ISA model + random program generator | `sw/loom_model.py`, `sw/gen_program.py` | toolchain | **in progress** |
| 24 | ISA tables as the single source of truth | `sw/isa.py` (`summary_table`/`csr_table`/`host_command_table`) | toolchain | **done** |
| 25 | Host client (Python) usable against RTL/model/board | `host/loom_host.py` | toolchain | **in progress** |
| 26 | Example programs for UART, SPI (both roles), I2C, PS/2, 1-Wire, WS2812 | `sw/examples/` | lead / firmware | **in progress** |
| 27 | Example programs assemble cleanly in CI | smoke test §4 | toolchain | TBD |
| 28 | Protocol VIPs (UART/SPI/I2C) with ASCII waveform dumps | `test/` | verify | **in progress** |
| 29 | Differential co-simulation RTL vs model | `test/` | verify | **in progress** |
| 30 | Formal properties F1–F9 (yosys SMT2 + z3) | `formal/` | verify | **in progress** |
| 31 | Coverage matrix report | `test/coverage.json` | verify | TBD |
| 32 | Optional demo video / scope captures | — | lead | optional |

## 4. Pre-submission smoke test

Run from the repo root. Every line must succeed (exit 0) before submitting; the last two are
informational checks on the docs themselves.

```sh
#!/bin/sh
set -e
cd loom

# --- metadata ---
python3 -c "import yaml,sys; d=yaml.safe_load(open('info.yaml')); assert d['yaml_version']==6; assert d['project']['tiles']=='8x4'; assert d['project']['clock_hz']==60000000; assert d['project']['top_module']=='tt_um_rajesh963_loom'; assert len(d['pinout'])==24; print('info.yaml OK')"

# --- toolchain ---
python3 -m sw.isa > /dev/null
for f in docs/examples_src/*.asm; do python3 -m sw.loom_asm "$f" -o /dev/null; done

# --- RTL lint + unit/integration benches ---
iverilog -g2012 -I rtl -o /dev/null rtl/*.v
bash test/unit_core/run.sh
bash test/unit_host/run.sh
bash test/unit_wave/run.sh
bash test/integration/run.sh

# --- src/ mirror is up to date for the flow ---
bash tools/sync_src.sh
for f in loom_bus.vh loom_mem.v loom_core.v loom_host.v loom_wave.v tt_um_rajesh963_loom.v; do
  test -f "src/$f" || { echo "missing src/$f"; exit 1; }
done

# --- docs hygiene ---
# 1. only explicit results tables may contain TBD
grep -rn "TBD" docs/ README.md | grep -v -E "docs/(results|verification)\.md|README\.md" && exit 1 || true
# 2. no image over 512 KB and images total under 1 MB
test -z "$(find docs/images -size +512k)" || exit 1
test "$(du -sk docs/images | cut -f1)" -lt 1024 || exit 1
# 3. no broken relative markdown links (TBD check at submit)
python3 - <<'PY'
import pathlib, re, sys
bad = []
for md in [pathlib.Path('README.md')] + sorted(pathlib.Path('docs').glob('*.md')):
    for target in re.findall(r'\]\(([^)#][^)]*)\)', md.read_text()):
        if target.startswith(('http://', 'https://', 'mailto:')):
            continue
        if not (md.parent / target).exists():
            bad.append(f'{md}: {target}')
print('\n'.join(bad) if bad else 'links OK')
sys.exit(1 if bad else 0)
PY

# --- CI gates that must be green in Actions (not runnable locally) ---
echo "REMEMBER: gds, precheck, gl_test and docs actions must be green on the final commit"
echo "REMEMBER: docs/results.md filled with the final git revision and CI numbers"
```

## 5. Submission day

1. Freeze `main`, run §4 on a clean clone (not just the working tree).
2. Confirm the four Actions jobs are green on that exact commit: `gds`, `precheck`, `gl_test`,
   `docs`.
3. Fill [`results.md`](results.md) and the results tables in [`writeup.md`](writeup.md) §7 and
   [`verification.md`](verification.md) §9 with the final revision and CI links.
4. Make the repository public (or flip it public at the moment of submission), verify from a logged-
   out browser that the README, docs and images render.
5. Record in `results.md` which build was submitted — **SRAM flagship if its CI is green, otherwise
   the FF fallback** — and the exact git SHA; both are built from the same RTL (`PMEM_IMPL`/
   `PMEM_WORDS` only).
6. Submit the repository link through the competition form (added to the announcement page before
   the deadline), with `spec/ARCHITECTURE.md`, `docs/writeup.md` and `docs/verification.md` called
   out as the entry points.

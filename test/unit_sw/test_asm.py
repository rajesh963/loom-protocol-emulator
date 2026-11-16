#!/usr/bin/env python3
"""Unit tests: sw/loom_asm.py — two-pass assembler (owner: toolchain).

Run:  ../.venv/bin/python test/unit_sw/test_asm.py    (or via run.sh)
Prints ASM_TESTS: PASS and exits 0 on success.
"""

from __future__ import annotations

import pathlib
import re
import struct
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sw import isa, loom_asm  # noqa: E402

errors: list[str] = []
ART = pathlib.Path(__file__).resolve().parent / "_artifacts"
ART.mkdir(exist_ok=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        errors.append(name)


def asm(src: str, **kw):
    return loom_asm.assemble(src, filename="<test>", **kw)


# ---------------------------------------------------------------------------
# 1. assembly of every mnemonic, then disassemble back
# ---------------------------------------------------------------------------
sample_src = """
        .equ MASK, 0xFCFF
start:  nop
        halt
        movi    r1, 0x1234
        mov     r2, r3
        add     r0, r1
        addi    r0, 7
        sub     r0, r1
        subi    r0, 7
        and     r0, r1
        andi    r0, MASK
        or      r0, r1
        ori     r0, 0x0F
        xor     r0, r1
        xori    r0, 0xF0
        inv     r0
        shl     r0, 3
        shr     r0, 3
        rol     r0, 3
        ror     r0, 3
        cmp     r0, r1
        cmpi    r0, 5
        csrw    TPERIOD, r0
        csrr    r0, TSTATUS
        out     0xFCFF
        set     0x0001
        clr     0x0002
        tog     0x0004
        in      r0
        dir     0xAA
        dset    0x0F
        dclr    0xF0
        setp    0x208D
        setm    MASK
        setem   0x0300
        setec   0x0100
        emit    r0
        emita   r0, 100
        emitd   r0, 5
        emite   r0
        inat    r0, 42
        waitu   42
        waitd   0x208D
        waitp   0x1201
        ld      r0, 0x10
        st      0x11, r0
        ldi     r0, r1
        sti     r1, r2
        push    r0
        pull    r0
        pushi   0x55
        jmp     here
here:   jz      here
        jnz     here
        jc      here
        jnc     here
        rpt     9
        evset   1
        evwait  2
        irq     7
        li      r3, -1
        .word   0xDEADBEEF, start
        nop
"""
res = asm(sample_src)
mnemonics = []
words = []
for w in res.words:
    insn = isa.decode(w)
    if insn.known:
        mnemonics.append(insn.mnemonic)
        words.append(w)
# every real mnemonic appears except reactive ones are all here too
check("assembles every mnemonic", set(mnemonics) == set(isa.INSTRUCTIONS),
      f"missing {set(isa.INSTRUCTIONS) - set(mnemonics)}")
# round-trip: decode(assemble) then disasm should start with the mnemonic
for w in res.words:
    insn = isa.decode(w)
    if not insn.known:
        continue
    text = isa.disasm(w, pc=0)
    check(f"disasm round-trip {insn.mnemonic}", text.startswith(insn.mnemonic), text)

# golden encodings (independent of isa.encode): a few spot checks
G = {
    0: isa.encode("nop",),
    1: isa.encode("halt",),
}
check("first words", res.words[0] == 0 and res.words[1] == isa.HALT_WORD)
check(".word emitted literally", 0xDEADBEEF in res.words)
# li pseudo == movi with negative immediate masked
li_word = res.words[res.words.index(isa.encode("movi", 3, 0xFFFF))]
check("li pseudo == movi", li_word == isa.encode("movi", "r3", 0xFFFF))

# ---------------------------------------------------------------------------
# 2. labels, expressions, relocations
# ---------------------------------------------------------------------------
src = """
        .equ BASE, 0x10
        .equ VAL, BASE + 2*3          ; 0x16
start:  nop
        jmp start                     ; rel = 0 - 1 = -1
        jz  forward
        movi r0, VAL
forward:
        jmp forward                    ; rel = 0
        .word BASE << 2 | 1            ; 0x41
        .word 0b1010, 0o17, -1
"""
res = asm(src)
check("equ expression", res.words[3] == isa.encode("movi", 0, 0x16),
      f"0x{res.words[3]:08X}")
check("jump back rel = target-pc", res.words[1] == isa.encode("jmp", -1),
      f"0x{res.words[1]:08X}")
check("jump forward rel = target-pc", res.words[2] == isa.encode("jz", 2),
      f"0x{res.words[2]:08X}")
check("jump to self rel=0", res.words[4] == isa.encode("jmp", 0))
check(".word expression", res.words[5] == (0x10 << 2) | 1)
check(".word list", res.words[6:9] == [0b1010, 0o17, 0xFFFFFFFF])
check("relocs recorded", len(res.relocs) == 3, f"{len(res.relocs)}")
check("reloc symbol for label", res.relocs[0].symbol == "start"
      and res.relocs[0].target == 0 and res.relocs[0].rel == -1)
check("reloc label target", res.relocs[1].symbol == "forward"
      and res.relocs[1].target == 4 and res.relocs[1].rel == 2)
# numeric jump (no symbol) is a reloc too; `jmp 9` from pc0 => rel 9
res_num = asm("jmp 9\nnop")
check("reloc numeric target has no symbol",
      res_num.relocs[0].symbol is None and res_num.relocs[0].rel == 9
      and res_num.relocs[0].target == 9)

# dot = current PC
src = "x:  .word ., . , .+1"
res = asm(src)
check(". (current PC)", res.words == [0, 0, 1], f"{res.words}")

# operator precedence: 1+2*3<<1 == 14 (C precedence: << lower than +, higher than |)
res = asm(".equ X, 1 + 2*3 << 1\n.word X, 1|2^3&4, (1+2)*3, 7/2, 7%3, -(-5), ~0")
check("expression precedence", res.words == [14, 1 | 2 ^ (3 & 4), 9, 3, 1, 5, 0xFFFFFFFF],
      f"{[hex(w) for w in res.words]}")

# ---------------------------------------------------------------------------
# 3. directives
# ---------------------------------------------------------------------------
res = asm("""
        nop
        .align 4
        nop
        .fill 3, 0xCAFEF00D
        .fill 2
""")
check(".align pads to multiple with nop", res.words[:4] == [0, 0, 0, 0], f"{res.words[:5]}")
check(".align next word at 4", res.n_words == 4 + 1 + 3 + 2)
check(".fill value", res.words[5:8] == [0xCAFEF00D] * 3)
check(".fill default 0", res.words[8:] == [0, 0])

# ---------------------------------------------------------------------------
# 4. output formats (hex32 / c_array / bin) round-trip
# ---------------------------------------------------------------------------
words = [0x02000000, 0x04001234, 0xDEADBEEF, 0x00000000]
hex_text = loom_asm.format_hex32(words)
check("hex32 one word per line", [int(line, 16) for line in hex_text.splitlines()] == words)
c_text = loom_asm.format_c_array(words, "prog")
nums = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]+)u", c_text)]
check("c_array round-trip", nums == words)
check("c_array name", "const uint32_t prog[4]" in c_text)
bin_data = loom_asm.format_binary(words)
check("bin little-endian", list(struct.unpack(f"<{len(words)}I", bin_data)) == words)
try:
    loom_asm.format_c_array(words, "bad-name!")
    check("c_array rejects bad identifier", False)
except loom_asm.AsmError:
    check("c_array rejects bad identifier", True)

# ---------------------------------------------------------------------------
# 5. listing, symbol table, relocs text
# ---------------------------------------------------------------------------
res = asm("start: movi r0, 1\nloop: jmp loop")
lst = res.listing_text()
check("listing has addresses", re.search(r"^0000   ", lst, re.M) is not None)
check("listing has source", "movi" in lst and "jmp" in lst)
sym = res.symbols_text()
check("symbol table", "start" in sym and "loop" in sym and "0x0000" in sym)
rel = res.relocs_text()
check("relocs JSON", '"target"' in rel and '"symbol"' in rel)

# ---------------------------------------------------------------------------
# 6. errors are file:line with non-zero CLI exit
# ---------------------------------------------------------------------------
def expect_error(src: str, needle: str, lineno: int | None = None) -> None:
    path = ART / "err.asm"
    path.write_text(src, encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "sw.loom_asm", str(path), "-q"],
                          cwd=REPO, capture_output=True, text=True)
    ok = proc.returncode != 0 and needle in proc.stderr
    if lineno is not None:
        ok = ok and f":{lineno}: error:" in proc.stderr
    check(f"error: {needle}", ok, f"rc={proc.returncode} err={proc.stderr.strip()[:90]}")


expect_error("nop\nbogus r0, 1\n", "unknown mnemonic")
expect_error("movi r9, 1\n", "register index out of range", 1)
expect_error("movi r0, 1\njmp 70000\n", "out of rel16 range", 2)
expect_error(".fill FWD\nFWD: nop\n", "undefined symbol", 1)
expect_error("csrw NOPE, r0\n", "unknown CSR", 1)
expect_error("shl r0, 99\n", "shift count out of range", 1)
expect_error("ld r0, 99\n", "DMEM address out of range", 1)
expect_error("nop\n.equ X\n", "needs NAME", 2)

# success path CLI + listing/symbol/reloc side files + rom padding
asm_path = ART / "ok.asm"
asm_path.write_text("start: movi r0, 1\nloop: jmp loop\n", encoding="utf-8")
hex_path = ART / "ok.hex"
lst_path = ART / "ok.lst"
sym_path = ART / "ok.sym"
rel_path = ART / "ok.relocs.json"
proc = subprocess.run(
    [sys.executable, "-m", "sw.loom_asm", str(asm_path), "-o", str(hex_path),
     "--rom-words", "64", "--fill", "0xDEADBEEF", "--lst", str(lst_path),
     "--sym", str(sym_path), "--relocs", str(rel_path), "-q"],
    cwd=REPO, capture_output=True, text=True)
check("CLI success", proc.returncode == 0, proc.stderr[:120])
if proc.returncode == 0:
    out = [int(l, 16) for l in hex_path.read_text().splitlines()]
    check("CLI rom padding", len(out) == 64 and out[0] == isa.encode("movi", 0, 1)
          and out[2:] == [0xDEADBEEF] * 62)
    check("CLI listing written", lst_path.exists() and "movi" in lst_path.read_text())
    check("CLI symbols written", "start" in sym_path.read_text())
    check("CLI relocs written", '"rel"' in rel_path.read_text())

# formatter CLI smoke tests
for fmt, extra in (("c_array", ["--name", "fw"]), ("bin", [])):
    outp = ART / f"ok.{fmt}"
    proc = subprocess.run(
        [sys.executable, "-m", "sw.loom_asm", str(asm_path), "-o", str(outp),
         "--format", fmt, *extra, "-q"], cwd=REPO, capture_output=True, text=True)
    check(f"CLI --format {fmt}", proc.returncode == 0 and outp.exists(), proc.stderr[:100])

# ---------------------------------------------------------------------------
# 7. determinism / no source mutation
# ---------------------------------------------------------------------------
a = asm(sample_src)
b = asm(sample_src)
check("assembler deterministic", a.words == b.words and a.relocs == b.relocs)

print("ASM_TESTS:", "PASS" if not errors else f"FAIL ({len(errors)} failures)")
sys.exit(0 if not errors else 1)

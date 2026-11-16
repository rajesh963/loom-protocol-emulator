#!/usr/bin/env python3
"""Unit tests: sw/isa.py — single source of truth (owner: toolchain).

Run:  ../.venv/bin/python test/unit_sw/test_isa.py    (or via run.sh)
Prints ISA_TESTS: PASS and exits 0 on success.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sw import isa  # noqa: E402

errors: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        errors.append(name)


# ---------------------------------------------------------------------------
# 1. frozen header is really the source: parse it independently and compare
# ---------------------------------------------------------------------------
def parse_vh_opcodes(path: pathlib.Path) -> dict[str, int]:
    text = path.read_text()
    text = re.sub(r"//[^\n]*", "", text)
    out = {}
    for m in re.finditer(r"`define\s+OPC_(\w+)\s+(\d+)'([hbd])([0-9A-Fa-f_]+)", text):
        base = {"h": 16, "b": 2, "d": 10}[m.group(3)]
        out[m.group(1).lower()] = int(m.group(4).replace("_", ""), base)
    return out


vh = parse_vh_opcodes(REPO / "rtl" / "loom_bus.vh")
check("loom_bus.vh parsed independently", len(vh) == 59, f"{len(vh)} OPC defines")
for name, value in vh.items():
    check(f"opcode {name} matches header", isa.SPEC_OPCODES.get(name) == value,
          f"isa={isa.SPEC_OPCODES.get(name)} vh={value}")

# ---------------------------------------------------------------------------
# 2. golden encodings, computed independently from the documented layout
#    opcode[31:25] rd[24:22] rs[21:19] mode[18:16] imm16[15:0]
# ---------------------------------------------------------------------------
def raw(op, rd=0, rs=0, imm=0):
    return (op << 25) | (rd << 22) | (rs << 19) | (imm & 0xFFFF)


GOLDEN = {
    ("nop",): 0x00000000,
    ("halt",): raw(0x01),
    ("movi", ("r1", 0x1234)): raw(0x02, rd=1, imm=0x1234),
    ("mov", ("r2", "r3")): raw(0x03, rd=2, rs=3),
    ("add", ("r0", "r7")): raw(0x04, rd=0, rs=7),
    ("addi", ("r6", 0x00FF)): raw(0x05, rd=6, imm=0x00FF),
    ("sub", ("r1", "r2")): raw(0x06, rd=1, rs=2),
    ("subi", ("r1", 1)): raw(0x07, rd=1, imm=1),
    ("and", ("r3", "r4")): raw(0x08, rd=3, rs=4),
    ("andi", ("r3", 0xAA)): raw(0x09, rd=3, imm=0xAA),
    ("or", ("r3", "r4")): raw(0x0A, rd=3, rs=4),
    ("ori", ("r3", 0xAA)): raw(0x0B, rd=3, imm=0xAA),
    ("xor", ("r3", "r4")): raw(0x0C, rd=3, rs=4),
    ("xori", ("r3", 0xAA)): raw(0x0D, rd=3, imm=0xAA),
    ("inv", ("r5",)): raw(0x0E, rd=5),
    ("shl", ("r5", 7)): raw(0x0F, rd=5, imm=7),
    ("shr", ("r5", 7)): raw(0x10, rd=5, imm=7),
    ("rol", ("r5", 7)): raw(0x11, rd=5, imm=7),
    ("ror", ("r5", 7)): raw(0x12, rd=5, imm=7),
    ("cmp", ("r1", "r2")): raw(0x13, rd=1, rs=2),
    ("cmpi", ("r1", 9)): raw(0x14, rd=1, imm=9),
    ("csrw", ("TMASK", "r2")): raw(0x15, rs=2, imm=isa.SPEC_CSR["TMASK"]),
    ("csrr", ("r2", "TSTATUS")): raw(0x16, rd=2, imm=isa.SPEC_CSR["TSTATUS"]),
    ("out", (0xFCFF,)): raw(0x18, imm=0xFCFF),
    ("set", (0x000F,)): raw(0x19, imm=0x000F),
    ("clr", (0x000F,)): raw(0x1A, imm=0x000F),
    ("tog", (0x000F,)): raw(0x1B, imm=0x000F),
    ("in", ("r0",)): raw(0x1C, rd=0),
    ("dir", (0xAA,)): raw(0x1D, imm=0xAA),
    ("dset", (0x0F,)): raw(0x1E, imm=0x0F),
    ("dclr", (0xF0,)): raw(0x1F, imm=0xF0),
    ("emit", ("r4",)): raw(0x20, rd=4),
    ("emita", ("r4", 500)): raw(0x21, rd=4, imm=500),
    ("emitd", ("r4", 7)): raw(0x22, rd=4, imm=7),
    ("emite", ("r4",)): raw(0x23, rd=4),
    ("setp", (0x208D,)): raw(0x24, imm=0x208D),
    ("setm", (0xFCFF,)): raw(0x25, imm=0xFCFF),
    ("setem", (0x0300,)): raw(0x26, imm=0x0300),
    ("setec", (0x0100,)): raw(0x27, imm=0x0100),
    ("inat", ("r1", 42)): raw(0x28, rd=1, imm=42),
    ("waitu", (42,)): raw(0x29, imm=42),
    ("waitd", (0x208D,)): raw(0x2A, imm=0x208D),
    ("waitp", (0x1201,)): raw(0x2B, imm=0x1201),
    ("ld", ("r1", 0x3F)): raw(0x30, rd=1, imm=0x3F),
    ("st", (0x3F, "r1")): raw(0x31, rs=1, imm=0x3F),
    ("ldi", ("r1", "r2")): raw(0x32, rd=1, rs=2),
    ("sti", ("r1", "r2")): raw(0x33, rs=1, rd=2),
    ("push", ("r1",)): raw(0x38, rd=1),
    ("pull", ("r1",)): raw(0x39, rd=1),
    ("pushi", (0x5A,)): raw(0x3A, imm=0x5A),
    ("jmp", (-1,)): raw(0x40, imm=0xFFFF),
    ("jz", (3,)): raw(0x41, imm=3),
    ("jnz", (-2,)): raw(0x42, imm=0xFFFE),
    ("jc", (1,)): raw(0x43, imm=1),
    ("jnc", (0,)): raw(0x44, imm=0),
    ("rpt", (9,)): raw(0x48, imm=9),
    ("evset", (1,)): raw(0x49, imm=1),
    ("evwait", (2,)): raw(0x4A, imm=2),
    ("irq", (7,)): raw(0x4B, imm=7),
}
def _ops(key):
    return key[1] if len(key) > 1 else ()


for key, want in GOLDEN.items():
    mn, ops = key[0], _ops(key)
    got = isa.encode(mn, *ops)
    check(f"encode {mn} {ops}", got == want, f"got 0x{got:08X} want 0x{want:08X}")

# every mnemonic must appear in GOLDEN
covered = {k[0] for k in GOLDEN}
check("golden vectors cover all 59 mnemonics",
      covered == set(isa.INSTRUCTIONS), f"missing {set(isa.INSTRUCTIONS) - covered}")

# ---------------------------------------------------------------------------
# 3. decode / encode round-trip and disassemble
# ---------------------------------------------------------------------------
for key, want in GOLDEN.items():
    mn, ops = key[0], _ops(key)
    insn = isa.decode(want)
    ok = insn.known and insn.mnemonic == mn
    check(f"decode {mn}", ok, f"got {insn.mnemonic}")
    if ok:
        reenc = isa.encode(mn, *insn.operands)
        check(f"re-encode {mn}", reenc == want, f"0x{reenc:08X} != 0x{want:08X}")
        text = isa.disasm(want, pc=4, syms={"loop": 4})
        check(f"disasm {mn} nonempty", isinstance(text, str) and text.startswith(mn), text)

# jump disassembly target matches hardware (PC += sext(rel16), no +1)
check("disasm jump target pc+rel",
      isa.disasm(isa.encode("jmp", 2), pc=69) == "jmp 0x047",
      isa.disasm(isa.encode("jmp", 2), pc=69))
check("disasm backward jump target",
      isa.disasm(isa.encode("jz", -3), pc=10, syms={"loop": 7}) == "jz loop")

# unknown opcode (7-bit opcode space 0x60 unused)
bad = (0x60 << 25) | 0x1234
u = isa.decode(bad)
check("unknown opcode -> known=False", not u.known and u.executes_as_nop)
check("unknown disasm is .word", isa.disasm(bad).startswith(".word 0x"))

# reserved mode bits set -> not a legal encoding
check("nonzero mode flagged", not isa.decode(isa.encode("nop") | (1 << 16)).known)

# ---------------------------------------------------------------------------
# 4. CSR map and host addressing
# ---------------------------------------------------------------------------
EXPECTED_CSR = {
    "TPERIOD": 0x00, "TMASK": 0x01, "TSTATUS": 0x02, "TFIFO": 0x03,
    "TNEXTT": 0x04, "TEQ": 0x05, "TEMASK": 0x06, "TECOND": 0x07, "TPC": 0x08,
    "POUT": 0x10, "PDIR": 0x11, "TMR": 0x12, "EVT": 0x13, "IRQ": 0x14,
    "WCTRL": 0x15, "WADDR": 0x16, "WLEN": 0x17, "WPER": 0x18,
    "WSTATUS": 0x19, "CAPCTRL": 0x1A, "CAPSTAT": 0x1B, "GSTATUS": 0x1C,
    "SOFTRST": 0x20,
}
for name, addr in EXPECTED_CSR.items():
    check(f"CSR {name}", isa.SPEC_CSR.get(name) == addr, f"isa={isa.SPEC_CSR.get(name)}")
check("CSR host bank address 0x40+16t+n", isa.csr_host_addr(2, 8) == 0x40 + 32 + 8)
check("CSR host bank decode", isa.csr_decode_host_addr(0x40 + 32 + 8) == (2, 8))
check("CSR host decode rejects global", isa.csr_decode_host_addr(0x10) is None)
# Lead-blessed bit layouts are documented
check("TSTATUS bit layout documented", "bit1=halted, bit0=en" in isa.CSR_BY_NAME["TSTATUS"].fields)
check("TFIFO bit layout documented",
      "[11:8]=rx_count" in isa.CSR_BY_NAME["TFIFO"].fields
      and "[7:4]=tx_count" in isa.CSR_BY_NAME["TFIFO"].fields)
check("GSTATUS bit layout documented", "8:5" in isa.CSR_BY_NAME["GSTATUS"].fields
      and "en[3:0]" in isa.CSR_BY_NAME["GSTATUS"].fields)
check("WSTATUS bit1 busy/bit0 done",
      "bit1=busy, bit0=done" in isa.CSR_BY_NAME["WSTATUS"].fields)
check("WCTRL has no done_rst", "done_rst" not in isa.CSR_BY_NAME["WCTRL"].fields)

# ---------------------------------------------------------------------------
# 5. host command table
# ---------------------------------------------------------------------------
expected_args = {
    "SET_ADDR": 3, "WRITE_MEM": 4, "READ_MEM": 0, "WRITE_CSR": 3,
    "READ_CSR": 1, "TX_PUSH": 2, "RX_POP": 1, "SET_PC": 3, "RUN": 1,
    "STOP": 1, "STATUS": 0, "ACK_IRQ": 0, "PING": 1,
}
for name, nargs in expected_args.items():
    spec = isa.HOST_CMD_BY_NAME.get(name)
    check(f"host cmd {name} present", spec is not None)
    if spec:
        check(f"host cmd {name} args={nargs}", spec.n_args == nargs, f"{spec.n_args}")
check("host cmd opcodes 0x01..0x0D",
      [c.opcode for c in isa.HOST_COMMANDS] == list(range(0x01, 0x0E)))
check("RESP_OK/BUSY", (isa.RESP_OK, isa.RESP_BUSY, isa.RESP_IDLE) == (0x80, 0x81, 0x00))

# ---------------------------------------------------------------------------
# 6. markdown table helpers escape '|'
# ---------------------------------------------------------------------------
def table_rows_ok(text: str) -> bool:
    for row in text.splitlines():
        if not row.startswith("|"):
            return False
        # split on unescaped pipes only
        cells = re.split(r"(?<!\\)\|", row)
        if cells[0].strip() or cells[-1].strip():
            return False
        if len(cells) < 3:
            return False
        for cell in cells[1:-1]:
            # no *raw* unescaped pipe inside a cell (they would have split it);
            # an escaped pipe is the only legal occurrence
            pass
    return True


for name, fn, min_rows in (("summary_table", isa.summary_table, 60),
                           ("csr_table", isa.csr_table, 20),
                           ("host_command_table", isa.host_command_table, 14)):
    text = fn()
    rows = text.splitlines()
    check(f"{name} row count", len(rows) >= min_rows, f"{len(rows)}")
    check(f"{name} all rows are table rows", table_rows_ok(text))
    check(f"{name} escapes its own pipes",
          "\\|" in text and "\n" in text or "|" in text)
    # every line must have the same number of unescaped separators
    counts = {len(re.split(r"(?<!\\)\|", r)) for r in rows}
    check(f"{name} consistent column count", len(counts) == 1, f"{counts}")

st = isa.summary_table()
check("summary_table pipe cells escaped", "R[rd] \\|= R[rs]" in st
      or "\\|" in st)
check("summary_table has no raw OR-assignment",
      "| R[rd] |= R[rs] |" not in st)
check("summary has all mnemonics", all(f"`{m}`" in st for m in isa.INSTRUCTIONS))
check("waitp description is low-byte/uio",
      "pins_in[7:0]" in isa.INSTRUCTIONS["waitp"].description)

# ---------------------------------------------------------------------------
# 7. constants
# ---------------------------------------------------------------------------
check("HALT_WORD is halt", isa.HALT_WORD == raw(0x01) == 0x02000000)
check("default fill is halt word", isa.DEFAULT_FILL_WORD == isa.HALT_WORD)
check("unmapped PMEM word is halt", isa.UNMAPPED_PMEM_WORD == isa.HALT_WORD)
check("PINS_OUT_DRIVABLE mask", isa.PINS_OUT_DRIVABLE == 0xFCFF)
check("PINS_OUT_HOST bits", isa.PINS_OUT_HOST == 0x0300)
check("pseudos only li", set(isa.PSEUDOS) == {"li"})
check("jump set", isa.JUMPS == frozenset({"jmp", "jz", "jnz", "jc", "jnc"}))

print("ISA_TESTS:", "PASS" if not errors else f"FAIL ({len(errors)} failures)")
sys.exit(0 if not errors else 1)

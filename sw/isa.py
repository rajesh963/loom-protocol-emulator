#!/usr/bin/env python3
"""LOOM ISA — the single source of truth (owner: toolchain).

Everything that encodes or decodes a LOOM instruction, names a CSR or a host
command lives here.  The RTL, the assembler, the golden model, the host client,
the random program generator and the generated docs all import this module, so
a field cannot drift between the two independent front-ends.

Constants are *read textually from `rtl/loom_bus.vh`* (which the spec declares
FROZEN) at import time; if a name is missing from the header the embedded
default below is used instead.  This means `rtl/loom_bus.vh` is authoritative and
`sw/isa.py` can never silently disagree with it.

Instruction word layout (spec §4, uniform for all instructions):

      31      25 24  22 21  19 18  16 15              0
    +----------+------+------+------+----------------+
    |  opcode  |  rd  |  rs  | mode |      imm16     |
    +----------+------+------+------+----------------+

**IMPORTANT FIELD-WIDTH CORRECTION (spec bug; flagged to Lead 2026-11-16).**
Spec §4 prints the diagram as `opcode[31:26]` (6 bits), `rd[25:23]`,
`rs[22:20]`, `mode[19:16]`, `imm16[15:0]`, and `rtl/loom_bus.vh` writes
`` `define OPC_JMP 6'h40 ``.  That is impossible: opcodes 0x40..0x4B need 7
bits, so `6'h40` truncates to `0x00` and collides with `nop` (likewise 0x41..0x4B
alias 0x01..0x0B).  A 6+3+3+4+16 layout also sums to 32 only by construction,
which leaves nowhere to put the missing bit.  The only self-consistent reading
that fits every listed opcode value in 32 bits is

    opcode[31:25] (7 bits) | rd[24:22] | rs[21:19] | mode[18:16] (3 bits) | imm16[15:0]

i.e. the opcode field starts one bit lower and `mode` is 3 reserved bits.  This
module uses that layout (constants `OPCODE_SHIFT`, `RD_SHIFT`, ... below), and
`samples`/tests pin it.  The frozen header needs `7'h` on every `OPC_*` define
plus a corrected diagram before RTL decoders are written; the Lead owns that
change (toolchain cannot edit `rtl/`).

`rd`/`rs` are 3-bit register indices (0..7), `mode` is 3 reserved bits and is
zero in every instruction of ISA v1.0, `imm16` is a 16-bit immediate whose
interpretation depends on the mnemonic (see `InsnSpec.operands`).

Operand descriptions (`kind` values used in `InsnSpec.operands`)
---------------------------------------------------------------
  reg     : 3-bit register index, syntax `r0`..`r7`
  imm     : unsigned 16-bit immediate
  imm8    : low 8 bits of imm16 are meaningful (0..255)
  imm4    : low 4 bits of imm16 are meaningful (0..15, shift counts)
  csr     : CSR address in imm16[7:0]; accepts a CSR name (e.g. `TSTATUS`)
  addr6   : 6-bit DMEM word address in imm16[5:0] (0..63)
  rel16   : signed 16-bit *word* offset in imm16.  The assembler accepts a label
            or an absolute target address and computes the offset; `encode()`
            takes the already-computed relative value.
  abs16   : absolute 16-bit cycle count / value in imm16 (emita/inat/waitu
            targets may be labels or expressions in the assembler)
  waitp   : imm16[15:8] = input mask, imm16[7:0] = expected value
  rpt     : imm16[7:0] = repeat count n; the following instruction executes n+1
            times

Anything not implemented hardware-wise is architecturally `nop` (spec §4.1);
`decode()` reports unknown opcodes with `known=False` and `disasm()` prints them
as `.word 0x........` so a disassembly never invents a mnemonic.

Public API
----------
  SPEC_OPCODES            dict mnemonic -> 6-bit opcode
  SPEC_CSR                dict CSR name -> 8-bit address
  SPEC_HCMDS              dict host-command name -> 8-bit opcode
  INSTRUCTIONS            dict mnemonic -> InsnSpec (operand layout)
  CSR_SPECS               ordered list of CsrSpec
  HOST_COMMANDS           ordered list of HostCmdSpec
  encode(mnemonic, *ops)  -> 32-bit word (ops are ints or `rN`/CSR-name strings)
  decode(word)            -> Insn
  disasm(word, pc=None, syms=None) -> str
  summary_table()         markdown rows for docs/isa.md
  csr_table(), host_command_table()  more markdown rows
  csr_host_addr(tid, n)   host-side address of thread `tid` CSR bank entry `n`
  csr_decode_host_addr(a) -> (tid, n) or None

Semantic decisions made where the spec is ambiguous
---------------------------------------------------
  * `mode` is 3 reserved bits and must be 0.  decode() preserves it but no
    instruction consumes it.
  * A CSR name is resolved case-insensitively in assembler operands; `sw/isa.py`
    itself exposes canonical upper-case names.
  * `st` operand order is (address, source register) and `sti` is (source
    register, address register) exactly as printed in spec §4.1, even though
    `ld`/`sti` read "backwards" relative to each other.
  * Bits 9:8 of `pins_out` are the host-link pins (`PINS_OUT_HOST = 0x0300`):
    they are not connected to physical protocol pins and are **masked to 0 on
    every write path** (`pout &= PINS_OUT_DRIVABLE = 0xFCFF`, Lead decision).
    `PINS_OUT_DRIVABLE` is the set of bits that reach the pin map (14 protocol
    outputs).  Out/set/clr/tog, CSR POUT, event merges and the wave player all
    respect the mask.
  * The IRQ CSR read is **non-destructive** (RTL); the host clears the IRQ
    latch with ACK_IRQ, i.e. writing CSR_IRQ (0x14) = 0.  Spec §4.2's
    "(read clears)" is superseded (Lead decision).
  * Spec §5's literal "unmapped PMEM reads 0x00000001 (halt)" predates the
    7-bit opcode fix: 0x00000001 decodes as `nop`.  Lead decision (2026-11-16):
    unmapped PMEM reads AND the assembler's default fill are `HALT_WORD` =
    0x02000000, so a runaway PC stops.  RTL must match.
"""

from __future__ import annotations

import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

__all__ = [
    "REPO_ROOT", "BUS_VH_PATH", "CSR_RUN", "CSR_STOP",
    "SPEC_OPCODES", "SPEC_CSR", "SPEC_HCMDS", "PARAMS",
    "INSTRUCTIONS", "InsnSpec", "Insn", "CsrSpec", "HostCmdSpec",
    "CSR_SPECS", "HOST_COMMANDS", "PSEUDOS",
    "encode", "encode_raw", "encode_insn", "decode", "disasm",
    "summary_table", "csr_table", "host_command_table",
    "csr_host_addr", "csr_decode_host_addr", "csr_name", "csr_addr",
    "parse_reg", "is_jump", "JUMPS", "operand_specs",
    "PINS_OUT_HOST", "PINS_OUT_DRIVABLE", "PINS_IN_HOST", "DIR_MASK",
    "HALT_WORD", "NOP_WORD", "DEFAULT_FILL_WORD", "UNMAPPED_PMEM_WORD",
]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BUS_VH_PATH = REPO_ROOT / "rtl" / "loom_bus.vh"

# ---------------------------------------------------------------------------
# Constants that the spec defines but loom_bus.vh does not spell out
# ---------------------------------------------------------------------------
PINS_OUT_HOST = 0x0300          # pins_out[9:8] are host bridge bits

# Instruction field positions.  See the FIELD-WIDTH CORRECTION note in the
# module docstring: opcode is 7 bits (31:25) so that 0x40..0x4B fit.
OPCODE_SHIFT = 25
OPCODE_MASK = 0x7F
RD_SHIFT = 22
RS_SHIFT = 19
MODE_SHIFT = 16
MODE_MASK = 0x7
IMM16_MASK = 0xFFFF
PINS_OUT_DRIVABLE = 0xFFFF & ~PINS_OUT_HOST  # 0xFCFF (14 protocol pins)
PINS_IN_HOST = 0x0700           # pins_in[10:8] are host SPI pins (readable)
DIR_MASK = 0xFF                 # DIR is uio_oe[7:0]
HALT_WORD = 0x01 << OPCODE_SHIFT     # 0x02000000 — encodes `halt`
NOP_WORD = 0x00
#: Word returned for addresses >= PMEM_WORDS and the assembler's default ROM fill.
#: Lead decision 2026-11-16: use the real `halt` encoding (spec §5's literal
#: 0x00000001 predates the 7-bit opcode fix and decodes as `nop`, so a runaway PC
#: would not stop).  `rtl/loom_mem.v` returns the same word.
UNMAPPED_PMEM_WORD = HALT_WORD
DEFAULT_FILL_WORD = UNMAPPED_PMEM_WORD

# ---------------------------------------------------------------------------
# Embedded defaults (spec §4.1 / §4.2 / §6).  Used only where loom_bus.vh has
# no definition; the .vh always wins.
# ---------------------------------------------------------------------------
_DEFAULT_OPCODES: dict[str, int] = {
    "nop": 0x00, "halt": 0x01, "movi": 0x02, "mov": 0x03,
    "add": 0x04, "addi": 0x05, "sub": 0x06, "subi": 0x07,
    "and": 0x08, "andi": 0x09, "or": 0x0A, "ori": 0x0B,
    "xor": 0x0C, "xori": 0x0D, "inv": 0x0E,
    "shl": 0x0F, "shr": 0x10, "rol": 0x11, "ror": 0x12,
    "cmp": 0x13, "cmpi": 0x14,
    "csrw": 0x15, "csrr": 0x16,
    "out": 0x18, "set": 0x19, "clr": 0x1A, "tog": 0x1B,
    "in": 0x1C, "dir": 0x1D, "dset": 0x1E, "dclr": 0x1F,
    "emit": 0x20, "emita": 0x21, "emitd": 0x22, "emite": 0x23,
    "setp": 0x24, "setm": 0x25, "setem": 0x26, "setec": 0x27,
    "inat": 0x28, "waitu": 0x29, "waitd": 0x2A, "waitp": 0x2B,
    "ld": 0x30, "st": 0x31, "ldi": 0x32, "sti": 0x33,
    "push": 0x38, "pull": 0x39, "pushi": 0x3A,
    "jmp": 0x40, "jz": 0x41, "jnz": 0x42, "jc": 0x43, "jnc": 0x44,
    "rpt": 0x48, "evset": 0x49, "evwait": 0x4A, "irq": 0x4B,
}

_DEFAULT_CSRS: dict[str, int] = {
    "TPERIOD": 0x00, "TMASK": 0x01, "TSTATUS": 0x02, "TFIFO": 0x03,
    "TNEXTT": 0x04, "TEQ": 0x05, "TEMASK": 0x06, "TECOND": 0x07,
    "TPC": 0x08,
    "POUT": 0x10, "PDIR": 0x11, "TMR": 0x12, "EVT": 0x13, "IRQ": 0x14,
    "WCTRL": 0x15, "WADDR": 0x16, "WLEN": 0x17, "WPER": 0x18,
    "WSTATUS": 0x19, "CAPCTRL": 0x1A, "CAPSTAT": 0x1B, "GSTATUS": 0x1C,
    "SOFTRST": 0x20,
}

_DEFAULT_HCMDS: dict[str, int] = {
    "SET_ADDR": 0x01, "WRITE_MEM": 0x02, "READ_MEM": 0x03,
    "WRITE_CSR": 0x04, "READ_CSR": 0x05, "TX_PUSH": 0x06,
    "RX_POP": 0x07, "SET_PC": 0x08, "RUN": 0x09, "STOP": 0x0A,
    "STATUS": 0x0B, "ACK_IRQ": 0x0C, "PING": 0x0D,
    "RESP_OK": 0x80, "RESP_BUSY": 0x81,
}

_DEFAULT_PARAMS: dict[str, int] = {
    "NUM_THREADS": 4, "EQ_DEPTH": 4, "FIFO_DEPTH": 8, "FIFO_AW": 3,
    "DMEM_WORDS": 64, "DMEM_AW": 6, "PMEM_AW": 10, "INSN_W": 32,
}

# ---------------------------------------------------------------------------
# `rtl/loom_bus.vh` textual parser
# ---------------------------------------------------------------------------
_DEFINE_RE = re.compile(
    r"^\s*`define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+(?P<value>[^\s/]+))?",
    re.MULTILINE,
)
_NUMBER_RE = re.compile(r"^(?:(?P<size>\d+)'[sS]?(?P<base>[bdhoBDHO]))?(?P<digits>[0-9A-Fa-f_]+)$")


def _parse_verilog_int(token: str) -> int | None:
    """Parse `6'h0A`, `8'd12`, `4'b1010`, `1024`, `16'ha_b` etc."""
    m = _NUMBER_RE.match(token.strip())
    if not m:
        return None
    base = (m.group("base") or "d").lower()
    digits = (m.group("digits") or "").replace("_", "")
    if not digits:
        return None
    try:
        return int(digits, {"b": 2, "d": 10, "h": 16, "o": 8}[base])
    except ValueError:
        return None


def _parse_bus_vh(path: pathlib.Path) -> dict[str, dict[str, int]]:
    """Return {'opcodes','csrs','hcmds','params'} parsed from the frozen header.

    Missing file => empty groups (callers fall back to the embedded defaults).
    Unknown/malformed defines are ignored, never raised: this module must import
    even in a tree where the RTL has not been dropped yet.
    """
    groups: dict[str, dict[str, int]] = {
        "opcodes": {}, "csrs": {}, "hcmds": {}, "params": {},
    }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return groups
    # Strip // line comments so commented-out defines are never picked up.
    text = re.sub(r"//[^\n]*", "", text)
    for m in _DEFINE_RE.finditer(text):
        name = m.group("name")
        raw = m.group("value")
        if raw is None:
            continue
        value = _parse_verilog_int(raw)
        if value is None:
            # A define that is not a plain number (e.g. a string) is not ours.
            continue
        if name.startswith("OPC_"):
            groups["opcodes"][name[4:].lower()] = value
        elif name.startswith("CSR_"):
            groups["csrs"][name[4:]] = value
        elif name.startswith("HCMD_"):
            groups["hcmds"][name[5:]] = value
        else:
            groups["params"][name] = value
    return groups


_VH = _parse_bus_vh(BUS_VH_PATH)
BUS_VH_FOUND = bool(_VH["opcodes"] or _VH["csrs"] or _VH["hcmds"])


def _merge_group(defaults: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    out = dict(defaults)          # embedded defaults first
    out.update(parsed)            # frozen header wins
    return out


#: mnemonic -> 6-bit opcode (header-derived, defaults fill gaps)
SPEC_OPCODES: dict[str, int] = _merge_group(_DEFAULT_OPCODES, _VH["opcodes"])
#: CSR name -> 8-bit address
SPEC_CSR: dict[str, int] = _merge_group(_DEFAULT_CSRS, _VH["csrs"])
#: host command name -> 8-bit opcode
SPEC_HCMDS: dict[str, int] = _merge_group(_DEFAULT_HCMDS, _VH["hcmds"])
#: build-time parameters from the header (`define NUM_THREADS 4` ...)
PARAMS: dict[str, int] = _merge_group(_DEFAULT_PARAMS, _VH["params"])


# ---------------------------------------------------------------------------
# Instruction table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InsnSpec:
    """Static description of one mnemonic: opcode plus operand layout."""

    mnemonic: str
    opcode: int
    operands: tuple[tuple[str, str], ...] = ()
    description: str = ""
    flags: str = ""            # human-readable flag/effect note for docs

    @property
    def operand_kinds(self) -> tuple[str, ...]:
        return tuple(kind for _field, kind in self.operands)

    @property
    def n_operands(self) -> int:
        return len(self.operands)


# operand kind tags
REG = "reg"
IMM = "imm"
IMM8 = "imm8"
IMM4 = "imm4"
CSR = "csr"
ADDR6 = "addr6"
REL16 = "rel16"
ABS16 = "abs16"
WAITP = "waitp"
RPT = "rpt"

_R = ((("rd"), REG),)
_RR = (("rd", REG), ("rs", REG))

_INSN_TABLE: tuple[InsnSpec, ...] = (
    InsnSpec("nop", 0x00, (), "no operation"),
    InsnSpec("halt", 0x01, (), "halt this thread (halt=1, en unchanged; PC frozen)"),
    InsnSpec("movi", 0x02, (("rd", REG), ("imm16", IMM)), "R[rd] = imm16", "Z; C=0"),
    InsnSpec("mov", 0x03, _RR, "R[rd] = R[rs]", "Z; C=0"),
    InsnSpec("add", 0x04, _RR, "R[rd] += R[rs]", "Z,C"),
    InsnSpec("addi", 0x05, (("rd", REG), ("imm16", IMM)), "R[rd] += imm16", "Z,C"),
    InsnSpec("sub", 0x06, _RR, "R[rd] -= R[rs]", "Z,C"),
    InsnSpec("subi", 0x07, (("rd", REG), ("imm16", IMM)), "R[rd] -= imm16", "Z,C"),
    InsnSpec("and", 0x08, _RR, "R[rd] &= R[rs]", "Z; C=0"),
    InsnSpec("andi", 0x09, (("rd", REG), ("imm16", IMM)), "R[rd] &= imm16", "Z; C=0"),
    InsnSpec("or", 0x0A, _RR, "R[rd] |= R[rs]", "Z; C=0"),
    InsnSpec("ori", 0x0B, (("rd", REG), ("imm16", IMM)), "R[rd] |= imm16", "Z; C=0"),
    InsnSpec("xor", 0x0C, _RR, "R[rd] ^= R[rs]", "Z; C=0"),
    InsnSpec("xori", 0x0D, (("rd", REG), ("imm16", IMM)), "R[rd] ^= imm16", "Z; C=0"),
    InsnSpec("inv", 0x0E, _R, "R[rd] = ~R[rd]", "Z; C=0"),
    InsnSpec("shl", 0x0F, (("rd", REG), ("imm16", IMM4)), "R[rd] <<= n (n=imm16[3:0])", "Z; C=0"),
    InsnSpec("shr", 0x10, (("rd", REG), ("imm16", IMM4)), "R[rd] >>= n (logical)", "Z; C=0"),
    InsnSpec("rol", 0x11, (("rd", REG), ("imm16", IMM4)), "R[rd] = rol16(R[rd], n)"),
    InsnSpec("ror", 0x12, (("rd", REG), ("imm16", IMM4)), "R[rd] = ror16(R[rd], n)"),
    InsnSpec("cmp", 0x13, _RR, "flags from R[rd] - R[rs]", "Z,C"),
    InsnSpec("cmpi", 0x14, (("rd", REG), ("imm16", IMM)), "flags from R[rd] - imm16", "Z,C"),
    InsnSpec("csrw", 0x15, (("imm16", CSR), ("rs", REG)), "CSR[imm16[7:0]] = R[rs] (thread bank if < 0x10)"),
    InsnSpec("csrr", 0x16, (("rd", REG), ("imm16", CSR)), "R[rd] = CSR[imm16[7:0]]"),
    InsnSpec("out", 0x18, (("imm16", IMM),), "pins_out = imm16 & 0xFCFF (host-link bits 9:8 masked)"),
    InsnSpec("set", 0x19, (("imm16", IMM),), "pins_out |= imm16"),
    InsnSpec("clr", 0x1A, (("imm16", IMM),), "pins_out &= ~imm16"),
    InsnSpec("tog", 0x1B, (("imm16", IMM),), "pins_out ^= imm16"),
    InsnSpec("in", 0x1C, _R, "R[rd] = pins_in (combinational read)"),
    InsnSpec("dir", 0x1D, (("imm16", IMM8),), "DIR = imm16[7:0] (uio_oe)"),
    InsnSpec("dset", 0x1E, (("imm16", IMM8),), "DIR |= imm16[7:0]"),
    InsnSpec("dclr", 0x1F, (("imm16", IMM8),), "DIR &= ~imm16[7:0]"),
    InsnSpec("emit", 0x20, _R, "enqueue timed event at NEXTT+PERIOD (frac accum), val=R[rd]; retry if queue full"),
    InsnSpec("emita", 0x21, (("rd", REG), ("imm16", ABS16)), "enqueue event at absolute TMR==imm16, val=R[rd]"),
    InsnSpec("emitd", 0x22, (("rd", REG), ("imm16", IMM)), "enqueue event at TMR+imm16, val=R[rd]"),
    InsnSpec("emite", 0x23, _R, "enqueue edge event, val=R[rd]; fires when (pins_in&EMASK)==(ECOND&EMASK)"),
    InsnSpec("setp", 0x24, (("imm16", IMM),), "PERIOD = imm16 (12.4 fixed point)"),
    InsnSpec("setm", 0x25, (("imm16", IMM),), "TMASK = imm16"),
    InsnSpec("setem", 0x26, (("imm16", IMM),), "EMASK = imm16 (edge kind only)"),
    InsnSpec("setec", 0x27, (("imm16", IMM),), "ECOND = imm16"),
    InsnSpec("inat", 0x28, (("rd", REG), ("imm16", ABS16)), "stall until TMR==imm16, latch pins_in that cycle, R[rd]=latch"),
    InsnSpec("waitu", 0x29, (("imm16", ABS16),), "stall until TMR==imm16"),
    InsnSpec("waitd", 0x2A, (("imm16", IMM),), "stall imm16 (12.4) cycles, drift-free fractional accumulation"),
    InsnSpec("waitp", 0x2B, (("imm16", WAITP),), "retry until (pins_in[7:0] & imm16[15:8]) == imm16[7:0] (uio pins only)"),
    InsnSpec("ld", 0x30, (("rd", REG), ("imm16", ADDR6)), "R[rd] = DMEM[imm16[5:0]] (single-cycle, combinational read)"),
    InsnSpec("st", 0x31, (("imm16", ADDR6), ("rs", REG)), "DMEM[imm16[5:0]] = R[rs] (single-cycle, write commits end of cycle)"),
    InsnSpec("ldi", 0x32, _RR, "R[rd] = DMEM[R[rs][5:0]] (indirect, single-cycle)"),
    InsnSpec("sti", 0x33, (("rs", REG), ("rd", REG)), "DMEM[R[rd][5:0]] = R[rs] (indirect, single-cycle)"),
    InsnSpec("push", 0x38, _R, "TXFIFO <<= R[rd][7:0]; retry if full"),
    InsnSpec("pull", 0x39, _R, "R[rd] = RXFIFO (zero-extended); retry if empty"),
    InsnSpec("pushi", 0x3A, (("imm16", IMM8),), "TXFIFO <<= imm16[7:0]"),
    InsnSpec("jmp", 0x40, (("imm16", REL16),), "PC += sext(imm16) (word units)"),
    InsnSpec("jz", 0x41, (("imm16", REL16),), "if Z: PC += sext(imm16)"),
    InsnSpec("jnz", 0x42, (("imm16", REL16),), "if !Z: PC += sext(imm16)"),
    InsnSpec("jc", 0x43, (("imm16", REL16),), "if C: PC += sext(imm16)"),
    InsnSpec("jnc", 0x44, (("imm16", REL16),), "if !C: PC += sext(imm16)"),
    InsnSpec("rpt", 0x48, (("imm16", RPT),), "repeat the following instruction n+1 times (imm16[7:0])"),
    InsnSpec("evset", 0x49, (("imm16", IMM8),), "set event flag (bit imm16[2:0])"),
    InsnSpec("evwait", 0x4A, (("imm16", IMM8),), "retry until event flag set, then clear it"),
    InsnSpec("irq", 0x4B, (("imm16", IMM8),), "raise host IRQ with code imm16[7:0]"),
)

#: canonical mnemonic -> InsnSpec (opcodes patched in from SPEC_OPCODES below)
INSTRUCTIONS: dict[str, InsnSpec] = {
    s.mnemonic: InsnSpec(s.mnemonic, SPEC_OPCODES.get(s.mnemonic, s.opcode),
                         s.operands, s.description, s.flags)
    for s in _INSN_TABLE
}

#: opcode -> canonical mnemonic (first table entry wins on duplicate)
_OPCODE_TO_MNEMONIC: dict[int, str] = {}
for _s in INSTRUCTIONS.values():
    _OPCODE_TO_MNEMONIC.setdefault(_s.opcode, _s.mnemonic)

#: assembler-only pseudos (documented in sw/loom_asm.py's header too)
PSEUDOS: dict[str, tuple[str, str]] = {
    "li": ("movi", "load immediate: `li rd, imm` == `movi rd, imm`"),
}

#: jump mnemonics whose operand is a rel16 word offset
JUMPS: frozenset[str] = frozenset({"jmp", "jz", "jnz", "jc", "jnc"})

#: mnemonics that write pins_out directly
PIN_WRITE_OPS: frozenset[str] = frozenset({"out", "set", "clr", "tog"})


def is_jump(mnemonic: str) -> bool:
    return mnemonic.lower() in JUMPS


def operand_specs(mnemonic: str) -> tuple[tuple[str, str], ...]:
    """Return the (field, kind) tuple for `mnemonic` (pseudo-resolved)."""
    return _resolve_spec(mnemonic).operands


def _resolve_spec(mnemonic: str) -> InsnSpec:
    m = mnemonic.lower()
    if m in PSEUDOS:
        m = PSEUDOS[m][0]
    try:
        return INSTRUCTIONS[m]
    except KeyError:
        raise ValueError(f"unknown mnemonic {mnemonic!r}") from None


# ---------------------------------------------------------------------------
# Encoding / decoding
# ---------------------------------------------------------------------------
def parse_reg(token: Any) -> int:
    """Accept 0..7 or 'rN'/'RN' and return the register index."""
    if isinstance(token, int):
        idx = token
    else:
        text = str(token).strip().lower()
        if text.startswith("r"):
            text = text[1:]
        try:
            idx = int(text, 0)
        except ValueError:
            raise ValueError(f"bad register {token!r} (expected r0..r7)") from None
    if not 0 <= idx <= 7:
        raise ValueError(f"register index out of range: {idx} (0..7)")
    return idx


def csr_addr(token: Any) -> int:
    """Resolve a CSR name (case-insensitive) or number to an 8-bit address."""
    if isinstance(token, int):
        addr = token
    else:
        text = str(token).strip()
        upper = text.upper()
        if upper in SPEC_CSR:
            return SPEC_CSR[upper]
        try:
            addr = int(text, 0)
        except ValueError:
            raise ValueError(f"unknown CSR {token!r}") from None
    if not 0 <= addr <= 0xFF:
        raise ValueError(f"CSR address out of range: {addr} (0..255)")
    return addr


def csr_name(addr: int) -> str | None:
    """Canonical name for a CSR address, or None."""
    for name, a in SPEC_CSR.items():
        if a == addr:
            return name
    return None


def _check_value(kind: str, value: int) -> int:
    """Range-check an operand value for its kind, returning the encoded value."""
    if kind in (REG,):
        if not 0 <= value <= 7:
            raise ValueError(f"register index out of range: {value}")
        return value
    if kind == IMM:
        if not -0x8000 <= value <= 0xFFFF:
            raise ValueError(f"immediate out of range: {value} (want -32768..65535)")
        return value & 0xFFFF
    if kind in (IMM8, RPT):
        if not -0x80 <= value <= 0xFF:
            raise ValueError(f"imm8 out of range: {value} (want -128..255)")
        return value & 0xFF
    if kind == IMM4:
        if not 0 <= value <= 15:
            raise ValueError(f"imm4 out of range: {value} (want 0..15)")
        return value
    if kind == CSR:
        return csr_addr(value)
    if kind == ADDR6:
        if not 0 <= value <= 63:
            raise ValueError(f"DMEM address out of range: {value} (want 0..63)")
        return value
    if kind in (REL16, ABS16):
        if kind == REL16:
            if not -0x8000 <= value <= 0x7FFF:
                raise ValueError(f"rel16 out of range: {value} (want -32768..32767)")
        else:
            if not -0x8000 <= value <= 0xFFFF:
                raise ValueError(f"abs16 out of range: {value} (want -32768..65535)")
        return value & 0xFFFF
    if kind == WAITP:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"waitp operand out of range: {value} (want 0..65535)")
        return value & 0xFFFF
    raise AssertionError(f"unhandled operand kind {kind!r}")


def encode_raw(opcode: int, rd: int = 0, rs: int = 0, mode: int = 0, imm16: int = 0) -> int:
    """Assemble a raw instruction word from already-decoded fields."""
    if not 0 <= opcode <= OPCODE_MASK:
        raise ValueError(f"opcode out of range: {opcode} (0..{OPCODE_MASK})")
    for name, value, limit in (("rd", rd, 7), ("rs", rs, 7)):
        if not 0 <= value <= limit:
            raise ValueError(f"{name} out of range: {value}")
    if not 0 <= mode <= MODE_MASK:
        raise ValueError(f"mode out of range: {mode} (0..{MODE_MASK})")
    if not 0 <= imm16 <= IMM16_MASK:
        raise ValueError(f"imm16 out of range: {imm16}")
    return (((opcode & OPCODE_MASK) << OPCODE_SHIFT)
            | ((rd & 7) << RD_SHIFT)
            | ((rs & 7) << RS_SHIFT)
            | ((mode & MODE_MASK) << MODE_SHIFT)
            | (imm16 & IMM16_MASK))


def encode(mnemonic: str, *operands: Any) -> int:
    """Encode one instruction.

    `operands` are in the order printed by the assembler (which is the order of
    `InsnSpec.operands`).  Values may be ints or, for register operands, 'rN'
    strings; CSR operands accept a CSR name.  For `rel16` the value is the
    *already computed* signed word offset (the assembler does relocation); use
    `encode_insn` when you have an `Insn`.
    """
    spec = _resolve_spec(mnemonic)
    if len(operands) != spec.n_operands:
        raise ValueError(
            f"{spec.mnemonic}: expected {spec.n_operands} operand(s), got {len(operands)}"
        )
    fields = {"rd": 0, "rs": 0, "imm16": 0}
    for (field, kind), value in zip(spec.operands, operands):
        if kind == REG:
            value = parse_reg(value)
        elif kind == CSR:
            value = csr_addr(value)
        fields[field] = _check_value(kind, int(value))
    return encode_raw(spec.opcode, fields["rd"], fields["rs"], 0, fields["imm16"])


def encode_insn(insn: "Insn") -> int:
    """Re-encode a decoded instruction (operand values are taken as stored)."""
    if insn.known:
        return encode(insn.mnemonic, *insn.operands)
    return encode_raw(insn.opcode, insn.rd, insn.rs, insn.mode, insn.imm16)


# ---------------------------------------------------------------------------
# CSR table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CsrSpec:
    name: str
    addr: int
    rw: str            # 'rw' | 'r' | 'w'
    scope: str         # 'thread' | 'global' | 'host'
    width: int         # meaningful bits
    description: str
    fields: str = ""   # bit-field breakdown for docs


CSR_SPECS: tuple[CsrSpec, ...] = (
    CsrSpec("TPERIOD", 0x00, "rw", "thread", 16, "event period, 12.4 fixed point", "int[15:4].frac[3:0]"),
    CsrSpec("TMASK", 0x01, "rw", "thread", 16, "event output mask"),
    CsrSpec("TSTATUS", 0x02, "r", "thread", 16, "thread status",
            "bit1=halted, bit0=en, bits15:2=0"),
    CsrSpec("TFIFO", 0x03, "r", "thread", 16, "FIFO occupancy / full flags",
            "[11:8]=rx_count, [7:4]=tx_count, bit1=tx_full, bit0=rx_empty"),
    CsrSpec("TNEXTT", 0x04, "r", "thread", 16, "last scheduled emit time (16-bit wrapping)"),
    CsrSpec("TEQ", 0x05, "r", "thread", 16, "event-queue occupancy",
            "bits2:0=eq_count (0..4)"),
    CsrSpec("TEMASK", 0x06, "rw", "thread", 16, "edge-trigger mask (emite kind only)"),
    CsrSpec("TECOND", 0x07, "rw", "thread", 16, "edge-trigger expected value"),
    CsrSpec("TPC", 0x08, "rw", "thread", 16, "program counter (10 meaningful bits)"),
    CsrSpec("POUT", 0x10, "rw", "global", 16, "pins_out register (host bits 9:8 never written)"),
    CsrSpec("PDIR", 0x11, "rw", "global", 8, "DIR / uio_oe[7:0]"),
    CsrSpec("TMR", 0x12, "r", "global", 16, "free-running 16-bit cycle counter"),
    CsrSpec("EVT", 0x13, "rw", "global", 8, "8 event flags"),
    CsrSpec("IRQ", 0x14, "rw", "global", 16, "host IRQ code (non-destructive read; ACK=write 0)", "bit0..15 code"),
    CsrSpec("WCTRL", 0x15, "rw", "global", 16, "wave player control",
            "bit0=start (write-1 pulse, reads 0), bit1=loop, bit2=oneshot (wins over loop)"),
    CsrSpec("WADDR", 0x16, "rw", "global", 6, "wave buffer start (DMEM word address)"),
    CsrSpec("WLEN", 0x17, "rw", "global", 7, "wave buffer length in words (1..64)"),
    CsrSpec("WPER", 0x18, "rw", "global", 16, "wave symbol period, 12.4 fixed point"),
    CsrSpec("WSTATUS", 0x19, "r", "global", 16, "wave player status",
            "bit1=busy, bit0=done"),
    CsrSpec("CAPCTRL", 0x1A, "rw", "global", 16, "capture trigger config (stretch)"),
    CsrSpec("CAPSTAT", 0x1B, "r", "global", 16, "capture status/count (stretch)"),
    CsrSpec("GSTATUS", 0x1C, "r", "global", 16, "global status",
            "bits8:5=en[3:0], bit0=irq, other bits 0"),
    CsrSpec("SOFTRST", 0x20, "w", "host", 1, "host-only soft reset (write 1): core only, not host bridge"),
)

#: Host-bridge -> core private CSR addresses (not in the spec §4.2 public map).
#: Lead decision 2026-11-16: HCMD_RUN writes a thread-enable mask here
#: (CSR_RUN = 0x30), HCMD_STOP writes a disable mask to CSR_STOP = 0x31.
#: HCMD_ACK_IRQ writes CSR_IRQ = 0.  They are not readable.
CSR_RUN = 0x30
CSR_STOP = 0x31

CSR_BY_NAME: dict[str, CsrSpec] = {c.name: c for c in CSR_SPECS}
CSR_BY_ADDR: dict[int, CsrSpec] = {c.addr: c for c in CSR_SPECS if c.scope != "thread"}

#: per-thread CSR address -> spec (same addresses in every thread bank)
THREAD_CSR_BY_ADDR: dict[int, CsrSpec] = {
    c.addr: c for c in CSR_SPECS if c.scope == "thread"
}


def csr_host_addr(tid: int, n: int) -> int:
    """Host-side CSR address of thread `tid`'s bank entry `n` (spec §4.2)."""
    if not 0 <= tid < PARAMS.get("NUM_THREADS", 4):
        raise ValueError(f"thread id out of range: {tid}")
    if not 0 <= n <= 0x0F:
        raise ValueError(f"thread CSR offset out of range: {n}")
    return 0x40 + 16 * tid + n


def csr_decode_host_addr(addr: int) -> tuple[int, int] | None:
    """Inverse of :func:`csr_host_addr`; None if `addr` is not a thread-bank addr."""
    if 0x40 <= addr <= 0x7F:
        return (addr - 0x40) >> 4, (addr - 0x40) & 0x0F
    return None


# ---------------------------------------------------------------------------
# Host command table (spec §6)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HostCmdSpec:
    name: str
    opcode: int
    n_args: int
    args: str
    n_returns: int
    returns: str
    description: str


HOST_COMMANDS: tuple[HostCmdSpec, ...] = (
    HostCmdSpec("SET_ADDR", 0x01, 3, "a[23:0] (3B, LSB first)", 1, "status", "set word address pointer"),
    HostCmdSpec("WRITE_MEM", 0x02, 4, "w[31:0] (4B, LSB first)", 1, "status", "MEM[addr] = w; addr++"),
    HostCmdSpec("READ_MEM", 0x03, 0, "-", 4, "w[31:0] (4B, LSB first)", "MEM[addr]; addr++"),
    HostCmdSpec("WRITE_CSR", 0x04, 3, "csr[7:0], d[15:0] (3B, LSB first)", 1, "status", "CSR write"),
    HostCmdSpec("READ_CSR", 0x05, 1, "csr[7:0]", 2, "d[15:0] (2B, LSB first)", "CSR read"),
    HostCmdSpec("TX_PUSH", 0x06, 2, "t[1:0], d[7:0]", 1, "status", "push byte to thread t RXFIFO"),
    HostCmdSpec("RX_POP", 0x07, 1, "t[1:0]", 1, "d[7:0]", "pop byte from thread t TXFIFO"),
    HostCmdSpec("SET_PC", 0x08, 3, "t[1:0], pc[15:0]", 1, "status", "thread PC"),
    HostCmdSpec("RUN", 0x09, 1, "mask[3:0]", 1, "status", "clear halt, en |= mask"),
    HostCmdSpec("STOP", 0x0A, 1, "mask[3:0]", 1, "status", "en &= ~mask"),
    HostCmdSpec("STATUS", 0x0B, 0, "-", 4, "status byte per thread", "{t3..t0 status bytes}"),
    HostCmdSpec("ACK_IRQ", 0x0C, 0, "-", 1, "status", "clear host IRQ latch"),
    HostCmdSpec("PING", 0x0D, 1, "0x5A", 1, "0xA5", "link check"),
)

HOST_CMD_BY_NAME: dict[str, HostCmdSpec] = {c.name: c for c in HOST_COMMANDS}
HOST_CMD_BY_OPCODE: dict[int, HostCmdSpec] = {c.opcode: c for c in HOST_COMMANDS}

#: host response status bytes (spec §6)
RESP_OK = SPEC_HCMDS.get("RESP_OK", 0x80)
RESP_BUSY = SPEC_HCMDS.get("RESP_BUSY", 0x81)
RESP_IDLE = 0x00
PING_SENTINEL = 0x5A
PING_REPLY = 0xA5


# ---------------------------------------------------------------------------
# Decoding / disassembly
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Insn:
    """A decoded instruction word.

    `rd`/`rs`/`mode` are the raw fields of *every* word (even when the mnemonic
    does not use them); `operands` is the per-mnemonic operand list in assembly
    order and `imm16` the raw immediate field.
    """

    word: int
    opcode: int
    mnemonic: str
    rd: int = 0
    rs: int = 0
    mode: int = 0
    imm16: int = 0
    known: bool = True
    pseudo: bool = False

    @property
    def name(self) -> str:
        return self.mnemonic

    @property
    def imm_signed(self) -> int:
        return self.imm16 - 0x10000 if self.imm16 & 0x8000 else self.imm16

    @property
    def spec(self) -> InsnSpec | None:
        return INSTRUCTIONS.get(self.mnemonic) if self.known else None

    @property
    def operands(self) -> list[int]:
        """Decoded operand values in assembly order.

        The operand -> word-field mapping comes from `InsnSpec.operands`, e.g.
        `csrw csr, rs` stores the register in the `rs` field while `csrr rd, csr`
        stores it in `rd`.
        """
        spec = self.spec
        if spec is None:
            return []
        out: list[int] = []
        for field, kind in spec.operands:
            raw = getattr(self, field) if field in ("rd", "rs", "imm16") else self.imm16
            out.append(_narrow_operand(kind, raw))
        return out

    @property
    def executes_as_nop(self) -> bool:
        """Unknown opcodes retire as nop (spec §4.1 forward compatibility)."""
        return not self.known


def _narrow_operand(kind: str, raw: int) -> int:
    """Apply a mnemonic's operand interpretation to a raw word field."""
    if kind == REL16:
        return raw - 0x10000 if raw & 0x8000 else raw
    if kind == IMM4:
        return raw & 0xF
    if kind in (IMM8, RPT):
        return raw & 0xFF
    if kind == ADDR6:
        return raw & 0x3F
    return raw


def decode(word: int) -> Insn:
    """Decode a 32-bit word into an :class:`Insn`."""
    if not isinstance(word, int):
        raise TypeError("word must be an int")
    word &= 0xFFFFFFFF
    opcode = (word >> OPCODE_SHIFT) & OPCODE_MASK
    rd = (word >> RD_SHIFT) & 0x7
    rs = (word >> RS_SHIFT) & 0x7
    mode = (word >> MODE_SHIFT) & MODE_MASK
    imm16 = word & 0xFFFF
    mnemonic = _OPCODE_TO_MNEMONIC.get(opcode)
    if mnemonic is None:
        return Insn(word=word, opcode=opcode, mnemonic="<invalid>", rd=rd, rs=rs,
                    mode=mode, imm16=imm16, known=False)
    if word != encode(mnemonic, *decode_fields(mnemonic, rd, rs, imm16)):
        # Reserved fields (mode, unused rd/rs) are set: still decode by opcode but
        # flag it so co-simulation can notice illegal encodings early.
        return Insn(word=word, opcode=opcode, mnemonic=mnemonic, rd=rd, rs=rs,
                    mode=mode, imm16=imm16, known=False)
    return Insn(word=word, opcode=opcode, mnemonic=mnemonic, rd=rd, rs=rs,
                mode=mode, imm16=imm16, known=True)


def decode_fields(mnemonic: str, rd: int, rs: int, imm16: int) -> list[int]:
    """Map raw rd/rs/imm16 fields to the mnemonic's operand values."""
    spec = _resolve_spec(mnemonic)
    out: list[int] = []
    for field, kind in spec.operands:
        raw = rd if field == "rd" else rs if field == "rs" else imm16
        out.append(_narrow_operand(kind, raw))
    return out


def _fmt_operand(insn: Insn, operand_index: int, kind: str, value: int,
                 pc: int | None, syms: dict[str, int] | None,
                 targets: dict[int, str] | None) -> str:
    if kind == REG:
        return f"r{value}"
    if kind == CSR:
        name = csr_name(value)
        return name.lower() if name else f"0x{value:02X}"
    if kind == ADDR6:
        return f"0x{value:02X}"
    if kind == REL16:
        if pc is not None:
            target = (pc + 1 + value) & 0x3FF
            if targets and target in targets:
                return targets[target]
            return f"0x{target:03X}"
        return f".{value:+d}"
    if kind == IMM4:
        return f"{value}"
    if kind == RPT:
        return f"{value}"          # n: repeat n+1 times
    if kind in (IMM8,):
        return f"0x{value:02X}"
    if kind == IMM:
        return f"0x{value:04X}"
    if kind == ABS16 or kind == WAITP:
        return f"0x{value:04X}"
    raise AssertionError(f"unhandled operand kind {kind!r}")


def disasm(word: int, pc: int | None = None,
           syms: dict[str, int] | None = None) -> str:
    """One-line disassembly of `word`.

    Unknown/unused encodings print as `.word 0x........`.  If `pc` (the word
    address of this instruction) is given, relative jumps are rendered as target
    addresses, resolved to labels when `syms` (or a precomputed `targets` map) is
    supplied.  `syms` maps labels to word addresses.
    """
    insn = decode(word)
    if not insn.known:
        return f".word 0x{insn.word:08X}"
    spec = insn.spec
    assert spec is not None
    targets: dict[int, str] | None = None
    if syms:
        targets = {}
        for label, addr in syms.items():
            targets.setdefault(addr & 0x3FF, label)
    text = insn.mnemonic
    ops: list[str] = []
    for i, (_field, kind) in enumerate(spec.operands):
        ops.append(_fmt_operand(insn, i, kind, insn.operands[i], pc, syms, targets))
    if ops:
        text += " " + ", ".join(ops)
    return text


# ---------------------------------------------------------------------------
# Docs helpers
# ---------------------------------------------------------------------------
def _md(text: str) -> str:
    """Escape a string for a markdown table cell (pipes would split the row)."""
    return str(text).replace("|", "\\|")


def _operand_text(spec: InsnSpec) -> str:
    names = {"reg": lambda f: f, "imm": lambda f: "imm16", "imm8": lambda f: "imm8",
             "imm4": lambda f: "n", "csr": lambda f: "csr", "addr6": lambda f: "a6",
             "rel16": lambda f: "rel16", "abs16": lambda f: "t16",
             "waitp": lambda f: "mask|value", "rpt": lambda f: "n"}
    return ", ".join(names[kind](field) for field, kind in spec.operands)


def summary_table(header: bool = True) -> str:
    """Markdown rows describing every real opcode (for docs/isa.md)."""
    rows: list[str] = []
    if header:
        rows.append("| Op | Mnemonic | Operands | Semantics | Flags |")
        rows.append("|---|---|---|---|---|")
    for spec in INSTRUCTIONS.values():
        rows.append(
            f"| `0x{spec.opcode:02X}` | `{spec.mnemonic}` | "
            f"`{_md(_operand_text(spec))}` | {_md(spec.description)} | "
            f"{_md(spec.flags or '—')} |"
        )
    return "\n".join(rows)


def csr_table(header: bool = True) -> str:
    rows: list[str] = []
    if header:
        rows.append("| CSR | Name | R/W | Bank | Bits | Meaning | Fields |")
        rows.append("|---|---|---|---|---|---|---|")
    for c in CSR_SPECS:
        rows.append(
            f"| `0x{c.addr:02X}` | `{c.name}` | {c.rw.upper()} | "
            f"{'per-thread' if c.scope == 'thread' else c.scope} | {c.width} | "
            f"{_md(c.description)} | {_md(c.fields or '—')} |"
        )
    return "\n".join(rows)


def host_command_table(header: bool = True) -> str:
    rows: list[str] = []
    if header:
        rows.append("| CMD | Name | Args (host→chip) | Returns | Meaning |")
        rows.append("|---|---|---|---|---|")
    for c in HOST_COMMANDS:
        rows.append(
            f"| `0x{c.opcode:02X}` | `{c.name}` | {_md(c.args)} | {_md(c.returns)} | "
            f"{_md(c.description)} |"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Self-test / docs printer
# ---------------------------------------------------------------------------
def _selftest() -> int:
    """Cheap consistency checks (also run from test/unit_sw)."""
    errors: list[str] = []
    if not BUS_VH_FOUND:
        errors.append(f"could not parse any defines from {BUS_VH_PATH}")
    # every mnemonic must round-trip through encode/decode with a sample value
    samples: dict[str, tuple[Any, ...]] = {
        "nop": (), "halt": (), "movi": ("r1", 0x1234), "mov": ("r2", "r3"),
        "add": ("r0", "r1"), "addi": ("r0", 7), "sub": ("r0", "r1"),
        "subi": ("r0", 7), "and": ("r0", "r1"), "andi": ("r0", 0xFF),
        "or": ("r0", "r1"), "ori": ("r0", 0xFF), "xor": ("r0", "r1"),
        "xori": ("r0", 0xFF), "inv": ("r0",), "shl": ("r0", 3),
        "shr": ("r0", 3), "rol": ("r0", 3), "ror": ("r0", 3),
        "cmp": ("r0", "r1"), "cmpi": ("r0", 5),
        "csrw": ("TMASK", "r1"), "csrr": ("r0", "TSTATUS"),
        "out": (0x1234,), "set": (0x000F,), "clr": (0x000F,), "tog": (0x000F,),
        "in": ("r0",), "dir": (0xAA,), "dset": (0x0F,), "dclr": (0xF0,),
        "emit": ("r0",), "emita": ("r0", 100), "emitd": ("r0", 5),
        "emite": ("r0",), "setp": (0x208D,), "setm": (0xFCFF,),
        "setem": (0x0300,), "setec": (0x0100,), "inat": ("r0", 42),
        "waitu": (42,), "waitd": (0x208D,), "waitp": (0x1201,),
        "ld": ("r0", 17), "st": (17, "r0"), "ldi": ("r0", "r1"),
        "sti": ("r1", "r2"), "push": ("r0",), "pull": ("r0",),
        "pushi": (0x5A,), "jmp": (3,), "jz": (-2,), "jnz": (0,),
        "jc": (1,), "jnc": (-1,), "rpt": (9,), "evset": (1,),
        "evwait": (2,), "irq": (7,),
    }
    for mn, spec in INSTRUCTIONS.items():
        if mn not in samples:
            errors.append(f"no self-test sample for {mn}")
            continue
        word = encode(mn, *samples[mn])
        insn = decode(word)
        if not insn.known or insn.mnemonic != mn:
            errors.append(f"{mn}: round-trip failed (word=0x{word:08X} -> {insn.mnemonic})")
            continue
        # re-encode from decoded operands
        if encode(mn, *insn.operands) != word:
            errors.append(f"{mn}: re-encode mismatch 0x{word:08X}")
    # opcode map must be unique
    seen: dict[int, str] = {}
    for mn, spec in INSTRUCTIONS.items():
        if spec.opcode in seen:
            errors.append(f"opcode 0x{spec.opcode:02X} used by both {seen[spec.opcode]} and {mn}")
        seen[spec.opcode] = mn
    # CSR address uniqueness
    addrs: dict[int, str] = {}
    for c in CSR_SPECS:
        if c.scope != "thread" and c.addr in addrs:
            errors.append(f"CSR 0x{c.addr:02X} used by both {addrs[c.addr]} and {c.name}")
        addrs[c.addr] = c.name
    hseen: dict[int, str] = {}
    for c in HOST_COMMANDS:
        if c.opcode in hseen:
            errors.append(f"host cmd 0x{c.opcode:02X} used twice ({hseen[c.opcode]}, {c.name})")
        hseen[c.opcode] = c.name
    if errors:
        for e in errors:
            print(f"ISA_SELFTEST: FAIL: {e}", file=sys.stderr)
        return 1
    print(f"ISA_SELFTEST: PASS ({len(INSTRUCTIONS)} mnemonics, "
          f"{len(CSR_SPECS)} CSRs, {len(HOST_COMMANDS)} host commands, "
          f"loom_bus.vh {'parsed' if BUS_VH_FOUND else 'MISSING'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

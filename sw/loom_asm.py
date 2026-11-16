#!/usr/bin/env python3
"""LOOM two-pass assembler `loom_asm`.

Usage
-----
    python -m sw.loom_asm prog.asm -o prog.hex \
        --format hex32|c_array|bin --rom-words 1024 --fill 0x00000001 \
        [--lst prog.lst] [--sym prog.sym] [--relocs prog.json]

    python -m sw.loom_asm --help          # this text + all options

Input syntax
------------
Labels
    ``name:`` on its own line, or in front of an instruction/directive
    (``loop: emit r0``).  Label names match ``[A-Za-z_][A-Za-z0-9_.$]*`` and are
    case-sensitive; mnemonics, register names and CSR names are case-insensitive.

Comments
    ``;``, ``//`` and ``#`` start a comment that runs to end of line.
    (No block comments, no strings, no ``#include`` — programs are a single file.)

Directives
    ``.equ NAME, expr``      define an absolute symbol (may reference symbols
                             defined earlier in the file, *not* forward ones)
    ``.word expr[, expr...]``emit one 32-bit word per expression
    ``.align n``             pad with ``nop`` (0) until the word address is a
                             multiple of n (n >= 1)
    ``.fill n[, value]``     emit n words of `value` (default 0); n and value
                             must be resolvable in pass 1 (no forward labels)

Mnemonics / operands
    59 real mnemonics exactly as spec/ARCHITECTURE.md §4.1, e.g.
    ``movi r1, 0x1234`` · ``add r1, r2`` · ``csrw TMASK, r3`` ·
    ``csrr r0, TSTATUS`` · ``emit r2`` · ``emita r2, done`` ·
    ``inat r1, 42`` · ``waitp 0x1201`` · ``ld r0, 0x10`` · ``st 0x10, r0`` ·
    ``ldi r0, r1`` · ``sti r1, r2`` · ``push r0`` · ``pull r0`` ·
    ``jmp loop`` · ``rpt 9`` · ``evset 1`` · ``irq 7``.

    Register operands are ``r0``..``r7`` (a bare ``0``..``7`` also works).
    CSR operands are CSR names (``TPERIOD``, ``TSTATUS``, ``POUT``, ...) or a
    numeric address.  Immediate expressions may be decimal, ``0x`` hex or
    ``0b`` binary (``_`` digit separators allowed), may use the operators
    ``+ - * / % << >> & | ^ ~`` (C precedence), unary ``-``/``~``, parentheses,
    labels and ``.`` (= address of the current instruction word).  Division is
    truncating integer division (C99 semantics); shifting needs count 0..63.
    Values are range-checked per operand (e.g. shift counts 0..15, DMEM
    addresses 0..63).

    Shift mnemonics take ``n`` directly: ``shl r1, 3``.

Jumps (rel16)
    The hardware field is a signed **word** offset from the next instruction.
    In source, you write the **absolute target** — a label or an expression
    that evaluates to a word address — and the assembler computes
    ``rel16 = target - pc`` (the hardware does ``PC += sext(rel16)`` from the
    jump instruction's own address; spec §3.1 S3 / §4.1).  Every jump is recorded in
    :attr:`AssemblyResult.relocs`; the ``symbol`` field is set when the target
    expression is exactly one label, so firmware can be relocated by rewriting
    that word.  (Pseudo-instruction ``li rd, imm`` is an alias for ``movi``;
    there is no ``ret`` — LOOM has no return stack, so it is an error.)

Output formats
    ``hex32``   one 8-digit lowercase hex word per line (``$readmemh`` friendly)
    ``c_array`` ``const uint32_t <name>[N] = { ... };`` with ``#include <stdint.h>``
    ``bin``     raw little-endian 32-bit words

    ``--rom-words N`` pads the image to N words with ``--fill`` (default
    ``0x02000000`` = the real `halt` word; Lead decision 2026-11-16 — spec §5's
    literal 0x00000001 decodes as `nop` after the 7-bit opcode fix, so a runaway
    PC would not stop); without it the image is exactly the emitted words.
    Padding is not listed in ``--lst``.

Semantic decisions (where this file had to choose)
--------------------------------------------------
  * Jump operands are absolute addresses; raw rel16 encoding is
    ``target-pc`` (hardware: ``PC += sext(rel16)``).  Numeric operands are
    therefore targets, not raw offsets.
  * ``.align n`` aligns to an n-word boundary (n is a word count, not log2).
  * ``.equ``/``.fill`` counts must be pass-1 resolvable: no forward references.
  * ``li`` is the only pseudo-instruction added.
  * A bare number in a register position is accepted as a register index.
  * Immediate range checks: imm16 accepts -32768..65535; imm8 -128..255;
    imm4 0..15; addr6 0..63; rel16 result must fit int16.
  * `ld/ldi/st/sti` are single-cycle instructions (Lead correction); the
    assembler does not need to know, but firmware cycle budgets do.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import struct
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

if __package__ in (None, ""):  # allow `python sw/loom_asm.py` as well as -m
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from sw import isa  # type: ignore
else:
    from sw import isa

__all__ = [
    "AsmError", "Reloc", "ListingEntry", "AssemblyResult", "assemble",
    "assemble_file", "split_operands", "eval_expr",
    "format_hex32", "format_c_array", "format_binary",
    "DEFAULT_ROM_FILL",
]

DEFAULT_ROM_FILL = isa.DEFAULT_FILL_WORD

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]*")
LABEL_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_.$]*)\s*:")
NUMBER_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*)"
)
DIRECTIVE_RE = re.compile(r"^\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$")


class AsmError(Exception):
    """Assembler error carrying source location."""

    def __init__(self, message: str, filename: str = "<input>", line: int = 0):
        self.message = message
        self.filename = filename
        self.line = line
        super().__init__(f"{filename}:{line}: error: {message}")


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------
_BINARY_PREC = {
    "|": 1, "^": 2, "&": 3, "<<": 4, ">>": 4,
    "+": 5, "-": 5, "*": 6, "/": 6, "%": 6,
}
_UNARY = ("-", "~", "+")


class _ExprParser:
    def __init__(self, text: str, symbols: dict[str, int], pc: int,
                 filename: str, line: int):
        self.text = text
        self.symbols = symbols
        self.pc = pc
        self.filename = filename
        self.line = line
        self.tokens = self._tokenize(text)
        self.pos = 0

    # -- errors -----------------------------------------------------------
    def err(self, msg: str) -> AsmError:
        return AsmError(f"{msg} in expression {self.text!r}", self.filename, self.line)

    # -- tokenizer --------------------------------------------------------
    @staticmethod
    def _tokenize(text: str) -> list[tuple[str, Any]]:
        toks: list[tuple[str, Any]] = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch.isspace():
                i += 1
                continue
            if text.startswith("<<", i) or text.startswith(">>", i):
                toks.append(("op", text[i:i + 2]))
                i += 2
                continue
            if ch in "+-*/%&|^~()":
                toks.append(("lparen" if ch == "(" else
                             "rparen" if ch == ")" else "op", ch))
                i += 1
                continue
            m = NUMBER_RE.match(text, i)
            if m:
                tok = m.group(0).replace("_", "")
                try:
                    value = int(tok, 0) if not tok.lstrip("-").startswith("0") else int(tok, 0)
                except ValueError:  # pragma: no cover - regex-validated
                    raise ValueError(f"bad number {tok!r}") from None
                toks.append(("num", value))
                i = m.end()
                continue
            if ch == ".":
                # `.` is the current PC; labels may contain dots but never start
                # with one, so this is unambiguous.
                toks.append(("pc", None))
                i += 1
                continue
            m = IDENT_RE.match(text, i)
            if m:
                toks.append(("id", m.group(0)))
                i = m.end()
                continue
            raise ValueError(f"unexpected character {ch!r} in expression {text!r}")
        toks.append(("eof", None))
        return toks

    # -- pratt parser -----------------------------------------------------
    def peek(self) -> tuple[str, Any]:
        return self.tokens[self.pos]

    def next(self) -> tuple[str, Any]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> int:
        value = self.expr(0)
        kind, val = self.peek()
        if kind != "eof":
            raise self.err(f"unexpected token {val!r} after expression")
        return value

    def expr(self, min_prec: int) -> int:
        left = self.unary()
        while True:
            kind, val = self.peek()
            if kind != "op":
                break
            prec = _BINARY_PREC.get(val)
            if prec is None or prec < min_prec:
                break
            self.next()
            right = self.expr(prec + 1)      # left-associative
            left = self.apply(val, left, right)
        return left

    def unary(self) -> int:
        kind, val = self.peek()
        if kind == "op" and val in _UNARY:
            self.next()
            operand = self.unary()
            if val == "-":
                return -operand
            if val == "+":
                return operand
            return ~operand
        if kind == "num":
            self.next()
            return val
        if kind == "pc":
            self.next()
            return self.pc
        if kind == "id":
            self.next()
            try:
                return self.symbols[val]
            except KeyError:
                raise self.err(f"undefined symbol {val!r}") from None
        if kind == "lparen":
            self.next()
            value = self.expr(0)
            kind2, val2 = self.peek()
            if kind2 != "rparen":
                raise self.err("missing ')'")
            self.next()
            return value
        raise self.err(f"unexpected token {val!r}")

    def apply(self, op: str, a: int, b: int) -> int:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "%"):
            if b == 0:
                raise self.err("division by zero")
            q = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                q = -q
            return q if op == "/" else a - q * b
        if op in ("<<", ">>"):
            if not 0 <= b <= 63:
                raise self.err(f"shift count out of range: {b}")
            return a << b if op == "<<" else a >> b
        if op == "&":
            return a & b
        if op == "|":
            return a | b
        if op == "^":
            return a ^ b
        raise self.err(f"unknown operator {op!r}")  # pragma: no cover


def eval_expr(text: str, symbols: dict[str, int], pc: int = 0,
              filename: str = "<input>", line: int = 0) -> int:
    """Evaluate one assembler expression to an integer."""
    text = text.strip()
    if not text:
        raise AsmError("empty expression", filename, line)
    try:
        return _ExprParser(text, symbols, pc, filename, line).parse()
    except ValueError as exc:  # tokenizer-level error
        raise AsmError(str(exc), filename, line) from None


def split_operands(text: str) -> list[str]:
    """Split an operand list on top-level commas (parentheses respected)."""
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise AsmError("unbalanced ')' in operand list")
        elif ch == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
    out.append(text[start:].strip())
    return out


def _split_directive_args(text: str, count: int, what: str,
                          filename: str, line: int) -> list[str]:
    parts = split_operands(text)
    if len(parts) > count:
        raise AsmError(f"{what}: too many arguments", filename, line)
    return parts


def _strip_comment(line: str) -> str:
    cut = len(line)
    for marker in (";", "//", "#"):
        idx = line.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return line[:cut]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class Reloc:
    """A rel16 jump and the symbol/target it refers to."""

    word: int          # word address of the jump instruction in the image
    mnemonic: str
    expression: str
    target: int        # absolute word address the jump goes to
    rel: int           # encoded signed offset (target - (word + 1))
    symbol: str | None = None   # set iff `expression` is exactly one label
    lineno: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "word": self.word, "mnemonic": self.mnemonic,
            "expression": self.expression, "target": self.target,
            "rel": self.rel, "symbol": self.symbol, "line": self.lineno,
        }


@dataclass
class ListingEntry:
    addr: int | None
    word: int | None
    text: str
    source: str
    lineno: int
    label: str | None = None


@dataclass
class AssemblyResult:
    words: list[int]
    symbols: dict[str, int]
    relocs: list[Reloc]
    listing: list[ListingEntry]
    symbol_kinds: dict[str, str] = field(default_factory=dict)
    fill: int = DEFAULT_ROM_FILL
    rom_words: int | None = None
    filename: str = "<input>"

    @property
    def n_words(self) -> int:
        return len(self.words)

    def padded_words(self) -> list[int]:
        words = list(self.words)
        if self.rom_words is not None:
            if len(words) > self.rom_words:
                raise AsmError(
                    f"program is {len(words)} words, larger than --rom-words "
                    f"{self.rom_words}", self.filename, 0)
            words.extend([self.fill & 0xFFFFFFFF] * (self.rom_words - len(words)))
        return words

    def listing_text(self) -> str:
        lines = [f"; LOOM listing — {self.filename}",
                 "; addr   word      disassembly               source"]
        for e in self.listing:
            if e.label is not None:
                lines.append(f"; --- {e.label} = 0x{e.addr or 0:03X}")
                continue
            if e.addr is None:
                lines.append(f"; {e.text}")
                continue
            lines.append(f"{e.addr:04X}   {e.word:08X}  {e.text:<24s} {e.source}")
        return "\n".join(lines) + "\n"

    def symbols_text(self) -> str:
        lines = ["; LOOM symbol table"]
        for name in sorted(self.symbols, key=lambda n: (self.symbols[n], n)):
            kind = self.symbol_kinds.get(name, "label")
            value = self.symbols[name]
            if value < 0:
                shown = f"{value} (0x{value & 0xFFFFFFFF:X})"
            else:
                shown = f"0x{value:04X}"
            lines.append(f"{name:<24s} = {shown}  ; {kind}")
        return "\n".join(lines) + "\n"

    def relocs_text(self) -> str:
        return json.dumps([r.to_dict() for r in self.relocs], indent=2) + "\n"


# ---------------------------------------------------------------------------
# Image formatters
# ---------------------------------------------------------------------------
def format_hex32(words: Sequence[int]) -> str:
    """One 8-digit lowercase hex word per line; ``$readmemh``-friendly."""
    return "".join(f"{w & 0xFFFFFFFF:08x}\n" for w in words)


def format_c_array(words: Sequence[int], name: str = "loom_prog") -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise AsmError(f"invalid C array name {name!r}")
    lines = [
        "/* Generated by sw/loom_asm.py — LOOM program image. Do not edit. */",
        "#include <stdint.h>",
        "",
        f"const uint32_t {name}[{len(words)}] = {{",
    ]
    per_line = 4
    for i in range(0, len(words), per_line):
        chunk = ", ".join(f"0x{w & 0xFFFFFFFF:08X}u" for w in words[i:i + per_line])
        lines.append(f"    {chunk},")
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def format_binary(words: Sequence[int]) -> bytes:
    """Raw little-endian 32-bit words."""
    return struct.pack(f"<{len(words)}I", *[w & 0xFFFFFFFF for w in words])


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------
@dataclass
class _Item:
    kind: str                    # 'insn' | 'word' | 'fill' | 'pad' | 'label'
    pc: int
    lineno: int
    text: str = ""               # raw source (for the listing)
    mnemonic: str = ""
    operands: tuple[str, ...] = ()
    exprs: tuple[str, ...] = ()
    count: int = 0
    value_expr: str | None = None
    label: str | None = None


def _parse_operand_value(kind: str, text: str, symbols: dict[str, int],
                         pc: int, filename: str, line: int) -> int:
    """Evaluate one operand of the given ISA kind."""
    if kind == isa.REG:
        t = text.strip()
        m = re.fullmatch(r"[rR]([0-7])", t)
        if m:
            return int(m.group(1))
        if re.fullmatch(r"-?[0-9]+", t):
            value = int(t, 10)
            if not 0 <= value <= 7:
                raise AsmError(f"register index out of range: {value}", filename, line)
            return value
        # Not a register: give a precise error instead of a generic expression one.
        if re.fullmatch(r"[rR]-?[0-9]+", t):
            raise AsmError(f"register index out of range: {t!r} (want r0..r7)",
                           filename, line)
        if not re.match(r"^[0-9]", t) and not t.startswith("(") and t:
            raise AsmError(f"expected register r0..r7, got {t!r}", filename, line)
        value = eval_expr(t, symbols, pc, filename, line)
        if not 0 <= value <= 7:
            raise AsmError(f"register index out of range: {value}", filename, line)
        return value
    if kind == isa.CSR:
        t = text.strip()
        upper = t.upper()
        if upper in isa.SPEC_CSR:
            return isa.SPEC_CSR[upper]
        if re.fullmatch(r"-?[0-9]+|0[xX][0-9a-fA-F]+", t):
            value = eval_expr(t, symbols, pc, filename, line)
            if not 0 <= value <= 0xFF:
                raise AsmError(f"CSR address out of range: {value}", filename, line)
            return value
        raise AsmError(f"unknown CSR {t!r}", filename, line)
    value = eval_expr(text, symbols, pc, filename, line)
    if kind == isa.IMM4 and not 0 <= value <= 15:
        raise AsmError(f"shift count out of range: {value} (want 0..15)", filename, line)
    if kind == isa.IMM8 and not -0x80 <= value <= 0xFF:
        raise AsmError(f"imm8 out of range: {value} (want -128..255)", filename, line)
    if kind == isa.RPT and not 0 <= value <= 0xFF:
        raise AsmError(f"rpt count out of range: {value} (want 0..255)", filename, line)
    if kind == isa.ADDR6 and not 0 <= value <= 63:
        raise AsmError(f"DMEM address out of range: {value} (want 0..63)", filename, line)
    if kind == isa.WAITP and not 0 <= value <= 0xFFFF:
        raise AsmError(f"waitp operand out of range: {value}", filename, line)
    if kind in (isa.IMM, isa.REL16, isa.ABS16) and not -0x8000 <= value <= 0xFFFF:
        raise AsmError(f"immediate out of range: {value} (want -32768..65535)",
                       filename, line)
    return value


def _looks_like_single_label(text: str, symbols: dict[str, int]) -> str | None:
    """Return the label name if the expression is exactly one label, else None."""
    t = text.strip()
    if IDENT_RE.fullmatch(t) and t in symbols and t != ".":
        return t
    return None


def assemble(source: str, filename: str = "<input>", *,
             rom_words: int | None = None,
             fill: int = DEFAULT_ROM_FILL) -> AssemblyResult:
    """Assemble LOOM source text into a program image.

    Raises :class:`AsmError` on the first error (message includes file:line).
    """
    lines = source.splitlines()
    items: list[_Item] = []
    symbols: dict[str, int] = {}
    symbol_kinds: dict[str, str] = {}
    labels_at: dict[int, list[str]] = {}
    pc = 0

    def err(msg: str, lineno: int) -> AsmError:
        return AsmError(msg, filename, lineno)

    # ------------------------------ pass 1 ------------------------------
    for lineno, raw in enumerate(lines, start=1):
        text = _strip_comment(raw).strip()
        if not text:
            continue
        # labels (possibly several, possibly followed by an instruction)
        while True:
            m = LABEL_RE.match(text)
            if not m:
                break
            name = m.group(1)
            if name in symbols:
                raise err(f"duplicate symbol {name!r}", lineno)
            symbols[name] = pc
            symbol_kinds[name] = "label"
            labels_at.setdefault(pc, []).append(name)
            text = text[m.end():].strip()
            if not text:
                break
        if not text:
            continue

        if text.startswith("."):
            dm = DIRECTIVE_RE.match(text)
            if not dm:
                raise err(f"malformed directive: {text!r}", lineno)
            directive = dm.group(1).lower()
            args_text = dm.group(2).strip()
            if directive in ("equ", "set"):
                parts = _split_directive_args(args_text, 2, ".equ", filename, lineno)
                if len(parts) != 2:
                    raise err(".equ needs NAME, expression", lineno)
                name = parts[0].strip()
                if not IDENT_RE.fullmatch(name):
                    raise err(f".equ: bad symbol name {name!r}", lineno)
                if name in symbols:
                    raise err(f".equ: duplicate symbol {name!r}", lineno)
                value = eval_expr(parts[1], symbols, pc, filename, lineno)
                if not -(1 << 63) <= value < (1 << 64):
                    raise err(f".equ: value out of 64-bit range: {value}", lineno)
                symbols[name] = value
                symbol_kinds[name] = "equ"
                items.append(_Item("label", pc, lineno, label=None))
                continue
            if directive == "word":
                exprs = split_operands(args_text)
                if not exprs or exprs == [""]:
                    raise err(".word needs at least one expression", lineno)
                items.append(_Item("word", pc, lineno, text=raw.strip(), exprs=tuple(exprs)))
                pc += len(exprs)
                continue
            if directive == "align":
                if not args_text:
                    raise err(".align needs an alignment", lineno)
                n = eval_expr(args_text, symbols, pc, filename, lineno)
                if n < 1:
                    raise err(f".align: alignment must be >= 1, got {n}", lineno)
                pad = (-pc) % n
                if pad:
                    items.append(_Item("pad", pc, lineno, text=raw.strip(), count=pad))
                    pc += pad
                continue
            if directive == "fill":
                parts = _split_directive_args(args_text, 2, ".fill", filename, lineno)
                if not parts or not parts[0]:
                    raise err(".fill needs a count", lineno)
                n = eval_expr(parts[0], symbols, pc, filename, lineno)
                if n < 0:
                    raise err(f".fill: negative count {n}", lineno)
                value_expr = parts[1] if len(parts) > 1 and parts[1] else None
                if value_expr is not None:
                    # validate pass-1 resolvable; the value is re-evaluated in pass 2
                    eval_expr(value_expr, symbols, pc, filename, lineno)
                items.append(_Item("fill", pc, lineno, text=raw.strip(), count=n,
                                   value_expr=value_expr))
                pc += n
                continue
            raise err(f"unknown directive .{directive}", lineno)

        # instruction
        parts = text.split(None, 1)
        mnemonic = parts[0].lower()
        operand_text = parts[1] if len(parts) > 1 else ""
        spec = isa.INSTRUCTIONS.get(isa.PSEUDOS.get(mnemonic, (mnemonic,))[0])
        if spec is None:
            raise err(f"unknown mnemonic {parts[0]!r}", lineno)
        operands = tuple(split_operands(operand_text))
        if len(operands) != spec.n_operands:
            raise err(
                f"{spec.mnemonic}: expected {spec.n_operands} operand(s), "
                f"got {len(operands)}", lineno)
        items.append(_Item("insn", pc, lineno, text=raw.strip(), mnemonic=mnemonic,
                           operands=operands))
        pc += 1

    # ------------------------------ pass 2 ------------------------------
    words: list[int] = []
    relocs: list[Reloc] = []
    listing: list[ListingEntry] = []
    emitted_labels: dict[int, list[str]] = {}
    for item in items:
        if item.kind == "label":
            continue
        if item.pc != len(words):
            raise AsmError(
                f"internal layout error: pc {item.pc} != image {len(words)}", filename,
                item.lineno)
        # labels attached to this address
        for label in labels_at.get(item.pc, []):
            emitted_labels.setdefault(item.pc, []).append(label)
        for label in labels_at.get(item.pc, []):
            listing.append(ListingEntry(item.pc, None, "", item.text, item.lineno,
                                        label=label))
        if item.kind == "insn":
            canonical = isa.PSEUDOS.get(item.mnemonic, (item.mnemonic,))[0]
            spec = isa.INSTRUCTIONS[canonical]
            values: list[int] = []
            for (field, kind), optext in zip(spec.operands, item.operands):
                if kind == isa.REL16:
                    target = eval_expr(optext, symbols, item.pc, filename, item.lineno)
                    if not -(1 << 63) <= target < (1 << 63):
                        raise AsmError("jump target out of range", filename, item.lineno)
                    rel = target - item.pc
                    if not -0x8000 <= rel <= 0x7FFF:
                        raise AsmError(
                            f"jump target 0x{target:X} out of rel16 range from "
                            f"word 0x{item.pc:X} (rel={rel})", filename, item.lineno)
                    relocs.append(Reloc(
                        word=item.pc, mnemonic=canonical, expression=optext.strip(),
                        target=target, rel=rel,
                        symbol=_looks_like_single_label(optext, symbols),
                        lineno=item.lineno))
                    values.append(rel)
                else:
                    values.append(_parse_operand_value(
                        kind, optext, symbols, item.pc, filename, item.lineno))
            try:
                word = isa.encode(canonical, *values)
            except ValueError as exc:
                raise AsmError(str(exc), filename, item.lineno) from None
            words.append(word)
            listing.append(ListingEntry(
                item.pc, word, isa.disasm(word, pc=item.pc, syms=symbols),
                item.text, item.lineno))
        elif item.kind == "word":
            for expr in item.exprs:
                value = eval_expr(expr, symbols, item.pc, filename, item.lineno)
                if not -0x80000000 <= value <= 0xFFFFFFFF:
                    raise AsmError(f".word value out of 32-bit range: {value}",
                                   filename, item.lineno)
                word = value & 0xFFFFFFFF
                words.append(word)
                listing.append(ListingEntry(
                    len(words) - 1, word, isa.disasm(word, pc=len(words) - 1, syms=symbols),
                    item.text, item.lineno))
        elif item.kind == "fill":
            value = (eval_expr(item.value_expr, symbols, item.pc, filename, item.lineno)
                     if item.value_expr is not None else 0)
            if not -0x80000000 <= value <= 0xFFFFFFFF:
                raise AsmError(f".fill value out of 32-bit range: {value}",
                               filename, item.lineno)
            fill_word = value & 0xFFFFFFFF
            for _ in range(item.count):
                words.append(fill_word)
                listing.append(ListingEntry(
                    len(words) - 1, fill_word,
                    isa.disasm(fill_word, pc=len(words) - 1, syms=symbols),
                    item.text, item.lineno))
        elif item.kind == "pad":
            for _ in range(item.count):
                words.append(0)
                listing.append(ListingEntry(len(words) - 1, 0, "nop",
                                            item.text, item.lineno))
        else:  # pragma: no cover
            raise AssertionError(item.kind)

    if rom_words is not None and rom_words < 0:
        raise AsmError(f"--rom-words must be >= 0 (got {rom_words})", filename, 0)
    return AssemblyResult(words=words, symbols=symbols, relocs=relocs,
                          listing=listing, symbol_kinds=symbol_kinds,
                          fill=fill & 0xFFFFFFFF, rom_words=rom_words,
                          filename=filename)


def assemble_file(path: str | pathlib.Path, **kwargs: Any) -> AssemblyResult:
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise AsmError(f"cannot read {p}: {exc.strerror}", str(p), 0) from None
    return assemble(text, filename=str(p), **kwargs)


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
        prog="python -m sw.loom_asm",
        description="LOOM two-pass assembler.  Reads one .asm file and emits a "
                    "32-bit program image (hex32 / C array / binary).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m sw.loom_asm prog.asm -o prog.hex\n"
            "  python -m sw.loom_asm prog.asm -o prog.hex --rom-words 1024 "
            "--fill 0x00000001 --lst prog.lst --sym prog.sym\n"
            "  python -m sw.loom_asm prog.asm -o prog.c --format c_array --name loom_fw\n"
            "  python -m sw.loom_asm prog.asm -o prog.bin --format bin\n"
        ),
    )
    p.add_argument("input", help="assembly source file (.asm)")
    p.add_argument("-o", "--output", default=None,
                   help="output file (default: stdout for text formats)")
    p.add_argument("--format", choices=("hex32", "c_array", "bin"), default="hex32",
                   help="output image format (default: hex32)")
    p.add_argument("--rom-words", type=_int_arg, default=None, metavar="N",
                   help="pad the image to N words with --fill (default: exact size)")
    p.add_argument("--fill", type=_int_arg, default=DEFAULT_ROM_FILL, metavar="VAL",
                   help=f"padding word for --rom-words and .align (default: "
                        f"0x{DEFAULT_ROM_FILL:08X})")
    p.add_argument("--name", default=None,
                   help="C array name for --format c_array (default: output stem)")
    p.add_argument("--lst", default=None, metavar="PATH",
                   help="write a disassembly listing")
    p.add_argument("--sym", default=None, metavar="PATH",
                   help="write a symbol table")
    p.add_argument("--relocs", default=None, metavar="PATH",
                   help="write the reloc list as JSON")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress the summary line")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = assemble_file(args.input, rom_words=args.rom_words, fill=args.fill)
        words = result.padded_words()
    except AsmError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.format == "hex32":
        payload: str | bytes = format_hex32(words)
    elif args.format == "c_array":
        name = args.name
        if name is None:
            name = pathlib.Path(args.output).stem if args.output else "loom_prog"
            name = re.sub(r"[^A-Za-z0-9_]", "_", name) or "loom_prog"
        payload = format_c_array(words, name)
    else:  # bin
        payload = format_binary(words)

    if args.output:
        out = pathlib.Path(args.output)
        try:
            if isinstance(payload, bytes):
                out.write_bytes(payload)
            else:
                out.write_text(payload, encoding="utf-8")
        except OSError as exc:
            print(f"{args.output}: cannot write: {exc.strerror}", file=sys.stderr)
            return 1
    else:
        if isinstance(payload, bytes):
            print("loom_asm: --format bin needs -o FILE", file=sys.stderr)
            return 1
        sys.stdout.write(payload)

    if args.lst:
        pathlib.Path(args.lst).write_text(result.listing_text(), encoding="utf-8")
    if args.sym:
        pathlib.Path(args.sym).write_text(result.symbols_text(), encoding="utf-8")
    if args.relocs:
        pathlib.Path(args.relocs).write_text(result.relocs_text(), encoding="utf-8")

    if not args.quiet:
        print(f"LOOM_ASM: {args.input}: {result.n_words} word(s) emitted, "
              f"{len(words)} in image, {len(result.symbols)} symbol(s), "
              f"{len(result.relocs)} jump(s) "
              f"[{args.format}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

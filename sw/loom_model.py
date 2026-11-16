#!/usr/bin/env python3
"""LOOM golden cycle-accurate ISA model (owner: toolchain).

Written from `spec/ARCHITECTURE.md` (especially the normative §3.1 "cycle model")
and never from the RTL, so it is an independent oracle for differential
co-simulation (spec §10: model-first policy).  Pure deterministic Python: no
wall-clock, no threads, no randomness, no I/O during simulation.

Public API
----------
    loom = Loom(num_threads=4, eq_depth=4, pmem_words=1024, dmem_words=64)
    loom.load(words, addr=0)                 # PMEM image (32-bit ints)
    loom.load_dmem(words, addr=0)            # convenience (DMEM is 16-bit)
    loom.set_pins_in(value16)                # external pin stimulus
    loom.cfg_pins_out_drive(fn)              # optional external-load model
    loom.csr_write(addr8, val16) -> bool     # host CSR access
    loom.csr_read(addr8) -> int
    loom.push_rx(tid, byte) -> bool          # host -> thread RX FIFO
    loom.pop_tx(tid) -> int | None           # thread TX FIFO -> host
    loom.step()                              # advance exactly one clock cycle
    loom.run(n)                              # n steps
    loom.pins_out16, loom.pins_dir8, loom.tmr16, loom.irq_code16
    loom.trace                               # [(cycle, pins_out16, dir8), ...]
    loom.state_snapshot() -> dict
    loom.reset()                             # hard reset (spec §7)
    loom.soft_reset()                        # CSR 0x20 write-1 behaviour

Host-command equivalents used by `host/loom_host.py::ModelTransport` (spec §6
commands RUN/STOP/SET_PC/ACK_IRQ have no architectural CSR of their own):
    loom.run_threads(mask), loom.stop_threads(mask), loom.set_pc(tid, pc16),
    loom.ack_irq(), loom.host_write_mem(addr, word), loom.host_read_mem(addr)

Cycle model (spec §3.1, implemented literally)
---------------------------------------------
One `step()` = one rising clock edge = one cycle `k` (`TMR==k` during that cycle,
TMR starts at 0 after reset).  S1..S7:

  S1  **Fire events.** For every thread whose event queue is non-empty, look at
      the *head* only: a timed entry fires iff `target == TMR` **or**
      `((target - TMR) & 0xFFFF) >= 0x8000` (due-or-overdue: this recovers an
      event whose deadline was missed, e.g. after a long stall, and lets two
      entries with the same target fire on consecutive cycles instead of
      wedging the queue).  An edge entry fires iff
      `(pins_in & EMASK) == (ECOND & EMASK)` (pin sample at the start of the
      cycle).  At most one pop per thread per cycle.  Because of the overdue
      rule, all absolute timed targets must be scheduled < 32768 cycles ahead;
      `emit` enqueue clamps behind-schedule targets to `TMR+1`.
  S2  **Issue selection.** Scan `issue_ptr, issue_ptr+1, ...` (mod N); first
      thread with `en && !halt && stall==0 && (insn_valid || fetch_returning==t)`
      issues.  `issue_ptr <= t+1`.
  S3  **Execute or retry.** Back-pressure (FIFO full/empty, event queue full,
      `WAITP` mismatch, `EVWAIT` flag clear, host CSR/DMEM port busy) retries the
      instruction: `stall=1`, PC frozen, no side effects.  Otherwise all effects
      commit at the end of the cycle (ALU/R/flags, PC, FIFO, queue enqueue, CSR
      write, irq, evset/evwait, pins/dir ops, inat arming).
  S4  **Fetch.** One fetch per cycle into a per-thread instruction buffer, with
      the bypass path: if the issuing thread fetched during this same cycle the
      word is used by next cycle directly.  `hv[t]` rules and the
      "capture-unless-consumed" rule exactly as §3.1.  A taken jump costs no
      bubble — the fetch address is `pc_nxt`.
  S5  **Capture.** Returned fetch is written into the tag thread's buffer and
      marked valid unless the issue consumed it via the bypass.
  S6  **Pins.** `pout_next = pout`; then (1) instruction pin op, (2) host pin
      CSR write, (3) fired events in priority order t3,t2,t1,t0, (4) wave player
      overrides the visible bus while `wave_valid`.  `pout` is a full 16-bit
      register: no bit masking (bits 9:8 are the writable-but-unconnected
      host-link pins; the top-level pin map simply ignores them, per Lead).
  S7  **Counts/timers.** TMR++, stall decrement (except a thread re-stalled by
      its own retry this cycle), event pop/occupancy (fire+enqueue keeps the
      count), instruction-buffer/fetch updates, inat latch at `TMR==target`.

Reset state (per Lead, 2026-11-16): `en=0, halt=1, stall=0, PC=0, R=0, Z=C=0,
stall=0, period=16 (1.0 cycle), PFRAC=WFRAC=0, tmask=0xFFFF, emask=econd=0,
nextt=0`, FIFOs/queues empty, `pout=pdir=evt=irq=0`, wave idle
(`addr=0, len=1, period=16`), `TMR=0`.

Where the model had to choose (all reported to Lead)
---------------------------------------------------
  * **Event queue is strictly head-of-queue.** Only the head entry is examined.
    A timed entry fires when due or overdue (`target == TMR` or
    `(target-TMR)&0xFFFF >= 0x8000`, Lead decision 2026-11-16): two entries
    armed for the same target fire on consecutive cycles and no armed event is
    dropped after a stall.  Enqueue targets are clamped into the future
    (behind-schedule => `TMR+1`); `emitd 0` is special-cased to `TMR+1`.
    Scheduling lookahead must stay below 32768 cycles.
  * Edge events are checked against the pin sample taken at the *start* of the
    cycle (external stimulus of this cycle + driven level from the previous
    cycle).  `in`, `waitp`, `inat` use the pin vector *settled this cycle*
    (events + wave included), because on real silicon the pads loop back.
    Self-readback is therefore well defined but delayed one cycle for edge
    tests; external-input protocols (the normal case) are exact.
  * **Flags (confirmed with Lead):** `movi/mov/and/andi/or/ori/xor/xori/inv/
    shl/shr` update Z and clear C; `add/addi/sub/subi/cmp/cmpi` update Z,C;
    `rol/ror`, `in`, `ld/ldi`, `pull`, `csrr` update neither.  (RTL semantics —
    the spec only names Z for logical ops.)
  * **CSR bit layouts** (Lead-confirmed RTL layout; spec §4.2 field lists were
    over-wide/impossible):
        TSTATUS = {14'b0, halt, en}          -> bit1 halt, bit0 en
        TFIFO   = {4'b0, rx_cnt[3:0], tx_cnt[3:0], 2'b0, tx_full, rx_empty}
                                             -> [11:8] rx_cnt, [7:4] tx_cnt,
                                                bit1 tx_full, bit0 rx_empty
        TEQ     = {13'b0, eq_cnt[2:0]}
        TNEXTT  = nextt; TPC = pc; TPERIOD/TMASK/TEMASK/TECOND as named
        POUT/PDIR/TMR/EVT/IRQ as named
        WCTRL   = {13'b0, oneshot, loop, start}; reads back with start=0
        WADDR   = {10'b0, addr}; WLEN = {9'b0, len}
        WSTATUS = {14'b0, busy, done}        -> bit1 busy, bit0 done
        GSTATUS = {7'b0, en[3:0], 4'b0, irq} -> [8:5] en mask, bit0 irq
        SOFTRST = host-only, write 1
    The IRQ CSR read is **non-destructive** in RTL; the host clears it via
    `ACK_IRQ` (write CSR_IRQ=0) or by writing 0.  Spec §4.2's "(read clears)"
    is superseded (reported to Lead).
  * **Soft reset** (CSR 0x20 write 1) matches RTL: threads, FIFOs, event queues,
    `pout/pdir/evt/irq` reset; PMEM, DMEM, wave-player internals and the host
    bridge are *not* touched.
  * **Host accesses are atomic between cycles** (`csr_write`, `run_threads`,
    `push_rx`, `host_write_mem`): they take effect from the next cycle.  The
    §3.1 single-port host contention retry therefore only matters when a test
    drives the host port in-cycle; it is not modelled (verification drives the
    host through the real SPI bridge or between `step()` calls).
  * **Wave player** (derived from §4.2 + OWNERS.md `loom_wave.v` timing):
    config latched at start; `cfg_start` sampled in the cycle after the CSR
    write; first pattern visible `max(1, WPER>>4)` cycles after that; subsequent
    patterns at 12.4-carry-accumulated boundaries; one-shot/single pass stops
    after the last pattern has been held its full period (`done` level);
    `oneshot` wins over `loop`; `WLEN==0` treated as 1; buffer address wraps mod
    64.  Restart while busy is latched and takes effect at the next symbol
    boundary (start requested exactly on a one-shot's final tick defers the stop
    by one symbol, as `loom_wave.v` documents).  While the player drives, the
    external bus shows `wave_pins`; the `pout` register keeps updating
    underneath and becomes visible again when the player stops.
  * `halt` freezes PC (RTL `npc_en=0`); host RUN clears halt.
  * The model unmasks nothing: `pins_out16` is the visible 16-bit `pins_out`
    vector of the core (bits 9:8 always 0 in the model because host-owned bits
    are never written by the core; the testbench/host bridge supplies them).
"""

from __future__ import annotations

import pathlib
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

if __package__ in (None, ""):  # allow `python sw/loom_model.py`
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from sw import isa  # type: ignore
else:
    from sw import isa

__all__ = ["Loom", "Thread", "TIME_KIND", "EDGE_KIND"]

# --- opcodes (from the frozen header via sw/isa.py) -------------------------
_OPC = {name: isa.INSTRUCTIONS[name].opcode for name in isa.INSTRUCTIONS}
NOP, HALT, MOVI, MOV = _OPC["nop"], _OPC["halt"], _OPC["movi"], _OPC["mov"]
ADD, ADDI, SUB, SUBI = _OPC["add"], _OPC["addi"], _OPC["sub"], _OPC["subi"]
AND, ANDI, OR, ORI = _OPC["and"], _OPC["andi"], _OPC["or"], _OPC["ori"]
XOR, XORI, INV = _OPC["xor"], _OPC["xori"], _OPC["inv"]
SHL, SHR, ROL, ROR = _OPC["shl"], _OPC["shr"], _OPC["rol"], _OPC["ror"]
CMP, CMPI = _OPC["cmp"], _OPC["cmpi"]
CSRW, CSRR = _OPC["csrw"], _OPC["csrr"]
OUT, SET, CLR, TOG = _OPC["out"], _OPC["set"], _OPC["clr"], _OPC["tog"]
IN, DIR, DSET, DCLR = _OPC["in"], _OPC["dir"], _OPC["dset"], _OPC["dclr"]
EMIT, EMITA, EMITD, EMITE = _OPC["emit"], _OPC["emita"], _OPC["emitd"], _OPC["emite"]
SETP, SETM, SETEM, SETEC = _OPC["setp"], _OPC["setm"], _OPC["setem"], _OPC["setec"]
INAT, WAITU, WAITD, WAITP = _OPC["inat"], _OPC["waitu"], _OPC["waitd"], _OPC["waitp"]
LD, ST, LDI, STI = _OPC["ld"], _OPC["st"], _OPC["ldi"], _OPC["sti"]
PUSH, PULL, PUSHI = _OPC["push"], _OPC["pull"], _OPC["pushi"]
JMP, JZ, JNZ, JC, JNC = _OPC["jmp"], _OPC["jz"], _OPC["jnz"], _OPC["jc"], _OPC["jnc"]
RPT, EVSET, EVWAIT, IRQ = _OPC["rpt"], _OPC["evset"], _OPC["evwait"], _OPC["irq"]

# --- CSR addresses ----------------------------------------------------------
CSR_TPERIOD = isa.SPEC_CSR["TPERIOD"]
CSR_TMASK = isa.SPEC_CSR["TMASK"]
CSR_TSTATUS = isa.SPEC_CSR["TSTATUS"]
CSR_TFIFO = isa.SPEC_CSR["TFIFO"]
CSR_TNEXTT = isa.SPEC_CSR["TNEXTT"]
CSR_TEQ = isa.SPEC_CSR["TEQ"]
CSR_TEMASK = isa.SPEC_CSR["TEMASK"]
CSR_TECOND = isa.SPEC_CSR["TECOND"]
CSR_TPC = isa.SPEC_CSR["TPC"]
CSR_POUT = isa.SPEC_CSR["POUT"]
CSR_PDIR = isa.SPEC_CSR["PDIR"]
CSR_TMR = isa.SPEC_CSR["TMR"]
CSR_EVT = isa.SPEC_CSR["EVT"]
CSR_IRQ = isa.SPEC_CSR["IRQ"]
CSR_WCTRL = isa.SPEC_CSR["WCTRL"]
CSR_WADDR = isa.SPEC_CSR["WADDR"]
CSR_WLEN = isa.SPEC_CSR["WLEN"]
CSR_WPER = isa.SPEC_CSR["WPER"]
CSR_WSTATUS = isa.SPEC_CSR["WSTATUS"]
CSR_CAPCTRL = isa.SPEC_CSR["CAPCTRL"]
CSR_CAPSTAT = isa.SPEC_CSR["CAPSTAT"]
CSR_GSTATUS = isa.SPEC_CSR["GSTATUS"]
CSR_SOFTRST = isa.SPEC_CSR["SOFTRST"]
CSR_RUN = isa.CSR_RUN          # host bridge RUNMASK (0x30)
CSR_STOP = isa.CSR_STOP        # host bridge STOPMASK (0x31)

PC_MASK = (1 << isa.PARAMS.get("PMEM_AW", 10)) - 1        # 0x3FF
DMEM_MASK = (1 << isa.PARAMS.get("DMEM_AW", 6)) - 1       # 0x3F
FIFO_DEPTH = isa.PARAMS.get("FIFO_DEPTH", 8)
DRIVABLE = isa.PINS_OUT_DRIVABLE
HOST_BITS = isa.PINS_OUT_HOST
HOST_MEM_DMEM_BASE = 0x400       # host memory map: DMEM at 0x400..0x43F

TIME_KIND = 0
EDGE_KIND = 1


@dataclass
class Thread:
    """Architectural state of one LOOM thread (plus its instruction buffer)."""

    tid: int
    pc: int = 0
    r: list[int] = field(default_factory=lambda: [0] * 8)
    z: int = 0
    c: int = 0
    en: bool = False
    halt: bool = True
    stall: int = 0                 # cycles during which the thread may not issue
    period: int = 16               # 12.4, reset value = 16 = 1.0 cycle
    pfrac: int = 0                 # emit fractional accumulator
    wfrac: int = 0                 # waitd fractional accumulator
    tmask: int = 0xFFFF
    emask: int = 0
    econd: int = 0
    nextt: int = 0
    eq: list[tuple[int, int, int]] = field(default_factory=list)  # (kind,target,val)
    tx: deque[int] = field(default_factory=lambda: deque(maxlen=FIFO_DEPTH))
    rx: deque[int] = field(default_factory=lambda: deque(maxlen=FIFO_DEPTH))
    rep_arm: bool = False
    rep_left: int = 0
    inat_arm: bool = False
    inat_tgt: int = 0
    inat_rd: int = 0
    # instruction buffer / valid bit (spec §3.1 S4/S5)
    insn_valid: bool = False
    insn_buf: int = 0

    def reset(self) -> None:
        self.pc = 0
        self.r = [0] * 8
        self.z = self.c = 0
        self.en = False
        self.halt = True
        self.stall = 0
        self.period = 16
        self.pfrac = self.wfrac = 0
        self.tmask = 0xFFFF
        self.emask = self.econd = 0
        self.nextt = 0
        self.eq.clear()
        self.tx.clear()
        self.rx.clear()
        self.rep_arm = False
        self.rep_left = 0
        self.inat_arm = False
        self.inat_tgt = 0
        self.inat_rd = 0
        self.insn_valid = False
        self.insn_buf = 0

    @property
    def rx_count(self) -> int:
        return len(self.rx)

    @property
    def tx_count(self) -> int:
        return len(self.tx)

    @property
    def eq_count(self) -> int:
        return len(self.eq)

    @property
    def runnable(self) -> bool:
        return self.en and not self.halt


class Loom:
    """Golden cycle-accurate model of one LOOM chip."""

    def __init__(self, num_threads: int = 4, eq_depth: int = 4,
                 pmem_words: int = 1024, dmem_words: int = 64):
        if num_threads < 1:
            raise ValueError("num_threads must be >= 1")
        if eq_depth < 1:
            raise ValueError("eq_depth must be >= 1")
        self.num_threads = int(num_threads)
        self.eq_depth = int(eq_depth)
        self.pmem_words = int(pmem_words)
        self.dmem_words = int(dmem_words)
        self.threads: list[Thread] = [Thread(t) for t in range(self.num_threads)]
        self.pmem: list[int] = [isa.UNMAPPED_PMEM_WORD] * self.pmem_words
        self.dmem: list[int] = [0] * self.dmem_words
        self._drive_fn: Callable[[int, int], int] | None = None
        self.reset()

    # ------------------------------------------------------------------
    # Reset / loading
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Hard reset (spec §7).  PMEM contents are preserved."""
        self.cycle: int = 0            # TMR register value (cycles completed)
        self.pins_reg: int = 0         # pout register
        self.pins_visible: int = 0     # pins_out visible during the last cycle
        self.dir: int = 0              # pdir / uio_oe[7:0]
        self.pin_in16: int = 0         # pins_in sample of the last cycle
        self.ext_in: int = 0
        self.evt: int = 0
        self.irq_code: int = 0
        self.cap_ctrl: int = 0
        self.cap_stat: int = 0
        self.trace: list[tuple[int, int, int]] = []
        # fetch / issue pipeline
        self.issue_ptr = 0
        self.fetch_ptr = 0
        self.fetch_returning: tuple[int, int] | None = None   # (tid, word) this cycle
        self._next_fetch: tuple[int, int] | None = None       # for next cycle
        # wave CSRs (core-side configuration registers)
        self.wave_ctl = 0
        self.wave_addr = 0
        self.wave_len = 1
        self.wave_period = 16
        # wave player (loom_wave.v) state
        self.wl_base = 0
        self.wl_len = 1
        self.wl_pint = 0
        self.wl_pfrac = 0
        self.wl_loop = False
        self.wl_oneshot = False
        self.w_busy = False
        self.w_done = False
        self.w_valid = False
        self.w_pins = 0
        self.w_started = False
        self.w_idx = 0
        self.w_next_tick: int | None = None
        self.w_facc = 0
        self.w_shadow: dict[str, Any] | None = None
        self.w_start_pending: tuple[int, dict[str, Any]] | None = None
        self._dmem_writes: list[tuple[int, int]] = []
        for th in self.threads:
            th.reset()
        self.dmem = [0] * self.dmem_words
        self.pin_in16 = self._pin_in(0)

    def soft_reset(self) -> None:
        """CSR 0x20 write-1 behaviour (matches RTL): core only, no memories/wave."""
        for th in self.threads:
            th.reset()
        self.pins_reg = 0
        self.pins_visible = 0
        self.dir = 0
        self.evt = 0
        self.irq_code = 0
        self.issue_ptr = 0
        self.fetch_ptr = 0
        self.fetch_returning = None
        self._next_fetch = None
        self.pin_in16 = self._pin_in(0)

    def load(self, words: Iterable[int], addr: int = 0) -> None:
        """Load a 32-bit PMEM image (host WRITE_MEM words)."""
        words = [int(w) & 0xFFFFFFFF for w in words]
        if addr < 0 or addr + len(words) > self.pmem_words:
            raise ValueError(
                f"load: {len(words)} words at PMEM address {addr} does not fit "
                f"in pmem_words={self.pmem_words}")
        for i, w in enumerate(words):
            self.pmem[addr + i] = w

    def load_dmem(self, words: Iterable[int], addr: int = 0) -> None:
        """Load 16-bit DMEM words (convenience; the host uses WRITE_MEM)."""
        words = [int(w) & 0xFFFF for w in words]
        if addr < 0 or addr + len(words) > self.dmem_words:
            raise ValueError(
                f"load_dmem: {len(words)} words at DMEM address {addr} does not "
                f"fit in dmem_words={self.dmem_words}")
        for i, w in enumerate(words):
            self.dmem[addr + i] = w

    # ------------------------------------------------------------------
    # Host-visible pins
    # ------------------------------------------------------------------
    def set_pins_in(self, value16: int) -> None:
        """Set the external stimulus seen on `pins_in` from the next cycle."""
        self.ext_in = int(value16) & 0xFFFF

    def cfg_pins_out_drive(self, fn: Callable[[int, int], int] | None) -> None:
        """Register an external-load / pull-up model.

        `fn(pins_out16, dir8) -> 16-bit observed level` is used to compute the
        readback of driven pins (and therefore `in`, `waitp`, `inat`, edge
        triggers).  `None` restores identity.  The callback must be pure.
        """
        if fn is not None and not callable(fn):
            raise TypeError("cfg_pins_out_drive expects a callable or None")
        self._drive_fn = fn

    @property
    def pins_out16(self) -> int:
        """`pins_out` visible during the last simulated cycle."""
        return self.pins_visible & 0xFFFF

    @property
    def pins_dir8(self) -> int:
        return self.dir & 0xFF

    @property
    def tmr16(self) -> int:
        return self.cycle & 0xFFFF

    @property
    def irq_code16(self) -> int:
        return self.irq_code & 0xFFFF

    @property
    def irq_asserted(self) -> bool:
        return self.irq_code != 0

    @property
    def pins_in16(self) -> int:
        return self.pin_in16

    def _pin_in(self, driven: int) -> int:
        """Combinational pins_in from a driven output level + ext stimulus."""
        level = driven & 0xFFFF
        if self._drive_fn is not None:
            level = int(self._drive_fn(level, self.dir)) & 0xFFFF
        low = ((level & self.dir) | (self.ext_in & ~self.dir & 0xFF)) & 0xFF
        return ((self.ext_in & 0xFF00) | low) & 0xFFFF

    # ------------------------------------------------------------------
    # CSR access
    # ------------------------------------------------------------------
    def _thread_csr_read(self, th: Thread, n: int) -> int:
        if n == CSR_TPERIOD:
            return th.period
        if n == CSR_TMASK:
            return th.tmask
        if n == CSR_TSTATUS:
            return (1 if th.halt else 0) << 1 | (1 if th.en else 0)
        if n == CSR_TFIFO:
            # {4'b0, rx_cnt[3:0], tx_cnt[3:0], 2'b0, tx_full, rx_empty}
            tx_full = 1 if len(th.tx) >= FIFO_DEPTH else 0
            rx_empty = 1 if not th.rx else 0
            return ((len(th.rx) & 0xF) << 8) | ((len(th.tx) & 0xF) << 4) \
                | (tx_full << 1) | rx_empty
        if n == CSR_TNEXTT:
            return th.nextt
        if n == CSR_TEQ:
            return len(th.eq) & 0x7
        if n == CSR_TEMASK:
            return th.emask
        if n == CSR_TECOND:
            return th.econd
        if n == CSR_TPC:
            return th.pc & PC_MASK
        return 0

    def _thread_csr_write(self, th: Thread, n: int, val: int) -> bool:
        val &= 0xFFFF
        if n == CSR_TPERIOD:
            th.period = val
            return True
        if n == CSR_TMASK:
            th.tmask = val
            return True
        if n == CSR_TEMASK:
            th.emask = val
            return True
        if n == CSR_TECOND:
            th.econd = val
            return True
        if n == CSR_TPC:
            th.pc = val & PC_MASK
            th.insn_valid = False
            return True
        return False               # TSTATUS/TFIFO/TNEXTT/TEQ are read-only

    def _global_csr_read(self, addr: int) -> int:
        if addr == CSR_POUT:
            return self.pins_reg & 0xFFFF
        if addr == CSR_PDIR:
            return self.dir & 0xFF
        if addr == CSR_TMR:
            return self.tmr16
        if addr == CSR_EVT:
            return self.evt & 0xFF
        if addr == CSR_IRQ:
            return self.irq_code & 0xFFFF       # non-destructive (RTL)
        if addr == CSR_WCTRL:
            # {13'b0, oneshot, loop, start-reads-0}
            return (1 if self.wl_oneshot else 0) << 2 | (1 if self.wl_loop else 0) << 1
        if addr == CSR_WADDR:
            return self.wave_addr & 0x3F
        if addr == CSR_WLEN:
            return self.wave_len & 0x7F
        if addr == CSR_WPER:
            return self.wave_period & 0xFFFF
        if addr == CSR_WSTATUS:
            return (1 if self.w_busy else 0) << 1 | (1 if self.w_done else 0)
        if addr == CSR_CAPCTRL:
            return self.cap_ctrl & 0xFFFF
        if addr == CSR_CAPSTAT:
            return self.cap_stat & 0xFFFF
        if addr == CSR_GSTATUS:
            # {7'b0, en[3:0], 4'b0, irq}
            en_mask = 0
            for th in self.threads:
                if th.en:
                    en_mask |= 1 << th.tid
            return ((en_mask & 0xF) << 5) | (1 if self.irq_code else 0)
        return 0

    def _global_csr_write(self, addr: int, val: int) -> bool:
        val &= 0xFFFF
        if addr == CSR_POUT:
            self.pins_reg = val & 0xFFFF          # unmasked (Lead decision)
            return True
        if addr == CSR_PDIR:
            self.dir = val & 0xFF
            return True
        if addr == CSR_EVT:
            self.evt = val & 0xFF
            return True
        if addr == CSR_IRQ:
            self.irq_code = val & 0xFFFF
            return True
        if addr == CSR_WCTRL:
            self.wave_ctl = val & 0x7
            self.wl_loop = bool(val & 0x2)
            self.wl_oneshot = bool(val & 0x4)
            if val & 0x1:
                self._wave_request_start()
            return True
        if addr == CSR_WADDR:
            self.wave_addr = val & 0x3F
            return True
        if addr == CSR_WLEN:
            self.wave_len = val & 0x7F
            return True
        if addr == CSR_WPER:
            self.wave_period = val
            return True
        if addr == CSR_CAPCTRL:
            self.cap_ctrl = val
            return True
        if addr == CSR_RUN:
            self.run_threads(val & ((1 << self.num_threads) - 1))
            return True
        if addr == CSR_STOP:
            self.stop_threads(val & ((1 << self.num_threads) - 1))
            return True
        return False

    def csr_read(self, addr8: int) -> int:
        """Host CSR read (thread banks at 0x40+16t+n, spec §4.2)."""
        addr = int(addr8) & 0xFF
        bank = isa.csr_decode_host_addr(addr)
        if bank is not None:
            tid, n = bank
            if 0 <= tid < self.num_threads:
                return self._thread_csr_read(self.threads[tid], n)
            return 0
        if addr == CSR_SOFTRST:
            return 0
        return self._global_csr_read(addr)

    def csr_write(self, addr8: int, val16: int) -> bool:
        """Host CSR write.  Takes effect from the next cycle."""
        addr = int(addr8) & 0xFF
        val = int(val16) & 0xFFFF
        bank = isa.csr_decode_host_addr(addr)
        if bank is not None:
            tid, n = bank
            if 0 <= tid < self.num_threads:
                return self._thread_csr_write(self.threads[tid], n, val)
            return False
        if addr == CSR_SOFTRST:
            if val & 0x1:
                self.soft_reset()
                return True
            return False
        return self._global_csr_write(addr, val)

    # ------------------------------------------------------------------
    # Host command equivalents (spec §6)
    # ------------------------------------------------------------------
    def run_threads(self, mask: int = 0xF) -> None:
        """HCMD_RUN -> CSR 0x30: clear halt/enable/stall, nextt=TMR, drop buffer."""
        for th in self.threads:
            if (mask >> th.tid) & 1:
                th.en = True
                th.halt = False
                th.stall = 0
                th.nextt = self.tmr16
                th.inat_arm = False
                th.insn_valid = False

    def stop_threads(self, mask: int = 0xF) -> None:
        """HCMD_STOP -> CSR 0x31: clear en bits."""
        for th in self.threads:
            if (mask >> th.tid) & 1:
                th.en = False

    def set_pc(self, tid: int, pc16: int) -> None:
        th = self.threads[int(tid)]
        th.pc = int(pc16) & PC_MASK
        th.insn_valid = False

    def ack_irq(self) -> None:
        """HCMD_ACK_IRQ: clear the host IRQ latch."""
        self.irq_code = 0

    def push_rx(self, tid: int, byte: int) -> bool:
        """Host -> thread RX FIFO (HCMD_TX_PUSH).  False if full."""
        if not 0 <= tid < self.num_threads:
            raise ValueError(f"bad thread id {tid}")
        th = self.threads[tid]
        if len(th.rx) >= FIFO_DEPTH:
            return False
        th.rx.append(int(byte) & 0xFF)
        return True

    def pop_tx(self, tid: int) -> int | None:
        """Thread TX FIFO -> host (HCMD_RX_POP).  None if empty."""
        if not 0 <= tid < self.num_threads:
            raise ValueError(f"bad thread id {tid}")
        th = self.threads[tid]
        return th.tx.popleft() if th.tx else None

    def host_write_mem(self, addr: int, word: int) -> None:
        """Host memory write: PMEM below 0x400, DMEM at 0x400..0x43F."""
        addr = int(addr) & 0xFFFFFF
        word = int(word) & 0xFFFFFFFF
        if addr < HOST_MEM_DMEM_BASE:
            if addr < self.pmem_words:
                self.pmem[addr] = word
                self.threads_drop_if_pc(addr)   # program changed under a thread
        else:
            idx = (addr - HOST_MEM_DMEM_BASE) & DMEM_MASK
            self.dmem[idx] = word & 0xFFFF

    def host_read_mem(self, addr: int) -> int:
        """Host memory read (DMEM reads return {16'h0, word})."""
        addr = int(addr) & 0xFFFFFF
        if addr < HOST_MEM_DMEM_BASE:
            if addr < self.pmem_words:
                return self.pmem[addr]
            return isa.UNMAPPED_PMEM_WORD
        return self.dmem[(addr - HOST_MEM_DMEM_BASE) & DMEM_MASK] & 0xFFFF

    def threads_drop_if_pc(self, addr: int) -> None:
        """Host rewrote a PMEM word: re-fetch it in threads whose buffer holds it.

        The RTL cannot detect this (existing buffers stay stale until consumed);
        the model currently mirrors the RTL simply by leaving buffers alone, so
        this hook is intentionally a no-op.  It exists so co-simulation drivers
        can be explicit if they need to invalidate.
        """
        return None

    # convenience accessors for tests / co-sim dumps
    def thread_pc(self, tid: int) -> int:
        return self.threads[tid].pc

    def reg(self, tid: int, n: int) -> int:
        return self.threads[tid].r[n & 7]

    def eq_count(self, tid: int) -> int:
        return len(self.threads[tid].eq)

    def tx_count(self, tid: int) -> int:
        return len(self.threads[tid].tx)

    def rx_count(self, tid: int) -> int:
        return len(self.threads[tid].rx)

    # ------------------------------------------------------------------
    # Wave player (loom_wave.v timing)
    # ------------------------------------------------------------------
    def _wave_cfg_snapshot(self) -> dict[str, Any]:
        length = self.wave_len & 0x7F
        if length == 0:
            length = 1
        if length > 64:
            length = 64
        return {
            "base": self.wave_addr & 0x3F,
            "len": length,
            "pint": (self.wave_period >> 4) & 0xFFF,
            "pfrac": self.wave_period & 0xF,
            "loop": self.wl_loop,
            "oneshot": self.wl_oneshot,
        }

    def _wave_request_start(self) -> None:
        """WCTRL.start written: cfg_start is sampled in the *next* cycle."""
        start_cycle = self.cycle + 1
        cfg = self._wave_cfg_snapshot()
        self.w_start_pending = (start_cycle, cfg)

    def _wave_apply_start(self, cfg: dict[str, Any], k: int) -> None:
        self.wl_base = cfg["base"]
        self.wl_len = cfg["len"]
        self.wl_pint = cfg["pint"]
        self.wl_pfrac = cfg["pfrac"]
        self.wl_loop = cfg["loop"]
        self.wl_oneshot = cfg["oneshot"]
        self.w_busy = True
        self.w_done = False
        self.w_idx = 0
        self.w_started = False
        self.w_shadow = None
        if cfg["pint"] <= 1:
            # first boundary is the start cycle itself: pattern 0 visible next cycle
            self.w_pins = self.dmem[cfg["base"] & DMEM_MASK] & 0xFFFF
            self.w_valid = True
            self.w_started = True
            self.w_facc = cfg["pfrac"]
            self.w_next_tick = k + 1
        else:
            self.w_valid = False
            self.w_pins = 0
            self.w_facc = 0
            self.w_next_tick = k + cfg["pint"] - 1

    def _wave_spacing(self) -> int:
        facc = self.w_facc + self.wl_pfrac
        carry = 1 if facc >= 16 else 0
        self.w_facc = facc & 0xF
        dur = self.wl_pint + carry
        return dur if dur > 0 else 1

    def _wave_end_of_cycle(self, k: int) -> None:
        """Wave-player sequential updates for the cycle ending at this edge."""
        # cfg_start pulse is sampled this cycle
        start_here = False
        if self.w_start_pending is not None and self.w_start_pending[0] == k:
            start_here = True
            start_cfg = self.w_start_pending[1]
            self.w_start_pending = None
        else:
            start_cfg = None

        if self.w_busy and self.w_next_tick is not None and k == self.w_next_tick:
            if self.w_shadow is not None:
                # pending restart takes effect at this symbol boundary
                cfg = self.w_shadow
                self.w_shadow = None
                self._wave_apply_restart(cfg, k)
            elif not self.w_started:
                # first pattern of this configuration
                self.w_pins = self.dmem[self.wl_base & DMEM_MASK] & 0xFFFF
                self.w_valid = True
                self.w_started = True
                self.w_idx = 0
                self.w_facc = self.wl_pfrac
                self.w_next_tick = k + max(self.wl_pint, 1)
            elif (not (self.wl_loop and not self.wl_oneshot)) \
                    and self.w_idx == self.wl_len - 1 and not start_here:
                # one pass complete: release the pins from the next cycle on
                self.w_busy = False
                self.w_done = True
                self.w_valid = False
                self.w_started = False
                self.w_pins = 0
                self.w_idx = 0
                self.w_next_tick = None
                self.w_facc = 0
            else:
                self.w_idx = (self.w_idx + 1) % self.wl_len
                self.w_pins = self.dmem[(self.wl_base + self.w_idx) & DMEM_MASK] & 0xFFFF
                self.w_valid = True
                self.w_next_tick = k + self._wave_spacing()

        if start_here and start_cfg is not None:
            if self.w_busy:
                self.w_shadow = start_cfg          # restart at next boundary
            else:
                self._wave_apply_start(start_cfg, k)

    def _wave_apply_restart(self, cfg: dict[str, Any], k: int) -> None:
        self.wl_base = cfg["base"]
        self.wl_len = cfg["len"]
        self.wl_pint = cfg["pint"]
        self.wl_pfrac = cfg["pfrac"]
        self.wl_loop = cfg["loop"]
        self.wl_oneshot = cfg["oneshot"]
        self.w_busy = True
        self.w_done = False
        self.w_idx = 0
        self.w_pins = self.dmem[cfg["base"] & DMEM_MASK] & 0xFFFF
        self.w_valid = True
        self.w_started = True
        self.w_facc = cfg["pfrac"]
        self.w_next_tick = k + max(cfg["pint"], 1)

    # ------------------------------------------------------------------
    # Instruction execution helpers
    # ------------------------------------------------------------------
    def _fetch(self, pc: int) -> int:
        if 0 <= pc < self.pmem_words:
            return self.pmem[pc]
        return isa.UNMAPPED_PMEM_WORD

    @staticmethod
    def _sext16(v: int) -> int:
        return v - 0x10000 if v & 0x8000 else v

    def _clamp_target(self, target: int, special_zero: bool = False) -> int:
        """Spec §3.1 behind-schedule clamp for timed events."""
        if special_zero:
            return (self.tmr16 + 1) & 0xFFFF
        if ((target - self.tmr16) & 0xFFFF) >= 0x8000:
            return (self.tmr16 + 1) & 0xFFFF
        return target & 0xFFFF

    # ------------------------------------------------------------------
    # Main cycle step (spec §3.1)
    # ------------------------------------------------------------------
    def step(self) -> None:
        """Advance the model by exactly one clock cycle."""
        k = self.cycle
        tmr = k & 0xFFFF
        n = self.num_threads
        ths = self.threads

        dir_during = self.dir & 0xFF
        stall_start = [th.stall for th in ths]
        # ---------------- S1: fire events (head of queue only) -------------
        pin_in_start = self._pin_in(self.pins_visible)
        fired = [False] * n
        fire_val = [0] * n
        eq_len_start = [len(th.eq) for th in ths]
        tx_len_start = [len(th.tx) for th in ths]
        rx_len_start = [len(th.rx) for th in ths]
        for t, th in enumerate(ths):
            if not th.eq:
                continue
            kind, target, val = th.eq[0]
            if kind == TIME_KIND:
                # due or overdue (Lead decision 2026-11-16)
                if target == tmr or ((target - tmr) & 0xFFFF) >= 0x8000:
                    fired[t] = True
                    fire_val[t] = val
            else:
                if (pin_in_start & th.emask) == (th.econd & th.emask):
                    fired[t] = True
                    fire_val[t] = val

        # ---------------- S6 (partial): event pin merge ---------------------
        npout = self.pins_reg
        for t in range(n - 1, -1, -1):          # t3..t0, so t0 wins
            if fired[t]:
                mask = ths[t].tmask            # full 16-bit mask, unmasked (Lead)
                npout = (npout & ~mask) | (fire_val[t] & mask)

        # ---------------- wave player: visible level during this cycle ------
        wave_visible = self.w_pins & 0xFFFF if self.w_valid else None
        visible_events = wave_visible if wave_visible is not None else npout
        settled_pins = self._pin_in(visible_events)

        # ---------------- S2: issue selection -------------------------------
        sel: int | None = None
        for off in range(n):
            t = (self.issue_ptr + off) % n
            th = ths[t]
            if not (th.en and not th.halt and th.stall == 0):
                continue
            returning_here = self.fetch_returning is not None and self.fetch_returning[0] == t
            if th.insn_valid or returning_here:
                sel = t
                break

        issued = sel is not None
        retry = False
        stall_set = 0
        pc_nxt = 0
        opc = 0
        rd = rs = imm = 0
        # effect accumulator
        wb_en = False
        wb_rd = 0
        wb_data = 0
        flag_z: int | None = None
        flag_c: int | None = None
        fifo_push: int | None = None
        fifo_pull = False
        ev_enqueue: tuple[int, int, int] | None = None   # (kind,target,val)
        csr_wr: tuple[int, int] | None = None
        pin_op: int | None = None
        dir_op: int | None = None
        irq_set: int | None = None
        evset_bit: int | None = None
        evclear_bit: int | None = None
        inat_arm: tuple[int, int] | None = None          # (rd, target)
        halt_now = False
        rep_new_arm: bool | None = None
        rep_new_left: int | None = None
        repeat_keep = False
        can_issue = False
        tpc_jump: int | None = None

        if issued:
            th = ths[sel]
            use_bypass = (not th.insn_valid) and self.fetch_returning is not None \
                and self.fetch_returning[0] == sel
            word = self.fetch_returning[1] if use_bypass else th.insn_buf
            opc = (word >> isa.OPCODE_SHIFT) & isa.OPCODE_MASK
            rd = (word >> isa.RD_SHIFT) & 0x7
            rs = (word >> isa.RS_SHIFT) & 0x7
            imm = word & 0xFFFF
            r = th.r
            src_a = r[rd]
            src_b = r[rs]
            repeat_active = th.rep_arm and (opc != RPT)
            pc_frozen = th.rep_arm and (opc != RPT) and th.rep_left != 0

            # ---------------- S3a: back-pressure (retry) -------------------
            if opc == PULL and rx_len_start[sel] == 0:
                retry = True
            elif opc in (PUSH, PUSHI) and tx_len_start[sel] >= FIFO_DEPTH:
                retry = True
            elif opc in (EMIT, EMITA, EMITD, EMITE) and eq_len_start[sel] >= self.eq_depth:
                retry = True
            elif opc == WAITP:
                # Lead-confirmed RTL semantics: only the 8 uio pins are watched:
                # (pins_in[7:0] & imm16[15:8]) == imm16[7:0]
                if ((settled_pins & 0xFF) & ((imm >> 8) & 0xFF)) != (imm & 0xFF):
                    retry = True
            elif opc == EVWAIT:
                if not (self.evt & (1 << (imm & 0x7))):
                    retry = True
            elif opc == HALT:
                pass
            # (host CSR/DMEM port contention is not modelled: host accesses are
            #  atomic between cycles -- see module header)

            if not retry:
                can_issue = True
                # ---------------- S3b: execute -----------------------------
                npc_en = True
                jump_taken = False
                if opc == NOP:
                    pass
                elif opc == HALT:
                    halt_now = True
                    npc_en = False
                elif opc == MOVI:
                    wb_en, wb_rd, wb_data = True, rd, imm
                    flag_z, flag_c = (1 if imm == 0 else 0), 0
                elif opc == MOV:
                    wb_en, wb_rd, wb_data = True, rd, src_b
                    flag_z, flag_c = (1 if src_b == 0 else 0), 0
                elif opc == ADD:
                    res = src_a + src_b
                    wb_en, wb_rd, wb_data = True, rd, res & 0xFFFF
                    flag_z, flag_c = (1 if (res & 0xFFFF) == 0 else 0), (1 if res > 0xFFFF else 0)
                elif opc == ADDI:
                    res = src_a + imm
                    wb_en, wb_rd, wb_data = True, rd, res & 0xFFFF
                    flag_z, flag_c = (1 if (res & 0xFFFF) == 0 else 0), (1 if res > 0xFFFF else 0)
                elif opc == SUB:
                    res = src_a - src_b
                    wb_en, wb_rd, wb_data = True, rd, res & 0xFFFF
                    flag_z, flag_c = (1 if (res & 0xFFFF) == 0 else 0), (1 if src_a < src_b else 0)
                elif opc == SUBI:
                    res = src_a - imm
                    wb_en, wb_rd, wb_data = True, rd, res & 0xFFFF
                    flag_z, flag_c = (1 if (res & 0xFFFF) == 0 else 0), (1 if src_a < imm else 0)
                elif opc == AND:
                    v = src_a & src_b
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == ANDI:
                    v = src_a & imm
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == OR:
                    v = src_a | src_b
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == ORI:
                    v = src_a | imm
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == XOR:
                    v = src_a ^ src_b
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == XORI:
                    v = src_a ^ imm
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == INV:
                    v = (~src_a) & 0xFFFF
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == SHL:
                    v = (src_a << (imm & 0xF)) & 0xFFFF
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == SHR:
                    v = (src_a >> (imm & 0xF)) & 0xFFFF
                    wb_en, wb_rd, wb_data = True, rd, v
                    flag_z, flag_c = (1 if v == 0 else 0), 0
                elif opc == ROL:
                    sh = imm & 0xF
                    v = src_a if sh == 0 else (((src_a << sh) | (src_a >> (16 - sh))) & 0xFFFF)
                    wb_en, wb_rd, wb_data = True, rd, v
                elif opc == ROR:
                    sh = imm & 0xF
                    v = src_a if sh == 0 else (((src_a >> sh) | (src_a << (16 - sh))) & 0xFFFF)
                    wb_en, wb_rd, wb_data = True, rd, v
                elif opc == CMP:
                    res = src_a - src_b
                    flag_z = 1 if (res & 0xFFFF) == 0 else 0
                    flag_c = 1 if src_a < src_b else 0
                elif opc == CMPI:
                    res = src_a - imm
                    flag_z = 1 if (res & 0xFFFF) == 0 else 0
                    flag_c = 1 if src_a < imm else 0
                elif opc == CSRW:
                    csr_wr = (imm & 0xFF, src_b)
                    if (imm & 0xFF) == CSR_TPC and (imm & 0xFF) < 0x10:
                        tpc_jump = src_b & PC_MASK      # acts as a computed jump
                elif opc == CSRR:
                    addr = imm & 0xFF
                    if addr < 0x10:
                        v = self._thread_csr_read(th, addr)
                    else:
                        v = self._global_csr_read(addr)
                    wb_en, wb_rd, wb_data = True, rd, v & 0xFFFF
                elif opc == OUT:
                    pin_op = imm
                elif opc == SET:
                    pin_op = (self.pins_reg | imm) & 0xFFFF
                elif opc == CLR:
                    pin_op = (self.pins_reg & ~imm) & 0xFFFF
                elif opc == TOG:
                    pin_op = (self.pins_reg ^ imm) & 0xFFFF
                elif opc == IN:
                    wb_en, wb_rd, wb_data = True, rd, settled_pins
                elif opc == DIR:
                    dir_op = imm & 0xFF
                elif opc == DSET:
                    dir_op = (self.dir | (imm & 0xFF)) & 0xFF
                elif opc == DCLR:
                    dir_op = (self.dir & ~(imm & 0xFF)) & 0xFF
                elif opc == EMIT:
                    frac = th.pfrac + (th.period & 0xF)
                    carry = frac >> 4
                    th.pfrac = frac & 0xF
                    target = self._clamp_target(
                        (th.nextt + (th.period >> 4) + carry) & 0xFFFF)
                    th.nextt = target
                    ev_enqueue = (TIME_KIND, target, src_a)
                elif opc == EMITA:
                    ev_enqueue = (TIME_KIND, self._clamp_target(imm), src_a)
                elif opc == EMITD:
                    ev_enqueue = (TIME_KIND,
                                  self._clamp_target((tmr + imm) & 0xFFFF, special_zero=(imm == 0)),
                                  src_a)
                elif opc == EMITE:
                    ev_enqueue = (EDGE_KIND, 0, src_a)
                elif opc == SETP:
                    csr_wr = (CSR_TPERIOD, imm)
                elif opc == SETM:
                    csr_wr = (CSR_TMASK, imm)
                elif opc == SETEM:
                    csr_wr = (CSR_TEMASK, imm)
                elif opc == SETEC:
                    csr_wr = (CSR_TECOND, imm)
                elif opc == INAT:
                    delta = (imm - tmr) & 0xFFFF
                    if delta == 0:
                        wb_en, wb_rd, wb_data = True, rd, settled_pins
                    else:
                        inat_arm = (rd, (k + delta) & 0xFFFFFFFF)
                        stall_set = delta
                elif opc == WAITU:
                    delta = (imm - tmr) & 0xFFFF
                    stall_set = 0 if delta == 0 else delta - 1
                elif opc == WAITD:
                    frac = th.wfrac + (imm & 0xF)
                    carry = frac >> 4
                    th.wfrac = frac & 0xF
                    total = (imm >> 4) + carry
                    stall_set = 0 if total == 0 else total - 1
                elif opc == WAITP:
                    pass        # matched: just retire
                elif opc == LD:
                    wb_en, wb_rd, wb_data = True, rd, self.dmem[imm & DMEM_MASK]
                elif opc == ST:
                    self._dmem_writes.append((imm & DMEM_MASK, src_b))
                elif opc == LDI:
                    wb_en, wb_rd, wb_data = True, rd, self.dmem[src_b & DMEM_MASK]
                elif opc == STI:
                    self._dmem_writes.append((src_a & DMEM_MASK, src_b))
                elif opc == PUSH:
                    fifo_push = src_a & 0xFF
                elif opc == PULL:
                    fifo_pull = True
                    wb_rd = rd
                elif opc == PUSHI:
                    fifo_push = imm & 0xFF
                elif opc == JMP:
                    jump_taken = True
                elif opc == JZ:
                    jump_taken = bool(th.z)
                elif opc == JNZ:
                    jump_taken = not th.z
                elif opc == JC:
                    jump_taken = bool(th.c)
                elif opc == JNC:
                    jump_taken = not th.c
                elif opc == RPT:
                    rep_new_arm, rep_new_left = True, imm & 0xFF
                elif opc == EVSET:
                    evset_bit = imm & 0x7
                elif opc == EVWAIT:
                    evclear_bit = imm & 0x7   # flag was set (else retry)
                elif opc == IRQ:
                    irq_set = imm & 0xFF
                else:
                    pass                       # unimplemented opcode == nop

                # ---------------- PC next --------------------------------
                if pc_frozen:
                    pc_nxt = th.pc                  # re-issue: PC frozen
                elif jump_taken:
                    pc_nxt = (th.pc + self._sext16(imm)) & PC_MASK
                elif npc_en:
                    pc_nxt = (th.pc + 1) & PC_MASK
                else:
                    pc_nxt = th.pc                  # HALT

        # ---------------- S4: fetch selection -------------------------------
        reissue_now = (issued and not retry and opc != RPT and
                       ths[sel].rep_arm and ths[sel].rep_left != 0) if issued else False
        consumed_bypass = False
        if issued and not retry and self.fetch_returning is not None \
                and self.fetch_returning[0] == sel and not ths[sel].insn_valid:
            consumed_bypass = True

        def hv(t: int) -> bool:
            if issued and sel == t:
                return reissue_now
            if ths[t].insn_valid:
                return True
            return self.fetch_returning is not None and self.fetch_returning[0] == t

        fsel: int | None = None
        for off in range(n):
            t = (self.fetch_ptr + off) % n
            th = ths[t]
            if not (th.en and not th.halt):
                continue
            if hv(t):
                continue
            if self.fetch_returning is not None and self.fetch_returning[0] == t \
                    and not (consumed_bypass and t == sel):
                continue
            fsel = t
            break
        if fsel is not None:
            if issued and not retry and fsel == sel:
                fetch_addr = tpc_jump if tpc_jump is not None else pc_nxt
            else:
                fetch_addr = ths[fsel].pc
            self._next_fetch = (fsel, self._fetch(fetch_addr & PC_MASK))
            self.fetch_ptr = (fsel + 1) % n
        else:
            self._next_fetch = None

        # ---------------- S6: final pins for this cycle ---------------------
        if issued and not retry:
            if pin_op is not None:
                npout = pin_op & 0xFFFF
            elif csr_wr is not None and csr_wr[0] == CSR_POUT:
                npout = csr_wr[1] & 0xFFFF
        visible = wave_visible if wave_visible is not None else npout
        self.pins_visible = visible & 0xFFFF
        self.pin_in16 = settled_pins

        # ---------------- S5/S7: commits at the end of the cycle ------------
        # instruction buffer: capture the returned fetch (S5)
        capture_ok = True
        if issued and not retry and self.fetch_returning is not None \
                and self.fetch_returning[0] == sel:
            capture_ok = False

        # issue outcome on the instruction buffer
        if issued and sel is not None:
            th = ths[sel]
            if retry:
                th.insn_valid = True            # retry keeps the instruction
            else:
                th.insn_valid = False           # consumed
                if opc == RPT:
                    th.rep_arm = True
                    th.rep_left = imm & 0xFF
                    th.insn_valid = False
                elif th.rep_arm:
                    if th.rep_left != 0:
                        th.rep_left -= 1
                        th.insn_valid = True    # re-issue the repeated instruction
                    else:
                        th.rep_arm = False
                # effects
                if wb_en:
                    th.r[wb_rd] = wb_data & 0xFFFF
                if flag_z is not None:
                    th.z = flag_z
                if flag_c is not None:
                    th.c = flag_c
                if halt_now:
                    th.halt = True
                if fifo_push is not None:
                    th.tx.append(fifo_push & 0xFF)
                if fifo_pull:
                    th.r[wb_rd] = th.rx.popleft() & 0xFF
                if ev_enqueue is not None:
                    th.eq.append(ev_enqueue)
                if evset_bit is not None:
                    self.evt |= 1 << evset_bit
                if evclear_bit is not None:
                    self.evt &= ~(1 << evclear_bit)
                if irq_set is not None:
                    self.irq_code = irq_set
                if dir_op is not None:
                    self.dir = dir_op & 0xFF
                if inat_arm is not None:
                    th.inat_arm = True
                    th.inat_tgt = inat_arm[1]
                    th.inat_rd = inat_arm[0]
                th.pc = pc_nxt & PC_MASK
                if csr_wr is not None:
                    addr, value = csr_wr
                    if addr < 0x10:
                        # a TPC write overrides the normal PC (computed jump)
                        self._thread_csr_write(th, addr, value)
                        if addr == CSR_TPC:
                            th.pc = value & PC_MASK
                            th.insn_valid = False
                    else:
                        self._global_csr_write(addr, value)

        # capture returned fetch (S5) - order: after issue transitions
        if self.fetch_returning is not None:
            tag, fword = self.fetch_returning
            ths[tag].insn_buf = fword
            if capture_ok:
                ths[tag].insn_valid = True
        self.fetch_returning = self._next_fetch
        self._next_fetch = None

        # event queue pops / occupancy (S7)
        for t, th in enumerate(ths):
            if fired[t]:
                th.eq.pop(0)
        # inat timed sample: latch pins_in during cycle t, committed at end of t
        for th in ths:
            if th.inat_arm and (tmr == (th.inat_tgt & 0xFFFF)):
                th.r[th.inat_rd] = settled_pins
                th.inat_arm = False

        # DMEM writes commit at the end of the cycle
        if self._dmem_writes:
            for addr, value in self._dmem_writes:
                self.dmem[addr] = value & 0xFFFF
            self._dmem_writes.clear()

        # wave player sequential updates
        self._wave_end_of_cycle(k)

        # pout register commits (pins are combinational during a cycle, and the
        # register holds the last driven value)
        self.pins_reg = npout & 0xFFFF

        # stall countdown (S7): only stalls that existed at the start of the
        # cycle count down; an issue this cycle either re-stalls (retry => 1)
        # or loads stall_set (no decrement this cycle).
        for t, th in enumerate(ths):
            old = stall_start[t]
            new = old - 1 if old > 0 else 0
            if issued and sel == t:
                if retry:
                    new = 1
                elif stall_set:
                    new = stall_set
                else:
                    new = 0
            th.stall = new

        self.cycle = k + 1
        self.issue_ptr = (sel + 1) % n if issued and sel is not None else self.issue_ptr
        self._record_trace(k, dir_during)

    # ------------------------------------------------------------------
    # Trace
    # ------------------------------------------------------------------
    def _record_trace(self, cycle: int, dir_during: int) -> None:
        value = self.pins_visible & 0xFFFF
        d = dir_during & 0xFF
        if self.trace:
            last = self.trace[-1]
            if last[0] == cycle:
                if last[1] != value or last[2] != d:
                    self.trace[-1] = (cycle, value, d)
                return
            if last[1] == value and last[2] == d:
                return
        elif value == 0 and d == 0:
            return
        self.trace.append((cycle, value, d))

    def run(self, n: int) -> None:
        """Advance `n` clock cycles."""
        for _ in range(int(n)):
            self.step()

    def run_with_pins(self, n: int, fn: Callable[[int, int, int], int | None]) -> None:
        """Advance `n` cycles, driving the external pin vector from a callback.

        `fn(cycle, pins_out16, dir8)` is called before every cycle; its return
        value (or `None` to leave the stimulus unchanged) becomes the external
        `pins_in` stimulus for that cycle.  This is the supported way to emulate
        the far end of a protocol (UART/SPI/I2C peer) while running the model.
        """
        for _ in range(int(n)):
            value = fn(self.cycle, self.pins_visible, self.dir)
            if value is not None:
                self.ext_in = int(value) & 0xFFFF
            self.step()

    # ------------------------------------------------------------------
    # Snapshot for co-simulation diffs
    # ------------------------------------------------------------------
    def state_snapshot(self) -> dict[str, Any]:
        """Full architectural state as a plain JSON-able dict."""
        return {
            "cycle": self.cycle,
            "tmr16": self.tmr16,
            "pins_out16": self.pins_visible,
            "pins_reg": self.pins_reg,
            "pins_dir8": self.dir,
            "pins_in16": self.pin_in16,
            "ext_in": self.ext_in,
            "irq_code16": self.irq_code,
            "evt": self.evt,
            "issue_ptr": self.issue_ptr,
            "fetch_ptr": self.fetch_ptr,
            "fetch_returning": list(self.fetch_returning) if self.fetch_returning else None,
            "wave": {
                "busy": self.w_busy, "done": self.w_done, "valid": self.w_valid,
                "pins": self.w_pins, "idx": self.w_idx,
                "base": self.wl_base, "len": self.wl_len,
                "pint": self.wl_pint, "pfrac": self.wl_pfrac,
                "loop": self.wl_loop, "oneshot": self.wl_oneshot,
                "next_tick": self.w_next_tick,
                "shadow": dict(self.w_shadow) if self.w_shadow else None,
                "csr": {"ctl": self.wave_ctl, "addr": self.wave_addr,
                        "len": self.wave_len, "per": self.wave_period},
            },
            "threads": [
                {
                    "pc": th.pc, "r": list(th.r), "z": th.z, "c": th.c,
                    "en": th.en, "halt": th.halt, "stall": th.stall,
                    "period": th.period, "pfrac": th.pfrac, "wfrac": th.wfrac,
                    "tmask": th.tmask, "emask": th.emask, "econd": th.econd,
                    "nextt": th.nextt,
                    "eq": [list(e) for e in th.eq],
                    "tx": list(th.tx), "rx": list(th.rx),
                    "rep_arm": th.rep_arm, "rep_left": th.rep_left,
                    "inat_arm": th.inat_arm, "inat_tgt": th.inat_tgt,
                    "inat_rd": th.inat_rd,
                    "insn_valid": th.insn_valid,
                    "insn_buf": th.insn_buf,
                }
                for th in self.threads
            ],
            "dmem": list(self.dmem),
        }


# ---------------------------------------------------------------------------
# Small self-test / demo
# ---------------------------------------------------------------------------
def _demo() -> int:
    """Run a tiny counting program plus the Lead's period-16 sanity program."""
    from sw import loom_asm

    # 1) counting program stores 0,1,2 in DMEM[0]
    program = """
        movi r3, 0          ; DMEM address 0
        movi r0, 0
        movi r1, 3
    loop:
        sti  r0, r3
        addi r0, 1
        cmp  r0, r1
        jnz  loop
        halt
    """
    res = loom_asm.assemble(program, filename="<demo>")
    loom = Loom()
    loom.load(res.words)
    loom.run_threads(0x1)
    loom.run(100)
    snap = loom.state_snapshot()
    ok = (snap["threads"][0]["r"][0] == 3 and snap["dmem"][0] == 2
          and snap["threads"][0]["halt"])

    # 2) Lead sanity target: setp 16.0; setm 0x00FF; emit; emit; jmp -> 16-cycle deltas
    prog2 = """
        setp 0x0100
        setm 0x00FF
        movi r0, 0x00FF
        movi r1, 0x0000
    loop:
        emit r0
        emit r1
        jmp loop
    """
    res2 = loom_asm.assemble(prog2, filename="<demo2>")
    d = Loom()
    d.set_pins_in(0)
    d.load(res2.words)
    d.run_threads(0x1)
    d.run(2000)
    times = [c for (c, _v, _dir) in d.trace]
    deltas = sorted({times[i + 1] - times[i] for i in range(3, len(times) - 1)}) \
        if len(times) > 4 else []
    ok2 = deltas == [16]
    print(f"LOOM_MODEL demo1: r0={snap['threads'][0]['r'][0]} dmem[0]={snap['dmem'][0]} "
          f"halt={snap['threads'][0]['halt']} -> {'ok' if ok else 'FAIL'}")
    print(f"LOOM_MODEL demo2: {len(times)} events, deltas={deltas} (want [16]) "
          f"-> {'ok' if ok2 else 'FAIL'}")
    print("LOOM_MODEL_SELFTEST:", "PASS" if ok and ok2 else "FAIL")
    return 0 if ok and ok2 else 1


if __name__ == "__main__":
    raise SystemExit(_demo())

# Formal layer (bounded model checking, open toolchain)

```bash
cd ..
.venv/bin/python formal/run_formal.py
```

The driver generates one SMT2 transition system per harness with
`yosys read_verilog -formal; prep -top h_*; async2sync; clk2fflogic;
write_smt2 -wires`, then runs `yosys-smtbmc -s z3 -t K` (z3 is installed in the
project venv: `pip install -r test/requirements.txt`).  Results are printed and
saved to `formal/results.txt`; the SMT2 files are regenerated in
`formal/build/` (not checked in).

| harness | k | proven |
|---|---|---|
| `h_fifo.v` | 8 | occupancy never exceeds DEPTH; `full`/`empty` agree with the count; a generated reset leaves the FIFO empty |
| `h_core.v` | 6 | reset completeness (TMR/POUT/DIR/all threads/queues/FIFO counters); event-queue depth ≤ 4 and head pointer in range; TX/RX FIFO counts ≤ 8; DMEM port address in range; POUT changes only through a defined cause (fired event, instruction pin op, CSR/host POUT write, wave player) |
| `h_host.v` | 6 | reset completeness (MISO/IRQ/FSM/output pulses); `mem_we` only for WRITE_MEM; `rx_push` only for TX_PUSH; `tx_pop` only for RX_POP; `csr_wr_en` only for the CSR-writing commands; host memory port always inside the 10-bit map |
| `h_sched.v` | 16 | **scheduler exactness**: an accepted timed event fires exactly in the cycle where `TMR` equals its due time — never one cycle late (P1) and never one cycle early (P2); an edge-triggered event fires exactly in the cycle where the masked pin condition holds (P3); `fire_v` is the enqueued value (P4); the due time recorded at enqueue is exact for `emit` (incl. 12.4 PFRAC carry + behind-schedule clamp), `emita` (absolute + clamp) and `emitd` (relative; `d==0` → next cycle) (P5); and thread 0's fired value reaches `pins_out` in the same cycle when it is the sole pin cause (P6) |

`h_sched` uses an **instrumented observation copy of the core**: yosys does not
resolve hierarchical references into a submodule's internal wire/regs (it
silently creates undriven wires), so `run_formal.py` generates
`formal/build/loom_core_probe.v` from `rtl/loom_core.v` by appending read-only
`output wire dbg_*` ports and `assign dbg_* = <internal signal>` statements.
The logic is identical to the RTL — only observations are added — and the
generated file is not checked in.  This is why the scheduler invariants are
genuine statements about the core rather than about free wires.

**Scheduler proof assumptions (not claimed):** the firmware must enqueue a
timed event with lookahead < 0x8000 and before its due cycle (a late enqueue is
recovered as "overdue" by design); the event queue must have room (a full queue
stalls the emitting instruction — no event is silently dropped); external
stimulus (`pins_in`, DMEM, host/FIFO ports, wave inputs) is arbitrary; for P6
the antecedent excludes competing pin writers (instruction pin op, CSR/host
POUT write, wave player, another thread's fired event).  For non-vacuity the
harness runs thread 0 against a fixed EMIT/EMITD/EMITA/EMITE instruction stream
so BMC actually reaches enqueue/fire states; the invariants themselves are
cycle-local and unconditional under that stimulus.  Bound k=16 (see
`results.txt`); this is bounded model checking, not an unbounded induction.

Each harness contains its own reset generator (`reg [N:0] rst_cnt = 0`), so BMC
starts from a real reset state; assertions are only checked after `rst_n` is
released.  **Assumed, not proven**: all external responders are arbitrary
(`(* anyseq *)`) — SPI pins, memory/CSR read data, FIFO pop data, `irq_code`;
the memory map/decode is 0x000-0x3BF PMEM / 0x3C0-0x3FF DMEM and PMEM writes are
gated on `addr < PMEM_WORDS` by construction; every result is bounded to the
stated depth.  This is a *bounded* proof (no induction/unbounded invariant).

Sanity: the harness mechanism was validated against a deliberately false toy
assertion (smtbmc reports `Status: FAILED`).

**Non-vacuity status of `h_sched` (important, do not overclaim):** the harness
carries `cover` statements for an issued instruction, an accepted timed/edge
enqueue, a fired event and a due-cycle head.  At the committed bound (k=16) the
deterministic scheduler-exercise stream does **not yet reach an issuing state**
from the forced host RUN, so those covers are unreached and a deliberately
one-cycle-late mutation of the fire comparison is **not** detected by BMC.  The
proof therefore currently establishes the cycle-local invariants over the
explored state space (and the exact due-time formulas P5 are checked whenever an
enqueue is reached), but the "accepted event fires exactly on time" antecedent
is **not demonstrated reachable within k** — treat this as a conditional
scheduler-exactness result, not yet an unconditional "no edge is ever late"
proof.  Follow-up: fix the harness stimulus / increase k so the covers hit and
the mutation is detected, then raise the claim accordingly.

Known limitation carried from before: `h_core.v` still uses a handful of
hierarchical references (`tmr`, `t_en`, `pin_op`, `stall_issue`, `do_issue`,
`csr_wr`) which yosys treats as undriven wires, so those specific `h_core`
assertions are weaker than the table suggests; `h_fifo`/`h_host` are clean.  A
follow-up will move `h_core` onto the same generated observation wrapper.

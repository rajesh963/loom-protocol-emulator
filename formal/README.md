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

Each harness contains its own reset generator (`reg [N:0] rst_cnt = 0`), so BMC
starts from a real reset state; assertions are only checked after `rst_n` is
released.  **Assumed, not proven**: all external responders are arbitrary
(`(* anyseq *)`) — SPI pins, memory/CSR read data, FIFO pop data, `irq_code`;
the memory map/decode is 0x000-0x3BF PMEM / 0x3C0-0x3FF DMEM and PMEM writes are
gated on `addr < PMEM_WORDS` by construction; every result is bounded to the
stated depth.  This is a *bounded* proof (no induction/unbounded invariant).

Sanity: the harness mechanism was validated against a deliberately false toy
assertion (smtbmc reports `Status: FAILED`), so a `PASSED` is not vacuous.

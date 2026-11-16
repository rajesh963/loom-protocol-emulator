# LOOM — Architecture & Interface Specification (v1.0, frozen 2026-11-16)

> A *protocol loom*: four programmable threads weave bit-exact waveforms on 19 general-purpose
> pins, while a hardware event scheduler (not the instruction stream) decides *when* every edge
> happens. Reprogrammable after fabrication; host-loaded over SPI.

**Competition:** Jane Street / Tiny Tapeout protocol-emulator ASIC, IHP CMOS5L, 8x4 tiles,
March 2027 shuttle, deadline 2027-01-18.

**Top module:** `tt_um_rajesh963_loom`  ·  **Repo:** `loom/`  ·  **Language:** Verilog-2005
(synthesisable), Verilator/iverilog-friendly.

---

## 1. Why this architecture (the pitch / judging narrative)

RP2040 PIO and TI PRU both bit-bang by tying a pin transition to *instruction issue*. That forces
every loop and branch in the protocol firmware to be cycle-counted, makes timing depend on other
engines, and quantises baud rates through an integer (or 1/256) divider.

LOOM instead separates **what to drive** (software: encoders, CRCs, framing, state) from **when**
(hardware: an event scheduler with fractional-cycle resolution):

* **Decoupled timing.** A thread enqueues `(time, pin_value)` events; the pin unit applies them at
  exactly the scheduled cycle. Branching, loop overhead, FIFO stalls and *other threads* cannot
  add edge jitter. Instruction scheduling can be work-conserving without hurting timing.
* **Fractional periodic events.** Each thread has a 12.4 fixed-point period register with carry
  accumulation, so a stream of events is drift-free over arbitrarily long runs (UART 115200 baud at
  60 MHz = 520.8333 cycles/bit; error is bounded by 1/32 cycle per event and averages to zero).
* **Reactive events (edge-triggered).** The same queue entries can fire on a pin-pattern match
  (with per-thread mask/expected value), giving glitch-free slave/reactive behaviour without the
  CPU having to poll.
* **1-cycle timed sampling.** `INAT rd, t` latches the pin vector at an exact cycle and returns it
  when the thread resumes — protocol decoding without sampling jitter.
* **Wave player.** A generic pattern player streams a precomputed 16-bit pin pattern per N
  fractional cycles from data memory → sustained symbol rates up to 60 Mbaud (10BASE-T class).
* **Host bridge with FIFOs.** Byte-streamed over a 4-wire SPI slave; program, data and configuration
  are loaded at runtime; per-thread TX/RX FIFOs decouple host latency from wire timing.
* **Save/restore = record & replay.** (stretch) Capture timestamped pin transitions, then replay.

Everything user-visible is reprogrammable after fabrication (no protocol hard-blocks).

---

## 2. Pins (Tiny Tapeout standard interface)

| TT pin | Direction | LOOM function |
|---|---|---|
| `ui_in[0]` | in | `H_CS_N` — host SPI chip select, active low |
| `ui_in[1]` | in | `H_SCK` — host SPI clock (sampled; may be up to 10 MHz) |
| `ui_in[2]` | in | `H_MOSI` — host SPI data in |
| `ui_in[7:3]` | in | protocol inputs `P[15:11]` (readable) |
| `uo_out[0]` | out | `H_MISO` — host SPI data out (driven by host bridge) |
| `uo_out[1]` | out | `H_IRQ` — level interrupt to host (head-of-line event / FIFO / thread IRQ) |
| `uo_out[7:2]` | out | protocol outputs `P[15:10]` (writable) |
| `uio_out[7:0]` | out | protocol outputs `P[7:0]`, gated by `uio_oe` |
| `uio_oe[7:0]` | out | per-pin output enable (`DIR`); `0` = high-Z input (open-drain friendly) |
| `uio_in[7:0]` | in | protocol inputs `P[7:0]` (readable, readback of pin state) |
| `clk` | in | system clock, target **60 MHz** (accept anything 10–66 MHz) |
| `rst_n` | in | asynchronous active-low reset; all state defined after reset |

**Protocol pin vectors** (the 16-bit instruction operand maps 1:1 onto these):

```
pins_out[15:0] : [15:10]=uo_out[7:2]  [9:8]=host (writes ignored)  [7:0]=uio_out[7:0]
pins_in [15:0] : [15:8] =ui_in[7:0]   ( [2:0] are host pins)       [7:0]=uio_in[7:0]
DIR[7:0]       : uio_oe
```

⇒ **19 protocol pins**, **14 independently drivable**, **13 readable**, of which `uio[7:0]` are
true bidirectional open-drain-capable pins. `uio[7:0]` are fully general purpose (the host link
uses `ui`/`uo` so that the 8 bidir pins stay free for I2C/PS2/1-Wire/etc).

`ena` is ignored (design is always on); `rst_n` resets everything, including the host bridge.

---

## 3. Execution model

**4 threads** (`NUM_THREADS=4`), all sharing one datapath. **One instruction is issued per cycle**
by a work-conserving round-robin scheduler (a stalled/halted thread is skipped, so throughput is
not wasted, yet pin timing is unaffected because edges come from the event scheduler).

Per thread state:

| State | Width | Notes |
|---|---|---|
| `PC` | 10 | instruction word address |
| `R0..R7` | 8×16 | general purpose |
| `Z,C` | 1+1 | flags from ALU ops |
| `en`, `halt` | 1+1 | run/halted |
| `stall` | 16 | cycles remaining before thread may issue |
| `PERIOD` | 16 | event period, 12.4 fixed point (int[15:4] . frac[3:0]) |
| `PFRAC` | 4 | fractional carry accumulator for `PERIOD` |
| `TMASK` | 16 | pins this thread's events may drive |
| `EMASK`,`ECOND` | 16+16 | edge-trigger mask / expected value for reactive events |
| `NEXTT` | 16 | last scheduled event time (wrap-safe 16-bit) |
| `EQ` | 4×{kind,t,val} | event queue, depth 4, FIFO |
| `TXFIFO` | 8×8 | thread→host bytes |
| `RXFIFO` | 8×8 | host→thread bytes |

`TMR` is a free-running 16-bit cycle counter (wraps every 1.09 ms at 60 MHz; all time arithmetic is
modular/signed-difference, so wrap is safe as long as scheduling look-ahead < 32K cycles).

Thread slots: instructions issue in round-robin order `0,1,2,3` skipping non-runnable threads.
`WAIT`/`INAT`/FIFO back-pressure stall only the issuing thread.

Priority when several writes hit a pin in one cycle: **t0 > t1 > t2 > t3 > instruction
`OUT/SET/CLR/TOG`** (documented, deterministic).

---

## 4. Instruction set (32-bit words)

Field layout (uniform):

```
 31       25 24  22 21  19 18  16 15              0
+-----------+------+------+------+----------------+
|  opcode   |  rd  |  rs  | mode |      imm16     |
+-----------+------+------+------+----------------+
     7b         3b     3b    3b         16b
```

`rd`,`rs` = register index (0–7). `mode` is reserved (0). Unused fields are 0. (7-bit opcode: the control group `0x40`–`0x4B` needs 7 bits.) Every instruction retires in 1 cycle unless
it stalls (FIFO/queue/host-port back-pressure, timed waits). `ld/ldi/st/sti` are single-cycle
(combinational DMEM read, write committed at end of cycle).

### 4.1 Opcode map (single source of truth: `sw/isa.py`; `docs/isa.md` is generated from it)

| Op | Mnemonic | Semantics |
|---|---|---|
| `0x00` | `nop` | no operation |
| `0x01` | `halt` | halt this thread (`halt=1`, `en` unchanged); optional IRQ via CSR |
| `0x02` | `movi rd, imm16` | `R[rd] = imm16` |
| `0x03` | `mov rd, rs` | `R[rd] = R[rs]` |
| `0x04` | `add rd, rs` | `R[rd] += R[rs]`; sets Z,C |
| `0x05` | `addi rd, imm16` | `R[rd] += imm16`; sets Z,C |
| `0x06` | `sub rd, rs` | `R[rd] -= R[rs]`; sets Z,C |
| `0x07` | `subi rd, imm16` | `R[rd] -= imm16`; sets Z,C |
| `0x08` | `and rd, rs` | bitwise; Z |
| `0x09` | `andi rd, imm16` | |
| `0x0A` | `or rd, rs` | |
| `0x0B` | `ori rd, imm16` | |
| `0x0C` | `xor rd, rs` | |
| `0x0D` | `xori rd, imm16` | |
| `0x0E` | `inv rd` | `~R[rd]` |
| `0x0F` | `shl rd, n` | `R[rd] <<= n` (n=imm16[3:0], 0–15) |
| `0x10` | `shr rd, n` | logical right |
| `0x11` | `rol rd, n` | rotate left 16 |
| `0x12` | `ror rd, n` | rotate right 16 |
| `0x13` | `cmp rd, rs` | flags from `R[rd]-R[rs]` |
| `0x14` | `cmpi rd, imm16` | flags from `R[rd]-imm16` |
| `0x15` | `csrw csr, rs` | `CSR[imm16[7:0]] = R[rs]` (thread bank if `csr<0x10`) |
| `0x16` | `csrr rd, csr` | `R[rd] = CSR[imm16[7:0]]` |
| `0x18` | `out imm16` | `pins_out = imm16` (host bits ignored) |
| `0x19` | `set imm16` | `pins_out |= imm16` |
| `0x1A` | `clr imm16` | `pins_out &= ~imm16` |
| `0x1B` | `tog imm16` | `pins_out ^= imm16` |
| `0x1C` | `in rd` | `R[rd] = pins_in` (combinational read) |
| `0x1D` | `dir imm8` | `DIR = imm16[7:0]` (`uio_oe`) |
| `0x1E` | `dset imm8` | `DIR |= imm16[7:0]` |
| `0x1F` | `dclr imm8` | `DIR &= ~imm16[7:0]` |
| `0x20` | `emit rd` | enqueue timed event: `t = NEXTT + PERIOD` (frac accum), `val = R[rd]`, `kind=time`; `NEXTT=t`; stalls thread if queue full |
| `0x21` | `emita rd, t16` | enqueue at absolute `TMR == imm16`, `val=R[rd]`, `kind=time` |
| `0x22` | `emitd rd, d16` | enqueue at `TMR + imm16`, `val=R[rd]`, `kind=time` |
| `0x23` | `emite rd` | enqueue **edge** event, `val=R[rd]`, fires when `(pins_in & EMASK)==(ECOND & EMASK)` |
| `0x24` | `setp imm16` | `PERIOD = imm16` (12.4) |
| `0x25` | `setm imm16` | `TMASK = imm16` |
| `0x26` | `setem imm16` | `EMASK = imm16` (edge kind only) |
| `0x27` | `setec imm16` | `ECOND = imm16` |
| `0x28` | `inat rd, t16` | stall until `TMR==imm16`, latch `pins_in` **that cycle**, `R[rd]=latch` |
| `0x29` | `waitu t16` | stall until `TMR==imm16` |
| `0x2A` | `waitd d16` | stall `imm16` cycles (fractional 12.4 accumulated, drift-free) |
| `0x2B` | `waitp imm16` | stall until `(pins_in & imm16[15:8]) == imm16[7:0]` (mask/value; polling, ≤4-cycle latency) |
| `0x30` | `ld rd, a6` | `R[rd] = DMEM[imm16[5:0]]` (2-cycle) |
| `0x31` | `st a6, rs` | `DMEM[imm16[5:0]] = R[rs]` (2-cycle) |
| `0x32` | `ldi rd, rs` | indirect: `R[rd] = DMEM[R[rs][5:0]]` |
| `0x33` | `sti rs, rd` | `DMEM[R[rd][5:0]] = R[rs]` |
| `0x38` | `push rd` | `TXFIFO <<= R[rd][7:0]`; stall if full |
| `0x39` | `pull rd` | `R[rd] = RXFIFO` (zero-extended); stall if empty |
| `0x3A` | `pushi imm8` | `TXFIFO <<= imm16[7:0]` |
| `0x40` | `jmp rel16` | `PC += sext(imm16)` (PC-relative, relocatable) |
| `0x41` | `jz rel16` | if `Z` |
| `0x42` | `jnz rel16` | if `!Z` |
| `0x43` | `jc rel16` | if `C` |
| `0x44` | `jnc rel16` | if `!C` |
| `0x48` | `rpt n` | repeat the following instruction `n+1` times (`imm16[7:0]`), hardware counter |
| `0x49` | `evset imm8` | set event flag |
| `0x4A` | `evwait imm8` | stall until event flag set, then clear it |
| `0x4B` | `irq imm8` | raise host IRQ with code `imm16[7:0]` |

Notes:
* `PC += rel16` where `rel16 = imm16` interpreted as signed 16-bit, in **word** units.
  Assembler computes it; all firmware is relocatable (host can place threads' programs anywhere).
* `rpt` is a 1-deep hardware repeat: the same instruction re-issues `n` more times without PC
  advance; stalls inside the repeated instruction apply per iteration.
* Register `R[rd]` writes: `ld/ldi` (2-cycle), `pull`, `in/inat`, `csrr`, ALU.
* Any unimplemented opcode = `nop` (forward compatibility).
* Flag semantics: `movi/mov/and/andi/or/ori/xor/xori/inv/shl/shr` set `Z` and **clear** `C`;
  `add/addi/sub/subi/cmp/cmpi` set both; `rol/ror/in/ld/ldi/pull/csrr` leave flags unchanged.
* `CSR_IRQ` read is **non-destructive**; the host clears it by writing `CSR_IRQ = 0`
  (that is what `HCMD_ACK_IRQ` does).

### 4.2 CSR map

Per-thread bank (CSR address `0x00`–`0x0F`; thread instructions address their own bank; the host
addresses thread `t`'s bank at `0x40 + 16*t + n`):

| CSR | Name | R/W | Meaning |
|---|---|---|---|
| `0x00` | `TPERIOD` | RW | event period, 12.4 |
| `0x01` | `TMASK` | RW | event output mask |
| `0x02` | `TSTATUS` | R | `{30'b0, halted, en}` |
| `0x03` | `TFIFO` | R | `{4'b0, rx_count[3:0], tx_count[3:0], 2'b0, tx_full, rx_empty}` (counts at [11:8]/[7:4]) |
| `0x04` | `TNEXTT` | R | last scheduled time |
| `0x05` | `TEQ` | R | `{4'b0, eq_count[2:0]}` event-queue occupancy |
| `0x06` | `TEMASK` | RW | edge-trigger mask |
| `0x07` | `TECOND` | RW | edge-trigger expected value |
| `0x08` | `TPC` | RW | program counter (host can set start address) |

Global registers:

| CSR | Name | R/W | Meaning |
|---|---|---|---|
| `0x10` | `POUT` | RW | `pins_out` register |
| `0x11` | `PDIR` | RW | `DIR` (uio_oe) |
| `0x12` | `TMR` | R | free-running cycle counter |
| `0x13` | `EVT` | RW | 8 event flags |
| `0x14` | `IRQ` | RW | host IRQ code (read clears) |
| `0x15` | `WCTRL` | RW | `{6'b0, oneshot, loop, start}` wave player |
| `0x16` | `WADDR` | RW | wave buffer start (DMEM word addr) |
| `0x17` | `WLEN` | RW | wave buffer length (words, 1–64) |
| `0x18` | `WPER` | RW | wave symbol period, 12.4 |
| `0x19` | `WSTATUS` | R | `{.., busy, done}` |
| `0x1A` | `CAPCTRL` | RW | capture trigger config (stretch) |
| `0x1B` | `CAPSTAT` | R | capture status/count (stretch) |
| `0x1C` | `GSTATUS` | R | `{7'b0, en[3:0], 4'b0, irq}` (bit0 = host IRQ pending) |

Host-only CSR `0x20` = soft reset (write 1) — resets threads/memories but not the host bridge.

---

## 5. Memory

| Region | Words | Width | Contents |
|---|---|---|---|
| PMEM | 1024 (`FF` build: 128) | 32 | instructions (PC 10 bits) |
| DMEM | 64 | 16 | scratch / wave buffer / (stretch) capture |
| TX/RX FIFOs | 8 each per thread | 8 | host ↔ thread byte streams |

* PMEM is `1RW`, synchronous read, 1-cycle latency (registered PC; fetch is pipelined with the
  scheduler). Instruction fetch never stalls except on data-memory instructions (2-cycle).
* DMEM has 1 CPU read + 1 CPU write + 1 wave-player read port (FF array; dual-read).
* **Build configurations** (`PMEM_IMPL` parameter):
  * `"ff"` — flip-flop array, `PMEM_WORDS` default 1024 for simulation, 128 for an area-safe
    silicon fallback. Zero flow risk.
  * `"sram"` — IHP `1024x32` 1-port SRAM macro (140,183 µm², 14 % of an 8x4 die). Flagship
    configuration. Wrapper must be swappable without touching the core.
* Unmapped PMEM addresses read `32'h0200_0000` (`halt` — opcode 0x01 << 25) so a runaway PC stops.
  The assembler's default fill word is the same value.

---

## 6. Host protocol (SPI slave, mode 0, MSB first)

4-wire: `H_CS_N`, `H_SCK`, `H_MOSI`, `H_MISO` + `H_IRQ` output. Max `H_SCK` 10 MHz at 60 MHz
`clk` (SCK is synchronous-sampled with 2-FF sync + edge detect; the core never sees an async
clock). Every 8-bit transfer simultaneously returns one byte; commands are a byte stream:
`[CMD][ARG...]`, and read commands return their payload on the *following* transfers.

| CMD | Name | Args (host→chip) | Returns | Meaning |
|---|---|---|---|---|
| `0x01` | `SET_ADDR` | `a[23:0]` (3B, LSB first) | status | set word address pointer |
| `0x02` | `WRITE_MEM` | `w[31:0]` (4B) | status | `MEM[addr] = w; addr++` |
| `0x03` | `READ_MEM` | — | 4B next transfers | `MEM[addr]; addr++` |
| `0x04` | `WRITE_CSR` | `csr[7:0]`, `d[15:0]` | status | CSR write |
| `0x05` | `READ_CSR` | `csr[7:0]` | 2B next | CSR read |
| `0x06` | `TX_PUSH` | `t[1:0]`, `d[7:0]` | status | push byte to thread `t` RXFIFO |
| `0x07` | `RX_POP` | `t[1:0]` | 1B next | pop byte from thread `t` TXFIFO |
| `0x08` | `SET_PC` | `t[1:0]`, `pc[15:0]` | status | thread PC |
| `0x09` | `RUN` | `mask[3:0]` | status | clear halt, `en |= mask` |
| `0x0A` | `STOP` | `mask[3:0]` | status | `en &= ~mask` |
| `0x0B` | `STATUS` | — | 4B next | `{t3..t0 status bytes}` |
| `0x0C` | `ACK_IRQ` | — | status | clear host IRQ latch |
| `0x0D` | `PING` | `0x5A` | `0xA5` | link check |

`status` returned in the third phase is `0x80` when the command was accepted / `0x81` busy /
`0x00` idle. `H_IRQ` is asserted (level) while `IRQ` CSR code ≠ 0 or a `STATUS` condition is set;
host clears via `ACK_IRQ` or by reading `IRQ`.

Host-side Python client lives in `host/` and works against (a) the cocotb RTL, (b) the Python ISA
model, (c) a real board via MicroPython/PIO or any SPI master.

---

## 7. Reset

`rst_n` low: all threads disabled & halted, PC=0, registers 0, queues/FIFOs empty, `pins_out=0`,
`DIR=0` (all `uio` inputs), host bridge idle, `H_MISO=0`, `H_IRQ=0`, wave player off, `TMR=0`.
Design must be **reset-complete** (no X after reset) — see formal properties in §10.

---

## 8. Module interfaces (file ownership in `spec/OWNERS.md`)

```verilog
// rtl/loom_types.vh — localparams shared by all modules

module loom_core #(
  parameter NUM_THREADS = 4,
  parameter EQ_DEPTH    = 4,
  parameter PMEM_WORDS  = 1024
)(
  input  wire        clk, rst_n,
  // instruction memory (synchronous read, 1-cycle)
  output wire [9:0]  imem_addr,
  input  wire [31:0] imem_data,
  // data memory (async read, sync write)
  output wire [5:0]  dmem_addr,
  output wire        dmem_we,
  output wire [15:0] dmem_wdata,
  input  wire [15:0] dmem_rdata,
  // pin interface
  output wire [15:0] pins_out,      // protocol outputs (host bits excluded)
  output wire [7:0]  pins_dir,      // uio_oe
  input  wire [15:0] pins_in,
  // CSR / peripheral bus (shared with host bridge)
  /* see loom_csr.vh: csr_req/csr_rsp handshake, host side + thread side */
  ...
);
```

Rather than duplicate the full port list here, `rtl/loom_bus.vh` defines the exact
core↔host↔peripheral interfaces; that file is frozen with this spec.

---

## 9. Deliverables (judging checklist)

1. `rtl/` — synthesisable RTL, `info.yaml` 8x4 tiles, `src/config.json`.
2. `sw/` — assembler (`loom_asm`), cycle-accurate ISA model, random program generator, docs-gen.
3. `host/` — Python host-protocol client + MicroPython/C reference snippets.
4. `sw/examples/` — UART (TX+RX, 9 common baud rates), SPI master (modes 0–3) + SPI slave, I2C
   controller (100/400 kHz) + I2C target, PS/2, 1-Wire, WS2812, capture/replay (stretch), USB LS
   (stretch), 10BASE-T via wave player (stretch).
5. `test/` — cocotb: ISA unit tests, CSR/host-bridge tests, random differential co-simulation vs the
   Python model, protocol VIP conformance tests, ASCII waveform dumps.
6. `formal/` — yosys `write_smt2` + z3 properties (see §10).
7. `docs/` — `info.md` (datasheet), `architecture.md`, `isa.md` (generated), `verification.md`,
   `host-protocol.md`, `examples.md`, `results.md` (area/timing/GDS), `writeup.md` (the narrative).
8. Passing `gds` + `precheck` + `gl_test` GitHub Actions (LibreLane, `tt-gds-action@ttihp27a`).

---

## 10. Verification strategy (what we will claim and prove)

1. **Golden ISA model** (`sw/loom_model.py`) written from the spec, not from the RTL.
2. **Differential co-simulation**: random programs generated by `sw/gen_program.py` run on RTL and
   model; compare every pin transition, TMR and architectural state at retirement boundaries.
3. **Protocol VIPs in cocotb**: independent UART/SPI/I2C reference models assert conformance
   (baud error, framing, CPOL/CPHA, setup/hold, ACK, clock stretching) and dump ASCII waveforms.
4. **Formal** (bounded proofs, yosys SMT + z3): reset completeness; FIFOs never overflow/underflow
   silently; event queue never drops an armed event; host bridge never writes outside memory;
   `pins_out` only changes on scheduled events or explicit ops; no spurious IRQ.
5. **Gate-level simulation** of the hardened netlist (`GATES=yes`) running the protocol suite.
6. **Coverage**: ISA opcode × CSR × thread cross-coverage matrix; all 4 threads active concurrently;
   all 19 protocol pins exercised.

---

## 11. Risks & fallbacks

| Risk | Mitigation |
|---|---|
| SRAM macro breaks LibreLane/precheck | `PMEM_IMPL="ff"` 128-word fallback build; both built in CI |
| Area overflow at 8x4 | measure with yosys+IHP liberty after every RTL drop (script in `tools/`); drop wave/capture first |
| Event-queue back-pressure limits fast protocols | depth 4/thread; wave player covers sustained high-rate patterns |
| Two independent front-ends (RTL/model) diverge | model-first policy; co-sim is a required check before any merge |
| Host board pin map unknown | host tool is pin-configurable; chip side only fixes `ui[2:0]`/`uo[0..1]` |

---

## 3.1 Normative cycle model (frozen — the golden model MUST implement this)

One cycle = one `posedge clk`. `TMR` holds the cycle index `k` (0 after reset). All `stall` values
count *cycles in which the thread may not issue*; `stall>0` at the start of a cycle blocks issue.

### Per-cycle order of operations

**S1. Fire events.** For each thread `t` (0..3) whose queue is non-empty, look at the head:
* timed entry: fires if `target == TMR` **or** `(target - TMR) & 0xFFFF >= 0x8000`
  (i.e. due or overdue; this also guarantees two entries with the same target both fire, and
  no armed event is dropped after a long stall. Absolute scheduling lookahead must be < 32768
  cycles.)
* edge entry: fires if `(pins_in & EMASK[t]) == (ECOND[t] & EMASK[t])`
At most **one pop per thread per cycle**. Fired values are applied to the pin vector in S6.
(Evaluated from state at the start of the cycle, i.e. `pins_in` of this cycle.)

**S2. Issue selection.** Scan threads in round-robin order starting from `issue_ptr`, take the first
`t` with `en[t] && !halt[t] && stall[t]==0 && (insn_valid[t] || fetch_returning_tid==t)`.
If found, `sel = t`, `issue_ptr = (t+1) & 3`.
The instruction to execute is `fetched_word` if `(fetch_returning_tid==t && !insn_valid[t])`
(the **bypass** path) else `insn_buf[t]`.

**S3. Execute (or retry).** If the instruction cannot execute (see back-pressure list), it is
**retried**: `stall[sel] <= 1`, `insn_valid` unchanged, no side effects, PC unchanged.
Otherwise all effects happen at the end of this cycle:
* ALU/shift/load writeback to `R[rd]`; flags (`Z`,`C`) only for flag-setting ops.
* `PC <= pc_nxt`, where `pc_nxt = PC` for re-issue (rpt) and HALT, `PC + sign_ext(rel16)` for a
  taken jump, else `PC + 1`.
  A **taken jump costs no fetch bubble**: the fetch started this cycle (S4) uses `pc_nxt`.
  It only loses a cycle if another thread owns the single fetch port that cycle.
* `stall[sel] <= stall_set` (timed waits; `stall_set==0` means no stall).
* FIFO push/pull, event enqueue (S5), CSR write, `irq`, `evset`, `evwait` (clear), `inat` arming.

**S4. Fetch.** Scan threads in round-robin order from `fetch_ptr`, take the first `t` with
`en[t] && !halt[t] && !hv[t] && !(fetch_returning_tid==t && not_consumed_this_cycle)`, where
`hv[t] = (sel==t && issued) ? reissue_now : (insn_valid[t] || fetch_returning_tid==t)`.
If found: `fetch_returning_tid <= t`, `fetch_ptr <= t+1`, and the word fetched is
`PMEM[fetch_pc]` where `fetch_pc = sel==t && issued ? (CSRW-to-TPC ? new_TPC : pc_nxt) : PC[t]`.
A `CSRW` to `CSR_TPC` is therefore a **computed jump with no bubble**: the same-cycle fetch
targets the new PC and the thread's instruction buffer is not filled from the old path.
The fetched word becomes available **next cycle** (`fetch_returning`), i.e. at most one fetch per
cycle and the tag travels with it.

**S5. Capture returned fetch.** If a fetch returned this cycle (tag `fetch_returning_tid`):
`insn_buf[tag] <= fetched_word`; `insn_valid[tag] <= 1` **unless** that word was consumed by the
issue in S2 (then `insn_valid` stays 0 and the buffer copy is harmless). This is what lets a single
active thread issue one instruction **every cycle** while four active threads share one fetch port.

**S6. Pins.** `pout_next = pout`; apply in order:
1. instruction pin op (`OUT/SET/CLR/TOG`) or `CSRW POUT/PDIR`
2. host `CSRW POUT/PDIR` (host wins over an instruction in the same cycle; the instruction retries)
3. fired events in priority order **t3, t2, t1, t0** (so t0 has highest priority):
   `pout = (pout & ~TMASK[t]) | (value & TMASK[t])`
4. wave player: while `wave_active`, the effective pin vector is `wave_pins` (pout is still updated
   underneath, and becomes visible again when the wave player stops).
`pout[9:8]` belong to the host link (MISO/IRQ) and are never bonded out; writes to them are
**masked to 0** (`PINS_OUT_DRIVABLE = 16'hFCFF`), so a `CSRR` of `CSR_POUT` after `out 0xFFFF`
reads `0xFCFF`. Events are masked by `TMASK` and then by the same mask.

**S7. Counts and timers.** `TMR <= TMR + 1`; `stall[t] <= stall[t]-1` for every thread with
`stall>0` that was not just re-stalled by its own issue this cycle; pop/enqueue head/count update:
if a thread both fires (pop) and enqueues in the same cycle, occupancy is **unchanged** (one slot
reused) — do not add and subtract separately.

### Back-pressure (retry) list
`PULL` with empty RX FIFO · `PUSH/PUSHI` with full TX FIFO · `EMIT/EMITA/EMITD/EMITE` with full
event queue (depth 4) · `CSRW` while the host owns the CSR write port this cycle · `LD/LDI/ST/STI`
while the host owns the DMEM write port · `WAITP` while the pattern does not match · `EVWAIT` while
the event flag is clear. A retried instruction does not advance PC and does not consume its
instruction buffer.

### Timing primitives (exact formulas; `k` = TMR in the issue cycle)
| op | effect |
|---|---|
| `waitd D` (12.4) | `f = WFRAC + D[3:0]`; `carry = f>=16`; `total = D[15:4]+carry`; `WFRAC = f & 15`; `stall_set = (total==0) ? 0 : total-1`; next issue at `k+total` |
| `waitu t` | `delta = t-k`; `stall_set = (delta==0) ? 0 : delta-1`; next issue at `max(k+1,t)` |
| `inat t` | latch `pins_in` during cycle `t`; `if t==k: R[rd]=pins_in` immediately; else arm and `stall_set = delta`; `R[rd]` written end of cycle `t`, next issue at `t+1` |
| `emit rd` | `f = PFRAC + PERIOD[3:0]`; `carry=f>=16`; `target = NEXTT + PERIOD[15:4] + carry`; `PFRAC = f&15`; `NEXTT = target` (then clamp) |
| `emita rd,t` | `target = t` |
| `emitd rd,d` | `target = TMR + d` |
| clamp (all timed) | if `(target - TMR) & 0xFFFF >= 0x8000` then `target = TMR+1` ("behind schedule: fire next cycle"), and `NEXTT = target` for `emit` |
| fire | entry fires when `target == TMR`; the pin change is visible **during** that cycle |
| `emite rd` | enqueue edge entry; fires in the first cycle where `(pins_in & EMASK) == (ECOND & EMASK)` |

### Host CSR contention
The host bridge has write priority on the CSR port. If the host writes a CSR in the same cycle an
instruction executes `CSRW`, the instruction retries (`stall<=1`); the host write commits at the end
of that cycle and is visible from the next cycle.

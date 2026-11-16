# LOOM instruction set reference

**What this is.** The complete ISA: how a 32-bit instruction is encoded, every mnemonic with its
semantics, flags and an example, the CSR and memory maps, the assembler's syntax, and a worked
"write your first protocol" tutorial. It is written to agree exactly with `rtl/loom_bus.vh`,
`rtl/loom_core.v` and the assembler's source of truth `sw/isa.py`; the opcode, CSR and host-command
tables below are **generated from `sw/isa.py`**, not hand-maintained (see the note above each).

Normative architecture: [`../spec/ARCHITECTURE.md`](../spec/ARCHITECTURE.md) §3–§5. Read
[`architecture.md`](architecture.md) first if "event scheduler" is a new idea.

---

## 1. Encoding

Every instruction is one 32-bit word, little-endian in memory, uniform field layout:

```
 31       25 24  22 21  19 18  16 15              0
+-----------+------+------+------+----------------+
|  opcode   |  rd  |  rs  | mode |      imm16     |
+-----------+------+------+------+----------------+
    7 bits     3b     3b     3b        16 bits
```

| Field | Bits | Use |
|---|---|---|
| `opcode` | `[31:25]` | 7-bit opcode (the control group `0x40`–`0x4B` needs the 7th bit) |
| `rd` | `[24:22]` | destination register index 0–7 |
| `rs` | `[21:19]` | source register index 0–7 |
| `mode` | `[18:16]` | reserved, must be 0; the hardware ignores non-zero values |
| `imm16` | `[15:0]` | 16-bit immediate, register index, CSR number or `rel16` |

For a two-register instruction the assembler writes `rd` and `rs`; for one-register instructions
`rs = 0`; for immediate-only instructions both register fields are 0. `rel16` is a **signed**
16-bit word offset (`PC += sext(imm16)`), so branches reach ±32 767 instructions.

The spec originally drew a 6-bit opcode with a spare bit in the field diagram; that truncates
`0x40`–`0x4B` onto `nop`–`ori`, so the frozen header, RTL and assembler all use the 7-bit layout
shown here.

## 2. Operand kinds

| Kind | Written as | Passed in | Assembler check |
|---|---|---|---|
| `rd`, `rs` | `r0`…`r7` (bare `0`…`7` also accepted) | `rd`/`rs` field | 0–7 |
| `imm16` | `123`, `0x1F`, `0b1010`, `1_000`, an expression, a label | `imm16` | 0–65535 |
| `imm8` | as `imm16`, low byte used | `imm16[7:0]` | warn if > 255 |
| `t16`, `d16` | absolute cycle count / 12.4 fixed delay | `imm16` | 0–65535 |
| `a6` | DMEM word address | `imm16[5:0]` | 0–63 |
| `rel16` | an absolute label or expression | `imm16` (assembler converts) | in range |

Expressions support `+ - * / % << >> & | ^ ~`, unary `-`, parentheses, `.equ` constants, labels,
and `.` (the current word address). No character or string literals; **`%` is modulo, not binary**.

## 3. Opcode map

<!-- BEGIN GENERATED: opcodes (python3 -c "import sw.isa as isa; print(isa.summary_table())") -->
| Op | Mnemonic | Operands | Semantics | Flags |
|---|---|---|---|---|
| `0x00` | `nop` | `` | no operation | — |
| `0x01` | `halt` | `` | halt this thread (halt=1, en unchanged; PC frozen) | — |
| `0x02` | `movi` | `rd, imm16` | R[rd] = imm16 | Z; C=0 |
| `0x03` | `mov` | `rd, rs` | R[rd] = R[rs] | Z; C=0 |
| `0x04` | `add` | `rd, rs` | R[rd] += R[rs] | Z,C |
| `0x05` | `addi` | `rd, imm16` | R[rd] += imm16 | Z,C |
| `0x06` | `sub` | `rd, rs` | R[rd] -= R[rs] | Z,C |
| `0x07` | `subi` | `rd, imm16` | R[rd] -= imm16 | Z,C |
| `0x08` | `and` | `rd, rs` | R[rd] &= R[rs] | Z; C=0 |
| `0x09` | `andi` | `rd, imm16` | R[rd] &= imm16 | Z; C=0 |
| `0x0A` | `or` | `rd, rs` | R[rd] \|= R[rs] | Z; C=0 |
| `0x0B` | `ori` | `rd, imm16` | R[rd] \|= imm16 | Z; C=0 |
| `0x0C` | `xor` | `rd, rs` | R[rd] ^= R[rs] | Z; C=0 |
| `0x0D` | `xori` | `rd, imm16` | R[rd] ^= imm16 | Z; C=0 |
| `0x0E` | `inv` | `rd` | R[rd] = ~R[rd] | Z; C=0 |
| `0x0F` | `shl` | `rd, n` | R[rd] <<= n (n=imm16[3:0]) | Z; C=0 |
| `0x10` | `shr` | `rd, n` | R[rd] >>= n (logical) | Z; C=0 |
| `0x11` | `rol` | `rd, n` | R[rd] = rol16(R[rd], n) | — |
| `0x12` | `ror` | `rd, n` | R[rd] = ror16(R[rd], n) | — |
| `0x13` | `cmp` | `rd, rs` | flags from R[rd] - R[rs] | Z,C |
| `0x14` | `cmpi` | `rd, imm16` | flags from R[rd] - imm16 | Z,C |
| `0x15` | `csrw` | `csr, rs` | CSR[imm16[7:0]] = R[rs] (thread bank if < 0x10) | — |
| `0x16` | `csrr` | `rd, csr` | R[rd] = CSR[imm16[7:0]] | — |
| `0x18` | `out` | `imm16` | pins_out = imm16 & 0xFCFF (host-link bits 9:8 masked) | — |
| `0x19` | `set` | `imm16` | pins_out \|= imm16 | — |
| `0x1A` | `clr` | `imm16` | pins_out &= ~imm16 | — |
| `0x1B` | `tog` | `imm16` | pins_out ^= imm16 | — |
| `0x1C` | `in` | `rd` | R[rd] = pins_in (combinational read) | — |
| `0x1D` | `dir` | `imm8` | DIR = imm16[7:0] (uio_oe) | — |
| `0x1E` | `dset` | `imm8` | DIR \|= imm16[7:0] | — |
| `0x1F` | `dclr` | `imm8` | DIR &= ~imm16[7:0] | — |
| `0x20` | `emit` | `rd` | enqueue timed event at NEXTT+PERIOD (frac accum), val=R[rd]; retry if queue full | — |
| `0x21` | `emita` | `rd, t16` | enqueue event at absolute TMR==imm16 (behind-schedule clamped to TMR+1), val=R[rd] | — |
| `0x22` | `emitd` | `rd, imm16` | enqueue event at TMR+imm16 (relative delay 1..65535, no behind-schedule clamp), val=R[rd] | — |
| `0x23` | `emite` | `rd` | enqueue edge event, val=R[rd]; fires when (pins_in&EMASK)==(ECOND&EMASK) | — |
| `0x24` | `setp` | `imm16` | PERIOD = imm16 (12.4 fixed point) | — |
| `0x25` | `setm` | `imm16` | TMASK = imm16 | — |
| `0x26` | `setem` | `imm16` | EMASK = imm16 (edge kind only) | — |
| `0x27` | `setec` | `imm16` | ECOND = imm16 | — |
| `0x28` | `inat` | `rd, t16` | stall until TMR==imm16, latch pins_in that cycle, R[rd]=latch (target now/overdue => sample in issue cycle) | — |
| `0x29` | `waitu` | `t16` | stall until TMR==imm16 (target now/overdue => no stall) | — |
| `0x2A` | `waitd` | `imm16` | stall imm16 (12.4) cycles, drift-free fractional accumulation | — |
| `0x2B` | `waitp` | `mask\|value` | retry until (pins_in[7:0] & imm16[15:8]) == imm16[7:0] (uio pins only) | — |
| `0x30` | `ld` | `rd, a6` | R[rd] = DMEM[imm16[5:0]] (single-cycle, combinational read) | — |
| `0x31` | `st` | `a6, rs` | DMEM[imm16[5:0]] = R[rs] (single-cycle, write commits end of cycle) | — |
| `0x32` | `ldi` | `rd, rs` | R[rd] = DMEM[R[rs][5:0]] (indirect, single-cycle) | — |
| `0x33` | `sti` | `rs, rd` | DMEM[R[rd][5:0]] = R[rs] (indirect, single-cycle) | — |
| `0x38` | `push` | `rd` | TXFIFO <<= R[rd][7:0]; retry if full | — |
| `0x39` | `pull` | `rd` | R[rd] = RXFIFO (zero-extended); retry if empty | — |
| `0x3A` | `pushi` | `imm8` | TXFIFO <<= imm16[7:0] | — |
| `0x40` | `jmp` | `rel16` | PC += sext(imm16) (word units) | — |
| `0x41` | `jz` | `rel16` | if Z: PC += sext(imm16) | — |
| `0x42` | `jnz` | `rel16` | if !Z: PC += sext(imm16) | — |
| `0x43` | `jc` | `rel16` | if C: PC += sext(imm16) | — |
| `0x44` | `jnc` | `rel16` | if !C: PC += sext(imm16) | — |
| `0x48` | `rpt` | `n` | repeat the following instruction n+1 times (imm16[7:0]) | — |
| `0x49` | `evset` | `imm8` | set event flag (bit imm16[2:0]) | — |
| `0x4A` | `evwait` | `imm8` | retry until event flag set, then clear it | — |
| `0x4B` | `irq` | `imm8` | raise host IRQ with code imm16[7:0] | — |
<!-- END GENERATED: opcodes -->

Two rows need reading carefully: **`waitp`**'s mask/value bytes apply to the **low 8 bits** of
`pins_in` (`P[7:0]`, the `uio` pins — see §4.5), and **`emit`** retries the instruction when the queue
is full, so an edge is never lost. Undefined opcodes retire as `nop` (forward compatibility). The gaps (`0x17`, `0x34–0x37`,
`0x3B–0x3F`, `0x45–0x47`, `0x4C+`) are deliberate.

## 4. Semantics, group by group

All instructions retire in **one cycle** unless they stall: an ALU op, a pin op, a CSR access, a
fetch, and `ld/ldi/st/sti` (DMEM is combinatorial) all complete in a single issue slot. Stalls come
only from (a) FIFO or event-queue back-pressure, (b) the explicit waits (`waitu`, `waitd`,
`waitp`, `inat`, `evwait`), and (c) `rpt` iterations.

### 4.1 Move and ALU

| Instruction | Result | Flags |
|---|---|---|
| `movi rd, imm16` / `li rd, imm16` | `rd = imm16` | `Z` set, `C` cleared |
| `mov rd, rs` | `rd = rs` | `Z` set, `C` cleared |
| `add/addi`, `sub/subi` | 16-bit wraparound result | `Z` and `C` (carry-out of bit 15; borrow for subtract) |
| `and/andi`, `or/ori`, `xor/xori` | bitwise | `Z` set, `C` cleared |
| `inv rd` | `rd = ~rd` | `Z` set, `C` cleared |
| `shl rd, n` / `shr rd, n` | logical shift by immediate `n` (0–15) | `Z` set, `C` cleared |
| `rol rd, n` / `ror rd, n` | 16-bit rotate | flags unchanged |
| `cmp rd, rs` / `cmpi rd, imm16` | computes `rd - rs` for the flags only | `Z`, `C` |

`rol`, `ror`, `in`, `ld`, `ldi`, `pull` and `csrr` leave the flags unchanged; every other
data-moving/logic op sets `Z` and clears `C` as shown, so `movi`/`mov`/`andi` can be used to test a
value for zero (`mov r2, r0; jz …`). There is no conditional move and no return stack: write
straight-line code, use `rpt` for repeats and `jmp` for tail loops.

```asm
    cmpi  r3, 0x20          ; Z=1 if r3 == 32
    jnz   not_ready         ; else keep waiting
```

### 4.2 CSR access

```asm
    csrw  TPERIOD, r4       ; CSR[0x00] = r4     (thread bank 0x00-0x0F)
    csrr  r5, TMR           ; r5 = CSR[0x12]     (global)
    csrw  POUT, r3          ; write pins_out directly (still masked with 0xFCFF)
```

Thread-code CSR numbers `0x00`–`0x0F` always address **the issuing thread's own bank**. The host
addresses thread `t`'s bank at `0x40 + 16*t + n`; thread code must not use those addresses. The
assembler accepts CSR names (case-insensitive) or raw numbers.

### 4.3 Pin operations

| Instruction | Effect |
|---|---|
| `out imm16` | `pins_out = imm16` **masked with `0xFCFF`**: bits `[9:8]` are the host-link pins and are forced to 0 in the register, so `csrr POUT` after `out 0xFFFF` reads `0xFCFF` |
| `set imm16` | `pins_out \|= imm16` |
| `clr imm16` | `pins_out &= ~imm16` |
| `tog imm16` | `pins_out ^= imm16` |
| `in rd` | `rd = pins_in` sampled this cycle, zero-extended |
| `dir imm8` / `dset imm8` / `dclr imm8` | replace / set / clear bits of `DIR` = `uio_oe[7:0]` |

Pin writes are *immediate*: unlike `emit*`, they take effect in the issue cycle itself. That makes
`out/set/clr/tog` the right tool for slow "static" signals (a chip select held for a whole
transaction) and `emit*` the right tool for anything with a timing requirement. Immediate writes
lose to scheduled events in the same cycle (`t0 > t1 > t2 > t3 > instruction`).

```asm
    dclr  0x01              ; release I2C_SCL (uio[0]) -> external pull-up takes it high
    dset  0x01              ; assert DIR bit 0 ...
    clr   0x0001            ; ... then drive it low: open-drain low
```

An open-drain line needs exactly two states: *driven low* and *released*. Because `DIR` is per pin,
`dclr PIN` releases the line (external pull-up takes it high) and `dset PIN` + `clr PIN` drives it
low — that is the I2C/1-Wire/PS-2 idiom, and it needs no tri-state buffer and no external part
beyond the pull-up. A protocol that drives push-pull (UART TX, SPI SCK) leaves `DIR` at 0 and
writes `uo` bits instead, which have no enable at all.

### 4.4 Event scheduling

| Instruction | Scheduled time | Pin value | Kind |
|---|---|---|---|
| `emit rd` | `NEXTT + PERIOD` (12.4 carry accumulator), then `NEXTT = t` | `rd` | timed |
| `emitd rd, d16` | `TMR + d16`, `d16` = 1…65535 (`0` ⇒ `TMR + 1`); **never clamped** | `rd` | timed |
| `emita rd, t16` | absolute `t16`; behind-schedule targets clamp to `TMR + 1` | `rd` | timed |
| `emite rd` | fires when `(pins_in & EMASK) == (ECOND & EMASK)` | `rd` | edge |

A timed event fires when `target == TMR` **or when it is overdue**
(`(target - TMR) & 0xFFFF >= 0x8000`) — so an armed edge can never be dropped, even if the
scheduler's look-ahead was exceeded; it fires on the next cycle instead of silently disappearing.

Enqueue-time clamping is a separate, narrower rule: `emit` (periodic chain) and `emita` (absolute
target) clamp a target that is already behind schedule to `TMR + 1`, while `emitd` takes a relative
delay in `1..65535` cycles and is never clamped (`d16 = 0` is defined as `TMR + 1`). In other words
the *firing* rule is "never drop an armed edge"; the *enqueue* rule is "an absolute target in the
past becomes the next cycle".

The value written on firing is `(pins_out & ~TMASK) | (value & TMASK)`, so a thread only disturbs
the pins it owns. `emit*` stalls the thread when the 4-deep queue is full; **the events already in
the queue are never affected**. `emite` events ignore `t` entirely — they are evaluated every cycle
against the live pin vector, which is what makes them glitch-free, and they are ordered behind any
timed events already queued.

```asm
    setp  0x1216            ; 115200 Bd at 33.33 MHz
    setm  0x0400            ; own P[10] (UART_TX in the info.yaml pin map)
    li    r0, 0x0400
    emit  r0                ; drive P10 high, PERIOD cycles from the last event
    emitd r0, 200           ; ... or 200 cycles from now
    emita r0, 1234          ; ... or exactly when TMR == 1234
    emite r0                ; ... or as soon as the EMASK/ECOND pattern appears
```

### 4.5 Waiting and timed sampling

| Instruction | Stalls until | Notes |
|---|---|---|
| `waitu t16` | `TMR == t16` | absolute; wrap-safe under the modular rules |
| `waitd d16` | `d16` cycles have elapsed | 12.4 fixed point, carry accumulated: drift-free (max 4095.9 cycles = 122.9 µs per call) |
| `waitp mask\|value` | `(pins_in[7:0] & mask) == value`, `mask = imm16[15:8]`, `value = imm16[7:0]` | watches **only the 8 bidirectional pins** `P[7:0]`; ≤ 2 cycles latency |
| `inat rd, t16` | `TMR == t16`, then `rd = pins_in` **sampled in that cycle** | zero sampling jitter |
| `evwait imm8` | event flag `imm8` is set, then clears it | thread-to-thread/host synchronisation |

**The `waitp` limitation, stated plainly:** its mask/value immediate is applied to `pins_in[7:0]`,
so it watches the 8 bidirectional (`uio`) pins and cannot see the input-only `ui` pins. Protocols
whose input lands on a `ui` pin therefore use a 3-cycle poll loop (`in`/`andi`/`jnz`), `inat`, or an
edge-triggered `emite` instead. That costs 0.6 % of a 115200-baud bit and nothing measurable at
I2C/PS-2/1-Wire rates — but choose the pin map with it in mind (`docs/architecture.md` §5).

`inat` is the decoder's instrument: the sample time is a compile-time constant, so a receiver that
knows the shared `TMR` grid samples exactly on the eye centre. `waitd` is the relative-delay tool
for data-dependent gaps; its 4-bit fraction accumulator keeps a chain of delays on the average
cycle count (never drifting by more than one cycle), which a plain loop of `nop`s cannot do.

```asm
    waitp 0x0A05            ; wait until (P[7:0] & 0b00001010) == 0b00000101
    waitd 0x1B21            ; 434.06 cycles = 1.5 bits @ 115200/33.33 MHz
    in    r0                ; centre of bit 0
```

### 4.6 Memory

`ld rd, a6` and `st a6, rs` address DMEM words 0–63 directly; `ldi rd, rs` and `sti rs, rd` use
`rs[5:0]`/`rd[5:0]` as an address, which is how you walk a buffer or a wave-player pattern table.
DMEM is 16-bit words, combinational read, one write port. `ld/st/ldi/sti` do **not** disturb the
flags. Reading an address the program did not initialise returns whatever the host loaded; DMEM
powers up undefined-content (reset does not clear it) — initialise what you read.

```asm
    li    r6, 0             ; index
    ldi   r2, r6            ; r2 = DMEM[r6]
    sti   r2, r6            ; DMEM[r6] = r2
```

### 4.7 FIFOs

`push rd` appends `rd[7:0]` to the thread's 8-byte TX FIFO (thread → host); `pull rd` removes the
head of the RX FIFO (host → thread) and zero-extends it. Both stall on back-pressure instead of
dropping data:

* `push` stalls while TX is full — the thread simply waits, the wire waveform is unaffected because
  its events are already queued;
* `pull` stalls while RX is empty — this is how a protocol thread idles waiting for host data.

`push`/`pull` do **not** modify the flags. Test occupancy via CSR `TFIFO`:
`rx_count = [11:8]`, `tx_count = [7:4]`, `tx_full = [1]`, `rx_empty = [0]`, bits `[3:2]` = 0.

### 4.8 Control flow, repeat, events, IRQ

`jmp/jz/jnz/jc/jnc rel16` are PC-relative in word units. The **assembler takes an absolute target**
(a label or expression) and encodes `rel16 = target - pc`, where `pc` is the jump instruction's own
address, so firmware remains relocatable and `--relocs` records where the fixups are. `.` is the
current PC, so `jmp .` is a spin.

A jump whose target comes from a register — or from a *newly written* `TPC` — resolves in the same
cycle: `csrw TPC, r3` immediately followed by `jmp rel16` redirects that cycle's fetch, with no
bubble and no stale fetch buffer (`verification.md` §9.2; `JUMP_TB` regression). Any write to `TPC`
— thread `csrw TPC` or the host's `SET_PC` command — **invalidates the thread's prefetched
instruction**, so there is no stale-word hazard and a host can re-point a thread at a new program
between `STOP` and `RUN` without a synchronisation dance. That is what makes "computed goto" a
supported idiom for thread dispatch, not an accident.

`rpt n` re-issues the following instruction `n+1` times without advancing the PC — a 1-instruction
loop that costs no branch slot, e.g. `rpt 7` + `shl r0, 1` = `shl` by 8.

`evset`/`evwait` are 8 event flags shared between threads and the host (CSR `EVT`). `irq imm8`
raises `H_IRQ` with a software-defined code in CSR `IRQ` (`0x14`); the CSR read is
**non-destructive**, and the only way to clear the code (and drop `H_IRQ`) is the host's `ACK_IRQ`
command, which writes CSR `IRQ = 0`.

## 5. CSR map

<!-- BEGIN GENERATED: csr (python3 -c "import sw.isa as isa; print(isa.csr_table())") -->
| CSR | Name | R/W | Bank | Bits | Meaning | Fields |
|---|---|---|---|---|---|---|
| `0x00` | `TPERIOD` | RW | per-thread | 16 | event period, 12.4 fixed point | int[15:4].frac[3:0] |
| `0x01` | `TMASK` | RW | per-thread | 16 | event output mask | — |
| `0x02` | `TSTATUS` | R | per-thread | 16 | thread status | bit1=halted, bit0=en, bits15:2=0 |
| `0x03` | `TFIFO` | R | per-thread | 16 | FIFO occupancy / full flags | [11:8]=rx_count, [7:4]=tx_count, bit1=tx_full, bit0=rx_empty |
| `0x04` | `TNEXTT` | R | per-thread | 16 | last scheduled emit time (16-bit wrapping) | — |
| `0x05` | `TEQ` | R | per-thread | 16 | event-queue occupancy | bits2:0=eq_count (0..4) |
| `0x06` | `TEMASK` | RW | per-thread | 16 | edge-trigger mask (emite kind only) | — |
| `0x07` | `TECOND` | RW | per-thread | 16 | edge-trigger expected value | — |
| `0x08` | `TPC` | RW | per-thread | 16 | program counter (10 meaningful bits) | — |
| `0x10` | `POUT` | RW | global | 16 | pins_out register (host bits 9:8 never written) | — |
| `0x11` | `PDIR` | RW | global | 8 | DIR / uio_oe[7:0] | — |
| `0x12` | `TMR` | R | global | 16 | free-running 16-bit cycle counter | — |
| `0x13` | `EVT` | RW | global | 8 | 8 event flags | — |
| `0x14` | `IRQ` | RW | global | 16 | host IRQ code (non-destructive read; ACK=write 0) | bit0..15 code |
| `0x15` | `WCTRL` | RW | global | 16 | wave player control | bit0=start (write-1 pulse), bit1=loop, bit2=oneshot (wins over loop); read returns last written data[2:0] |
| `0x16` | `WADDR` | RW | global | 6 | wave buffer start (DMEM word address) | — |
| `0x17` | `WLEN` | RW | global | 7 | wave buffer length in words (1..64) | — |
| `0x18` | `WPER` | RW | global | 16 | wave symbol period, 12.4 fixed point | — |
| `0x19` | `WSTATUS` | R | global | 16 | wave player status | bit1=busy, bit0=done |
| `0x1A` | `CAPCTRL` | RW | global | 16 | capture trigger config (stretch) | — |
| `0x1B` | `CAPSTAT` | R | global | 16 | capture status/count (stretch) | — |
| `0x1C` | `GSTATUS` | R | global | 16 | global status | bits8:5=en[3:0], bit0=irq, other bits 0 |
| `0x20` | `SOFTRST` | W | host | 1 | host-only soft reset (write 1): core only, not host bridge | — |
<!-- END GENERATED: csr -->

Bit layouts worth memorising (`[x:y]` in the column headers are bit positions):

| CSR | 15..12 | 11..8 | 7..4 | 3 | 2 | 1 | 0 |
|---|---|---|---|---|---|---|---|
| `TSTATUS` | 0 | 0 | 0 | 0 | 0 | `halted` | `en` |
| `TFIFO` | 0 | `rx_count[3:0]` | `tx_count[3:0]` | 0 | 0 | `tx_full` | `rx_empty` |
| `TEQ` | 0 | 0 | 0 | 0 | 0 | 0 | `eq_count[2:0]` (0–4) |
| `GSTATUS` | 0 | `en[3:0]` at [8:5] | 0 | 0 | 0 | 0 | `irq` pending |
| `WCTRL` | 0 | 0 | 0 | 0 | `oneshot` | `loop` | `start` (one-cycle pulse; read returns the last written `data[2:0]`, so `start` reads back as written) |
| `WSTATUS` | 0 | 0 | 0 | 0 | 0 | `busy` | `done` |

**Host addressing.** Per-thread CSRs live in bank `n = 0x00`–`0x0F`; the host reaches thread `t`
at `0x40 + 16*t + n`. Global CSRs (`0x10`–`0x1C`) and the host-only `SOFTRST` (`0x20`) are the
same from thread and host side.

## 6. Memory map

| Symbolic address | Host `SET_ADDR` → same pointer | Contents |
|---|---|---|
| `0x000`–`0x3BF` | PMEM word address (`a[9:0]`, 960 words reachable) | instruction words (32-bit) |
| `0x3C0`–`0x3FF` | DMEM word address (`a[5:0]`) | data words (16-bit) |
| outside the map | — | unmapped PMEM reads return `0x0200_0000` (`halt`) |

Unmapped PMEM reads return `0x0200_0000` = `halt`, so a runaway PC stops its own thread instead of
executing garbage. `src/` programs are loaded as 32-bit words through `WRITE_MEM`; the same command
pointer reaches DMEM, which is how wave-player pattern tables and (stretch) capture buffers are
written.

Reset state of the two memories is *not* defined content-wise (FF arrays have no reset to save
area); the host is expected to load PMEM before `RUN`, and firmware to initialise DMEM before use.
Execution starts at `PC = 0` after reset, and `RUN` executes whatever is there — always `SET_PC`
first.

## 7. Assembler

> **Timing constants:** every `TPERIOD`/`waitd` value in this document is for the **shipped 33.33 MHz**
> build (`CLOCK_PERIOD` 30 ns; 60 MHz failed timing at a 20.8 ns critical path). Firmware derives them
> from `CLOCK_HZ = 33333333` in its `.equ` sweep (`sw/examples/`, `sw/prog/manifest.json`), so
> re-targeting the clock again would be a constant sweep, not an ISA change.

`sw/loom_asm.py` (owner: toolchain) is a two-pass assembler with relocations.

```sh
python3 -m sw.loom_asm prog.asm -o prog.hex --format hex32 \
        --rom-words 1024 --fill 0x02000000 --lst prog.lst --sym prog.sym
```

| Option | Meaning |
|---|---|
| `--format hex32\|c_array\|bin` | one 8-digit hex word per line / C array / raw little-endian words |
| `--rom-words N` | pad the image to N words (PMEM size) with `--fill` |
| `--fill W` | pad word, default `0x02000000` (the real `halt` instruction — also what an unmapped PMEM word reads) |
| `--lst`, `--sym`, `--relocs` | listing, symbol table, relocation JSON |

Syntax: `;`, `//` and `#` start comments; `label:` on its own line or in front of an instruction;
directives `.equ NAME, expr`, `.word expr[, …]`, `.align n`, `.fill n[, value]`; no `.org`, no
`#include`, no macros. `li rd, imm` is the only pseudo (= `movi`). Data words can be mixed with code
(`.word`), which is how a program embeds a tiny lookup table.

## 8. Tutorial: a UART transmitter in 15 instructions

The whole point of LOOM is that the *program* never has to be cycle-counted. Here is a complete
115200-baud 8N1 transmitter. Timing decisions in this program: exactly two — `BITPER` and
`MIDBIT`-style constants. Everything else is logic.

```asm
; UART transmitter, 8N1, 115200 baud @ 33.33 MHz. TX on P[10] = uo[2].
.equ BITPER, 0x1216          ; 289.375 cycles/bit (12.4); RTL measures 289.35 = 115.20 kBd
.equ TX,     0x0400          ; P[10] in the pins_out vector

start:
    setm   TX                ; this thread only ever drives the TX pin
    setp   BITPER            ; every emit() is one bit after the previous one
send:
    pull   r0                ; block until the host pushes a byte
    shl    r0, 1             ; frame: b0 = start(0), b1..b8 = data LSB first
    ori    r0, 0x0200        ; b9 = stop(1)
    li     r1, 10            ; 10 bit cells
bitloop:
    mov    r2, r0
    andi   r2, 1             ; Z = 1 when the bit is 0
    jz     drive
    li     r2, TX            ; bit is 1 -> line high
drive:
    emit   r2                ; schedule at NEXTT + PERIOD, arm the next slot
    shr    r0, 1
    subi   r1, 1
    jnz    bitloop
    jmp    send
```

How it works, line by line:

1. `setm TX` writes `TMASK`, so although the *value* register holds a full 16-bit pin vector, only
   P13 is affected — other threads' pins are untouched.
2. `setp BITPER` loads the 12.4 period. From now on each `emit` schedules its event exactly one
   (fractional) bit after the previous one, with the carry accumulator handling the 0.8333.
3. `pull` blocks until the host writes a byte into this thread's RX FIFO with `TX_PUSH`. The host
   prepares the next byte while the previous frame is still being serialised — a FIFO in, a FIFO
   out, and the wire never notices host latency.
4. `shl`/`ori` build the 10-bit frame `{stop, data[7:0] LSB-first, start}`, with the start bit at
   bit 0.
5. The `emit` loop enqueues one event per bit cell. Ten events at `PERIOD` spacing are exactly one
   frame; consecutive frames are automatic because the next byte's start bit is scheduled at
   `NEXTT + PERIOD`, i.e. one bit cell after the stop bit was asserted — back-to-back framing with
   no gap arithmetic.
6. The loop takes about 8 issue slots per bit, but *the queue is filled faster than the wire drains
   it*: at 115200 the first four emits fit in the 4-deep queue and the fifth stalls the thread for
   ~289 cycles. That stall is invisible on the wire: events 1–4 fire on schedule, and the thread
   wakes with ~868 cycles of slack. At 1 Mbaud, still ~100 cycles of slack.
7. The old stop bit remains asserted until the next start bit's event fires, so the line idles high
   between frames — 8N1 by construction, not by luck.

Host-side, one frame is: `TX_PUSH 0, 'A'` (command `0x06`). The host can verify the waveform from a
cocotb VIP or a scope; the demonstration in `docs/examples.md` includes the expected 10-bit
waveform with cycle counts.

Relative timing you can *read off* the program: `BITPER = 0x1216` → 289.375 cycles → bit rate
`33 333 333 / 289.375 = 115 191 Bd` at the divider level, and the **RTL-measured** rate is
289.35 cycles/bit = **115.20 kBd** (+0.001 % — start-to-start, averaged over a frame). Frame time =
10 × 289.375 = 2893.75 cycles = 86.8 µs — unchanged in real time, because the clock and the constants
were both rescaled.

### Common variations

| Want | Change |
|---|---|
| 9600 baud | `.equ BITPER, 0x186A0` (6250 cycles; still 12.4-exact) |
| 921600 baud | `.equ BITPER, 0x00411` (65.0625 cycles, +0.086 %) |
| 7E1 or 9-bit | change the `shl`/`ori` frame construction and the bit count `r1` |
| inverted logic (RS-232 idle low) | `movi `TMASK` value and swap the two `li` values in the loop, or drive through an external inverter |
| two UARTs | copy the program to another thread, `setm` a different pin, push to its FIFO |

The receiver, an SPI engine, an I2C controller and a WS2812 driver follow the same pattern: one
`setm`, one `setp`, then a loop that pushes *events*. See [`examples.md`](examples.md) for all of
them; [`examples_src/`](examples_src/) holds the assembler-ready sources for this document, and
`sw/examples/` the firmware workstream's complete programs.

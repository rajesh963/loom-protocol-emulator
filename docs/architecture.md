# LOOM architecture

**What this is.** A beginner-friendly expansion of `spec/ARCHITECTURE.md` (the frozen v1.0
spec): what a *protocol loom* is, why splitting "what to drive" from "when to drive it" is the
whole idea, and how the four threads, the event schedulers, the wave player and the host bridge
fit together. Every number below is computed for the LOOM build: `clk = 60 MHz`, 8×4 tiles of
IHP CMOS5L, `NUM_THREADS = 4`, event queue depth 4, 8-byte FIFOs.

Read this first, then [`isa.md`](isa.md) for the instruction set, [`host-protocol.md`](host-protocol.md)
for how a host loads a program, and [`examples.md`](examples.md) for worked protocols.

![LOOM block diagram](images/block.svg)

---

A CPU bit-bangs a protocol by executing a store to a GPIO at the right moment, so **the edge time is
the instruction retirement time**. It depends on how many instructions the loop took, whether a
branch was taken, whether a FIFO load stalled, and what the other engines are doing. At 60 MHz a bit
is 520.8333 cycles at 115200 baud; padding the loop to an integer number of cycles is the standard
trick, but then the *fact* that the bit is 520.8333 cycles long lives in the programmer's head, not in
the program, and every added feature risks the waveform.

| Approach | Timing source | What it costs you |
|---|---|---|
| CPU bit-bang | instruction issue | every instruction is a timing decision |
| RP2040 PIO | SM instruction issue, 16.8-divided SM clock | 32 shared instruction words per block, one divider per SM for all its timings, edges land on SM-clock edges, program must be cycle-padded |
| TI PRU | instruction issue at 200 MHz | 8 KB program, cycle-counted loops, no fractional divider, resolution bought with clock rate |
| fixed-function blocks | dedicated counters | not reprogrammable after fabrication — exactly what the competition forbids |
| **LOOM** | **hardware event schedule vs. a 16-bit cycle counter** | the program says `(when, what)`; hardware owns the edge |

LOOM's thesis is the separation of concerns: **a thread publishes intent, the scheduler publishes
edges.** The instruction stream can then be as sloppy as it likes — branch, stall on a FIFO, loop —
because the edge was scheduled before the branch happened.

## 2. Timing, quantitatively

### 2.1 Cycles per bit at 60 MHz

| Protocol rate | cycles/unit | LOOM `PERIOD` (12.4 fixed) | register value | resulting rate | rate error |
|---|---|---|---|---|---|
| UART 9600 | 6250 | 6250.0000 | `0x186A0` | 9600.0 Bd | 0 |
| UART 19200 | 3125 | 3125.0000 | `0x0C350` | 19200.0 Bd | 0 |
| UART 38400 | 1562.5 | 1562.5000 | `0x061A8` | 38400.0 Bd | 0 |
| UART 115200 | 520.8333 | 520.8125 | `0x0208D` | 115205.4 Bd | +0.0046 % |
| UART 921600 | 65.1042 | 65.0625 | `0x00411` | 922390.0 Bd | +0.086 % |
| I2C 100 kHz (½ bit) | 300 | 300.0000 | `0x012C0` | 100 kHz | 0 |
| I2C 400 kHz (¼ bit) | 75 | 75.0000 | `0x004B0` | 400 kHz | 0 |
| SPI mode 0 @ 10 MHz (½ SCK) | 3 | 3.0000 | `0x00030` | 10 MHz | 0 |

`PERIOD` is a 16-bit **12.4 fixed-point** number: `int[15:4]` cycles plus `frac[3:0]` sixteenths
of a cycle, so the programming unit is `1/16` cycle = **1.042 ns at 60 MHz**, and the longest
representable period is 4095.9375 cycles (14.65 µs, i.e. a 34.1 kHz floor for a single-frequency
event stream — use `emitd`/`emita` for longer, irregular gaps).

### 2.2 The carry accumulator

`PERIOD` is not a divider that quantises every event independently. Each thread keeps a 4-bit
fractional accumulator `PFRAC`, and `emit` computes

```
sum    = PERIOD_int * 16 + PERIOD_frac + PFRAC      ; 20-bit intermediate
t      = NEXTT + (sum >> 4)                          ; integer cycle, wraps in 16 bits
PFRAC  = sum[3:0]                                    ; carry into the next event
NEXTT  = t
```

so the *average* period is exactly `PERIOD/16` cycles and the per-event phase error stays in
`(-1, +1)` cycle forever instead of growing by a fraction every bit — see
[`images/timing.svg`](images/timing.svg). For 115200 baud that is a bounded ±0.5 cycle
(±8.3 ns) error around an ideal 520.8333-cycle grid; UART tolerates ±2 % (10.4 cycles), so the
margin is a factor of 20. The first event is scheduled at `t = NEXTT + PERIOD`; `NEXTT` resets to
0, and reads are available in CSR `TNEXTT` (`0x04`).

### 2.3 Jitter compared with instruction-issue timing

Because the event is in the queue before the thread executes the instructions that *follow* the
emit, the thread's own execution time is irrelevant to the edge. The edge fires when the
scheduler's 16-bit comparator says so, which is a fixed hardware function of `TMR`. The only
software-visible jitter is:

1. `≤ 1` cycle, from the scheduler's fire cycle (an event scheduled for cycle *t* drives the pins
   on the clock edge that makes `TMR == t`);
2. an event scheduled *in the past* fires on the next cycle (by design, see §4.4);
3. `emit`-queue back-pressure: if the queue is full the `emit` stalls the thread, never the events
   already queued.

There is no bound on how many cycles a thread may take between two `emit`s, as long as it keeps
the queue non-empty. That is why a 4-deep queue is enough for a 10-bit UART frame at any baud
rate above ~1 Mbaud: the loop that builds the frame runs at >1 instruction/cycle and the queue
only needs to hide instruction latency, not protocol latency.

---

## 3. Execution model

### 3.1 Four threads, one datapath, one issue slot

There are `NUM_THREADS = 4` thread states. Each has its own PC (10 bits, word addressed), eight
16-bit registers `R0..R7`, flags `Z`/`C`, a stall counter, the timing state (`PERIOD`, `PFRAC`,
`NEXTT`, `TMASK`, `EMASK`, `ECOND`), a 4-entry event queue, and a pair of 8-byte host FIFOs. All
four share one ALU and one instruction port: **one instruction retires per cycle**, chosen
round-robin over runnable threads (`0,1,2,3`, skipping halted/disabled/stalled threads —
work-conserving, so a protocol waiting for its next bit does not waste the fetch slot).

A thread is *runnable* when `en = 1`, `halt = 0` and `stall = 0`. Stalls come from four places:
`waitu`/`waitd`/`inat` (timing), `emit`/`emita`/`emitd`/`emite` when the event queue is full,
`pull`/`push` FIFO back-pressure, and 2-cycle data-memory instructions.

**Priority when several pin writes land in the same cycle** (lowest to highest): instruction
`OUT/SET/CLR/TOG` < host CSR write to `POUT` < `thread3` < `thread2` < `thread1` < `thread0`, and an
active wave player overrides the whole merge (`pins_out = wave_pins`). This is deterministic and
documented so that overlapping protocols (a debug pin toggled by `tog` while an event scheduler
drives the same pin) behave predictably. It is the reason a protocol thread should set `TMASK` to
the pins it owns — the merge masks the event value:

```
pins_out <= (pins_out & ~TMASK_t) | (event_val & TMASK_t)     per thread, in priority order
```

Reset values that matter to firmware: `TMASK = 0xFFFF` (a thread owns *every* pin until it says
otherwise — a fresh thread that emits before `setm` will drive all of them), `PERIOD = 16`
(i.e. 1.0 cycle), `PFRAC = 0`, `EMASK = ECOND = 0` (an `emite` with default mask fires
immediately). Always programme `setm`/`setp`/`setem`+`setec` before the first enqueue.

### 3.2 Instruction fetch

PMEM is 1024 × 32-bit words, one synchronous read port, addressed by the issuing thread's PC. The
fetch is pipelined with issue: the PC register feeds PMEM, the fetched word is decoded the next
cycle, and a thread can issue every cycle unless it stalls. Data memory (DMEM, 64 × 16-bit) is an
FF array with combinational read, so `ld/st` take 2 cycles effectively, while `ldi/sti` add one
more cycle for the address computation. A runaway PC reads `0x0200_0000`, which decodes to
`halt` — a deliberate safety net for bad firmware.

### 3.3 Relocatable programs

All jumps are PC-relative (`rel16` in word units), so a program can be placed at any PMEM word
address by the host (`SET_PC`, CSR `TPC`). The assembler emits `rel16` values and a relocation
list; there is no `.org`. Four programs, one per thread, are typically loaded at `0x000`, `0x100`,
`0x200`, `0x300` and each thread's start address is set with `SET_PC` before `RUN`.

---

## 4. The event scheduler

![Event scheduler detail](images/eventqueue.svg)

### 4.1 The unit of work is an event

An event is a triple `{kind, t, val}` held in a per-thread 4-deep FIFO:

* `val` is a **16-bit pin vector** — the same encoding as `pins_out` (§5);
* `t` is a 16-bit absolute `TMR` value (modular; see §4.4);
* `kind` selects the firing rule: `time` (compare `TMR == t`) or `edge` (compare pin pattern).

Four instructions enqueue events:

| Instruction | `t` | Use |
|---|---|---|
| `emit rd` | `NEXTT + PERIOD` (fractional carry) | periodic waveforms: UART bits, I2C phases, clocks |
| `emitd rd, d16` | `TMR + d16` | one-shot relative gap of `d16` cycles |
| `emita rd, t16` | absolute `t16` | phase-locked/jitter-free alignment to a known grid |
| `emite rd` | fire on `(pins_in & EMASK) == (ECOND & EMASK)` | reactive/slave behaviour, glitch-free |

`NEXTT` is updated by **every** timed enqueue (not only `emit`), so a mixed sequence continues the
periodic chain from whichever event was scheduled last; `RUN` initialises `NEXTT` to the current
`TMR`, which makes the first `emit` after a run land exactly `PERIOD` later. `emite` never touches
`NEXTT`. `emit` is the workhorse: it turns "send the next bit every 520.8 cycles" into ten instructions per
frame, and the drift-free carry does the rest. `emita` exists for the case where several threads
must agree on a common phase (e.g. an SPI clock and a data line derived from a shared `TMR` grid)
without a software round-trip.

### 4.2 Firing

Every cycle the scheduler of each thread compares the head-of-queue `t` against `TMR`. A `time`
event fires when it is due (`t == TMR`) or **overdue** (`(t - TMR) & 0xFFFF >= 0x8000`), so an edge
that was armed can never be dropped — worst case it lands a cycle late instead of vanishing. An `edge` event fires when the masked pin
comparison holds; its `t` is ignored, which is what makes it *glitch-free*: the hardware samples
`pins_in` every cycle, so a 4-cycle-wide pulse cannot be missed the way it can by a polling loop.
On firing, `val` is written to `pins_out` under `TMASK`, and the queue advances. Events are ordered
(FIFO), so a thread can schedule a burst of edges ahead of time.

### 4.3 Back-pressure is safe by construction

If the queue is full, `emit` stalls the issuing thread and the other three threads keep running.
This is the correct failure mode for a timing engine: the *already scheduled* edges still happen
exactly when promised. Firmware should keep the queue fed; the depth-4 design means a thread must
re-enter `emit` at least once every 3 periods. For a 1 Mbaud UART that is every 180 cycles — about
30 instructions of slack — and for 115200 baud, every ~1560 cycles.

The `TEQ` CSR (`0x05`, `eq_count[2:0]`) exposes occupancy, so firmware can pick the number of
events to enqueue adaptively instead of blindly stalling.

### 4.4 Time is modular: the 1.09 ms wrap

`TMR` is 16 bits and free-running at 60 MHz, so it wraps every `65536 / 60 MHz = 1.0923 ms`. All
comparisons use the *signed* modular difference `int16(TMR - t)`; a time event is considered
reached when that difference is `0`. Consequences, which are part of the programming model:

* scheduling look-ahead must be **less than 32 768 cycles (546 µs)** — otherwise a "later" time
  looks like "1.09 ms ago";
* an event whose `t` is already in the past fires **on the next cycle**;
* there is no epoch, no counter high word, and nothing to reset between frames. Protocols whose
  unit interval is shorter than 546 µs (all of UART, SPI, I2C, PS/2, 1-Wire, WS2812, USB LS,
  10BASE-T bit times) fit comfortably.

Software that needs a longer gap chains `waitd` (also 12.4 accumulated) or re-arms `emit`.

### 4.5 What the scheduler does *not* do

It does not serialise events across threads, does not retry, and does not know protocol semantics.
If two threads schedule different values for the same pin at the same cycle, the priority rule in
§3.1 decides. If a thread schedules an event for a pin not in its `TMASK`, the bits are masked
out. Both are deliberate: a shared bus is a coordination decision for firmware, not a hardware
policy.

---

## 5. Pins

Tiny Tapeout gives 24 digital pads: 8 dedicated inputs `ui`, 8 dedicated outputs `uo`, 8
bidirectional `uio` with a per-pin output-enable `uio_oe`. LOOM spends 3 inputs and 2 outputs on
the host link, leaving **19 protocol pins**:

```
pins_out[15:0] : [15:10] = uo_out[7:2]   [9:8] = host (writes ignored)   [7:0] = uio_out[7:0]
pins_in [15:0] : [15: 8] = ui_in [7:0]   ( [2:0] are host pins)          [7:0] = uio_in [7:0]
DIR[7:0]       : uio_oe
```

| Physical pad | `ui`/`uo` index | Pin-vector bit | Direction | Notes |
|---|---|---|---|---|
| `ui[0]` | in 0 | `pins_in[8]` | in | `H_CS_N` host SPI select — **not** a protocol pin |
| `ui[1]` | in 1 | `pins_in[9]` | in | `H_SCK` host SPI clock — not a protocol pin |
| `ui[2]` | in 2 | `pins_in[10]` | in | `H_MOSI` host SPI data — not a protocol pin |
| `ui[3]` | in 3 | `pins_in[11]` | in | protocol input P11 |
| `ui[4..7]` | in 4..7 | `pins_in[12..15]` | in | protocol inputs P12..P15 |
| `uo[0]` | out 0 | `pins_out[8]` | out | `H_MISO` — not a protocol pin |
| `uo[1]` | out 1 | `pins_out[9]` | out | `H_IRQ` — not a protocol pin |
| `uo[2]` | out 2 | `pins_out[10]` | out | protocol output P10 |
| `uo[3..7]` | out 3..7 | `pins_out[11..15]` | out | protocol outputs P11..P15 |
| `uio[0..7]` | — | `pins_in/out[0..7]`, `DIR[7:0]` | bidir | protocol pins P0..P7, true open-drain-capable |

So: **19 protocol pins** (5 input-only + 6 output-only + 8 bidirectional), **14 independently
drivable** (6 + 8), **13 readable** (5 + 8). Counting the two host-link pins as protocol pins
would give 24, which is wrong — and in the pin vector `P[9:8]` are the host outputs, `P[10:8]`
read back the host inputs, so a bit's *in* and *out* halves can belong to different pads. For
example `pins_out[10]` is `uo[2]` (protocol) while `pins_in[10]` is `ui[2]` (`H_MOSI`).

`DIR[i] = 1` makes `uio[i]` an output; `DIR[i] = 0` makes it high-impedance (an input with the
external pull-up holding it high). Because `DIR` is per pin and writable mid-frame (`dir`, `dset`,
`dclr`, or CSR `PDIR`), **open-drain I2C, PS/2 and 1-Wire work with no external parts**: drive 0
by setting the pin low with `DIR=1`, release by setting `DIR=0`. The protocol pins are otherwise
completely generic — the mapping in `info.yaml` is only the example firmware's choice.

---

## 6. Timed sampling

A protocol *decoder* has the opposite problem from a driver: it must read a pin at a specific
cycle, not "soon". LOOM has three tools:

* **`inat rd, t16`** — stall the thread until `TMR == t16`, latch `pins_in` *in that cycle*, and
  write it to `rd` when the thread resumes. Zero sampling jitter: the sample time is a compile-time
  constant, so a receiver that knows the absolute grid (the usual case: `TMR` is shared by all
  threads) samples exactly on the ideal eye centre.
* **`waitu t16`** — stall until an absolute cycle. Use before an `in` when the value is not needed
  in a register.
* **`waitd d16`** — stall for a *relative* 12.4 fixed-point delay with the same drift-free carry
  accumulator as `emit`, so a chain of `waitd`s stays aligned to a fractional clock.
* **`waitp mask|value`** — poll until `(pins_in[7:0] & mask) == value`, where the mask and expected
  value come from the immediate's two bytes. Note the granularity: `waitp` sees **only P[7:0]**, the
  eight bidirectional pins — the same bits an open-drain protocol (I2C, PS/2, 1-Wire) lives on. It is
  a cheap edge detector for exactly those protocols; for `ui` pins use a two-instruction polling
  loop (`in`/`andi`/`jnz`, 3 cycles).

`inat`/`waitu`/`waitd`/`emit*` share the same stall machinery, so mixing them in one thread is
allowed: a thread can sample a pin at `t`, then immediately enqueue the next output edge.

---

## 7. Wave player

Some protocols are faster than a 1-instruction/cycle loop can drive: 10BASE-T needs a new line
state every 8-10 cycles (2 bits per symbol at 10 Mbaud with Manchester encoding), and USB LS needs
a 1.5 Mbaud bit cell with 4× oversampling. For those, LOOM includes a **wave player**: a
peripheral that streams 16-bit pin patterns out of DMEM.

* `WADDR` (DMEM word address), `WLEN` (1..64 patterns; `0` behaves as `1`), `WPER` (symbol period,
  12.4), `WCTRL`
  (`start`, `loop`, `oneshot`), `WSTATUS` (`busy`, `done`);
* `wave_pins` updates once per `WPER` cycles with the same carry accumulator (drift-free), reading
  the next DMEM word one cycle ahead;
* the first pattern appears exactly `WPER` cycles after `start`;
* with `WLEN = 64` and a period of 1 cycle, the sustained symbol rate is one 16-bit pin pattern
  **per clock = 60 M patterns/s**, which is 10BASE-T class with room for the Manchester encoder
  running in a thread concurrently.

The wave player participates in the same pin priority scheme as the event queues, so firmware can
choose: bit-bang a preamble with `emit`, then start the wave player for the payload, then go back
to `emit`.

---

## 8. Host bridge and memory

The host bridge is a 4-wire SPI slave (mode 0, MSB first) on `ui[0..2]`/`uo[0]` with a level IRQ
on `uo[1]`. SCK is asynchronously sampled with a 2-FF synchroniser and an edge detector, so the
core clock never sees an external clock and the design stays a single-clock design for the
LibreLane flow (target 60 MHz, `CLOCK_PERIOD` 16.67 ns). At 10 MHz SCK each byte takes 800 ns,
so a 1024-word program loads in ~30 ms including command bytes. Details in
[`host-protocol.md`](host-protocol.md).

Memory:

| Region | Depth × width | Implementation | Who writes |
|---|---|---|---|
| PMEM | 1024 × 32 | FF array (simulation / area-safe fallback: 128 words) **or** IHP `1024x32` 1-port SRAM macro | host only |
| DMEM | 64 × 16 | FF array, 1 CPU read + 1 CPU write + 1 wave-player read port | host, threads, wave player |
| TX/RX FIFOs | 8 × 8 per thread | FF, fall-through read | host ⇄ threads |

---

## 9. Reset, determinism, and what "bit-exact" means here

`rst_n` is asynchronous and active low. It resets everything: threads (disabled, halted, PC 0,
registers 0), queues and FIFOs empty, `pins_out = 0`, `DIR = 0` (all `uio` high-Z inputs),
`TMR = 0`, wave player off, host bridge idle, `H_MISO = 0`, `H_IRQ = 0`. The design is
*reset-complete*: no flop is left with an undefined value, which is a formal property in
[`verification.md`](verification.md), not an aspiration.

"Bit-exact" in this document means: for a given program, `TMR` value of `RUN`, and host traffic
schedule, the *cycle number* of every pin transition is reproducible in RTL simulation, in
gate-level simulation and (modulo the host's SPI timing, which only affects which cycle a byte
arrives, not the wire timing after that) on silicon. The host load time is not part of the
protocol waveform: firmware arms its schedule after the last `RUN` bit.

---

## 10. Area strategy for 8×4 tiles

8×4 = 32 tiles ≈ 32 × (202 × 155) µm ≈ **1.00 mm²** of nominal area, with a rule of thumb of about
1K logic cells per tile (~32K cells). The budget is dominated by **PMEM**: 1024 × 32 flops is far
too big for the die, so the flagship build uses the IHP `RM_IHPSG13_1P_1024x32` 1-port SRAM macro
(416.64 × 336.46 µm = 140 183 µm², **~14 % of the die**) with the `pdn_cfg.tcl` power-grid setup and
`Project.ExtendPowerStripes` LibreLane plugin from the Tiny Tapeout CMOS5L SRAM example. The
`PMEM_IMPL="ff", PMEM_WORDS=128` build is the zero-risk fallback (also the simulation build), so both
are always buildable and exercised in CI. Per-thread state (4 × registers, PC, flags, queue, timing
registers, 8-byte FIFOs) is a few thousand flops; the ALU and scheduler are combinational. If the
flow complains, drop in this order: capture/record-replay, wave player, `EQ_DEPTH`/thread count
(parameters). Measured numbers: [`results.md`](results.md); the measurement script lives in
`tools/`.

---

## 11. Extensibility after fabrication

Nothing user-visible is hard-wired. After the chip comes back:

* **new protocol** = new program loaded over SPI, no bitstream, no mask;
* **record & replay** (stretch) = a capture engine timestamps pin transitions into DMEM and a
  replay mode feeds those timestamps back through the same queues — the same hardware, reversed;
* **co-processing** = the host can treat LOOM as a protocol *engine*: push bytes into a thread's
  RX FIFO, read bytes from its TX FIFO, and let the threads own the wire;
* **new instructions** — the opcode space is 7 bits with gaps left deliberately (undefined opcodes
  retire as `nop`), so a future revision can add ops without breaking old programs.

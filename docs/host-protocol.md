# LOOM host protocol

**What this is.** The wire-level specification of the 4-wire SPI link that loads and drives LOOM:
how a byte is transferred, what the chip puts on MISO during every transfer of every command,
byte-for-byte tables for all 13 commands, IRQ behaviour, and complete example sessions with the
actual bytes. It is written against `rtl/loom_host.v` (owner: hostbus) and the `host/` Python
client; if a host driver and this document disagree, this document is the bug report.

Pins: `ui[0]=H_CS_N`, `ui[1]=H_SCK`, `ui[2]=H_MOSI`, `uo[0]=H_MISO`, `uo[1]=H_IRQ`.

---

## 1. Electrical / framing rules

| Property | Value |
|---|---|
| SPI mode | 0 (CPOL = 0, CPHA = 0), MSB first |
| MOSI sampled by LOOM on | `H_SCK` **rising** edge |
| MISO shifted by LOOM on | `H_SCK` **falling** edge |
| Max `H_SCK` | `clk / 6` = **10 MHz** at 60 MHz (11 MHz at 66 MHz) |
| CS setup / hold | ≥ 3 `clk` (50 ns) before the first rising edge, ≥ 3 `clk` after the last |
| Multi-byte values | **least-significant byte first** |
| Idle MISO | `0x00` |
| Idle H_IRQ | 0 (level, active high) |

There is **no over-speed detection and no clock stretching**: a slave cannot stall SCK, so if the
host runs faster than `clk/6` the 2-FF synchroniser misses edges and bytes are corrupted silently.
Respect the limit.

`H_SCK` is asynchronous to the core clock. It is sampled through a 2-flip-flop synchroniser and an
edge detector, so the LOOM core is a single-clock (60 MHz) design; the host link is the only place
where an external clock exists, and it never reaches the protocol engines.

### One byte on the wire

```
H_CS_N  ‾‾‾\___________________________________________________________/‾‾‾
            |                                                           |
H_SCK   ____|_/‾‾‾\_/‾‾‾\_/‾‾‾\_/‾‾‾\_/‾‾‾\_/‾‾‾\_/‾‾‾\_/‾‾‾\___________|____
              ^     ^     ^     ^     ^     ^     ^     ^
              b7    b6    b5    b4    b3    b2    b1    b0        (MOSI sampled on rising edges)
H_MOSI  ====X=b7==X=b6==X=b5==X=b4==X=b3==X=b2==X=b1==X=b0==X=====
H_MISO  ====X=c7==X=c6==X=c5==X=c4==X=c3==X=c2==X=c1==X=c0==X=====
                  (LOOM shifts the next MISO bit out on each falling edge)
              |<------------------ 8 x SCK ------------------>|
```

`H_CS_N` must stay low for the **whole** command, including every argument and every payload byte
of that command. Deasserting CS mid-command aborts it: the partial byte is discarded, unread payload
is dropped, and the next CS assertion starts a fresh command. Per-byte CS toggling is not supported.

---

## 2. What MISO means on each transfer

Every transfer is 8 clocks and moves one byte in and one byte out. The chip does not have a
"status pipeline": the meaning of each MISO byte is fixed by the command that is currently open.

| Transfer | MOSI | MISO |
|---|---|---|
| T1 (first transfer after CS falls) | command byte | **`0x80`** = engine idle, command accepted (`0x81` = busy is reserved and never generated in normal operation, because CS assertion always re-arms the engine) |
| argument transfers | argument bytes, LSB first | `0x00` |
| payload transfers | `0x00` (ignored) | the command's response, LSB first |

Consequences the host driver must implement:

* **The command byte is always the first transfer** after CS falls — there is no dummy byte, and
  there is no one-transfer status pipeline: the `0x80` on T1 belongs to the command being sent.
* For commands with **no arguments** (`READ_MEM`, `STATUS`, `ACK_IRQ`), the payload starts on T2.
* Clock **exactly** the documented number of transfers, then raise CS. A command's transfer count is
  fixed; for `ACK_IRQ` that is **one** transfer only (an extra clocked byte would be consumed as the
  next argument/command byte).
* A byte clocked while the bridge is mid-command is consumed as that command's next transfer; it is
  never re-decoded as a new command.

`0x81` exists in the encoding but is not observable: CS deassertion aborts any in-flight command and
re-arms the accept path, so a fresh window always answers `0x80`. Drivers may ignore it.

---

## 3. Command set

<!-- BEGIN GENERATED: hostcmds (python3 -c "import sw.isa as isa; print(isa.host_command_table())") -->
| CMD | Name | Args (host→chip) | Returns | Meaning |
|---|---|---|---|---|
| `0x01` | `SET_ADDR` | a[23:0] (3B, LSB first) | status | set word address pointer |
| `0x02` | `WRITE_MEM` | w[31:0] (4B, LSB first) | status | MEM[addr] = w; addr++ |
| `0x03` | `READ_MEM` | - | w[31:0] (4B, LSB first) | MEM[addr]; addr++ |
| `0x04` | `WRITE_CSR` | csr[7:0], d[15:0] (3B, LSB first) | status | CSR write |
| `0x05` | `READ_CSR` | csr[7:0] | d[15:0] (2B, LSB first) | CSR read |
| `0x06` | `TX_PUSH` | t[1:0], d[7:0] | status | push byte to thread t RXFIFO |
| `0x07` | `RX_POP` | t[1:0] | d[7:0] | pop byte from thread t TXFIFO |
| `0x08` | `SET_PC` | t[1:0], pc[15:0] | status | thread PC |
| `0x09` | `RUN` | mask[3:0] | status | clear halt, en \|= mask |
| `0x0A` | `STOP` | mask[3:0] | status | en &= ~mask |
| `0x0B` | `STATUS` | - | status byte per thread | {t3..t0 status bytes} |
| `0x0C` | `ACK_IRQ` | - | status | clear host IRQ latch |
| `0x0D` | `PING` | 0x5A | 0xA5 | link check |
<!-- END GENERATED: hostcmds -->

### 3.1 Byte-by-byte tables for the multi-byte commands

`T1` = first transfer after `H_CS_N` falls. "in" = MOSI byte the host sends, "out" = MISO byte the
chip returns during that transfer. All multi-byte values are LSB first.

**`0x01` `SET_ADDR` — 4 transfers** · sets the word-address pointer used by `WRITE_MEM`/`READ_MEM`,
which auto-increments after every access (so a program is loaded with one `SET_ADDR` followed by N
`WRITE_MEM` commands). The 10-bit address map is PMEM `0x000`–`0x3BF`, DMEM `0x3C0`–`0x3FF`.

| | T1 | T2 | T3 | T4 |
|---|---|---|---|---|
| in | `01` | `a[7:0]` | `a[15:8]` | `a[23:16]` |
| out | `80` | `00` | `00` | `00` |

**`0x02` `WRITE_MEM` — 5 transfers** · `MEM[addr] = w; addr++`

| | T1 | T2 | T3 | T4 | T5 |
|---|---|---|---|---|---|
| in | `02` | `w[7:0]` | `w[15:8]` | `w[23:16]` | `w[31:24]` |
| out | `80` | `00` | `00` | `00` | `00` |

**`0x03` `READ_MEM` — 5 transfers** · no arguments: the payload starts on T2

| | T1 | T2 | T3 | T4 | T5 |
|---|---|---|---|---|---|
| in | `03` | `00` | `00` | `00` | `00` |
| out | `80` | `w[7:0]` | `w[15:8]` | `w[23:16]` | `w[31:24]` |

**The remaining commands** (all start with the command byte on T1, MISO `80`):

| CMD | Transfers | MOSI after T1 | MISO after T1 |
|---|---|---|---|
| `04` `WRITE_CSR` | 4 | `csr`, `d[7:0]`, `d[15:8]` | `00` × 3 |
| `05` `READ_CSR` | 4 | `csr`, `00`, `00` | `00`, then `csr[7:0]`, `csr[15:8]` |
| `06` `TX_PUSH` | 3 | `{6'b0,t[1:0]}`, `d[7:0]` | `00` × 2 |
| `07` `RX_POP` | 3 | `{6'b0,t[1:0]}`, `00` | `00`, then the byte (`00` if the FIFO is empty) |
| `08` `SET_PC` | 4 | `{6'b0,t[1:0]}`, `pc[7:0]`, `pc[15:8]` | `00` × 3 |
| `09` `RUN` / `0A` `STOP` | 2 | `mask[3:0]` | `00` |
| `0B` `STATUS` | 5 | `00` × 4 | `s0`, `s1`, `s2`, `s3` (threads 0..3) |
| `0C` `ACK_IRQ` | 1 | — | — (raise CS right after T1) |
| `0D` `PING` | 2 | any byte (conventionally `5A`; not validated) | `A5` |

`RUN` sets `en |= mask`, `halt = 0`, `stall = 0` and `NEXTT = TMR` for the selected threads — the
last assignment is what makes the first `emit` of a freshly-run thread land exactly `PERIOD` cycles
after the `RUN` byte. `STOP` clears `en` and freezes the thread with its state intact. A `STATUS`
byte `sn` is `[7]=halt, [6]=en, [5]=tx_full, [4]=tx_empty, [3]=rx_full, [2]=rx_empty, [1:0]=00`.

### 3.2 CSR addressing pitfall (VER-003)

`SET_PC` writes `TPC`, which **invalidates the thread's prefetched instruction**: there is no
stale-word hazard when a host re-points a thread at a new program, and no extra dummy `RUN` is
needed to flush a pipeline. (Writing `WCTRL`'s `start` bit likewise reads back as written; the
one-cycle start pulse is generated in hardware.) Thread-local CSR addresses `0x00`–`0x0F` are only
meaningful *inside* a thread. A **host** read or
write of `0x00`–`0x0F` addresses no thread bank: reads return `0x0000` and writes are ignored. To
reach thread *t*'s bank the host must use `0x40 + 16*t + n` (`t` = 0..3, `n` = 0..0x0F), e.g.
thread 2's `TPERIOD` is CSR `0x60`.

The RTL has separate thread and host CSR read paths, so a host read of a *global* CSR (`POUT`,
`PDIR`, `TMR`, `EVT`, `IRQ`, `W*`, `GSTATUS`) cannot be poisoned by a thread's `CSRR` address
register. That was not true in the first cocotb bring-up: global reads decoded from the thread-side
address latch and returned stale thread-bank data (`test/integration` now asserts the host reads
`POUT = 0x0000` and `TMR = 0x04A0` at the documented point in its script). Keep the rule in mind
when writing a host driver: pass the *host* CSR number, never a thread-local one.

## 4. IRQ behaviour

`H_IRQ` is a **sticky, registered level**: it asserts when the core's IRQ register (CSR `0x14`) is
non-zero — written by the thread instruction `irq code` — and is cleared **only** by `ACK_IRQ`
(`0x0C`), which writes CSR `0x14 = 0`. Reading CSR `0x14` is **non-destructive** (the code stays
until acknowledged).

* FIFO occupancy conditions (RX not empty, TX full/empty) do **not** raise `H_IRQ` by themselves;
  firmware raises an explicit `irq` code when it wants the host's attention, or the host polls
  `STATUS`/`READ_CSR`.
* The latch only changes at byte boundaries, so it can never glitch inside a transfer.
* Recommended host pattern: drive `H_IRQ` to a GPIO interrupt; on interrupt, run one `STATUS`
  (5 transfers) and then `ACK_IRQ` (1 transfer), then drain `RX_POP` as needed.

Interrupt codes are protocol-defined. Suggested convention used by the example firmware:
`0x01` = frame received, `0x02` = TX underrun, `0x03` = framing/parity error, `0x40+t` = thread `t`
halted.

---

## 5. Captured wire traces

These are **real captures**, not hand-written examples: `test/unit_host/run.sh` bit-bangs SCK at
10 MHz against `rtl/loom_host.v` at `clk = 60 MHz` and dumps every transfer to
`test/unit_host/build/tb_host.log`. Format: `txn N xfer K MOSI/MISO` with bytes in hex, T1 = first
transfer after CS falls.

```
; --- PING (txn 1) ---
xfer0  0d/80      command accepted
xfer1  5a/a5      link is alive

; --- SET_ADDR 0x000010 (txn 2) ---
xfer0  01/80      SET_ADDR
xfer1  10/00      a[7:0]
xfer2  00/00      a[15:8]
xfer3  00/00      a[23:16]

; --- WRITE_MEM 0xDEADBEEF (txn 3) ---
xfer0  02/80      WRITE_MEM, pointer auto-increments after the payload
xfer1  ef/00      w[7:0]
xfer2  be/00      w[15:8]
xfer3  ad/00      w[23:16]
xfer4  de/00      w[31:24]

; --- two READ_MEM back to back in ONE CS window (txn 5) ---
01/80 10/00 00/00 00/00 | 03/80 00/ef 00/be 00/ad 00/de | 03/80 00/44 00/33 00/22 00/11
   SET_ADDR 0x10        | READ_MEM -> 0xDEADBEEF           | READ_MEM (addr 0x11) -> 0x11223344

; --- WRITE_CSR 0x10 (POUT) = 0xCAFE (txn 6) ---
04/80 10/00 fe/00 ca/00

; --- READ_CSR 0x10 (txn 7) ---
05/80 10/00 00/fe 00/ca

; --- RX_POP thread 2 (txn 9) ---
07/80 02/00 00/3c      t=2, one byte 0x3C from the thread's TX FIFO

; --- STATUS (txn 15) ---
0b/80 00/54 00/90 00/64 00/94
   s0=0x54 {halt=0,en=1,tx_full=0,tx_empty=1,rx_full=0,rx_empty=1}
   s1=0x90 {halt=1,en=0,tx_full=0,tx_empty=1,rx_full=0,rx_empty=0}
   s2=0x64 {halt=0,en=1,tx_full=1,tx_empty=0,rx_full=1,rx_empty=0}   TX FIFO full, RX has a byte
   s3=0x94 {halt=1,en=0,tx_full=0,tx_empty=1,rx_full=0,rx_empty=0}

; --- ACK_IRQ (txn 17) ---
0c/80                  no payload: CS goes high immediately after this transfer

; --- abort at a byte boundary (txn 22) ---
02/80 33/00            WRITE_MEM interrupted: CS high -> nothing written, payload discarded
                       next window is a fresh command (txn 23 PING -> 0d/80, 5a/a5)
```

Three behaviours the trace makes concrete: the command byte always answers `0x80`; read payloads
appear on the transfers immediately *after* the command byte (no dummy phase); and a CS abort at a
byte boundary discards a partial command cleanly, so the next window starts fresh.

### 5.1 A complete load-and-run session

A host-side sequence for the UART example (thread 0 = transmitter at PMEM word 0), annotated with
what LOOM does. Bytes are from the client API, not a capture.

```
h.ping()                       -> 0xa5
h.load_hex(".../uart_tx.hex")  -> SET_ADDR 0x000000, then one WRITE_MEM per word
                                  (pmem auto-increments; 15 words for the UART sketch)
h.set_pc(0, 0x0000)            -> SET_PC t=0
h.run(0b0001)                  -> RUN mask=0b0001: en=1, halt=0, NEXTT=TMR
h.tx_push(0, ord("A"))         -> TX_PUSH: 'A' lands in thread 0's RX FIFO
                                  thread 0 pulls it and schedules 10 events;
                                  the frame is on the wire 87 us later
h.status()                     -> e.g. b"\x02\x02..." (en=1 for the running thread)
```

`RUN` initialising `NEXTT = TMR` is what makes the first `emit` land exactly `PERIOD` cycles after
the run command — the same byte sequence always produces the same waveform.

---

## 6. Host client and demo board

The reference client (`host/loom_host.py`, toolchain-owned, in progress) is specified to implement
this protocol over any `spi_transfer(bytes) -> bytes` transport, so the same code runs against the
cocotb RTL, the Python ISA model and a real board. The API below is the target interface; **the
normative artefact is §1–§4 of this document**, and `test/unit_host/run.sh` +
[`test/host.py`](../test/host.py) exercise the wire protocol today:

```python
from host.loom_host import LoomHost, SpiTransport

h = LoomHost(SpiTransport(sck_hz=8_000_000))
assert h.ping()                       # 0x0D -> 0xA5
h.load_hex("sw/examples/uart_tx.hex") # SET_ADDR + WRITE_MEM loop
h.set_pc(0, 0x0000)
h.run(0b0001)                         # start thread 0
h.tx_push(0, ord("A"))                # feed the UART
print(h.status())                     # four status bytes
print(h.rx_pop(0))                    # bytes the firmware pushed
```

### 6.1 Tiny Tapeout demo board (RP2350)

The demo board's RP2350 is the natural host. The chip's pads land on these RP2350 GPIOs:

| LOOM pad | RP2350 GPIO | LOOM pad | RP2350 GPIO |
|---|---|---|---|
| `clk` | GPIO16 | `rst_n` | GPIO14 |
| `ui[0..7]` | GPIO17–24 | `uio[0..7]` | GPIO25–32 |
| `uo[0..7]` | GPIO33–40 | | |

`H_CS_N/H_SCK/H_MOSI` are `ui[0..2]` = GPIO17–19 and `H_MISO` is `uo[0]` = GPIO33. The RP2350's
hardware SPI0 can plausibly be routed to CS/SCK/MOSI on GPIO17–19, but its MISO is on GPIO33, which
is not the conventional SPI0 MISO pin — so **the reference host implements the SPI master in PIO (or
bit-bangs it)**. Any 4-wire SPI master up to ~10 MHz SCK works: the protocol has no clock stretching,
no dummy byte and no minimum transfer length constraints beyond "clock exactly the documented
number of bytes, then raise CS".

A minimal MicroPython/PIO transport is ~20 lines: shift 8 bits per transfer, `cs.value(0)`, loop the
transfer pattern, `cs.value(1)`. Because LOOM's program/data memories are loaded word-wise, the same
loader works for the FF build and for the SRAM build.

---

## 7. Limits and errata

| Limit | Consequence |
|---|---|
| one command per CS-low window | a second command byte in the same window is consumed as an argument |
| no busy/stall signalling | the host must not exceed `clk/6`; over-speed corrupts bytes silently |
| `ACK_IRQ` is one transfer | clocking a "filler" byte would be decoded as an argument/command |
| `READ_MEM`/`STATUS` payload has no dummy phase | the first payload byte rides in the transfer right after the command byte |
| `0x81 busy` never observable | treat any non-`0x80` T1 response as a link fault |
| 8-byte FIFOs | the host must drain `RX_POP` before a thread's 8-byte TX FIFO fills, or firmware `push` stalls (timing on the wire is unaffected — that is the point of the FIFOs) |
| thread-local CSRs from the host | `0x00`–`0x0F` read as 0 from the host side; use `0x40 + 16*t + n` |

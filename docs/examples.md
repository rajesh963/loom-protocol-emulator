# LOOM protocol examples

**What this is.** A walkthrough of every protocol LOOM targets — UART, SPI (controller and target),
I2C, PS/2, 1-Wire, WS2812, plus the stretch goals USB low-speed and 10BASE-T — with the pin
mapping, the timing budget at 60 MHz, the event-scheduling strategy, and an assembler-ready code
sketch. Every sketch below compiles with the repo's assembler:

```sh
python3 -m sw.loom_asm docs/examples_src/<name>.asm -o /dev/null   # from the repo root
```

The pin names refer to the example assignment published in [README](../README.md#pinout) and
`info.yaml`. It is only a convention: `setm`/`out` address the pin *vector*, so moving a protocol to
another pin is a constants change. Bit values: `uo[2..7]` → `P[10..15]`, `ui[3..7]` → `P[11..15]`,
`uio[0..7]` → `P[0..7]`. The mapping the sketches assume:

| Signal | Pin | Vector bit | Signal | Pin | Vector bit |
|---|---|---|---|---|---|
| UART_TX | `uo[5]` | `0x2000` (out) | UART_RX | `ui[3]` | `0x0800` (in) |
| SPI_SCK | `uo[2]` | `0x0400` (out) | SPI_MISO | `ui[4]` | `0x1000` (in, controller) |
| SPI_MOSI | `uo[3]` | `0x0800` (out) | SPI_CS_N | `uo[4]` | `0x1000` (out, controller) |
| I2C_SCL | `uio[0]` | `0x0001` | I2C_SDA | `uio[1]` | `0x0002` |
| PS2_CLK | `uio[2]` | `0x0004` | PS2_DAT | `uio[3]` | `0x0008` |
| ONEWIRE | `uio[4]` | `0x0010` | WS2812_DATA | `uo[6]` | `0x4000` (out) |
| WAVE_IN0/1 | `ui[6:7]` | `0x4000`/`0x8000` (in) | WAVE_OUT0 | `uo[7]` | `0x8000` (out) |

Remember that `P[10]`'s *output* is `uo[2]` while its *input* is `H_MOSI`, and `P[12]`'s output is
`SPI_CS_N` while its input is `SPI_MISO` — the pin vector overlaps by design, so a thread that only
writes never reads a host pin by accident and vice versa. `TMASK` (reset value `0xFFFF`) keeps
threads apart; set it first.

---

## 1. UART

### 1.1 Transmitter (115200 Bd, 8N1)

| Quantity | Cycles @ 60 MHz | Value |
|---|---|---|
| bit cell | 520.8333 | `PERIOD = 0x208D` (520.8125, +0.0046 %) |
| frame (10 bit) | 5208.33 | 86.80 µs |
| queue refill deadline | 3 periods = 1562 | > 150 instructions of slack |
| host latency tolerated | 8 bytes deep × 86.8 µs = 694 µs | FIFO, not wire |

**Strategy.** One periodic event stream: `setp` once, then `emit` one pin vector per bit cell. The
scheduler owns the wire; the loop that builds the frame can branch, stall, and fetch from the host
FIFO without touching the timing.

```asm
; UART transmitter, 8N1, 115200 baud @ 60 MHz. TX on P[13] = uo[5].
.equ BITPER, 0x208D          ; 520.8125 cycles/bit (12.4 fixed) -> 115205 Bd
.equ TX,     0x2000          ; P[13] in the pins_out vector

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

Other rates: 9600 → `0x186A0`, 19200 → `0x0C350`, 38400 → `0x061A8`, 921600 → `0x00411`. Changing
the rate is one `.equ`; there is no divider, prescaler or code re-padding.

### 1.2 Receiver (115200 Bd)

| Quantity | Cycles | Note |
|---|---|---|
| start-edge hunt | 3-cycle loop | `in`/`andi`/`jnz`, ±3 cycles = 0.6 % of a bit |
| first sample | 1.5 bits = 781.25 | start-edge → bit-0 centre |
| subsequent samples | + 520.8125 each | `waitd` fractional accumulator, drift-free |
| stop-bit sample | + 9.5 bits | framing check |

**Strategy.** Acquisition is a tight polling loop (`waitp` only sees the `uio` pins, and `UART_RX`
is a `ui` pin); after the edge, the fractional `waitd` chain owns the sample instants. Each `in`
executes at a fixed cycle after the wait expires, so the sample point is deterministic even though
the thread is not cycle-counted as a whole.

```asm
; UART receiver, 8N1, 115200 baud @ 60 MHz. RX on P[11] = ui[3].
.equ BITPER,  0x208D         ; 520.8125 cycles
.equ MIDBIT,  0x30D4         ; 1.5 bits = 781.25 cycles
.equ RX,      0x0800         ; P[11] in the pins_in vector

rx:
    setm   0                 ; input thread: events must not drive
hunt:
    in     r2                ; poll for the start edge (3-cycle loop)
    andi   r2, RX
    jnz    hunt              ; loop while P[11] is high
    waitd  MIDBIT            ; move to the centre of bit 0
    li     r0, 0
    li     r1, 8
bitloop:
    shr    r0, 1
    in     r2                ; sample pins_in at this exact cycle
    andi   r2, RX
    jz     no_bit
    ori    r0, 0x80
no_bit:
    waitd  BITPER
    subi   r1, 1
    jnz    bitloop
    in     r2                ; stop bit, 9.5 bits after the start edge
    andi   r2, RX
    jz     rx
    push   r0                ; hand the byte to the host
    jmp    rx
```

A 4-thread configuration runs TX, RX and two more protocols at once; each owns its own `TMASK`
bits and `PERIOD`, and the round-robin issue slot interleaves their loops without affecting any
scheduled edge.

---

## 2. SPI

### 2.1 Controller, mode 0, transmit-only

| SCK rate | Half period | `PERIOD` | `PERIOD` (hex) |
|---|---|---|---|
| 1 MHz | 30 cycles | 30.0000 | `0x01E0` |
| 2 MHz | 15 cycles | 15.0000 | `0x00F0` |
| 4 MHz | 7.5 cycles | 7.5000 | `0x0078` |
| 10 MHz | 3 cycles | 3.0000 | `0x0030` |

**Strategy.** Two events per bit: one for the data-setup phase (SCK low, MOSI valid) and one for the
SCK rising edge. Both are emitted on the same periodic stream, so the SCK duty cycle and the data
setup are exact and independent of loop overhead. Modes 1/3 shift the same events by one half
period; mode 2 inverts the SCK bit in the two emitted values. At 10 MHz the queue depth limits a
thread to ~4 pending events, which is still two complete bits of look-ahead.

```asm
; SPI controller, mode 0, transmit only, 1 MHz SCK @ 60 MHz.
; SCK=P[10]=uo[2], MOSI=P[11]=uo[3], CS_N=P[12]=uo[4]
.equ SCK,  0x0400
.equ MOSI, 0x0800
.equ CSN,  0x1000
.equ HALF, 0x01E0            ; 30 cycles = half of a 1 MHz SCK period

spi:
    setm  SCK|MOSI|CSN       ; own all three wires
    setp  HALF               ; one event per half SCK period
    li    r2, CSN            ; CS asserted, SCK low, MOSI low
    emit  r2
send:
    pull  r0                 ; next byte from the host
    li    r1, 8
bitloop:
    mov   r3, r0
    andi  r3, 0x80           ; MSB first
    shl   r3, 4              ; -> MOSI bit (0x0800)
    ori   r3, CSN            ; CS held low, SCK low: data setup
    emit  r3
    ori   r3, SCK            ; rising edge: the target samples MOSI
    emit  r3
    shl   r0, 1
    subi  r1, 1
    jnz   bitloop
    li    r3, CSN            ; SCK low after the last bit
    emit  r3
    li    r3, 0              ; release CS
    emit  r3
    jmp   send
```

Full-duplex read is the one place the periodic stream is not enough: the controller must sample
MISO while the slave is driving it. Two idioms work:

* *read-late*: sample MISO with an `in` in the loop and accept that the value is the bit that was
  valid at the last SCK edge (mode-0 MISO is stable from the falling edge to the next rising edge,
  so this is correct as long as the loop keeps up with the SCK rate);
* *synchronise*: poll `TEQ` until the scheduled "SCK high" event has fired, then `in` — bounded by
  the queue, not by instruction counting.

### 2.2 Target / slave (reactive)

For target mode the same three wires must be *inputs*. Re-mux them to the spare bidirectional pins
(`uio[5..7]` = `SPARE_B0..2`) and drive MISO from `uo[7]` (`0x8000`):

| Signal | Pin | Vector bit |
|---|---|---|
| SCK_T | `uio[5]` | `0x0020` (in) |
| MOSI_T | `uio[6]` | `0x0040` (in) |
| CS_T_N | `uio[7]` | `0x0080` (in) |
| MISO_T | `uo[7]` | `0x8000` (out) |

**Strategy.** This is what `emite` is for. Program `EMASK = CS_T | SCK_T` and `ECOND = SCK_T`
(CS low, SCK high). An edge event fires **on the cycle the pattern appears**, with no polling
latency and no possibility of missing a short SCK pulse. The thread arms the next bit as soon as the
previous one has fired (`TEQ` drops to 0), so the bit value is chosen by firmware while the *edge*
stays hardware-timed.

```asm
; SPI target, mode 0, on the spare bidirectional pins.
.equ SCK_T,  0x0020          ; uio[5]
.equ MOSI_T, 0x0040          ; uio[6]
.equ CS_T,   0x0080          ; uio[7]
.equ MISO_T, 0x8000          ; uo[7]

target:
    dir    0x00              ; SCK_T/MOSI_T/CS_T are inputs; all uio released
    setm   MISO_T            ; our only output
    setem  CS_T|SCK_T        ; EMASK: watch the chip select and the clock
    setec  SCK_T             ; ECOND: fire when CS is low and SCK is high
    pull   r0                ; byte to return, supplied by the host
    li     r1, 8
bit:
    mov    r3, r0
    andi   r3, 0x80
    jz     zero
    li     r2, MISO_T
    jmp    armed
zero:
    li     r2, 0
armed:
    waitp  0x0080            ; let SCK return low before re-arming (CS stays low)
    emite  r2                ; drive MISO on the next (CS low & SCK high)
poll:
    csrr   r4, TEQ
    cmpi   r4, 0
    jnz    poll              ; wait until the event has fired
    shl    r0, 1
    subi   r1, 1
    jnz    bit
    push   r0                ; byte received on MOSI would be read with `in` similarly
    jmp    target
```

(`waitp` sees only `P[7:0]`, which is exactly where the remuxed target pins live — the design
coincidence is deliberate: the bidirectional half of the pin vector is where slave protocols live.)

---

## 3. I2C controller (100 kHz and 400 kHz)

| Quantity | 100 kHz | 400 kHz |
|---|---|---|
| bit period | 600 cycles (10 µs) | 150 cycles (2.5 µs) |
| quarter phase | 150 cycles = `0x0960` | 37.5 cycles = `0x0258` |
| 9-bit byte (8 + ACK) | 5400 cycles (90 µs) | 1350 cycles (22.5 µs) |
| START/STOP setup | 150 cycles | 37.5 cycles |

**Strategy.** I2C is open-drain: `uio_oe` is the tri-state control and an event cannot write it, so
the *edges* are immediate `dset`/`dclr` pin operations while the *delays* go through the
fractional `waitd` chain (drift-free, so a long transfer never accumulates clock error). At 100 kHz
a cycle of instruction jitter is 0.0017 % of a bit; the protocol's only timing requirements are
minimum setup/hold, which the 150-cycle quarter phase satisfies with a factor of ~6. As an I2C
*target* the `emite` pattern matcher gives glitch-free START/STOP detection (see §2.2's idiom with
`EMASK = SCL|SDA`).

```asm
; I2C controller, 100 kHz, open drain on P[0]=I2C_SCL, P[1]=I2C_SDA (uio[0:1]).
; DIR bit = 1 pulls the line low, DIR bit = 0 releases it to the external pull-up.
.equ SCL,     0x0001
.equ SDA,     0x0002
.equ QUARTER, 0x0960         ; 150 cycles = 2.5 us

i2c:
    dset  SCL|SDA            ; enable both drivers
    li    r5, 0
    csrw  POUT, r5           ; both drive 0 when enabled
    dclr  SCL|SDA            ; release: both lines idle high
    waitd QUARTER
    dset  SDA                ; START: SDA falls while SCL is high
    waitd QUARTER
    dset  SCL
    waitd QUARTER
    li    r1, 8
sbit:
    mov   r2, r0
    andi  r2, 0x80
    jz    slow
    dclr  SDA                ; data 1: release SDA
    jmp   sclk
slow:
    dset  SDA                ; data 0: pull SDA low
sclk:
    dclr  SCL                ; SCL high: the target samples SDA
    waitd QUARTER
    dset  SCL                ; SCL low
    waitd QUARTER
    shl   r0, 1
    subi  r1, 1
    jnz   sbit
    dclr  SDA                ; ACK slot: release SDA
    dclr  SCL                ; SCL high
    waitd QUARTER
    in    r4                 ; sample SDA: 0 = ACK, 1 = NAK
    andi  r4, SDA
    dset  SCL
    waitd QUARTER
    dset  SDA                ; STOP: SDA rises while SCL is high
    dclr  SCL
    waitd QUARTER
    dclr  SDA
    waitd QUARTER
    jmp   i2c
```

For 400 kHz change `QUARTER` to `0x0258` (37.5 cycles). Nothing else changes — that is the point
of a fixed-point delay: the same program is a different bus rate.

---

## 4. PS/2 (device → host)

| Quantity | Cycles @ 60 MHz |
|---|---|
| clock period (10–16.7 kHz) | 3600–6000 |
| 11-bit frame | 39 600–66 000 (0.66–1.1 ms) |
| sampling | on each falling CLK edge |

**Strategy.** PS/2 is slow and glitch-prone: the keyboard's clock can stop for milliseconds between
bytes. `waitp` (which sees `P[7:0]`, where both PS/2 lines live) is the natural edge detector, and
`in` immediately after it samples DAT a couple of cycles after the falling edge — margins are
10–25 µs, so 100 ns of jitter is irrelevant. PS/2 *device* emulation (LOOM as the keyboard) uses the
same `emite` pattern matcher as §2.2 to answer a host request-to-send.

```asm
; PS/2 host (device-to-host), ~11 kHz clock from the keyboard.
; PS2_CLK = P[2] = uio[2] (0x04), PS2_DAT = P[3] = uio[3] (0x08)
ps2:
    dir    0x00              ; both lines are inputs (external pull-ups)
    li     r0, 0
    li     r1, 8
bitloop:
    shr    r0, 1             ; make room for the bit at LSB-first position 7
    waitp  0x0404            ; CLK high
    waitp  0x0400            ; CLK low: falling edge, data is valid
    in     r2
    andi   r2, 0x0008        ; DAT
    jz     no_bit
    ori    r0, 0x0080
no_bit:
    subi   r1, 1
    jnz    bitloop
    waitp  0x0404            ; parity bit
    waitp  0x0400
    waitp  0x0404            ; stop bit
    waitp  0x0400
    push   r0                ; scan code to the host
    jmp    ps2
```

---

## 5. 1-Wire

| Phase | Duration | Cycles | Value |
|---|---|---|---|
| reset pulse | 480 µs | 28 800 | 8 × `waitd 0xE100` (60 µs each) |
| presence sample | ~30 µs after release | 1800 | `waitd 0x7080` |
| write-1 low | ~6 µs | 360 | `0x1680` |
| write-0 low | ~60 µs | 3600 | `0xE100` |
| read slot: low then sample | 6 µs / 9 µs | 360 / 540 | `0x1680` / `0x21C0` |
| recovery | 60 µs | 3600 | `0xE100` |

**Strategy.** `waitd`'s maximum is `0xFFFF/16 = 4095.9` cycles (68.3 µs), so long 1-Wire pulses are
built from chained `waitd`s. Because the accumulator is fractional, 8 × 60 µs is exactly 28 800
cycles, not 28 800 ± rounding. `ONEWIRE` is `uio[4]`, so `DIR` is the open-drain control.

```asm
; 1-Wire reset/presence on P[4] = ONEWIRE = uio[4] (0x10), open drain.
.equ DQ,   0x0010
.equ W60,  0xE100            ; 3600 cycles = 60 us
.equ W30,  0x7080            ; 1800 cycles = 30 us
.equ W6,   0x1680            ;  360 cycles =  6 us
.equ W9,   0x21C0            ;  540 cycles =  9 us

ow_reset:
    dset   DQ                ; enable the driver
    clr    DQ                ; pull DQ low
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60
    waitd  W60               ; 480 us reset pulse
    dclr   DQ                ; release: pull-up restores idle high
    waitd  W30               ; sample the presence pulse at ~30 us
    in     r2
    andi   r2, DQ
    jz     present           ; 0 = a device answered
    jmp    ow_reset

; one read time slot: low 6 us, release, sample at 9 us
present:
    dset   DQ
    clr    DQ
    waitd  W6
    dclr   DQ
    waitd  W9
    in     r3
    andi   r3, DQ            ; r3 = 0 for a 0 bit, 0x10 for a 1 bit
    waitd  W60               ; recovery
    jmp    ow_reset
```

---

## 6. WS2812 / SK6812

| Quantity | Cycles @ 60 MHz |
|---|---|
| bit period (800 kHz) | 75 |
| T0H (0.4 µs) | 24 |
| T1H (0.8 µs) | 48 |
| reset latch (>50 µs) | > 3000 |
| 24-bit pixel | 1800 (30 µs) |
| 60-pixel strip | 108 000 (1.8 ms) |

**Strategy.** This is the one protocol where the edge times are data-dependent, so neither a single
`PERIOD` nor a wave-player pattern applies directly: the two edges of each bit go out as `emitd`
one-shots relative to `TMR`, and a `waitd` pads the loop to exactly 75 cycles. WS2812 tolerates
±150 ns (±9 cycles) per bit, so the ±1-cycle loop quantisation is invisible. The `nop` keeps both
data paths the same length; that is the only cycle-counting in the program, and it is one
instruction, not a whole protocol.

```asm
; WS2812 / SK6812 stream at 800 kHz (75 cycles/bit @ 60 MHz).
; LED data on P[14] = WS2812_DATA = uo[6] (0x4000). T0H = 24, T1H = 48 cycles.
.equ LED,   0x4000
.equ SPIN,  0x0410            ; waitd 65 cycles; loop body = 10 cycles -> 75
.equ RESET, 0xF000            ; 3840 cycles = 64 us low

pixel:
    setm   LED
    li     r5, LED            ; constant "high" value for the rise edge
    li     r3, 0              ; constant "low" value for the fall edge
    pull   r0                 ; one byte from the host = one colour component
    li     r1, 8
bitloop:
    mov    r2, r0
    andi   r2, 0x80           ; MSB first
    jz     bit0
    emitd  r5, 0              ; rise (fires next cycle)
    emitd  r3, 48             ; fall after 48 cycles = a '1'
    jmp    next
bit0:
    emitd  r5, 0
    emitd  r3, 24             ; fall after 24 cycles = a '0'
    nop                       ; pad so both branches take the same time
next:
    waitd  SPIN               ; 65 + 10 = 75 cycles per bit
    shl    r0, 1
    subi   r1, 1
    jnz    bitloop
    jmp    pixel
```

A complete strip driver adds a CRC-free 24-bit-per-pixel loop with GRB ordering and holds the line
low for ≥ 50 µs (`waitd RESET`) between frames.

---

## 7. Wave player: sustained high-rate patterns

| Use | Pattern length | `WPER` | Result |
|---|---|---|---|
| WS2812 at 800 kHz, pre-encoded | 8 patterns × 1 cycle each = 8 cycles/bit? | — | not needed, see §6 |
| 10BASE-T Manchester symbols | 64 patterns | 6 cycles (100 ns) | 10 Mbaud, 6-cycle symbol |
| arbitrary 16-bit waveform | 64 patterns | 1 cycle | 60 M patterns/s |

**Strategy.** Anything that is a repeating or precomputed pin-pattern sequence does not need the
thread at all: preload DMEM with 16-bit pin vectors and let the wave player walk them, one pattern
per `WPER` fractional cycles. A thread only sets the CSRs and gets out of the way:

```asm
; Wave player: stream 16 patterns/s from DMEM. The host preloads DMEM first.
wavedemo:
    li    r6, 0x0000          ; pattern table starts at DMEM word 0
    csrw  WADDR, r6
    li    r7, 16              ; 16 patterns
    csrw  WLEN, r7
    li    r1, 0x00C0          ; 12.0 cycles per pattern (12.4 fixed point)
    csrw  WPER, r1
    li    r2, 0x0003          ; start = 1, loop = 1
    csrw  WCTRL, r2
done:
    halt
```

---

## 8. Stretch: USB low-speed

USB LS is 1.5 Mbit/s = 40 cycles per bit at 60 MHz, with NRZI encoding, bit stuffing after six
identical bits, a 7-bit sync and a CRC-5/CRC-16. The plan: one thread does the NRZI/bit-stuffing
state machine and emits a symbol every 40 cycles with `setp 640` (`0x0280`), while the host streams
the packet payload through the FIFO — the protocol logic is real firmware, the *bit* timing is the
scheduler's. Sampling the upstream traffic uses `emite` on the `D+`/`D-` pins with a 4×
oversampling strategy: `EMASK` = both data lines, sample with `inat` on a 10-cycle (0.25-bit) grid.
USB's own bit-rate tolerance (±0.25 %) is far wider than LOOM's fractional period error
(≤ 0.09 % at 1.5 Mbit/s), so the timing side is not the risk; the packet/CRC state machine and the
host's USB stack are.

## 9. Stretch: 10BASE-T

10BASE-T is 10 Mbaud Manchester: a 100 ns symbol, 6 cycles at 60 MHz, with the line state changing
every half symbol (2 bits per symbol, one transition per bit). At 6 cycles per symbol the bit-banger
would need an event every 6 cycles — possible with `emit` (`PERIOD = 0x0060` = 6.0 cycles) but with
almost no slack for the encoder. The wave player is the intended tool: precompute 64 symbols
(384 cycles = 6.4 µs) encoding a preamble/start-of-frame/CRC-32 bit-stream, `WPER = 0x0060`, and
reload the pattern table from a thread that runs the MAC state machine. Peak sustained rate is
therefore 16 patterns per 96 cycles of DMEM reads, which fits comfortably inside the 6-cycle
per-pattern budget at `WLEN ≤ 64`.

---

## 10. Example program status

"Sketch here, assembles" means the listing in this document is accepted verbatim by
`sw/loom_asm.py` (and the copies in [`examples_src/`](examples_src/) are the exact files CI
assembles). "planned" means the complete program lives with the firmware workstream in
`sw/examples/`. Nothing in this document claims a program has been run on hardware — see
[`verification.md`](verification.md) for what is actually proven, and [`info.md`](info.md#example-programs)
for the one-table summary.

| Program | Threads | Pins | Rate | Core instruction | Status |
|---|---|---|---|---|---|
| `uart_tx` / `uart_rx` | 1 each | UART_TX / UART_RX | 9600–921600 | `setp`+`emit` / polling+`waitd` | sketch here, assembles |
| `spi_master` (mode 0) | 1 | SCK/MOSI/CS | ≤ 10 MHz | `setp`+`emit` | sketch here, assembles |
| `spi_master` modes 1–3 | 1 | same | as above | constant changes | planned |
| `spi_target` | 1 | re-mux `uio[5..7]` | as master | `setem`/`setec`/`emite` | sketch here, assembles |
| `i2c_master` | 1 | SCL/SDA | 100 / 400 kHz | `waitd` + `dset`/`dclr` | sketch here, assembles |
| `i2c_target`, `ps2_device` | 1 each | SCL/SDA, PS2_CLK/DAT | bus rate | `emite` pattern match | planned |
| `ps2_host`, `onewire`, `ws2812`, `wave_player` | 1 each | see §4–§8 | see §4–§8 | `waitp`/`in`, chained `waitd`, `emitd`, CSRs | sketch here, assembles |
| `uart_echo`, `capture_replay`, `usb_ls`, `10base_t` | 1–2 | various | various | mixed | planned / stretch |

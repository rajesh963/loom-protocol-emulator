# LOOM protocol examples

**What this is.** A walkthrough of every protocol LOOM targets — UART, SPI (controller and target),
I2C, PS/2, 1-Wire, WS2812, plus the stretch goals USB low-speed and 10BASE-T — with the pin
mapping, the timing budget at 33.33 MHz (the shipped clock), the event-scheduling strategy, and an assembler-ready code
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
| UART_TX | `uo[2]` | `0x0400` (out) | UART_RX | `ui[3]` | `0x0800` (in) |
| SPI_SCK | `uio[5]` | `0x0020` | SPI_MISO | `uio[7]` | `0x0080` (in/driven per role) |
| SPI_MOSI | `uio[6]` | `0x0040` | SPI_CS_N | `uo[3]` | `0x0800` (out, controller) |
| SPI_CS_IN | `ui[4]` | `0x1000` (in, target) | TRIG_IN | `ui[5]` | `0x2000` (in) |
| I2C_SCL | `uio[0]` | `0x0001` | I2C_SDA | `uio[1]` | `0x0002` |
| PS2_CLK | `uio[2]` | `0x0004` | PS2_DAT | `uio[3]` | `0x0008` |
| ONEWIRE | `uio[4]` | `0x0010` | WS2812_DATA | `uo[4]` | `0x1000` (out) |
| WAVE_IN0/1 | `ui[6:7]` | `0x4000`/`0x8000` (in) | WAVE_OUT0/1 | `uo[6:7]` | `0x4000`/`0x8000` (out) |

These are the names published in `info.yaml`. Individual sketches below (and the shipped programs in
`sw/examples/`) declare their own `.equ` pin map in the listing header, exactly as any firmware would
— the mapping is software, which is the point of the architecture. Where a sketch differs from the
table above, the header comment in that sketch is authoritative for that sketch.

Remember that `P[10]`'s *output* is `uo[2]` (`UART_TX`) while its *input* is `H_MOSI`, and `P[12]`'s
output is `WS2812_DATA` while its input is `SPI_CS_IN` — the pin vector overlaps by design, so a thread that only
writes never reads a host pin by accident and vice versa. `TMASK` (reset value `0xFFFF`) keeps
threads apart; set it first.

---

## 1. UART

### 1.1 Transmitter (115200 Bd, 8N1)

| Quantity | Cycles @ 33.33 MHz | Value |
|---|---|---|
| bit cell (design) | 289.3518 | `PERIOD = 0x1216` = 289.375 cyc (divider −80 ppm) |
| bit cell (RTL-measured) | **289.35** | **115.20 kBd (+0.001 %)** — start-to-start averaged over a frame |

*Measurement definition:* the UART rate is the **start-to-start interval averaged over a frame**
(10 bit cells), measured on the RTL pin trace — not the instantaneous cell period, which is 289.375
cycles from the divider.
| frame (10 bit) | 2893.75 | 86.81 µs (unchanged in real time) |
| queue refill deadline | 3 periods = 868 | > 150 instructions of slack |
| host latency tolerated | 8 bytes deep × 86.8 µs = 694 µs | FIFO, not wire |

**Strategy.** One periodic event stream: `setp` once, then `emit` one pin vector per bit cell. The
scheduler owns the wire; the loop that builds the frame can branch, stall, and fetch from the host
FIFO without touching the timing.

```asm
; UART transmitter, 8N1, 115200 baud @ 33.33 MHz. TX on P[10] = uo[2].
.equ BITPER, 0x1216          ; 289.375 cycles/bit (12.4 fixed) -> 115.2 kBd
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

Other rates: 9600 → `0x186A0`, 19200 → `0x0C350`, 38400 → `0x061A8`, 921600 → `0x00411`. Changing
the rate is one `.equ`; there is no divider, prescaler or code re-padding.

### 1.2 Receiver (115200 Bd)

| Quantity | Cycles | Note |
|---|---|---|
| start-edge hunt | 3-cycle loop | `in`/`andi`/`jnz`, ±3 cycles = 1.0 % of a bit |
| first sample | 1.5 bits = 434.06 | start-edge → bit-0 centre |
| subsequent samples | + 289.375 each | `waitd` fractional accumulator, drift-free |
| stop-bit sample | + 9.5 bits | framing check |

**Strategy.** Acquisition is a tight polling loop (`waitp` only sees the `uio` pins, and `UART_RX`
is a `ui` pin); after the edge, the fractional `waitd` chain owns the sample instants. Each `in`
executes at a fixed cycle after the wait expires, so the sample point is deterministic even though
the thread is not cycle-counted as a whole.

```asm
; UART receiver, 8N1, 115200 baud @ 33.33 MHz. RX on P[11] = ui[3].
.equ BITPER,  0x1216         ; 289.375 cycles
.equ MIDBIT,  0x1B21         ; 1.5 bits = 434.0625 cycles
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
| 1 MHz | 16.67 cycles | 16.6250 | `0x010A` |
| 2 MHz | 8.33 cycles | 8.3125 | `0x0085` |
| 4 MHz | 4.17 cycles | 4.1875 | `0x0043` |
| 8 MHz | 2.08 cycles | 2.0625 | `0x0021` |

The shipped firmware runs a nominal 32 cycles/bit (`QUARTER_CYCLES=4`, ~1.03 MHz) and **RTL measures
31.5–32 cycles/bit = 1.04–1.06 MHz SCK** standalone, with a ceiling of ~1.63 MHz and ~0.3–0.6 MHz
when all four threads are busy.

**Strategy.** Two events per bit: one for the data-setup phase (SCK low, MOSI valid) and one for the
SCK rising edge. Both are emitted on the same periodic stream, so the SCK duty cycle and the data
setup are exact and independent of loop overhead. Modes 1/3 shift the same events by one half
period; mode 2 inverts the SCK bit in the two emitted values. At the ~1 MHz shipped rate the queue
depth (4 events = 2 bits) is ample, and even the ~1.6 MHz ceiling keeps one bit of look-ahead.

```asm
; SPI controller, mode 0, transmit only, 1 MHz SCK @ 33.33 MHz (canonical info.yaml map).
; SCK=P[5]=uio[5], MOSI=P[6]=uio[6], CS_N=P[11]=uo[3]  (MISO would be P[7]=uio[7], input)
.equ SCK,  0x0020            ; P[5]  = uio[5]
.equ MOSI, 0x0040            ; P[6]  = uio[6]
.equ CSN,  0x0800            ; P[11] = uo[3]
.equ DIRB, 0x0060            ; DIR bits 5:6 -> SCK/MOSI are outputs
.equ HALF, 0x010A            ; 16.625 cycles = half of a ~1 MHz SCK period

spi:
    dir   DIRB               ; uio pins need DIR=1 to drive
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
    shr   r3, 1              ; bit 7 -> bit 6 = MOSI
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
| CS_T_N | `ui[4]` = `SPI_CS_IN` | `0x1000` (in) |
| MISO_T | `uo[5]` = `SPARE_OUT` | `0x2000` (out) |

**Strategy.** This is what `emite` is for. Program `EMASK = CS_T | SCK_T` and `ECOND = SCK_T`
(CS low, SCK high). An edge event fires **on the cycle the pattern appears**, with no polling
latency and no possibility of missing a short SCK pulse. The thread arms the next bit as soon as the
previous one has fired (`TEQ` drops to 0), so the bit value is chosen by firmware while the *edge*
stays hardware-timed.

```asm
; SPI target, mode 0, on the spare bidirectional pins.
.equ SCK_T,  0x0020          ; P[5]  = uio[5]
.equ MOSI_T, 0x0040          ; P[6]  = uio[6]
.equ CS_T,   0x1000          ; P[12] = ui[4] SPI_CS_IN
.equ MISO_T, 0x2000          ; P[13] = uo[5] SPARE_OUT

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
    waitp  0x2000            ; let SCK return low before re-arming (mask=0x20 -> P[5])
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
| bit period | 333.3 cycles (10 µs) | 83.3 cycles (2.5 µs) |
| half phase (firmware) | 166.625 cycles = `0x0A6A` | 41.625 cycles = `0x029A` |
| quarter phase (sketch) | 83.3 cycles = `0x0535` | 20.8 cycles = `0x014D` |
| RTL-measured SCL **full period, rising-to-rising** | 360.4 cyc = **92.5 kHz** (high phase 169.1) | 96.9 cyc = **344.2 kHz** (high phase 43.9) |
| golden-model figure (model-only) | 97.6 kHz | 364 kHz |

*Measurement definition:* the I²C rate is the **full SCL period, rising-to-rising** (high + low
phase), not 2 × the high phase. The low phase carries the polled SCL-release overhead, which is why
the measured rate is below the nominal setting — I²C specifies *maxima*, so both are legal.
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
.equ QUARTER, 0x0535         ; 83.3 cycles = 2.5 us (100 kHz; 0x014D for 400 kHz)

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

For 400 kHz change `QUARTER` to `0x014D` (20.8 cycles). Nothing else changes — that is the point
of a fixed-point delay: the same program is a different bus rate.

---

## 4. PS/2 (device → host)

| Quantity | Cycles @ 33.33 MHz |
|---|---|
| clock period (10–16.7 kHz) | 2000–3333 |
| 11-bit frame | 22 000–36 700 (0.66–1.1 ms) |
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

| Phase | Duration | Cycles @ 33.33 MHz | Value |
|---|---|---|---|
| reset pulse | 480 µs | 16 000 | 8 × `waitd 0x7D00` (60 µs = 2000 cycles each) |
| presence sample | ~30 µs after release | 1000 | `waitd 0x3E80` |
| write-1 low | ~6 µs | 200 | `0x0C80` |
| write-0 low | ~60 µs | 2000 | `0x7D00` |
| read slot: low then sample | 6 µs / 9 µs | 200 / 300 | `0x0C80` / `0x12C0` |
| recovery | 60 µs | 2000 | `0x7D00` |

**Strategy.** `waitd`'s maximum is `0xFFFF/16 = 4095.9` cycles (122.9 µs at 33.33 MHz), so the
sub-120 µs 1-Wire pulses each fit in one `waitd`; the 480 µs reset is eight 60 µs (`0x7D00`) steps.
Because the accumulator is fractional, 8 × 60 µs is exactly 16 000 cycles, not 16 000 ± rounding. `ONEWIRE` is `uio[4]`, so `DIR` is the open-drain control.

```asm
; 1-Wire reset/presence on P[4] = ONEWIRE = uio[4] (0x10), open drain.
.equ DQ,   0x0010
.equ W60,  0x7D00            ; 2000 cycles = 60 us
.equ W30,  0x3E80            ; 1000 cycles = 30 us
.equ W6,   0x0C80            ;  200 cycles =  6 us
.equ W9,   0x12C0            ;  300 cycles =  9 us

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

| Quantity | Cycles @ 33.33 MHz |
|---|---|
| bit period (800 kHz) | 41.67 |
| T0H (0.4 µs) | 13.3 |
| T1H (0.8 µs) | 26.7 |
| reset latch (>50 µs) | > 1667 |
| 24-bit pixel | 1000 (30 µs) |
| 60-pixel strip | 60 000 (1.8 ms) |

**Strategy.** This is the one protocol where the edge times are data-dependent, so neither a single
`PERIOD` nor a wave-player pattern applies directly: the two edges of each bit go out as `emitd`
one-shots relative to `TMR`, and a `waitd` pads the loop to exactly 75 cycles. WS2812 tolerates
±150 ns (±9 cycles) per bit, so the ±1-cycle loop quantisation is invisible. The `nop` keeps both
data paths the same length; that is the only cycle-counting in the program, and it is one
instruction, not a whole protocol.

```asm
; WS2812 / SK6812 stream at 800 kHz (41.67 cycles/bit @ 33.33 MHz).
; LED data on P[12] = WS2812_DATA = uo[4] (0x1000). T0H = 24, T1H = 48 cycles.
.equ LED,   0x1000
.equ SPIN,  0x01FC            ; waitd 31.75 cycles; loop body = 10 cycles -> 41.75
.equ RESET, 0x682B            ; 1666.7 cycles = 50 us low

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
    emitd  r3, 27             ; fall after 27 cycles = a '1'
    jmp    next
bit0:
    emitd  r5, 0
    emitd  r3, 13             ; fall after 13 cycles = a '0'
    nop                       ; pad so both branches take the same time
next:
    waitd  SPIN               ; 31.75 + 10 = 41.75 cycles per bit
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
| arbitrary 16-bit waveform | 64 patterns | 1 cycle | 33.3 M patterns/s |

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

## 8. USB 1.1 low-speed device — TX and RX decode/ACK (stretch)

USB LS is 1.5 Mbit/s = **22.2 cycles per bit** at 33.33 MHz, NRZI-coded, with bit stuffing after six
identical bits, an 8-bit SYNC pattern, a PID, USB CRC16 and an EOP. The shipped prototype
(`sw/examples/usb_ls_tx.asm`) is a **device-mode transmitter**: one thread owns the line and emits a
symbol per bit period with `setp`, while the host streams the 4-byte payload through the FIFO. The
protocol logic is real firmware; the *bit* timing is the scheduler's.

| Element | Implementation |
|---|---|
| bit period | 22.2 cycles ≈ `PERIOD 0x0100` rounded to the 1.5 Mbit/s grid; each frame period accumulates fractionally, so no drift |
| encoding | NRZI — a 0 bit toggles D+, a 1 leaves it; idle J level between packets |
| SYNC / PID | 8 raw bits `00000001` LSB-first, then DATA0 PID `0xC3` (nibble + complement) |
| payload / CRC | 4 data bytes, then CRC16 (poly `0x8005` reflected, init `0xFFFF`, final XOR `0xFFFF`) LSB-first |
| stuffing / EOP | a 0 inserted after six consecutive 1s (SYNC never stuffed, none before EOP); EOP is 2 bit cells of SE0 |
| pins | **single-ended prototype**: D+ on `P[0] = uio[0]`, push-pull, NRZI; the chip drives/monitors only the D+ half |
| external parts | a real board needs a USB connector/Pmod with **D− biased to the LS idle level and a 1.5 kΩ pull-up**; the FAQ allows external parts |

**Status: TX RTL-verified; RX decode + ACK golden-model verified; RX pin path RTL-verified; enumeration
not implemented.** (The RX firmware and its ceiling analysis are **v2 post-freeze additions** — the
hardened design and its claims are unchanged.)

`test/test_usb_ls.py` decodes the transmitted packet with an independent model on both memory builds
(2/2 PASS) and checks SYNC, PID, payload, CRC16, stuffing and EOP. `host/usb_ls_rx.py` is a validated
**differential host transmitter + device decoder**: DPLL clock recovery seeded from the KJKJKJKK SYNC
pattern, NRZI decode, bit unstuffing, PID+complement, CRC-5 and CRC-16, EOP framing; a synthetic SETUP
+ DATA0 `GET_DESCRIPTOR` decodes correctly across **±0.25 % host clock drift** (22.186–22.314
cycles/bit). `test/test_usb_ls_rx.py` proves the pin path on the RTL: the chip is **high-Z after reset**
(`PDIR = 0`), the testbench drives a real differential LS waveform on **D+ = `uio[0]`, D− = `uio[1]`**
through the open-drain pad model, samples the RTL `pins_in` path, and decodes with the reference
receiver — **0 pad-path mismatches**, SETUP token + 8-byte DATA0 decoding at nominal and ±0.25 % drift
(recovered 22.2528 / 22.3141 / 22.1856 cycles/bit = 0.013 % / +0.288 % / −0.289 %, DPLL residual
≤ 0.83 cycle), PID/complement, CRC-5, CRC-16, SYNCH/EOP and unstuffing all ok.

### 8.1 RX firmware: decode + ACK, and its measured ceiling

`sw/examples/usb_ls_rx.asm` (**357 instructions → SRAM-1024 build only**, DMEM 32/32 words) runs the
LS device receiver in one thread: it samples the differential line on the exact 22.25-cycle bit grid,
NRZI-decodes, validates SYNC, bit-unstuffs, checks PID + complement, CRC-5 (tokens) or CRC-16 (data)
and the EOP, publishes the decoded packet to the host, and ACKs a valid DATA0.

| Decode evidence (golden model) | Value |
|---|---|
| SETUP token | PID `0x2D` + complement, addr 0 / endpoint 0, **CRC-5 = `0x02`** |
| DATA0 `GET_DESCRIPTOR` | payload `8006000100004000` **byte-exact**, **CRC-16 = `0x94DD`** |
| EOP | at samples 32 (token) / 96 (data) |
| bit period | **22.249–22.255 cycles** = 1.4981–1.4982 Mbit/s |
| host tolerance | decodes at nominal **and ±0.25 % drift** |
| sampler alignment | fixed 9-cycle offset; valid window 6–12 cycles; **104 samples, none dropped or added** |
| ACK response | SYNC + PID `0xD2` + EOP, **18 cells @ 22.25 cycles**, host-decoded ok |

**Measured ACK latency (PC-breakpoint breakdown, DATA0 8-byte packet)** — the honest number that matters:

| Stage | Cycles | Bit times |
|---|---|---|
| sampler per bit (`WAITD 7.25` + 16 instructions) | 22.25 | 1.0 (72 % of the bit budget, **zero issue slack**) |
| host EOP → end of the fixed 104-sample window | 198 | 8.9 |
| NRZI + unstuff + byte assembly | 2 505 | 112.6 |
| PID + CRC-16 over 8 bytes | 564 | 25.3 |
| EOP double-SE0 scan | 1 155 | 51.9 |
| publish + ACK setup/emit | 23 | 1.0 |
| **EOP → first ACK cell** | **4 445** | **199.8** vs USB LS's **7.5** |

**This is an architectural ceiling, not a tuning problem.** The packet is decoded *after* capture, so
even perfect post-processing costs hundreds of cycles; wire-speed ACK needs decoding *during*
reception, at ≈34 instructions per bit (sampler 16 + inline NRZI/unstuff/accumulate ~10 + CRC-16
~8/bit) against **22.25 available — ≈1.5× over single-issue IPC** (four threads share the one issue
slot, so more threads do not help). Even an idealised 8-instruction sampler gives ≈26 > 22.25. A
revision-B option (not built) is a small hardware LS RX FSM (edge detect + DPLL + NRZI/stuffing/CRC-16
+ EOP + byte FIFO/interrupt to firmware).

**Constraints and boundaries, stated plainly:** RX is **single-thread and host-quiet** while a packet
is sampled (zero issue slack — one stolen cycle shifts the grid by a cycle, tolerated once by the
6–12-cycle window, but repeated stalls break it); the 104-sample window is **104 × 22.25 = 2 314
cycles** (do not mistake that window for a rate); **enumeration is not implemented** (no
GET_DESCRIPTOR response / SET_ADDRESS); a **config descriptor does not fit** in 32 DMEM words (a
16–18-byte device descriptor would, by reusing the dead sample region DMEM[0..8] at response time), and
a device that cannot supply one must **STALL** the request. So the honest USB row is: *TX timing-exact
and RTL-verified; RX decode + ACK functional-only at 199.8 bit times; enumeration not implemented.*

## 9. JTAG TAP controller *(v2 post-freeze addition; model-verified)*

JTAG (IEEE 1149.1) is explicitly named in the competition FAQ, so the bit-banged **TAP controller**
`sw/examples/jtag_tap.asm` (115 instructions — **fits the 128-word FF fallback**) runs one complete TAP
transaction per `RUN`. `host/jtag.py` models the far end (a DUT with a 4-bit IR, `IDCODE = 0x14B5A5A5`
in IR `0x2`, BYPASS in IR `0xF`) and independently decodes the pin trace with its own TAP state
machine.

| Element | Implementation |
|---|---|
| pins | TCK = `uio[0]`, TMS = `uio[1]`, TDI = `uio[2]` (push-pull outputs); **TDO = `uio[3]` stays an input**, sampled with `in` |
| sequence | Test-Logic-Reset (5×TCK, TMS=1) → Run-Test/Idle → Select-DR/IR → Capture-IR → Shift-IR (IDCODE, 4 bits) → Update-IR → Capture-DR → Shift-DR (`DR_BITS`, 8 by default; 32 reads the full IDCODE) → Update-DR |
| captured result | 4 IR-scan bits (expect Capture-IR status `0b0001`) then `DR_BITS` DR bits, pushed to the host TX FIFO one byte per bit; the host decoder sees every bit too |
| timing | 12.4 `waitd` accumulator, no instruction counting: **TCK period 31–39 cycles (mean 34.8 → 959 kHz)**, TCK HIGH exactly **6 cycles**, low phase padded by issue path (±4 cycles = ±127 ns) so the DUT always sees a clean setup/hold window; TDO changes on the falling edge |
| legality | there is no JTAG maximum TCK rate, so the padded low phase is legal; raise `HALF` (0x0100 → 0x0200 = 512 cycles) for a slow cable |

**Status: golden-model verified** with the independent host TAP decoder; an RTL test for this program
has not been added yet (v2). The example demonstrates the point of the architecture: a 4-wire
synchronous state machine with its own edge timing is *firmware*, not a hardblock.

## 10. CAN 2.0A transmitter *(v2 post-freeze addition; model-verified)*

`sw/examples/can_tx.asm` (79 instructions — **fits the FF fallback**) is a CAN **2.0A standard-data-frame
transmitter**: TXD on `uo[2]` (`P[10]`), RXD polled on `ui[3]` (`P[11]`) after the frame to sample the
ACK slot. A real board needs a CAN transceiver (external parts are FAQ-allowed); `host/can.py` models
the far end at the pin level, including driving the ACK slot dominant, and carries an independent
CRC-15 + stuffing decoder (`CanWireDecoder`, with the ISO 11898-1 CRC-15 check value
`crc15_can("123456789") = 0x059E` validated).

| Element | Implementation |
|---|---|
| frame | SOF → ID[10:0] → RTR=0 → IDE=0 → r0=0 → DLC[3:0] → data (8·DLC bits) → **CRC-15 (0x4599, MSB first)** → CRC delimiter → ACK slot (recessive, then poll RXD ~1000 cycles = 15 bit times for a dominant ACK) → ACK delimiter → EOF ×7 → IFS ×3 |
| stuffing | dynamic bit stuffing after five identical bits, applied to SOF..end of CRC sequence and never to CRC delimiter/ACK/EOF; the stuff bit is unconditional (the receiver can destuff deterministically) |
| timing | **500 kbit/s**: 66.6667 cycles/bit, `setp 0x042B` (1067/16 = **66.6875 cycles** → **499.85 kbit/s, −0.03 %**, inside the CAN oscillator tolerance); every bit and stuff bit is one `emit`, so all edges sit on the grid and the 12.4 carry keeps 100+ bits drift-free |
| host interface | host writes the SOF..data bit stream into the TX mailbox (DMEM[0] = N, DMEM[1..] = bits packed 16/word); firmware does CRC-15, stuffing, framing, ACK sampling and bit timing, then halts (single shot) |
| status | **golden-model verified** with the independent CRC/stuffing decoder; RTL test not yet added (v2) |

## 11. DMX512 transmitter *(v2 post-freeze addition; model-verified)*

`sw/examples/dmx_tx.asm` (37 instructions — **fits the FF fallback**) transmits one full **DMX512
frame** on `uo[2]` (`P[10]`, UART-style 8N2; a real board adds an RS-485 transceiver, which the FAQ
allows): BREAK (line low ≈120 µs, spec ≥88 µs) → MAB (high ≈12 µs, spec ≥8 µs) → start-code slot
`0x00` → **512 data slots**, one byte per slot pulled from the host RX FIFO. `host/dmx.py` models the
far end and decodes the stream independently.

| Element | Implementation |
|---|---|
| framing | 11 bit cells per slot (start 0, 8 data LSB-first, two stop bits), BREAK/MAB from `waitd` |
| timing | **250.03 kbaud** (+0.015 %, inside the DMX ±1 % window): 133.3125 cycles/bit from `setp 0x0855`; every bit cell is one `emit`, and the 12.4 carry keeps an 11-bit slot (1 466.4 cycles) drift-free across 513 slots |
| frame time | break 4 000 + MAB 400 + 513 slots × 1 466.4 ≈ **756 k cycles = 22.7 ms** |
| first slot | started by `emit`'s behind-schedule clamp (target is in the past after BREAK/MAB), which is exactly the spec's "first slot starts after MAB" |
| host interface | one data byte per slot through the RX FIFO; a slot is ~1 466 cycles, so a 33 MHz host has >1.4 k cycles per byte to keep the 8-byte FIFO ahead |
| status | **golden-model verified** with the independent host decoder; RTL test pending (v2) |

## 12. Ethernet frame TX (stage 1, achieved) and why true 10BASE-T is out of reach

### 12.1 What the chip does emit: IEEE 802.3 Manchester frames at 2.083 Mbit/s

`sw/examples/eth_tx.asm` (97 words, fits even the 128-word FF fallback) is a full **Ethernet frame
transmitter**: 7 × `0xAA` preamble → `0xD5` SFD → a **64-byte minimum frame** (dst 6 + src 6 +
ethertype 2 + 46-byte payload + 4-byte FCS, LSB-first) → 1536-cycle inter-packet gap, all
**Manchester-coded** (0 → high,low; 1 → low,high), with the **CRC-32/802.3 FCS computed in firmware**
(reflected `0xEDB88320`, init/final-XOR `0xFFFFFFFF`).

| Quantity | Value |
|---|---|
| Manchester cell | **8.000 cycles exact** (`setp 0x0080`, one `emit` per cell) |
| bit / rate | 16.00 cycles → 4.167 Mbaud → **2.083 Mbit/s** |
| frame wire time | (56 + 8 + 512) bits × 16 = **9216 cycles = 276 µs** |
| inter-packet gap | 1536 cycles (96 bit times) |
| edge-grid residual | **0.00 cycle** (max), because every cell is a scheduled event |
| firmware budget | ~9 instructions per bit against 16 cycles → 56 % of the window |
| DMEM footprint | **32/32 words** (the whole 64-byte frame buffer; state in registers) |

**Status: RTL-verified.** `test/test_eth.py` drives the firmware on the RTL and decodes the actual
`uio[0]` pin trace with the independent receiver in `host/eth.py` (edge-clock recovery, Manchester
decode, preamble/SFD lock, FCS check), for **two host-preloaded frames**:

* default frame: preamble/SFD lock, **64 bytes exact**, **FCS `0xF57743B9`** (the preloaded FCS words are
  zero, so the match proves the firmware computed it), **cells 8.000 cycles**, bits **16.00 cycles →
  2.0833 Mbit/s**, **max edge residual 0.00 cycle**, frame window exactly **9216 cycles**;
* custom host-preloaded frame: also **64 bytes exact**, **FCS `0x696CF29E`** (matches the Python
  reference), same 8.000 / 16.00 / 2.0833 / 0.00.

**IPG nuance, stated precisely:** `eth_tx.asm` recomputes the CRC-32 *after* the gap, so the
**edge-observable frame-to-frame gap is IPG + untimed compute** — TXD is quiet for **at least 1536
cycles** after each frame (measured **8508 and 8625 cycles** between frame edges in the two captures).
1536 cycles is the asserted quiet floor, not the edge-to-edge period.

This is *stage 1* of the Ethernet stretch goal: Manchester line coding + framing + CRC-32 are protocol
work the chip does itself.

### 12.2 True 10BASE-T: measured arithmetic for why it is not defensible on this build

The FAQ says there are "no fixed speed requirements" and that "the more of the protocol work (line
coding, CRC, clock recovery) your chip does itself, the better". Stage 1 does that work — at
2.083 Mbit/s. Pushing the same code to true 10BASE-T fails on arithmetic, not on effort:

| Constraint | 10BASE-T needs | This build provides | Verdict |
|---|---|---|---|
| line rate | 20 Mbaud = **1.6667 cycles/cell** at 33.33 MHz | a data-bearing Manchester cell costs ≥ 4 instructions (extract bit, compute level, emit, advance) → **80 M instr/s** required vs **33.33 M instr/s** single-issue IPC | **2.4× over** |
| more threads? | — | work-conserving round-robin shares *one* instruction per cycle; 4 threads add concurrency, not IPC — and four pin-merge writers add risk | does not help |
| degenerate toggler | — | a 2-instruction toggler caps at **8.3 Mbaud** (half of 10BASE-T, and no data) | still short |
| measured ceiling, same code | — | model run with only the cell divider patched: decodes at **5.5 cycles/cell = 3.03 Mbit/s**, fails at 5.0 cycles/cell (3.33 Mbit/s) | ~3 Mbit/s is the real ceiling |
| TX jitter | a few ns, ±100 ppm | edges are cycle-quantised → **±15–23 ns on a 50 ns cell** | unattainable |
| pattern buffer | 1024 Manchester half-cells per frame | wave player holds ≤ 64 patterns in a **32-word DMEM** | does not fit |

So: **we did the protocol work (framing, CRC-32, Manchester line coding) in firmware at 2.083 Mbit/s,
and we can show exactly why 10BASE-T itself is not physically defensible at 33.33 MHz with this
microarchitecture.** A true 10BASE-T port is a revision-B item: it needs a faster issue path (or a
dedicated serializer) plus a larger pattern buffer, not a bigger firmware effort.

## 13. Example program status

The firmware workstream now has **16 programs in `sw/examples/`** covering **11 protocol families**:
UART, SPI, I²C, PS/2, 1-Wire, WS2812, USB-LS, Ethernet, JTAG, CAN and DMX512 (MIDI is in progress and
not counted). Eight of them are exercised by
RTL tests (`uart_tx`, `uart_rx`, `spi_master`, `spi_slave`, `i2c_master`, `i2c_target`, `usb_ls_tx`,
`eth_tx`); the rest — including the three v2 additions below — are **golden-model verified** with
independent decoders, and their status says so.

"Sketch here, assembles" means the listing in this document is accepted verbatim by
`sw/loom_asm.py` (and the copies in [`examples_src/`](examples_src/) are the exact files CI
assembles). "planned" means the complete program lives with the firmware workstream in
`sw/examples/`. Nothing in this document claims a program has been run on hardware — see
[`verification.md`](verification.md) for what is actually proven, and [`info.md`](info.md#example-programs)
for the one-table summary.

| Program | Threads | Pins | Rate | Core instruction | Status |
|---|---|---|---|---|---|
| `uart_tx` / `uart_rx` | 1 each | UART_TX / UART_RX | 9600–921600 | `setp`+`emit` / polling+`waitd` | sketch here, assembles |
| `spi_master` (mode 0) | 1 | SCK/MOSI/CS | **1.04–1.06 MHz measured** (1.63 MHz ceiling) | `setp`+`emit` | sketch here, assembles |
| `spi_master` modes 1–3 | 1 | same | as above | constant changes | planned |
| `spi_target` | 1 | re-mux `uio[5..7]` | as master | `setem`/`setec`/`emite` | sketch here, assembles |
| `i2c_master` | 1 | SCL/SDA | 100 / 400 kHz | `waitd` + `dset`/`dclr` | sketch here, assembles |
| `i2c_target`, `ps2_device` | 1 each | SCL/SDA, PS2_CLK/DAT | bus rate | `emite` pattern match | planned |
| `ps2_host`, `onewire`, `ws2812`, `wave_player` | 1 each | see §4–§7 | see §4–§7 | `waitp`/`in`, chained `waitd`, `emitd`, CSRs | sketch here, assembles |
| `usb_ls_tx` | 1 | D+ (single-ended) | 1.5 Mbit/s | `setp` + NRZI/stuffing/CRC16 firmware | **TX RTL-verified** (`test/test_usb_ls.py`, 2 tests) |
| `usb_ls_rx` **(v2)** | 1 | D+ / D− (differential) | 1.4981–1.4982 Mbit/s | 22.25-cycle grid sampler + NRZI/unstuff/CRC-5/16/EOP firmware | **model-verified decode + ACK** (357 words → SRAM-1024 only; DMEM 32/32); ACK is functional-only at **199.8 bit times** — see §8.1 ceiling |
| `jtag_tap` **(v2)** | 1 | TCK/TMS/TDI out, TDO in | TCK 31–39 cyc (mean 959 kHz) | chained `waitd` pulses + `in` on TDO | **model-verified** with independent host TAP decoder (115 instructions, fits FF build) |
| `can_tx` **(v2)** | 1 | TXD `uo[2]`, RXD `ui[3]` | **499.85 kbit/s** (−0.03 %) | `setp 0x042B` (66.6875 cyc/bit) + `emit` per bit; CRC-15/stuffing in firmware | **model-verified** with independent CRC/stuffing decoder (79 instructions, fits FF build) |
| `dmx_tx` **(v2)** | 1 | DMX `uo[2]` (8N2, needs RS-485) | **250.03 kbaud** (+0.015 %) | `setp 0x0855` (133.3125 cyc/bit) + `emit`/bit; BREAK/MAB via `waitd` | **model-verified** with independent host decoder (37 instructions, fits FF build); full 512-slot frame in 22.7 ms |
| `eth_tx` | 1 | TXD (Manchester) | **2.0833 Mbit/s** (4.167 Mbaud) | `setp` + Manchester cells, CRC-32 in firmware | **RTL-verified** (`test/test_eth.py`, two frames, independent decoder, FCS ok; `sw/examples/eth_tx.asm`, 97 words) |
| `uart_loopback`, `ws2812`, `ps2_target`, `onewire`, `spi_slave` | 1 each | see §1–§6 | see §1–§6 | mixed | model-verified (sketch here for the §-documented ones; full programs in `sw/examples/`) |
| `uart_echo`, `capture_replay` | 1–2 | various | various | mixed | planned |
| true 10BASE-T (20 Mbaud) | — | — | — | — | **out of reach at 33.33 MHz** — see §12.2 arithmetic; revision-B item |

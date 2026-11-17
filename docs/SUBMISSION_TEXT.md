# Submission text (draft — paste into the competition form when the repo is public)

**Project:** LOOM — a programmable protocol loom
**Repo:** https://github.com/rajesh963/loom-protocol-emulator (made public before submission)
**Tiles:** 8x4 (IHP CMOS5L) · **Clock:** 33.33 MHz shipped design point (`CLOCK_PERIOD` 30 ns)
**Memory:** one IHP `RM_IHPSG13_1P_1024x32` SRAM (1024 x 32 instruction memory) + 32 x 16 data memory
**Flow:** Tiny Tapeout `tt-gds-action@ttihp27a` (LibreLane), standard precheck + gate-level test — no custom GDS flow; one IHP SRAM macro (explicitly FAQ-allowed)

## One-liner

LOOM is a protocol emulator with no protocol hardblocks: four programmable threads whose pin
transitions are driven by a hardware event scheduler, so every edge lands on the exact cycle it was
scheduled for regardless of what the firmware is doing. Protocols are programs loaded over SPI after
the chip is fabricated.

## What is demonstrated

Twelve firmware programs covering ten protocol roles across seven families — all run on the same RTL
and ISA with no per-protocol hardware:

| Protocol | Roles | Measured at 33.33 MHz |
|---|---|---|
| UART | TX, RX, loopback (full duplex) | 115.20 kBd (115200 target, +0.001 %) |
| SPI | master modes 0-3, target | 1.04-1.06 MHz SCK (master) |
| I2C | controller 100/400 kHz, target | 92.5 / 344.2 kHz full-period (spec maxima) |
| PS/2 | target | 14.28 kHz |
| 1-Wire | master | standard speed, DS18B20 +25.0 C |
| WS2812 | driver | 800 kHz |
| USB 1.1 low-speed | device TX (stretch) | 1.4982 Mbit/s (-0.122 %) — **RTL-verified** (2 tests, independent decoder); RX reference decoder in `host/usb_ls_rx.py` (no on-chip RX firmware yet); single-ended D+ prototype, not silicon-tested |
| Ethernet (802.3 framing) | frame TX (stretch) | **2.083 Mbit/s**, Manchester + firmware CRC-32; 64-byte minimum frame **model-verified** (RTL test in progress) |

SPI and I2C are demonstrated in **both controller and target roles**; four threads can run
protocols concurrently, with timing that is deterministic by construction.

## Why the architecture is different

1. **Decoupled timing.** Instructions enqueue `(time, value)` events; a hardware scheduler applies
   them on the exact cycle. Branches, stalls and other threads cannot add edge jitter.
2. **Fractional periodic scheduling.** 12.4 fixed-point period with carry accumulation: baud rates
   are exact on average (UART 115200 -> 289.375 cycles/bit at 33.33 MHz, -80 ppm), and long streams
   do not drift.
3. **Edge-triggered events and 1-cycle timed sampling (`inat`)** give reactive/slave behaviour and a
   logic-analyser primitive, without the CPU having to poll at cycle granularity.
4. **Wave player** streams precomputed pin patterns from data memory at fractional symbol periods
   (up to 33.3 M patterns/s), for waveforms whose encoding is precomputable.
5. **Record & replay** (capture engine): RTL complete and unit-verified; silicon integration is
   revision B because it aliases the wave-player memory. It makes the chip a tool for capturing an
   unknown bus and replaying it as an emulator.

## Verification

- **62-test cocotb suite, 62 PASS / 0 FAIL / 0 SKIP** on both memory configurations (FF and SRAM-1024),
  reproducible from a clean clone (~144 s); frozen at `a55c1b7`.
- **100-seed differential co-simulation** against an independent, spec-derived cycle-accurate model:
  cycle-exact on the full pin trace *and* full architectural state; plus 12/12 generated
  pull/reactive seeds and 6/6 hand-written programs.
- Nine protocol-level tests against independent UART/SPI/I2C VIPs plus **two USB low-speed TX tests**
  (NRZI + sync + DATA0 PID + CRC16 + bit stuffing + EOP, independently decoded), all RTL-verified.
- **3 formal proofs** (FIFO/queue bounds, reset completeness and event properties, host command
  ordering) via yosys SMT + z3.
- Gate-level simulation of the hardened netlist in the Tiny Tapeout flow.
- Sixteen RTL deviations found during bring-up and all closed — recorded in `docs/FLOW_STATUS.md`
  and the write-up, including the ones found by the differential model.

## Write-up

`docs/writeup.md` — the narrative: the problem, the idea, the timing arithmetic, the machine, the
protocol suite, verification, the honest hardening log (area 98.4 % → 44.7 % movable-relative / 51.5 %
raw via an SRAM instruction memory and a library-hygiene fix; 60 MHz failed timing with a 20.8 ns
critical path, so the design ships at 33.33 MHz; detailed routing was then fixed by a routing-config
change after a template knob cost the 6-hour CI cap), limitations, and how AI tools were used.

## Honest limitations

- Shipped clock is 33.33 MHz (60 MHz target did not close timing at 130 nm).
- **GDS is not yet claimed.** Attempt 6 reached detailed routing in 15 min and was cancelled at the
  6-hour GitHub Actions cap with 6 violations left — root-caused to `GRT_ALLOW_CONGESTION: true`
  inherited from the TT template (global routing deferred 47 track overflows to DRT). Attempt 7 fixes
  the routing configuration; if it still cannot fit the cap, the FAQ's prescribed fallback is a local
  LibreLane run plus the custom GDS action.
- SPI is bit-banged in firmware: ~1.06 MHz master. **True 10BASE-T is not achievable** (20 Mbaud =
  1.6667 cycles/cell needs ≥4 instructions/cell = 80 M instr/s vs 33.33 M instr/s IPC, 2.4× over;
  measured same-code ceiling 3.03 Mbit/s; edges are cycle-quantised to ±15–23 ns on a 50 ns cell).
  We instead do the *protocol work* — 802.3 framing, Manchester, CRC-32 — at 2.083 Mbit/s, and show
  the arithmetic for why 10BASE-T is revision-B (faster issue path + larger pattern buffer).
- USB is device-TX-only on a single-ended D+ pin, proven on the golden model; real silicon needs
  external D- termination/pull-up parts and an on-chip RX/DPLL for full enumeration.
- Capture/record-replay is RTL-complete and unit-verified but not integrated in this revision.

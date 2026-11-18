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

Twenty firmware programs covering fifteen protocol families *(as of `04c22f9`)* — all run on the same RTL
and ISA with no per-protocol hardware:

| Protocol | Roles | Measured at 33.33 MHz |
|---|---|---|
| UART | TX, RX, loopback (full duplex) | 115.20 kBd (115200 target, +0.001 %) |
| SPI | master modes 0-3, target | 1.04-1.06 MHz SCK (master) |
| I2C | controller 100/400 kHz, target | 92.5 / 344.2 kHz full-period (spec maxima) |
| PS/2 | target | 14.28 kHz |
| 1-Wire | master | standard speed, DS18B20 +25.0 C |
| WS2812 | driver | 800 kHz |
| USB 1.1 low-speed | device TX (stretch) | 1.4982 Mbit/s (-0.122 %) — **RTL-verified** (2 tests, independent decoder); single-ended D+ TX prototype, not silicon-tested |
| USB 1.1 low-speed | device RX decode + ACK (v2, post-freeze) | 1.4981–1.4982 Mbit/s — **golden-model verified** (SETUP CRC-5 `0x02`, DATA0 `GET_DESCRIPTOR` byte-exact, CRC-16 `0x94DD`, ±0.25 % host drift); ACK is **functional-only at 199.8 bit times** (measured architectural ceiling vs 7.5 allowed); enumeration not implemented |
| JTAG / CAN / DMX512 / MIDI / NEC-IR | TAP controller / frame / lighting / MIDI / IR remote (v2, post-freeze) | **model-verified** with independent decoders: JTAG TCK mean 959 kHz, CAN **499.85 kbit/s (−0.03 %)** with CRC-15 + dynamic stuffing, DMX512 **250.03 kbaud (+0.015 %)** 512-slot frame in 22.7 ms, MIDI **31250.6 baud (+0.002 %)**, NEC-IR **38.00 kHz** scheduler-synthesised carrier, 67.5 ms/frame; RTL tests pending |
| SWD / HDMI-CEC | debug target / CEC initiator (v2, post-freeze) | **model-verified** with independent decoders: SWD target DP/AP subset (IDCODE `0x0BB11477`, FAULT, dropped writes; **SWCLK ceiling 694 kHz**, 210 words → SRAM-1024 only), CEC initiator **416.7 bit/s** (START 3.699/0.801 ms, 21 cells = 52.5 ms); RTL tests pending |
| Ethernet (802.3 framing) | frame TX (stretch) | **2.0833 Mbit/s**, Manchester + firmware CRC-32; 64-byte minimum frame **RTL-verified** (two frames, independent decoder, FCS ok) |

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

## Explore it without reading Verilog

The repo ships an **interactive RTL module explorer** (`viewer/`, self-contained, no build step):
top → core → per-thread internals (event queue, wait/issue, timed sample), host bridge, memory and wave
player, with details, badges and references into the architecture/ISA docs. It is SiliconPulse-compatible
(`diagram.v1`) and is deployed alongside the 3D GDS viewer:

- 3D GDS viewer: <https://gds-viewer.tinytapeout.com/?pdk=ihp-sg13cmos5l&model=https%3A%2F%2Frajesh963.github.io%2Floom-protocol-emulator%2Ftinytapeout.oas>
- RTL module explorer: <https://rajesh963.github.io/loom-protocol-emulator/modules/>
- landing page with both: <https://rajesh963.github.io/loom-protocol-emulator/>

## Verification

- **64-test cocotb suite, 62 PASS / 0 FAIL / 0 SKIP** on both memory configurations (FF and SRAM-1024),
  reproducible from a clean clone (≈271 s per configuration); frozen at `7d2fb2f`.
- **100-seed differential co-simulation** against an independent, spec-derived cycle-accurate model:
  cycle-exact on the full pin trace *and* full architectural state; plus 12/12 generated
  pull/reactive seeds and 6/6 hand-written programs.
- Nine protocol-level tests against independent UART/SPI/I2C VIPs plus **two USB low-speed TX tests**
  (NRZI + sync + DATA0 PID + CRC16 + bit stuffing + EOP, independently decoded), all RTL-verified.
- **4 formal proofs** (FIFO/queue bounds, reset completeness and event properties, host command
  ordering, and — added post-freeze — **scheduler exactness at bound k=16**) via yosys SMT + z3.
- **Gate level on the hardened netlist**: `TESTS=64 PASS=60 FAIL=0 SKIP=4` (four expected skips are
  RTL-backdoor-dependent tests), from the CI artifact's `results.xml` (no GL FST is produced).
- **All four Tiny Tapeout CI jobs green** on design SHA `7d2fb2f`: `gds`, `precheck` (10/10), `viewer`,
  `gl_test`. No silicon measurements exist.
- Gate-level simulation of the hardened netlist in the Tiny Tapeout flow (a real GL-only defect was
  caught this way: DMEM was zeroed only by an unsynthesised `initial`, so GL started undefined — fixed
  by having the testbench initialise all 64 DMEM words over the host port, matching the firmware
  contract).
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
- **GDS and precheck PASSED** (attempt 8, commit `d7bec7a`): Tiny Tapeout **precheck 10/10**, DRC and
  antenna clean, detailed routing converged to **0 violations** in a ~2.5 h flow. Measured for the
  hardened image: **658 414 µm²** instance area (**54.5 %** of the 1 208 170 µm² core, 518 231 µm²
  stdcells + 140 183 µm² SRAM macro, 35 971 instances), **timing closed at all three corners** with
  worst setup slack **+1.6 ns** at the slow corner (28.4 ns path vs 30 ns period), **8.60 mW** at
  33.33 MHz / VDD 1.2 V. **No silicon measurements exist** — these are flow results.
- SPI is bit-banged in firmware: ~1.06 MHz master. **True 10BASE-T is not achievable** (20 Mbaud =
  1.6667 cycles/cell needs ≥4 instructions/cell = 80 M instr/s vs 33.33 M instr/s IPC, 2.4× over;
  measured same-code ceiling 3.03 Mbit/s; edges are cycle-quantised to ±15–23 ns on a 50 ns cell).
  We instead do the *protocol work* — 802.3 framing, Manchester, CRC-32 — at 2.083 Mbit/s, and show
  the arithmetic for why 10BASE-T is revision-B (faster issue path + larger pattern buffer).
- USB is device-TX-only on a single-ended D+ pin, proven on the golden model; real silicon needs
  external D- termination/pull-up parts and an on-chip RX/DPLL for full enumeration.
- Capture/record-replay is RTL-complete and unit-verified but not integrated in this revision.

#!/usr/bin/env python3
"""LOOM record & replay — skeleton for the capture/replay feature (stretch).

The full intended feature is on-chip: `loom_capture.v` timestamps every pin
transition into DMEM, the host reads it back with `READ_MEM`, and a replay
program re-emits the timestamped waveform from the event scheduler.  The
capture engine is still being brought up (`CSR_CAPCTRL`/`CSR_CAPSTAT`, owned by
the wavecapt teammate), so this script demonstrates the **same data flow at the
model level**, which is what the capture engine will produce:

    record : drive/observe the wires for N cycles, keep (cycle, pins_in) changes
    replay : feed those changes back into a fresh chip and run the same firmware;
             the decoded bytes must be identical

Run:

    python host/demos/demo_record_replay.py

What it proves today
--------------------
* the waveform of an external UART session can be captured as a compact
  transition list (the on-chip capture buffer format), and
* replaying that transition list into a fresh instance of `uart_rx.asm`
  reproduces the exact same decoded byte stream — so "record once, replay on
  any LOOM" works at the protocol level already.

When `loom_capture.v` lands, replace `record_with_far_end()` with:
    1. write CAPCTRL (trigger on any pin change, post-trigger length)
    2. run the protocol thread for the window
    3. poll CAPSTAT for done, `READ_MEM` the DMEM capture buffer
    4. convert DMEM records to the `Capture` structure below (same fields)
The replay half of this script is already the final form.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw import loom_asm, loom_model                                  # noqa: E402
from sw.protocols import UartBus, WireWorld                          # noqa: E402
from host.loom_host import ModelLink                                  # noqa: E402

EXAMPLES = ROOT / "sw" / "examples"


class Capture:
    """A recorded pin-session: `(cycle, pins_in16)` transitions + metadata."""

    def __init__(self, clock_hz: int = 60_000_000, initial: int = 0xFFFF):
        self.clock_hz = clock_hz
        self.initial = initial
        self.transitions: list[tuple[int, int]] = []
        self.cycles = 0
        self.pin_mask = 0xFFFF

    def record(self, cycle: int, pins_in16: int) -> None:
        if not self.transitions or self.transitions[-1][1] != pins_in16:
            self.transitions.append((cycle, pins_in16))

    def level_at(self, cycle: int) -> int:
        """pins_in level for a cycle (piecewise-constant between transitions)."""
        level = self.initial
        for t, v in self.transitions:
            if t > cycle:
                break
            level = v
        return level

    def __len__(self) -> int:
        return len(self.transitions)

    def summary(self) -> str:
        return (f"{len(self.transitions)} transitions over {self.cycles} cycles "
                f"({self.cycles / self.clock_hz * 1e6:.1f} us)")


def record_with_far_end(payload: bytes = b"LOOM") -> tuple[Capture, bytes]:
    """Run uart_rx.asm against the far-end UART while recording pins_in."""
    prog = loom_asm.assemble_file(EXAMPLES / "uart_rx.asm")
    loom = loom_model.Loom()
    loom.load(prog.words, 0)
    loom.set_pc(1, 0)
    link = ModelLink(loom)
    world = WireWorld(link)
    uart = UartBus(world, tx_tid=0, rx_tid=1)
    loom.run_threads(0b0010)
    world.run(300)
    uart.wire_send(payload)
    budget = (len(payload) * 12 + 8) * 521 + 4000
    cap = Capture(clock_hz=world.clock_hz, initial=world.ext16)
    # record the *external* pin stimulus applied to the chip, cycle by cycle
    for _ in range(budget):
        world.step()
        cap.record(world.cycle - 1, world.ext16)
    cap.cycles = world.cycle
    # drain any decoded bytes so the replay comparison is exact
    decoded = uart.app_recv()
    return cap, decoded


def replay(capture: Capture) -> bytes:
    """Feed a captured pin-session into a fresh Loom running uart_rx.asm."""
    prog = loom_asm.assemble_file(EXAMPLES / "uart_rx.asm")
    loom = loom_model.Loom()
    loom.load(prog.words, 0)
    loom.set_pc(1, 0)
    link = ModelLink(loom)
    uart = UartBus(None, link=link, tx_tid=0, rx_tid=1)   # no world: link only
    loom.run_threads(0b0010)
    for cycle in range(capture.cycles):
        loom.set_pins_in(capture.level_at(cycle))
        loom.step()
    out = bytearray()
    while True:
        b = link.rx_pop(1)
        if b is None:
            break
        out.append(b)
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LOOM record & replay skeleton")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args(argv)
    payload = b"LOOM"
    print("LOOM record/replay (model-level skeleton)")
    print(f"  source firmware: sw/examples/uart_rx.asm")
    print(f"  session        : one UART frame burst {payload!r} at 115200 baud")
    capture, decoded_live = record_with_far_end(payload)
    print(f"  recorded       : {capture.summary()}")
    print(f"  live decode    : {decoded_live!r}")
    if not args.quick:
        print("  capture excerpt (cycle, pins_in):")
        for cycle, pins in capture.transitions[:8]:
            print(f"    {cycle:7d}  0x{pins:04x}   "
                  f"RX P11={((pins >> 11) & 1)}")
        if len(capture.transitions) > 8:
            print(f"    ... {len(capture.transitions) - 8} more transitions")
    replayed = replay(capture)
    print(f"  replayed decode: {replayed!r}")
    ok = replayed == decoded_live == payload
    print(f"  capture format : list[(cycle, pins_in16)] in DMEM words "
          f"(2 words / transition with the capture engine)")
    print("DEMO_RECORD_REPLAY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

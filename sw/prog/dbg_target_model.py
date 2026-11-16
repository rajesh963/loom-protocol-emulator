#!/usr/bin/env python3
"""Replay verify's I2C-target VIP master schedule against the golden model.

Feeds the identical `vip/i2c.py::I2cMasterModel.schedule(0x50, writes=[0x5A],
reg=0x02)` stimulus into `sw/loom_model.py` so we can tell firmware vs RTL.

Run:  python sw/prog/dbg_target_model.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

from sw import loom_asm, loom_model                                   # noqa: E402
from vip import i2c as v_i2c                                          # noqa: E402

EX = ROOT / "sw" / "examples"
SCL_BIT, SDA_BIT = 6, 7


def wire(loom, ext16: int, bit: int) -> int:
    if (loom.dir >> bit) & 1:
        return (loom.pins_reg >> bit) & 1
    return (ext16 >> bit) & 1


def main() -> int:
    prog = loom_asm.assemble_file(EX / "i2c_target.asm")
    print(f"i2c_target.asm: {len(prog.words)} words  "
          f"main=0x{prog.symbols['main']:x} write_next=0x{prog.symbols['write_next']:x} "
          f"sub_next_byte=0x{prog.symbols['sub_next_byte']:x} rxb_loop=0x{prog.symbols['rxb_loop']:x}")
    loom = loom_model.Loom()
    loom.load(prog.words, 0)
    loom.set_pc(0, 0)
    loom.run_threads(0b0001)
    loom.run(400)

    m = v_i2c.I2cMasterModel(half_period=300, setup=40)
    t_start = loom.cycle + 400
    events, ack_samples = m.schedule(0x50, writes=[0x5A], reg=0x02, t_start=t_start)
    actions = [("drive", c, (scl, sda)) for c, scl, sda in events]
    actions += [("ack", c, None) for c in ack_samples]
    actions.sort(key=lambda a: a[1])

    ext = 0xFFFF
    by_cycle: dict[int, list] = {}
    for a in actions:
        by_cycle.setdefault(a[1], []).append(a)
    acks = []
    trace = []
    end = events[-1][0] + 600
    while loom.cycle < end:
        cyc = loom.cycle
        for kind, _c, lv in by_cycle.get(cyc, []):
            if kind == "drive":
                ext = 0xFFFF
                if lv[0] == 0:
                    ext &= ~(1 << SCL_BIT)
                if lv[1] == 0:
                    ext &= ~(1 << SDA_BIT)
            else:
                acks.append((wire(loom, ext, SDA_BIT), cyc))
        loom.set_pins_in(ext)
        pc = loom.threads[0].pc
        trace.append((cyc, pc,
                      wire(loom, ext, SCL_BIT), wire(loom, ext, SDA_BIT),
                      (loom.dir >> SDA_BIT) & 1, loom.threads[0].r[1],
                      loom.threads[0].r[5], loom.threads[0].r[6]))
        loom.step()

    print(f"ack samples (sda, cyc)={acks}")
    print(f"DMEM[0..7]={[hex(x) for x in loom.dmem[:8]]}  final pc=0x{loom.threads[0].pc:x}")
    print(f"trace window 4000..18500 (pc changes and SCL rising edges):")
    prev_scl = 1
    prev_pc = None
    prev_dut = 0
    for cyc, pc, scl, sda, dut_sda, r1, r5, r6 in trace:
        if 4000 <= cyc <= 18500:
            if scl == 1 and prev_scl == 0:
                print(f"   rise @{cyc} pc=0x{pc:02x} sda={sda} r1={r1:#04x} r5={r5:#04x} r6={r6}")
            if pc != prev_pc and pc < 0x40:
                print(f"   pc->0x{pc:02x} @{cyc} r1={r1:#04x} r5={r5:#04x} r6={r6}")
            if dut_sda != prev_dut:
                print(f"   dut SDA-low {dut_sda} @{cyc} pc=0x{pc:02x}")
                prev_dut = dut_sda
        prev_scl = scl
        prev_pc = pc
    ok = loom.dmem[2] == 0x5A and all(a[0] == 0 for a in acks)
    print("MODEL_RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

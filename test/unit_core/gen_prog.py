#!/usr/bin/env python3
"""Generate hand-encoded test programs for test/unit_core/tb_core.v.

Encodes LOOM instructions (see spec/ARCHITECTURE.md §4) without using the
assembler, so the core RTL can be smoke-tested independently of the toolchain.
"""
import sys

OPC = dict(nop=0x00, halt=0x01, movi=0x02, mov=0x03, add=0x04, addi=0x05,
           sub=0x06, subi=0x07, and_=0x08, andi=0x09, or_=0x0A, ori=0x0B,
           xor=0x0C, xori=0x0D, inv=0x0E, shl=0x0F, shr=0x10, rol=0x11,
           ror=0x12, cmp_=0x13, cmpi=0x14, csrw=0x15, csrr=0x16, out=0x18, set=0x19, clr=0x1A,
           tog=0x1B, in_=0x1C, dir=0x1D, dset=0x1E, dclr=0x1F,
           emit=0x20, emita=0x21, emitd=0x22, emite=0x23, setp=0x24,
           setm=0x25, setem=0x26, setec=0x27, inat=0x28, waitu=0x29,
           waitd=0x2A, waitp=0x2B, ld=0x30, st=0x31, ldi=0x32, sti=0x33,
           push=0x38, pull=0x39, pushi=0x3A, jmp=0x40, jz=0x41, jnz=0x42,
           jc=0x43, jnc=0x44, rpt=0x48, evset=0x49, evwait=0x4A, irq=0x4B)


def enc(mn, rd=0, rs=0, imm=0):
    return ((OPC[mn] & 0x7F) << 25) | ((rd & 7) << 22) | ((rs & 7) << 19) | (imm & 0xFFFF)


def main():
    mem = [0x00000000] * 1024          # default: NOP
    mem = [0x00000001] * 1024          # default: HALT (safe runaway)

    # ---- thread 0 (PC 0): square wave on pins [7:0], period 16 cycles ----
    t0 = [
        enc('movi', rd=0, imm=0x00FF),
        enc('movi', rd=1, imm=0x0000),
        enc('setp', imm=0x0100),        # period = 16.0 cycles (12.4 fixed)
        enc('setm', imm=0x00FF),
        enc('emit', rd=0),              # addr 4: target 16, 48, 80...
        enc('emit', rd=1),              # addr 5: target 32, 64...
        enc('jmp',  imm=0xFFFE),        # to addr 4  (pc(6) + (-2))
    ]
    mem[0:len(t0)] = t0

    # ---- thread 1 (PC 16): square wave on pins [11:8], period 8 cycles ----
    t1 = [
        enc('movi', rd=0, imm=0x0400),
        enc('movi', rd=1, imm=0x0000),
        enc('setp', imm=0x0080),        # period = 8.0 cycles
        enc('setm', imm=0x0C00),
        enc('emit', rd=0),
        enc('emit', rd=1),
        enc('jmp',  imm=0xFFFE),
    ]
    mem[16:16 + len(t1)] = t1

    # ---- computed-jump regression program (csrw TPC) --------------------
    jmem = [0x02000000] * 1024
    jprog = [
        enc('movi', rd=0, imm=6),            # 0: r0 = 6
        enc('csrw', rs=0,  imm=0x0008),      # 1: TPC = r0 -> jump to 6
        enc('movi', rd=1, imm=0x00FF),       # 2: must be skipped
        enc('out',  imm=0x000A),             # 3: must be skipped
        enc('halt'),                         # 4
        enc('halt'),                         # 5
        enc('movi', rd=2, imm=0x0400),       # 6: executed
        enc('out',  imm=0x0400),             # 7: pins out = 0x0400
        enc('halt'),                         # 8
    ]
    jmem[:len(jprog)] = jprog
    with open('jump.hex', 'w') as f:
        for w in jmem:
            f.write('%08x\n' % w)

    with open('prog.hex', 'w') as f:
        for w in mem:
            f.write('%08x\n' % w)
    print('wrote prog.hex (%d words)' % len(mem))


if __name__ == '__main__':
    main()

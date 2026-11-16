#!/usr/bin/env python3
"""Assemble (by hand-encoding) the integration test program: a 16-cycle square
wave on protocol pin P[10] (= uo_out[2]) produced by the hardware event
scheduler while a single thread runs a 3-instruction loop."""
OPC = dict(movi=0x02, setp=0x24, setm=0x25, emit=0x20, jmp=0x40)
def enc(mn, rd=0, imm=0):
    return ((OPC[mn] & 0x7F) << 25) | ((rd & 7) << 22) | (imm & 0xFFFF)
prog = [
    enc('movi', 0, 0x0400),   # pattern A: P[10] high
    enc('movi', 1, 0x0000),   # pattern B: low
    enc('setp', 0, 0x0100),   # event period = 16.0 cycles
    enc('setm', 0, 0x0C00),   # drive only P[11:10]
    enc('emit', 0),           # addr 4
    enc('emit', 1),
    enc('jmp',  0, 0xFFFD),   # -> addr 4
]
mem = [0x02000000] * 1024
mem[:len(prog)] = prog
open('itest.hex', 'w').write('\n'.join('%08x' % w for w in mem) + '\n')
print('wrote itest.hex with %d-word program' % len(prog))

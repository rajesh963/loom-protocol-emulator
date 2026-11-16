import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from sw import loom_asm
from sw.loom_model import Loom
prog = loom_asm.assemble("""
        movi r0, 30
    loop:
        subi r0, 1
        nop
        jnz loop
        halt
""")
print([hex(w) for w in prog.words], prog.symbols)
m = Loom(); m.load(prog.words); m.run_threads(1)
for i in range(16):
    st = m.state_snapshot()["threads"][0]
    print(f"cyc={i:2d} pc={st['pc']:2d} r0={st['r'][0]:3d} z={st['z']} stall={st['stall']} "
          f"valid={int(st['insn_valid'])} buf=0x{st['insn_buf']:08x} fetch_ret={m.fetch_returning}")
    m.step()

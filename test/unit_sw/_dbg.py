import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from sw import loom_asm
from sw.loom_model import Loom
prog = loom_asm.assemble("\n".join(["nop"]*6 + ["halt"]))
m = Loom(); m.load(prog.words); m.run_threads(1)
for i in range(14):
    st = m.state_snapshot()["threads"][0]
    print(f"cyc={i:2d} pc={st['pc']:2d} stall={st['stall']} valid={int(st['insn_valid'])} "
          f"buf=0x{st['insn_buf']:08x} fetch_ret={m.fetch_returning} issue_ptr={m.issue_ptr} fetch_ptr={m.fetch_ptr}")
    m.step()

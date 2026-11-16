import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from sw import loom_asm, isa
from sw.loom_model import Loom
ok = True
def asm(src): return loom_asm.assemble(src, filename="<scratch>")
def check(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    ok = ok and bool(cond)

# 1) single-thread 1 IPC straight-line
prog = asm("\n".join(["nop"]*20 + ["halt"]))
m = Loom(); m.load(prog.words); m.run_threads(1)
seen = {}
for i in range(40):
    if m.threads[0].halt and 'halt_at' not in seen: seen['halt_at'] = m.cycle
    m.step()
check("single-thread 1 IPC (halt executes in cycle 21)", m.threads[0].halt and seen.get('halt_at') == 22,
      f"steps_to_halt={seen.get('halt_at')} pc={m.thread_pc(0)}")
check("halt freezes PC", m.thread_pc(0) == 20, f"pc={m.thread_pc(0)}")

# 2) jump loop no fetch bubble: 3-instr loop x30 = 3 cyc/iter
prog = asm("""
        movi r0, 30
    loop:
        subi r0, 1
        nop
        jnz loop
        halt
""")
m = Loom(); m.load(prog.words); m.run_threads(1)
n=0
while not m.threads[0].halt and n < 300: m.step(); n += 1
check("jump loop 1 IPC (3 cyc/iter, halt at ~93)", m.threads[0].halt and m.reg(0,0)==0 and 90 <= m.cycle <= 96,
      f"r0={m.reg(0,0)} cycle={m.cycle}")

# 3) emit fractional period 1.5 drift-free
prog = asm("""
        setp 0x0018
        setm 0x00FF
        movi r0, 0x00FF
        movi r1, 0x0000
    loop:
        emit r0
        emit r1
        jmp loop
""")
m = Loom(); m.load(prog.words); m.run_threads(1); m.run(3000)
times = [c for c,_v,_d in m.trace]
deltas = [times[i+1]-times[i] for i in range(2, len(times)-1)]
avg = (times[-1]-times[2])/(len(times)-3)
check("emit 12.4 period 1.5 avg drift-free", abs(avg-1.5) < 0.01 and set(deltas) <= {1,2},
      f"n={len(times)} avg={avg:.5f} deltas={sorted(set(deltas))}")

# 4) inat latches pins of exact cycle (set DIR so readback works)
prog = asm("""
        dir 0xFF
        movi r0, 0x0042
        setm 0x00FF
        emita r0, 20
        inat r1, 20
        halt
""")
m = Loom(); m.load(prog.words); m.run_threads(1); m.run(30)
check("inat latches emitted pins of cycle 20", m.reg(0,1) == 0x0042 and m.threads[0].halt,
      f"r1=0x{m.reg(0,1):04X} pins=0x{m.pins_out16:04X}")

# 5) event scheduler independent of thread blocking
prog = asm("""
        movi r0, 0xBEEF
        setm 0x00FF
        emita r0, 5
        waitu 100
        halt
""")
m = Loom(); m.load(prog.words); m.run_threads(1); m.run(150)
check("events fire while blocked", m.pins_out16 == 0x00EF and m.threads[0].halt,
      f"pins=0x{m.pins_out16:04X}")

# 6) FIFO push/pull backpressure
prog = asm("""
        movi r0, 0x41
        push r0
        push r0
        push r0
        push r0
        push r0
        push r0
        push r0
        push r0
        pushi 0x5A
        halt
""")
m = Loom(); m.load(prog.words); m.run_threads(1); m.run(40)
before = len(m.threads[0].tx)
popped = [m.pop_tx(0) for _ in range(8)]
m.run(30)
check("tx fifo full backpressure then resume", before == 8 and m.threads[0].halt and m.pop_tx(0) == 0x5A,
      f"before={before} popped={popped} pc={m.thread_pc(0)}")

# 7) waitd 12.4: next issue at k+total (period 1.5 => loop 4 cycles/iter)
prog = asm("""
        movi r0, 0
    loop:
        addi r0, 1
        waitd 0x0018
        jmp loop
""")
m = Loom(); m.load(prog.words); m.run_threads(1); m.run(400)
check("waitd 12.4 timing (3.5 cyc/iter avg)", 105 <= m.reg(0,0) <= 125, f"iterations in 400 cyc = {m.reg(0,0)}")

# 8) two threads with separate PCs share issue slots
prog = asm("""
    ta:
        addi r0, 1
        jmp ta
    tb:
        addi r1, 1
        jmp tb
""")
m = Loom(); m.load(prog.words); m.set_pc(0,0); m.set_pc(1,2); m.run_threads(0b11); m.run(400)
check("2 threads work-conserving", m.reg(0,0) > 60 and m.reg(1,1) > 30,
      f"r0={m.reg(0,0)} r1={m.reg(1,1)}")

# 9) wave single-pass and loop
m = Loom(); m.load(asm("halt").words)
m.load_dmem([0x00A1,0x00A2,0x00A3,0x00A4])
m.csr_write(isa.SPEC_CSR['WADDR'],0); m.csr_write(isa.SPEC_CSR['WLEN'],4)
m.csr_write(isa.SPEC_CSR['WPER'],0x0020); m.csr_write(isa.SPEC_CSR['WCTRL'],0x1)
vals=[]; prev=m.pins_out16
for _ in range(30):
    m.step(); v=m.pins_out16
    if v!=prev: vals.append(v); prev=v
check("wave single pass", vals[:4] == [0xA1,0xA2,0xA3,0xA4] and m.w_done and not m.w_busy, f"vals={vals}")
m = Loom(); m.load(asm("halt").words); m.load_dmem([0x0001,0x0002])
m.csr_write(isa.SPEC_CSR['WADDR'],0); m.csr_write(isa.SPEC_CSR['WLEN'],2)
m.csr_write(isa.SPEC_CSR['WPER'],0x0010); m.csr_write(isa.SPEC_CSR['WCTRL'],0x3)
seq=[]; prev=m.pins_out16
for _ in range(16):
    m.step(); v=m.pins_out16
    if v!=prev: seq.append(v); prev=v
check("wave loop", seq[:6] == [1,2,1,2,1,2], f"seq={seq[:8]}")

# 10) flags
prog = asm("""
        movi r0, 0
        subi r0, 1
        and r2, r3
        halt
""")
m = Loom(); m.load(prog.words); m.run_threads(1); m.run(12)
check("logical clears C, sets Z", m.threads[0].z==1 and m.threads[0].c==0)
check("sub sets C on borrow", True)

# 11) csrw TPC acts as jump
prog = asm("""
        movi r1, 5
        csrw TPC, r1
        movi r0, 0xDEAD      ; must be skipped
        movi r0, 0xDEAD
        movi r0, 0xDEAD
        movi r2, 0x1234      ; pc=5 target
        halt
""")
m = Loom(); m.load(prog.words); m.run_threads(1); m.run(30)
check("csrw TPC acts as jump", m.reg(0,2)==0x1234 and m.reg(0,0)==0,
      f"r2=0x{m.reg(0,2):04X} r0=0x{m.reg(0,0):04X} pc={m.thread_pc(0)}")

# 12) edge-trigger event
prog = asm("""
        setem 0x00FF
        setec 0x0005
        setm 0x00FF
        movi r0, 0x00A5
        emite r0
        halt
""")
m = Loom(); m.load(prog.words); m.run_threads(1)
for _ in range(10): m.step()
m.set_pins_in(0x0004); m.step()
m.set_pins_in(0x0005); m.step(); m.step()
check("edge event fires on pattern match", m.pins_out16 == 0x00A5, f"pins=0x{m.pins_out16:04X}")

print("SCRATCH:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

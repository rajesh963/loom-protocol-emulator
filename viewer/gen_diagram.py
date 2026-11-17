#!/usr/bin/env python3
"""Generate the LOOM module-hierarchy diagram.

Outputs (both committed):
  viewer/loom.diagram.json   -- SiliconPulse "diagram.v1" JSON (loadable into
                                siliconpulse-3d via "Load JSON" for the full app)
  viewer/loom.diagram.js     -- the same object as `window.LOOM_DIAGRAM = {...}`
                                so viewer/index.html works from file:// (fetch()
                                on file:// is blocked by browsers).

The hierarchy, instance names and parameters mirror the RTL:
  rtl/tt_um_rajesh963_loom.v  -> loom_core / loom_mem / loom_host / loom_wave
                                 (+ loom_capture, rev-B and gated off)
  docs/architecture.md        -> section references in `specRef`
Approximate cell counts come from tools/area_synth.sh (yosys, generic cells).
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

CYAN, VIOLET, BLUE, PINK, SLATE, AMBER, EMERALD = (
    "#22d3ee", "#a78bfa", "#3b82f6", "#f472b6", "#64748b", "#f59e0b", "#34d399")



# SiliconPulse diagram.v1 restricts `kind` to this enum (see siliconpulse-3d
# public/examples/diagram.schema.json): cpu, bus, crossbar, bridge, memory,
# peripheral, accelerator, io, security, clock, debug, group.
KIND = {
    # top level
    "u_host": "bridge", "u_core": "cpu", "u_mem": "memory", "u_wave": "accelerator",
    "pins": "io", "capture": "debug",
    # core internals
    "thread0": "cpu", "thread1": "cpu", "thread2": "cpu", "thread3": "cpu",
    "eq_row": "clock", "arbiter": "accelerator", "decode": "cpu", "alu": "accelerator",
    "csr": "peripheral", "fifos": "peripheral", "pinmerge": "io",
    "hostport": "bridge", "memport": "bridge",
    # thread internals
    "t_regs": "cpu", "t_pc": "cpu", "t_flags": "cpu", "t_eq": "clock",
    "t_wait": "cpu", "t_is": "cpu", "t_inat": "cpu", "t_rpt": "cpu",
    # memory
    "pmem": "memory", "macro": "memory", "dmem": "memory",
    "hostarb": "bridge", "haltfill": "peripheral", "ports": "bus",
    # host bridge
    "shifter": "bridge", "cs": "bridge", "cmddec": "peripheral",
    "fsm": "peripheral", "txrx": "bridge", "status": "peripheral",
    # wave player
    "frac": "clock", "addr": "accelerator", "read": "accelerator", "merge": "io",
}


def blk(id, label, x, y, w, h, color, *, sublabel=None, kind=None, details=None,
        badges=None, spec=None, internal=None):
    b = {"id": id, "label": label, "kind": kind or KIND.get(id, "peripheral"),
         "x": x, "y": y, "w": w, "h": h, "color": color}
    if sublabel: b["sublabel"] = sublabel
    if details:  b["details"] = details
    if badges:   b["badges"] = badges
    if spec:     b["specRef"] = spec
    if internal: b["internal"] = internal
    return b


def grp(id, label, x, y, w, h, color):
    return {"id": id, "label": label, "x": x, "y": y, "w": w, "h": h, "color": color}


def wire(id, frm, to, bus, width, clock="clk", note=None):
    w = {"id": id, "from": frm, "to": to, "busType": bus, "width": width, "clock": clock}
    if note: w["note"] = note
    return w


def thread_internal(n):
    """One thread: register file, PC/prefetch, flags, waits, shifter."""
    t = f"T{n}"
    return {
        "title": f"Thread {n} — program-visible state",
        "subtitle": f"one of four identical threads (thread{n})",
        "source": "rtl/loom_core.v",
        "width": 900, "height": 620,
        "groups": [
            grp("t_state", "Program state", 30, 60, 380, 300, VIOLET),
            grp("t_time", "Timing state", 30, 390, 380, 190, PINK),
            grp("t_flow", "Control", 440, 60, 430, 520, CYAN),
        ],
        "blocks": [
            blk("t_regs", "registers", 60, 100, 320, 120, VIOLET,
                sublabel="16 × 16-bit", badges=["1,024 FFs / 4 threads"],
                details="r0..r15, one bank per thread; two read ports + one write port.",
                spec="architecture.md §4.2"),
            blk("t_pc", "PC + prefetch", 60, 240, 150, 100, VIOLET,
                sublabel="10-bit PC", details="PC, instruction buffer, i_rdy; invalidated on TPC writes and jumps.",
                spec="architecture.md §3.1"),
            blk("t_flags", "flags / status", 230, 240, 150, 100, VIOLET,
                sublabel="Z C N V", details="ALU flags, halted/running, wait mode."),
            blk("t_eq", "event queue", 60, 420, 320, 120, PINK,
                sublabel="4 entries × 22 bit", badges=["12.4 fixed period", "drift-free"],
                details="Per-thread periodic/absolute/edge events; due-or-overdue fire rule.",
                spec="architecture.md §4.4"),
            blk("t_wait", "wait logic", 470, 100, 370, 100, CYAN,
                sublabel="waitu / waitd / waitp", details="Stall state machine; wake on target time, pin condition or absolute deadline.",
                spec="architecture.md §4.5"),
            blk("t_is", "issue slot", 470, 230, 370, 100, CYAN,
                sublabel="1 instr / cycle", details="Work-conserving round-robin: exactly one thread issues per cycle.",
                spec="architecture.md §4.1"),
            blk("t_inat", "timed sample", 470, 360, 370, 100, CYAN,
                sublabel="INAT", details="Samples the 16 input pins on an exact target cycle into rd; 1-cycle accurate.",
                spec="isa.md §6"),
            blk("t_rpt", "repeat / branch", 470, 490, 370, 80, CYAN,
                sublabel="RPT, JMP, JZ/JNZ", details="Loop counter and branch-target resolution."),
        ],
        "wires": [
            wire("w_regs_pc", "t_regs", "t_pc", "operands", 16),
            wire("w_pc_is", "t_pc", "t_is", "instruction", 32),
            wire("w_is_eq", "t_is", "t_eq", "emit / event", 22),
            wire("w_eq_wait", "t_eq", "t_wait", "wake", 1),
            wire("w_is_inat", "t_is", "t_inat", "sample", 16),
        ],
        "notes": ["Click any block for details; Open internal module drills further where defined.",
                  "All state is per-thread except the shared datapath in the parent view."],
    }


def core_internal():
    blocks = []
    # four threads laid out in a row
    for n in range(4):
        blocks.append(blk(f"thread{n}", f"thread{n}", 70 + n * 200, 100, 180, 140, VIOLET,
                          sublabel="program state", badges=[f"T{n}", "regs+PC+flags"],
                          details="One of four identical threads: register file, PC/prefetch, flags, wait state, event queue.",
                          spec="architecture.md §4.2", internal=thread_internal(n)))
    # event queues column
    blocks.append(blk("eq_row", "4 × event queue", 70, 340, 520, 90, PINK,
                      sublabel="4 entries each, 12.4 fixed", badges=["16 events", "PFRAC carry"],
                      details="Hardware scheduler: periodic (emitd), absolute (emita) and edge-triggered (emite) events; due-or-overdue fires on the next cycle.",
                      spec="architecture.md §4.4"))
    # control
    blocks.append(blk("arbiter", "issue arbiter", 690, 340, 420, 60, CYAN,
                      sublabel="round-robin, work-conserving", details="Picks one runnable thread per cycle.",
                      spec="architecture.md §4.1"))
    blocks.append(blk("decode", "decode", 690, 420, 200, 70, CYAN,
                      sublabel="32-bit ISA", details="7-bit opcode, rd/rs, mode, imm16; includes shift/rotate and CSR ops.",
                      spec="isa.md §3"))
    blocks.append(blk("alu", "ALU", 910, 420, 200, 70, CYAN,
                      sublabel="16-bit + flags", details="add/sub/mov/logic/shift with Z C N V.",
                      spec="isa.md §4"))
    # csr / fifos / merge
    blocks.append(blk("csr", "CSR bank", 1070, 395, 280, 90, AMBER,
                      sublabel="per-thread + global", badges=["14 CSRs × 4", "6 global"],
                      details="Per-thread control/status CSRs plus global TMR/clock/host CSRs; host and instruction views.",
                      spec="architecture.md §4.3"))
    blocks.append(blk("fifos", "TX / RX FIFOs", 1070, 500, 280, 90, AMBER,
                      sublabel="8 B per thread", details="Per-thread byte FIFOs between firmware and the host bridge.",
                      spec="architecture.md §4.6"))
    blocks.append(blk("pinmerge", "pin merge (S6)", 1070, 605, 280, 70, EMERALD,
                      sublabel="pout → op → POUT → events → wave",
                      details="Fixed precedence order decides the final 16-bit pin word every cycle.",
                      spec="architecture.md §5.2"))
    blocks.append(blk("hostport", "host port", 1070, 100, 280, 80, SLATE,
                      sublabel="combinational reads", details="CSR/FIFO/status read port used by the SPI bridge.",
                      spec="architecture.md §6"))
    blocks.append(blk("memport", "IMEM / DMEM ports", 1070, 195, 280, 70, SLATE,
                      sublabel="10-bit / 6-bit", details="Synchronous instruction fetch; combinational data memory.",
                      spec="architecture.md §5.1"))
    blocks.append(blk("capture", "capture engine", 1070, 280, 280, 70, SLATE,
                      sublabel="revision B", badges=["RTL complete", "gated off"],
                      details="Unit-verified record/replay engine; gated off because it aliases the wave-player DMEM (documented limitation).",
                      spec="architecture.md §7.3"))
    return {
        "title": "loom_core — 4-thread scheduler and datapath",
        "subtitle": "u_core (rtl/loom_core.v) — 28,788 generic cells",
        "source": "rtl/loom_core.v",
        "width": 1400, "height": 800,
        "groups": [
            grp("threads", "Threads (4 ×)", 40, 60, 820, 210, VIOLET),
            grp("timing", "Hardware event scheduler", 40, 300, 580, 140, PINK),
            grp("ctl", "Issue & datapath", 660, 300, 480, 220, CYAN),
            grp("iface", "CSRs, FIFOs, ports", 1040, 60, 340, 630, AMBER),
        ],
        "blocks": blocks,
        "wires": [
            wire("c_thr_arb", "thread0", "arbiter", "issue request", 1),
            wire("c_eq_thr", "eq_row", "thread0", "wake / emit", 22),
            wire("c_arb_dec", "arbiter", "decode", "instruction", 32),
            wire("c_dec_alu", "decode", "alu", "operands", 16),
            wire("c_alu_csr", "alu", "csr", "result / flags", 16),
            wire("c_fifo_merge", "fifos", "pinmerge", "pin words", 16),
            wire("c_host_fifo", "hostport", "fifos", "host data", 8),
            wire("c_mem_fetch", "memport", "decode", "imem_data", 32),
        ],
        "notes": ["Four threads share one issue slot: the scheduler is work-conserving round-robin, so aggregate IPC is 1.",
                  "Every pin edge from firmware goes through the event scheduler, never through instruction retirement.",
                  "thread0..thread3 each have their own internal diagram (click Open internal module)."],
    }


def mem_internal():
    return {
        "title": "loom_mem — program and data memory",
        "subtitle": "u_mem (rtl/loom_mem.v) — PMEM is an IHP SRAM macro",
        "source": "rtl/loom_mem.v",
        "width": 1100, "height": 640,
        "groups": [
            grp("pm", "Program memory (IHP SRAM)", 40, 60, 520, 320, BLUE),
            grp("dm", "Data memory (flip-flops)", 620, 60, 440, 200, EMERALD),
            grp("arb", "Host arbitration", 40, 420, 1020, 170, AMBER),
        ],
        "blocks": [
            blk("pmem", "loom_pmem_sram", 70, 100, 460, 120, BLUE,
                sublabel="fetch + host access", badges=["1-cycle read", "1024 × 32"],
                details="Wrapper around the macro: presents the fetch address in the fetch cycle, arbitrates host writes, holds read data.",
                spec="architecture.md §5.1"),
            blk("macro", "RM_IHPSG13_1P_1024x32", 70, 240, 460, 110, BLUE,
                sublabel="IHP CMOS5L SRAM macro", badges=["140,183 µm²", "single port"],
                details="The only macro in the design. Real silicon has no defined contents at power-up; the host must load the program.",
                spec="flow/README.md"),
            blk("dmem", "DMEM array", 650, 100, 380, 120, EMERALD,
                sublabel="32 × 16-bit", badges=["no reset", "wave pattern store"],
                details="Combinational read, three write sources (instruction, host, capture). Also feeds the wave player.",
                spec="architecture.md §5.1"),
            blk("hostarb", "host address decode", 70, 460, 300, 100, AMBER,
                sublabel="0x000–0x3BF / 0x3C0–0x3FF",
                details="Maps the 10-bit host address space: program memory vs data memory.",
                spec="architecture.md §6.1"),
            blk("haltfill", "unwritten-word HALT", 400, 460, 300, 100, AMBER,
                sublabel="RTL contract", details="FF build and simulation models return HALT for unwritten PMEM; the host loader pads the image on silicon.",
                spec="architecture.md §5.1"),
            blk("ports", "fetch / data ports", 730, 460, 300, 100, SLATE,
                sublabel="imem 10→32, dmem 6→16",
                details="Fetch port (synchronous) and data port (combinational), plus the host read port.",
                spec="architecture.md §5.1"),
        ],
        "wires": [
            wire("m_fetch", "ports", "pmem", "imem_addr / imem_data", 32),
            wire("m_pmem_macro", "pmem", "macro", "A_ADDR / A_DIN / A_DOUT", 32),
            wire("m_dmem", "ports", "dmem", "dmem_addr / rdata", 16),
            wire("m_host", "hostarb", "pmem", "host_we / wdata", 32),
            wire("m_wave", "dmem", "ports", "wave pattern", 16),
        ],
        "notes": ["One SRAM macro (FAQ-permitted) instead of ~180 k µm² of flip-flops: this is what made the 8×4 tape-out fit.",
                  "Host writes and fetch never collide: the host owns the write port while threads are halted."],
    }


def host_internal():
    return {
        "title": "loom_host — SPI host bridge",
        "subtitle": "u_host (rtl/loom_host.v) — 1,434 generic cells",
        "source": "rtl/loom_host.v",
        "width": 1000, "height": 600,
        "groups": [
            grp("spi", "SPI mode 0", 40, 60, 380, 240, CYAN),
            grp("cmd", "Command engine", 460, 60, 500, 240, AMBER),
            grp("io", "Data movement", 40, 350, 920, 200, EMERALD),
        ],
        "blocks": [
            blk("shifter", "8-bit shifter", 70, 100, 320, 100, CYAN,
                sublabel="MSB first, mode 0",
                details="Shift in on SCK rising, shift out on falling; MISO is driven combinationally from the response byte.",
                spec="architecture.md §6.1"),
            blk("cs", "CS / edge detect", 70, 230, 320, 70, CYAN,
                sublabel="byte boundary", details="Detects command boundaries and resets the bit counter.",
                spec="architecture.md §6.1"),
            blk("cmddec", "command decoder", 490, 100, 440, 100, AMBER,
                sublabel="13 commands", badges=["0x01–0x0D"],
                details="SET_ADDR, WRITE_MEM, READ_MEM, WRITE_CSR, READ_CSR, TX_PUSH, RX_POP, SET_PC, RUN, STOP, STATUS, ACK_IRQ, PING.",
                spec="architecture.md §6.2"),
            blk("fsm", "command FSM", 490, 230, 440, 70, AMBER,
                sublabel="arg / nresp phases",
                details="Sequences argument transfers, status bytes and multi-byte responses without stalling the host.",
                spec="architecture.md §6.2"),
            blk("txrx", "TX / RX bridge", 70, 390, 380, 120, EMERALD,
                sublabel="per-thread FIFOs",
                details="Moves bytes between the host and the per-thread byte FIFOs, with status byte per transfer.",
                spec="architecture.md §4.6"),
            blk("status", "STATUS capture", 490, 390, 440, 120, EMERALD,
                sublabel="4 thread bytes",
                details="Latches a coherent snapshot of all four threads' halt/enable/FIFO state for the 4-byte STATUS response.",
                spec="architecture.md §6.2"),
        ],
        "wires": [
            wire("h_spi_cmd", "shifter", "cmddec", "byte", 8),
            wire("h_cmd_fsm", "cmddec", "fsm", "opcode", 8),
            wire("h_fsm_txrx", "fsm", "txrx", "data", 8),
            wire("h_fsm_status", "fsm", "status", "snapshot", 32),
        ],
        "notes": ["Maximum SCK is clk/6 (5.56 MHz at the shipped 33.33 MHz clock).",
                  "The bridge is how programs, data and protocol bytes get in and out of the chip after fabrication."],
    }


def wave_internal():
    return {
        "title": "loom_wave — pattern player",
        "subtitle": "u_wave (rtl/loom_wave.v) — 1,270 generic cells",
        "source": "rtl/loom_wave.v",
        "width": 980, "height": 560,
        "groups": [
            grp("tim", "Timing", 40, 60, 420, 200, PINK),
            grp("pat", "Pattern fetch", 500, 60, 440, 200, BLUE),
            grp("out", "Pin merge", 40, 320, 900, 170, EMERALD),
        ],
        "blocks": [
            blk("frac", "12.4 divider", 70, 100, 360, 100, PINK,
                sublabel="fractional symbol period",
                details="Accumulates a 12.4 fixed-point period so symbol rates are exact on average (up to 33.3 M patterns/s).",
                spec="architecture.md §7.1"),
            blk("addr", "pattern address", 530, 100, 380, 100, BLUE,
                sublabel="DMEM word pointer",
                details="Walks the DMEM pattern buffer; loops or stops at the programmed length.",
                spec="architecture.md §7.1"),
            blk("read", "DMEM pattern read", 530, 230, 380, 60, BLUE,
                sublabel="16-bit word = 16 pins",
                details="Reads one pattern word per symbol; the only consumer of DMEM besides threads.",
                spec="architecture.md §7.1"),
            blk("merge", "wave → pin merge", 70, 360, 840, 100, EMERALD,
                sublabel="last stage of S6",
                details="Wave patterns override firmware pin values while enabled; disabled by default in the shipped firmware except the wave demo.",
                spec="architecture.md §5.2"),
        ],
        "wires": [
            wire("wv_frac_addr", "frac", "addr", "advance", 1),
            wire("wv_addr_read", "addr", "read", "pattern addr", 6),
            wire("wv_read_merge", "read", "merge", "pins", 16),
        ],
        "notes": ["The wave player is for waveforms whose encoding is precomputable: arbitrary pin patterns at fractional periods.",
                  "Peak 33.3 M patterns/s at the shipped 33.33 MHz clock."],
    }


def top_diagram():
    return {
        "schema": "socpulse.diagram.v1",
        "title": "LOOM — programmable protocol emulator",
        "subtitle": "tt_um_rajesh963_loom · Tiny Tapeout IHP CMOS5L · 8×4 tiles · 33.33 MHz",
        "source": "rtl/tt_um_rajesh963_loom.v — 4 threads + hardware event scheduler",
        "width": 1280, "height": 840,
        "groups": [
            grp("host", "Host link (SPI)", 40, 60, 300, 180, CYAN),
            grp("compute", "Compute", 380, 60, 500, 330, VIOLET),
            grp("memory", "Memory", 920, 60, 320, 330, BLUE),
            grp("engine", "Pattern engine", 380, 420, 500, 200, PINK),
            grp("revb", "Revision B (not in this tape-out)", 920, 420, 320, 200, SLATE),
            grp("pins", "24 digital pins", 40, 660, 1200, 150, SLATE),
        ],
        "blocks": [
            blk("u_host", "u_host", 70, 110, 240, 100, CYAN,
                sublabel="loom_host", badges=["SPI mode 0", "13 commands", "5.56 MHz max"],
                details="Host bridge: loads programs and data, reads status, moves FIFO bytes. This is how the chip is reprogrammed after fabrication.",
                spec="architecture.md §6", internal=host_internal()),
            blk("u_core", "u_core", 410, 110, 440, 250, VIOLET,
                sublabel="loom_core", badges=["4 threads", "32-bit ISA", "1 IPC"],
                details="Scheduler, datapath, event queues, CSRs and FIFOs. No protocol is hardwired: protocols are firmware loaded at runtime.",
                spec="architecture.md §4", internal=core_internal()),
            blk("u_mem", "u_mem", 950, 110, 260, 250, BLUE,
                sublabel="loom_mem", badges=["1024×32 SRAM", "32×16 DMEM"],
                details="Program memory (one IHP SRAM macro, 140,183 µm²) and the data memory / wave pattern buffer.",
                spec="architecture.md §5", internal=mem_internal()),
            blk("u_wave", "u_wave", 430, 460, 400, 120, PINK,
                sublabel="loom_wave", badges=["33.3 M patterns/s", "12.4 divider"],
                details="Streams precomputed pin patterns from DMEM at fractional symbol periods.",
                spec="architecture.md §7.1", internal=wave_internal()),
            blk("pins", "pins_out / pins_in / dir", 70, 700, 1140, 80, SLATE,
                sublabel="19 protocol pins + host SPI + spare", badges=["uo[7:0]", "uio[7:0]", "ui[7:0]"],
                details="16-bit pin word + direction. UART, SPI, I²C, PS/2, 1-Wire, WS2812 and host SPI all map onto these 24 pins as firmware conventions.",
                spec="architecture.md §5.2"),
            blk("capture", "u_capture", 950, 470, 260, 100, SLATE,
                sublabel="revision B", badges=["gated off"],
                details="Record/replay engine: RTL complete and unit-verified, excluded from this tape-out (DMEM aliasing).",
                spec="architecture.md §7.3"),
        ],
        "wires": [
            wire("w_host_core", "u_host", "u_core", "host cmd/status + FIFOs", 8),
            wire("w_core_mem", "u_core", "u_mem", "imem 32 / dmem 16", 32),
            wire("w_core_wave", "u_core", "u_wave", "wave ctrl", 8),
            wire("w_core_pins", "u_core", "pins", "pins_out / dir", 16),
            wire("w_pins_core", "pins", "u_core", "pins_in", 16),
            wire("w_pins_host", "pins", "u_host", "H_CS_N / SCK / MOSI", 3),
            wire("w_host_pins", "u_host", "pins", "H_MISO / H_IRQ", 2),
            wire("w_mem_wave", "u_mem", "u_wave", "pattern data", 16),
            wire("w_host_capture", "u_capture", "u_core", "captured word", 32),
        ],
        "notes": [
            "Click a block with an internal diagram (badge “modules”) to drill in; use the breadcrumb to go back.",
            "Instance names match the RTL exactly: u_core / u_mem / u_host / u_wave (+ u_capture, gated off in this revision).",
            "This JSON is SiliconPulse diagram.v1 — it can also be loaded into siliconpulse-3d via “Load JSON”.",
        ],
    }


def main():
    d = top_diagram()
    (HERE / "loom.diagram.json").write_text(json.dumps(d, indent=2) + "\n")
    js = ("// Generated by gen_diagram.py — do not edit by hand.\n"
          "window.LOOM_DIAGRAM = " + json.dumps(d, indent=2) + ";\n")
    (HERE / "loom.diagram.js").write_text(js)

    def count(x):
        n = 1
        for b in x.get("blocks", []):
            if b.get("internal"):
                n += count(b["internal"])
        return n

    print(f"diagram: {len(d['blocks'])} top blocks, "
          f"{count(d)} blocks total, "
          f"{sum(1 for _ in pathlib.Path(HERE).glob('*'))} viewer files")


if __name__ == "__main__":
    main()

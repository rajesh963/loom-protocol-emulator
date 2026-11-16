#!/usr/bin/env python3
"""Known RTL / model / spec deviations found by the verification suite.

Every entry is a *reported* bug with a minimal repro; the corresponding test is
skipped (not deleted) while ``open`` is True so that CI is honest: a green run
means "everything except the open deviations passed".  When the owner fixes an
issue, flip ``open`` to False and the test starts guarding it.

Owner of the fix / channel:
  rtl   -> Lead (core) or hostbus (host bridge), reported via Lead
  model -> toolchain
  spec  -> Lead (spec is frozen; a deviation is a spec bug report)
"""

import os as _os

# Pre-validation override: `LOOM_KNOWN_FIXED=VER-004,VER-005 make ...` forces the
# listed issues closed (used to test a proposed RTL fix on a scratch copy before
# the owner lands it; CI never sets this).
_FORCE_FIXED = {x.strip() for x in _os.environ.get("LOOM_KNOWN_FIXED", "").split(",") if x.strip()}

KNOWN_ISSUES: dict[str, dict] = {
    "pout_host_bits": {
        "open": False,
        "id": "VER-001",
        "title": "OUT/SET/CLR/TOG and CSR POUT writes do not mask host bits 9:8",
        "spec": "spec §4.1 `out imm16`: pins_out = imm16 (host bits ignored); §3 S6",
        "rtl": "rtl/loom_core.v:328-331,533-536 apply imm16 unmasked to pout, so "
               "`out 0xFFFF` leaves POUT[9:8]=1 (readable through CSR 0x10).",
        "model": "sw/loom_model.py pins_visible also keeps the raw 16 bits "
                 "(matches RTL now, diverges from §4.1).",
        "impact": "externally invisible (pins_out[9:8] are not bonded out) but "
                  "architecturally visible via CSR POUT reads; co-sim programs "
                  "must avoid bits 9:8 until resolved.",
        "repro": "load `out 0xFFFF; halt`, RUN, host READ_CSR(0x10) -> 0xFFFF, "
                 "spec expects 0xFCFF.",
        "decision": "Lead: mask with PINS_OUT_DRIVABLE (0xFCFF) in the RTL, or "
                    "update spec §4.1. Either way model+RTL+test must agree.",
    },
    "tfifo_layout": {
        "open": False,
        "id": "VER-002",
        "title": "TFIFO CSR bit layout differs between spec, RTL and model",
        "spec": "spec §4.2 (frozen 2026-11-16): {4'b0, rx_count[11:8], "
                "tx_count[7:4], 2'b0, tx_full[1], rx_empty[0]}",
        "rtl": "rtl/loom_core.v:457 -> {4'b0, rx_cnt[11:8], tx_cnt[7:4], 2'b0, "
               "tx_cnt==8 [1], rx_cnt==0 [0]} (counts per spec; full flags "
               "swapped: [1]=tx_full, [0]=rx_empty)",
        "model": "sw/loom_model.py _thread_csr_read(TFIFO) -> tx_cnt at [9:6], "
                 "rx_cnt at [5:2], bit1=tx_full, bit0=rx_empty (both counts and "
                 "flags differ from spec AND from RTL)",
        "impact": "host STATUS byte is unaffected (it uses counts directly), but "
                  "a thread doing `csrr r, TFIFO` sees different bits on RTL and "
                  "model; differential co-sim will flag it.",
        "repro": "push 3 bytes into thread TX FIFO, host READ_CSR(0x43) "
                 "(thread 0 TFIFO): RTL=0x0030, spec=0x0030? -> flags differ; "
                 "model=0x00C0.",
        "decision": "RESOLVED 2026-11-16: spec §4.2 frozen to the RTL layout "
                    "(counts [11:8]/[7:4], tx_full bit1, rx_empty bit0); RTL "
                    "already matches, toolchain re-aligning the model. Test "
                    "test_tfifo_layout now guards the frozen layout.",
    },
    "global_csr_read": {
        "open": False,
        "id": "VER-003",
        "title": "Host CSR read path broken: global registers decode to the wrong "
                 "source, and back-to-back reads (STATUS command) are off by one "
                 "address because host_csrr_data is not registered",
        "spec": "spec §4.2 global CSR map + §6 host protocol (read commands return "
                "the addressed register)",
        "rtl": "rtl/loom_core.v csr_rdata mux: the global `case (csr_rd_addr)` uses "
               "the *thread* csrr address register (never loaded by the host) instead "
               "of the host address; only the 0x40..0x7F bank decode uses "
               "host_csrr_addr. Host reads therefore return bank_rdata of the "
               "last-issued thread.",
        "impact": "host cannot read POUT/PDIR/TMR/EVT/IRQ/WCTRL/WADDR/WLEN/WPER/"
                  "WSTATUS/CAPCTRL/CAPSTAT/GSTATUS: IRQ read-clear + GSTATUS + TMR "
                  "polling all dead from the host side; thread `csrr` path works.",
        "repro": "after rst_n: READ_CSR(0x10) -> 0x0010 (=period[0]); "
                 "READ_CSR(0x12) TMR -> 0x0010; after `csrw TPERIOD, r0` with "
                 "r0=0x208D, host READ_CSR(0x11) -> 0x208D instead of 0x0000. "
                 "STATUS command returns 0x54 0x54 0x54 0x54 instead of "
                 "0x94 0x94 0x94 0x94 (csr_rd_data is combinational on a "
                 "host_csrr_addr that changes every cycle; hostbus samples 2 clk "
                 "after issue, so it reads the *next* CSR).",
        "status": "FIXED 2026-11-16 by Lead: split host/thread CSR read views + "
                  "registered host read response; repro now returns 0x0000/0x0000/"
                  "non-zero TMR and STATUS bytes 0x94. test_global_csr_reads + "
                  "test_status_command now guard it.",
        "decision": "Lead (rtl/loom_core.v owner) fixing: (a) decode the global "
                    "case from host_csrr_addr when host_csrr_en is/was asserted, "
                    "(b) register host_csrr_data (valid the cycle after "
                    "host_csrr_en, as rtl/loom_host.v expects) so the hostbus "
                    "STATUS 8-read pipeline lands on the right addresses. Tests "
                    "are gated and start guarding the moment this flips to False.",
    },
    "dmem_addr": {
        "open": False,
        "id": "VER-004",
        "title": "dmem_addr output never driven -> ld/st/ldi/sti all address DMEM[0]",
        "spec": "spec §4.1 ld/st/ldi/sti use imm16[5:0] / R[rs][5:0] as the DMEM address",
        "rtl": "rtl/loom_core.v: `output reg [5:0] dmem_addr` is only assigned in the "
               "reset branch; dmem_addr_i is computed in the decode block but there "
               "is no assign/connection to the port, so it is stuck at 0.",
        "impact": "all absolute and indirect DMEM addressing aliases to word 0; "
                  "programs that store results are unwritable, so the whole ISA/cosim "
                  "observability is blocked.",
        "repro": "asm `movi r0,0x1111 / st 1,r0 / movi r0,0x2222 / st 2,r0 / halt` "
                 "-> host DMEM dump [0x2222,0,0,...]; TPC=halt proves all insns "
                 "retired. Lead notified 2026-11-16.",
        "decision": "Lead (rtl/loom_core.v owner) fixing; test_isa skips the memory "
                    "test while open (other ISA tests still run to surface further bugs).",
    },
    "i2c_400k": {
        "open": True,
        "id": "VER-006",
        "title": "I2C 400 kHz write: VIP target decodes bit errors on the second/third byte",
        "spec": "spec §1/§9: I2C controller 100 and 400 kHz",
        "rtl": "at 400 kHz (75-cycle half phases) the i2c_master firmware runs "
               "37.5-cycle quarter waits with WAITP/WAITD jitter; the VIP target "
               "(8-cycle poll) decodes addr 0xA0 correctly but reg/data as 0x80/0x56 "
               "instead of 0x00/0x5A, i.e. single-bit sampling errors on the DUT->VIP "
               "SDA stream (could be VIP poll aliasing or firmware quarter timing).",
        "impact": "400 kHz is advertised in the datasheet; 100 kHz (+ clock stretch) "
                  "is verified pass.",
        "repro": "make sim COCOTB_TEST_MODULES=test_protocols "
                 "TESTCASE=test_i2c_controller_400k -> decoded bytes "
                 "['0xa0','0x80','0x56'].",
        "decision": "firmware + verify to disambiguate VIP poll aliasing vs firmware "
                    "quarter timing (needs a faster VIP poll than 8 cycles at 400 kHz); "
                    "tests gated until then.",
    },
    "i2c_target_ack": {
        "open": True,
        "id": "VER-007",
        "title": "I2C target NAKs the data byte of a write transaction",
        "spec": "spec §9: I2C target example acks address/reg/data, DMEM register file",
        "rtl": "with i2c_target.asm on the RTL, a VIP master write 0x50/reg0x02=0x5A "
               "decodes ACK ACK NAK (data byte NAKed); DUT DMEM[2] not written. On the "
               "golden model the same firmware self-test is green, so this is an "
               "RTL-vs-model divergence in the WAITP/WAITD/target polling path.",
        "impact": "I2C target mode unverified on RTL until resolved (controller mode "
                  "100 kHz passes).",
        "repro": "make sim COCOTB_TEST_MODULES=test_protocols "
                 "TESTCASE=test_i2c_target_write -> decoded events end with "
                 "('ack', ack=1), DMEM[2]=0x00.",
        "decision": "reported to firmware + Lead; verify will bisect against the "
                    "model waveform and re-enable.",
    },
    "host_read_during_wait": {
        "open": False,
        "id": "VER-009",
        "title": "Host READ_CSR issued while a thread is stalled in WAITU can return "
                 "a stale value and poison later host reads",
        "spec": "spec §4.2/§6: host CSR reads are independent of thread execution",
        "rtl": "observed on RTL: polling TSTATUS via the host while the thread is "
               "parked in WAITU (e.g. after an absolute WAITU) returns en=1/halted=0 "
               "correctly at the first read, but subsequent reads keep returning "
               "that stale byte even after the thread has halted (VPI shows "
               "t_halt=1 and pc at HALT). With no host reads during the run, all "
               "reads return 0x0003 (halted+en) as expected. Root cause not yet "
               "narrowed (hostbus capture timing vs core host-read view under "
               "stall).",
        "impact": "host-side polling loops (wait-for-halt / TMR polling) are "
                  "unreliable while a thread is in a timed wait; workaround used "
                  "by the timing layer: run a computed number of cycles with no "
                  "host traffic, then STOP and read.",
        "repro": "make sim COCOTB_TEST_MODULES=test_timing "
                 "TESTCASE=test_waitu_absolute_jitter_free with wait_halted "
                 "polling inserted -> timeout; VPI t_halt=1 while TSTATUS reads 0x1.",
        "decision": "reported to Lead/hostbus; tests avoid host polling during "
                    "timed-wait windows until resolved.",
    },
    "status_pipeline": {
        "open": True,
        "id": "VER-005",
        "title": "STATUS command bytes are still off by one CSR after the VER-003 fix",
        "spec": "spec §6 STATUS: 4 thread bytes {halted,en,tx_full,tx_empty,"
                "rx_full,rx_empty,2'b00}; after reset 0x94 each",
        "rtl": "the hostbus STATUS engine issues 8 CSR reads one per clk and "
               "samples csr_rd_data 2 clk later (OWNERS contract: valid the cycle "
               "after csr_rd_en), but loom_core's host_csrr_data is still "
               "combinational on host_csrr_addr, so the byte is assembled from the "
               "next CSR (after reset thread0 byte reads 0x84/0x54 instead of 0x94). "
               "Single READ_CSR reads are correct (global matrix passes).",
        "impact": "STATUS is the only host command that breaks; per-thread "
                  "TSTATUS/TFIFO single reads and the whole global CSR map work.",
        "repro": "after rst_n: spi_xfer([0x0B,0,0,0,0]) -> 80 84 84 84 84 80 "
                 "(want 94 ...).",
        "decision": "reported to Lead: register host_csrr_data one cycle after "
                    "host_csrr_en in the core (or make hostbus capture one cycle "
                    "earlier). test_status_command is gated and will guard it.",
    },
    "irq_read_clear": {
        "open": False,
        "id": "VER-011",
        "title": "CSR IRQ (0x14) read does not clear the code (host and thread)",
        "spec": "spec §4.2: IRQ RW, \"host IRQ code (read clears)\"",
        "rtl": "rtl/loom_core.v global_read(CSR_IRQ) returns irq_code but the read "
               "has no side effect: reading 0x14 twice returns the same code.",
        "impact": "host IRQ ack via read (documented) does not work; ACK_IRQ "
                  "command still clears it.",
        "repro": "WRITE_CSR(0x14,0x1234); READ_CSR(0x14) -> 0x1234; READ_CSR(0x14) "
                 "-> 0x1234 (spec expects 0).",
        "decision": "RESOLVED by spec change 2026-11-16: CSR_IRQ reads are "
                    "non-destructive; ACK_IRQ (host writes 0 to CSR_IRQ) clears. "
                    "Tests updated to the new spec.",
    },
    "waitu_past": {
        "open": False,
        "id": "VER-008",
        "title": "WAITU with an already-past absolute target stalls ~65k cycles",
        "spec": "spec §3.1 timing primitives: waitu t -> next issue at max(k+1,t), "
                "so a past target retires immediately",
        "rtl": "the absolute-target resolution computes an unsigned modular delta "
               "and stalls delta-1 cycles; a target a few cycles behind therefore "
               "becomes a ~65534-cycle stall (observed stall=53779 after `waitu "
               "0x0064` at TMR 11657). The model resolves past targets to the "
               "current cycle.",
        "impact": "affects any firmware that chains absolute WAITU targets where "
                  "the next target can already be behind the wake time; the timing "
                  "layer works around it by using well-separated future targets.",
        "repro": "load `waitu 0x0064 / movi r0,0x2222 / halt`, run after TMR>100: "
                 "thread stalls instead of retiring; VPI stall[0] ~53779 and "
                 "decreasing.",
        "decision": "reported to Lead (core); a targeted test will be added once "
                    "fixed (test_timing uses future-only targets today).",
    },
    "csrr_tperiod_read": {
        "open": False,
        "id": "VER-010",
        "title": "Thread `csrr rd, TPERIOD` returns the reset period (0x0010), "
                 "not the register value",
        "spec": "spec §4.2: TPERIOD (0x00) is RW, thread bank; `csrr` must return it",
        "rtl": "observed on RTL: with period[0] set to 0x1000 (host write or "
               "`csrw`), `csrr r1, TPERIOD` yields r1=0x0010 while VPI shows "
               "period[0]=0x1000; `csrr TMASK/TEMASK/TSTATUS` read correctly in "
               "the same program. Debug capture at the csrr issue cycle shows "
               "csr_rd_addr=00, sel=0, csr_rdata=0x0010. Likely a combinational "
               "scheduling hazard: `wb_data=csr_rdata` is assigned in the decode "
               "`always @*` block while `csr_rdata` is driven by a different "
               "`always @*` block that also depends on signals the decode block "
               "assigns (csr_rd_addr) -- the read data can be one evaluation stale.",
        "impact": "firmware that reads TPERIOD back (and any test of the RW bank) "
                  "sees 0x0010; other thread-bank reads are unaffected.",
        "repro": "host WRITE_CSR(0x40,0x1000); load `csrr r1,TPERIOD / halt`; RUN; "
                 "VPI regf[1]=0x0010, period[0]=0x1000.",
        "decision": "reported to Lead (core); ISA test verifies the write through "
                    "the host view and gates the thread readback until fixed.",
    },
    "host_csr_read_stale": {
        "open": False,
        "id": "VER-012",
        "title": "Host READ_CSR can return the previous read's data (back-to-back "
                 "CSR reads are one read stale)",
        "spec": "spec §6: read commands return the addressed register on the "
                "following transfers",
        "rtl": "observed on RTL with the thread halted: READ_CSR(TMR) twice in a "
               "row returns the same value while VPI TMR advances (200,200 then "
               "EVT read forces a fresh 1376 on the third); after an EMITE fires, "
               "READ_CSR(TEQ) keeps returning 1 while VPI eq_cnt=0. The hostbus "
               "samples csr_rd_data at dead>=1 while the core's host read view is "
               "combinational on host_csrr_addr -- the handshake landing cycle "
               "needs one more pipeline stage on one side.",
        "impact": "affects repeated/same-address host CSR reads and is the likely "
                  "root cause of VER-005 (STATUS byte off by one CSR) and VER-011 "
                  "(IRQ appears not to clear on read). Single reads of distinct "
                  "registers work (global CSR matrix passes).",
        "repro": "thread halted; host.read_csr(TMR) -> 200; host.read_csr(TMR) -> "
                 "200 (VPI tmr is ~800 larger each time); host.read_csr(EVT); "
                 "host.read_csr(TMR) -> fresh.",
        "decision": "reported to Lead/hostbus; tests avoid consecutive same-address "
                    "reads and the co-sim layer compares VPI state directly.",
    },
    "inat_rd_truncation": {
        "open": False,
        "id": "VER-013",
        "title": "INAT writeback register index is truncated to 2 bits "
                 "(rd bit 2 lost)",
        "spec": "spec §4.1: `inat rd, t16` latches pins_in into R[rd] of the "
                "issuing thread, rd = 0..7",
        "rtl": "rtl/loom_core.v: `reg [1:0] inat_rd[0:3]` + "
               "`inat_rd[sel] <= rd[1:0]`, then "
               "`regf[{t[1:0], inat_rd[t]}] <= pins_in` -> 4-bit index, so a "
               "thread-1 inat r4..r7 writes thread-0's r4..r7 and a thread-0 "
               "inat r4..r7 writes its own r0..r3.  Toolchain reproduced the "
               "seed-2 register file exactly by emulating only this bug.",
        "impact": "corrupts registers in any program that arms an INAT with "
                  "rd >= 4 (1/3 of the random wait bundle, all opt-in seeds); "
                  "co-sim seed 2/5 diffs were this cascade.",
        "repro": "thread1 `inat r4, <future>` -> RTL `t0.r4` gets pins_in and "
                 "`t1.r4` is unchanged.",
        "fix": "`reg [2:0] inat_rd [0:3];` and `inat_rd[sel] <= rd;` (reset "
               "`3'd0`).",
        "decision": "requested from Lead; test_inat_all_registers_all_threads "
                    "is the directed regression and is gated until fixed.",
    },
    "nextt_emita": {
        "open": False,
        "id": "VER-014",
        "title": "EMITA/EMITD update NEXTT and PFRAC (spec: only EMIT does)",
        "spec": "spec §3.1 timing table: `emit` advances NEXTT/PFRAC; "
                "`emita`/`emitd` set their own target and leave NEXTT alone",
        "rtl": "rtl/loom_core.v event-enqueue block: "
               "`if (ev_enq_t) begin nextt[sel] <= ev_enq_time; "
               "pfrac[sel] <= per_frac[3:0]; end` runs for all three timed "
               "emits, so an absolute/relative emit pollutes the periodic "
               "chain (final NEXTT observed as generator absolute targets, "
               "e.g. 1200+256k).",
        "impact": "downstream emit timing after any emita/emitd; co-sim "
                  "`nextt`/EQ-target and one-cycle trace diffs in the remaining "
                  "5/100 seeds.",
        "repro": "`emit r; emita r,0x3000; emitd r,77; csrr TNEXTT` -> RTL "
                 "TNEXTT = TMR+77, spec = NEXTT after the first emit.",
        "fix": "`if (ev_enq_t && (opc == `OPC_EMIT)) begin nextt[sel] <= ...; "
               "pfrac[sel] <= ...; end`.",
        "decision": "requested from Lead; test_emita_emitd_do_not_touch_nextt "
                    "is the directed regression and is gated until fixed.",
    },
    "host_csrr_data_registered": {
        "open": True,
        "id": "VER-015",
        "title": "host_csrr_data is combinational on host_csrr_addr, so the "
                 "STATUS engine's 8 back-to-back reads sample the wrong address",
        "spec": "rtl/loom_host.v header: STATUS issues one CSR read per clk and "
                "captures 2 clk after each issue, i.e. csr_rd_data must be "
                "f(csr_rd_addr) registered one cycle after the address is "
                "presented (pipelined reads)",
        "rtl": "rtl/loom_core.v `assign host_csrr_data = host_csrr_rdata;` is "
               "purely combinational on the registered `host_csrr_addr`.  For "
               "isolated reads that is fine (address held), but the STATUS "
               "engine advances the address every cycle, so the p2 capture "
               "sees f(addr_{i+1}) while p2_i says addr_i.  Thread 0's byte is "
               "launched in two halves, so the result is 0x84 instead of "
               "0x94 (tx_empty/tx_full bits sampled one read late).",
        "impact": "host STATUS command: thread 0 byte 0x84 instead of 0x94 "
                  "(other bytes usually right by luck of the byte boundary); "
                  "test_host.test_status_command.",
        "repro": "`make TESTCASE=test_status_command` -> bytes [0x24,0x90,"
                 "0x94,0x94] vs expected thread0 0x94.",
        "fix": "Register the read data one cycle, e.g.\n  reg [15:0] "
               "host_csrr_data_r;\n  always @(posedge clk) if (!rst_n) "
               "host_csrr_data_r <= 16'd0; else host_csrr_data_r <= "
               "host_csrr_rdata;\n  assign host_csrr_data = host_csrr_data_r;\n"
               "Pre-validated on `test/_scratch_rtl2` (copy of current rtl + "
               "this patch): test_status_command PASS.",
        "decision": "reported to Lead; test_status_command stays gated on this "
                    "entry (also re-gates the STATUS-dependent host checks).",
    },
}

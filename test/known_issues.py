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
        "open": True,
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
        "decision": "Lead (rtl/loom_core.v owner) fixing: (a) decode the global "
                    "case from host_csrr_addr when host_csrr_en is/was asserted, "
                    "(b) register host_csrr_data (valid the cycle after "
                    "host_csrr_en, as rtl/loom_host.v expects) so the hostbus "
                    "STATUS 8-read pipeline lands on the right addresses. Tests "
                    "are gated and start guarding the moment this flips to False.",
    },
}

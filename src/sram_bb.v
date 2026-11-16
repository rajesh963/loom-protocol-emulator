// ============================================================================
//  src/sram_bb.v — black-box declaration of the LOOM program-memory SRAM macro
// ============================================================================
//  Owner: wavecapt (SRAM integration).  This file lives in src/ (like the
//  official Tiny Tapeout cmos5l SRAM example) so the Tiny Tapeout GDS flow
//  sees it as a source file; it is copied to src/ by tools/sync_src.sh.
//
//  Synthesis + lint use this black box; the physical GDS/LEF/Liberty views
//  come from the `MACROS` block in src/config.json, and the behavioural model
//  used by RTL simulation is rtl/loom_sram_model.v (LOOM_SIM).
//
//  Selectable macro (build define, see tools/select_build.sh):
//    (default)        RM_IHPSG13_1P_1024x32_c2_bm_bist : 1024 x 32, 140 183 um^2
//    -DLOOM_PMEM_512  RM_IHPSG13_1P_512x32_c2_bm_bist  :  512 x 32,  79 720 um^2
//  Firmware constraint for the 512-word build: the four 128-word program slots
//  live at words 0x000/0x080/0x100/0x180 (top used word 509), so nothing may
//  be placed above word 509.
//
//  `ifndef SIM: gate-level simulation defines -DSIM and supplies the IHP PDK
//  behavioural model instead (flow/sram_model/), exactly as the official
//  example does with test/sram_model/.
// ============================================================================

`default_nettype none

`ifndef SIM

`ifdef LOOM_PMEM_512
/* verilator lint_off UNDRIVEN */
/* verilator lint_off UNUSEDSIGNAL */
(* blackbox *)
module RM_IHPSG13_1P_512x32_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [8:0]  A_ADDR,
    input  wire [31:0] A_DIN,
    input  wire        A_DLY,
    output wire [31:0] A_DOUT,
    input  wire [31:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [8:0]  A_BIST_ADDR,
    input  wire [31:0] A_BIST_DIN,
    input  wire [31:0] A_BIST_BM
);
endmodule
/* verilator lint_on UNUSEDSIGNAL */
/* verilator lint_on UNDRIVEN */
`else
/* verilator lint_off UNDRIVEN */
/* verilator lint_off UNUSEDSIGNAL */
(* blackbox *)
module RM_IHPSG13_1P_1024x32_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [9:0]  A_ADDR,
    input  wire [31:0] A_DIN,
    input  wire        A_DLY,
    output wire [31:0] A_DOUT,
    input  wire [31:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [9:0]  A_BIST_ADDR,
    input  wire [31:0] A_BIST_DIN,
    input  wire [31:0] A_BIST_BM
);
endmodule
/* verilator lint_on UNUSEDSIGNAL */
/* verilator lint_on UNDRIVEN */
`endif // LOOM_PMEM_512

`endif // SIM

`default_nettype wire

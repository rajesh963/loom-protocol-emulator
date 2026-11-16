# LOOM — IHP SRAM instruction memory: flow integration

The program memory (PMEM) is replaced by one IHP single-port SRAM macro to get
the design under the 8x4 tile area budget: 4,096 PMEM flops (~200 kµm²) + the
128:1 32-bit read mux (~117 kµm²) become one macro (140 kµm² for 1024×32, or
80 kµm² for the 512×32 area-reduced variant).

## Build selection

```sh
tools/select_build.sh sram      # RM_IHPSG13_1P_1024x32 (default flagship)
tools/select_build.sh sram512   # RM_IHPSG13_1P_512x32  (-60 kµm², 512 words)
tools/select_build.sh ff        # 128-word FF-array fallback build
tools/select_build.sh status    # what is selected in src/config.json now
```

`select_build.sh` merges `flow/config_sram.json` (or `config_sram512.json`)
**over the current `src/config.json`** — it never replaces the file, so
locally maintained keys (`CLOCK_PERIOD`, `PL_TARGET_DENSITY_PCT`, the
`pdk::ihp-sg13cmos5l` max-fanout block, …) are preserved; `ff` removes exactly
the SRAM delta keys again.  It then runs `tools/sync_src.sh`, which mirrors
`rtl/` into `src/` (the GDS workflow does the same before hardening).

Build switches replace only the program-memory defines (`LOOM_PMEM_SMALL`,
`LOOM_PMEM_SRAM`, `LOOM_PMEM_512`); any independent area lever already in
`VERILOG_DEFINES` (e.g. `LOOM_DMEM_SMALL`, `LOOM_ENABLE_CAPTURE`) is preserved.

512-word firmware constraint: the four 128-word program slots live at word
addresses 0x000/0x080/0x100/0x180 and use words 0..509, so 512 words is exactly
enough — nothing may be placed above word 509 (addresses ≥512 read HALT).

## Files (official Tiny Tapeout layout, adapted from `ttihp-cmos5l-sram-example`)

| File | Role |
|---|---|
| `rtl/loom_pmem_sram.v` | the wrapper: fetch port (1-cycle sync read), host write/read ports, single-macro-port arbitration (host write > host read > fetch), `A_DLY=1`, BIST off, HALT word when a fetch is displaced |
| `rtl/loom_sram_model.v` | behavioural macro model for RTL simulation (`LOOM_SIM` only, no PDK needed) |
| `src/sram_bb.v` | `(* blackbox *)` declaration of the selected macro for synthesis/lint |
| `src/pdn_cfg.tcl` | LibreLane PDN config without the per-macro grid (copied verbatim from the official example) |
| `librelane_plugin_sram_pdn.py`, `odb_stripes.py` | `Project.ExtendPowerStripes` step, at the repo root where LibreLane auto-imports it (copied verbatim) |
| `flow/config_sram.json`, `flow/config_sram512.json` | config deltas merged into `src/config.json` |
| `flow/sram_model/` | IHP PDK behavioural macro models for **gate-level** simulation (Apache-2.0, unmodified) |

The macro instance is a grandchild of the top: `u_mem` (loom_mem) →
`u_pmem` (loom_pmem_sram) → `sram` (macro).  After synthesis flattening its
name is **`u_mem.u_pmem.sram`**, which is the key in `MACROS.instances` and in
`PDN_MACRO_CONNECTIONS`.  (A generate-if around the macro would rename it to
`u_mem.u_pmem.g_*.sram`, which is why the macro choice is a compile-time
`` `ifdef LOOM_PMEM_512 `` instead of a parameter.)

## The merged config keys (documented patch for src/config.json)

`flow/config_sram.json` adds, in addition to `VERILOG_DEFINES`:

```json
"meta": { "flow": "Classic",
          "substituting_steps": { "+OpenROAD.GeneratePDN": "Project.ExtendPowerStripes" } },
"MACROS": { "RM_IHPSG13_1P_1024x32_c2_bm_bist": {
    "instances": { "u_mem.u_pmem.sram": { "location": [3.36, 3.78], "orientation": "FS" } },
    "gds":   ["pdk_dir::libs.ref/sg13cmos5l_sram/gds/RM_IHPSG13_1P_1024x32_c2_bm_bist.gds"],
    "lef":   ["pdk_dir::libs.ref/sg13cmos5l_sram/lef/RM_IHPSG13_1P_1024x32_c2_bm_bist.lef"],
    "spice": ["pdk_dir::libs.ref/sg13cmos5l_sram/cdl/RM_IHPSG13_1P_1024x32_c2_bm_bist.cdl"],
    "lib": { "*_typ_*":  ["…_typ_1p20V_25C.lib"],
             "*_fast_*": ["…_fast_1p32V_m55C.lib"],
             "*_slow_*": ["…_slow_1p08V_125C.lib"] } } },
"PDN_MACRO_CONNECTIONS": [
    "u_mem\\.u_pmem\\.sram VPWR VGND VDD! VSS!",
    "u_mem\\.u_pmem\\.sram VPWR VGND VDDARRAY! VSS!" ],
"PDN_CFG": "dir::pdn_cfg.tcl",
"EXTEND_STRIPES_SRAM_ARRAY_EVERY_OTHER": true,
"ERROR_ON_PDN_VIOLATIONS": 0,
"RUN_MAGIC_DRC": 0,
"ERROR_ON_ILLEGAL_OVERLAPS": 0,
"FP_PDN_VPITCH": 44.96, "FP_PDN_VWIDTH": 2.1,
"FP_PDN_VSPACING": 3.52, "FP_PDN_VOFFSET": 6.15,
"FP_PDN_MULTILAYER": 0
```

Why each one (see `../research/sram-recipe.md` for the full derivation):
the cmos5l PDN is a single vertical Metal4 stripe layer — the same layer as the
macro's `VDD!`/`VDDARRAY!`/`VSS!` columns — so pdngen cannot connect the macro
and its per-macro grid must be disabled (`pdn_cfg.tcl`); `ExtendPowerStripes`
then walks the tile stripes onto the macro's columns.  The four `FP_PDN_*`
numbers match the tile grid to the macro's 11.24 µm column pitch with the macro
at x = 3.36 µm, and the two `PDN_MACRO_CONNECTIONS` lines connect both supply
rails.  The waivers are the official values: Magic DRC/overlap/PDN-violation
checks fire on the LEF abstract (there is no metal in the GDS obstruction bar),
while the Tiny Tapeout precheck still runs the full KLayout DRC deck + LVS in
LibreLane.

## After a hardening run — check list

1. `runs/wokwi/*-project-extendpowerstripes/` log lists the stripes it moved
   onto the macro's columns (expected: "moved … onto a column" messages).
2. `runs/wokwi/*-openroad-irdropreport/irdrop.rpt`: all VPWR/VGND shapes
   connected.
3. LVS clean (the macro `spice:` CDL must be present, otherwise its power pins
   look unconnected).
4. Precheck (KLayout DRC on the final GDS) green; `SRAM.drawing` (25/0) and
   `DigiBnd.drawing` (16/0) are whitelisted for SRAM designs.
5. `src/config.json` committed with the intended build; `info.yaml`
   `source_files` must list `loom_pmem_sram.v`, `loom_sram_model.v`,
   `sram_bb.v` (docs owns `info.yaml`).

## Simulation

```sh
cd test
make                    # FF build, unchanged (57 PASS / 0 FAIL / 2 SKIP)
make SRAM_SIM=1024      # 1024x32 macro, behavioural model (no PDK needed)
make SRAM_SIM=512       #  512x32 macro, behavioural model
```
Gate-level simulation (`make GATES=yes`) needs the IHP macro behavioural model
from `flow/sram_model/` (already wired into `test/Makefile` for the GL branch),
because the hardened netlist keeps the macro as a black box.

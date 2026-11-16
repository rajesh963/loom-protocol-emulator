# LibreLane flow status (running log)

## Attempt 1 — 2026-11-16, FF 128-word build, commit 5ff6c6c
**Result: FAILED at detailed placement (congestion).**

| Metric | Value |
|---|---|
| Die / core | 1724.16 × 710.64 µm / 1,208,172 µm² |
| Mapped cell area after synthesis+ABC | **1,147,604 µm²** |
| Effective utilisation at floorplan | **95.0 %** |
| Post-resizer utilisation | **98.4 %** |
| Global placement routing overflow | 2,125.6 (8,917 tiles = **38.1 %**) |
| Failing step | `OpenROAD.RepairDesignPostGPL` → `[DPL-0036] Detailed placement failed` (451 instances) |
| Cell histogram notes | 8,042 flip-flops (394k µm²), **29,195 `buf_1` = 212k µm²**, 5,658 `mux2_1` + 3,063 `mux4_1` = 220k µm² |

Diagnosis: the design is ~2× over the 8x4 budget (needs <= ~700–750k µm², i.e. ~60 %), so
placement cannot succeed at any target density. Dominant costs: PMEM as a flip-flop array
(4096 flops + 128:1 32-bit read mux ≈ 320k), DMEM (64×16, two read ports, three write ports),
and a large number of ABC-inserted buffers.

Corrective plan (in order):
1. **SRAM instruction memory** (`RM_IHPSG13_1P_1024x32`, 140k µm² macro) replacing the 4096-flop
   array and its read mux: ~−180k µm² and 1024 instruction words (also removes the FF-fallback
   area problem for the flagship build).
2. Shrink DMEM 64→32 words (two 32:1 read muxes) and TX/RX FIFOs 8→4: ~−110k µm².
3. Re-evaluate; if still over, reduce threads 4→2 or instruction width 32→24.
4. Re-harden; record utilisation/WNS/TNS/precheck/GL here.

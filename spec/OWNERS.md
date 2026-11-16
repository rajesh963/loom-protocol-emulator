# LOOM — Ownership, Interfaces & Working Agreement

**Read `spec/ARCHITECTURE.md` first — it is the authoritative design.**
`rtl/loom_bus.vh` is FROZEN (parameters, opcodes, CSRs, host commands).

Repo root: `/home/rajesh/Documents/deepseek-harness/default-workspace/chipchallenge/loom`
Python: use `../.venv/bin/python` (has cocotb + yowasp-yosys). `iverilog` is on PATH.
**`/tmp` is NOT persistent between shell calls — never use it.** Set
`export YOWASP_CACHE_DIR=<repo>/.yowasp_cache` for yosys.
Simulator: Icarus Verilog 12 (`iverilog -g2012`).

## Rules
1. **Only edit files in your scope.** If you need a change in someone else's file or in
   `loom_bus.vh`/the spec, message the Lead instead of editing.
2. Verilog-2005-compatible, `default_nettype none`, no SystemVerilog-only constructs in `rtl/`
   (Icarus and LibreLane/yosys both consume it).
3. Every module you write needs a small self-checking testbench you can run yourself with iverilog
   (`make -C test/<name>` or a script); keep it in your scope.
4. Commit-quality output only: no TODOs left in delivered modules, no `$display` debug left on.
5. Report back with: files created, what you verified and how, known gaps, and any interface
   question. Keep it short.

## File ownership

| Scope | Owner | Files |
|---|---|---|
| core + memory + top + integration | **Lead** | `rtl/loom_core.v`, `rtl/loom_mem.v`, `rtl/tt_um_rajesh963_loom.v`, `rtl/loom_types.vh` |
| host bridge | **hostbus** | `rtl/loom_host.v`, `rtl/loom_fifo.v`, `test/unit_host/` |
| wave player (+ capture stretch) | **wavecapt** | `rtl/loom_wave.v`, `rtl/loom_capture.v`, `test/unit_wave/` |
| toolchain + model + host client | **toolchain** | `sw/`, `host/`, `tools/`, `test/unit_sw/` |
| docs / datasheet / write-up | **docs** | `README.md`, `docs/`, `info.yaml`, `spec/` (read-only for spec) |
| verification suite | **verify** | `test/*.py`, `test/tb.v`, `test/Makefile`, `test/requirements.txt`, `formal/` |
| protocol firmware | **firmware** | `sw/examples/`, `sw/prog/`, `host/demos/` |

## Exact module interfaces (copy these ports verbatim)

### `rtl/loom_host.v` (owner: hostbus)
```verilog
module loom_host (
  input  wire        clk,
  input  wire        rst_n,
  // Tiny Tapeout host pins (async to clk on the SCK side: 2-FF sync inside)
  input  wire        spi_cs_n,      // ui_in[0]
  input  wire        spi_sck,       // ui_in[1]
  input  wire        spi_mosi,      // ui_in[2]
  output wire        spi_miso,      // uo_out[0]
  output wire        irq_out,       // uo_out[1]
  // host -> memory port (PMEM if addr < 0x400 else DMEM)
  output reg         mem_we,
  output reg  [9:0]  mem_addr,
  output reg  [31:0] mem_wdata,
  output reg         mem_re,        // start a read
  input  wire [31:0] mem_rdata,
  input  wire        mem_rvalid,    // one-cycle pulse: mem_rdata valid
  // host -> core CSR port (core owns all CSRs / thread state)
  output reg         csr_wr_en,
  output reg  [7:0]  csr_wr_addr,
  output reg  [15:0] csr_wr_data,
  output reg         csr_rd_en,
  output reg  [7:0]  csr_rd_addr,
  input  wire [15:0] csr_rd_data,   // valid the cycle after csr_rd_en
  // host <-> per-thread byte FIFOs
  output reg         rx_push,       // push a byte into thread RX FIFO
  output reg  [1:0]  rx_push_tid,
  output reg  [7:0]  rx_push_data,
  input  wire [3:0]  rx_full,
  output reg         tx_pop,        // pop a byte from thread TX FIFO
  output reg  [1:0]  tx_pop_tid,
  input  wire [7:0]  tx_pop_data,   // valid same cycle as tx_pop
  input  wire [3:0]  tx_empty,
  // irq / status sources
  input  wire [15:0] irq_code,
  input  wire [3:0]  thread_run_mask
);
```
Semantics: command byte arrives MSB-first on `spi_mosi` sampled at `spi_sck` rising edges while
`spi_cs_n` low; MISO shifts out MSB-first on falling edges. One byte in / one byte out per 8 SCK
cycles. Multi-byte args LSB byte first. Responses are pipelined by one transfer as specified in
`spec/ARCHITECTURE.md` §6. `mem_rvalid`/`csr_rd_data` are consumed by the command FSM; the FSM must
never issue a new command byte until the previous command is complete. `irq_out` is a registered
level: set when `irq_code != 0` or a command raises it; cleared by `HCMD_ACK_IRQ` (write 1 to
`CSR_IRQ`) — implement the latch here from `irq_code`.

### `rtl/loom_wave.v` (owner: wavecapt)
```verilog
module loom_wave (
  input  wire        clk,
  input  wire        rst_n,
  // configuration (from core CSR block)
  input  wire        cfg_start,     // 1-cycle pulse
  input  wire        cfg_loop,
  input  wire        cfg_oneshot,
  input  wire [5:0]  cfg_addr,      // DMEM start word
  input  wire [6:0]  cfg_len,       // 1..64 patterns
  input  wire [15:0] cfg_period,    // 12.4 fixed point
  output wire        busy,
  output wire        done,          // level until cfg_start
  // DMEM read port
  output reg  [5:0]  dmem_addr,
  input  wire [15:0] dmem_data,
  // pins
  output reg  [15:0] wave_pins,
  output reg         wave_valid     // wave_pins valid this cycle (else byte select 0)
);
```
Period semantics: `wave_pins` updates once every `cfg_period` cycles (12.4 fixed, carry
accumulator, drift-free), reads `dmem_data` at `dmem_addr` one cycle ahead (synchronous from
DMEM is not required: DMEM is an FF array with combinational read, see `loom_mem.v`), walks
`cfg_addr .. cfg_addr+cfg_len-1`, then loops / one-shots / stops per config. First pattern appears
`cfg_period` cycles after `cfg_start`.

### `rtl/loom_fifo.v` (owner: hostbus)
```verilog
module loom_fifo #(parameter DEPTH = 8, parameter AW = 3, parameter WIDTH = 8) (
  input  wire            clk, rst_n,
  input  wire            wr_en,  input wire [WIDTH-1:0] wr_data, output wire full,
  input  wire            rd_en,  output wire [WIDTH-1:0] rd_data, output wire empty,
  output wire [AW:0]     count
);
```
Standard synchronous FIFO, `rd_data` = current head (fall-through read, no output register).
`rd_en` while `empty` is a no-op (and a bug in the user's code).

### Memory (`rtl/loom_mem.v`, owner: Lead)
FF arrays; PMEM write port is host-only, DMEM has core rw + wave read + host rw (host wins,
`dmem_denied` tells the core to retry — implement core side of that in `loom_core.v`).

## Integration contract checklist (Lead runs before merge)
- `iverilog -g2012 -o /dev/null rtl/*.v` clean (no implicit nets, all ports connected).
- ISA model and RTL agree on the random-program co-simulation suite.
- yosys area report (`tools/area_synth.sh`) under budget for the FF build.
- No file outside the owner's scope modified.

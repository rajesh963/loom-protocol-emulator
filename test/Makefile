# ============================================================================
#  test/Makefile -- cocotb testbench for LOOM (tt_um_rajesh963_loom)
#
#  Layers (each is its own cocotb module, runs in a fresh simulator process):
#    test_host.py          host SPI protocol / commands / FIFOs / IRQ
#    test_isa.py           directed per-opcode ISA conformance
#    test_timing.py        headline timing claims + ASCII waveform dumps
#    test_random_cosim.py  differential RTL vs sw/loom_model.py (seeded)
#    test_protocols.py     independent UART / SPI / I2C VIPs
#
#  Usage:
#    make                     # run everything (CI entry point)
#    make host|isa|timing|cosim|proto|one MODULE=test_isa
#    make COCOTB_TEST_MODULES=test_host
#    make WAVES=1             # + tb.fst
#    make GATES=yes           # gate-level (needs gate_level_netlist.v + PDK)
# ============================================================================

SIM           ?= icarus
TOPLEVEL_LANG ?= verilog
TOPLEVEL       = tb

# Locate cocotb-config: use PATH if the venv is active, else the repo venv.
VENV_BIN      ?= $(abspath $(CURDIR)/../../.venv/bin)
COCOTB_CONFIG ?= $(shell command -v cocotb-config 2>/dev/null || \
                  echo $(VENV_BIN)/cocotb-config)
# cocotb's own makefiles need the interpreter that has cocotb installed.
PYTHON_BIN    ?= $(shell $(COCOTB_CONFIG) --python-bin 2>/dev/null || echo $(VENV_BIN)/python)
# ...and they find it through PATH inside `$(shell ...)` calls.
export PATH := $(VENV_BIN):$(PATH)

RTL_DIR    ?= $(abspath $(CURDIR)/../rtl)
TEST_DIR   = $(abspath $(CURDIR))
RTL_SRCS   = $(RTL_DIR)/loom_core.v \
             $(RTL_DIR)/loom_mem.v \
             $(RTL_DIR)/loom_host.v \
             $(RTL_DIR)/loom_wave.v \
             $(RTL_DIR)/loom_capture.v \
             $(RTL_DIR)/tt_um_rajesh963_loom.v

SIM_BUILD = sim_build/rtl

VERILOG_SOURCES = $(RTL_SRCS) $(TEST_DIR)/tb.v
COMPILE_ARGS   += -g2012 -I$(RTL_DIR)
COCOTB_HDL_TIMEUNIT      ?= 1ns
COCOTB_HDL_TIMEPRECISION ?= 1ps

# Default = full regression.  Individual layers can be selected on the cmdline.
COCOTB_TEST_MODULES ?= test_host,test_isa,test_timing,test_random_cosim,test_protocols

ifneq ($(GATES),yes)
else
  SIM_BUILD = sim_build/gl
  COMPILE_ARGS += -DGL_TEST -DFUNCTIONAL -DSIM
  PDK ?= ihp-sg13cmos5l
  PDK_CELL_PREFIX = $(patsubst ihp-%,%,$(PDK))
  PDK_LIBS_REF = $(PDK_ROOT)/$(PDK)/libs.ref
  VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_io/verilog/$(PDK_CELL_PREFIX)_io.v
  VERILOG_SOURCES += $(wildcard $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_udp.v)
  VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_stdcell.v
  VERILOG_SOURCES += $(PWD)/gate_level_netlist.v
endif

ifdef WAVES
  COMPILE_ARGS += -DDUMP
  SIM_ARGS     += +dump
endif

include $(shell $(COCOTB_CONFIG) --makefiles)/Makefile.sim

# ---- layer shortcuts -------------------------------------------------------
.DEFAULT_GOAL := sim
.PHONY: host isa timing cosim proto
host:    ; $(MAKE) sim COCOTB_TEST_MODULES=test_host
isa:     ; $(MAKE) sim COCOTB_TEST_MODULES=test_isa
timing:  ; $(MAKE) sim COCOTB_TEST_MODULES=test_timing
cosim:   ; $(MAKE) sim COCOTB_TEST_MODULES=test_random_cosim
proto:   ; $(MAKE) sim COCOTB_TEST_MODULES=test_protocols

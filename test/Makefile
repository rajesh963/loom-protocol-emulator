# ============================================================================
#  test/Makefile -- cocotb testbench for LOOM (tt_um_rajesh963_loom)
#
#  Layers (each is its own cocotb module, runs in a fresh simulator process):
#    test_host.py          host SPI protocol / commands / FIFOs / IRQ
#    test_isa.py           directed per-opcode ISA conformance
#    test_timing.py        headline timing claims + ASCII waveform dumps
#    test_random_cosim.py  differential RTL vs sw/loom_model.py (seeded)
#    test_protocols.py     independent UART / SPI / I2C VIPs
#    test_usb_ls.py        USB 1.1 low-speed device TX (NRZI/stuffing/CRC, RTL)
#    test_eth.py           Ethernet 802.3 Manchester frame TX (preamble/SFD/FCS)
#    test_usb_ls_rx.py     USB LS differential-receiver readiness (pin path)
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
             $(RTL_DIR)/loom_pmem_sram.v \
             $(RTL_DIR)/loom_sram_model.v \
             $(RTL_DIR)/tt_um_rajesh963_loom.v

SIM_BUILD = sim_build/rtl

# Program-memory flavour in RTL simulation.  Empty (default) = the FF-array
# PMEM build, unchanged.  The SRAM builds use the behavioural macro model in
# rtl/loom_sram_model.v, so no IHP PDK is needed:
#   make SRAM_SIM=1024   IHP RM_IHPSG13_1P_1024x32 (140 183 um^2, 1024 words)
#   make SRAM_SIM=512    IHP RM_IHPSG13_1P_512x32  ( 79 720 um^2,  512 words)
SRAM_SIM ?=
export SRAM_SIM          # tests pick the PMEM depth from this (512 vs 1024)
export GATES             # gl_test: skip the layers that need RTL-internal backdoor

VERILOG_SOURCES = $(TEST_DIR)/tb.v
COMPILE_ARGS   += -g2012 -I$(RTL_DIR)
COCOTB_HDL_TIMEUNIT      ?= 1ns
COCOTB_HDL_TIMEPRECISION ?= 1ps

# Default = full regression.  Individual layers can be selected on the cmdline.
COCOTB_TEST_MODULES ?= test_host,test_isa,test_timing,test_random_cosim,test_protocols,test_usb_ls,test_eth,test_usb_ls_rx

# RTL simulation sources (the FF-array or SRAM behavioural build).  In the
# gate-level branch below the hardened netlist replaces ALL of these (the netlist
# already contains tt_um_rajesh963_loom and the SRAM macro instance).
ifneq ($(GATES),yes)
  VERILOG_SOURCES += $(RTL_SRCS)
  ifeq ($(SRAM_SIM),512)
    COMPILE_ARGS += -DLOOM_PMEM_SRAM -DLOOM_PMEM_512 -DLOOM_SIM
  else ifeq ($(SRAM_SIM),1024)
    COMPILE_ARGS += -DLOOM_PMEM_SRAM -DLOOM_SIM
  endif
else
  # ---- gate-level: hardened netlist + PDK cell models + SRAM behavioural ----
  SIM_BUILD = sim_build/gl
  COMPILE_ARGS += -DGL_TEST -DFUNCTIONAL -DSIM
  PDK ?= ihp-sg13cmos5l
  PDK_CELL_PREFIX = $(patsubst ihp-%,%,$(PDK))
  PDK_LIBS_REF = $(PDK_ROOT)/$(PDK)/libs.ref
  VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_io/verilog/$(PDK_CELL_PREFIX)_io.v
  VERILOG_SOURCES += $(wildcard $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_udp.v)
  VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_stdcell.v
  # copied in by the GDS action workflow from the LibreLane run
  VERILOG_SOURCES += $(TEST_DIR)/gate_level_netlist.v
  # IHP SRAM macro behavioural model for gate-level simulation (the RTL build
  # uses rtl/loom_sram_model.v with -DLOOM_SIM instead).  Copied unmodified
  # from IHP-Open-PDK, Apache-2.0 (flow/sram_model/README.md).
  SRAM_MODEL_DIR = $(abspath $(CURDIR)/../flow/sram_model)
  ifeq ($(SRAM_SIM),512)
    VERILOG_SOURCES += $(SRAM_MODEL_DIR)/RM_IHPSG13_1P_512x32_c2_bm_bist.v
  else
    VERILOG_SOURCES += $(SRAM_MODEL_DIR)/RM_IHPSG13_1P_1024x32_c2_bm_bist.v
  endif
  VERILOG_SOURCES += $(SRAM_MODEL_DIR)/RM_IHPSG13_1P_core_behavioral_bm_bist.v
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

ifeq ($(COCOTB),)
  $(error "Please set COCOTB to point to cocotb")
endif

TOPLEVEL_LANG = verilog
VERILOG_SOURCES = sources/picorv32.v
TOPLEVEL = picorv32
MODULE = test_picorv32
COMPILE_ARGS=-DRISCV_FORMAL

ifneq ($(WAVEFORM),)
VERILOG_SOURCES += waveform.v
COMPILE_ARGS += -s waveform
endif


include $(COCOTB)/makefiles/Makefile.inc
include $(COCOTB)/makefiles/Makefile.sim

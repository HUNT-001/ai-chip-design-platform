TB = testbench_top.sv
PKG_FILES = vscale_mul_div_pkg.sv

run_cds: cds_batchmode

# ----------------------------------------------------
# Xrun Cadence
# ----------------------------------------------------

CDS_XRUN 			= xrun
CDS_DATABASE  = -sv -64 -access rwc
CDS_INCDIR    = -incdir ../design/  
CDS_DEFINES   = # options: -define DEF_DUMMY -timescale 1ns/100ps
CDS_DEBUG     = # options: -debug -linedebug -classlinedebug
CDS_UVM_FLAGS = -uvm +UVM_NO_RELNOTES +ELABORATION_PHASE +UVM_TESTNAME=muldiv_test_3 # options: -uvm +UVM_NO_RELNOTES +ELABORATION_PHASE +UVM_TESTNAME=muldiv_test_1, muldiv_test_2, muldiv_test_3
CDS_UVM_DEBUG = # options: -uvmlinedebug, +UVM_PHASE_TRACE, +UVM_OBJECTION_TRACE,
CDS_COV_FLAGS = # options:
CDS_FILES			= -f rtl.f

cds_batchmode:
	$(CDS_XRUN) $(CDS_DATABASE) $(CDS_INCDIR) $(CDS_INCLUDE) $(CDS_DEFINES) $(CDS_DEBUG) $(CDS_UVM_FLAGS) $(CDS_UVM_DEBUG) $(CDS_COV_FLAGS) $(CDS_FILES) $(PKG_FILES) $(TB)

cds_clean:
	rm -rf xcelium.d
	rm -rf xrun.*
	rm -rf .simvision
	rm -rf xmsim*
	rm -rf cov_work
	rm -rf *.vcd
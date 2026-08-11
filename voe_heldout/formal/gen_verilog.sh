#!/usr/bin/env bash
# Convert the held-out DUT (real pulp lfsr_8bit + parameter wrapper) to plain
# Verilog for yosys. The assertion harness lfsr_fv.v is NOT converted — sv2v
# strips assert/assume, which would make every proof vacuous.
set -e
cd "$(dirname "$0")"
mkdir -p gen
RTL=../rtl

sv2v "$RTL/lfsr_8bit.sv"      "$RTL/lfsr_wrap.sv"      > gen/lfsr.v
sv2v "$RTL/lfsr_8bit_mut.sv"  "$RTL/lfsr_wrap_mut.sv"  > gen/lfsr_mut.v
sv2v "$RTL/mv_filter.sv"      "$RTL/mvf_wrap.sv"       > gen/mvf.v
sv2v "$RTL/mv_filter_mut.sv"  "$RTL/mvf_wrap_mut.sv"   > gen/mvf_mut.v

echo "generated: gen/lfsr.v gen/lfsr_mut.v gen/mvf.v gen/mvf_mut.v"

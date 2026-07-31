#!/usr/bin/env bash
# Convert ONLY the DUT (byte-identical ibex_alu + its package) to plain Verilog
# that yosys's built-in reader accepts. The assertion harness (ibex_alu_fv.v) is
# read directly by yosys and is NOT passed through sv2v — sv2v strips assertions
# (it targets synthesizable Verilog), which would make every proof vacuous.
# Run once after installing sv2v (https://github.com/zachjs/sv2v/releases):
set -e
cd "$(dirname "$0")"
mkdir -p gen
RTL=../rtl

sv2v "$RTL/ibex_pkg.sv" "$RTL/ibex_alu.sv"     > gen/ibex_alu.v
sv2v "$RTL/ibex_pkg.sv" "$RTL/ibex_alu_mut.sv" > gen/ibex_alu_mut.v

echo "generated: gen/ibex_alu.v gen/ibex_alu_mut.v"

#!/usr/bin/env bash
# Convert the multiplier DUT (both variants + wrappers, one file) to plain
# Verilog for yosys. The assertion harness mul_fv.v is NOT converted — sv2v
# strips assert/assume.
set -e
cd "$(dirname "$0")"
mkdir -p gen
sv2v ../rtl/mul_dut.sv > gen/mul.v
echo "generated: gen/mul.v"

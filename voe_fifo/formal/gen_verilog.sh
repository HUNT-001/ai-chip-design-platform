#!/usr/bin/env bash
# Convert the DUT (real cv32e40p_fifo + parameter-fixing wrapper) to plain
# Verilog for yosys. The assertion harness fifo_fv.v is NOT passed through sv2v
# — sv2v emits synthesizable Verilog and strips assert/assume, which would make
# every proof vacuous.
set -e
cd "$(dirname "$0")"
mkdir -p gen
RTL=../rtl

sv2v "$RTL/cv32e40p_fifo.sv"     "$RTL/fifo_wrap.sv"     > gen/fifo.v
sv2v "$RTL/cv32e40p_fifo_mut.sv" "$RTL/fifo_wrap_mut.sv" > gen/fifo_mut.v

echo "generated: gen/fifo.v gen/fifo_mut.v"

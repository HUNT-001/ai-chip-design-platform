#!/usr/bin/env bash
# Run each validation ELF on the Verilator simple_system built by build_ibex.sh.
# Run from the repo root.  Each program runs in its own directory, because the
# tracer always writes trace_core_00000000.log into the working directory.
set -euo pipefail
OUT=${OUT:-work/ibex_val}
SIM=$(pwd)/work/ibex/build/lowrisc_ibex_ibex_simple_system_0/sim-verilator/Vibex_simple_system
for name in arith_branch muldiv loadstore csr; do
  d="$OUT/$name.ibex"
  rm -rf "$d"; mkdir -p "$d"
  elf=$(realpath "$OUT/$name.elf")
  set +e
  (cd "$d" && timeout 120 "$SIM" --term-after-cycles=100000 --meminit=ram,"$elf" > sim.stdout 2>&1)
  rc=$?
  set -e
  cp "$d/trace_core_00000000.log" "$OUT/$name.ibex.log"
  echo "$name ibex rc=$rc trace_lines=$(wc -l < "$OUT/$name.ibex.log") halted=$(grep -c 'Terminating simulation by software request' "$d/sim.stdout")"
done

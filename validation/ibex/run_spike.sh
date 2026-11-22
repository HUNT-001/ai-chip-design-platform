#!/usr/bin/env bash
# Run each validation ELF on Spike with the memory map of Ibex simple_system.
# Run from the repo root after build_programs.sh.  Spike writes the commit log
# to stderr; stdout is kept separately so a non-empty one is noticed.
set -euo pipefail
OUT=${OUT:-work/ibex_val}
SPIKE=${SPIKE:-spike}
for name in arith_branch muldiv loadstore csr; do
  set +e
  timeout 60 "$SPIKE" --isa=rv32imc --priv=m -m0x20000:0x1000,0x100000:0x100000 \
    --pc=0x100080 --log-commits "$OUT/$name.elf" \
    2> "$OUT/$name.spike.log" > "$OUT/$name.spike.stdout"
  rc=$?
  set -e
  echo "$name spike rc=$rc lines=$(wc -l < "$OUT/$name.spike.log") stdout_bytes=$(wc -c < "$OUT/$name.spike.stdout")"
done

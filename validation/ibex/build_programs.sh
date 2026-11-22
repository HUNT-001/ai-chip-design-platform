#!/usr/bin/env bash
# Assemble the Ibex validation programs.  Run from the repo root.
# Output: work/ibex_val/<name>.elf (gitignored).
set -euo pipefail
SRC=validation/ibex/programs
OUT=${OUT:-work/ibex_val}
mkdir -p "$OUT"
for name in arith_branch muldiv loadstore csr; do
  riscv64-unknown-elf-gcc -march=rv32im_zicsr -mabi=ilp32 -nostdlib -static \
    -I "$SRC" -T "$SRC/link.ld" "$SRC/$name.S" -o "$OUT/$name.elf"
  echo "$OUT/$name.elf"
done

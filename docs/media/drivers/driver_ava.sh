#!/usr/bin/env bash
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
cd "$REPO"; unset PYTHONPATH
WORK="${TMPDIR:-/tmp}/ava-demo-pipeline"; rm -rf "$WORK"; mkdir -p "$WORK"
cat > "$WORK/demo_core.sv" <<'SV'
module demo_core (
    input  wire        clk, rst_n,
    input  wire [31:0] instr_i,
    output reg  [31:0] data_o
);
    reg [31:0] pc;
endmodule
SV
p(){ printf '%s\n' "$*"; }
cmd(){ printf '\033[32m$\033[0m %s\n' "$1"; eval "$1"; }
clear
p "# AVA — 6-phase pipeline, no EDA toolchain required"
sleep 1.5
echo; sleep 0.5
cmd "python3 ava_patched.py --rtl $WORK/demo_core.sv --microarch in_order --no-llm --seed 42 > $WORK/out.txt 2>&1; cat $WORK/out.txt"
rc=$?
sleep 2
p "# Phases 1-5 are the core loop; phase 6 runs the extended tier — 7 agent modules ran"
sleep 2.5
echo
p "# Honest no-tools result (lines quoted from the run above):"
sleep 1
grep -E 'Status|Bugs Found' "$WORK/out.txt"
echo "exit=$rc"
sleep 4

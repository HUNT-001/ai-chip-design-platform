#!/usr/bin/env bash
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
cd "$REPO/voe_bench"; unset PYTHONPATH
p(){ printf '%s\n' "$*"; }
cmd(){ printf '\033[32m$\033[0m %s\n' "$1"; eval "$1"; }
clear
p "# VOE commits its criteria BEFORE collecting data, and checks the board"
p "# can exhibit the effect at all. --mock gives a degenerate board on purpose."
sleep 2.5
p "# NOT EVALUABLE is a third verdict, not a weak NOT MET:"
sleep 1.5
p "#   NOT MET       -> the effect is absent or too small"
sleep 1.5
p "#   NOT EVALUABLE -> the question was never asked"
sleep 2.5
echo; sleep 0.5
cmd "python3 run_coupled_noise.py --mock --seeds 8"
sleep 4

#!/usr/bin/env bash
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
cd "$REPO"; unset PYTHONPATH
p(){ printf '%s\n' "$*"; }
cmd(){ printf '\033[32m$\033[0m %s\n' "$1"; eval "$1"; }
clear
p "# AGENT_D is the sole arbiter of pass/fail — what it cannot read is a silent PASS."
sleep 2
p "# One corrupted store, compared field-by-field:"
sleep 1.5
cmd "python3 $HERE/cmp_snippet.py"
sleep 2.5
p "# Before the fix for #1 this printed 0 — a hard PASS on corrupted store data."
sleep 2.5
echo
p "# The canonical-schema regression suite, via the repo's own runner:"
sleep 1.5
cmd "bash run_tests.sh -q AGENT_D/test_comparator.py"
sleep 3

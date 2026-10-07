#!/usr/bin/env bash
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
cd "$REPO"; unset PYTHONPATH
SAMP="${TMPDIR:-/tmp}/ava-demo-verdict"; rm -rf "$SAMP"; mkdir -p "$SAMP"
p(){ printf '%s\n' "$*"; }
cmd(){ printf '\033[32m$\033[0m %s\n' "$1"; eval "$1"; }
python3 AGENT_D/compare_commitlogs.py --generate-sample-logs --sample-dir "$SAMP" >/dev/null 2>&1
clear
p "# AGENT_D — the commit-log comparator (compare_commitlogs.py)."
sleep 1.5
p "# Its EXIT_MISMATCH = 1 is the signal that feeds the orchestrator verdict (#44)."
sleep 2
p "# This is the comparator on its own shipped sample logs — the component alone."
sleep 2
echo; sleep 0.5
p "# 1) RTL vs ISS with a real store-address divergence:"
sleep 1.5
cmd "python3 AGENT_D/compare_commitlogs.py $SAMP/rtl_fail_mem.commitlog.jsonl $SAMP/iss_fail_mem.commitlog.jsonl"
rc_fail=$?
sleep 2.5
p "# One MEMMISMATCH found -> exit 1. The nonzero exit is the verdict signal."
sleep 2.5
echo
p "# 2) The matching clean pair:"
sleep 1.5
cmd "python3 AGENT_D/compare_commitlogs.py $SAMP/rtl_pass.commitlog.jsonl $SAMP/iss_pass.commitlog.jsonl"
rc_pass=$?
sleep 2.5
p "# No mismatches -> exit 0. Same comparator, opposite verdict."
sleep 2.5
echo
p "# Verdict signal from both runs (captured exit codes):"
sleep 1.5
printf 'fail → exit %s\npass → exit %s\n' "$rc_fail" "$rc_pass"
sleep 4

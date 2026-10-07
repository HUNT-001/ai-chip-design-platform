# vhs script — the comparator's verdict integrity, on its own sample logs.
#
# Shows AGENT_D (compare_commitlogs.py) returning EXIT_MISMATCH = 1 on a real
# RTL/ISS divergence and 0 on a clean pair. This is the component whose exit
# code feeds the orchestrator's run verdict (#44); it is the honest view of the
# verdict-integrity behaviour, NOT a claim that a whole pipeline run failed.
#
# Self-contained: the sample logs are generated fresh with --generate-sample-logs,
# so this regenerates from a clean clone with no EDA tools installed.
#
#   vhs verdict-exit-code.tape   ->   verdict-exit-code.gif

Output "verdict-exit-code.gif"

Set Shell bash
Set FontSize 14
Set Width 1280
Set Height 860
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 20

Hide
Type "cd ../.." Enter
Type "unset PYTHONPATH" Enter
Type "python3 AGENT_D/compare_commitlogs.py --generate-sample-logs --sample-dir /tmp/verdict_samples" Enter
Type "clear" Enter
Show

Type "# AGENT_D — the commit-log comparator (compare_commitlogs.py)."
Sleep 1500ms
Enter
Type "# Its EXIT_MISMATCH = 1 is the signal that feeds the orchestrator verdict (#44)."
Sleep 2s
Enter
Type "# This is the comparator on its own shipped sample logs — the component alone."
Sleep 2s
Enter
Enter
Sleep 500ms

Type "# 1) RTL vs ISS with a real store-address divergence:"
Sleep 1500ms
Enter
Type "python3 AGENT_D/compare_commitlogs.py /tmp/verdict_samples/rtl_fail_mem.commitlog.jsonl /tmp/verdict_samples/iss_fail_mem.commitlog.jsonl"
Sleep 800ms
Enter
Sleep 4s
Type "rc_fail=$?" Enter
Sleep 500ms
Type "# One MEMMISMATCH found -> exit 1. The nonzero exit is the verdict signal."
Sleep 2500ms
Enter
Enter

Type "# 2) The matching clean pair:"
Sleep 1500ms
Enter
Type "python3 AGENT_D/compare_commitlogs.py /tmp/verdict_samples/rtl_pass.commitlog.jsonl /tmp/verdict_samples/iss_pass.commitlog.jsonl"
Sleep 800ms
Enter
Sleep 3s
Type "rc_pass=$?" Enter
Sleep 500ms
Type "# No mismatches -> exit 0. Same comparator, opposite verdict."
Sleep 2500ms
Enter
Enter

Type "# Verdict signal from both runs (captured exit codes):"
Sleep 1500ms
Enter
Type "printf 'fail → exit %s\npass → exit %s\n' $rc_fail $rc_pass"
Sleep 800ms
Enter
Sleep 4s

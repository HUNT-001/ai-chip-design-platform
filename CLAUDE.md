# CLAUDE.md — ai-chip-design-platform

Two things live here. **AVA**: a 12-agent RISC-V RTL verification pipeline
(`AGENT_A`…`AGENT_L`, entry point `ava_patched.py`). **VOE**: a research track
on whether verification agents can be trusted — frozen kernel, pre-registered
experiments, append-only ledger (`voe/`, `voe_bench/`, `phase3/`, `voe_stoch*/`).

The project's recurring enemy is **a control that reports success without having
observed anything** — green tests that agree with the bug, CI that never ran the
tests, a probe that checks a tool exists but not that it works. Assume every
green result is suspect until you have seen it fail when it should.

## Environment

- Run everything from the **repo root** in WSL (`/mnt/e/ai-chip-design-platform`).
  `gh` fails with "no git remotes found" from any other directory.
- Tools: Verilator 5.x, Yosys/SymbiYosys/z3 (OSS CAD Suite), Spike, `riscv64-unknown-elf-gcc`.
- Tests: `./run_tests.sh` — runs exactly what CI runs. Baseline: **~1169 passed**.
  Two `AGENT_E` full-pipeline tests fail locally because this Spike lacks
  `--signature`; that is a known AGENT_E defect, not your change.
- Pytest must use `--import-mode=importlib` (already in `run_tests.sh`).
  `ava_patched.py` exists at root AND in `AGENT_F/`; default import mode lets one shadow the other.

## Git workflow — non-negotiable

- **Never commit or push to `main`.** One branch per issue: `fix/<topic>`, `ci/<topic>`, `docs/<topic>`.
- **Never `git add -A`, `git add .`, or `git add --renormalize .`.** Add exact paths,
  then check `git diff --cached --stat` before every commit. The working tree often
  holds unrelated uncommitted VOE work.
- **Bug fixes are two commits**: (1) a test that FAILS, written from the schema/spec/issue —
  never from the code under test; (2) the fix that turns it green. Verify commit 1 is red.
- PR body starts with `Fixes #N` (on its own line, so squash-merge closes the issue)
  and credits the reporter: `Reported by @user`.
- Before opening a PR: `git diff main...HEAD --stat` must show only the intended files.
- Check which branch you are on before diagnosing "missing" code. Files absent on
  `main` may simply live on an unmerged branch — verify with `git merge-base --is-ancestor`.

## Working an issue

1. **Reproduce it against the tree first.** An issue is a claim, not evidence.
   Grep, run the repro, confirm. If you can't confirm, say so — don't fix a guess.
2. Look for the *class*, not the instance. Most bugs here share a root cause with
   an open issue (#7 name collisions, #9 fixtures-from-implementation, #14 CI blind spots).
   Link them.
3. Fix the minimum. Put scope you discover but don't fix into a new issue.
4. Prefer invariant tests (e.g. "these two fields never contradict") over instance tests.

## Filing a new issue

Title `[Bug] <specific claim>`. Body: exact file:line, a copy-pasteable repro, expected
vs actual, **why existing tests didn't catch it**, suggested fix. Labels: pick from
`silent-failure` (reports success while broken — highest priority), `test-gap`, `ci`,
`tech-debt`, `area: comparator|iss|coverage|schema|infra|portability`, `good first issue`.

## Known traps (each cost a real run — do not repeat)

- **SystemVerilog**: a comment line starting with the word `Verilator` is parsed as a
  pragma and aborts the build. `cross`, `bins`, `sample`, `within` are keywords — not
  variable names. Variables declared inside a `begin` block of an `initial` are static
  (initialised once). You cannot compile RTL reliably in your head — build it.
- **Before any sim grid**, run one direct `verilator --binary` build and one binary; a
  build error surfaces as "positive control FAIL", which misdirects you to the checker.
- `$urandom()`: consecutive calls are correlated. Use one draw per cycle, separated bit lanes.
- `SimChannel` compiles campaign length in via `-DNVEC`; builds are cached by `(variant, nvec)`.
  Testbenches must read `` `NVEC ``, never a private define.
- `except Exception` that logs and continues hides TypeErrors. Degrade only past
  environment errors (`DatabaseError`, `sqlite3.Error`, `OSError`).
- A probe that checks a tool is on PATH does not check it supports the flags you need.
- Pytest skips count as passes in CI. A new suite that skips without tools proves nothing there.

## VOE rules — the science depends on these

- The kernel (VSA v1.0: `Judgment`, `KnowledgeState`, `check_laws`) is **frozen**. Do not edit it.
- **Pre-register before data.** Every experiment commits its criteria via
  `voe/preregistration.py` (`Criteria` or `Commitment`) and prints the digest as INTACT.
  Never edit a committed `commit_*.json` or `prereg_*.json`; write a new one.
- **Never tune the instrument toward the hypothesis.** Do not change seeds, `nvec`,
  budgets, grids or thresholds after seeing results. Budgets come from theory (e.g.
  `voe_stoch3/theory.py`), not from observed rates. If a commitment was wrong, record
  the failure and re-register with **fresh seeds**.
- Verdicts: MET / NOT MET / UNDERPOWERED / **NOT EVALUABLE** (the environment cannot
  exhibit the effect — not a weak NOT MET). Report what the committed rule says.
- `voe_bench/capability_ledger.json` is **append-only**. Failures and rejections stay.
- `tests/test_failure_memory.py` records every incident that fooled us. Add a numbered
  `# INCIDENT N` block + test for each new one. Numbering must stay gap-free
  (`test_incident_numbering_has_no_gaps` enforces this). Never regenerate the file.
- Never claim "this proves the kernel is correct". Say what was observed, and its limits.
- H (posterior/belief machinery) stays **closed** until the Stochastic Generalization Gate
  passes: two structurally distinct boards, memoryless law fits one and is excluded on the other.

## Current state (update as it changes)

- Stochastic boards: `voe_stoch` (sat_mac, memoryless), `voe_stoch2` (pfifo, admitted
  but same regime — gate NOT MET), `voe_stoch3` (dfifo, reachability-limited, ADMITTED
  under `commit_dfifo_s1b.json`). **Next: S2** — cross-board shape comparison, write the
  pre-registration before running anything.
- Open AVA work: see `gh issue list`. #23 (manifest lost updates) is `silent-failure`;
  AGENT_E `probe_spike` checks existence not `--signature` support (file if not yet filed).

## How to talk to the maintainer

Be concise. Lead with what you verified vs. what you assumed. When you were wrong,
say so plainly and fix it. Don't pad.

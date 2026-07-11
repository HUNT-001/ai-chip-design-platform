# Design & Build Plan — T52 Out-of-Order Scoreboard Checker

**Status:** Implemented & tested (AVA v2.39.0, 2026-07-09)
**Module:** `AGENT_H/ooo_verifier.py`

---

## 1. Why this level

The `pipeline_verifier` models an **in-order** pipeline (forwarding, stalls,
hazards). A real high-performance core executes **out of order** — a scoreboard
/ Tomasulo engine issues instructions as their operands become ready, they
execute and complete in any order, and a reorder buffer (ROB) retires them back
in program order. The *functional* result is checked by the tandem-diff; what
OOO adds is a whole class of **scheduling** bugs (retiring out of order, issuing
before an operand is ready, reusing a physical register too early, committing a
mis-speculated instruction) that no in-order model can see. This agent checks
that scheduling discipline.

## 2. The invariants

| Check | Sev | Rule |
|---|---|---|
| `ooo_commit_order` | HIGH | commit cycles are non-decreasing in program order — the ROB retires **in order** |
| `ooo_raw_hazard` | HIGH | an instruction issues **no earlier than** the completion of the newest earlier producer of each source (RAW via wakeup/forwarding; `issue == producer.complete` is legal) |
| `ooo_exec_timing` | HIGH | `issue ≤ complete ≤ commit` for every instruction |
| `ooo_rename` | MED | no two **in-flight** (issue…commit) instructions share a physical destination register — the rename table gives each a private tag |
| `ooo_squash` | MED | a **squashed** (mis-speculated) instruction must never commit |

### 2.1 RAW through the scoreboard

The core OOO property. Walking program order, the agent keeps `last_writer[reg]`
(the newest producer of each architectural register). When an instruction issues,
every source's producer must already have **completed** — otherwise the operand
wasn't ready and the issue read a stale value. Forwarding is honoured: issuing on
the exact cycle the producer completes is legal (`issue < complete` is the
violation, not `issue ≤ complete`).

### 2.2 Rename correctness

Two instructions may map the same architectural register to *different* physical
registers, but a single physical register must not be live for two instructions
at once. The agent groups instructions by `pdst` and flags any pair whose
lifetimes `[issue, commit]` overlap — a rename/free-list bug.

## 3. Metrics (never fail)

Instruction count, **max in-flight** (a proxy for reorder-buffer occupancy /
reorder depth, from an interval sweep of issue/commit events), and mean
issue→commit latency.

## 4. Trace contract (additive on the commit log)

```
{"seq":0, "pc":"0x..", "disasm":"add x5,x6,x7",
 "ooo": {"issue":1, "complete":2, "commit":3,
         "src":["x6","x7"], "dst":"x5", "pdst":40, "squashed":false}}
```

`src`/`dst` are **parsed from the disassembly** when not given, so the agent
works from just `disasm` + cycle stamps. Fields may live in an `ooo` sub-dict or
at top level. `x0` writes are ignored. Clean no-op when no scheduling fields are
present.

## 5. Integration & tests

Wired into `_run_extended_pipeline` as a commit-log verifier (`_ooo`, runs on
`rtl_log`, writes `ooo_report.json` when `ooo_active`). Exported from
`AGENT_H/__init__.py`.
`tests/test_extended_agents.py::TestOOOVerifier` — 7 cases (validated standalone:
14): clean OOO, commit-out-of-order, RAW (+ forwarding-ok), exec-timing, rename
reuse (+ free-then-reuse-ok), squash discipline, reorder metrics, disasm-parsed
sources, no-op/robustness/schema, manifest. All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 6. Limitations / next steps

- **Load/store queue** ordering — memory disambiguation, store→load forwarding,
  and speculative-load replay (links with the memory-model / coherence agents).
- **Value-level rename check** — verify the committed architectural value equals
  the in-order golden result (reuse `pipeline_verifier.alu_eval`) to catch a
  rename that delivered the *wrong* physical register's value.
- **Precise-exception replay** — on a trap, assert all older instructions
  committed and all younger were squashed (needs a fuller ROB-flush trace).
- **WAR/WAW at the physical level** and free-list underflow.

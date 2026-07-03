# Design & Build Plan — T42 Functional Coverage Collector

**Status:** Implemented & tested (AVA v2.21.0, 2026-06-30)
**Module:** `AGENT_H/coverage_collector.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this — it closes the self-evolving loop

T40 built a research-grade RL coverage-closure planner
(`self_evolving_engine`), but it consumed a *hypothetical* `coverage_summary.json`
— there was no agent producing one from a real run. This collector fills that
gap. It computes functional coverage from the commit log and emits exactly the
machine snapshot the planner reads, so the loop becomes real:

```
commit log ─▶ CoverageCollector ─▶ coverage_summary.json ─▶ self_evolving_engine
                                     (covered / holes / weights)   (ranks holes,
                                                                    synthesises
                                                                    constraints)
```

## 2. Coverage model

Bins are `category:key` strings — deliberately the same shape
`self_evolving_engine.constraint_for` already parses, so every hole round-trips
into a usable generation constraint with no glue code.

| Category | Bin | Universe (→ holes) | Weight |
|---|---|---|---|
| register write | `reg:x{1..31}` | 31 | 1 |
| value class | `valclass:{zero,one,neg,pos_small,pos_large,all_ones}` | 6 | 1 |
| **cross (opcode × value-class)** | `cross:{mnem}:{valclass}` | 12 arith × 6 = 72 | 2 |
| branch direction | `branch:{taken,not_taken}` | 2 | 2 |
| privilege mode | `priv:{M,S,U}` | 3 | 3 |
| instruction | `instr:{mnem}` | only if a model lists them | 1 |
| CSR / trap / vtype | `csr:… / trap:… / vtype:…` | observed-only telemetry | — |

**Cross-coverage** (`cross:add:neg`, `cross:sub:all_ones`, …) is the highest
bug-yield category: it asks not just "did we run `add`?" but "did `add` *produce*
a negative / all-ones / zero result?". Bins are derived from each record's
mnemonic × its written-register value class over a default arithmetic set
(`cross_instructions`, model-overridable); the matching stimulus template lets
the self-evolving loop close them too.

**Finite universes give real holes.** Register (x1–x31), value-class, branch
and privilege categories have a known, bounded set of bins, so "uncovered"
means a specific, actionable target. Open-ended categories (which CSRs, which
trap causes, which SEW/LMUL combos appeared) are recorded as telemetry, not as
holes, to avoid a meaningless denominator.

**Importance weights** rank holes for the downstream scheduler: privilege gaps
(weight 3) and branch-direction gaps (2) are chased before ordinary
register-write gaps (1). A `model` can extend the universe (expected-instruction
list, extra bins) and override any weight.

## 3. How each bin is derived (soundly)

- **register write / value class** — from each record's `regs` map (post-state
  writes). `x0` is excluded (not a meaningful destination); ABI names
  (`ra`,`sp`,`gp`,`tp`,`fp`) map to their x-numbers. `classify_value` buckets
  the written value at the detected XLEN (RV32/RV64 auto-detected from any
  value > 32 bits).
- **branch direction** — for a conditional-branch mnemonic, compare the *next*
  record's PC to `pc + size` (2 for `c.*`, else 4): equal ⇒ `not_taken`, else
  `taken`. Coverage-grade (not a correctness claim — that's the branch-predictor
  agent's job).
- **privilege** — from a `priv`/`mode` field when present.
- **CSR / trap / vtype** — recorded as telemetry from `csrs`, `trap.cause`, and
  `vtype`.

Everything is defensive: non-dict records are skipped, unparly values ignored,
and the collector **never fails a run** (`pass` is always `True`) — it is an
observer, not a checker.

## 4. Output

`collect()` returns a schema-v2.1.0 report (coverage %, per-category breakdown,
top-instruction histogram, telemetry) **plus an embedded `coverage_summary`**.
`run_from_manifest` writes `coverage_report.json` (human) and
`coverage_summary.json` (machine — `covered_bins`, `total_bins`, `holes`,
`weights`), the latter read verbatim by the self-evolving planner.

## 5. Integration

Wired into `_run_extended_pipeline` **immediately before** the self-evolving
planner block, so within a single run the collector produces the summary and the
planner consumes it. Records `reports["coverage"]`. Exported from
`AGENT_H/__init__.py`. Standalone:
`python AGENT_H/coverage_collector.py --manifest run_manifest.json`.

## 6. Test coverage

`tests/test_agents.py::TestCoverageCollector` — 13 cases: value classification,
register + value-class bins (with holes), `x0` exclusion, ABI-name mapping,
branch taken/not-taken, privilege coverage + holes, instruction-model holes,
weights, CSR/trap/vtype telemetry, report schema, manifest round-trip, and an
**integration test** that runs the collector output through
`self_evolving_engine.plan_from_coverage` and asserts (a) hole counts match,
(b) high-weight `priv:M` sorts ahead of `reg:x31`, and (c) a hole label
round-trips into a constraint target. All pass (validated standalone: 13 in the
isolated harness, including the cross-agent integration test).

> Build note: stdlib-only and self-contained, validated in isolation; the
> workspace mount truncates recently-grown files so the full in-repo suite runs
> against the real repo. Additive change (new module + lazy pipeline hook + new
> test class), existing agents unaffected.

## 7. Limitations / next steps

- **More cross dimensions** — opcode × *operand* value-class (not just result),
  and instruction-pair sequences (temporal cross), for deeper corner cases.
- **Toggle / FSM / expression coverage** — requires RTL structural signals, not
  just the commit log.
- **Live generator wiring** — feed the planner's chosen constraints back into
  `AGENT_G/causal_engine` and re-collect, iterating the closed loop end-to-end.
- **Coverage merging** across runs (persistent cumulative coverage DB).

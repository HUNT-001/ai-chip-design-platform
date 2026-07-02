# Design & Build Plan — T39 Branch Predictor Verifier

**Status:** Implemented & tested (AVA v2.18.0, 2026-06-30)
**Module:** `AGENT_H/branch_predictor_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this — and the key insight

**Level 7** of the taxonomy, and one most projects skip. The subtlety: a branch
predictor is a *performance* feature, not a correctness one. There is no "right"
prediction — only a right *mechanism*. So the sound thing to verify is not "did
the predictor guess correctly," but:

1. **Recovery** — whatever the predictor guessed, the **committed** instruction
   stream must follow the **actual** branch outcomes. A predictor that
   mis-speculates and fails to squash the wrong path commits a wrong-path
   instruction — a real, architectural bug.
2. **Self-consistency** — if the DUT reports its own prediction hit/miss, that
   flag must match reality.

Everything else the predictor produces is *metrics*, not pass/fail.

---

## 2. The sound checks

| Check | Severity | Catches |
|---|---|---|
| `bp_recovery` | HIGH | committed next-PC ≠ the actual (operand-derived) outcome — taken→target, not-taken→fall-through |
| `bp_hit_flag` | HIGH | DUT-reported `predict.correct` inconsistent with the actual outcome |

`bp_recovery` recomputes taken/not-taken **independently** from the register
operands (`beq/bne/blt/bge/bltu/bgeu/beqz/bnez`, and unconditional `jal/j` which
are always taken), then checks the committed next-PC. It is completely
predictor-agnostic, so it never assumes a particular BHT/BTB design — it only
asserts the architectural invariant. A trap on the next record is excluded
(traps legitimately redirect the PC).

---

## 3. Level-7 metrics (analytics — never fail the run)

- **branches, cond_branches, jumps, taken_rate**
- **prediction accuracy, mispredicts, MPKI** — from the DUT's reported
  `predict.correct` flags.
- **RAS return-prediction accuracy** — a golden **return-address stack** driven
  by the RISC-V call/return hint rules (`jal`/`jalr` push when the link register
  is `x1`/`x5`; `jalr`/`ret` pop) predicts each return target; it is scored
  against the committed return PC.

These are exactly the Level-7 numbers (accuracy, mispredictions, recovery) that
plain tandem diffing never surfaces.

---

## 4. Soundness & gating

- `bp_recovery` fires only when the operands, the branch target, and the next
  PC are all available. The target comes from an explicit `target` field or an
  absolute address in the disassembly (≥ 0x1000 — a small immediate is ignored
  to avoid absolute-vs-relative ambiguity).
- `jalr`/`ret` control transfers are deliberately **not** re-checked here — the
  `pipeline_verifier` already owns the jalr-target control-hazard check; this
  agent adds the *conditional-branch* case plus the RAS and metrics, with no
  overlap.
- Clean no-op on traces with no branch information.

### Optional trace contract (additive)

```
a branch record may carry:
  "target":  "0x.."                                  actual branch target
  "predict": { "taken": bool, "correct": bool, "kind": "bht|btb|ras" }
```

---

## 5. Report & integration

`run()` returns the standard schema v2.1.0 report plus a `metrics` block, band
`CLEAN→CRITICAL`. Wired into `_run_extended_pipeline` (`_branchp` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `branch_predictor_report.json` when branches
are present, records `reports["branch_predictor"]`) and exported from
`AGENT_H/__init__.py`. Standalone:
`python AGENT_H/branch_predictor_verifier.py --rtl rtl_commit.jsonl`.

---

## 6. Test coverage

`tests/test_agents.py::TestBranchPredictorVerifier` — 12 cases: helpers,
recovery (taken clean / taken bug / not-taken bug), `jal` jump-recovery bug,
hit-flag inconsistency, accuracy metric, RAS return-prediction accuracy,
malformed-input robustness, report schema, manifest round-trip. All pass.

> Build note: the new module is fully self-contained (stdlib only), so it was
> validated in isolation; the mount used for the scratch test copy truncates
> previously-edited files, so the *full* 349-case suite is run against the real
> repo rather than the scratch copy. The change is purely additive (new module +
> lazy pipeline hook), so existing agents are unaffected.

---

## 7. Limitations / next steps

- **Predictor-structure modelling** (golden 2-bit BHT / gshare / tournament) can
  be added to *predict* the outcome and compare against the DUT's prediction
  stream — turning accuracy into a mechanism check when the DUT exposes its
  index/history state.
- **Recovery-cycle metric** needs a `mispredict_penalty`/cycle field to report
  the Level-7 "recovery cycles" number.
- **pc-relative targets** are currently only used when given as an absolute
  address; a `target` field (or offset + width) makes every branch checkable.

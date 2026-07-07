# Design & Build Plan — T46 Interrupt Controller Checker

**Status:** Implemented & tested (AVA v2.30.0, 2026-06-30)
**Module:** `AGENT_H/interrupt_verifier.py`

---

## 1. Why this level

Interrupt handling is a top source of severe, hard-to-reproduce silicon bugs: a
mis-prioritised claim, an interrupt that fires while masked, a timer that misses
its compare. None of this is covered by the trap/privilege agents — those check
the CSR side of an *already-delivered* trap (mcause, mepc, delegation), not the
controller that decides **which** interrupt is delivered. This agent adds the
controller level: **PLIC** (external interrupts) and **CLINT** (timer/software).

## 2. PLIC — golden priority arbitration

`PLICModel` holds per-source priority, per-context enable sets, per-context
threshold, and the pending set. The core semantics, checked on every `claim`:

- **claim = argmax priority** over sources that are pending ∧ enabled for the
  context ∧ `priority > threshold` ∧ `priority > 0`; ties broken to the
  **lowest source id**; `0` when nothing qualifies. (`plic_claim_wrong`)
- a claimed source at/below threshold (`plic_threshold`) or at priority 0
  (`plic_priority0`) is an explicit second signal.
- **claim clears pending** — the golden model removes the claimed source, so the
  *next* claim must return the next-highest source; a DUT that re-claims a
  serviced interrupt diverges from the model.

All checks are HIGH. The model progresses on the golden decision, so one wrong
claim doesn't cascade into spurious follow-on errors.

## 3. CLINT — timer & software interrupts

- **MTIP** (`clint_mtip`): pending iff `mtime >= mtimecmp` — the single most
  common timer bug (off-by-one at the compare boundary is caught: `mtime==cmp`
  must be pending).
- **MSIP** (`clint_msip`): equals the written software-interrupt bit.

## 4. Trace contract (additive, separate stream)

```
interrupt_trace.jsonl (in order):
  {"op":"config","priorities":{"3":7},"enables":{"0":[3,5]},"thresholds":{"0":2}}
  {"op":"pending","source":3}
  {"op":"claim","context":0,"result":3}      # DUT's claimed id (0 = none)
  {"op":"complete","context":0,"source":3}
  {"op":"clint","mtime":100,"mtimecmp":100,"mtip":true,"msip":false,"expected_msip":false}
```

Config is cumulative; keys may be ints or JSON strings (normalised). Clean no-op
on an empty / absent trace.

## 5. Integration & tests

Wired into `_run_extended_pipeline` (`_interrupt` import, `run_from_manifest` →
`interrupt_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/test_agents.py::TestInterruptVerifier` — 9 cases (validated standalone:
12): highest-priority claim + wrong-claim, threshold + priority-0, tie-break +
claim-clears-pending, disabled + no-pending, CLINT mtip/msip, robustness/schema,
manifest. All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 6. Limitations / next steps

- **Nested/preemptive interrupts** — priority-based preemption while an
  interrupt is in service (claim before complete of a higher-priority source).
- **Multi-context / multi-hart** PLIC gateways and per-context claim registers
  (the model supports multiple contexts; add cross-context fairness).
- **Edge vs. level** gateways and the complete→re-arm interaction for level
  sources.
- **CLINT `mtime` progression** and interrupt-taken latency bounds.

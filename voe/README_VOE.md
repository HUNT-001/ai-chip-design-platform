# Phase 2 — Verification Operating Environment (VOE)

Phase 2 of the roadmap: the **society layer** on the FROZEN VSA kernel. Multiple
heterogeneous engineers share one canonical knowledge state, compete for
attention under a finite budget, contribute evidence through a typed judgment
bus, and earn reputation computed *only* from what their evidence actually did.

Nothing here extends the kernel. Every OS-grade behaviour is a consequence of a
kernel law (see `docs/VSA_KERNEL_FREEZE_AND_ROADMAP.md`):

| VOE mechanism | Kernel basis |
|---|---|
| Judgment bus, single canonical state, order-independent merge | Struct-1 confluence |
| Nothing enters sign-off without a witness | Wit-1 + Fire-1 (generation/adjudication firewall) |
| A counterexample settles a property over prior passes | Sem-1 formal dominance |
| Scheduler attention = argmax risk-reduction / cost | Utility 𝒰 |
| Reputation from provenance + calibration | typed-judgment ownership `E ⊢ K` |

## Files

| File | Role |
|---|---|
| `board.py` | `TaskBoard` of obligations + `ResourceLedger` (budget, per-action cost). |
| `bus.py` | `JudgmentBus` — Fire-1 witness gate, confluent merge with warrant precedence, miscalibration detection. |
| `reputation.py` | `ReputationService` — reputation from risk discharged, proofs/bugs, calibration, value-per-cost. |
| `workers.py` | `Worker` + archetypes: `skeptic` (formal-first) and `explorer` (sim-first). Heterogeneous cognition, same kernel. |
| `voe.py` | `VOE` orchestrator — the scheduler loop. |
| `run_voe.py` | Two-engineer demo on a shared board. |

Reuses the Phase-3 evidence channels and frozen-kernel importer unchanged.

## Run

```bash
cd voe
python3 run_voe.py --mock     # no toolchain (mechanics + laws + reputation)
python3 run_voe.py --real     # real verilator + sby evidence
```

## What the demo shows

```
explorer sims the clean + buggy DUTs (cheap, high early value density)
skeptic proves the clean property (deductive) and finds the bug (counterexample)
the counterexample refutes the explorer's earlier 'pass'  -> miscalibration
reputation (evidence only): skeptic 1.000 (proof+bug) , explorer ~0.21 (no discharge, 1 miscal)
final R = 0 ; all laws hold every step ; kernel unchanged
```

This is the first real test of the society layer: two cognitive styles, one
shared truth, trust that is *earned from evidence* rather than declared. The
same result that Phase 3 proved for one engineer now holds when several
engineers — with different strategies and different reliability — contribute to
the same canonical state without corrupting it.

## Next (Phase 4+)

Specialised engineers (property-class + semantic-memory ownership), leads and
review authority (witness gate), self-improving strategies (`Γ`/`M_p`
extensions) — each plugging into the frozen kernel. Scaling the board to real
Ibex properties (per-op obligations over `corpus/ibex_rtl/ibex_alu.sv`) is the
same config change described in `phase3/README_PHASE3.md`.

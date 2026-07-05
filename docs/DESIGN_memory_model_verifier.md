# Design & Build Plan — T45 Memory-Consistency Checker

**Status:** Implemented & tested (AVA v2.27.0, 2026-06-30)
**Module:** `AGENT_H/memory_model_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this is the most impactful level

Cache coherence (T44) governs a *single* memory location. **Memory
consistency** governs the ordering of accesses *across* locations — whether one
core's stores can be observed out of order by another, whether a load can be
hoisted past a store, whether a fence is actually enforced. These are the
hardest multicore bugs to find (they need specific interleavings) and the most
severe (they silently break locks, message-passing, and every concurrency
primitive). This agent adds that level, and it sits exactly one layer above the
coherence checker.

## 2. Method — axiomatic ("herd"-style) verification

An observed execution is described by relations over its memory operations, and
the model *permits* the execution iff certain relations are **acyclic**. This is
the standard formal approach (Alglave/Maranget `herd`, the RISC-V RVWMO
appendix), reduced to graph cycle detection:

| Relation | Meaning |
|---|---|
| `po` | program order within a core |
| `ppo` | *preserved* program order — the po pairs the model keeps ordered |
| `rf` / `rfe` | reads-from (load ← store it observed) / external rf |
| `co` | coherence order — the total order of stores to each address |
| `fr` | from-read: `r ─fr→ w'` when `r` read a store `co`-before `w'` |
| `fence` | orders every earlier op before every later op in that core |

**Two axioms:**

```
sc-per-location :  acyclic( po-loc ∪ rf ∪ co ∪ fr )        (coherence)
global order    :  acyclic( ppo ∪ fence ∪ rfe ∪ co ∪ fr )  (the model)
```

A **cycle means the hardware produced an ordering the model forbids** — a real
consistency violation — reported HIGH with the cycle as a human-readable witness
(e.g. `c0:Wx=0x1 → c0:Wy=0x1 → c1:Ry=0x1 → c1:Rx=0x0 → …`).

### 2.1 Where the models differ — `ppo`

- **SC** — all of `po` is preserved (nothing reorders).
- **TSO** — all of `po` *except* store→load (the store-buffer relaxation, x86).
- **RVWMO** — only same-address pairs or a syntactic **dependency** are
  preserved; any other ordering must be supplied by a **fence**.

The pairwise `ppo` rule is applied over *all* `i<j` pairs in a core (not just
adjacent), which is what makes it sound: e.g. under TSO a `store→…→load` chain is
correctly left unordered because the pair itself is store→load.

## 3. Soundness — validated on the canonical litmus tests

The whole point of an axiomatic checker is that it must reproduce the textbook
results exactly. It does (these are unit tests):

| Litmus | SC | TSO | RVWMO |
|---|---|---|---|
| **SB** (store buffering) | forbidden | **allowed** | allowed |
| **SB + fences** | forbidden | forbidden | forbidden |
| **MP** (message passing) | forbidden | forbidden | **allowed** (no fence) |
| **MP + fences** | forbidden | forbidden | forbidden |
| **LB** (load buffering) | forbidden | forbidden | **allowed** (no dep) |
| **CoRR** (coherence) | forbidden | forbidden | forbidden (sc-per-loc) |

A subtle case that's also tested: an RVWMO MP with a load→load *dependency* but
the two stores left unordered is still **allowed** — you need to order *both*
sides. The checker gets this right (it's a common source of confusion), which is
strong evidence the relation construction is correct.

## 4. Deriving `co` and `rf`

- **`co`** (coherence order per address): from an explicit per-store `co` rank,
  else the global `cycle` stamp, else appearance order.
- **`rf`**: explicit `rf` index if given, else inferred by matching the load's
  value to the `co`-latest store of that value (unmatched → reads the initial
  value). Litmus values are distinct per store, so inference is exact; a DUT
  trace should stamp `cycle` (visibility order) and, where values repeat, an
  explicit `rf`.

These assumptions are documented so a trace can be produced correctly; nothing
is guessed silently.

## 5. Integration

Wired into `_run_extended_pipeline` (`_memmodel` import,
`EXTENDED_AGENTS_AVAILABLE`); `run_from_manifest` reads
`outputs.consistency_trace` and `memory_model` (default `tso`), writes
`memory_model_report.json`. Exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/memory_model_verifier.py --manifest run_manifest.json`.

## 6. Test coverage

`tests/test_agents.py::TestMemoryModelVerifier` — 12 cases: SB (TSO-allowed /
SC-forbidden / fenced-forbidden), MP (TSO-forbidden / RVWMO-allowed /
fenced-forbidden), LB (TSO-forbidden / RVWMO-allowed), CoRR coherence, cycle
witness, model normalisation + single-core no-op, robustness, schema, manifest.
All pass (validated standalone: 17 in the isolated harness including the RVWMO
dependency-vs-fence subtleties).

> Build note: stdlib-only and self-contained, validated in isolation; the
> workspace mount truncates recently-grown files so the full in-repo suite runs
> against the real repo. Additive change (new module + lazy pipeline hook + new
> test class), existing agents unaffected.

## 7. Relationship to `AGENT_I` litmus & `coherence_verifier`

- `coherence_verifier` (T44) checks per-location coherence from a value trace;
  this agent generalises to cross-location ordering and *subsumes* coherence via
  the sc-per-location axiom (useful as an independent cross-check).
- `AGENT_I` litmus *generates/runs* litmus programs; this agent *decides* whether
  an arbitrary observed execution is permitted by a model. They compose: run a
  litmus test → feed its execution here to classify the outcome.

## 7b. RVWMO enrichment (v2.28.0)

The RISC-V synchronization mechanisms real concurrent code relies on are now
modelled:

- **Acquire / release** (`.aq` / `.rl`, RCsc): an acquire load is ordered before
  every later op in po; every earlier op is ordered before a release store.
  These edges join the global-order axiom, so a release+acquire pair restores an
  ordering weak RVWMO would otherwise drop. Validated: release+acquire MP is
  forbidden, but a release *without* the matching acquire is insufficient.
- **Fence predecessor/successor sets** (`FENCE pr,pw,sr,sw`): a fence orders an
  earlier access only if its type (`r`/`w`) is in the predecessor set and a
  later access only if in the successor set. So `FENCE r,r` leaves a store→load
  relaxed (SB still allowed) while `FENCE rw,rw` orders it. Default is full
  `rw,rw`.
- **RMW atomicity axiom** (third axiom): ops sharing an `rmw` group id (an
  LR/SC pair or an AMO's read+write) must have *no* store coherence-interposed
  between the atomic's read and its write. An interposing store breaks atomicity
  and is flagged HIGH with a `load → interposed store(s) → write` witness.

## 8. Limitations / next steps

- **Dependency taxonomy** — distinguish address / data / control dependencies
  (currently a single generic `deps` relation) and the `Ztso` overlay.
- **Automatic `co` recovery** from final memory state when `cycle` is absent.
- **Consistency coverage** — feed observed litmus outcomes into the coverage
  loop (which shapes / fences / aq-rl / RMW patterns have been exercised),
  mirroring the coherence coverage work.

# Design & Build Plan — T53 Load/Store Queue Checker

**Status:** Implemented & tested (AVA v2.40.0, 2026-07-09)
**Module:** `AGENT_H/lsq_verifier.py`

---

## 1. Why this level

An out-of-order core lets loads and stores execute out of program order, held in
a **load queue** and **store queue**. Two mechanisms keep this correct within a
core: **store-to-load forwarding** (a load reads the value of an older in-flight
store to the same address, before it reaches cache) and **memory
disambiguation** (a speculatively-executed load must not bypass an older store
to the same address). Bugs here are among the nastiest in silicon — they
manifest only on specific address/timing collisions. This is distinct from the
coherence/consistency agents, which govern ordering *across cores*; the LSQ is
*within* one core.

The agent rides the **standard commit log**: `mem_reads` are loads, `mem_writes`
are stores — no separate trace required.

## 2. The invariant

Sequential semantics require a load to observe the **youngest program-order-older
store to the same address**, or memory if none:

- **`lsq_forward`** (HIGH) — when a load's address has an earlier store in the
  trace, the load's value must equal that youngest store's value. One check
  catches three bug shapes: missing forwarding (load read stale memory),
  forwarding from the *wrong* store, and a load that bypassed an older store it
  should have observed (a memory-ordering / disambiguation violation).
- **`lsq_store_order`** (HIGH, when commit cycles are present) — stores to the
  same address drain to memory in program order (the store queue commits in
  order).

## 3. Soundness — no false positives

The subtle part: a load whose address has **no earlier in-trace store** is
*skipped*. Its value comes from initial memory (or a store outside the trace
window) that the agent doesn't model, so asserting it would be a false positive.
Only forwarding cases with ground truth — an older store to the same address is
present — are checked. Tests pin this (a lone load, and a load to a different
address, both pass).

## 4. Trace contract

The commit log (loads via `mem_reads`, stores via `mem_writes`), or a simplified
per-op stream:

```
{"seq":0, "op":"store", "addr":"0x40", "value":"0x5"}
{"seq":1, "op":"load",  "addr":"0x40", "value":"0x5"}
```

Program order is the `seq` field (else appearance). Store commit cycles (for the
drain-order check) come from an `ooo.commit` / `commit` field. Clean no-op when a
record carries no memory operations.

## 5. Integration & tests

Wired into `_run_extended_pipeline` as a commit-log verifier (`_lsq`, runs on
`rtl_log`, writes `lsq_report.json` when `lsq_active`). Exported from
`AGENT_H/__init__.py`.
`tests/test_extended_agents.py::TestLSQVerifier` — 7 cases (validated standalone:
12): forwarding clean / missing / wrong-store, youngest-store-wins, soundness
skips (no prior store / other address), store-drain order, simplified stream +
metrics, no-op/robustness/schema, manifest. All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 6. Limitations / next steps

- **Partial / sub-word forwarding** — a load overlapping a store only partially
  (size/alignment), where forwarding stalls or merges.
- **Speculative-load replay accounting** — verify a disambiguation replay
  actually happened (needs squashed/replayed markers, links with
  `ooo_verifier`).
- **Un-forwardable cases** — stores with unresolved addresses that must block
  younger loads.
- **Multi-word memory image** — track a full byte-addressable memory to check
  loads with no in-trace store against a known initial image (removes the
  conservative skip).

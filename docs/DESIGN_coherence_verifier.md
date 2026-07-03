# Design & Build Plan — T44 Multicore Cache-Coherence Checker

**Status:** Implemented & tested (AVA v2.24.0, 2026-06-30)
**Module:** `AGENT_H/coherence_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this is a distinct verification level

Every checker before this one reasons about a *single* core's architectural
state. Cache-coherence bugs are fundamentally *cross-core*: a missing
invalidation, a stale read, or two caches believing they own the same line
writable at once. None of these show up in a single-core tandem-diff, because
each core's local trace looks self-consistent — the bug is in how the cores'
views *disagree*. This agent adds that cross-core level.

## 2. Coherence, not consistency

A deliberate scoping decision. **Coherence** is the set of *per-location*
guarantees that essentially every system provides; **consistency** (the memory
model — TSO, RVWMO, …) governs ordering *across* locations and is far more
subtle. This checker targets coherence, which is both universally required and
soundly checkable from a value trace:

1. **read-from-a-real-write** — loads never return fabricated values.
2. **write serialization** — writes to one address form a single total order
   that *all* cores observe consistently.
3. **SWMR** — single writer *or* multiple readers per line, never both.

(Full consistency-model / litmus checking is the sibling `AGENT_I` litmus
level; this agent stays in the coherence lane, no overlap.)

## 3. The checks

| Check | Severity | Catches |
|---|---|---|
| `read_from_valid` | HIGH | a load value that no store produced (fabricated / wrong forwarding) |
| `coherence_read_monotonic` | HIGH | a core observing writes out of the global order (stale read / lost invalidation / cores disagreeing on write order) |
| `swmr` | HIGH | two writable holders, or a writer coexisting with a reader (from MESI state) |

### 3.1 Write serialization via per-core monotonicity

The elegant part. Writes to an address are assigned a **global version** in the
trace's visibility order (`cycle` stamps, else list order). Coherence requires
every core to observe them in that one order. So the checker tracks, per
`(core, address)`, the highest version that core has read-from; if a later load
resolves to an *earlier* version, that core has gone backwards in the write
order — a stale read. This single monotonicity test simultaneously catches
lost-invalidation stale reads *and* two cores disagreeing on the write order
(one of them must go backwards relative to the global order), without building
an explicit constraint graph.

A load's version is resolved by explicit `ver` if present, else by matching the
value to the most-recent store of that value (equal-valued writes are
disambiguated by `ver`; the initial value `0` maps to version −1 when the
address was never written).

### 3.2 SWMR from MESI state

When events carry per-line MESI `state`, the checker maintains each core's
current state per address and, after every state update, asserts the invariant:
a line held M or E by one core may not be held (M/E/S) by any other. Two
exclusive holders, or an exclusive holder plus a sharer, is flagged. Gated on
the presence of `state`, so a value-only trace still gets checks 1–2.

## 4. Soundness & the visibility-order assumption

The one assumption is that the trace's `cycle` order is the **global-visibility**
order of writes (the order they become coherence-visible), which *is* the
coherence order for a commit/retire trace. This is stated explicitly so a
relaxed-visibility model can be fed correctly by stamping visibility rather than
issue time. Everything else is defensive: non-dict events skipped, unparseable
fields ignored, single-core traces a clean no-op, `pass` false only on a real
HIGH violation.

## 5. Trace contract (additive, separate stream)

```
coherence_trace.jsonl — one event per line (or a JSON array):
  {"core":0, "op":"load"|"store", "addr":"0x40", "value":"0x7",
   "cycle":12,               # global-visibility order (optional)
   "state":"M"|"E"|"S"|"I",  # per-line MESI state after the op (optional)
   "ver":3}                  # explicit write-id (optional)
```

## 6. Integration

Wired into `_run_extended_pipeline` (`_coherence` import,
`EXTENDED_AGENTS_AVAILABLE`); `run_from_manifest` reads
`outputs.coherence_trace` (JSONL or JSON array) and writes
`coherence_report.json` when a trace is present. Exported from
`AGENT_H/__init__.py`. Standalone:
`python AGENT_H/coherence_verifier.py --manifest run_manifest.json`.

## 7. Test coverage

`tests/test_agents.py::TestCoherenceVerifier` — 18 cases: clean producer-
consumer, fabricated value, initial-value ok / unwritten-nonzero flagged, stale
read, cross-core write-order disagreement, explicit-`ver` disambiguation, SWMR
two-writers / writer-with-reader / clean-after-invalidation / multiple-sharers,
cycle reordering, single-core no-op, robustness, report schema, manifest
round-trip. All pass (validated standalone: 18 in the isolated harness).

> Build note: stdlib-only and self-contained, validated in isolation; the
> workspace mount truncates recently-grown files so the full in-repo suite runs
> against the real repo. Additive change (new module + lazy pipeline hook + new
> test class), existing agents unaffected.

## 8. Limitations / next steps

- **Full MESI/MOESI directory model** — replay bus transactions
  (BusRd/BusRdX/invalidate) against a golden directory to check *protocol
  transitions*, not just the SWMR end-state.
- **Consistency-model checking** — hand off to / extend `AGENT_I` litmus for
  TSO / RVWMO fences, `sc`/`lr` global ordering.
- **Atomic RMW coherence** — `amo*`/`lr`/`sc` participation in the per-address
  order (link with `atomics_verifier`).
- **Livelock / fairness** — detect starvation in the coherence arbitration from
  long traces.

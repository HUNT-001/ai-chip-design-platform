# Design & Build Plan — T33 Cache Subsystem Verifier

**Status:** Implemented & tested (AVA v2.11.0, 2026-06-29)
**Module:** `AGENT_H/cache_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

**Level 3**, **priority #6** — and, as the taxonomy notes, the point where "many
projects stop." Replacement-policy and dirty-eviction bugs are insidious: they
silently drop or duplicate data with no architectural exception, so they slip
past instruction-level checking. A golden cache model that predicts every
hit/miss, victim, and write-back is the right tool, and it produces the Level-16
cache miss-rate metric for free.

---

## 2. The novel core — a golden set-associative cache

`CacheModel` is a precise, configurable software cache:

- **Geometry**: `sets` × `ways`, `line_size` bytes (power of two). Address is
  split into offset / index / tag exactly as hardware does.
- **Replacement**: `lru` (per-way timestamp, updated on hit) or `fifo`
  (insertion order). Invalid ways are filled first; otherwise the policy chooses
  the victim.
- **Write policy**: `wb` (write-back — writes set the dirty bit; a dirty victim
  triggers a write-back on eviction) or `wt` (write-through — every write is a
  bus write, lines never dirty).
- **Output** per access: `hit`, set index, way, **victim address** (whenever a
  valid line is replaced — clean or dirty), and **write-back** (only when the
  victim was dirty).

The model is unit-tested against hand-traced sequences with hand-computed
results (resident hit, LRU victim selection, dirty-eviction write-back) so the
golden reference is proven before it judges the DUT.

---

## 3. The checker

`CacheVerifier` replays the memory-access stream through `CacheModel` and
compares against the DUT-reported cache event on each access:

| Check | Severity | Catches |
|---|---|---|
| `cache_hitmiss` | HIGH | reported hit/miss ≠ golden model |
| `cache_eviction` | HIGH | wrong replacement victim (policy violation) |
| `cache_writeback` | HIGH | dirty eviction without a write-back (or a spurious one) |
| `cache_data` | HIGH | a hit returned data ≠ the last write (stale / corrupted line) |

**Metrics** (analytics, never fail the run): accesses, hits, misses, hit-rate,
evictions, write-backs.

---

## 4. Soundness & gating

- Runs only when a cache configuration is available (manifest `cache_config` or
  constructor) **and** the replacement policy is deterministic (LRU/FIFO) —
  random/PLRU cannot be predicted from the access stream alone, so the agent
  stays a metrics-only no-op there rather than guessing.
- Each correctness check fires only for the fields the DUT actually reports
  (`hit` / `evict_addr` / `writeback` / `value`); partial reports are handled
  gracefully.
- `cache_data` integrity uses a golden word-memory seeded from observed writes,
  so it only flags a hit that genuinely contradicts the last write.

### Optional trace contract (additive only)

```
manifest["cache_config"] = {sets, ways, line_size, policy, write_policy}
mem_reads / mem_writes entry:
    {"addr": "0x..", "value": "0x..",
     "cache": {"hit": bool, "evict_addr": "0x..", "writeback": bool}}
    (the cache fields may also be inline on the entry)
```

---

## 5. Report & integration

`run()` returns the standard schema v2.1.0 report plus `cache_enabled`,
`config`, and `metrics`, band `CLEAN→CRITICAL`. Wired into
`_run_extended_pipeline` (`_cache` import, `EXTENDED_AGENTS_AVAILABLE`, writes
`cache_report.json`, records `reports["cache"]`, sources the config from the
semantic map / manifest) and exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/cache_verifier.py --rtl rtl_commit.jsonl --sets 64 --ways 4 --line 64`.

---

## 6. Test coverage

`tests/test_agents.py::TestCacheVerifier` — 14 cases: golden model (resident
hit, LRU victim, dirty-eviction write-back), checker clean pass with hit-rate
metric, hit/miss bug, eviction-victim bug, missing-write-back bug, line
corruption, both gating paths (no config, non-deterministic policy),
malformed-input robustness, report schema, manifest round-trip.

**Full suite: 270 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 7. Limitations / next steps

- **Multi-level hierarchies** (L1/L2/L3, victim cache, inclusion/exclusion) are
  built by composing `CacheModel` instances — a thin wrapper is the next step.
- **PLRU / pseudo-random** policies need either the policy state exposed in the
  trace or a relaxed "plausible victim set" check.
- **Cache coherency** (MSI/MESI/MOESI) is Level 10 and builds on this model once
  multi-hart (`hart_id`) traces are available — the per-line valid/dirty state
  here is the substrate for coherence-state tracking.
- **Prefetch / speculative fills** would need the DUT to tag speculative
  accesses so they are not scored as demand misses.

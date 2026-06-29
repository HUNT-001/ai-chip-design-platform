# Design & Build Plan — T31 TLB Coherence & sfence.vma Verifier

**Status:** Implemented & tested (AVA v2.9.0, 2026-06-29)
**Module:** `AGENT_H/tlb_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this — and why it fits the flow

The TLB caches the translations the Sv32 MMU produces, so it is the direct
companion to T30 (`vm_verifier`). It reuses the **same golden `Sv32MMU`** as its
reference and the **same conservative gating**, so it slots in with no schema
disruption — exactly the low-risk, in-flow choice over an RV64 rewrite.

The bug class it targets — a stale TLB entry served after the page table changed
and was flushed — is one of the most damaging and hardest-to-reproduce in real
silicon: it manifests as rare, data-dependent memory corruption long after the
faulty `sfence.vma` handling.

---

## 2. The correctness model

A TLB is allowed to be *stale* — that is the whole point of a cache. The
architecturally-observable rules are narrow and precise:

1. A served translation must be **explainable**: it is either the current
   page-table walk, or a value that was cached **before** the page table changed
   and has **not** been invalidated by a covering `sfence.vma`.
2. After a covering `sfence.vma`, the next access must reflect the **current**
   page tables.

The verifier therefore maintains a **golden TLB** — `(ASID, VPN) → {pa, global,
invalidated}` — alongside the page-table image, and for every successful golden
walk compares the DUT-served physical address:

| Served value | Verdict |
|---|---|
| = current walk | legitimate fill / refresh (cache the entry) |
| = a cached, **non-invalidated** entry (≠ current) | permitted pre-`sfence` staleness — OK |
| = a cached, **invalidated** entry (≠ current) | `tlb_stale_after_sfence` |
| anything else | `tlb_incoherent` (fabricated translation / ASID leak) |

Crucially, staleness is only flagged **after** a covering `sfence.vma` marks the
entry invalidated — so legal caching windows never produce a false positive.

### sfence.vma scope

- `sfence.vma` / `sfence.vma x0, x0` → full flush (all ASIDs incl. global),
  handled precisely.
- `sfence.vma rs1` (address) / `sfence.vma x0, rs2` (ASID) / both → scoped
  invalidation using the register values recovered from a shadow register file;
  ASID-scoped flushes correctly **spare global pages**.
- If an operand register's value is not recoverable from the trace, the flush
  invalidates **nothing** — under-invalidating is the safe direction (it can
  only miss a bug, never invent one).

---

## 3. Conservative gating (no false positives)

Identical contract to the VM verifier: a check runs only when `satp` selects
Sv32, a physical page-table image is available, the privilege is S/U, and the
access carries a virtual address with a committed `paddr`. Only **successful**
golden walks are compared — page-fault correctness is the VM verifier's job.
On any other trace the agent is a clean, passing no-op.

---

## 4. Report & integration

`run()` returns the standard schema v2.1.0 report plus `translations` and
`stats` (fills, sfence count, permitted-stale count), band `CLEAN→CRITICAL`.
Wired into `_run_extended_pipeline` (`_tlb` import, `EXTENDED_AGENTS_AVAILABLE`,
writes `tlb_report.json`, records `reports["tlb"]`) and exported from
`AGENT_H/__init__.py`. Standalone:
`python AGENT_H/tlb_verifier.py --rtl rtl_commit.jsonl`.

---

## 5. Test coverage

`tests/test_agents.py::TestTLBVerifier` — 8 cases walking the full lifecycle:
fill → permitted pre-`sfence` staleness (passes) → stale-after-`sfence`
(CRITICAL) → correct refill (passes) → incoherent/fabricated translation →
non-Sv32 gating → malformed-input robustness → manifest round-trip.

**Full suite: 244 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 6. Limitations / next steps

- ASID isolation is enforced via the `(ASID, VPN)` key plus the global-page
  lookup; a dedicated `tlb_asid_leak` check name (currently surfaced as
  `tlb_incoherent`) could make that diagnosis more explicit.
- `sfence.vma` ordering relative to in-flight accesses (the fence-as-a-barrier
  semantics) is modelled at retire granularity; a finer pipeline model would
  pair with the temporal checker.
- Sv39/Sv48 TLBs reuse this model unchanged once the RV64 widening lands — only
  the underlying `Sv32MMU` walk geometry changes.
- Superpage TLB entries are keyed by their 4 KB VPN; modelling coalesced
  superpage entries explicitly is a possible refinement.

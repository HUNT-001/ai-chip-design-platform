# Design & Build Plan — T30 Sv32 Virtual-Memory Verifier

**Status:** Implemented & tested (AVA v2.8.0, 2026-06-28)
**Module:** `AGENT_H/vm_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this — and why it fits the flow

Virtual memory is the layer directly above the privilege/PMP foundation we just
hardened, and it is the gate for full **Linux-class** cores (Rocket, CVA6,
Shakti C-class). A wrong page-table walk, a missing or spurious page fault, or a
permission check that ignores the U / SUM / MXR rules is security-critical and
is not reliably surfaced by value-level tandem diffing.

It was chosen over the RV64 widening deliberately: RV64 touches the schema and
every existing verifier (a disruption to the flow), whereas the Sv32 verifier is
a **single self-contained module** that follows the established golden-checker
pattern and introduces **no schema-breaking changes** — every trace field it
reads is additive and optional.

---

## 2. The novel core — a golden Sv32 MMU

`Sv32MMU` is a spec-faithful software implementation of the RISC-V Sv32
translation algorithm (privileged spec §4.3): a two-level walk over a
physical-memory image with `satp` as the root.

- **Geometry**: 32-bit VA = VPN[1] (`[31:22]`) · VPN[0] (`[21:12]`) · offset
  (`[11:0]`); 4-byte PTEs; two levels.
- **PTE decode**: V/R/W/X/U/G/A/D flags, PPN in bits `[31:10]`.
- **Walk**: invalid PTE or reserved `W=1,R=0` → page fault; leaf when R or X
  set, otherwise descend; running off the bottom level → fault.
- **Permissions**: per access type (fetch→X, load→R or X-with-MXR, store→W),
  with U-bit handling and the S-mode SUM rule for user pages.
- **Superpages**: a level-1 leaf is a 4 MB superpage; PPN[0] must be zero
  (misaligned superpage → fault); PA composed from PPN[1] and VA's VPN[0].
- **Output**: a `Translation` carrying either the physical address (with the
  leaf level) or the exact fault cause (12 fetch / 13 load / 15 store).

This model is independently unit-tested against **hand-built page tables** with
hand-computed expected results (4 KB map, 4 MB superpage, invalid PTE, write to
a read-only page, S-mode access to a user page with/without SUM, misaligned
superpage, Bare mode), so the golden reference itself is proven before it is
used to judge the DUT.

---

## 3. The checker

`VMVerifier` runs the golden MMU against the commit log:

| Check | Severity | Catches |
|---|---|---|
| `vm_translation` | HIGH | committed physical address ≠ golden translation |
| `vm_missing_fault` | HIGH | access that must page-fault didn't (or wrong cause) |
| `vm_spurious_fault` | HIGH | a valid translation that raised a page fault |

### Conservative gating (no false positives, no flow disruption)

A check runs only when **all** of the following hold, otherwise it is skipped:

- `satp` selects Sv32 (`MODE` bit set);
- a physical page-table image is available (`phys_mem` on the manifest or a
  record) — without it the walk cannot be reproduced, so the agent stays silent;
- the privilege is S or U (Sv32 translation does not apply in M-mode);
- the memory access carries a virtual address (`vaddr`); the committed physical
  address is read from `paddr` when present.

On a bare-metal / M-mode / no-MMU trace the agent is a clean, passing no-op.

### Optional trace contract (additive only)

```
record["csrs"]["satp"]        Sv32 enable + root PPN
record["priv"|"mode"|...]      current privilege
record["phys_mem"]             {hex_addr: hex_word}  page-table image
record["csrs"]["mstatus"]      SUM (bit 18) / MXR (bit 19)
mem_reads/mem_writes entries:   {"vaddr": "...", "paddr": "...", ...}
record["trap"]                 page-fault cause 12 / 13 / 15
```

---

## 4. Report & integration

`run()` returns the standard schema v2.1.0 report plus `sv32_enabled` and
`translations`, band `CLEAN→CRITICAL`. Wired into `_run_extended_pipeline`
(`_vm` import, `EXTENDED_AGENTS_AVAILABLE`, writes `vm_report.json`, records
`reports["vm"]`, sources the page-table image from the semantic map / manifest)
and exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/vm_verifier.py --rtl rtl_commit.jsonl`.

---

## 5. Test coverage

`tests/test_agents.py::TestVMVerifier` — 18 cases: 7 golden-MMU vectors (4 KB,
superpage, misaligned superpage, invalid PTE, write-without-W, U-page+SUM, Bare
mode), checker pass/translation-bug/missing-fault/correctly-trapped/spurious-
fault, per-record page-table image, both gating paths (non-Sv32, M-mode),
malformed-input robustness, report schema, manifest round-trip.

**Full suite: 236 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 6. Limitations / next steps

- **A/D bits**: the model computes whether the fault scheme would require an A/D
  fault but the checker does not assert it by default (the A/D-update vs
  A/D-fault scheme is implementation-defined); a manifest flag can enable the
  fault-scheme check.
- **Sv39/Sv48** (3- and 4-level, RV64) reuse the identical walk structure and
  drop in once the RV64 widening lands; the level loop and PTE decode are
  already parameterised by geometry.
- **TLB behaviour** (caching, `sfence.vma` invalidation ordering) is a natural
  companion agent built on this translator.
- **Instruction-fetch translation** (`X` permission on `pc`) is supported by the
  MMU (`access="fetch"`) and only needs a `vaddr`-carrying fetch field in the
  trace to be exercised by the checker.

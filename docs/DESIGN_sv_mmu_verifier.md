# Design & Build Plan — T37 Sv39 / Sv48 MMU Verifier (RV64 widening, phase 2)

**Status:** Implemented & tested (AVA v2.15.0, 2026-06-30)
**Module:** `AGENT_H/sv_mmu_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

This is the **marquee RV64 unlock**: Sv39 (and Sv48) is the address-translation
scheme every Linux-class RISC-V core uses (Rocket, BOOM, CVA6, Shakti C-class).
Phase 1 (`rv64_verifier`) gave AVA the 64-bit datapath; this phase gives it
64-bit virtual memory, completing the two things that separate an embedded core
from a Linux-capable one.

It is the natural generalisation the Sv32 design doc anticipated: *"Sv39/Sv48
reuse the identical walk structure once the RV64 widening lands."*

---

## 2. The novel core — one mode-parameterised walker

`SvMMU` handles Sv39 and Sv48 from a single code path driven by a mode table
(`levels`, `vpn_bits`, `va_bits`):

- **8-byte PTEs**, 64-bit `satp` (MODE in bits [63:60]: 8 = Sv39, 9 = Sv48),
  44-bit PPN, 56-bit physical addresses.
- **2/3/4-level walk** with the same general leaf formula, so 4 KB, 2 MB, 1 GB
  (Sv39) and 512 GB (Sv48) pages all fall out of one expression:
  `PA = (ppn >> level·vpn_bits) << (12 + level·vpn_bits) | (va & page_mask)`.
- **Superpage alignment** — a leaf at level *L* requires the low `L·vpn_bits`
  PPN sub-fields to be zero (misaligned superpage → page fault).
- **Non-canonical VA rule** — the VA bits above `va_bits` must all equal the top
  VA bit (sign extension); otherwise a page fault, exactly as hardware requires.
- **Permissions** — R/W/X per access, the U-bit, S-mode SUM, and load-MXR.

The walker is unit-tested against hand-built page tables with hand-computed
results for 4 KB, 2 MB and 1 GB pages, a misaligned superpage, an invalid PTE,
and a non-canonical address — so the golden reference is proven before it judges
any DUT.

---

## 3. The checker

`SvMMUVerifier` runs the golden walker against the trace and compares the
DUT-served translation:

| Check | Severity | Catches |
|---|---|---|
| `sv_translation` | HIGH | committed physical address ≠ golden translation |
| `sv_missing_fault` | HIGH | access that must page-fault didn't (or wrong cause) |
| `sv_spurious_fault` | HIGH | a valid translation that raised a page fault |

It deliberately handles **only Sv39/Sv48** (gated on the `satp` MODE field), so
it never double-covers the Sv32 `vm_verifier`. The rest of the gating matches
the Sv32 verifier: a physical page-table image must be available, the privilege
must be S/U, and the access must carry a virtual address. Clean no-op on
bare-metal / Sv32 / M-mode traces.

---

## 4. Report & integration

`run()` returns the standard schema v2.1.0 report plus `mode`, `sv_enabled`, and
`translations`, band `CLEAN→CRITICAL`. Wired into `_run_extended_pipeline`
(`_svmmu` import, `EXTENDED_AGENTS_AVAILABLE`, writes `sv_mmu_report.json` only
when Sv39/Sv48 is detected, records `reports["sv_mmu"]`) and exported from
`AGENT_H/__init__.py`. Standalone:
`python AGENT_H/sv_mmu_verifier.py --rtl rtl_commit.jsonl`.

---

## 5. Test coverage

`tests/test_agents.py::TestSvMMUVerifier` — 14 cases: satp mode detection
(sv39/sv48/bare); Sv39 golden walks for **4 KB, 2 MB, and 1 GB** pages;
misaligned superpage; invalid PTE; non-canonical VA; checker clean pass,
translation bug, missing fault; bare-mode gating; malformed-input robustness;
report schema; manifest round-trip.

**Full suite: 319 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes. Live walk: 4 KB → `0x90000000`, 2 MB → `0x40000000`,
1 GB → `0xC0000000`.

---

## 6. RV64 widening — status & remaining

| Phase | Item | Status |
|---|---|---|
| 1 | RV64 datapath (64-bit ALU + W-ops sign-extension) | ✅ `rv64_verifier` |
| 2 | **Sv39 / Sv48 virtual memory** | ✅ this module |
| 3 | RV64 atomics (`AMO*.D`, `LR.D`/`SC.D`) | next — extend `atomics_verifier` |
| 4 | RV64 M-extension W-ops (`mulw/divw/divuw/remw/remuw`) | extend `rv64_verifier` |
| 5 | 64-bit addresses in PMP / cache / bus (xlen parameter) | widen address masks |

### Module limitations / next steps

- **A/D bits** are not asserted by default (implementation-defined
  fault-vs-update scheme), matching the Sv32 verifier.
- **TLB over Sv39** — the existing `tlb_verifier` is built on the Sv32 walker;
  pointing it at `SvMMU` extends TLB-coherence checking to RV64.
- **Sv57** (5-level) drops in by adding one mode-table row.

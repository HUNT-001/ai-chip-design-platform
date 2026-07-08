# Design & Build Plan — T50 Hypervisor Two-Stage Translation Checker

**Status:** Implemented & tested (AVA v2.35.0, 2026-06-30)
**Module:** `AGENT_H/hypervisor_verifier.py`

---

## 1. Why this level

The RISC-V Hypervisor (H) extension's defining feature is **two-stage address
translation**: a guest virtual address (GVA) goes through the **VS-stage**
(guest page tables, `vsatp`) to a guest physical address (GPA), which the
**G-stage** (hypervisor page tables, `hgatp`) then translates to a supervisor/
host physical address (SPA). This is where virtualization bugs live, and it has
fault semantics no non-virtual core has.

## 2. Scope — composition, not re-walking a single stage

The multi-level page-table *walk* of one stage is already covered by
`vm_verifier` (Sv32) and `sv_mmu_verifier` (Sv39/48). The genuinely new,
bug-prone parts of the H-extension are:

1. the **composition** of the two stages (GVA → GPA → SPA), and
2. the **fault classification** — which stage faulted and with which cause.

So this agent takes each stage as its resolved mapping (the walk's *output*):
`vs_map: VPN → {gpn, r,w,x,v}` and `g_map: GPN → {ppn, r,w,x,v}`. `TwoStageMMU`
composes them and classifies faults. This keeps the checker focused and its
tests tractable while covering exactly the H-specific logic.

## 3. The fault semantics (the crux)

A fault's cause tells you *which stage* failed — and getting this wrong is a
classic H-extension bug:

| Access | VS-stage fault (ordinary page fault) | G-stage fault (guest-page fault) |
|---|---|---|
| instruction / exec | **12** | **20** |
| load | **13** | **21** |
| store / AMO | **15** | **23** |

`TwoStageMMU.translate(gva, access)` walks VS first (missing/invalid PTE or a
permission failure ⇒ the ordinary cause), then G (⇒ the guest-page-fault cause),
and only on success composes `(g.ppn << 12) | page_offset`.

## 4. Checks

- **htrans_result** (HIGH) — the composed supervisor PA differs from the DUT's.
- **htrans_fault** (HIGH) — the DUT is missing an expected fault, raises a
  spurious one, or reports the wrong cause (which also means the wrong *stage*).

Both directions are tested (spurious fault, wrong cause across stages, wrong PA),
along with VS-stage miss + permission faults and G-stage miss + permission
guest-faults, and the exec-cause pair (12/20).

## 5. Trace contract (additive, separate stream)

```
hypervisor_trace.jsonl:
  {"op":"config",
   "vs_map":{"0x80":{"gpn":"0x120","r":true,"w":true,"x":false,"v":true}},
   "g_map": {"0x120":{"ppn":"0x300","r":true,"w":true,"x":false,"v":true}}}
  {"op":"translate","gva":"0x80abc","access":"load","pa":"0x300abc","fault":null}
```

Maps may also be given per-`translate` (overriding the config). Clean no-op on an
absent trace.

## 6. Integration & tests

Wired into `_run_extended_pipeline` (`_hyp`, `run_from_manifest` →
`hypervisor_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/test_agents.py::TestHypervisorVerifier` — 8 cases (validated standalone:
10): clean translate, VS-stage page fault, VS permission fault, G-stage
guest-page fault (miss + permission), exec causes, spurious/wrong-cause/wrong-PA,
robustness/schema, manifest. All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 7. Limitations / next steps

- **Full interleaved walk** — each guest PTE fetch is itself a GPA that the
  G-stage translates; model over a host-physical memory image (reusing
  `sv_mmu_verifier`) to check the nested walk, not just the resolved mapping.
- **G-stage x4 root widening** (Sv39x4/Sv48x4) and GPA-width bounds.
- **`hlv`/`hsv` hypervisor load/store** instructions and `hstatus.SPVP`
  effective-privilege selection.
- **VS-mode CSR aliasing** (`vsstatus`/`vsie`/… ↔ `sstatus`/`sie`) and virtual
  interrupt injection (`hvip`/`hip`).

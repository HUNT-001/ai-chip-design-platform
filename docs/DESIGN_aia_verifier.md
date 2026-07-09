# Design & Build Plan — T51 AIA / IMSIC Checker

**Status:** Implemented & tested (AVA v2.36.0, 2026-06-30)
**Module:** `AGENT_H/aia_verifier.py`

---

## 1. Why this level

The Advanced Interrupt Architecture (AIA) is the modern RISC-V interrupt story,
and **IMSIC** (Incoming Message-Signaled Interrupt Controller) is its core: it
receives MSIs and, per hart, selects the highest-priority interrupt to present
to the core via the **`topei`** register. This completes the interrupt-controller
family alongside the wired PLIC/CLINT/CLIC (T46).

## 2. The selection rule (the crux, and it's *inverted* from PLIC)

IMSIC priority is the **interrupt identity number itself — smaller = higher
priority** (the opposite of PLIC, where a larger `priority` value wins). A common
bug is applying PLIC-style ordering to IMSIC. `topei` returns the identity that
is:

- **pending** (`eip[i]=1`) AND **enabled** (`eie[i]=1`),
- eligible w.r.t. **`eithreshold`** — if non-zero, only identities
  `1 .. eithreshold-1` are eligible,
- and only if **`eidelivery`** is enabled (else `topei = 0`),

with the **smallest** eligible identity winning.

## 3. Checks

| Check | Catches |
|---|---|
| `imsic_topei` | wrong selection (e.g. PLIC-style largest-wins, or ignoring threshold) |
| `imsic_delivery` | `topei ≠ 0` while `eidelivery` is off |
| `imsic_threshold` | a selected identity ≥ `eithreshold` (should be masked) |
| `imsic_disabled` | a selected identity that isn't both pending and enabled |

## 4. Trace contract (additive, separate stream)

```
aia_trace.jsonl:
  {"op":"imsic_config","eidelivery":1,"eithreshold":8,"eie":[2,3,7],"eip":[3,7]}
  {"op":"imsic_topei","result":3}     # DUT topei identity (0 = none)
```

`eie`/`eip` are the enabled/pending identity sets. Clean no-op on an absent
trace.

## 5. Integration & tests

Wired into `_run_extended_pipeline` (`_aia`, `run_from_manifest` →
`aia_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/test_agents.py::TestAIAVerifier` — 7 cases (validated standalone: 9):
lowest-identity-wins + wrong pick, threshold masking, delivery-off (both
directions), disabled / not-pending, none-pending, robustness/schema, manifest.
All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 6. Limitations / next steps

- **APLIC** (Advanced PLIC) — domain hierarchy, source modes (edge/level), and
  MSI-vs-direct delivery forwarding to IMSIC.
- **Guest interrupt files** — IMSIC `VGEIN`/guest external interrupts for the
  H-extension (links with `hypervisor_verifier`).
- **`iprio` / indirect CSR** access (`miselect`/`mireg`) and the
  `mtopi`/`stopi` major-interrupt selection.

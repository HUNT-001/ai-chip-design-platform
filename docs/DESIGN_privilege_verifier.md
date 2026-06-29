# Design & Build Plan — T29 Privilege & PMP Verifier

**Status:** Implemented & tested (AVA v2.7.0, 2026-06-26)
**Module:** `AGENT_H/privilege_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

Privilege levels and Physical Memory Protection are the dimension AVA previously
did not cover, and they gate the *secure* and *Linux-class* application domains.
Privilege/PMP bugs are security-critical and slip past value-level tandem
diffing: an MRET that returns to the wrong mode, a U-mode access that bypasses a
PMP region, or a CSR written from too low a privilege are all functional faults
with no register divergence at the point of failure.

---

## 2. What it checks

| Check | Severity | Catches |
|---|---|---|
| `priv_xret_illegal` | HIGH | MRET/SRET from too low a privilege without an illegal-instruction trap |
| `priv_csr_access` | HIGH | CSR access above the current privilege without a trap |
| `priv_ecall_cause` | HIGH | ECALL trap cause ≠ 8/9/11 for U/S/M |
| `priv_mret_target` | HIGH | privilege after MRET/SRET ≠ mstatus.MPP / sstatus.SPP |
| `pmp_missing_fault` | HIGH | a U/S access denied by PMP that did not fault |
| `pmp_spurious_fault` | HIGH | a PMP-permitted access that raised an access fault |

Privilege is encoded U=0, S=1, M=3. CSR privilege is read from address bits
`[9:8]`, reusing the `csr_verifier` address table.

---

## 3. PMP model

`PMPModel` shadows 16 entries (`pmpcfg0..3` packing four config bytes each on
RV32, `pmpaddr0..15`) and implements full region matching:

- **OFF** — no match.
- **TOR** — `[pmpaddr[i-1]<<2, pmpaddr[i]<<2)` (entry 0 lower bound is 0).
- **NA4** — 4-byte region at `pmpaddr<<2`.
- **NAPOT** — trailing-ones decode: `size = 1 << (trailing_ones + 3)`,
  `base = (pmpaddr & ~mask) << 2`.

Lowest matching index wins. Permission resolution:

- `priv == M` and entry **unlocked** → allowed (M-mode ignores unlocked PMP);
- otherwise the access needs the matching entry's R/W/X bit;
- no matching entry → allowed only in M-mode.

The expected fault cause is load = 5, store/AMO = 7, fetch = 1.

---

## 4. Conservatism (false-positive avoidance)

Every check is gated on information actually present in the trace:

- privilege-dependent checks run only when the record carries a privilege field
  (`priv` / `mode` / `privilege` / `prv`);
- PMP checks run only when at least one PMP entry is configured;
- `mret` target tracking reads `mstatus.MPP` from the shadow CSR state that
  existed *before* the MRET retired.

A core that emits no privilege field simply produces an all-skipped, passing
report — honest rather than noisy.

---

## 5. Report & integration

`run()` returns the standard schema v2.1.0 report plus `pmp_configured` and
per-category `stats`, band `CLEAN→CRITICAL`. Wired into
`_run_extended_pipeline` (`_privilege` import, `EXTENDED_AGENTS_AVAILABLE`,
writes `privilege_report.json`, records `reports["privilege"]`) and exported
from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/privilege_verifier.py --rtl rtl_commit.jsonl`.

---

## 6. Test coverage

`tests/test_agents.py::TestPrivilegeVerifier` — 12 cases: `parse_priv`, the
NAPOT PMP model (match + permission + M-mode bypass), MRET-in-U illegal, legal
MRET, ECALL cause bug, CSR-from-U, MRET target mismatch, PMP missing fault, PMP
permitted access passes, report schema, manifest round-trip.

**Full suite: 156 passed**, `compileall` clean.

---

## 7. Limitations / next steps

- **Virtual memory (Sv32/Sv39/Sv48), `satp`, TLB and page-table walks** are the
  next layer up; the privilege/PMP foundation here is the prerequisite.
- Instruction-fetch PMP (`X` permission on `pc`) is modelled but only exercised
  for explicit load/store accesses in the current tests; enabling per-fetch
  checks is a config flag away.
- `mstatus` field effects beyond MPP (MIE/MPIE stacking, MPRV) are not yet
  asserted; they pair naturally with the `csr_verifier` field-mask work.
- PMP `mseccfg` (Smepmp) and locked-region M-mode enforcement edge cases are
  modelled but lightly tested.

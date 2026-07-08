# Design & Build Plan — T49 Reset-State Checker

**Status:** Implemented & tested (AVA v2.33.0, 2026-06-30)
**Module:** `AGENT_H/reset_verifier.py`

---

## 1. Why this level

A wrong reset value is one of the nastiest bug classes: it silently corrupts
every boot, the core still "runs" so it's easy to miss, and none of the runtime
verifiers touch it (they observe a machine that's already executing). This agent
checks the *reset snapshot* — privilege mode, PC, and CSRs at t=0 — against the
values the ISA mandates plus any implementation-specific golden values.

## 2. Architecturally-mandated invariants (priv spec §3.4)

| Check | Rule | Why it matters |
|---|---|---|
| `reset_priv` | hart resets into **M-mode** | anything else is unbootable |
| `reset_mstatus_mie` | `mstatus.MIE = 0` | a core that resets with interrupts enabled can take a spurious trap before software configures a handler |
| `reset_mstatus_mprv` | `mstatus.MPRV = 0` | loads/stores must use the current privilege, not a stale one |
| `reset_pc` | PC = reset vector | fetch starts at the wrong place |
| `reset_misa` | valid MXL (1/2/3) + I/E base bit, if `misa` ≠ 0 | a malformed misa mis-advertises the machine |

`misa` MXL is read from the top two bits, with RV32/RV64 auto-detected by
magnitude (a value > 32 bits ⇒ MXL at bits [63:62], else [31:30]).

## 3. Golden comparison

`reset_csr` compares every CSR listed in the snapshot's `expected.csrs`
(implementation-specific values like `mtvec`, `pmpcfg`/`pmpaddr`, vendor CSRs)
against the observed reset value. This lets a platform pin its exact reset ROM
configuration while the mandated invariants above stay implementation-agnostic.

## 4. Input & integration

One snapshot dict or a **multi-hart** list:

```json
{"hart":0, "priv":"M", "pc":"0x80000000",
 "csrs":{"mstatus":"0x0","misa":"0x40141101","mie":"0x0"},
 "expected":{"pc":"0x80000000","csrs":{"mtvec":"0x80000004"}}}
```

Wired into `_run_extended_pipeline` (`_reset`, `run_from_manifest` reads
`outputs.reset_snapshot` + `reset_config`, writes `reset_report.json`). Exported
from `AGENT_H/__init__.py`. Clean no-op on an absent snapshot.

## 5. Test coverage

`tests/test_agents.py::TestResetVerifier` — 8 cases (validated standalone: 12):
clean, priv/MIE/MPRV, reset-PC (snapshot + config vector), misa RV32-bad /
RV64-ok, expected-CSR mismatch, multi-hart, robustness/schema, manifest. All
pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 6. Limitations / next steps

- **WARL/WPRI field legality** on reset (e.g. `mstatus` reserved fields read 0)
  and **read-only-zero** CSR enforcement.
- **PMP reset** — all regions off (no `pmpcfg` lock forcing access faults) as a
  mandated invariant rather than only via `expected`.
- **Debug reset** — `dcsr` reset fields and the `resethaltreq` entry path
  (links with `debug_verifier`).
- **Vector/FP reset** — `vtype.vill=1`, `vl=0`, `mstatus.VS/FS=Off`.

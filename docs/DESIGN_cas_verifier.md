# Design & Build Plan — T55 Zacas Compare-and-Swap Checker

**Status:** Implemented & tested (AVA v2.43.0, 2026-07-09)
**Module:** `AGENT_H/cas_verifier.py`

---

## 1. Why this level

Compare-and-swap is *the* primitive lock-free and concurrent code is built on —
mutexes, spinlocks, MPSC queues, reference counts, hazard pointers all reduce to
CAS. A subtly-wrong `amocas` (returns the wrong value, writes on a mismatch, or
fails to write on a match) silently corrupts every one of those, while each
individual memory access still looks correct in isolation. The RISC-V **Zacas**
extension (`amocas.w/.d/.q`) is a small, exactly-specified instruction, so it is
checkable to the bit.

## 2. Semantics and checks

`amocas` atomically: reads `mem_old`, compares it to the expected value in `rd`,
writes `swap` (`rs2`) to memory **iff** they match, and returns `mem_old` in `rd`
either way. Three HIGH checks capture this completely:

| Check | Rule |
|---|---|
| `cas_return` | `rd` after the op equals `mem_old` (the returned value) |
| `cas_success` | `mem_old == compare` ⇒ memory becomes `swap` |
| `cas_fail` | `mem_old != compare` ⇒ memory is **unchanged** |

Compared values are masked to the op width (`.w`=32, `.d`=64, `.q`=128), so a
32-bit CAS ignores the upper register bits. Metrics: op count, successes,
failures.

## 3. Trace contract (additive)

```
cas_trace.jsonl:
  {"op":"amocas.w", "addr":"0x40", "compare":"0x5", "swap":"0x9",
   "mem_old":"0x5", "mem_new":"0x9", "rd":"0x5"}
```

All values explicit (no shadow state needed), so the golden is unambiguous.
Clean no-op on an absent trace or a non-`amocas` op.

## 4. Integration & tests

Wired into `_run_extended_pipeline` (`_cas`, `run_from_manifest` →
`cas_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/test_extended_agents.py::TestCASVerifier` — 5 cases (validated standalone:
10): success/fail clean, wrong-return, success-no-write, fail-modified, width
masking, metrics, no-op/robustness/schema, manifest. All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 5. Limitations / next steps

- **`amocas.q`** (128-bit, register-pair operands) — the value model handles the
  128-bit mask; a register-pair decode from the commit log would let it run off
  `rtl_log` instead of an explicit trace.
- **Atomicity under contention** — that no other core's write interposes between
  the CAS read and write (links with `coherence_verifier`'s RMW atomicity and
  `memory_model_verifier`).
- **`amocas` vs `lr/sc`** equivalence — cross-check a CAS against the equivalent
  LR/SC sequence via `atomics_verifier`.

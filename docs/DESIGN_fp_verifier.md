# Design & Build Plan — T27 RV32F / RV32D Floating-Point Verifier

**Status:** Implemented & tested (AVA v2.5.0, 2026-06-26)
**Module:** `AGENT_H/fp_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

Floating point is the "G" half of RV64**GC** and a notorious source of silicon
bugs that ordinary tandem diffing under-tests: NaN-boxing of single values in
wide registers, the canonical-NaN rule, signed zeros, the ±inf / NaN corner
cases of min/max and compare, sign-injection bit plumbing, and the
exception-flag (`fflags`) side effects. A wrong result here may not surface in a
general-purpose register for many instructions. This agent recomputes each FP
operation with a golden IEEE-754 model and compares bit-exact.

---

## 2. Golden model

The model leans on Python's `struct` + `float`, which **are** IEEE-754
binary32/binary64 and are correctly rounded for the basic operations under
round-to-nearest-even:

- single (`.s`): compute in Python `float` (binary64) then round to binary32 via
  `struct` — provably correctly rounded for `+ - * / sqrt` (double-rounding is
  safe binary64→binary32 for these);
- double (`.d`): Python `float` *is* binary64, so the op is directly the golden
  result;
- generated NaNs are normalised to the RISC-V **canonical** NaN
  (`0x7fc00000` / `0x7ff8000000000000`), matching the spec's "FP ops never
  propagate NaN payloads" rule;
- overflow on packing maps to a signed infinity (the IEEE overflow result).

A shadow FP register file (raw bit patterns, folded from each record's FP
write-backs) supplies operands, so a single commit record is enough to check.

### Conservatism (false-positive avoidance)

- **Rounding mode.** Value checks run under round-to-nearest-even (the
  architectural default and the mode used by essentially all FP test suites).
  Under a *confirmed* directed-rounding mode a mismatch is reported at MEDIUM,
  not HIGH; an explicit `rne` confirms HIGH.
- **Missing operands.** If an operand bit pattern isn't in the trace, the check
  is *skipped* (counted in `stats.skipped`), never failed.
- **FMA / some conversions.** Correctly-rounded fused multiply-add can't be
  reproduced in stock Python, so FMA value checks are skipped (the NaN-boxing
  check still applies). This keeps the agent honest rather than flaky.

---

## 3. Checks

| Check | Severity | Catches |
|---|---|---|
| `fp_nan_boxing` | HIGH | single result in a 64-bit reg without all-ones upper half |
| `fp_result` | HIGH / MEDIUM | `fadd/fsub/fmul/fdiv/fsqrt` ≠ golden (HIGH under confirmed RNE) |
| `fp_sgnj` | HIGH | `fsgnj/fsgnjn/fsgnjx` sign-injection wrong |
| `fp_minmax` | HIGH | `fmin/fmax` wrong (incl. NaN and ±0 rules) |
| `fp_compare` | HIGH | `feq/flt/fle` integer result wrong (incl. NaN → 0) |
| `fp_class` | HIGH | `fclass` 10-bit mask wrong |
| `fp_move` | HIGH | `fmv.x.w` / `fmv.w.x` (and `.d`) bit copy wrong |
| `fp_flag_missing` | MEDIUM | mandatory `fflags` bit (NV invalid, DZ div-by-zero) not raised |

`fclass_mask()` is a standalone, fully-tested classifier (the 10 IEEE classes
from the raw bit fields).

---

## 4. Report & integration

`run()` returns the standard schema v2.1.0 report plus `flen` and `fp_ops`, with
band `CLEAN→CRITICAL`. Wired into `_run_extended_pipeline` (`_fp` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `fp_report.json`, records `reports["fp"]`)
and exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/fp_verifier.py --rtl rtl_commit.jsonl`.

The verifier reads FP register state from a `fregs`/`fpregs` map or from
`f*`-named entries in the generic `regs` map, and `fflags` from the `fflags` or
`fcsr` CSR — so it works with whatever convention the trace uses.

---

## 5. Test coverage

`tests/test_agents.py::TestFPVerifier` — 14 cases: decode, `fclass_mask` (all
classes), clean `fadd`, `fadd` result bug (CRITICAL), NaN-boxing, divide-by-zero
flag, sign-injection bug, `fmax`, NaN compare bug, `fclass` bug, `fmv.x.w` bug,
report schema, manifest round-trip.

**Full suite: 110 passed**, `compileall` clean.

---

## 6. Limitations / next steps

- **FMA** (`fmadd/fmsub/fnmadd/fnmsub`) value checks are skipped pending a
  correctly-rounded fused-multiply-add reference (e.g. `math.fma` on Python 3.13+,
  or an mpmath/`Fraction`-based exact rounder).
- **Directed rounding** (RTZ/RDN/RUP/RMM) is detected but not value-checked;
  add per-mode rounding to promote those to HIGH.
- **Conversions** (`fcvt.*`) currently rely on the NaN-box check only; a golden
  int↔float / single↔double converter with the saturation + flag rules is the
  next addition.
- **Subnormal/underflow (UF) and inexact (NX) flags** are not yet asserted (only
  NV and DZ); they need the rounded-vs-exact comparison to be precise.

# Design & Build Plan — T36 RV64 Datapath Verifier (XLEN-64 Widening, Phase 1)

**Status:** Implemented & tested (AVA v2.14.0, 2026-06-30)
**Module:** `AGENT_H/rv64_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this — and why it's the feasible first step of RV64

RV64 is the strategic widening that unlocks the Linux-class core tier (Rocket,
BOOM, CVA6, Shakti C-class). A full widening eventually touches the schema and
every verifier's arithmetic — a large, risky change. The **feasible first
step**, delivered here, is a single self-contained agent that verifies the
*defining* RV64 semantics without disturbing anything else:

- **64-bit registers and ALU** — the obvious widening.
- **W-suffix word ops** (`addw/subw/sllw/srlw/sraw` + immediates) — RV64's
  distinctive addition: a 32-bit operation whose result is **sign-extended to 64
  bits**. Forgetting that sign extension (leaving the upper 32 bits zero) is the
  single most common RV64 silicon bug, and it is *invisible* to an RV32 model.

Because the agent **auto-detects** RV64 (a trace is RV64 if it contains a W-op or
a register value wider than 32 bits), it is a clean no-op on the entire existing
RV32 suite — so it lands with zero disruption and no schema change.

---

## 2. The golden core

A 64-bit in-order ALU with two evaluators:

- `alu64(op, a, b)` — full-width `add/sub/and/or/xor/sll/srl/sra/slt/sltu` and
  immediates, with 6-bit shift amounts (vs 5-bit on RV32).
- `aluw(op, a, b)` — the word ops: do the operation on the low 32 bits, then
  `sext32(...)` the result to 64 bits. W-shifts use a 5-bit shamt.

`sext32()` is the heart of the matter: `0x80000000 → 0xFFFFFFFF80000000`. All
three are unit-tested against hand-computed vectors (e.g. `aluw("subw",0,1) ==
0xFFFFFFFFFFFFFFFF`, `aluw("sraw",0x80000000,4) == 0xFFFFFFFFF8000000`).

The checker recomputes every modelled instruction from the architectural 64-bit
register file (updated from committed values, so no error cascade) and compares
to the committed `rd`.

---

## 3. Checks

| Check | Severity | Catches |
|---|---|---|
| `rv64_word_sext` | HIGH | W-op low 32 bits correct but upper 32 ≠ sign-extension of bit 31 |
| `rv64_word_op` | HIGH | W-op result wrong for another reason |
| `rv64_result` | HIGH | 64-bit ALU result ≠ golden |
| `rv64_shamt` | HIGH | W-shift immediate shamt > 31 (reserved encoding) |

The `rv64_word_sext` case is reported with an **explainable** diagnosis — it
specifically recognises the missing-sign-extension signature and names it, so a
1-line fix (the sign-extend) is obvious from the report.

**Metrics**: `ops_checked`, `word_ops`.

---

## 4. Soundness & gating

- Runs only on RV64-detected traces (or with `force=True`); pure RV32 traces are
  a passing no-op even if they contain a wrong result — the agent stays strictly
  within RV64 territory and never second-guesses the RV32 model.
- A check fires only when every source operand is available in the shadow
  register file; ABI and `xN` register names are both supported.
- Robust against malformed records (guarded loop).

---

## 5. Report & integration

`run()` returns the standard schema v2.1.0 report plus `rv64_detected` and
`ops_checked`, band `CLEAN→CRITICAL`. Wired into `_run_extended_pipeline`
(`_rv64` import, `EXTENDED_AGENTS_AVAILABLE`, writes `rv64_report.json` only when
RV64 is detected, records `reports["rv64"]`) and exported from
`AGENT_H/__init__.py`. Standalone:
`python AGENT_H/rv64_verifier.py --rtl rtl_commit.jsonl`.

---

## 6. Test coverage

`tests/test_agents.py::TestRV64Verifier` — 10 cases: golden 64-bit + W-op
vectors, clean RV64 pass, the **sign-extension bug** (verifying the expected
value), a 64-bit-result bug, the **RV32 no-op** guarantee, reserved shamt,
malformed-input robustness, report schema, manifest round-trip.

**Full suite: 305 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 7. RV64 widening roadmap (remaining phases)

This agent is **phase 1**. The remaining widening, in feasible order, each
reusing the established pattern:

1. **Sv39 / Sv48 virtual memory** — generalise `Sv32MMU` to 3- and 4-level
   walks with 64-bit PTEs and addresses. The walk loop and PTE decode are
   already structured for this; it is the marquee Linux-class unlock.
2. **RV64 atomics (`AMO*.D`, `LR.D`/`SC.D`)** — extend `atomics_verifier`
   (currently flags `.d` as illegal on RV32) to verify 64-bit atomics.
3. **RV64 M-extension word ops** (`mulw`, `divw`, `divuw`, `remw`, `remuw`) —
   add to the W-op evaluator.
4. **64-bit addresses across PMP / cache / bus** — widen the address masks in
   those agents behind an `xlen` parameter (default 32, set 64 for RV64 runs).
5. **A shared `riscv_xlen` helper** consolidating the per-module `_u32/_s32`
   masks into XLEN-parameterised functions, so the widening is set once.

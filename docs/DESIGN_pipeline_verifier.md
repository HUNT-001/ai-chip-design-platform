# Design & Build Plan — T32 Pipeline & Hazard Verifier

**Status:** Implemented & tested (AVA v2.10.0, 2026-06-29)
**Module:** `AGENT_H/pipeline_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

This is **Level 2** of the verification taxonomy and **priority #4** on the
research roadmap. Most projects verify *instruction correctness* and stop;
pipeline-internal behaviour (forwarding, stalls, flushes, hazard detection) is
where in-order cores actually break. The architecturally-observable consequence
of a hazard-handling bug is a **wrong committed value** or a **wrong committed
program order** — and this agent detects those from the commit log, with an
*explainable* diagnosis, independently of the ISS.

---

## 2. The novel core — golden in-order ALU with forwarding diagnosis

For every RV32I ALU instruction whose operands are known, the result is
recomputed from the architectural (committed-in-order) register file. On a
mismatch the model does something a plain tandem-diff cannot: it **re-derives
the result using the un-forwarded stale operand** — the value a source register
held *before* its most recent write within the forwarding window. If the stale
value reproduces the committed (wrong) result, the bug is diagnosed precisely:

```
add x6, x5, x0   committed 0x5, golden 0xA
  └─ x5 was written 1 instruction earlier (5 → 10)
  └─ using the stale x5 (=5) reproduces the committed result
  ⇒ hazard_forwarding: the pipeline did not forward/stall the new x5
```

Otherwise it is reported as a generic `alu_result` mismatch. This turns
"assertion failed" into a root-caused, human-readable hazard explanation — the
Explainable-Verification idea applied to the pipeline.

**No error cascade:** the shadow register file is updated from the *committed*
value after each instruction, so a faulty instruction is flagged exactly once
and never poisons later checks; a correct trace produces zero violations.

---

## 3. Checks & metrics

| Check | Severity | Catches |
|---|---|---|
| `hazard_forwarding` | HIGH | ALU result explained by an un-forwarded stale operand (forwarding/stall failure) |
| `alu_result` | HIGH | ALU result wrong, not explained by a forwarding hazard |
| `control_hazard` | HIGH | `jalr`/`ret`/`jr` did not redirect to its computed target (flush / branch-recovery failure) |

**Metrics** (analytics — never fail the run, so always false-positive-free):

- **Hazard inventory**: RAW / WAR / WAW / control-transfer counts over the
  forwarding window.
- **Performance**: cycles, instret, IPC, CPI, stall cycles, pipeline
  utilisation — derived from `perf_counters` (handles both cumulative and
  per-instruction cycle counts).

---

## 4. Soundness & gating

- The golden ALU covers the deterministic RV32I R-type and I-type ops
  (`add/sub/and/or/xor/sll/srl/sra/slt/sltu` + immediates + `mv`); a check runs
  only when every source operand is available, so non-modelled or
  partially-observed instructions are skipped, never guessed.
- Writes to `x0` are ignored. Both ABI (`a0`, `sp`, `ra`, …) and `xN` register
  names are accepted in disassembly and in the `regs` map.
- The control-hazard check is restricted to `jalr`-family transfers whose target
  is computable from the register file (`ret`/`jr` included) — direct branches
  and jumps are counted as metrics only, avoiding any disassembly-format
  dependence.

---

## 5. Report & integration

`run()` returns the standard schema v2.1.0 report plus `alu_checked`, `hazards`,
and `perf`, band `CLEAN→CRITICAL`. Wired into `_run_extended_pipeline`
(`_pipeline` import, `EXTENDED_AGENTS_AVAILABLE`, writes `pipeline_report.json`,
records `reports["pipeline"]`) and exported from `AGENT_H/__init__.py`.
Standalone: `python AGENT_H/pipeline_verifier.py --rtl rtl_commit.jsonl`.

---

## 6. Test coverage

`tests/test_agents.py::TestPipelineVerifier` — 12 cases: golden ALU vector table,
clean multi-op pass, **forwarding-hazard diagnosis** (verifying the
expected/actual values), generic ALU mismatch, control-hazard ±, RAW hazard
inventory, perf-metric (CPI/stall) computation, malformed-input robustness,
report schema, manifest round-trip.

**Full suite: 256 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 7. Limitations / next steps

- **Loads/stores** are classified for the hazard inventory but their values are
  not recomputed (they depend on memory state); pairing with the existing
  memory-ordering agent (`AGENT_I` RVWMO) would extend RAW checking through
  load-use hazards.
- **Branch (conditional) recovery** is counted but not hard-checked; computing
  taken-ness from operands + a reliable target field would promote it to a hard
  `control_hazard` check.
- **Structural hazards** (port/unit contention) need micro-architectural signals
  beyond the architectural commit log — a natural fit for a future RTL-signal
  trace input.
- The forwarding window (default 5, a classic 5-stage depth) is configurable per
  core via the constructor.

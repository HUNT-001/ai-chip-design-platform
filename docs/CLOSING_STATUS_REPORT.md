# AVA — Closing Status Report

**Date:** 2026-07-19 · **Version:** 2.60.0
**Verification:** full repository sweep — **999 passed, 8 skipped, 0 failed**
(`pytest tests/ AGENT_C/ AGENT_D/ AGENT_E/ --import-mode=importlib`)

This report closes out the multi-batch development arc that took AVA from an
ISA-verification platform with a strong diagonal to a broad, honestly-scoped
verification platform. It records what was built, how each piece was validated,
and — just as importantly — what was **deliberately not faked**.

---

## 1. Headline

- **70 AGENT_H modules** (~30k lines) + AGENT_A/B/C/D/E core pipeline.
- **204 extended-tier tests across 32 classes**, all green, plus the base suite.
- **16 tagged releases** this arc (2.46.0 → 2.60.0).
- Every golden-reference agent validated against a **published standard vector**
  or **exhaustive/brute-force** check, not self-reference.
- The RTL-facing tooling validated against a **real 9-repo, 1,795-file corpus**
  (Ibex, CVA6, cv32e40p, VeeR EH1, BlackParrot, RSD, core-v-verif, riscv-dv).

---

## 2. What was built, by batch

### Crypto completion (T58–T63)
Vector AES (Zvkned), vector SHA-2 (Zvknh), vector SM3 (Zvksh), vector SM4
(Zvksed), the AES key schedule (scalar `aes64ks1i` + vector `vaeskf1/2`), and
vector GHASH (Zvkg). **Every golden validated against FIPS-197 / GB/T 32905 /
GB/T 32907 / NIST SP 800-38D / `hashlib`** — provably correct, not
self-referential.

### Consolidation
Fixed a real bug in `AGENT_C/run_iss.validate_commitlog` (hard-coded a stale
schema and would have rejected valid v2.1.0 commit logs) and migrated the stale
AGENT_C tests to v2.1.0.

### Batch 1 — the microarch frontier
Power-aware verification (level 15, previously unstarted), CDC/RDC checker
(AGENT_J upgrade), equivalence checker (AGENT_L upgrade, with an **exhaustive**
combinational proof engine).

### Batch 2 — interconnect levels 1–3
RTL basics (FSM/FIFO/memory), SoC peripherals (GPIO/SPI/I²C/timer/PWM), internal
buses (Wishbone/AXI-Lite/AXI-Stream/TileLink), advanced links
(PCIe/CXL/UCIe/CCIX/NVLink/OpenCAPI/Ethernet/NoC).

### Batch 3 — analytics & intelligence
Failure analytics (clustering/dedup/triage/trends), bug intelligence
(**Ochiai** spectrum-based localization, severity/lifetime/reopen prediction,
root-cause classification), regression intelligence (impact/selection/**LPT**
scheduling/health/cost), and six standalone HTML dashboards.

### Batch 4 — formal + AI
- **Formal core** — a real **DPLL SAT solver** + Tseitin CNF + **bounded model
  checking** (safety/liveness/reachability/deadlock/mutex), the solver
  validated against exhaustive brute force.
- **Formal analysis** — vacuity detection, COI reduction, proof cores,
  counterexample minimization, Daikon-style assertion mining.
- **RTL graph layer** — SystemVerilog parser → dataflow/FSM graphs, embeddings,
  clone detection; validated on the real corpus (17/17 `ibex_controller`
  transitions, 32-edge VeeR JTAG TAP, 0 false loops in `ibex_alu`).
- **AGENT_B** — RTL → full UVM + cocotb + smoke-SV verification environment
  synthesis (validated on cv32e40p/ibex ALU).
- **Verification digital twin** — live status, deterministic replay, what-if,
  coverage-closure forecasting (fits `Cmax·(1−e^(−t/τ))`), tape-out readiness.

---

## 3. Bugs that only real RTL / real vectors exposed

The value of testing against published vectors and real cores was concrete —
these were caught **before** they could ship as false results:

| Bug | Surfaced by | Consequence if shipped |
|---|---|---|
| Decrypt round order (vaes) | FIPS-197 round-trip test | silent wrong decryption |
| `run_iss` stale schema | full-suite consolidation | rejects valid commit logs |
| `for(` parsed as instance | Ibex ALU | 34 phantom instances |
| Truncated FSM case arms | Ibex controller | most transitions lost |
| 7 false combinational loops | Ibex ALU | loop checker cries wolf on good RTL |
| CVA6 "0 assertions" | CVA6 (has 572) | assertion coverage blind |
| VeeR JTAG TAP invisible | VeeR EH1 | whole FSM missed |
| 1,734 fake "clones" | CVA6 | absence-of-parse read as sameness |
| `first`→reset false match | ibex_alu ports | wrong reset in every generated TB |

Several of these (the false loops, the fake clones) are the same failure mode:
**a metric reporting a confident finding built on nothing.** Chasing them to
principled fixes rather than tuning thresholds is the difference between a demo
and a tool.

---

## 4. What was deliberately NOT faked

These need inputs this environment cannot supply. Each has a real interface and
is documented in `docs/DATA_AND_HARDWARE_REQUIREMENTS.md`:

- **Trained area/timing/power predictors** — need a Yosys/OpenSTA/OpenROAD
  label dataset. The RTL-graph feature layer they'd sit on is built; the model
  is not, and an untrained network emitting numbers would be a facade.
- **LLM-driven planning / RAG assistant** — need a runtime LLM endpoint.
- **FPGA / emulator / post-silicon correlation** — need hardware in the loop.
  The `silicon_sync` adapters are ready and return `awaiting_hardware` rather
  than fabricated data.
- **AGENT_B interface/struct bus classification** — a bounded extension (parse
  the interface definitions) to catch CVA6-style AXI-over-interface ports.

Design choices in the same spirit: BMC verdicts distinguish `bounded_proof`
from `proved`; coverage forecasting reports *unreachable* when the asymptote is
below goal; bug lifetime returns `None` when history is thin; test selection is
fail-safe (never silently skips an unknown test); the AGENT_B scoreboard's
reference model is a marked scaffold, not an invented golden.

---

## 5. Taxonomy scorecard (20 levels)

Start of arc: **6 Done / 8 Partial / 6 Planned.**
Now: **essentially all 20 covered** for the checker/analysis frontier, with the
residual gaps being the data/hardware/LLM items in §4 rather than missing
capability. See `docs/ROADMAP_AUDIT_2026-07.md` for the level-by-level detail
and `docs/CORPUS_SCOREBOARD.md` for the multi-core validation evidence.

---

## 6. How to reproduce

```bash
# full suite (pure-Python, no EDA tools needed)
pytest tests/ AGENT_C/ AGENT_D/ AGENT_E/ --import-mode=importlib -p no:cacheprovider -q

# RTL graph over a real core
python -m AGENT_H.rtl_graph --rtl-dir corpus/ibex_rtl --json /tmp/ibex.json

# generate a testbench from real RTL
python -m AGENT_B.testbench_generator --rtl corpus/cv32e40p/rtl/cv32e40p_alu.sv --out /tmp/tb
```

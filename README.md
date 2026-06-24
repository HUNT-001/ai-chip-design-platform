# AVA — Autonomic Verification Agent v3.0

[![CI](https://github.com/HUNT-001/ai-chip-design-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/HUNT-001/ai-chip-design-platform/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

A **multi-agent RISC-V RTL verification platform** powered by causal AI, formal methods, and a 14-agent extended pipeline. AVA takes a raw RTL spec and drives it through semantic analysis, tandem simulation, coverage-guided test generation, and a full extended verification suite — all without requiring a complete EDA toolchain for basic operation.

---

## Architecture

AVA runs a 6-phase pipeline. Phases 1–5 form the core loop (always active); Phase 6 is the extended verification suite that runs when optional agents are available.

```
RTL Spec
   │
   ▼
[Phase 1] Semantic Analysis      — AGENT_A: schema, DUT extraction
   │
   ▼
[Phase 2] Testbench Generation   — AGENT_B: ISS/RTL backend wiring
   │
   ▼
[Phase 3] Tandem Simulation      — AGENT_C: Spike ISS + Verilator RTL
   │                               AGENT_D: commit-log comparison
   ▼
[Phase 4] Bug Analysis           — AGENT_E: compliance checks
   │                               AGENT_F: coverage analysis
   ▼
[Phase 5] Coverage Adaptation    — AGENT_G: genetic + causal test gen
   │
   ▼
[Phase 6] Extended Verification  — 14 specialised AGENT_H modules (below)
   │
   ▼
Verification Report  (JSON / CSV / HTML)
```

### Agent Map

| Agent | Module | Task | EDA needed? |
|---|---|---|---|
| A | `AGENT_A/` | Schema validation, DUT semantic parsing | No |
| B | `AGENT_B/` | RTL/ISS backend wiring, Verilator build | Verilator |
| C | `AGENT_C/` | Spike ISS execution & commit-log capture | Spike |
| D | `AGENT_D/` | Commit-log comparison, bug hypothesis | No |
| E | `AGENT_E/` | RISC-V compliance test runner | GCC + Spike |
| F | `AGENT_F/` | Coverage analysis, cold-path ranking | No |
| G | `AGENT_G/` | Genetic + causal AI test generation | GCC (optional) |
| I | `AGENT_I/` | RVWMO memory-model validator (litmus) | No |
| J | `AGENT_J/` | CDC / reset / power checker | Yosys (optional) |
| K | `AGENT_K/` | Microarch performance collector | No |
| L | `AGENT_L/` | RTL→netlist equivalence checker | Yosys + sby |
| H-intent | `AGENT_H/agent_h_intent.py` | Architectural intent verification | No |
| H-contract | `AGENT_H/contract_dsl.py` | Design contract DSL (`@contract`) | No |
| H-temporal | `AGENT_H/temporal_checker.py` | LTL-style temporal property monitors | No |
| H-security | `AGENT_H/security_intel.py` | Spectre/privilege/cache covert-channel detection | No |
| H-causal | `AGENT_G/causal_engine.py` | Causal AI-guided test generation | GCC (optional) |
| H-minimize | `AGENT_H/minimizer.py` | Delta-debug counterexample minimizer | No |
| H-formal | `AGENT_H/formal_fuzzer.py` | SymbiYosys witness → assembly seeds | sby (optional) |
| H-twin | `AGENT_H/digital_twin.py` | Python micro-ISS for fast pre-screening | No |
| H-explain | `AGENT_H/explainer.py` | Human-readable bug explanations | No |
| H-rc | `AGENT_H/root_cause_localizer.py` | RTL root-cause localisation | No |
| H-kg | `AGENT_H/knowledge_graph.py` | Cross-campaign verification knowledge graph | No |
| H-econ | `AGENT_H/economics_engine.py` | Verification ROI / bugs-per-hour ledger | No |
| H-conf | `AGENT_H/confidence_scorer.py` | Weighted verification confidence score [0,1] | No |
| H-cross | `AGENT_H/cross_domain.py` | CRYPTO / DMA / UART DUT adapters | No |

### Verification Confidence Score

The confidence scorer aggregates evidence from all agents into a single score:

| Band | Score | Meaning |
|---|---|---|
| VERIFIED | ≥ 0.90 | Ready for sign-off |
| HIGH | ≥ 0.70 | Strong evidence, minor gaps |
| MEDIUM | ≥ 0.50 | Partial coverage, more testing advised |
| LOW | ≥ 0.30 | Significant gaps |
| CRITICAL | < 0.30 | Do not tape out |

---

## Quick Start

### Minimal (no EDA tools)

```bash
python ava_patched.py --rtl path/to/your_core.sv --microarch in_order
```

### Disable extended pipeline

```bash
python ava_patched.py --rtl core.sv --no-extended
```

### With RTL sources for root-cause analysis

```bash
python ava_patched.py \
  --rtl core.sv \
  --rtl-sources rtl/alu.sv rtl/decode.sv rtl/execute.sv \
  --microarch out_of_order
```

### Full run with custom coverage target

```bash
python ava_patched.py \
  --rtl core.sv \
  --target-cov 90.0 \
  --timeout 7200 \
  --formats json html
```

### Cross-domain DUT (e.g. crypto accelerator)

```python
from AGENT_H.cross_domain import get_adapter, DUTClass

adapter = get_adapter(DUTClass.CRYPTO)
canonical_log = adapter.translate(raw_dut_output)
# canonical_log is in AVA commit-log schema v2.1.0
```

---

## Installation

```bash
git clone https://github.com/HUNT-001/ai-chip-design-platform.git
cd ai-chip-design-platform
pip install -r requirements.txt
```

**Optional (for full pipeline):**

```bash
# RISC-V GCC toolchain
sudo apt install gcc-riscv64-unknown-elf

# Spike ISS
# https://github.com/riscv-software-src/riscv-isa-sim

# Verilator
sudo apt install verilator

# Yosys + SymbiYosys (for formal agents)
sudo apt install yosys
pip install yowasp-yosys
```

---

## Running Tests

```bash
# All pure-Python agent tests (no EDA tools needed)
pytest tests/test_agents.py -v

# Full suite including integration tests
pytest -v

# Skip the async orchestrator smoke test
pytest tests/test_agents.py -k "not test_ava_generate_suite_smoke" -v
```

---

## Schema

All agent communication uses **commit-log schema v2.1.0**. Key files:

- `AGENT_A/commitlog.schema.json` — per-instruction commit record
- `AGENT_A/run_manifest.schema.json` — per-campaign run manifest
- `AGENT_A/interfaces.md` — inter-agent contract documentation

---

## Repository Layout

```
ai-chip-design-platform/
├── ava_patched.py          # Main AVA v3.0 orchestrator
├── AGENT_A/                # Schema + semantic analysis
├── AGENT_B/                # RTL/ISS backends
├── AGENT_C/                # Spike ISS execution
├── AGENT_D/                # Commit-log comparator
├── AGENT_E/                # Compliance runner
├── AGENT_F/                # Coverage analysis
├── AGENT_G/                # Test generation (genetic + causal)
├── AGENT_H/                # Extended verification suite (14 modules)
├── AGENT_I/                # RVWMO memory-model validator
├── AGENT_J/                # CDC / reset checker
├── AGENT_K/                # Performance collector
├── AGENT_L/                # Equivalence checker
├── tests/                  # Pytest test suite (46 tests, pure-Python)
├── .github/workflows/      # GitHub Actions CI
├── requirements.txt        # Core dependencies
├── requirements-ml.txt     # Optional LLM/ML dependencies
├── pyproject.toml          # Build + pytest config
└── _legacy/                # Archived earlier versions (read-only)
```

---

## Capabilities Matrix

| Capability | Pure Python | + GCC | + Spike | + Verilator | + Yosys/sby |
|---|:---:|:---:|:---:|:---:|:---:|
| Schema validation | ✓ | ✓ | ✓ | ✓ | ✓ |
| Intent / contract / temporal | ✓ | ✓ | ✓ | ✓ | ✓ |
| Security intelligence | ✓ | ✓ | ✓ | ✓ | ✓ |
| Digital twin simulation | ✓ | ✓ | ✓ | ✓ | ✓ |
| Confidence scoring | ✓ | ✓ | ✓ | ✓ | ✓ |
| Causal test generation (no ELF) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Causal test generation (ELF) | | ✓ | ✓ | ✓ | ✓ |
| ISS tandem simulation | | | ✓ | ✓ | ✓ |
| RTL tandem simulation | | | | ✓ | ✓ |
| Equivalence checking | | | | | ✓ |
| Formal-guided fuzzing | | | | | ✓ |

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

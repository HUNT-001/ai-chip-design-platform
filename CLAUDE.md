# CLAUDE.md — AVA Verification Platform

This file gives AI assistants (Claude, etc.) the context needed to work
effectively in this codebase without re-reading every file.

---

## What this project is

**AVA v3.0** — Autonomic Verification Agent. A multi-agent RISC-V RTL
verification platform. Given an RTL spec, it runs a 6-phase pipeline:

1. **Semantic analysis** (AGENT_A) — schema validation, DUT module extraction
2. **Testbench generation** (AGENT_B) — Verilator/cocotb/UVM wiring
3. **Tandem simulation** (AGENT_C + AGENT_D) — Spike ISS vs RTL, commit-log diff
4. **Compliance** (AGENT_E) — RISC-V compliance test runner
5. **Coverage adaptation** (AGENT_F + AGENT_G) — UCB1 bandit + genetic/causal tests
6. **Extended verification** (AGENT_H modules + I/J/K/L) — 14 specialised agents

The main entry point is `ava_patched.py` — specifically the `AVA` class and
`generate_suite()` method.

---

## Key files

| File | Purpose |
|---|---|
| `ava_patched.py` | Main orchestrator — `AVA` class, 6-phase pipeline, `VerificationReportWriter` |
| `AGENT_G/causal_engine.py` | `CausalGeneticEngine` — causal AI-guided test generation |
| `AGENT_H/agent_h_intent.py` | `IntentChecker` — architectural intent verification |
| `AGENT_H/confidence_scorer.py` | `ConfidenceScorer` — weighted confidence score [0,1] |
| `AGENT_H/contract_dsl.py` | `ContractRunner` + `@contract` / `@for_instruction` decorators |
| `AGENT_H/temporal_checker.py` | `TemporalChecker` — LTL-style monitors over commit stream |
| `AGENT_H/security_intel.py` | `SecurityIntelligence` — Spectre/privilege/cache detection |
| `AGENT_H/economics_engine.py` | `EconomicsEngine` — bugs/hour, ROI, persistent ledger |
| `AGENT_H/cross_domain.py` | `get_adapter(DUTClass.CRYPTO/DMA/UART)` — non-CPU adapters |
| `AGENT_H/digital_twin.py` | `DigitalTwin` — Python micro-ISS for fast pre-screening |
| `AGENT_H/formal_fuzzer.py` | `FormalFuzzBridge` — SymbiYosys witness → assembly seeds |
| `AGENT_H/explainer.py` | `BugExplainer` — human-readable bug explanations |
| `AGENT_H/knowledge_graph.py` | `KnowledgeGraph` — cross-campaign bug DB (SQLite) |
| `AGENT_H/minimizer.py` | `CommitLogMinimizer` — delta-debug counterexample reduction |
| `AGENT_H/root_cause_localizer.py` | `RootCauseLocalizer` — RTL file-level root cause |
| `tests/test_agents.py` | 46 pure-Python pytest tests (no EDA tools needed) |
| `AGENT_A/commitlog.schema.json` | Commit-log record schema v2.1.0 |
| `AGENT_A/run_manifest.schema.json` | Per-run manifest schema v2.1.0 |
| `AGENT_A/interfaces.md` | Inter-agent contract documentation |

---

## Schema v2.1.0

Every agent communicates via two JSON schemas:

**Commit-log record** (`commitlog.schema.json`):
```json
{
  "schema_version": "2.1.0",
  "seq": 0,
  "pc": "0x80000000",
  "disasm": "addi x1,x0,1",
  "regs": {"x1": "0x00000001"},
  "csrs": {"mstatus": "0x00001800"},
  "mem_reads":  [{"addr": "0x...", "size": 4, "value": "0x..."}],
  "mem_writes": [{"addr": "0x...", "size": 4, "value": "0x..."}],
  "trap": {"cause": 11, "tval": "0x0"},
  "perf_counters": {"cycles": 10, "instret": 1}
}
```

**Run manifest** (`run_manifest.json` written to each run dir):
```json
{
  "schema_version": "2.1.0",
  "run_id": "...",
  "run_dir": "/path/to/run",
  "status": "running|completed|fail",
  "started_at": "2026-01-01T00:00:00Z",
  "isa": "rv32im",
  "dut_module": "riscv_core",
  "metrics": {"total_commits": 0, "total_mismatches": 0},
  "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}
}
```

---

## Architecture rules

1. **Never delete files** — move to `_legacy/` instead. The user is explicit about this.
2. **All new agents use graceful degradation** — wrap EDA tool calls in `try/except`,
   return `{"status": "skipped", "reason": "..."}` when tools are missing.
3. **`_try_import()` pattern** in `ava_patched.py` — all Phase 6 agent modules are
   lazily imported so missing modules never crash the base pipeline.
4. **Schema version** — always `"2.1.0"` in new output dicts.
5. **All AGENT_* dirs are Python packages** — `__init__.py` present in all of them.

---

## Running tests

```bash
# Fast (no EDA tools, ~0.5s)
pytest tests/test_agents.py --import-mode=importlib -p no:cacheprovider -q

# Or via Makefile
make test       # pure-Python tests
make smoke      # AVA orchestrator end-to-end
make lint       # syntax check all .py files
```

---

## Confidence score bands

| Band | Score | Meaning |
|---|---|---|
| VERIFIED | ≥ 0.90 | Ready for sign-off |
| HIGH | ≥ 0.70 | Strong evidence, minor gaps |
| MEDIUM | ≥ 0.50 | Partial coverage |
| LOW | ≥ 0.30 | Significant gaps |
| CRITICAL | < 0.30 | Do not tape out |

---

## Common tasks

**Add a new AGENT_H module:**
1. Create `AGENT_H/my_module.py` with a main class and `run_from_manifest(path)` fn
2. Add `_my_module = _try_import("AGENT_H.my_module", "MyModule")` in `ava_patched.py`
3. Call it inside `_run_extended_pipeline()` with a `try/except` block
4. Add it to `EXTENDED_AGENTS_AVAILABLE` check
5. Add tests in `tests/test_agents.py`

**Add a new cross-domain DUT adapter:**
```python
from AGENT_H.cross_domain import DUTAdapter, DUTClass, register_adapter

class MyAdapter(DUTAdapter):
    dut_class = DUTClass.CUSTOM
    name = "my_adapter"
    def translate_record(self, raw, seq):
        rec = self._base_record(seq)
        rec["disasm"] = raw.get("op", "nop")
        return rec

register_adapter(DUTClass.CUSTOM, MyAdapter)
```

**Generate all report formats:**
```bash
python ava_patched.py --rtl core.sv --formats json csv html
```

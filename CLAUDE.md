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
| `AGENT_H/coherence_verifier.py` | `CoherenceVerifier` — multicore cache-coherence (read_from_valid: no fabricated load values; coherence_read_monotonic: per-core reads non-decreasing in the global per-address write order = write serialization; swmr: single-writer/multiple-reader from MESI state). Golden checks over a `coherence_trace.jsonl` multicore event stream |
| `AGENT_H/coverage_collector.py` | `CoverageCollector` + `classify_value` — functional coverage from the commit log (reg-write/value-class/branch-dir/priv/instr bins with finite universes → holes + importance weights; CSR/trap/vtype telemetry). Emits `coverage_summary.json` consumed by `self_evolving_engine`, closing the RL loop |
| `AGENT_H/contract_dsl.py` | `ContractRunner` + `@contract` / `@for_instruction` decorators |
| `AGENT_H/temporal_checker.py` | `TemporalChecker` — LTL-style monitors over commit stream |
| `AGENT_H/atomics_verifier.py` | `AtomicsVerifier` — RV32A golden-reference checker (LR/SC + 9 AMO ops) |
| `AGENT_H/csr_verifier.py` | `CSRVerifier` — Zicsr/Zifencei semantics (CSR RMW, read-only enforcement, FENCE.I sync) |
| `AGENT_H/rvc_verifier.py` | `RVCVerifier` — RV32C checks (PC+2 stride, reserved encodings, x8-x15 prime fields) |
| `AGENT_H/fp_verifier.py` | `FPVerifier` — RV32F/D golden IEEE-754 checker (NaN-boxing, RNE arithmetic, sgnj/min-max/compare/fclass/fmv, fflags) |
| `AGENT_H/bitmanip_verifier.py` | `BitmanipVerifier` — RV32B golden checker (Zba/Zbb/Zbc/Zbs: shadd, clz/ctz/cpop, rol/ror, clmul, b{set,clr,ext,inv}) |
| `AGENT_H/privilege_verifier.py` | `PrivilegeVerifier` — privilege transitions + PMP (xRET/CSR legality, ECALL cause, MRET target, PMP region permission/access-fault model) |
| `AGENT_H/vm_verifier.py` | `Sv32MMU` + `VMVerifier` — golden Sv32 page-table walker (4KB/4MB, permissions, faults) and translation/page-fault checker |
| `AGENT_H/tlb_verifier.py` | `TLBVerifier` — TLB coherence + sfence.vma (golden TLB over Sv32MMU: stale-after-sfence, incoherent/fabricated translation, scoped invalidation) |
| `AGENT_H/pipeline_verifier.py` | `PipelineVerifier` — pipeline hazards (golden in-order ALU forwarding/stall diagnosis, control-hazard, RAW/WAR/WAW inventory, IPC/CPI/stall metrics) |
| `AGENT_H/branch_predictor_verifier.py` | `BranchPredictorVerifier` — Level-7 branch prediction (bp_recovery: operand-derived outcome vs committed next-PC, bp_hit_flag, accuracy/mispredict/MPKI + golden RAS return-prediction metrics) |
| `AGENT_H/vector_verifier.py` | `VectorVerifier` + `decode_vtype`/`vlmax`/`velem_compute` — RISC-V Vector "V"/RVV (vset_vl: spec-accurate vl from AVL/VLEN/SEW/LMUL incl. fractional LMUL + impl-defined band; vtype_vill; velem: element-wise golden SEW-width ALU across active/unmasked elements for vadd/sub/logic/shift/mul/min-max/merge/mv .vv/.vx/.vi; vtail undisturbed; vmem: golden per-element load/store addressing for unit/strided/indexed + access-count/EEW/value checks; SEW/LMUL/vl metrics) |
| `AGENT_H/cache_verifier.py` | `CacheModel` + `CacheVerifier` — golden set-associative cache (LRU/FIFO, WB/WT) checking hit/miss, eviction victim, dirty write-back, line integrity + miss-rate metrics |
| `AGENT_H/bus_verifier.py` | `BusVerifier` + `axi_expected_beats` — AXI4/AHB/APB protocol checks (burst length, WLAST, beat-addr FIXED/INCR/WRAP, 4KB boundary, WRAP alignment, response codes) |
| `AGENT_H/fault_injector.py` | `FaultCampaign` + `inject_fault` — fault-injection / mutation testing of the verification suite (bit-flip/stuck-at/corruption models, detection-rate & blind-spot reporting) |
| `AGENT_H/rv64_verifier.py` | `RV64Verifier` — RV64 datapath (64-bit `alu64` + W-ops `aluw` with 32→64 sign-extension, `rv64_word_sext` diagnosis; auto-detects RV64, no-op on RV32) |
| `AGENT_H/sv_mmu_verifier.py` | `SvMMU` + `SvMMUVerifier` — golden Sv39/Sv48 multi-level page-table walker (4KB/2MB/1GB superpages, non-canonical VA, permissions) for RV64 virtual memory |
| `AGENT_H/rv64_atomics_verifier.py` | `RV64AtomicsVerifier` + `amo_compute64` — RV64 64-bit atomics (LR.D/SC.D + 9 AMO.D ops, golden signed/unsigned 64-bit math, reservation, 8-byte alignment) |
| `AGENT_H/stimulus_generator.py` | `StimulusGenerator` + `generate_from_holes` — coverage-directed RISC-V stimulus: hole/constraint → concrete instruction seed (asm + golden commit records), self-validating via `coverage_collector`. `make_env`/`close_coverage` provide real generate/evaluate plugins so `self_evolving_engine` closes coverage with generated stimulus; `run_from_manifest` emits `stimulus.json` from coverage holes |
| `AGENT_H/self_evolving_engine.py` | `SelfEvolvingEngine` + pluggable **non-stationary** bandits (`UCB1`/`DiscountedUCB1`/`SlidingWindowUCB`/`ThompsonSampling` via `make_policy`) + `constraint_for` escalation ladder + `CoverageState` (importance-weighted, novelty) + `run_campaign` (multi-seed mean±CI) — RL coverage-closure loop (difficulty-aware hole scheduler, suspected-unreachable waivers, weighted+novelty reward, regret/velocity/closure-prediction metrics; offline planner via `plan_from_coverage`/`run_from_manifest`) |
| `AGENT_H/security_intel.py` | `SecurityIntelligence` — Spectre/privilege/cache detection |
| `AGENT_H/economics_engine.py` | `EconomicsEngine` — bugs/hour, ROI, persistent ledger |
| `AGENT_H/cross_domain.py` | `get_adapter(DUTClass.CRYPTO/DMA/UART)` — non-CPU adapters |
| `AGENT_H/peripheral_verifier.py` | `PeripheralVerifier` — DMA/UART/CRYPTO protocol checkers (reference model + scoreboard) |
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

# AVA — Master Status & Roadmap

**Autonomic Verification Agent (AVA) v3.0**
*A multi-agent, AI-assisted RISC-V RTL verification platform*

Last updated: 2026-07-09 · Pure-Python core (no EDA tools required to run the agent tier)

Status legend: ✅ **Done** (implemented + tested) · 🟡 **Partial** (foundation exists / partially covered) · ⬜ **Planned**

---

## 1. Executive summary

AVA is not a conventional testbench — it is an **autonomous, multi-agent
verification engineer**. Given an RTL spec it runs a six-phase pipeline that
analyses the design, generates a testbench, runs tandem simulation against a
golden ISS (Spike), drives compliance, adapts coverage with AI, and then runs a
research tier of **30+ specialised agents** that each verify one slice of the
architecture from the canonical commit log.

This document inventories everything built, maps it honestly against the
**20-level verification taxonomy** and the **17 advanced research ideas**, and
lays out a sequenced roadmap for the rest.

**Headline status:** the ISA + memory-management + security-architecture spine
is now substantially complete (Levels 1, 6, 11 and large parts of 13/14/17),
including this development cycle's nine new golden-reference agents covering the
A, C, Zicsr/Zifencei, F/D, and B extensions plus PMP, Sv32 virtual memory and
TLB coherence. The microarchitectural tiers (pipeline, cache, branch predictor,
out-of-order, multicore/coherency — Levels 2, 3, 7, 8, 9, 10) and the
fault-injection / power / bus-protocol tiers (Levels 5, 12, 15) are the main
open frontier.

---

## 1a. Development cycle 2 — T39→T51 (2026-07)

A second build arc added **13 new golden-reference agents** (all stdlib-only,
each with a standalone + in-repo pytest suite), closing the microarchitectural,
multicore-memory, interrupt-architecture and system-integration frontiers, and
turning the AI coverage tier into a genuine closed loop.

**Micro-architecture**
- ✅ **Branch prediction** (T39, `branch_predictor_verifier`) — recovery
  (operand-derived outcome vs committed next-PC), hit-flag, accuracy/MPKI +
  golden RAS metrics.
- ✅ **Vector "V" / RVV** (T41, `vector_verifier`) — `vset_vl` (spec-accurate vl
  incl. fractional LMUL + impl-defined band), `vill`, element-wise golden ALU
  (`.vv/.vx/.vi`), tail policy, and vector load/store addressing
  (unit/strided/indexed + access-count/EEW/value).

**AI self-evolving coverage/generation loop (advanced idea #3, now closed)**
- ✅ **Self-evolving engine** (T40) — non-stationary bandits
  (UCB1/Discounted-UCB/Sliding-Window/Thompson), difficulty-aware hole
  scheduler, suspected-unreachable waivers, regret/velocity/closure-prediction,
  multi-seed CI.
- ✅ **Functional coverage collector** (T42) — reg/value-class/branch/priv/instr
  + **cross** (opcode×result) + **opnd** (source-operand pair via a golden
  shadow regfile) + **coherence** + **consistency** bins; emits the
  `coverage_summary.json` the engine consumes.
- ✅ **Coverage-directed stimulus generator** (T43) — turns holes into concrete,
  **self-validating** seeds; `close_coverage()` drives the loop to ≥90% over a
  166-bin universe with generated stimulus.

**Multicore memory stack**
- ✅ **Cache coherence** (T44, `coherence_verifier`) — read-from-valid,
  write-serialization (per-core monotonic observation), SWMR from MESI state;
  plus coherence *coverage* bins into the loop.
- ✅ **Memory consistency** (T45, `memory_model_verifier`) — axiomatic
  SC/TSO/RVWMO (po/ppo/rf/co/fr + fences), acquire/release, fence pred/succ
  sets, RMW atomicity; litmus-validated (SB/MP/LB/CoRR); consistency coverage
  into the loop.

**Interrupt architecture** — PLIC + CLINT + CLIC + AIA/IMSIC
- ✅ **PLIC/CLINT/CLIC** (T46, `interrupt_verifier`) — priority arbitration,
  threshold/priority-0 masking, CLINT mtip/msip, CLIC level/priority fast
  interrupts.
- ✅ **AIA / IMSIC** (T51, `aia_verifier`) — `topei` selection (smallest
  identity = highest priority, eithreshold, eidelivery).

**System integration**
- ✅ **Performance counters** (T47, `perf_counter_verifier`) — minstret/mcycle
  increment + `mcountinhibit`, IPC/CPI (runs on the standard commit log).
- ✅ **Debug & Trigger** (T48, `debug_verifier`) — mcontrol trigger match,
  dcsr.cause/dpc, single-step, abstract commands.
- ✅ **Reset state** (T49, `reset_verifier`) — mandated reset invariants
  (priv=M, MIE/MPRV=0), reset PC, misa sanity, golden CSR compare.
- ✅ **Hypervisor two-stage translation** (T50, `hypervisor_verifier`) —
  VS-stage→G-stage composition; VS fault = page-fault (12/13/15), G fault =
  guest-page-fault (20/21/23).

**Still open:** APLIC + guest interrupt files (AIA depth); full interleaved
two-stage page-table walk (H depth); out-of-order/scoreboard microarchitecture;
power/CDC (AGENT_J foundation) and DFT/formal-property tiers.

---

## 2. Architecture — the six-phase pipeline

The orchestrator is `ava_patched.py` (`AVA` class, `generate_suite()`).
Every agent communicates through two versioned JSON schemas
(`commitlog.schema.json`, `run_manifest.schema.json`, both v2.1.0), which is
what makes the platform modular and portable.

```
   RTL spec
      │
  [1] AGENT_A   Semantic analysis      schema validation, DUT extraction
      │
  [2] AGENT_B   Testbench generation   Verilator / cocotb / UVM wiring
      │
  [3] AGENT_C + AGENT_D   Tandem simulation   Spike ISS vs RTL, commit-log diff
      │
  [4] AGENT_E   Compliance             RISC-V architectural test runner
      │
  [5] AGENT_F + AGENT_G   Coverage adaptation   UCB1 bandit + genetic/causal AI test gen
      │
  [6] AGENT_H/I/J/K/L     Extended verification   30+ specialised research agents
      │
   JSON / CSV / HTML reports + confidence score + economics ledger
```

Design rules: never delete (move to `_legacy/`), graceful degradation (every
EDA call wrapped, missing tools → `{"status":"skipped"}`), lazy `_try_import`
of every Phase-6 agent so a missing module never breaks the base pipeline.

---

## 3. Complete module inventory

### Core pipeline (Phases 1–5)

| Module | Capability | Status |
|---|---|---|
| `ava_patched.py` | Orchestrator, 6-phase pipeline, report writers, confidence bands | ✅ |
| `AGENT_A/` | Schema validation, DUT module extraction, inter-agent contracts | ✅ |
| `AGENT_B/` | Testbench generation (Verilator / cocotb / UVM) | 🟡 |
| `AGENT_C/` (`spike_parser`, `run_iss`, `iss_efficiency`) | Spike ISS execution + commit-log parsing | ✅ |
| `AGENT_D/` (`compare_commitlogs`, `bug_hypothesis`) | Tandem commit-log diff + bug hypothesis | ✅ |
| `AGENT_E/` (`run_compliance`, `run_rtl_adapter`) | RISC-V compliance test runner | ✅ |
| `AGENT_F/` (`coverage_pipeline`, `coverage_database`, `cold_path_ranker`, `manifest_lock`) | Verilator coverage backend, UCB1 cold-path ranker, coverage DB | 🟡 |
| `AGENT_G/` (`causal_engine`, `genetic_engine`, `random_gen`, `directed_tests`, `asm_builder`) | AI-guided / genetic / random / directed test generation | ✅ |

### Research tier — AGENT_H (and I/J/K/L)

| Module | Capability | Status |
|---|---|---|
| `AGENT_I/agent_i_litmus` | RVWMO weak-memory-model litmus validator | ✅ |
| `AGENT_J/agent_j_cdc` | Clock-domain-crossing / reset checker | 🟡 |
| `AGENT_K/agent_k_perf` | Performance counters (IPC, stalls, cycles) | 🟡 |
| `AGENT_L/agent_l_equiv` | Equivalence checker | 🟡 |
| `AGENT_H/agent_h_intent` | Architectural **intent** verification (7 specs) | ✅ |
| `AGENT_H/contract_dsl` | Design-contract DSL (`@contract`, `@for_instruction`) | ✅ |
| `AGENT_H/temporal_checker` | LTL-style temporal property monitors | ✅ |
| `AGENT_H/confidence_scorer` | Weighted confidence score → VERIFIED…CRITICAL bands | ✅ |
| `AGENT_H/security_intel` | Spectre / privilege / cache covert-channel detection | ✅ |
| `AGENT_H/economics_engine` | Bugs/hour, ROI, persistent ledger | ✅ |
| `AGENT_H/digital_twin` | Python micro-ISS for fast pre-screening | 🟡 |
| `AGENT_H/formal_fuzzer` | SymbiYosys witness → assembly-seed bridge | 🟡 |
| `AGENT_H/explainer` | Human-readable bug explanations | ✅ |
| `AGENT_H/knowledge_graph` | Cross-campaign bug knowledge graph (SQLite) | ✅ |
| `AGENT_H/minimizer` | Delta-debug counterexample minimiser | ✅ |
| `AGENT_H/root_cause_localizer` | RTL file-level root-cause localisation | ✅ |
| `AGENT_H/cross_domain` | CRYPTO / DMA / UART DUT adapters | ✅ |

### Research tier — added this development cycle (T23–T31)

| Module | Capability | Tests | Status |
|---|---|---|---|
| `AGENT_H/atomics_verifier` | **RV32A** golden checker — LR/SC reservation + 9 AMO ops, alignment, signed/unsigned | 11 | ✅ |
| `AGENT_H/peripheral_verifier` | **SoC peripherals** — DMA byte-conservation, UART framing/parity, CRYPTO SHA-256 KAT + AES round-trip | 15 | ✅ |
| `AGENT_H/csr_verifier` | **Zicsr/Zifencei** — CSR RMW, read-only enforcement, FENCE.I instruction-stream sync | 12 | ✅ |
| `AGENT_H/rvc_verifier` | **RV32C** — PC+2 stride, reserved encodings, x8–x15 prime-field rule | 11 | ✅ |
| `AGENT_H/fp_verifier` | **RV32F/D** golden IEEE-754 — NaN-boxing, RNE arithmetic, sgnj/min-max/compare/fclass/fmv, fflags | 14 | ✅ |
| `AGENT_H/bitmanip_verifier` | **RV32B** (Zba/Zbb/Zbc/Zbs) — shadd, clz/ctz/cpop, rol/ror, clmul, single-bit | 34 | ✅ |
| `AGENT_H/privilege_verifier` | **Privilege + PMP** — xRET/CSR legality, ECALL cause, MRET target, PMP region model | 12 | ✅ |
| `AGENT_H/vm_verifier` | **Sv32 virtual memory** — golden `Sv32MMU` page-table walker + translation/fault checker | 18 | ✅ |
| `AGENT_H/tlb_verifier` | **TLB coherence** — stale-after-`sfence.vma`, incoherent/fabricated translation, scoped invalidation | 8 | ✅ |

Every new agent follows the same contract: a golden software reference, a
commit-log checker, conservative gating (skip rather than false-positive), a
schema v2.1.0 report with a `CLEAN→CRITICAL` severity band, a
`run_from_manifest()` pipeline hook, and a per-module design doc under `docs/`.

---

## 4. Coverage against the 20-level taxonomy

| Level | Area | Status | Where it lives / what's missing |
|---|---|---|---|
| **1** | ISA verification | ✅ (Vector ⬜) | Integer/branches/jumps via tandem diff; **A** `atomics_verifier`; **C** `rvc_verifier`; **CSR** `csr_verifier`; **F/D** `fp_verifier`; **B** `bitmanip_verifier`; privilege modes `privilege_verifier`; exceptions/interrupts via `temporal_checker`+privilege. **Vector (V)** not started. Coverage: code/toggle/branch via Verilator backend 🟡 |
| **2** | Pipeline (RAW/WAR/WAW, stalls, forwarding, flush, IPC) | 🟡 | Hazard hints in `temporal_checker`, IPC/stalls in `AGENT_K`. No dedicated hazard/forwarding verifier yet |
| **3** | Cache (L1/L2/victim, LRU/FIFO, hit/miss/evict) | ⬜ | Only cache **side-channel** detection in `security_intel`; no functional cache model |
| **4** | Memory system (SRAM/DRAM/AXI/AHB/APB, latency, arbitration) | 🟡 | Peripheral-level via `peripheral_verifier`/`cross_domain`; bus timing/arbitration ⬜ |
| **5** | Bus protocol (AXI4/Lite/Stream, AHB, APB, Wishbone, TileLink) | ⬜ | Handshake/burst/backpressure/outstanding not modelled |
| **6** | **MMU** (page walk, TLB, faults, superpages, permissions) | ✅ | `vm_verifier` (golden Sv32MMU, 4KB/4MB, invalid/permission/misaligned faults) + `tlb_verifier` + PMP. Sv39/Sv48 pending RV64 |
| **7** | Branch predictor (BTB/BHT/RAS/tournament, accuracy) | ⬜ | Not started |
| **8** | Out-of-order (rename, RS, ROB, commit, precise exceptions) | ⬜ | `AGENT_I` RVWMO is the memory-ordering foundation; microarch OoO ⬜ |
| **9** | Multicore (shared memory, sync, locks, atomics) | 🟡 | `atomics_verifier` is single-hart; `AGENT_I` RVWMO is the basis. Needs `hart_id` in schema |
| **10** | Cache coherency (MSI/MESI/MOESI/MESIF) | ⬜ | Not started (depends on multicore) |
| **11** | **Security** (PMP, isolation, privilege escalation, CSR protection, illegal-instr, speculative) | ✅ (glitch/rowhammer ⬜) | `privilege_verifier` (PMP, escalation, CSR, illegal) + `security_intel` (Spectre/speculative/cache covert). Physical fault-injection ⬜ |
| **12** | Fault injection (bit-flip, stuck-at, corruption, detection rate) | ⬜ | Not started |
| **13** | **AI-assisted** (RL test gen, coverage prioritisation, clustering, minimisation, LLM debug) | ✅🟡 | `causal_engine` + `genetic_engine` (AI test gen); `AGENT_F` UCB1 bandit (coverage-guided prioritisation); `minimizer` (test minimisation); `explainer` (debug summaries); `knowledge_graph`. Failure **clustering** + bug **prediction** 🟡 |
| **14** | Formal (SVA, property/equivalence/BMC, liveness/safety) | 🟡 | `formal_fuzzer` (SymbiYosys bridge), `AGENT_L` (equivalence), `contract_dsl` + `temporal_checker` (properties). Full SVA/BMC closure 🟡 |
| **15** | Power-aware (clock gating, sleep, DVFS, power domains) | ⬜ | Not started |
| **16** | Performance (IPC/CPI, miss rate, throughput, dashboards) | 🟡 | `AGENT_K` perf + `economics_engine`; automated dashboards 🟡 |
| **17** | **Compliance** (official arch tests, random gen, differential, CI) | ✅ | `AGENT_E` runner + tandem **differential vs Spike** + `AGENT_G` random/directed gen + GitHub Actions CI |
| **18** | Portable (multiple RTL impls, modular interfaces/scoreboards) | 🟡 | Schema-driven design + `cross_domain` adapters make this feasible; only one core family wired so far |
| **19** | Complete SoC (UART/GPIO/SPI/I²C/DMA/timers/debug) | 🟡 | DMA/UART/CRYPTO done in `peripheral_verifier`; GPIO/SPI/I²C/timers/debug ⬜ |
| **20** | Production platform (UVM, CRV, all coverage, diff, regression mgmt, AI, formal, dashboards, CI) | 🟡 | Many pillars present; the open levels above are what remains for "production-grade" |

**Scorecard:** of 20 levels — **6 Done**, **8 Partial**, **6 Planned**. The
"baseline + memory-management + security + compliance + AI" diagonal is strong;
the microarchitecture and physical-fault frontiers are the growth area.

---

## 5. Coverage against the 17 advanced research ideas

A pleasant surprise: most of the "novel" ideas already have a working basis in
AVA — this platform was architected as exactly the "autonomous verification
engineer" the first idea describes.

| # | Idea | Status | Where it lives |
|---|---|---|---|
| 1 | **Autonomous Verification Engineer** (analyse→plan→generate→run→analyse→improve) | ✅ | This *is* AVA — the 6-phase multi-agent pipeline with the causal engine closing the loop |
| 2 | **Digital Twin** of the processor | 🟡 | `digital_twin.py` (micro-ISS pre-screen). Occupancy/power/temperature modelling ⬜ |
| 3 | **Self-Evolving / RL verification** (coverage-hole → constraint → test → reward) | 🟡 | `causal_engine` + `AGENT_F` UCB1 bandit reward loop; full RL agent ⬜ |
| 4 | **Verification Knowledge Graph** | ✅ | `knowledge_graph.py` (SQLite, cross-campaign bugs) |
| 5 | **Intent-Driven Verification** | ✅ | `agent_h_intent.py` + `contract_dsl.py` |
| 6 | **Explainable Verification** | ✅ | `explainer.py` (failure → root-cause narrative) |
| 7 | **Large-Scale Regression Intelligence** (failure clustering) | 🟡 | `knowledge_graph` + `minimizer`; ML clustering of failures ⬜ |
| 8 | **Cross-Core Differential** | 🟡 | Tandem-vs-Spike + `cross_domain`; multi-core fan-out ⬜ |
| 9 | **Hardware "Time Travel"** (record/rewind) | 🟡 | `minimizer` delta-debug; full reversible replay ⬜ |
| 10 | **Natural-Language Verification** | 🟡 | `contract_dsl` + intent specs; NL→assertion synthesis ⬜ |
| 11 | **Multi-Agent Ecosystem** | ✅ | The A–L + AGENT_H agent fleet with narrow responsibilities |
| 12 | **Verification Operating System** | 🟡 | The orchestrator + manifest + ledger + knowledge graph; cloud/asset mgmt ⬜ |
| 13 | **GNN for RTL** | ⬜ | Not started |
| 14 | **Bug Prediction before simulation** | 🟡 | `digital_twin` pre-screen + `knowledge_graph` priors; trained ML predictor ⬜ |
| 15 | **Risk-Based Verification** | 🟡 | `confidence_scorer` bands + `cold_path_ranker`; explicit per-module risk score ⬜ |
| 16 | **Continuous-Learning Platform** | 🟡 | Persistent `knowledge_graph` + `economics` ledger; cross-project reuse ⬜ |
| 17 | **Autonomous Verification Research Platform** (the grand vision) | 🟡 | AVA is a concrete instance of this diagram; maturing each agent is the path |

---

## 6. Mapping to the priority table

| Pri | Extension | Status in AVA |
|---|---|---|
| 1 | Differential testing vs golden reference | ✅ Tandem Spike-vs-RTL (Phase 3) + every new agent is a golden-reference checker |
| 2 | AI-driven constrained-random test gen | ✅🟡 `causal_engine` + `genetic_engine` + UCB1 bandit; deeper RL ⬜ |
| 3 | Formal verification with SVA | 🟡 `formal_fuzzer` + `contract_dsl` + `temporal_checker`; native SVA/BMC closure ⬜ |
| 4 | Pipeline & hazard verification | 🟡 Hints in `temporal_checker`/`AGENT_K`; dedicated agent ⬜ (recommended next) |
| 5 | Functional coverage closure automation | 🟡 `AGENT_F` cold-path ranker + coverage DB; closed-loop automation 🟡 |
| 6 | Cache & memory subsystem | ⬜ (recommended) |
| 7 | AXI/AHB/APB protocol | ⬜ (recommended) |
| 8 | Security & fault injection | ✅🟡 Security architectural done; fault-injection ⬜ |
| 9 | Multicore & cache coherency | 🟡/⬜ RVWMO foundation; coherency ⬜ |
| 10 | AI-assisted debug & regression analytics | ✅🟡 `explainer` + `knowledge_graph` + `root_cause_localizer`; analytics dashboards 🟡 |

---

## 7. Test & quality status

- **244 tests passing, 1 skipped** (`tests/test_agents.py`), pure-Python,
  ~0.4 s — no EDA tools required.
- Every new agent ships with: golden-reference validation (e.g. 28 hand-computed
  bit-manip vectors; hand-built Sv32 page tables; IEEE-754 reference math),
  bug-injection tests, a robustness battery (empty / non-dict / partial inputs),
  report-schema consistency, and a manifest round-trip.
- Cross-agent: a single clean mixed commit log must pass every commit-log
  verifier; all agents share the common report schema.
- `compileall` clean across all twelve `AGENT_*` packages; the live
  `python ava_patched.py` self-test passes with all agents wired.
- Hardening: every commit-log verifier guards against malformed records;
  conservative gating throughout prevents false positives on traces that lack
  optional fields.

---

## 8. Roadmap — what to build next

Sequenced by *feasibility × impact*, reusing the proven golden-checker pattern.
Items marked **(no schema change)** drop in like the recent agents; items marked
**(schema)** need an additive field.

**Near-term, high-leverage (continue the current cadence):**

1. **Pipeline & hazard verifier** *(no schema change)* — RAW/WAR/WAW + forwarding
   + flush correctness from the commit stream; complements `AGENT_K` IPC. *(Priority #4.)*
2. **Vector (RVV) verifier** *(schema: vector regs)* — completes the ISA column (Level 1).
3. **Bus-protocol verifier** *(schema: bus transactions)* — AXI4/AHB/APB
   handshake/ordering/burst/outstanding (Levels 4–5, Priority #7).
4. **Cache model verifier** *(schema: cache events)* — L1/L2 hit/miss/replacement/
   write-back vs a golden cache (Level 3, Priority #6).

**Strategic:**

5. **RV64 widening** *(schema + every verifier parameterised by XLEN)* — unlocks
   Sv39/Sv48 (drop into the existing MMU), 64-bit datapath, the Linux-class core tier.
6. **Multicore + cache coherency** *(schema: hart_id)* — extends `atomics_verifier`
   and `AGENT_I` RVWMO to true concurrency + MSI/MESI/MOESI (Levels 9–10, Priority #9).
7. **Fault-injection campaign engine** — bit-flip / stuck-at / corruption with
   detection-rate metrics (Level 12, Priority #8).

**AI / research distinctiveness (build on existing foundations):**

8. **Self-evolving RL test loop** — formalise the coverage-hole → constraint →
   reward loop around `causal_engine` (idea #3, Priority #2).
9. **Failure-clustering + bug-prediction analytics** over the `knowledge_graph`
   (ideas #7, #14; Priority #10).
10. **Live metrics/coverage dashboards** + regression DB for CI (Levels 16/20).

**Deployment / market readiness:**

11. **Regression database + JIRA/Linear ticketing** and **Grafana/Prometheus**
    exporters — turn runs into tracked, gated team CI.

---

## 9. How to run & extend

```bash
# Fast pure-Python agent tests (no EDA tools)
pytest tests/test_agents.py --import-mode=importlib -q

# Full pipeline on an RTL spec, all report formats
python ava_patched.py --rtl core.sv --formats json csv html

# A single new agent, standalone
python AGENT_H/vm_verifier.py --rtl rtl_commit.jsonl
```

**Adding a new AGENT_H verifier** (the established recipe): create
`AGENT_H/<name>.py` with a golden model, a checker `run()` returning the
schema-v2.1.0 report, and `run_from_manifest()`; export it from
`AGENT_H/__init__.py`; add `_<name> = _try_import(...)` + an `EXTENDED_AGENTS`
entry + a `_call` block in `_run_extended_pipeline`; add a `Test<Name>` class to
`tests/test_agents.py`; write `docs/DESIGN_<name>.md`. Every agent built this
cycle followed exactly this recipe.

---

*Per-agent design notes live alongside this file: `docs/DESIGN_atomics_verifier.md`,
`DESIGN_peripheral_verifier.md`, `DESIGN_csr_verifier.md`, `DESIGN_rvc_verifier.md`,
`DESIGN_fp_verifier.md`, `DESIGN_bitmanip_verifier.md`, `DESIGN_privilege_verifier.md`,
`DESIGN_vm_verifier.md`, `DESIGN_tlb_verifier.md`. Release history is in `CHANGELOG.md`.*

# Roadmap Audit — what the 🟡 / ⬜ markers look like now

**Date:** 2026-07-18 · **Against:** `AVA_MASTER_STATUS_AND_ROADMAP.md` (T23–T31 snapshot)
**Verified by:** module inventory + full test sweep (886 passed, 7 skipped)

> Short answer: **No — not everything is closed.** The *verification* frontier
> (levels 1–12) is now essentially complete. What remains open is the
> **ML-depth, formal-closure, power, SoC-breadth and productisation** band.

---

## 1. Twenty-level taxonomy — then vs now

| # | Area | Was | Now | Evidence / what's still missing |
|---|---|---|---|---|
| 1 | ISA verification | ✅ (Vector ⬜) | ✅ | `vector_verifier` (RVV), `rv64_verifier`, full Zk/Zvk crypto tier |
| 2 | Pipeline hazards | 🟡 | ✅ | `pipeline_verifier` (golden forwarding/stall, RAW/WAR/WAW, IPC/CPI) |
| 3 | Cache | ⬜ | ✅ | `cache_verifier` (`CacheModel`, LRU/FIFO, WB/WT, evict, dirty WB) |
| 4 | Memory system | 🟡 | 🟡 | `bus_verifier` + `lsq_verifier` + `memory_model_verifier`. **DRAM timing / latency / arbitration still ⬜** |
| 5 | Bus protocol | ⬜ | 🟡 | `bus_verifier` covers AXI4/AHB/APB. **Wishbone, TileLink, AXI-Stream ⬜** |
| 6 | MMU | ✅ (Sv39/48 pending) | ✅ | `vm_verifier` (Sv32) + `sv_mmu_verifier` (Sv39/Sv48) + `tlb_verifier` + PMP |
| 7 | Branch predictor | ⬜ | ✅ | `branch_predictor_verifier` (recovery, accuracy/MPKI, golden RAS) |
| 8 | Out-of-order | ⬜ | ✅ | `ooo_verifier` (ROB order, RAW, rename, squash, exec timing) |
| 9 | Multicore | 🟡 | ✅ | `coherence_verifier` (multicore event stream) + `memory_model_verifier` (RVWMO) |
| 10 | Cache coherency | ⬜ | ✅ | `coherence_verifier` (MESI SWMR, read-from-valid, write serialization) |
| 11 | Security | ✅ (glitch ⬜) | ✅ (glitch ⬜) | Unchanged. **Physical glitch / rowhammer still ⬜** |
| 12 | Fault injection | ⬜ | ✅ | `fault_injector` (bit-flip/stuck-at/corruption, detection-rate, blind spots) |
| 13 | AI-assisted | ✅🟡 | ✅🟡 | `self_evolving_engine` (non-stationary bandits, regret/closure metrics), `stimulus_generator`, `coverage_collector` close the RL loop. **ML failure clustering ⬜, bug prediction ⬜** |
| 14 | Formal (SVA/BMC) | 🟡 | 🟡 | **Unchanged.** `formal_fuzzer` bridge + `contract_dsl` + `temporal_checker`; native SVA / BMC closure ⬜ |
| 15 | Power-aware | ⬜ | ⬜ | **Untouched.** No clock-gating / DVFS / power-domain checking |
| 16 | Performance | 🟡 | 🟡 | `perf_counter_verifier` + pipeline/cache metrics added. **Automated dashboards ⬜** |
| 17 | Compliance | ✅ | ✅ | Unchanged |
| 18 | Portable | 🟡 | 🟡 | **Unchanged.** Still one core family wired |
| 19 | Complete SoC | 🟡 | 🟡 | `debug_verifier`, `interrupt_verifier` (PLIC/CLINT/CLIC), `reset_verifier` added. **GPIO / SPI / I²C / timers ⬜** |
| 20 | Production platform | 🟡 | 🟡 | Many more pillars, but UVM / regression mgmt / dashboards ⬜ |

**Scorecard:** was 6 ✅ / 8 🟡 / 6 ⬜ → now **11 ✅ / 7 🟡 / 2 ⬜**.

---

## 2. Module-inventory 🟡s — status

| Module | Was | Now | Note |
|---|---|---|---|
| `AGENT_B/` testbench gen | 🟡 | 🟡 | **Untouched this cycle** |
| `AGENT_F/` coverage backend | 🟡 | 🟡 | RL loop closed via `coverage_collector` + `self_evolving_engine`; the **Verilator coverage backend itself** is still the partial piece |
| `AGENT_J/` CDC | 🟡 | 🟡 | **Untouched** |
| `AGENT_K/` perf | 🟡 | 🟡 | Superseded in practice by `perf_counter_verifier`, but `AGENT_K` itself unchanged |
| `AGENT_L/` equivalence | 🟡 | 🟡 | **Untouched** |
| `AGENT_H/digital_twin` | 🟡 | 🟡 | **Untouched** |
| `AGENT_H/formal_fuzzer` | 🟡 | 🟡 | **Untouched** |

---

## 3. "Breakthrough ideas" section — mostly still open

Closed / materially advanced:
- **#3 Self-evolving RL verification** — `self_evolving_engine` with pluggable
  non-stationary bandits, importance-weighted novelty reward, regret and
  closure-prediction metrics, multi-seed campaigns. This is now a real RL loop.

Still 🟡/⬜ (honest list): digital-twin power/thermal modelling, ML failure
clustering, multi-core cross-core differential fan-out, reversible "time-travel"
replay, NL→assertion synthesis, **GNN for RTL (⬜ untouched)**, trained bug
prediction, explicit per-module risk scoring, cross-project continuous learning,
cloud/asset management.

---

## 4. Honest bottom line

What this development arc actually did: it took the **microarchitecture and ISA
verification frontier** from half-covered to essentially complete — pipeline,
cache, coherence, OoO, LSQ, branch prediction, bus, interrupts, debug, reset,
vector, RV64, and a full scalar+vector crypto tier where **every golden is
validated against a published standard vector** (FIPS-197, GB/T 32905/32907,
NIST SP 800-38D / GCM, `hashlib`).

What it did **not** do: the software-engineering and ML half of the roadmap.
Power-aware verification and GNN-for-RTL remain entirely unstarted; formal
SVA/BMC closure, dashboards, UVM/regression management, SoC peripheral breadth,
CDC, equivalence and testbench generation remain partial exactly as before.

Those are real gaps, not bookkeeping. They are also a different *kind* of work
(infrastructure, ML training, EDA-tool integration) than the golden-reference
checkers that dominate this codebase.

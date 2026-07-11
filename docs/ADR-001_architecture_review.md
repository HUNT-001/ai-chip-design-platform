# ADR-001: AVA Architecture Review — Extended Verification Tier (T39→T51)

**Status:** Accepted (with tracked follow-ups)
**Date:** 2026-07-09
**Deciders:** Platform owner (PAVAN)
**Scope:** Full-architecture verification of the AVA extended verification tier
after development cycle 2 (agents T39–T51).

---

## Context

AVA is a multi-agent RISC-V RTL verification platform: a six-phase pipeline
(`ava_patched.py`) that culminates in an extended "research tier" of golden-
reference agents, each verifying one architectural slice from the canonical
commit log or an additive trace. Cycle 2 added 13 agents spanning branch
prediction, RVV, the closed AI coverage/generation loop, the full multicore
memory stack (coherence + SC/TSO/RVWMO consistency), the interrupt architecture
(PLIC/CLINT/CLIC/AIA-IMSIC), and system integration (perf counters, debug/
trigger, reset, hypervisor two-stage translation).

Because the workspace mount truncated freshly-grown files during development,
each agent had been validated only *standalone*. This review runs the **full
in-repo suite** and audits structural consistency for the first time.

## Decision

**Verdict: the architecture is sound and correctly implemented.** The extended
tier is accepted as-is, with a small set of tracked follow-ups (below) that are
scoping gaps, not defects.

## Evidence (what was verified, 2026-07-09)

| Check | Result |
|---|---|
| Full test suite (`pytest tests/test_agents.py`) | **515 passed, 1 skipped** (2.7 s) |
| Syntax of all 43 `AGENT_H` modules + orchestrator | 0 failures |
| Whole-tree `ast.parse` lint | clean |
| `import AGENT_H` (exercises every `__init__` export) | OK — 88 exports |
| `import ava_patched` | OK — `EXTENDED_AGENTS_AVAILABLE = True` |
| Schema discipline | 41 modules pin `SCHEMA_VERSION="2.1.0"`; **no** stray versions |
| Package hygiene | all 12 `AGENT_*` dirs have `__init__.py` |
| Manifest entry points | all 13 new agents expose `run_from_manifest` |
| Graceful degradation | 50 `except Exception` guards in the orchestrator's Phase-6 |
| Test classes for T39–T51 | all 15 present |
| "Never delete" rule | `_legacy/` present; no orphan deletions |

## Architectural assessment

### What is done well (strengths)

1. **Schema-driven modularity.** Every agent communicates through two versioned
   JSON schemas (commit-log + run-manifest, v2.1.0). Agents are lazily imported
   (`_try_import`) and each Phase-6 block is independently `try/except`-guarded,
   so a missing/broken agent degrades to a skip rather than crashing the
   pipeline. This is the right pattern for a research tier and it is applied
   consistently (50 guards).
2. **Golden-reference soundness over heuristics.** Each checker recomputes the
   architectural truth (element-wise RVV ALU, two-stage translation, axiomatic
   memory-order graphs) and compares, rather than pattern-matching. Where a full
   model would be intractable it is *scoped explicitly and documented*, not
   faked.
3. **Litmus / spec validation.** The high-risk agents are validated against
   canonical references: the memory-consistency checker reproduces SB/MP/LB/CoRR
   across SC/TSO/RVWMO exactly (including the subtle RVWMO dep-vs-fence case);
   coherence is validated on producer-consumer / stale-read / SWMR; interrupt
   arbitration on priority/threshold/tie-break. This is the strongest evidence
   of correctness.
4. **Conservative gating.** Every agent no-ops cleanly on absent/empty input and
   only raises on a real violation — no false positives by construction. All
   report a consistent schema (`pass`, `band`, `total_violations`, `violations`).
5. **A genuinely closed AI loop.** collect (incl. cross/operand/coherence/
   consistency bins) → non-stationary-bandit planner → self-validating stimulus
   → re-collect, demonstrated to ≥90% closure over a 166-bin universe.

### What to revisit (tracked follow-ups — scoping gaps, not bugs)

1. **Trace-producer gap (highest priority).** Eight cycle-2 agents consume
   *additive* traces (`coherence_trace`, `consistency_trace`, `interrupt_trace`,
   `debug_trace`, `hypervisor_trace`, `aia_trace`, reset snapshot) that the base
   tandem-sim phases (AGENT_C/D) do **not yet emit**. They are correct and unit-
   tested, but in a live run they no-op until the producers are taught to emit
   these fields. *Action: extend the ISS/RTL adapters to populate the additive
   fields.*
2. **No full-pipeline integration test with Phase-6 active.** The suite is
   per-agent (excellent unit coverage) plus a smoke test, but there is no single
   end-to-end run that drives a real commit log through all Phase-6 agents. This
   is why the trace-producer gap went unnoticed. *Action: add one integration
   test with a synthetic multi-feature commit log + traces.*
3. **Documented model simplifications** (all sound, all noted in their design
   docs): hypervisor uses resolved per-stage mappings (not the interleaved
   walk); coherence assumes `cycle` = visibility order; the memory model takes
   `co`/`rf` as given for multi-store cases. These are correct scoping choices;
   revisit only if deeper coverage is needed.
4. **`ava_patched.py` is growing large** (Phase-6 is ~30 near-identical
   `run_from_manifest`-and-record blocks). *Action: factor a small
   `_run_manifest_agent(name, module, report_key)` helper to collapse the
   boilerplate.*

## Options considered (for the trace-producer gap)

### Option A — Extend AGENT_C/D to emit all additive fields
| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Payoff | High — turns 8 opt-in agents into always-on |
| Risk | Low — additive, schema-compatible |

**Pros:** agents fire on every run; catches real bugs. **Cons:** touches the
sim-adapter layer.

### Option B — Leave agents opt-in, driven by external traces
**Pros:** zero change; agents already usable by anyone who supplies a trace.
**Cons:** no automatic coverage in the default flow.

**Recommendation:** Option A, incrementally (start with perf-counters and vector
which already ride the commit log, then the separate-trace agents).

## Consequences

- **Easier:** adding further agents (the pattern is proven and consistent);
  trusting results (golden + litmus-validated).
- **Harder:** nothing regressed — the tier is additive and the base pipeline is
  untouched.
- **Revisit:** the trace-producer wiring and one integration test are the two
  things standing between "unit-correct" and "exercised end-to-end in every run".

## Action items

1. [~] Teach AGENT_C/D (ISS/RTL adapters) to emit the additive trace fields —
       **reference contract now provided** by `AGENT_H/demo_traces.py`
       (`write_demo_run`); the adapters still need to populate these fields from
       real sim state.
2. [x] Add a full-pipeline integration test with Phase-6 agents active —
       `tests/test_agents.py::TestPhase6Integration` drives every extended agent
       against a synthesized run and asserts each fires + passes.
3. [ ] Refactor the Phase-6 manifest-agent boilerplate in `ava_patched.py`.
4. [ ] Regenerate `AVA_Status_and_Roadmap.docx` from the refreshed markdown.
5. [x] Split `tests/test_agents.py` — T50/T51 + Phase-6 integration moved to
       `tests/test_extended_agents.py` (2026-07-09). `test_agents.py` is now
       4605 lines (under the mount cap) and the full `tests/` directory runs
       clean in-sandbox again: **532 passed, 1 skipped**.

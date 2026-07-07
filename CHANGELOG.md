# Changelog

All notable changes to AVA — Autonomic Verification Agent are documented here.

---

## [2.31.0] — 2026-06-30

### Added
- **T47 — Performance-Counter Checker** (`AGENT_H/perf_counter_verifier.py`).
  Golden checker for `mcycle` / `minstret` / `mcountinhibit`, reading the
  `perf_counters` field the **standard commit log** already carries — so it needs
  no separate trace and runs in the normal pipeline.
  - **perf_instret_increment** (HIGH) — `minstret` increases by exactly 1 per
    retired instruction; 0 when IR-inhibited; a trapping (non-retiring) record
    may be 0 or 1.
  - **perf_cycle_monotonic** (HIGH) — `mcycle` is non-decreasing (superscalar
    retire ⇒ delta 0 allowed), and exactly 0 when CY-inhibited.
  - Metrics: cycles/instret spanned, IPC, CPI. Clean no-op when no record
    carries `perf_counters`.
- Wired into `ava_patched.py::_run_extended_pipeline` as a commit-log verifier
  (`_perfcnt`, runs on `rtl_log`, writes `perf_counter_report.json`).
- 8 new pytest cases (`TestPerfCounterVerifier`) + 11 standalone.

---

## [2.30.0] — 2026-06-30

### Added
- **T46 — Interrupt Controller Checker** (`AGENT_H/interrupt_verifier.py`).
  Golden model of the RISC-V interrupt controllers, a verification level the
  trap/privilege agents don't cover (they check the CSR side of an already-
  delivered trap, not the controller that decides *which* interrupt fires):
  - **PLIC** priority arbitration — `claim` must return the highest-priority
    source that is pending AND enabled for the context AND priority > threshold,
    ties broken to the lowest source id, priority 0 = never
    (`plic_claim_wrong`); a source at/below threshold or at priority 0 is never
    claimable (`plic_threshold`, `plic_priority0`); claim clears pending (state
    progression verified by the golden `PLICModel`).
  - **CLINT** — timer interrupt `MTIP` set iff `mtime >= mtimecmp`
    (`clint_mtip`); software interrupt `MSIP` equals the written bit
    (`clint_msip`).
  - Additive `interrupt_trace.jsonl` contract
    (`config`/`pending`/`claim`/`complete`/`clint`). Clean no-op without a trace.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_interrupt` import,
  `EXTENDED_AGENTS_AVAILABLE`, `run_from_manifest` → `interrupt_report.json`).
- 9 new pytest cases (`TestInterruptVerifier`) + 12 standalone (priority,
  threshold, priority-0, tie-break, claim-clears-pending, disabled/no-pending,
  CLINT mtip/msip, robustness, schema, manifest).

---

## [2.29.0] — 2026-06-30

### Added
- **Consistency coverage** — the memory-consistency level now feeds the closed
  self-evolving loop (mirroring the coherence-coverage work).
  `memory_model_verifier.consistency_coverage_bins()` / `consistency_universe()`
  emit `mmpair:{store_load,load_load,store_store,load_store}` (the po pair types
  / reordering opportunities present) and `mmsync:{fence,aq,rl,rmw}` (ordering
  mechanisms exercised) — an 8-bin universe (weights 2–3 → real holes).
  `coverage_collector` accepts `consistency_execution=` and merges them into the
  same `coverage_summary.json` (new `mmpair`/`mmsync` categories);
  `stimulus_generator` has matching templates that emit minimal
  `consistency_execution` seeds, self-validating. The loop closes the full
  register/value/cross/operand/coherence/consistency universe with generated
  stimulus.
- 1 in-repo pytest case (`test_consistency_coverage_and_loop`) + 5 standalone.

---

## [2.28.0] — 2026-06-30

### Added
- **RVWMO enrichment** of the memory-consistency checker
  (`memory_model_verifier.py`) — the RISC-V synchronization mechanisms real
  concurrent code depends on:
  - **Acquire/release** (`.aq` / `.rl`, RCsc) — an acquire load is ordered
    before every later op in program order; every earlier op is ordered before a
    release store. Restores ordering weak RVWMO otherwise drops. (Test: a
    release+acquire MP is forbidden; release *alone* is insufficient.)
  - **Fence predecessor/successor sets** (`FENCE pr,pw,sr,sw`) — a fence now
    orders only accesses whose types are in its predecessor/successor sets, so
    `FENCE r,r` correctly leaves a store→load relaxed while `FENCE rw,rw` orders
    it. (Default = full `rw,rw`.)
  - **RMW atomicity axiom** — for an atomic read-modify-write (LR/SC, AMO,
    grouped by `rmw` id), no store may be coherence-interposed between the
    atomic's read and its write; an interposing store breaks atomicity and is
    reported HIGH with a `load → interposed store(s) → write` witness.
- 4 new in-repo pytest cases (release/acquire, fence r/w sets, RMW atomicity)
  + 7 standalone.

---

## [2.27.0] — 2026-06-30

### Added
- **T45 — Memory-Consistency Checker** (`AGENT_H/memory_model_verifier.py`). The
  verification level *above* cache coherence: coherence governs one location,
  consistency governs ordering *across* locations — the missing-fence / illegal-
  reordering bugs that are the hardest and most severe in multicore. Rigorous
  **axiomatic** ("herd"-style) verification for **SC / TSO / RVWMO**:
  - Builds the standard relations from an observed execution — program order
    (po), **preserved** program order (ppo, model-specific), reads-from
    (rf / external rfe), coherence order (co), from-read (fr), and fence order.
  - Checks two acyclicity axioms: **sc-per-location**
    (`po-loc ∪ rf ∪ co ∪ fr`, i.e. coherence) and the model's **global order**
    (`ppo ∪ fence ∪ rfe ∪ co ∪ fr`). A **cycle = the execution is not permitted
    by the model** = a real consistency bug, reported HIGH with the offending
    cycle as a witness.
  - `ppo` per model: `sc` keeps all po; `tso` all *except* store→load (the
    store-buffer relaxation); `rvwmo` keeps only same-address pairs or a
    syntactic dependency, otherwise ordering must come from a fence.
  - **Validated against the canonical litmus tests**: SB (allowed under TSO,
    forbidden under SC, forbidden under TSO once fenced), MP and LB (forbidden
    reorderings under TSO; allowed under RVWMO without fences/deps; forbidden
    once fenced), and coherence (CoRR) via sc-per-location. `co` from `cycle`/
    `co` rank; `rf` explicit or inferred by value. Additive
    `consistency_trace.jsonl` contract (`core/op/addr/value/cycle/rf/co/deps`).
- Wired into `ava_patched.py::_run_extended_pipeline` (`_memmodel` import,
  `EXTENDED_AGENTS_AVAILABLE`, `run_from_manifest` writing
  `memory_model_report.json` when an execution trace is present).
- 12 new pytest cases in `tests/test_agents.py::TestMemoryModelVerifier`
  (SB/MP/LB across models, fences, coherence, cycle witness, normalisation,
  robustness, schema, manifest).

---

## [2.26.0] — 2026-06-30

### Added
- **Operand-value cross-coverage** (`opnd:{srcA_class}:{srcB_class}`) — the
  operand corner-case combinations plain coverage misses ("did we test `add` of
  neg+neg? sub with a zero operand? all-ones × all-ones?"). 6×6 = 36-bin finite
  universe (weight 2 → real holes).
  - `coverage_collector.py`: maintains a **golden register-file shadow** so the
    *source*-operand values of each binary arithmetic instruction can be
    recovered from the commit log (operands read **before** the write is
    applied, so `add x5,x5,x6` correctly reads the old `x5`). Register and
    immediate operands both resolved; unresolvable operands are skipped (no
    false bins). New `opnd` category in the per-category breakdown.
  - `stimulus_generator.py`: `opnd` template sets two source registers to the
    target classes then adds them — self-validating like every other template.
- The self-evolving loop now closes a **166-bin** universe (register,
  value-class, opcode×result cross, operand-pair cross, branch, privilege, and
  four coherence categories) to ~92% with generated stimulus, discounted-UCB
  preferring directed generation.
- 3 new in-repo pytest cases + standalone operand suite.

---

## [2.25.0] — 2026-06-30

### Added
- **Coherence-aware coverage/generation loop** — the multicore coherence work
  (T44) now participates in the closed self-evolving loop, so the platform can
  *drive coverage of coherence corner cases*, not just detect violations.
  - `coherence_verifier.py`: `coherence_coverage_bins()` + `coherence_universe()`
    compute functional coverage of coherence *scenarios*:
    `cohpat:{producer_consumer,migratory,read_shared,write_shared}` (sharing
    patterns, always computable from the load/store cores); and — when the trace
    carries MESI state — `cohstate:{M,E,S,I}`, `cohtrans:{I->S,I->E,I->M,S->M,
    E->M}` and `cohshare:{1,2,3plus}`. **Soundness:** transitions are restricted
    to those a core produces via its *own* op; snoop-induced downgrades
    (`M->S`, `E->S`, remote `M->I`) are deliberately excluded because they can't
    be attributed to the owning core's op trace. Universe is dynamic (state bins
    only appear when states are present, avoiding a meaningless denominator).
    The verifier report now carries a `coherence_coverage` block.
  - `coverage_collector.py`: accepts `coherence_events=` and merges coherence
    bins into the same coverage model / `coverage_summary.json`, with new
    `cohpat/cohstate/cohtrans/cohshare` categories (weights 2–3, so coherence
    scenarios are high-priority holes).
  - `stimulus_generator.py`: coherence templates emit multicore
    `coherence_events` for each coherence bin (producer-consumer, migratory,
    read/write-shared, per-state, own-op transitions, sharer counts),
    self-validating via the collector; the generated scenarios are themselves
    coherence-clean (verified). The self-evolving loop closes coherence bins
    with generated multicore stimulus (end-to-end test ≥90%).
- 6 new in-repo pytest cases (`TestCoherenceCoverage`) + 11 standalone.

---

## [2.24.0] — 2026-06-30

### Added
- **T44 — Multicore Cache-Coherence Checker** (`AGENT_H/coherence_verifier.py`).
  A distinct verification *level*: coherence bugs (missing invalidations, stale
  reads, two simultaneous writers) only manifest across cores, so a single-core
  tandem-diff can't see them. Golden checks over a multicore memory-access trace:
  - **`read_from_valid`** (HIGH) — every load's value was actually written by
    some store to that address (or the initial value); no fabricated data.
  - **`coherence_read_monotonic`** (HIGH) — **write serialization**: all writes
    to one address occur in a single total order and every core observes them in
    that order. Per core, reads-from must be non-decreasing in the global
    per-address write order; a core that sees a newer write then an older one has
    hit a stale read / lost invalidation. Catches cross-core write-order
    disagreement.
  - **`swmr`** (HIGH) — Single-Writer/Multiple-Reader invariant, checked
    structurally from per-line MESI state: a writable (M/E) line may not coexist
    with another writable or shared holder.
  - Coherence order is taken from the trace's global-visibility order (`cycle`
    stamps, else list order); `ver` fields disambiguate equal-valued writes.
    Additive trace contract (`core/op/addr/value/cycle/state/ver`), separate
    `coherence_trace.jsonl`. Metrics: cores, addresses, loads, stores, SWMR
    checks. Clean no-op on single-core / absent traces.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_coherence` import,
  `EXTENDED_AGENTS_AVAILABLE`, `run_from_manifest` writing `coherence_report.json`
  when a coherence trace is present).
- 18 new pytest cases in `tests/test_agents.py::TestCoherenceVerifier`
  (producer-consumer, fabricated value, initial-value, stale read, cross-core
  order disagreement, explicit `ver`, SWMR two-writers / writer+reader / clean
  paths, cycle reordering, single-core no-op, robustness, schema, manifest).

---

## [2.23.0] — 2026-06-30

### Added
- **Cross-coverage (opcode × result value-class)** in the coverage collector +
  stimulus generator — the most bug-productive coverage type. New
  `cross:{mnem}:{valclass}` bins over a default arithmetic instruction set
  (`add,sub,addi,and,or,xor,sll,srl,sra,slt,sltu,mul`) × the six value classes
  (72-bin finite universe → real holes, weight 2). Answers questions plain
  coverage can't: "did we test `add` producing a *negative* result?
  `sub` producing *all-ones*?".
  - `coverage_collector.py`: cross bins derived from each record's mnemonic ×
    written-register value class; `cross_instructions` overridable via the
    coverage model; new `cross` category in the per-category breakdown.
  - `stimulus_generator.py`: `cross` template emits `{mnem} …` producing a
    value of the target class — self-validating like every other template, so
    the self-evolving loop closes over cross bins too.
- 3 new pytest cases (`TestCoverageCollector::test_cross_coverage`,
  `TestStimulusGenerator::test_cross_coverage_targets`) + the loop-closure and
  planner-integration tests updated for the larger, importance-ranked hole set.

---

## [2.22.0] — 2026-06-30

### Added
- **T43 — Coverage-Directed Stimulus Generator** (`AGENT_H/stimulus_generator.py`).
  The generation half of the self-evolving loop — it converts a coverage hole /
  constraint into a concrete RISC-V instruction **seed**, closing the loop
  end-to-end (`holes → planner → constraints → generator → seeds → run →
  collector → holes`).
  - **Templates** map each bin kind to directed stimulus: `reg:x{n}`
    (`addi`), `valclass:*` (value-producing `li`), `branch:{taken,not_taken}`
    (operand-set + branch with the next-PC reflecting the direction),
    `priv:{M,S,U}`, `instr:{mnem}`; a random fallback for unknown kinds.
  - **Self-validating by construction** — every template emits the assembly
    *and* the golden commit records it should produce; `predicted_coverage()`
    runs those through the real `CoverageCollector`, so `covers_target()` proves
    a seed hits its bin. The generator checks its own work.
  - **Real self-evolving plugins** — `make_env()` returns a `generate`/`evaluate`
    pair, and **`close_coverage()`** wires the generator into
    `SelfEvolvingEngine` and drives coverage to target with *generated*
    stimulus (not a synthetic env); the bandit learns to prefer directed over
    random generation (directed-random hybrid / coverage-guided generation).
  - `generate_from_holes()` + `run_from_manifest` read the run's
    `coverage_summary.json` and write `stimulus.json` (directed seeds + a
    self-validation count) for the next round.
- Wired into `ava_patched.py::_run_extended_pipeline` after the self-evolving
  planner (`_stimgen` import, `EXTENDED_AGENTS_AVAILABLE`), so a run now emits
  directed stimulus for its own open holes.
- 14 new pytest cases in `tests/test_agents.py::TestStimulusGenerator`,
  including an **end-to-end closure test** proving generated stimulus reaches
  ≥95% coverage through the self-evolving loop and the bandit prefers directed
  generation.

---

## [2.21.0] — 2026-06-30

### Added
- **T42 — Functional Coverage Collector** (`AGENT_H/coverage_collector.py`).
  Computes functional coverage from the commit log and, crucially, **closes the
  self-evolving loop**: it emits the exact `coverage_summary.json` that
  `AGENT_H.self_evolving_engine` consumes, so the RL closure-planner now runs on
  real coverage instead of a hypothetical snapshot.
  - **Bins** as `category:key` labels (the shape `constraint_for` already
    understands): `reg:x{1..31}` (register writes), `valclass:{zero,one,neg,
    pos_small,pos_large,all_ones}` (value classes, `classify_value`),
    `branch:{taken,not_taken}` (direction, from the next PC), `priv:{M,S,U}`,
    and `instr:{mnem}` when a model lists an expected instruction set.
  - **Finite-universe categories produce real holes**; CSR / trap / vtype are
    reported as observed-only telemetry.
  - **Importance weights** per bin (priv=3, branch=2, others=1) — higher-weight
    holes are the ones the self-evolving scheduler chases first.
  - `collect()` returns a schema-v2.1.0 report with an embedded
    `coverage_summary`; `run_from_manifest` writes both `coverage_report.json`
    and the machine `coverage_summary.json`; RV32/RV64 auto-detected.
- Wired into `ava_patched.py::_run_extended_pipeline` **before** the
  self-evolving planner (`_covcoll` import, `EXTENDED_AGENTS_AVAILABLE`), so the
  planner picks up live coverage in the same run.
- 13 new pytest cases in `tests/test_agents.py::TestCoverageCollector`,
  including an **integration test** proving the collector's holes/weights drive
  the self-evolving planner (high-weight `priv` holes ranked ahead of register
  holes; hole labels round-trip into constraints).

---

## [2.20.0] — 2026-06-30

### Added
- **T41 — RISC-V Vector (RVV) Verifier** (`AGENT_H/vector_verifier.py`). Golden
  checker for the Vector extension — the widest ISA gap, because RVV correctness
  hinges on dynamic `vtype`/`vl` state a scalar tandem-diff never inspects:
  - **`vset_vl`** (HIGH) — spec-accurate application-vector-length rules for the
    configured SEW/LMUL/VLEN (incl. **fractional LMUL** 1/8…1/2):
    `AVL ≤ VLMAX → vl==AVL`; `AVL ≥ 2·VLMAX → vl==VLMAX`; in the impl-defined
    band `VLMAX<AVL<2·VLMAX` only the spec-guaranteed bounds
    `ceil(AVL/2) ≤ vl ≤ VLMAX` are asserted (a compliant design is never
    falsely flagged); always `vl ≤ VLMAX`.
  - **`vtype_vill`** (HIGH) — an unsupported `vtype` (reserved LMUL, SEW > ELEN,
    VLMAX < 1) must set `vill` and force `vl = 0`.
  - **`velem`** (HIGH) — element-wise golden SEW-width recompute for the vector
    ALU (`vadd/vsub/vrsub`, `vand/vor/vxor`, `vsll/vsrl/vsra`,
    `vmul/vmulh/vmulhu`, `vmin/vmax/vminu/vmaxu`, `vmerge`, `vmv`) across
    **active, unmasked** elements only — masked/tail elements are skipped
    (agnostic policy is legal), so only architecturally-pinned elements are
    checked. Handles `.vv` / `.vx` / `.vi` forms.
  - **`vtail`** (MEDIUM) — under `vta=0` the destination tail `[vl, VLMAX)` must
    stay undisturbed (when the pre-state is exposed).
  - **Vector load/store** (`vle/vse`, `vlse/vsse`, `vlxei/vsxei`) — golden
    per-element **address generation** for unit-stride / strided / indexed
    modes, checking: `vmem_addr` (address set correctness), `vmem_count`
    (exactly one access per active, unmasked element — catches spurious or
    missing accesses, incl. accesses to masked-off/tail elements), `vmem_eew`
    (access size == encoded EEW), and `vmem_value` (loaded/stored element vs.
    memory value at that address). `decode_vmem` mnemonic decode; mem metrics.
  - `decode_vtype` (dict or encoded int), `vlmax`, `velem_compute`,
    `decode_vector_alu`; RVV metrics (vector-instr / vset counts, mean `vl`,
    SEW & LMUL histograms, masked-op & active-element counts). Additive trace
    contract (`vtype`, `vl`, `avl`, `vlen`, `vregs`, `vmask`, `vstate_prev`) —
    clean no-op on non-vector traces.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_vector` import,
  `EXTENDED_AGENTS_AVAILABLE`, writes `vector_report.json` when vector activity
  is present).
- 24 new pytest cases in `tests/test_agents.py::TestVectorVerifier` +
  `TestVectorMemory` (vtype decode, vlmax, element golden, vset
  clean/exceed/ambiguous-band, vill, velem clean/bug/vx/vi, tail+mask skipping,
  vtail, no-op, robustness, schema, manifest; plus vmem decode, unit/strided/
  indexed addressing, spurious-access count, mask-suppressed accesses, EEW,
  load/store value, metrics/gating).

---

## [2.19.0] — 2026-06-30

### Added
- **T40 — Self-Evolving Verification Engine** (`AGENT_H/self_evolving_engine.py`).
  A reinforcement-learning coverage-closure loop — the AI "self-evolving test"
  research track. Instead of blindly generating more constrained-random tests
  as coverage plateaus, it treats closure as a **multi-armed bandit** problem:
  - **Non-stationary bandit policies** (pluggable) — coverage closure is a
    *non-stationary* problem (a strategy decays once it has covered the holes it
    is good at), so vanilla UCB1 is theoretically wrong for it. The engine ships
    four provably-grounded policies behind one interface:
    `UCB1` (stationary baseline, Auer 2002), **`DiscountedUCB1`** (γ-discounted
    counts — "forgetting", Garivier & Moulines 2011, **the default**),
    **`SlidingWindowUCB`** (window re-estimation), and **`ThompsonSampling`**
    (Bayesian Beta posterior with native uncertainty). `make_policy()` factory.
  - **Difficulty-aware + importance-ranked hole scheduler** — targets
    least-attempted, highest-weight holes first; tracks per-hole attempts and
    flags **suspected-unreachable** holes (candidate coverage waivers).
  - **`constraint_for(hole, level)` escalation ladder** — baseline → widen
    ranges → edge values → repair → adversarial, auto-climbed as a hole resists
    (constraint auto-tuning / mutation / repair / adversarial).
  - **Importance-weighted, novelty-boosted reward** = weighted coverage-closure
    + curiosity (rarely-hit regions) + bugs − runtime cost, bounded to [0,1].
  - **Intelligence metrics**: cumulative **regret** (learning-stability
    diagnostic), coverage **velocity**, coverage-**per-cost**, **closure
    prediction** (est. rounds-to-target + confidence), per-arm **uncertainty**,
    weighted coverage.
  - **`run_campaign()`** — multi-seed reproducibility runner reporting
    **mean ± 95% CI** over final coverage / rounds / regret, plus a modal
    recommended strategy (results with error bars, not one lucky run).
  - **`CoverageState`** (importance weights + region-novelty tracking),
    `evolve(generate, evaluate)` with plateau/target/exhausted stops and
    bad-plugin containment, schema-v2.1.0 report.
  - **`plan_from_coverage` / `run_from_manifest`** — offline advisory mode: no
    simulator; reads `coverage_summary.json`, ranks holes by importance ×
    difficulty, attaches an escalated constraint to each, recommends a strategy.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_selfevolve` import,
  `EXTENDED_AGENTS_AVAILABLE`, writes `self_evolving_report.json` when a
  coverage snapshot is present).
- 34 new pytest cases across
  `tests/test_agents.py::TestSelfEvolvingEngine` +
  `TestSelfEvolvingResearchGrade` (policy variants, non-stationarity vs.
  stationarity, sliding-window forgetting, Thompson posterior, constraint
  escalation, importance scheduling, novelty, suspected-unreachable, regret /
  efficiency / closure-prediction, multi-seed CI + determinism, per-policy
  plumbing, offline planning, robustness).

---

## [2.18.0] — 2026-06-30

### Added
- **T39 — Branch Predictor Verifier** (`AGENT_H/branch_predictor_verifier.py`).
  Level-7 branch-prediction verification from the commit log:
  - `bp_recovery` — after a conditional branch or direct jump, the committed
    next-PC must equal the architecturally-correct outcome (taken → target,
    not-taken → fall-through). The outcome is recomputed **independently** from
    the register operands, so a predictor that mis-speculates and fails to
    recover (commits a wrong-path instruction) is caught. Sound — no assumption
    about which predictor the DUT uses.
  - `bp_hit_flag` — if the DUT reports its own prediction (`predict.taken` /
    `predict.correct`), that hit/miss flag must agree with the actual outcome.
  - **Metrics** (analytics, never fail the run): branches, taken-rate,
    prediction accuracy, mispredicts, MPKI, and **RAS return-prediction
    accuracy** from a golden return-address stack (call/return hint model).
  - Conservatively gated: checks run only when operands / target / next-PC (and,
    for the flag check, the DUT's prediction) are available; a clean no-op on
    traces without branch information.
  - `BranchPredictorVerifier.run()` (schema v2.1.0 report with band),
    `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_branchp` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `branch_predictor_report.json` when
  branches are present).
- 12 new pytest cases in `tests/test_agents.py::TestBranchPredictorVerifier`.

---

## [2.17.0] — 2026-06-30

### Added — RV64 widening (phase 4)
- **RV64 M-extension ops** in `AGENT_H/rv64_verifier.py`:
  - 64-bit `alu64`: `mul`, `mulh`, `mulhsu`, `mulhu`, `div`, `divu`, `rem`,
    `remu` — with the RISC-V division semantics (truncate toward zero,
    divide-by-zero → −1 / all-ones, signed-overflow → dividend / 0).
  - W-suffix `aluw`: `mulw`, `divw`, `divuw`, `remw`, `remuw` — 32-bit
    operation with the same div/rem rules, result sign-extended to 64 bits.
  - `decode` recognises all the new mnemonics; the W forms are treated as word
    ops (so the `rv64_word_sext` diagnosis applies to them too).
- 5 new pytest cases in `tests/test_agents.py::TestRV64MExtension`
  (64-bit + W-op golden vectors incl. truncate-toward-zero, div-by-zero and
  overflow; `mulw`/`divw` integration pass; `mulw` bug caught).

### Verified
- Suite: **337 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes.

---

## [2.16.0] — 2026-06-30

### Added — RV64 widening (phase 3)
- **T38 — RV64 Atomics Verifier** (`AGENT_H/rv64_atomics_verifier.py`).
  Verifies the 64-bit "A" extension — `LR.D`/`SC.D` and the nine `AMO*.D`
  operations — against a golden 64-bit reference model (`amo_compute64`, with
  correct signed/unsigned 64-bit min/max and wrap). Checks AMO destination =
  old memory value, AMO write-back = `f(old, rs2)`, SC.D success/fail vs a live
  reservation, and 8-byte alignment. Reuses the shared `decode_atomic` decoder
  and acts only on `.D` atomics (clean no-op on RV32 / non-atomic traces).
- `atomics_verifier` (RV32) now **detects RV64** (a >32-bit register value) and
  no longer flags legal `.D` atomics as `rv32_illegal_d` on RV64 traces —
  leaving them to the RV64 module. RV32 behaviour is unchanged.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_rv64atom` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `rv64_atomics_report.json` when `.D`
  atomics are present).
- 13 new pytest cases in `tests/test_agents.py::TestRV64AtomicsVerifier`
  (64-bit golden vectors, clean AMO.D, signed min.D, write-back bug, LR/SC.D,
  spurious SC.D, misalignment, the RV32-guard both ways, robustness, schema,
  manifest).

### Verified
- Suite: **332 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all sixteen new agents wired.

---

## [2.15.0] — 2026-06-30

### Added — RV64 widening (phase 2)
- **T37 — Sv39 / Sv48 Virtual-Memory Verifier** (`AGENT_H/sv_mmu_verifier.py`).
  The marquee Linux-class unlock: generalises the golden page-table walker to
  the RV64 paging modes.
  - `SvMMU` — one mode-parameterised walker for **Sv39** (3-level, 39-bit VA)
    and **Sv48** (4-level, 48-bit VA): 8-byte PTEs, 64-bit satp/VA/PA, 4 KB /
    2 MB / 1 GB (and 512 GB) superpages with alignment checking, the
    non-canonical-address rule (VA high bits must sign-extend the top VA bit),
    and the R/W/X + U + SUM + MXR permission model.
  - `SvMMUVerifier` checks the DUT-served translation: `sv_translation`,
    `sv_missing_fault`, `sv_spurious_fault`.
  - Gated on **Sv39/Sv48** only, so it never double-covers the Sv32
    `vm_verifier`; conservative gating otherwise (page-table image + S/U
    privilege + virtual-address access). Clean no-op on bare-metal / Sv32 / M.
  - `satp_mode()`, `SvMMU.translate()`, `SvMMUVerifier.run()` (schema v2.1.0),
    `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_svmmu` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `sv_mmu_report.json` when Sv39/Sv48 is
  detected).
- 14 new pytest cases in `tests/test_agents.py::TestSvMMUVerifier`
  (mode detection; Sv39 4 KB / 2 MB / 1 GB walks; misaligned superpage; invalid
  PTE; non-canonical VA; checker bugs; gating; robustness; manifest).

### Verified
- Suite: **319 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all fifteen new agents wired.

---

## [2.14.0] — 2026-06-30

### Added — RV64 widening (phase 1)
- **T36 — RV64 Datapath Verifier** (`AGENT_H/rv64_verifier.py`). The first agent
  of the XLEN-64 widening. Verifies the defining RV64 semantics:
  - 64-bit `alu64()` golden ALU (full-width add/sub/logic/shift/compare,
    6-bit shift amounts).
  - W-suffix word ops `aluw()` (`addw/subw/sllw/srlw/sraw` + immediate forms):
    32-bit operation with mandatory **sign-extension to 64 bits**.
  - `rv64_word_sext` — explainable diagnosis of the classic "forgot to
    sign-extend" bug (low 32 bits correct, upper 32 not the sign-extension of
    bit 31); `rv64_word_op`, `rv64_result`, `rv64_shamt` (reserved W-shift
    shamt > 31).
  - **Auto-detects RV64** (a W-op or a >32-bit register value); a clean no-op on
    RV32 traces, so the existing suite is unaffected and no schema change is
    needed. `force=True` overrides detection.
  - `RV64Verifier.run()` (schema v2.1.0 report with band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_rv64` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `rv64_report.json` when RV64 is detected).
- 10 new pytest cases in `tests/test_agents.py::TestRV64Verifier`
  (64-bit + W-op golden vectors, sign-extension bug, 64-bit-result bug, RV32
  no-op, reserved shamt, robustness, schema, manifest).

### Verified
- Suite: **305 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all fourteen new agents wired.

---

## [2.13.0] — 2026-06-30

### Added
- **T35 — Fault-Injection Campaign Engine** (`AGENT_H/fault_injector.py`).
  A meta-verification agent: it verifies the **verification suite itself** by
  injecting hardware fault models into a known-good commit log and measuring how
  many the AVA detector panel catches (mutation testing of the environment).
  - Fault models: `bit_flip`, `stuck_at_0`, `stuck_at_1`, `register_corruption`,
    `memory_corruption`, `pc_corruption`.
  - `inject_fault()` applies a fault to a deep copy (original untouched);
    `FaultCampaign` runs a reproducible (seeded) campaign and reports
    **detection_rate / fault_coverage**, a **per-model** breakdown, and the list
    of **undetected** faults — each a concrete verification blind spot.
  - Default detector panel = golden-ALU `PipelineVerifier` + `CSRVerifier` +
    `AtomicsVerifier`; custom detector callables supported.
  - `band` reflects coverage (VERIFIED ≥0.9 … CRITICAL <0.3); the agent is a
    *measurement*, so it never fails the DUT.
  - `FaultCampaign.run()` (schema v2.1.0 report), `run_from_manifest()` runs a
    small per-run campaign measuring the panel's coverage on the run's own log.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_faultinj` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `fault_report.json`).
- 10 new pytest cases in `tests/test_agents.py::TestFaultInjector`
  (injection, bit-flip math, detection, 100% register-fault coverage, blind-spot
  reporting, determinism, robustness, schema, manifest).

### Verified
- Suite: **295 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all thirteen new agents wired.

---

## [2.12.0] — 2026-06-30

### Added
- **T34 — Bus Protocol Verifier** (`AGENT_H/bus_verifier.py`).
  Verifies on-chip bus transactions (AXI4 / AXI4-Lite / AHB / APB) against a
  golden transaction-level protocol model:
  - `axi_expected_beats()` generates the exact mandated beat-address sequence
    for FIXED / INCR / WRAP bursts (with correct WRAP wrap-around).
  - `bus_burst_length` (beats ≠ AxLEN+1), `bus_wlast` (LAST not on the final
    beat), `bus_beat_addr` (beat address ≠ mandated), `bus_4kb_boundary`
    (burst crosses a 4 KB page), `bus_wrap_invalid` (WRAP length ∉ {2,4,8,16}
    or unaligned start), `bus_resp` (invalid response code per protocol).
  - Metrics: transactions, reads, writes, beats, error responses.
  - Conservatively gated: each check fires only for the descriptor fields a
    transaction provides; unknown-protocol/partial transactions are
    metrics-only. Transactions are read from a `bus_trace` file or from a
    record's additive `bus` field.
  - `BusVerifier.run()` (schema v2.1.0 report with band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_bus` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `bus_report.json`).
- 15 new pytest cases in `tests/test_agents.py::TestBusVerifier`
  (golden FIXED/INCR/WRAP beat vectors + protocol-rule bugs + gating + metrics).

### Verified
- Suite: **285 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all twelve new agents wired.

---

## [2.11.0] — 2026-06-29

### Added
- **T33 — Cache Subsystem Verifier** (`AGENT_H/cache_verifier.py`).
  Verifies cache behaviour against a **golden set-associative cache model**
  (`CacheModel`): configurable sets/ways/line-size, LRU or FIFO replacement,
  write-back or write-through. Replays the access stream and checks the DUT's
  reported cache events:
  - `cache_hitmiss` — reported hit/miss ≠ golden model.
  - `cache_eviction` — wrong replacement victim (policy violation).
  - `cache_writeback` — dirty eviction without a write-back (or spurious one).
  - `cache_data` — a hit returned data inconsistent with the last write
    (line corruption / stale data).
  - Metrics: accesses, hits, misses, hit-rate, evictions, write-backs.
  - Conservatively gated: runs only with a known cache config and a
    **deterministic** policy (LRU/FIFO); each check fires only for the fields
    the DUT actually reports. A clean no-op otherwise.
  - `CacheModel.access()`, `CacheVerifier.run()` (schema v2.1.0 report with
    band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_cache` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `cache_report.json`).
- 14 new pytest cases in `tests/test_agents.py::TestCacheVerifier`
  (golden model hit/miss/LRU/dirty-eviction + checker bugs + gating + metrics).

### Verified
- Suite: **270 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all eleven new agents wired.

---

## [2.10.0] — 2026-06-29

### Added
- **T32 — Pipeline & Hazard Verifier** (`AGENT_H/pipeline_verifier.py`).
  Verifies pipeline hazard handling from the commit log and produces the
  Level-2 pipeline metrics:
  - **Golden in-order ALU** recomputes every RV32I ALU result from the
    architectural register file. On a mismatch it re-derives the result with the
    **un-forwarded stale** operand; if that reproduces the committed value the
    bug is diagnosed precisely as `hazard_forwarding` (forwarding/stall failure,
    naming the stale source and producer distance) — otherwise `alu_result`.
  - `control_hazard` — `jalr`/`ret`/`jr` that did not redirect to its computed
    target (flush / branch-recovery failure).
  - **Metrics** (analytics, never fail the run): RAW/WAR/WAW + control hazard
    inventory; IPC, CPI, stall cycles, utilization from `perf_counters`.
  - Shadow register file is updated from the *committed* value after each
    instruction, so a single bug is flagged exactly once — no error cascade, no
    false positive on a correct trace. Hard checks fire only when fully
    evaluable; ABI and `xN` register names both supported.
  - `alu_eval()`, `PipelineVerifier.run()` (schema v2.1.0 report with band),
    `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_pipeline` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `pipeline_report.json`).
- 12 new pytest cases in `tests/test_agents.py::TestPipelineVerifier`
  (golden ALU table, clean pass, forwarding-hazard diagnosis, generic mismatch,
  control hazard ±, hazard inventory, perf metrics, robustness, manifest).

### Verified
- Suite: **256 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all ten new agents wired.

---

## [2.9.0] — 2026-06-29

### Added
- **T31 — TLB Coherence & sfence.vma Verifier** (`AGENT_H/tlb_verifier.py`).
  Builds on the golden Sv32 MMU to verify Translation-Lookaside-Buffer
  behaviour:
  - `tlb_stale_after_sfence` — a translation served from a TLB entry that a
    covering `sfence.vma` should have invalidated (the classic "forgot to flush
    the TLB" bug).
  - `tlb_incoherent` — a served translation that is neither the current
    page-table walk nor a legitimately-cached, non-invalidated entry (covers
    fabricated translations and ASID leakage).
  - Models a golden TLB keyed by (ASID, VPN) with global-page handling and
    **scoped `sfence.vma` invalidation** (full-flush handled precisely;
    operand-scoped flush applied when the register values are recoverable, else
    conservatively skipped). Staleness *before* a covering `sfence.vma` is
    correctly treated as architecturally permitted — no false positives.
  - Same conservative gating as the VM verifier (Sv32 + page-table image + S/U
    privilege + virtual-address-carrying access); a clean no-op otherwise.
  - `TLBVerifier.run()` (schema v2.1.0 report with band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_tlb` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `tlb_report.json`).
- 8 new pytest cases in `tests/test_agents.py::TestTLBVerifier`
  (fill → permitted stale → stale-after-sfence → correct refill → incoherent
  → gating → robustness → manifest).

### Verified
- Suite: **244 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all nine new agents wired.

---

## [2.8.0] — 2026-06-28

### Added
- **T30 — Sv32 Virtual-Memory Verifier** (`AGENT_H/vm_verifier.py`).
  The layer above privilege/PMP and the gate for Linux-class cores. Its core is
  a spec-faithful **golden Sv32 MMU** (`Sv32MMU`) — a two-level page-table
  walker that, given a physical page-table image and `satp`, translates a
  virtual address for a given access type and privilege and returns either a
  physical address or the exact page-fault cause. Handles 4 KB pages and 4 MB
  superpages, the reserved `W=1,R=0` encoding, invalid PTEs, R/W/X + U + SUM +
  MXR permission rules, and misaligned superpages.
  - `VMVerifier` runs the golden MMU against the trace: `vm_translation`
    (committed PA ≠ golden), `vm_missing_fault` (should page-fault but didn't),
    `vm_spurious_fault` (valid translation that faulted).
  - **Conservatively gated** — checks run only when `satp` selects Sv32, a
    physical page-table image is available, the access carries a virtual
    address, and privilege is S/U (M-mode skipped). A clean no-op on
    bare-metal / no-MMU traces, with no schema-breaking changes (all trace
    fields are additive/optional).
  - `Sv32MMU`, `VMVerifier.run()` (schema v2.1.0 report with band),
    `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_vm` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `vm_report.json`).
- 18 new pytest cases in `tests/test_agents.py::TestVMVerifier` (golden MMU
  validated against hand-built page tables + checker + gating + robustness).

### Verified
- Suite: **236 passed, 1 skipped**; `compileall` clean; orchestrator self-test
  passes with all eight new agents wired.

---

## [2.7.1] — 2026-06-28

### Hardening
- All seven new commit-log verifiers (atomics, CSR, RVC, FP, bit-manip,
  privilege, peripheral) now guard against malformed log records: a non-dict
  record is skipped rather than crashing the run. The FP verifier additionally
  guards its constructor-time FLEN auto-detection and FP-register collection.
- Added a cross-agent **robustness battery** (`TestRobustness`): every new agent
  is exercised against empty logs, non-dict records, empty/`None`-field records,
  and a 300-record bulk log — 42 cases asserting no crash and a well-formed
  report.
- Added **report-schema consistency** tests (`TestReportSchemaConsistency`):
  every agent report carries the common keys, `schema_version == "2.1.0"`, a
  valid band, and correctly-typed fields.
- Added a **cross-agent clean-log** test (`TestCrossAgentCleanLog`): one mixed
  realistic commit log must pass every commit-log verifier simultaneously.
- Verified the full pipeline end-to-end: `python ava_patched.py` self-test
  passes with all seven agents wired into `_run_extended_pipeline`.
- Suite: **218 passed, 1 skipped**; `compileall` clean across all AGENT_* dirs.

---

## [2.7.0] — 2026-06-26

### Added
- **T29 — Privilege & PMP Verifier** (`AGENT_H/privilege_verifier.py`).
  Verifies the RISC-V privileged architecture and Physical Memory Protection —
  the gating capabilities for secure and Linux-class cores:
  - `priv_xret_illegal` — MRET/SRET from too low a privilege without an
    illegal-instruction trap.
  - `priv_csr_access` — accessing a CSR above the current privilege without a
    trap (reuses the `csr_verifier` address table).
  - `priv_ecall_cause` — ECALL trap cause must be 8/9/11 for U/S/M.
  - `priv_mret_target` — privilege after MRET/SRET must equal mstatus.MPP /
    sstatus.SPP.
  - `pmp_missing_fault` / `pmp_spurious_fault` — full PMP region model
    (OFF/TOR/NA4/NAPOT decode, R/W/X + lock bit, M-mode bypass) enforcing
    access-fault correctness.
  - All checks gated on available trace fields (privilege field, configured PMP)
    so the agent degrades to a no-op rather than producing false positives.
  - `parse_priv()`, `PMPModel`, `PrivilegeVerifier.run()` (schema v2.1.0 report
    with band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_privilege` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `privilege_report.json`).
- 12 new pytest cases in `tests/test_agents.py::TestPrivilegeVerifier`.

---

## [2.6.0] — 2026-06-26

### Added
- **T28 — RV32B Bit-Manipulation Verifier** (`AGENT_H/bitmanip_verifier.py`).
  Golden checker for the scalar bit-manipulation extensions:
  - **Zba**: sh1add, sh2add, sh3add
  - **Zbb**: andn, orn, xnor, clz, ctz, cpop, min, max, minu, maxu, sext.b,
    sext.h, zext.h, rol, ror, rori, orc.b, rev8
  - **Zbc**: clmul, clmulh, clmulr (carry-less multiply)
  - **Zbs**: bclr, bclri, bext, bexti, binv, binvi, bset, bseti
  Each instruction is recomputed exactly from a shadow register file and
  compared bit-for-bit against the committed `rd` (`bitmanip_result`). Writes
  to `x0` are ignored; checks are skipped (not failed) when an operand is
  unavailable.
  - `decode_bitmanip()`, `BitmanipVerifier.run()` (schema v2.1.0 report with
    band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_bitmanip` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `bitmanip_report.json`).
- 34 new pytest cases in `tests/test_agents.py::TestBitmanipVerifier`
  (28 golden vectors + decode/bug/schema/manifest).

---

## [2.5.0] — 2026-06-26

### Added
- **T27 — RV32F / RV32D Floating-Point Verifier** (`AGENT_H/fp_verifier.py`).
  Golden IEEE-754 checker for the single- and double-precision FP extensions:
  - `fp_nan_boxing` — single results in a 64-bit register must be NaN-boxed.
  - `fp_result` — `fadd/fsub/fmul/fdiv/fsqrt` (`.s`/`.d`) recomputed with a
    correctly-rounded golden model (round-to-nearest-even); directed-rounding
    mismatches are reported at MEDIUM, never HIGH.
  - `fp_sgnj` (sign-injection), `fp_minmax` (incl. NaN/±0 rules),
    `fp_compare` (feq/flt/fle incl. NaN), `fp_class` (fclass mask),
    `fp_move` (fmv bit copies).
  - `fp_flag_missing` — mandatory fflags exceptions (NV invalid, DZ
    divide-by-zero) that were not raised.
  - Auto-detects FLEN (32/64); conservative — skips a check when an operand is
    unavailable; compares generated NaNs against the canonical NaN.
  - `decode_fp()`, `fclass_mask()`, `FPVerifier.run()` (schema v2.1.0 report
    with band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_fp` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `fp_report.json`).
- 14 new pytest cases in `tests/test_agents.py::TestFPVerifier`.

---

## [2.4.0] — 2026-06-26

### Added
- **T26 — RV32C Compressed Instruction Verifier** (`AGENT_H/rvc_verifier.py`).
  Verifies the compressed extension from the commit log:
  - `rvc_pc_stride` — a compressed instruction must advance the PC by 2; a
    +4 stride means a 16-bit instruction was mis-sized and an instruction was
    skipped (control-transfer forms are excluded).
  - `rvc_reserved` — reserved/illegal encodings (all-zero halfword,
    `c.addi4spn`/`c.lui`/`c.addi16sp` with zero immediate, `c.lwsp`/`c.jr` with
    `x0`) must raise an illegal-instruction trap.
  - `rvc_reg_constraint` — "prime" forms (`c.lw`, `c.sw`, `c.and`, …) may only
    name `x8`–`x15`.
  - `is_compressed()` heuristic (insn_len / compressed flag / encoding width /
    `c.` prefix), `RVCVerifier.run()` (schema v2.1.0 report with band),
    `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_rvc` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `rvc_report.json`).
- 11 new pytest cases in `tests/test_agents.py::TestRVCVerifier`.

---

## [2.3.0] — 2026-06-26

### Added
- **T25 — Zicsr / Zifencei Semantics Verifier** (`AGENT_H/csr_verifier.py`).
  Golden checker for control & status register access and the instruction-fetch
  fence:
  - Decodes CSRRW/RS/RC, the immediate variants, and common pseudos
    (`csrr`, `csrw`, `csrs`, `csrc`, `csrwi/si/ci`).
  - Recovers the *old* CSR value from the destination write-back and verifies
    the read-modify-write: `csr_rd_value`, `csr_writeback`,
    `csr_spurious_write` (set/clear with x0 source), and `csr_read_value`.
  - Enforces read-only CSRs (table + read-only-by-encoding) via
    `csr_readonly_write` (expects an illegal-instruction trap, cause 2).
  - Zifencei: `fencei_missing` flags execution of just-modified code without an
    intervening `FENCE.I`.
  - `decode_csr()`, `csr_is_readonly()`, `CSRVerifier.run()` (schema v2.1.0
    report with severity band), `run_from_manifest()`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_csr` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `csr_report.json`).
- 12 new pytest cases in `tests/test_agents.py::TestCSRVerifier`.

---

## [2.2.0] — 2026-06-26

### Added
- **T24 — SoC Peripheral Protocol Verifier** (`AGENT_H/peripheral_verifier.py`).
  Promotes the `cross_domain.py` DMA/UART/CRYPTO *adapters* from format shims
  into real **protocol checkers** with reference models + scoreboards:
  - **DMA** — per-channel FSM + byte-conservation scoreboard; null-pointer,
    non-positive length, write-underflow, src/dst overlap, use-after-error,
    spurious/dangling-channel checks.
  - **UART** — configure-before-use FSM, 8-bit data-integrity, baud/parity
    sanity, and parity-error-without-parity consistency.
  - **CRYPTO** — key-before-op, status/output consistency (no result on ERROR
    = leak detection), determinism scoreboard, AES encrypt→decrypt round-trip
    scoreboard, and a real **SHA-256 known-answer test** (golden via `hashlib`).
  - `get_checker()` / `register_checker()` factory; `PeripheralVerifier.run()`
    returns the standard schema v2.1.0 report with severity band;
    `run_from_manifest()` self-gates on `agent_config.dut_class`.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_peripheral` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `peripheral_report.json`).
- 15 new pytest cases in `tests/test_agents.py::TestPeripheralVerifier`.

---

## [2.1.0] — 2026-06-26

### Added
- **T23 — RV32A Atomics Verification Agent** (`AGENT_H/atomics_verifier.py`).
  Golden-reference checker for the RISC-V "A" extension: `LR.W`/`SC.W`
  reservation semantics and all nine `AMO*.W` operations. Replays the commit
  log against an in-process golden model (shadow register file + shadow memory
  + reservation set) and flags any record whose destination value, memory
  write-back, store-conditional success/fail outcome, alignment trap, or
  reservation handling disagrees with the specification. Pure-Python, no EDA
  tools required.
  - `amo_compute()` — golden AMO arithmetic (signed/unsigned min/max, 32-bit
    wrap, swap/add/and/or/xor).
  - `decode_atomic()` — disassembly parser incl. `.aq`/`.rl` ordering bits.
  - `AtomicsVerifier.run()` — schema v2.1.0 report with severity band.
  - `run_from_manifest()` — Phase 6 pipeline integration.
- Wired into `ava_patched.py::_run_extended_pipeline` (`_atomics` import,
  `EXTENDED_AGENTS_AVAILABLE`, per-run `atomics_report.json`).
- 11 new pytest cases in `tests/test_agents.py::TestAtomicsVerifier`.

### Fixed
- Completed the previously truncated `test_ava_generate_suite_smoke` smoke test.

---

## [2.0.0] — 2026-06-25

### Overview
Major release — AVA v2.0 introduces a full **Phase 6 Extended Verification Pipeline** with 14 specialised research-tier agents, a complete pytest test suite, GitHub Actions CI, multi-format report generation, and a formal Python package structure.

### New Agents

| Agent | Module | Capability |
|---|---|---|
| T9  | `AGENT_G/causal_engine.py` | Causal AI-guided test generation (mismatch-class bias, 5× improvement) |
| T10 | `AGENT_H/minimizer.py` | Delta-debug counterexample minimizer |
| T11 | `AGENT_H/agent_h_intent.py` | Architectural intent verification (7 built-in specs) |
| T12 | `AGENT_H/confidence_scorer.py` | Weighted verification confidence score [0,1] |
| T13 | `AGENT_H/formal_fuzzer.py` | SymbiYosys witness → assembly seed converter |
| T14 | `AGENT_H/digital_twin.py` | Python micro-ISS for fast test pre-screening |
| T15 | `AGENT_H/explainer.py` | Human-readable bug explanations |
| T16 | `AGENT_H/contract_dsl.py` | Design contract DSL (`@contract`, `@for_instruction`) |
| T17 | `AGENT_H/temporal_checker.py` | LTL-style temporal property monitors |
| T19 | `AGENT_H/security_intel.py` | Spectre/privilege/cache covert-channel detection |
| T21 | `AGENT_H/economics_engine.py` | Bugs/hour, ROI score, persistent ledger |
| T22 | `AGENT_H/cross_domain.py` | CRYPTO / DMA / UART DUT adapters |
| —   | `AGENT_H/knowledge_graph.py` | Cross-campaign bug knowledge graph (SQLite) |
| —   | `AGENT_H/root_cause_localizer.py` | RTL file-level root-cause localisation |

### Orchestrator (`ava_patched.py`)

- **Phase 6 Extended Verification** — `_run_extended_pipeline()` wires all 14 agents in order with graceful `try/except` degradation per agent
- **`_try_import()` pattern** — lazy optional imports; missing modules never crash the base pipeline
- **`VerificationReportWriter`** — new class handling JSON, CSV, and HTML output formats
- **`--no-extended` CLI flag** — skip Phase 6 for fast iteration
- **`--rtl-sources` CLI flag** — pass RTL files to Agent J/L and root-cause localiser
- **Confidence score in summary** — final `_print_summary()` shows score, band, security, ROI

### Report Formats

- **JSON** — full results dict (was already working)
- **CSV** *(new)* — one row per bug with severity, PC, disasm, confidence, security band, ROI
- **HTML** *(new)* — single-page report with coverage progress bars, colour-coded bug table, extended verification panel

### Testing & CI

- **`tests/test_agents.py`** — 46 pure-Python pytest tests covering all new modules; runs in ~0.5s with no EDA tools
- **`.github/workflows/ci.yml`** — GitHub Actions matrix on Python 3.10/3.11/3.12: syntax check, pytest, schema validation, import check, orchestrator smoke test
- **`Makefile`** — `make test`, `make lint`, `make smoke`, `make clean`

### Package Structure

- `__init__.py` added to all agent packages (AGENT_A through AGENT_L)
- `AGENT_H/__init__.py` exports all 14 classes at the package level
- `conftest.py` simplified — only project root needs to be on `sys.path`

### Documentation

- **`README.md`** — complete rewrite with 6-phase pipeline diagram, full agent table, capabilities matrix, quick-start examples
- **`CLAUDE.md`** — AI assistant context file with schema, architecture rules, key files, common task recipes

### Confidence Score Bands

| Band | Score | Meaning |
|---|---|---|
| VERIFIED | ≥ 0.90 | Ready for sign-off |
| HIGH | ≥ 0.70 | Strong evidence, minor gaps |
| MEDIUM | ≥ 0.50 | Partial coverage |
| LOW | ≥ 0.30 | Significant gaps |
| CRITICAL | < 0.30 | Do not tape out |

---

## [1.1.0] — 2026-06 (prior)

- Agents A–G: semantic analysis, testbench generation, Spike ISS tandem simulation, compliance runner, coverage adaptation, genetic test generation
- Core commit-log schema v2.1.0
- Agents I, J, K, L: RVWMO validator, CDC checker, performance collector, equivalence checker

---

## [1.0.0] — Initial release

- Proof-of-concept multi-agent RISC-V verification pipeline
- Basic RTL execution, ISS comparison, commitlog analysis

# Changelog

All notable changes to AVA — Autonomic Verification Agent are documented here.

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

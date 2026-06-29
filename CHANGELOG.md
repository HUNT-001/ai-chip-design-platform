# Changelog

All notable changes to AVA — Autonomic Verification Agent are documented here.

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

# Design & Build Plan — T23 RV32A Atomics Verification Agent

**Status:** Implemented & tested (AVA v2.1.0, 2026-06-26)
**Module:** `AGENT_H/atomics_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this, why now

The roadmap map shows *RV32A atomics* as a **partially-addressed** capability:
the temporal checker (`AGENT_H/temporal_checker.py::LrBeforeSc`) does a shallow
ordering check on `LR.W`/`SC.W`, but nothing in the pipeline verifies the
*semantics* of atomics — that an AMO returns the old memory value, writes back
the correct read-modify-write result, that a store-conditional only succeeds
with a live reservation, or that signed vs unsigned `MIN`/`MAX` are computed
correctly.

Atomics are the highest-value-per-effort gap on the board:

- **Crucial.** Atomic read-modify-write, reservation tracking and SC
  success/fail are where real silicon bugs hide. They are notoriously hard to
  catch with plain instruction-by-instruction tandem diffing, because a wrong
  AMO write-back may not diverge a register until many instructions later.
- **Feasible.** The check is a pure-Python golden model over the existing
  commit-log schema — no Spike, Verilator or Yosys required. It drops into the
  established `AGENT_H` module pattern (`run()` + `run_from_manifest()`),
  matching the project's "no-EDA-tools pytest" philosophy.
- **Self-contained & deployable.** One module, graceful degradation, schema
  v2.1.0 output, fully wired into Phase 6, covered by 11 tests.

---

## 2. Architecture

The verifier reconstructs three pieces of golden shadow state purely from the
commit log, then checks every atomic instruction against the specification.

```
            commit log (rtl_commit.jsonl)
                       │
          ┌────────────┴────────────┐
          │   AtomicsVerifier.run()  │
          └────────────┬────────────┘
                       │  per record
   ┌───────────────────┼────────────────────┐
   │  shadow register file   (regs writeback)│
   │  shadow word memory     (mem_writes)    │
   │  reservation set        (LR/SC/AMO/store)│
   └───────────────────┬────────────────────┘
                       │
        decode_atomic(disasm) ──► LR | SC | AMO | (none)
                       │
        golden expectation vs committed effect
                       │
                  AtomicViolation[]
                       │
        report{ band, pass, violations, stats }
```

**Golden state.**

- *Shadow register file* — folded from each record's `regs` write-back, so the
  `rs2` operand of a later AMO/SC is available even though the commit record
  only carries the destination write. `x0` is pinned to zero.
- *Shadow memory* — word-addressed map updated from `mem_writes` (and from the
  observed `mem_reads` value of LR/AMO, to stay coherent).
- *Reservation set* — single-hart `LR`/`SC` model. A reservation is set by
  `LR.W`, and invalidated by: an `SC` (always), an `AMO` to the reserved word,
  a plain store to the reserved word, a re-`LR`, or (optionally) a configurable
  forward-progress window (default 64 instructions, matching `LrBeforeSc`).

---

## 3. Checks performed

| Check | Severity | What it catches |
|---|---|---|
| `amo_rd_value` | HIGH | AMO destination ≠ old memory value |
| `amo_writeback` | HIGH | AMO stored value ≠ `f(old, rs2)` (per-op golden math) |
| `lr_rd_value` | HIGH | `LR.W` destination ≠ value read from memory |
| `lr_mem_coherence` | MEDIUM | `LR.W` reads a value inconsistent with last write to that word |
| `sc_success_no_write` | HIGH | `SC.W` with a valid reservation didn't write memory |
| `sc_success_rd` | HIGH | `SC.W` succeeded but didn't return success code 0 |
| `sc_store_value` | HIGH | `SC.W` stored a value ≠ `rs2` |
| `sc_fail_wrote` | HIGH | `SC.W` without a reservation wrote memory (atomicity violation) |
| `sc_fail_rd` | HIGH | `SC.W` failed but returned success code 0 |
| `alignment` | HIGH | Misaligned atomic that did not raise an address-misaligned trap |
| `rv32_illegal_d` | MEDIUM | 64-bit `.d` atomic committed on an RV32 core |
| `lr_no_read` / `amo_no_mem` | MEDIUM | Atomic committed without the expected memory access record |

**Golden AMO math** (`amo_compute`): `swap, add` (32-bit wrap), `and, or, xor`,
signed `min/max`, unsigned `minu/maxu`. Signedness is the classic bug source and
is unit-tested explicitly (e.g. `amomin.w(0xFFFFFFFF, 1) == 0xFFFFFFFF` because
`0xFFFFFFFF == -1`, whereas `amominu.w(0xFFFFFFFF, 1) == 1`).

---

## 4. Severity band

`run()` returns a normalised `severity_score ∈ [0,1]` and a band:

| Band | Meaning |
|---|---|
| `CLEAN` | no violations |
| `MINOR` | only low-weight violations, normalised score < 0.3 |
| `DEGRADED` | normalised score ≥ 0.3, no HIGH violations |
| `CRITICAL` | at least one HIGH violation — do not sign off atomics |

The report shape mirrors every other AGENT_H module (`schema_version`,
`agent`, `records_checked`, `total_violations`, `pass`, `violations[]`,
timestamps), so the existing report writers need no changes.

---

## 5. Integration points

1. `AGENT_H/__init__.py` — exports `AtomicsVerifier`, `amo_compute`,
   `decode_atomic`.
2. `ava_patched.py`:
   - `_atomics = _try_import("AGENT_H.atomics_verifier", "AtomicsVerifier")`
   - added to `EXTENDED_AGENTS_AVAILABLE`
   - `_run_extended_pipeline` runs it after the temporal checker, writes
     `atomics_report.json`, and records `reports["atomics"]`
     (`atomics_examined`, `violations`, `band`, `pass`).
3. `run_from_manifest(path)` — standalone Phase 6 entry point; loads the
   manifest's commit logs, writes the report, and updates
   `manifest["phases"]["atomics_check"]`. Returns `0` pass / `1` fail, and
   degrades to `0` (skipped) when no commit log is present.

Graceful degradation throughout: a malformed record is logged and skipped, and
the verifier never throws into the pipeline.

---

## 6. Test coverage

`tests/test_agents.py::TestAtomicsVerifier` (11 cases, pure-Python):

- golden AMO math incl. signed/unsigned min/max and 32-bit wrap
- disassembly decode incl. `.aq`/`.rl`
- clean AMO log passes
- injected AMO write-back bug → `amo_writeback`, band `CRITICAL`
- valid LR/SC success path passes
- spurious SC store without reservation → caught
- reservation broken by an intervening store → SC must fail
- misaligned atomic without a trap → `alignment`
- report-schema completeness
- `run_from_manifest` round-trip writes `atomics_report.json`

**Full suite: 58 passed** (47 pre-existing + 11 new), `compileall` clean.

```bash
pytest tests/test_agents.py::TestAtomicsVerifier --import-mode=importlib -q
python AGENT_H/atomics_verifier.py --rtl path/to/rtl_commit.jsonl   # standalone
```

---

## 7. Known limitations / next steps for this module

- Single-hart reservation model. Multi-hart / SMP reservation stealing needs a
  per-hart reservation set keyed on a `hart_id` field (add to schema first).
- 32-bit (`.w`) width only; `.d` is flagged as illegal on RV32. Extend
  `amo_compute` to 64-bit when an RV64 target lands.
- Reservation-window default (64) is a guideline, not a hard ISA requirement;
  expose per-core override via the manifest `agent_config`.
- Cross-checks against the ISS log are currently structural; a future pass can
  diff AMO old-values against Spike directly when both logs are present.

---

## 8. Roadmap — recommended sequence for the remaining gaps

Ordered by *crucial × feasible*, reusing patterns already in the codebase:

1. **Finish the SoC peripheral adapters (DMA / UART / CRYPTO)** — promote the
   `cross_domain.py` translators from "format shims" to real protocol checkers
   (reference model + scoreboard). Same partial→done win as atomics.
2. **`Zicsr` / `Zifencei` CSR semantics agent** — natural sibling to this
   module; the temporal checker already has hooks (`CsrReadAfterWrite`).
3. **RV32C compressed** — decode/expand check; high coverage value, low risk.
4. **PMP + S/U-mode privilege agent** — extends `security_intel.py`.
5. **Regression database + JIRA/Linear ticket creation** — the market-adoption
   blocker; build on the existing `knowledge_graph.py` SQLite layer and the
   per-run manifest. This is what turns the platform from "runs" into "tracks".
6. **Grafana / Prometheus metrics exporter** — thin layer over the report dicts.

Each is a single self-contained `AGENT_*` module following the
`run_from_manifest` + pytest pattern demonstrated here.

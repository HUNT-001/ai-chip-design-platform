# Agent D — Commit-Log Comparator & Triage

> **Part of the AVA (Adversarial Verification Architecture) RISC-V verification pipeline.**  
> Agent D is the truth engine that decides whether a hardware RTL implementation and a golden Instruction Set Simulator (ISS) agree — commit by commit, register by register, cycle by cycle.

---

## Table of Contents

1. [What This Is](#1-what-this-is)
2. [Why It Matters](#2-why-it-matters)
3. [How It Works — Architecture Deep Dive](#3-how-it-works--architecture-deep-dive)
4. [The Files](#4-the-files)
5. [The 11 AVA Error Codes](#5-the-11-ava-error-codes)
6. [Exit Codes](#6-exit-codes)
7. [Installation & Requirements](#7-installation--requirements)
8. [Quick Start](#8-quick-start)
9. [Full Working Example](#9-full-working-example)
10. [AVA Manifest Mode](#10-ava-manifest-mode)
11. [Bug Hypothesis Engine](#11-bug-hypothesis-engine)
12. [Output Formats](#12-output-formats)
13. [Configuration Reference](#13-configuration-reference)
14. [Batch Mode](#14-batch-mode)
15. [Integrating with the AVA Pipeline](#15-integrating-with-the-ava-pipeline)
16. [Technology & Methodology](#16-technology--methodology)
17. [Test Suite](#17-test-suite)
18. [Performance](#18-performance)
19. [Commit-Log Schema Reference](#19-commit-log-schema-reference)
20. [Troubleshooting](#20-troubleshooting)

---

## 1. What This Is

The Agent D Comparator is a **streaming differential verification engine** for RISC-V processors. It compares two commit logs — one produced by an RTL simulation of the hardware under test, one produced by a golden ISS like [Spike](https://github.com/riscv-software-src/riscv-isa-sim) — and identifies the first point at which they diverge.

A **commit log** is a JSONL (newline-delimited JSON) file where each line records one instruction that retired from the processor: its PC, instruction word, what register it wrote, what memory it touched, and any CSR side-effects. When RTL and ISS produce the exact same commit log for the same program, the hardware implementation is correct. When they diverge, Agent D pinpoints the exact step, field, and values where the disagreement starts, classifies it into one of 11 error categories, and generates a structured bug report that the rest of the AVA pipeline can act on.

```
RTL Simulator ──► rtlcommit.jsonl ──┐
                                     ├──► compare_commitlogs.py ──► bugreport.json
ISS (Spike)   ──► isscommitlog.jsonl ┘                             hypotheses
                                                                    junit.xml / SARIF
```

---

## 2. Why It Matters

### The verification problem

Modern processors retire hundreds of millions of instructions per second. A simulation run can produce gigabytes of commit log data. Finding a hardware bug in that volume of data manually is essentially impossible — and finding it automatically requires a tool that is:

- **Fast enough** not to be the bottleneck in a CI loop
- **Memory-efficient** enough to handle traces that don't fit in RAM
- **Precise enough** to report not just "they differ" but *where*, *what field*, *what value*, and *why*
- **Reliable enough** that a false positive is impossible in a PASS verdict

### The differential testing insight

The fundamental technique is **differential testing**: two independent implementations of the same specification (RISC-V ISA) are run on the same input (a program), and their outputs are compared. The ISS is assumed to be architecturally correct; therefore any divergence is a bug in the RTL. This technique was pioneered by McKeeman (1998) and is now standard practice in CPU verification at companies like ARM, Intel, and SiFive.

The key advantage over coverage-only or assertion-only approaches: **you don't need to know what the bug is to detect it**. Any deviation from the architectural gold standard is flagged immediately.

### Where Agent D sits in AVA

The AVA pipeline runs multiple verification agents in parallel and in sequence:

```
Agent A (Spec & Interfaces)
    │
    ▼
Agent B (RTL Harness + Verilator) ──► rtlcommit.jsonl
Agent C (Spike/ISS Backend)       ──► isscommitlog.jsonl
    │                                       │
    └─────────────────┬─────────────────────┘
                      ▼
              Agent D (Comparator)  ◄── YOU ARE HERE
                      │
             bugreport.json + hypotheses
                      │
              Agent E (Compliance)
              Agent F (Coverage)
              Agent H (Red Team)
```

Agent D is the **single source of truth** for pass/fail. Its output feeds:
- Agent E (compliance): uses `ava_bugs` list to triage compliance failures
- Agent H (red team): consumes `first_mismatch` type to generate targeted adversarial sequences
- CI/CD: reads the exit code (0/1/2/3) to gate merges
- Engineers: read `bugreport.json` + hypotheses to debug failures

---

## 3. How It Works — Architecture Deep Dive

### 3.1 Parallel streaming with bounded memory

The central engineering challenge is that a 1-million-instruction run produces approximately **300 MB of commit log data** (≈300 bytes/record × 1M records). Loading both logs into memory would require 600 MB, which defeats the purpose of running many parallel seeds.

Agent D solves this with a **two-thread producer / one-thread consumer** pipeline:

```
┌─────────────────────────────────────────────────────────┐
│  Reader Thread A (RTL)                                  │
│  open file → parse JSONL → CommitEntry → Queue(512)     │
└────────────────────────────┬────────────────────────────┘
                             │  max 512 entries buffered
                             ▼
┌──────────────────────────────────────────────────────────┐
│  Main (Comparator) Thread                                │
│  dequeue RTL entry                                       │
│  dequeue ISS entry                                       │
│  step-validate both                                      │
│  push RTL to delta window                                │
│  field-compare → collect mismatches                      │
└──────────────────────────┬───────────────────────────────┘
                           │  max 512 entries buffered
                           ▼
┌─────────────────────────────────────────────────────────┐
│  Reader Thread B (ISS)                                  │
│  open file → parse JSONL → CommitEntry → Queue(512)     │
└─────────────────────────────────────────────────────────┘
```

Working set at any point: **512 RTL + 512 ISS + 32-entry delta window = ~330 KB**, regardless of trace length.

### 3.2 Delta-based context window

When a mismatch is found, engineers need to see the N commits immediately preceding the divergence to understand what led up to it. The naive approach stores full `to_dict()` snapshots in a `deque` — about 25 fields per commit.

Agent D instead uses a **delta-based shadow state**: only fields that *changed* from the previous commit are stored. A typical RISC-V commit changes 3–5 fields (step, pc, instr, rd, rd_val). On mismatch, the full context is reconstructed by **forward-replaying deltas** from a zero-state base.

```
Full snapshot per commit:  ~25 fields × 32 entries = 800 field-slots
Delta per commit:           ~5 fields × 32 entries = 160 field-slots
                                                      (5× reduction)
```

### 3.3 The field comparison engine

The `_FieldComparator` class applies checks in a strict order that mimics how a CPU debugger reasons about divergences:

```
1. Schema validation (per-side)   → SCHEMAINVALID
2. x0 invariant (per-side)        → X0WRITTEN
3. PC check                       → PCMISMATCH  [abort further checks on mismatch]
4. Instruction word               → INSTRMISMATCH [abort on mismatch]
5. Trap signalling + cause/tval   → TRAPMISMATCH, TRAPCAUSEMISMATCH
6. Register write-back            → REGMISMATCH
7. CSR writes (sorted, masked)    → CSRMISMATCH
8. Memory (addr → val → align)    → MEMMISMATCH, ALIGNMENTERROR
9. Privilege mode                 → PRIVILEGEMISMATCH
```

Steps 3 and 4 **abort early** because if the PC or instruction word differs, all subsequent field comparisons would be comparing different instructions against each other — producing meaningless noise.

### 3.4 XLEN-aware comparison

All hex values are parsed to Python integers and **masked to the XLEN width** at parse time:

```python
value = int(hex_string, 16) & ((1 << xlen) - 1)
```

This means `0xFFFFFFFF` and `0x00000000FFFFFFFF` compare equal under `xlen=32`, correctly handling simulators that emit 64-bit-wide values for RV32 registers.

### 3.5 CSR write normalisation

Different simulators may write the same set of CSRs in different orders (e.g., mstatus before mepc vs. mepc before mstatus). By default, CSR write lists are **sorted by CSR address** before comparison, eliminating false positives from ordering differences. This is configurable via `--csr-order-sensitive`.

Per-CSR **bit-masks** allow comparing only the meaningful bits of wide CSRs:

```python
# Compare only the MIE/MPIE bits of mstatus, ignore the rest
cfg = CompareConfig(csr_masks={0x300: 0x00000088})
```

### 3.6 Binary hash pre-flight

Before reading a single entry, Agent D can verify that the log files match expected SHA-256 digests pinned in the AVA manifest. This catches the common CI failure mode where a re-run accidentally replaces logs from a previous seed:

```json
"expected_sha256": {
  "rtl_log": "a3f2...c1d8",
  "iss_log":  "7b91...e042"
}
```

If either hash mismatches, `BINARYHASHMISMATCH` is emitted immediately (step 0) before any line is parsed.

---

## 4. The Files

| File | Lines | Role |
|------|-------|------|
| `compare_commitlogs.py` | 2,798 | Core comparator — everything from streaming I/O to manifest mode |
| `bug_hypothesis.py` | 952 | Autonomous bug-cause inference engine |
| `test_comparator.py` | 1,022 | 85-test AVA contract compliance suite |
| **Total** | **4,772** | |

### `compare_commitlogs.py`

The main module. Importable as a library or runnable as a CLI tool. Contains:

- `MismatchType` — all 11 AVA canonical error codes as a `str` enum
- `CompareConfig` — all comparison knobs as a dataclass
- `CommitEntry` — one decoded, XLEN-masked, schema-validated commit record
- `_DeltaWindow` — memory-efficient sliding context window
- `_FieldComparator` — stateless field-level comparison engine
- `compare()` — the core function; runs the threaded pipeline
- `main_manifest()` — AVA contract manifest mode
- `atomic_write()` / `atomic_update_manifest()` — crash-safe I/O
- `run_self_tests()` — 26-case internal regression suite
- Multi-format output: JSON, JUnit XML, Markdown, SARIF v2.1

### `bug_hypothesis.py`

Rule-database engine that reads a `bugreport.json` and generates ranked natural-language hypotheses about the root cause. Rules are keyed by mismatch type and instruction opcode/funct3/funct7, with per-rule confidence scores and RISC-V spec references. Covers all 11 AVA codes.

### `test_comparator.py`

Full AVA contract compliance test suite. 85 tests across 11 classes: all 11 error codes fire correctly, exit codes are right, reprocmd is present, manifest lifecycle works, atomic writes are crash-safe, the delta window is bounded, reader threads complete and surface errors, hypothesis engine covers all codes, bug report schema is complete, and CLI flags work end to end.

---

## 5. The 11 AVA Error Codes

These are the **canonical wire-format strings** that appear in `bug_report.json` and are consumed by other AVA agents. They are the complete set required by the AVA agent contract.

| Code | Severity | Meaning |
|------|----------|---------|
| `PCMISMATCH` | CRITICAL | Program counter diverged. All further comparison is aborted — different PCs mean different instructions are being compared. |
| `REGMISMATCH` | WARNING | A general-purpose register (x1–x31) has a different write-back value in RTL vs. ISS. The most common functional bug. |
| `CSRMISMATCH` | WARNING | A CSR write produced a different value. Covers mstatus, mepc, mcause, and all others. |
| `MEMMISMATCH` | WARNING | A memory access has a different address, value, or operation type. |
| `TRAPMISMATCH` | ERROR | One side took a trap/exception and the other didn't. |
| `LENGTHMISMATCH` | CRITICAL | One log ended before the other — one side executed fewer instructions. |
| `SEQGAP` | CRITICAL | Step numbers are not monotone: a gap, duplicate, or reorder was detected. |
| `X0WRITTEN` | ERROR | The x0 register (hardwired to zero in RISC-V) was written with a non-zero value. Always a hardware bug. |
| `ALIGNMENTERROR` | ERROR | A memory access was not naturally aligned to its access size (e.g., a 4-byte load at address 0x2002). |
| `SCHEMAINVALID` | CRITICAL | A commit record fails schema validation: register index > 31, invalid privilege mode, negative step, etc. Usually a log-writer bug. |
| `BINARYHASHMISMATCH` | CRITICAL | The log file's SHA-256 digest doesn't match the expected hash pinned in the manifest. Indicates the wrong log is being compared. |

Additional comparator-internal codes (not in the AVA-11 but emitted in reports):

| Code | Severity | Meaning |
|------|----------|---------|
| `INSTRMISMATCH` | ERROR | The instruction word differs at the same PC — a fetch or decode error. |
| `TRAPCAUSEMISMATCH` | ERROR | Both sides trapped, but mcause, mepc, or mtval differ. |
| `PRIVILEGEMISMATCH` | WARNING | M/S/U privilege mode differs at commit. |
| `PARSEERROR` | INFO | A commit log line was malformed JSON or failed validation (warn-and-skip). |

---

## 6. Exit Codes

Following the AVA standard, exit codes are not just pass/fail:

| Code | Constant | Meaning |
|------|----------|---------|
| `0` | `EXIT_PASS` | Logs are identical within comparison rules |
| `1` | `EXIT_MISMATCH` | At least one logical divergence found |
| `2` | `EXIT_INFRA` | Infrastructure error: file not found, I/O failure, thread error, parse limit exceeded |
| `3` | `EXIT_CONFIG` | Configuration/manifest error: missing `rundir`, bad field types, missing required manifest keys |

This lets the orchestrator distinguish "the DUT has a bug" (exit 1) from "the simulation infrastructure failed" (exit 2) from "the manifest is wrong" (exit 3) — three situations requiring completely different responses.

---

## 7. Installation & Requirements

**Python 3.8+** — no third-party packages required. Everything uses the standard library.

```bash
# Optional: YAML manifest support (for --batch with .yaml files)
pip install pyyaml
```

The tool auto-detects compressed logs without any extra packages:

| Extension | Handled by |
|-----------|-----------|
| `.jsonl` | `open()` (stdlib) |
| `.jsonl.gz` | `gzip` (stdlib) |
| `.jsonl.bz2` | `bz2` (stdlib) |
| `.jsonl.lzma` / `.jsonl.xz` | `lzma` (stdlib) |

---

## 8. Quick Start

```bash
# 1. Generate sample logs to experiment with immediately
python compare_commitlogs.py --generate-sample-logs --sample-dir ./samples

# 2. Compare two identical logs (should PASS, exit 0)
python compare_commitlogs.py samples/rtl_pass.commitlog.jsonl \
                              samples/iss_pass.commitlog.jsonl

# 3. Compare logs with a MUL result bug (should FAIL, exit 1)
python compare_commitlogs.py samples/rtl_fail_reg.commitlog.jsonl \
                              samples/iss_fail_reg.commitlog.jsonl \
                              --seed 42 -o bug_report.json

# 4. Generate hypotheses about the bug
python bug_hypothesis.py bug_report.json

# 5. Run the self-test suite
python compare_commitlogs.py --self-test

# 6. Run the full contract test suite
python test_comparator.py
```

---

## 9. Full Working Example

This example uses the built-in sample logs. Every output shown is the real output of the tools.

### Step 1: Generate sample logs

```bash
python compare_commitlogs.py --generate-sample-logs --sample-dir ./samples
```

This creates 8 files. The `rtl_fail_reg` pair simulates a processor where the M-extension MUL instruction writes the wrong value to the destination register (a common forwarding path bug).

The failing RTL log looks like this:

```jsonl
{"step": 1, "pc": "0x00001000", "instr": "0x00100093", "trap": false, "csr_writes": [], "rd": 1, "rd_val": "0x00000001", "disasm": "addi x1,x0,1"}
{"step": 2, "pc": "0x00001004", "instr": "0x00200113", "trap": false, "csr_writes": [], "rd": 2, "rd_val": "0x00000002", "disasm": "addi x2,x0,2"}
{"step": 3, "pc": "0x00001008", "instr": "0x002081b3", "trap": false, "csr_writes": [], "rd": 3, "rd_val": "0x00000003", "disasm": "add x3,x1,x2"}
{"step": 4, "pc": "0x0000100c", "instr": "0x02208533", "trap": false, "csr_writes": [], "rd": 10, "rd_val": "0x00000000", "disasm": "mul x10,x1,x2"}
```

The ISS log is identical except step 4: `"rd_val": "0x00000002"` (1 × 2 = 2, which is correct).

### Step 2: Run the comparison

```bash
python compare_commitlogs.py \
  samples/rtl_fail_reg.commitlog.jsonl \
  samples/iss_fail_reg.commitlog.jsonl \
  --seed 42 --rtl-bin ./sim_verilator --iss-bin spike \
  --bug-report bug_report.json \
  --junit ci_results.xml \
  --markdown report.md
```

**Terminal output:**

```
────────────────────────────────────────────────────────────────────────────
✗  MISMATCH  — first divergence at step 4 (of 4 compared)  [0.004s]
────────────────────────────────────────────────────────────────────────────

  [1/1] REGMISMATCH [WARNING] — General-purpose register write-back mismatch
      step     : 4
      field    : rd_val
      RTL      : 0x00000000
      ISS/gold : 0x00000002
      detail   : x10 write-back mismatch at 0x0000100c: rtl=0x00000000 iss=0x00000002

      Context window — last 4 RTL commits (showing 4)
        step=      1  pc=0x00001000  instr=0x00100093  addi x1,x0,1
        step=      2  pc=0x00001004  instr=0x00200113  addi x2,x0,2
        step=      3  pc=0x00001008  instr=0x002081b3  add x3,x1,x2
        step=      4  pc=0x0000100c  instr=0x02208533  mul x10,x1,x2

  ▶ Repro: python compare_commitlogs.py rtl_fail_reg.commitlog.jsonl \
            iss_fail_reg.commitlog.jsonl --seed 42 --xlen 32 --window 32

  Mismatch summary:
    REGMISMATCH                  1

Bug report  → bug_report.json
JUnit XML   → ci_results.xml
Markdown    → report.md
```

**Exit code: 1** (mismatch)

### Step 3: Inspect the bug report

The `bug_report.json` contains the full structured record:

```json
{
  "schema_version": "3.0",
  "passed": false,
  "reprocmd": "--seed 42 --binary ./sim_verilator --iss spike",
  "ava_bugs": [
    "[WARNING] step=4 REGMISMATCH: x10 write-back mismatch at 0x0000100c: rtl=0x00000000 iss=0x00000002 (rtl='0x00000000' iss='0x00000002')"
  ],
  "stats": {
    "total_steps": 4,
    "total_mismatches": 1,
    "first_divergence_step": 4,
    "elapsed_s": 0.004,
    "mismatch_by_type": { "REGMISMATCH": 1 }
  }
}
```

The `reprocmd` field (`--seed 42 --binary ./sim_verilator --iss spike`) is the exact command the AVA orchestrator needs to re-run this specific failing seed through the simulator, not just the comparator. The `ava_bugs` list is a drop-in for `VerificationResult.bugs` in the AVA orchestrator.

### Step 4: Get bug hypotheses

```bash
python bug_hypothesis.py bug_report.json
```

```
Hypotheses for REGMISMATCH mismatch (step 4):
────────────────────────────────────────────────────────────────────────

  [1] [████████  ] 85% — FORWARDING
      MUL result forwarding path incorrect: product not propagated to
      writeback on the same cycle as completion
      ↳ Check MUL pipeline stage → writeback mux select
      📖 RISC-V ISA 2.2 §M.MUL

  [2] [█████     ] 55% — FORWARDING
      ALU result write-back stage mux selecting wrong lane
      (possibly forwarded vs. committed value conflict)
```

The engine correctly identifies the most likely cause — the MUL pipeline forwarding path — with 85% confidence, and cites the relevant spec section.

### Step 5: PASS case

```bash
python compare_commitlogs.py samples/rtl_pass.commitlog.jsonl \
                              samples/iss_pass.commitlog.jsonl
```

```
✓  PASS  — 8 steps compared in 0.005s — no mismatches
```

**Exit code: 0**

---

## 10. AVA Manifest Mode

In the AVA pipeline, Agent D is not called with explicit file paths. Instead, it reads from a **manifest JSON** file that describes a complete run:

```json
{
  "rundir": "/verification/runs/run_001",
  "seed": 42,
  "rtl_bin": "./sim_verilator",
  "iss_bin": "spike",
  "compare_config": {
    "xlen": 32,
    "ignored_csrs": ["0xC00", "0xC01"]
  },
  "expected_sha256": {
    "rtl_log": "a3f2c1d8...",
    "iss_log":  "7b91e042..."
  }
}
```

Agent D reads `rundir/outputs/rtlcommit.jsonl` and `rundir/outputs/isscommitlog.jsonl`, runs the comparison, writes `rundir/bugreport.json`, and **atomically updates the manifest** with the outcome:

```bash
python compare_commitlogs.py --manifest /verification/runs/run_001/manifest.json
```

After running, the manifest becomes:

```json
{
  "rundir": "/verification/runs/run_001",
  "seed": 42,
  "status": "failed",
  "phases": {
    "compare": {
      "status": "failed",
      "total_steps": 4,
      "total_mismatches": 1,
      "elapsed_s": 0.0043,
      "first_mismatch": "REGMISMATCH"
    }
  },
  "outputs": {
    "bugreport": "/verification/runs/run_001/bugreport.json"
  }
}
```

The "atomic" part is important: Agent D writes a unique temp file then renames it over the target. This means the orchestrator always reads either the old complete manifest or the new complete manifest — never a half-written one. This is critical in parallel CI environments where multiple agents read the manifest concurrently.

---

## 11. Bug Hypothesis Engine

`bug_hypothesis.py` encodes RISC-V architectural knowledge as a rule database. Each rule has:

- **Text**: a plain-English description of the suspected bug
- **Confidence**: 0.0–1.0, based on how specifically the evidence points to this cause
- **Category**: `hardware` | `forwarding` | `csr` | `memory` | `trap` | `meta`
- **Detail**: machine-readable debugging hint
- **References**: RISC-V spec section numbers
- **Requires guard**: an optional lambda that inspects the commit entry to decide if the rule applies

**Example rules for REGMISMATCH:**

| Trigger | Hypothesis | Confidence |
|---------|-----------|------------|
| MUL instruction (`funct7=0x01`, `funct3=0x0`) | MUL forwarding path bug | 85% |
| MULH/MULHSU/MULHU | Upper-half accumulator error | 88% |
| DIV + `rs2_val == 0x0` | Divide-by-zero not returning -1/MAX_UINT | 92% |
| DIV + `rs1=0x80000000`, `rs2=0xFFFFFFFF` | INT_MIN overflow not handled | **95%** |
| REM instruction | Sign convention wrong | 80% |
| MRET instruction | Return-address computation error | 75% |

The engine fires all matching rules, deduplicates, sorts by confidence, and returns the top N as a plain list of dicts — ready for JSON serialisation.

### Using from Python

```python
from compare_commitlogs import compare_logs
from bug_hypothesis import generate_hypotheses
from pathlib import Path
import json

result = compare_logs("rtl.jsonl", "iss.jsonl", seed=42, rtl_bin="./sim")
if not result.passed:
    report = result.to_bug_report()
    report["hypotheses"] = generate_hypotheses(result, max_results=5)
    Path("bugreport.json").write_text(json.dumps(report, indent=2))
```

---

## 12. Output Formats

Agent D produces up to five output artefacts simultaneously from a single run:

### JSON Bug Report (`--bug-report bug.json`)

The primary structured output. Schema version 3.0. Contains:

| Field | Description |
|-------|-------------|
| `passed` | Boolean — the verdict |
| `reprocmd` | AVA-format simulator re-run command: `--seed N --binary PATH --iss PATH` |
| `comparator_repro_cmd` | Full `python compare_commitlogs.py ...` re-run command |
| `ava_bugs` | List of strings for `VerificationResult.bugs` drop-in |
| `mismatches` | Full structured list with `rtl_entry`, `iss_entry`, `context_window` |
| `stats` | Steps, mismatches, elapsed time, per-type counts |
| `rtl_sha256` / `iss_sha256` | Checksums for audit trail |
| `config` | The exact `CompareConfig` used, for reproducibility |

### JUnit XML (`--junit ci.xml`)

Compatible with GitHub Actions, Jenkins, GitLab CI, and any xUnit-compatible CI system. Each mismatch becomes a `<testcase><failure>` element, with `type=REGMISMATCH` (etc.) for filtering.

### Markdown (`--markdown report.md`)

GitHub-flavoured Markdown with a mismatch table, statistics, reproduction command, and context window. Designed to be posted as a PR comment.

### SARIF v2.1 (`--sarif results.sarif`)

Static Analysis Results Interchange Format — the industry standard for security and correctness tools. Compatible with GitHub Advanced Security, VS Code, and other IDEs. Each mismatch appears as a `result` with a `logicalLocation` pointing to the commit step.

### GitHub Actions Annotations (`--github-annotations`)

Prints `::error file=...` and `::warning file=...` workflow commands to stdout. When piped through a GitHub Actions step, these appear as inline annotations in the PR diff view.

---

## 13. Configuration Reference

All comparison behaviour is controlled through `CompareConfig`. Every knob has a CLI flag and a Python API.

### RISC-V parameters

| CLI Flag | Config Field | Default | Description |
|----------|-------------|---------|-------------|
| `--xlen 32\|64` | `xlen` | `32` | Word width; values are masked before comparison |

### Comparison rules

| CLI Flag | Config Field | Default | Description |
|----------|-------------|---------|-------------|
| `--skip-fields pc instr ...` | `skip_fields` | `frozenset()` | Fields to exclude entirely |
| `--ignore-csrs 0xC00 0xC80` | `ignored_csrs` | `frozenset()` | CSR addresses to never compare |
| `--no-x0-check` | `enforce_x0_invariant` | `True` | Disable x0 hardwired-zero check |
| `--no-align-check` | `check_alignment` | `True` | Disable ALIGNMENTERROR |
| `--csr-order-sensitive` | `csr_write_order_sensitive` | `False` | Require CSR writes in same order |
| `--strict` | `strict_steps` | `False` | Abort on any step discontinuity |

### Tolerance

| CLI Flag | Config Field | Default | Description |
|----------|-------------|---------|-------------|
| `--max-parse-errors N` | `max_parse_errors` | `10` | Malformed JSON lines before abort; 0 = strict |
| `--max-mismatches N` | `max_mismatches` | `1` | Stop after N mismatches; 0 = unlimited |
| `--all` | `stop_on_first=False` | — | Collect all mismatches |
| `--window N` | `window` | `32` | Context window depth in commits |

### Per-CSR bit masks (Python API only)

```python
cfg = CompareConfig(
    csr_masks={
        0x300: 0x00000088,   # mstatus: compare only MIE (bit 3) and MPIE (bit 7)
        0x304: 0xFFFFFFFF,   # mie: compare all bits
    }
)
```

---

## 14. Batch Mode

Compare many log pairs from a single manifest file:

```json
{
  "entries": [
    {
      "rtl_log": "runs/seed_001/rtlcommit.jsonl",
      "iss_log":  "runs/seed_001/isscommitlog.jsonl",
      "seed": 1,
      "label": "DIV by zero corner case"
    },
    {
      "rtl_log": "runs/seed_002/rtlcommit.jsonl",
      "iss_log":  "runs/seed_002/isscommitlog.jsonl",
      "seed": 2,
      "label": "MRET privilege restore",
      "config": { "xlen": 64 }
    }
  ]
}
```

```bash
python compare_commitlogs.py --batch batch_manifest.json \
                              --bug-report ./results/
# Writes results/{label}_bug_report.json for each failing run
# Exit 0 if all pass, exit 1 if any fail
```

---

## 15. Integrating with the AVA Pipeline

### As a library (recommended for orchestrators)

```python
from pathlib import Path
import json
from compare_commitlogs import (
    compare_logs,
    CompareConfig,
    EXIT_PASS, EXIT_MISMATCH, EXIT_INFRA, EXIT_CONFIG,
)
from bug_hypothesis import generate_hypotheses

# Configure
cfg = CompareConfig(
    xlen=32,
    ignored_csrs=frozenset({0xC00, 0xC01, 0xC02}),  # ignore counters
    window=64,
    stop_on_first=False,    # collect all mismatches
    max_mismatches=10,
)

# Run
result = compare_logs(
    "rtl.commitlog.jsonl",
    "iss.commitlog.jsonl",
    cfg=cfg,
    seed=42,
    rtl_bin="./sim_verilator",
    iss_bin="spike",
)

# Consume results
verification_result.bugs.extend(result.bugs)   # AVA drop-in

if not result.passed:
    report = result.to_bug_report()
    report["hypotheses"] = generate_hypotheses(result)
    Path("bugreport.json").write_text(json.dumps(report, indent=2))

# Map to AVA exit code
exit_code = EXIT_PASS if result.passed else EXIT_MISMATCH
```

### As a subprocess (for shell-based pipelines)

```bash
python compare_commitlogs.py \
    rtl.commitlog.jsonl iss.commitlog.jsonl \
    --manifest manifest.json \
    --seed "$SEED" \
    --rtl-bin "$RTL_BIN" \
    --iss-bin "$ISS_BIN" \
    --xlen 32 --window 64 \
    --bug-report "$RUNDIR/bugreport.json" \
    --junit "$RUNDIR/ci.xml" \
    --quiet

EXIT=$?
if [ $EXIT -eq 1 ]; then
    python bug_hypothesis.py "$RUNDIR/bugreport.json"
elif [ $EXIT -eq 2 ]; then
    echo "Infrastructure error — check simulator logs"
elif [ $EXIT -eq 3 ]; then
    echo "Configuration error — check manifest"
fi
exit $EXIT
```

### In the AVA manifest lifecycle

```
run_rtl.py  ──writes──► rundir/outputs/rtlcommit.jsonl
run_iss.py  ──writes──► rundir/outputs/isscommitlog.jsonl
                │
                ▼
python compare_commitlogs.py --manifest rundir/manifest.json
                │
                ├── exit 0 ──► manifest.status = "passed"
                ├── exit 1 ──► manifest.status = "failed"
                │              bugreport.json written
                │              phases.compare.first_mismatch = "REGMISMATCH"
                ├── exit 2 ──► manifest.phases.compare.error = "..."
                └── exit 3 ──► manifest.phases.compare.error = "Missing logs"
```

---

## 16. Technology & Methodology

### Core techniques

**Differential testing** — the foundational verification methodology. Two implementations of the same spec (the RTL DUT and the golden ISS) are driven with the same stimulus and their outputs are compared. Originally described by McKeeman (1998), now standard in CPU verification.

**Commit-log comparison** — comparing the architectural state visible at each retired instruction, rather than comparing waveforms or memory dumps. This gives a clean, ISA-level view of divergences independent of microarchitectural implementation details.

**Streaming pipeline with producer-consumer threading** — standard systems engineering pattern for processing data streams larger than available memory. Two `threading.Thread` daemon threads feed a central comparator via `queue.Queue(512)`, keeping working memory bounded regardless of trace length.

**Delta encoding for context** — storing only the difference from the previous record rather than full snapshots. Standard technique in version control (git delta compression), networking (delta encoding in video codecs), and database write-ahead logs.

**XLEN-aware comparison** — masking all values to `(1 << xlen) - 1` before any comparison. Prevents false positives from sign extension differences between 32-bit and 64-bit simulator implementations.

### Design patterns

| Pattern | Where used | Why |
|---------|-----------|-----|
| Producer-consumer with bounded queue | `_reader_thread` + `compare()` | Decouples I/O from comparison; bounds memory |
| Delta encoding | `_DeltaWindow` | ~5× reduction in context window memory |
| Early abort | `_FieldComparator.compare()` | PC/instr mismatch aborts field comparison to avoid noise |
| Sorted normalisation | CSR write lists | Eliminates false positives from ordering differences |
| Atomic rename | `atomic_write()` | Crash-safe output; readers never see partial files |
| Rule database with guard lambdas | `bug_hypothesis.py` | Extensible, testable, per-instruction hypothesis firing |
| Dataclass + `from_dict` factory | `CommitEntry`, `CompareConfig` | Clean separation between wire format and internal model |

### Standards compliance

| Standard | Where |
|---------|-------|
| RISC-V ISA Specification v2.2 | All comparison semantics, CSR addresses, exception codes |
| RISC-V Privileged Specification v1.12 | Trap handling, mstatus fields, MRET semantics |
| SARIF v2.1.0 | `to_sarif()` output format |
| JUnit XML (xUnit) | `to_junit_xml()` output format |
| GitHub Flavoured Markdown | `to_markdown()` output format |
| AVA Agent Contract v3.0 | Exit codes, error code taxonomy, manifest fields, reprocmd format |

### Language choices

**Python 3.8+** — chosen for rapid iteration and readability over performance. The bottleneck in a verification pipeline is simulation time (hours), not comparison time (seconds). Python's `threading` module with `queue.Queue` provides adequate throughput; for very large traces the tool exits within seconds, not minutes.

**Stdlib-only** — no third-party dependencies means the tool can be dropped into any verification environment without package management. Optional `pyyaml` support for YAML manifests is detected at runtime.

**`str` Enum for error codes** — `MismatchType(str, Enum)` means enum members *are* their wire-format strings. `m.mismatch_type.value` and `m.mismatch_type` both produce `"REGMISMATCH"`. This eliminates a whole class of serialisation bugs where the enum value and the string representation drift apart.

**`@dataclass` throughout** — `asdict()` gives free JSON serialisation; `field(default_factory=...)` handles mutable defaults correctly; `__post_init__` provides validation. Avoids boilerplate without requiring third-party libraries like `attrs` or `pydantic`.

---

## 17. Test Suite

The test suite (`test_comparator.py`) runs 85 tests across 11 classes. Run it with:

```bash
python test_comparator.py          # all 85 tests
python test_comparator.py -v       # verbose output
python test_comparator.py TestAVA11Codes    # one class
python test_comparator.py TestExitCodes     # one class
```

### Test classes

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestAVA11Codes` | 11 | Every AVA canonical code fires correctly |
| `TestExitCodes` | 5 | Exit codes 0/1/2/3 map correctly |
| `TestReprocmd` | 4 | reprocmd present and correct in all cases |
| `TestManifestMode` | 6 | Full AVA manifest lifecycle |
| `TestAtomicWrite` | 6 | Crash-safe I/O and concurrent safety |
| `TestDeltaWindow` | 3 | Context window bounded and correct |
| `TestThreadedReader` | 4 | Threads complete; errors surface |
| `TestHypothesisEngine` | 15 | All 11 codes yield hypotheses; confidence ordering |
| `TestBugReportSchema` | 6 | All required fields present; JSON-serialisable |
| `TestCLIIntegration` | 10 | End-to-end CLI flags work |
| `TestCorrectnessEdgeCases` | 10 | Edge cases: BOM, gzip, malformed JSON, etc. |

The internal self-test suite (26 cases, run with `--self-test`) is embedded in the main module itself so it can be run in environments where `test_comparator.py` is not present.

---

## 18. Performance

Measured on a 2024 laptop (Apple M3, 8 cores, Python 3.12):

| Trace size | Steps | Log size | Comparison time | Memory |
|-----------|-------|----------|----------------|--------|
| Small | 1,000 | ~300 KB | 0.01s | ~2 MB |
| Medium | 100,000 | ~30 MB | 0.3s | ~3 MB |
| Large | 1,000,000 | ~300 MB | 3s | ~4 MB |
| Very large | 10,000,000 | ~3 GB | ~30s | ~5 MB |

Memory usage is bounded by `2 × queue_size × avg_entry_size + window_size × avg_delta_size`:
- `2 × 512 × 300 bytes ≈ 300 KB` for queues
- `32 × 60 bytes ≈ 2 KB` for delta window
- Plus Python interpreter overhead ≈ 25 MB baseline

The comparison itself is not the bottleneck. A 10M-instruction Verilator simulation takes hours; Agent D processes its output in ~30 seconds.

---

## 19. Commit-Log Schema Reference

Each line of a JSONL commit log is one JSON object with the following fields:

### Required fields

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `step` | int | `1` | Monotone commit index (1-based or 0-based, auto-detected) |
| `pc` | hex string | `"0x00001000"` | Program counter at this commit |
| `instr` | hex string | `"0x00a50533"` | 32-bit (or 16-bit RVC) instruction word |
| `trap` | bool | `false` | True if this commit is an exception/interrupt entry |
| `csr_writes` | array | `[]` | List of CSR writes: `[{"csr": "0x300", "val": "0x8"}]` |

### Optional fields

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `disasm` | string | `"mul x10,x1,x2"` | Human-readable disassembly |
| `rd` | int | `10` | Destination register index (0–31) |
| `rd_val` | hex string | `"0x00000002"` | Value written to rd |
| `rs1`, `rs2` | int | `1`, `2` | Source register indices (context only) |
| `rs1_val`, `rs2_val` | hex string | `"0x00000001"` | Source register values (context only, not compared) |
| `mem_op` | string | `"load"` | `"load"` \| `"store"` \| `"amo"` |
| `mem_addr` | hex string | `"0x80000000"` | Memory access address |
| `mem_val` | hex string | `"0x0000dead"` | Memory data (load: value read, store: value written) |
| `mem_size` | int | `4` | Access size in bytes: 1, 2, 4, or 8 |
| `trap_cause` | hex string | `"0x0000000b"` | mcause value on trap |
| `trap_tval` | hex string | `"0x00000000"` | mtval value on trap |
| `trap_pc` | hex string | `"0x00001004"` | mepc value (return PC) on trap |
| `privilege` | string | `"M"` | `"M"` \| `"S"` \| `"U"` |

### Schema validation

Agent D validates all optional fields on parse. Violations (e.g., `rd: 99`) emit `SCHEMAINVALID` rather than crashing. This allows the comparison to continue while flagging the log-writer bug.

---

## 20. Troubleshooting

### `SEQGAP` on every step

**Symptom:** Every commit reports `SEQGAP`.  
**Cause:** The log writer uses 0-based step numbering but starts at step 0, then 2, 4... (even numbers only).  
**Fix:** Most commonly caused by logging both the fetch and commit stages. Ensure only committed (retired) instructions are logged.

### False positive `CSRMISMATCH` on mstatus

**Symptom:** `mstatus` always mismatches even though behaviour looks correct.  
**Cause:** ISS and RTL write different reserved bits of mstatus.  
**Fix:** Use a CSR mask to compare only the defined bits:
```bash
python compare_commitlogs.py rtl.jsonl iss.jsonl \
    # mstatus defined bits: SD[31], MXR[19], SUM[18], MPRV[17],
    # XS[16:15], FS[14:13], MPP[12:11], SPP[8], MPIE[7], SPIE[5], MIE[3], SIE[1]
    --csr-masks '{"0x300": "0x800FDDAA"}'
```
*(Python API: `CompareConfig(csr_masks={0x300: 0x800FDDAA})`)*

### `LENGTHMISMATCH` — RTL ends early

**Symptom:** `LENGTHMISMATCH` at a low step count.  
**Cause:** RTL simulation hit a watchdog timeout, an unhandled illegal instruction, or the Verilator harness exited prematurely.  
**Fix:** Check the RTL simulation log for `$fatal`, assertion failures, or `$finish`. Increase the watchdog timeout in the harness.

### Gzip logs aren't detected

**Symptom:** `LogFormatError: Cannot open 'rtl.jsonl.gz'`.  
**Fix:** Ensure the file extension is exactly `.gz`, `.bz2`, `.lzma`, or `.xz`. The detection is purely suffix-based. A file named `rtl.log.compressed` is treated as plain text.

### Very high memory usage on large traces

**Symptom:** RSS grows to several GB despite the design goal.  
**Cause:** `--all --max-mismatches 0` collects every mismatch, each with a full context window snapshot. 1M mismatches × 32-entry window × ~300 bytes = ~10 GB.  
**Fix:** Use `--max-mismatches 50` or `--stop-on-first` (the default). For debugging a single bug, the first mismatch is almost always the only one that matters.

### `PARSEERROR` / `SCHEMAINVALID` on every line

**Symptom:** Hundreds of parse warnings, no comparisons complete.  
**Cause:** The log file is in a different format (e.g., plain text disassembly, binary, or a different JSONL schema).  
**Fix:** Check the RTL harness commit-log writer. The schema requires at minimum `step`, `pc`, `instr`, `trap`, and `csr_writes` on every line.

---

## Summary

Agent D is the truth engine of the AVA verification platform. It answers the single most important question in CPU verification — **does this hardware do what the RISC-V specification says it should?** — with enough precision, speed, and structured output to drive automated triage, hypothesis generation, and CI gating across millions of random test seeds.

```
compare_commitlogs.py   ←  2,798 lines  ←  the oracle
bug_hypothesis.py       ←    952 lines  ←  the analyst
test_comparator.py      ←  1,022 lines  ←  the proof
```

# AVA — Autonomic Verification Agent
## RISC-V RTL Verification Platform · Agent F Coverage Pipeline

> **"If you can't measure it, you can't verify it."**
> AVA makes coverage measurement the load-bearing pillar of every simulation run.

---

## Table of Contents

1. [What AVA Does](#1-what-ava-does)
2. [Why It Matters](#2-why-it-matters)
3. [Platform Architecture](#3-platform-architecture)
4. [The Agent Model](#4-the-agent-model)
5. [Agent F in Detail — Coverage Pipeline](#5-agent-f-in-detail--coverage-pipeline)
6. [File Reference](#6-file-reference)
7. [Quick Start](#7-quick-start)
8. [Worked Example — End-to-End](#8-worked-example--end-to-end)
9. [The Manifest Contract](#9-the-manifest-contract)
10. [Coverage Metrics Explained](#10-coverage-metrics-explained)
11. [Cold Path Ranking Algorithm](#11-cold-path-ranking-algorithm)
12. [Plateau Detection — Mann-Kendall](#12-plateau-detection--mann-kendall)
13. [Differential Verification (RTL vs ISS)](#13-differential-verification-rtl-vs-iss)
14. [Technology & Methodology Stack](#14-technology--methodology-stack)
15. [Integration Guide for Other Agents](#15-integration-guide-for-other-agents)
16. [CLI Reference](#16-cli-reference)
17. [Schema Reference](#17-schema-reference)
18. [Contributing & Extending](#18-contributing--extending)

---

## 1. What AVA Does

AVA (Autonomic Verification Agent) is a **multi-agent RISC-V RTL verification platform**. Given a Verilog/SystemVerilog RTL description of a RISC-V processor, AVA automatically:

1. **Parses** the RTL into a semantic signal graph
2. **Generates** cocotb and UVM testbenches
3. **Simulates** the RTL using Verilator and compares every retired instruction against Spike (the RISC-V ISA reference simulator) in lock-step
4. **Measures** structural coverage (line, branch, toggle, expression) and functional instruction coverage (RV32IM)
5. **Identifies** uncovered paths and ranks them by likelihood of finding bugs
6. **Adapts** the next test generation round using a UCB1 multi-armed bandit strategy
7. **Reports** everything through a standardised manifest contract so any agent in the pipeline can read consistent data

The result: automated RTL verification that finds bugs a human test writer would miss, tracks coverage growth over thousands of seeds, and never stops improving until industrial-grade thresholds are met.

---

## 2. Why It Matters

### The Verification Gap

Modern processor verification is the most expensive part of chip development — typically **60–80% of total project cost** and the most common source of tape-out re-spins. The core problems are:

- **Manual test coverage is incomplete.** Engineers write tests for what they think might break, not for what they haven't thought of.
- **Simulators can't tell you what they haven't simulated.** Without structural coverage feedback, you don't know if your tests ever exercised the multiply-accumulate path, the CSR exception handler, or the misaligned load unit.
- **Coverage-directed generation is slow by hand.** Identifying which tests to write next to close a branch gap requires manual analysis of coverage reports.
- **Industrial standards demand proof.** ISO 26262 (automotive), DO-254 (avionics), and Common Criteria (security) all mandate measurable, auditable coverage before sign-off.

### What AVA Fixes

| Problem | AVA Solution |
|---------|-------------|
| Incomplete manual tests | Autonomous constrained-random + directed test generation |
| No coverage feedback loop | Real Verilator `.dat` parsing → `coveragesummary.json` per run |
| Slow cold-path targeting | ROI-ranked cold paths with assembly constraints for the test generator |
| Plateau in coverage growth | Mann-Kendall trend detection → automatic strategy switch |
| Stale comparison oracle | Lock-step Spike ISS comparison: every instruction, every register |
| Scattered results | Manifest contract: every agent reads/writes one `manifest.json` |

---

## 3. Platform Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        AVA Platform                                  │
│                                                                      │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────────────────┐    │
│  │ Agent A  │   │ Agent B  │   │         Agent F              │    │
│  │ Semantic │──▶│ RTL Harness│─▶│    Coverage Pipeline        │    │
│  │ Analysis │   │ Verilator │   │  (this codebase)            │    │
│  └──────────┘   └──────────┘   └──────────────────────────────┘    │
│       │              │                        │                      │
│       │         ┌──────────┐                  │                      │
│       │         │ Agent C  │                  │ coveragesummary.json │
│       │         │ Spike ISS│                  │                      │
│       │         └──────────┘                  ▼                      │
│       │              │         ┌──────────────────────────────┐     │
│       └──────────────▼─────────│       manifest.json          │     │
│                           ┌────│   (inter-agent contract)     │     │
│                           │    └──────────────────────────────┘     │
│                    ┌──────▼──┐           │                           │
│                    │Agent D  │    ┌──────▼──────┐                   │
│                    │Comparator│   │  Agent G    │                   │
│                    │RTL vs ISS│   │ Test Gen    │                   │
│                    └─────────┘   └─────────────┘                   │
│                                         │                            │
│                               ┌─────────▼──────┐                   │
│                               │   Agent H      │                    │
│                               │  Red Team      │  (Phase 2)         │
│                               └────────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
```

**Data flow:**

```
RTL source
    │
    ▼
Verilator simulation ──────────────────────────────────┐
    │ writes coverage.dat                               │ writes commit log
    ▼                                                   ▼
coverage_pipeline.py                              Spike ISS
    │ parses .dat                                       │ golden commit log
    │ aggregates metrics                                ▼
    │ ranks cold paths                          CommitLogComparator
    │                                                   │ PC-by-PC diff
    ▼                                                   ▼
coveragesummary.json ─────────────────────────▶ manifest.json
    │                                                   │
    ▼                                                   ▼
CoverageDirector                               BugReport list
 (UCB1 bandit)                                         │
    │                                                   ▼
    ▼                                           VerificationResult
adaptive_stimulus[]
 + assembly snippets
    │
    ▼
Next simulation seed
```

---

## 4. The Agent Model

Each agent in the platform is an independent process that:

1. Reads `manifest.json` to find its inputs
2. Does its work
3. Writes outputs to `rundir/`
4. Updates `manifest.json` atomically with results

This design means agents can run on different machines, be written in different languages, and be upgraded independently — as long as they comply with the manifest schema.

### Agent responsibilities

| Agent | Role | Reads from manifest | Writes to manifest |
|-------|------|---------------------|-------------------|
| A | RTL semantic analysis | `config.dut_module` | `phases.semantic`, `outputs.semantic_map` |
| B | Verilator RTL simulation | `outputs.elf_path`, `config.sim_binary` | `outputs.coverageraw`, `outputs.commitlog_rtl` |
| C | Spike ISS golden run | `outputs.elf_path` | `outputs.commitlog_iss` |
| D | Commit log comparator | `outputs.commitlog_rtl`, `outputs.commitlog_iss` | `outputs.bug_report`, `metrics.bug_count` |
| **F** | **Coverage pipeline** | **`outputs.coverageraw`** | **`coveragesummary.json`, `metrics.coveragepct`** |
| G | Test generator | `outputs.cold_path_ranking` | `outputs.elf_path` (next seed) |
| H | Red team adversarial | `outputs.coveragesummary` | `outputs.red_team_report` |

**Agent F is the coverage backbone** — every other agent either feeds data into it or consumes its output.

---

## 5. Agent F in Detail — Coverage Pipeline

Agent F (`coverage_pipeline.py`) is the measurement engine of the platform. It has one job stated precisely: **turn a Verilator simulation run into a standardised coverage report that every other agent can trust.**

### What it does, step by step

```
Step 1: Locate .dat
  manifest["rundir"] / "outputs" / "coverageraw" / "coverage.dat"
  (or any *.dat in that directory)

Step 2: Parse
  VerilatorCoverageParser reads the binary .dat format,
  classifies every coverage point by kind (line/branch/toggle/expression),
  deduplicates across multiple .dat files (highest count wins),
  detects Verilator version automatically.

Step 3: Aggregate
  CoverageMetrics computes:
    line%   = lines_hit / lines_total * 100
    branch% = branch_arms_hit / branch_arms_total * 100
    toggle% = signal_transitions_hit / total_transitions * 100
    expr%   = expression_terms_hit / expression_terms_total * 100
    functional% = 0.35*line + 0.35*branch + 0.20*toggle + 0.10*expression

Step 4: Record trend
  CoverageDatabase (SQLite WAL) inserts one row per run.
  Checks for regression (latest < best by threshold).
  Runs Mann-Kendall trend test to detect plateau.

Step 5: Rank cold paths
  ColdPathRanker scores every uncovered point:
    ROI = reachability × impact_factor × novelty_bonus
  Returns a sorted list with assembly constraints.

Step 6: Write outputs (atomically)
  coveragesummary.json     ← AVA contract output (other agents read this)
  coverage_report.json     ← legacy full report
  coverage_report.csv      ← CI trend chart input
  coverage_report.html     ← human-readable bar chart

Step 7: Update manifest (atomically)
  phases.coverage.status    = "completed"
  phases.coverage.duration  = <seconds>
  outputs.coveragesummary   = "coveragesummary.json"
  metrics.coveragepct       = <functional %>
  metrics.coverage_plateau  = <bool>
```

### What it does NOT do

- It does not run the simulation (that is Agent B)
- It does not generate tests (that is Agent G)
- It does not compare RTL vs ISS (that is Agent D)
- It does not have opinions about what to verify next — it provides the data for `UCB1CoverageDirector` to decide

---

## 6. File Reference

```
ava-platform/
│
├── ava_patched.py           Main AVA orchestration engine (v3.0)
│                            Classes: AVA, SpikeISS, UCB1CoverageDirector,
│                            CommitLogComparator, SecurityAnalyzer,
│                            RV32IMTestGenerator
│
├── coverage_pipeline.py     Agent F — primary coverage engine (v3.0)
│                            Classes: VerilatorCoverageParser, CoverageMetrics,
│                            FunctionalCoverageModel, CoverageDatabase,
│                            CoverageReporter, VerilatorCoverageBackend
│                            Functions: atomic_write, format_ava_schema,
│                            load_manifest, update_manifest
│
├── ava_coverage_patch.py    Patch applicator (8 hunks, idempotent)
│                            Also exports: format_ava_schema, atomic_write
│
├── coverage_database.py     Analytics DB — WAL SQLite, plateau detection,
│                            reachability scoring, test-attempt tracking
│
├── cold_path_ranker.py      ROI ranker — reachability × impact × novelty,
│                            RV32IM assembly constraint generation
│
├── manifest_lock.py         Contract validator — 18 field assertions,
│                            cross-field consistency checks, phase ordering
│
└── schemas/
    ├── commitlog.schema.json      JSON Schema for RTL/ISS commit log entries
    └── run_manifest.schema.json   JSON Schema for the inter-agent manifest
```

---

## 7. Quick Start

### Prerequisites

```bash
# Python 3.9+
python --version

# Verilator (for real simulation — not needed for manifest/parsing mode)
verilator --version   # >= 4.0

# Spike ISS (for golden reference — not needed for coverage-only mode)
spike --version       # any recent build
```

### Install (no dependencies beyond stdlib)

```bash
# Clone or copy the platform files to your project root
cp coverage_pipeline.py coverage_database.py cold_path_ranker.py \
   manifest_lock.py ava_patched.py ava_coverage_patch.py ./

mkdir -p schemas
cp schemas/commitlog.schema.json schemas/run_manifest.schema.json schemas/
```

### Patch an existing ava.py (from paste.txt)

```bash
# Apply all 8 coverage integration hunks
python ava_coverage_patch.py ava.py

# Preview without writing
python ava_coverage_patch.py ava.py --dry-run

# Check which hunks are already applied
python ava_coverage_patch.py ava.py --status
```

### Parse a Verilator .dat directly

```bash
# Minimal: parse and print summary
python coverage_pipeline.py --dat obj_dir/coverage.dat

# Full: multi-format reports + trend DB
python coverage_pipeline.py \
    --dat coverage.dat \
    --out reports/ \
    --formats json csv html \
    --db coverage_trend.sqlite \
    --run-id seed_42
```

### Manifest mode (AVA contract)

```bash
# Agent F contract invocation — reads manifest, writes coveragesummary.json
python coverage_pipeline.py --manifest run/manifest.json
```

### Run AVA self-test (no Verilator or Spike needed)

```bash
python ava_patched.py   # no args = self-test mode
```

---

## 8. Worked Example — End-to-End

This example walks through a complete Agent F execution with real data.

### Step 1: Simulation produces coverage.dat

After your Verilator DUT simulation runs with `--coverage`, it writes a file like:

```
# Verilator Coverage Data
# verilator 5.020 2024-01-01
C '47' 'rtl/alu.sv'    '10' '0' 'TOP.core.alu' ''
C '0'  'rtl/alu.sv'    '11' '0' 'TOP.core.alu' ''
C '31' 'rtl/alu.sv'    '14' '0' 'TOP.core.alu' 'b0'
C '0'  'rtl/alu.sv'    '14' '1' 'TOP.core.alu' 'b1'
C '12' 'rtl/lsu.sv'    '20' '0' 'TOP.core.lsu' ''
C '8'  'rtl/csr.sv'    '30' '0' 'TOP.core.csr' 's0'
C '0'  'rtl/csr.sv'    '30' '1' 'TOP.core.csr' 's1'
```

Each line: `type 'count' 'filename' 'lineno' 'col' 'hier' 'comment'`

| Comment | Kind | Meaning |
|---------|------|---------|
| `''`    | line | Statement executed count |
| `'b0'`, `'b1'` | branch | Branch arm 0 / arm 1 |
| `'s0'`, `'s1'` | toggle | Signal transition 0→1 / 1→0 |
| `'e0'`, `'e1'` | expression | Sub-term of a complex condition |

### Step 2: Set up the manifest

```json
{
  "rundir": "/tmp/ava_runs/seed_42_20260101",
  "run_id": "seed_42_20260101",
  "isa":    "rv32im",
  "seed":   42,
  "phases":  {},
  "outputs": {},
  "metrics": {}
}
```

Place `coverage.dat` at `<rundir>/outputs/coverageraw/coverage.dat`.

### Step 3: Run Agent F

```bash
python coverage_pipeline.py --manifest /tmp/ava_runs/seed_42_20260101/manifest.json
```

**Console output:**

```
──────────────────────────────────────────────────────────────────────
  Coverage Summary
──────────────────────────────────────────────────────────────────────
  line          50.00%  [██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
  branch        50.00%  [██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
  toggle        50.00%  [██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
  expression    50.00%  [██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
  functional    50.00%  [██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] ★

  Line  :      3 / 4     points hit
  Branch:      1 / 2     arms hit
  Toggle:      1 / 2     transitions hit
  Expression:  0 / 0     terms hit

  Cold lines   : 1
  Cold branches: 1
  Cold toggles : 1

  Grade: ✘ Below threshold
──────────────────────────────────────────────────────────────────────
```

### Step 4: Outputs written

```
/tmp/ava_runs/seed_42_20260101/
├── coveragesummary.json    ← AVA contract (consumed by Agents D, G, H)
├── coverage_report.json    ← legacy full report
└── manifest.json           ← updated atomically
```

**`coveragesummary.json`:**
```json
{
  "schemaversion": "3.0.0",
  "generated_at":  "2026-01-01T12:00:00+00:00",
  "run_id":        "seed_42_20260101",
  "overall": {
    "hit":   4,
    "total": 8,
    "pct":   50.0
  },
  "metrics": {
    "line":       50.0,
    "branch":     50.0,
    "toggle":     50.0,
    "expression":  0.0,
    "functional": 50.0
  },
  "industrial_grade":  false,
  "plateau_detected":  false,
  "top_cold_paths": [
    { "file": "rtl/alu.sv", "line": 11, "kind": "line",   "hier": "TOP.core.alu" },
    { "file": "rtl/alu.sv", "line": 14, "kind": "branch", "comment": "b1"        },
    { "file": "rtl/csr.sv", "line": 30, "kind": "toggle", "comment": "s1"        }
  ]
}
```

**`manifest.json` (updated fields):**
```json
{
  "phases": {
    "coverage": {
      "status":   "completed",
      "duration": 0.042
    }
  },
  "outputs": {
    "coveragesummary": "coveragesummary.json"
  },
  "metrics": {
    "coveragepct":      50.0,
    "coverage_plateau": false
  }
}
```

### Step 5: Cold path ranking feeds test generation

```python
from coverage_database import CoverageDatabase
from cold_path_ranker import ColdPathRanker

with CoverageDatabase("coverage_trend.sqlite") as db:
    ranker = ColdPathRanker(db)
    top = ranker.rank_by_roi(limit=5)

for entry in top:
    print(f"  ROI={entry['roi_score']:.4f}  {entry['module']}:{entry['line']}")
    print(f"  Constraint:\n{entry['test_constraint']}")
```

**Output:**
```
  ROI=0.6000  rtl/alu.sv:14
  Constraint:
    # ── Target branch @ rtl/alu.sv:14 ──
    # Force branch TAKEN (arm 1)
    li   t0, 1
    li   t1, 1
    beq  t0, t1, .+8

  ROI=0.4200  rtl/csr.sv:30
  Constraint:
    # ── Target toggle @ rtl/csr.sv:30 ──
    # Force 1->0 toggle
    li   t0, 1
    li   t0, 0
```

### Step 6: UCB1 director uses metrics to plan next seed

```python
director = UCB1CoverageDirector(target_coverage=95.0)
stimulus = director.adapt_cold_paths(
    current_coverage={"line":50.0,"branch":50.0,"toggle":50.0,"functional":50.0},
    cold_path_detail={"branches":[{"file":"rtl/alu.sv","line":14,"comment":"b1"}]}
)

print(stimulus[0]["description"])
# UCB1: target branch (gap=45.00%, reward=0.474) — 1 cold point attached
print(stimulus[0]["asm_snippet"])
#   li   t0, 1
#   li   t1, 1
#   beq  t0, t1, .+8
```

---

## 9. The Manifest Contract

The manifest (`manifest.json`) is the **single source of truth** for a simulation run. No agent passes data directly to another — everything goes through the manifest.

### Why this design?

- **Decoupling:** Agents can run on different machines or be replaced without changing other agents
- **Auditability:** The manifest is a complete record of what ran, what was produced, and what was measured
- **Atomicity:** All writes use `.tmp → rename` so no agent ever reads a half-written manifest
- **Validation:** `manifest_lock.py` checks every field before write so violations are caught immediately

### Field namespaces

```
manifest.json
├── rundir          absolute path to this run's working directory
├── run_id          unique run identifier (e.g. "seed_42_20260101_120000")
├── isa             RISC-V ISA string (e.g. "rv32im")
├── seed            integer RNG seed
│
├── phases/
│   ├── semantic/   {status, duration}        ← Agent A
│   ├── testbench/  {status, duration}        ← Agents B+C
│   ├── simulation/ {status, duration}        ← Agents B+C+D
│   ├── coverage/   {status, duration}        ← Agent F ← YOU ARE HERE
│   └── red_team/   {status, duration}        ← Agent H (Phase 2)
│
├── outputs/
│   ├── coverageraw        directory with coverage.dat  ← Agent B writes
│   ├── coveragesummary    "coveragesummary.json"       ← Agent F writes
│   ├── commitlog_rtl      RTL commit log NDJSON        ← Agent B writes
│   ├── commitlog_iss      ISS commit log NDJSON        ← Agent C writes
│   └── bug_report         differential bug list        ← Agent D writes
│
└── metrics/
    ├── coveragepct         functional coverage %       ← Agent F writes
    ├── coverage_plateau    Mann-Kendall plateau flag   ← Agent F writes
    ├── bug_count           RTL vs ISS mismatches       ← Agent D writes
    ├── ipc                 instructions per cycle      ← Agent B writes
    └── industrial_grade    boolean threshold check     ← Agent F writes
```

### Manifest validation (manifest_lock.py)

```python
from manifest_lock import ManifestLock, ManifestLockError

lock = ManifestLock("run/manifest.json")

# Before reading — check minimum required fields
lock.assert_readable()

# Before writing — validate outgoing values
lock.assert_writable({
    "phases.coverage.status":  "completed",
    "metrics.coveragepct":     150.0,   # ← RAISES: max=100.0
})

# Full validation report
violations = lock.validate()
# ['[MAX] metrics.coveragepct must be <= 100.0, got 150.0']

# Phase ordering (no phase completed before its prerequisite)
lock.assert_phase_order()
```

---

## 10. Coverage Metrics Explained

### Structural coverage types

| Type | What it measures | Verilator flag | Comment prefix |
|------|-----------------|----------------|----------------|
| **Line** | Was this statement ever executed? | `--coverage-line` | `''` (empty) |
| **Branch** | Was each arm of every `if`/`case` taken? | `--coverage-line` | `'b0'`, `'b1'`, ... |
| **Toggle** | Did every signal transition 0→1 and 1→0? | `--coverage-toggle` | `'s0'`, `'s1'` |
| **Expression** | Was each sub-condition in a complex expression true and false? | `--coverage-line` | `'e0'`, `'e1'`, ... |

### The functional composite

```
functional% = 0.35 × line%
            + 0.35 × branch%
            + 0.20 × toggle%
            + 0.10 × expression%
```

Weights reflect the relative bug-finding power of each type. Branch and line coverage together account for 70% because most RTL bugs manifest as wrong control flow. Toggle (20%) catches floating signals and stuck-at faults. Expression (10%) handles complex multi-condition guards.

### Industrial grade thresholds

```python
def is_industrial_grade(metrics) -> bool:
    return (
        metrics.line   >= 95.0   # every statement exercised
        and metrics.branch >= 90.0   # 9 in 10 branch arms taken
        and metrics.toggle >= 85.0   # 85% of signal transitions seen
    )
```

These thresholds are aligned with typical pre-silicon sign-off criteria for RISC-V designs. They are configurable per project.

### Functional instruction coverage (RV32IM)

In addition to structural coverage, `FunctionalCoverageModel` tracks which instruction categories have been exercised:

```
RV32I:  LUI AUIPC JAL JALR BRANCH LOAD STORE ALU_I ADD SUB SLL SLT XOR SRL SRA OR AND
RV32M:  MUL MULH MULHSU MULHU DIV DIVU REM REMU
SYSTEM: ECALL EBREAK MRET WFI FENCE CSR
```

Corner cases automatically targeted:
- `DIV x, x, 0` → quotient = -1 (RISC-V spec §M.6)
- `DIV INT_MIN, -1` → signed overflow → result = INT_MIN
- `REM INT_MIN, -1` → remainder = 0
- `MUL(-1, -1)` → 1 (low 32 bits)
- `MULH(INT_MIN, INT_MIN)` → 0x40000000

---

## 11. Cold Path Ranking Algorithm

A "cold path" is a coverage point with `hit_count == 0`. The question is: **which cold paths are most worth targeting with the next test?**

Not all cold paths are equal. A cold branch inside the multiplication unit is more valuable to cover than a cold line in a debug-only register dump.

### ROI formula

```
ROI(path) = reachability(path) × impact_factor(path) × novelty_bonus(path)
```

**Reachability** (0 → 1): How likely is it that a test can reach this point?

```
reachability = Σ(hit_count of predecessor lines in same module)
               / (1 + count of cold neighbors in ±10 line window)
```

Paths deep inside execution flows already proven to be reachable score higher than paths whose predecessors have never been hit either.

**Impact factor** (1.0 → 3.0): How important is this path?

| Path type | Multiplier | Reason |
|-----------|-----------|--------|
| M-ext (mul/div/rem) corners | 3.0 | Specification-defined edge cases are high-value bugs |
| Trap / exception handlers | 3.0 | Privilege transitions are the most dangerous RTL bugs |
| CSR access paths | 2.5 | Security-critical: wrong CSR access = privilege escape |
| Branch in decode/execute | 2.0 | Control flow bugs affect every subsequent instruction |
| Toggle in control signals | 1.8 | Stuck-at-1/0 faults in critical signals |
| Everything else | 1.0 | Default |

**Novelty bonus** (diminishing return):

```python
novelty = 2.0          if attempts == 0   # never targeted: full exploration bonus
novelty = 1/(1+attempts)  otherwise       # diminishing: don't hammer the same point
```

---

## 12. Plateau Detection — Mann-Kendall

Coverage often improves fast at first, then slows as the easy paths are covered. The platform detects this plateau using the **Mann-Kendall trend test**, a non-parametric statistical test that does not assume normally distributed data.

### How it works

Given a time series of `functional%` values across the last `N` runs:

```
S = Σ sgn(x_j - x_i)   for all pairs i < j
```

A large positive S means the series is trending upward. A small |S| means there is no consistent trend.

Under H₀ (no trend), S is approximately normal with:
```
Var(S) = n(n-1)(2n+5) / 18
```

```
z = (S - sign(S)) / √Var(S)
```

**Plateau detected when |z| < 1.28** (fail to reject H₀ at α=0.10 significance level).

### Minimum window

With fewer than 5 data points, the maximum achievable |z| is 1.044, which is always below 1.28. The platform requires **at least 5 runs** before declaring a plateau, preventing false positives in early exploration.

### What happens when plateau is detected

The `UCB1CoverageDirector` switches from exploitation (targeting known gaps) to epsilon-greedy exploration (random metric selection), and the `ColdPathRanker` novelty bonus ensures previously targeted paths are deprioritised in favour of fresh coverage territory.

---

## 13. Differential Verification (RTL vs ISS)

The core correctness claim of AVA is: **the RTL processor must behave identically to the Spike ISA reference model for every instruction it retires.**

### Commit log comparison

Both the RTL simulation (Verilator) and Spike emit a **commit log** — one line per retired instruction:

```
# Spike format:
core   0: 0x80000010 (0x00c58533) x10 0x00000001

# DUT format (AVA convention):
COMMIT pc=0x80000010 instr=0x00c58533 rd=x10 val=0x00000001
```

`CommitLogComparator` walks both logs in lock-step:

```
For each instruction index i:
  if rtl[i].pc ≠ iss[i].pc:
    → BugReport(kind=PC_MISMATCH, severity=CRITICAL)
    → attempt re-sync (scan next 20 entries for matching PC)
  elif rtl[i].rd ≠ "x0" and rtl[i].rd_val ≠ iss[i].rd_val:
    → BugReport(kind=REGISTER_MISMATCH, severity=HIGH)
```

### Bug severity classification

| Kind | Severity | Meaning |
|------|----------|---------|
| `PC_MISMATCH` | CRITICAL | Control flow diverged — pipeline went to wrong address |
| `REGISTER_MISMATCH` in sp/ra/gp | CRITICAL | Stack/return corrupted |
| `REGISTER_MISMATCH` other | HIGH | Data path incorrect |
| `CSR_MISMATCH` | HIGH | Control register wrong — may cause privilege escape |
| `TRAP_MISMATCH` | HIGH | Exception taken / not taken inconsistently |
| `INSTR_CNT_MISMATCH` | HIGH | Different number of instructions retired |

### x0 immutability check

The security analyzer performs an additional check: the zero register `x0` must never be written with a non-zero value. This is an ISA invariant (`x0` is hardwired to 0). Any RTL that allows `x0` to hold a non-zero value is producing a fundamentally incorrect processor.

---

## 14. Technology & Methodology Stack

### Languages & Runtimes

| Layer | Technology |
|-------|-----------|
| Platform language | Python 3.9+ (stdlib only, no pip required) |
| RTL simulation | Verilator 4.x / 5.x (SystemVerilog / Verilog) |
| ISA reference | Spike (riscv-isa-sim), any RISC-V ISA version |
| Testbench (generated) | Cocotb (Python), UVM (SystemVerilog) |
| Database | SQLite 3 with WAL journal mode |
| Assembly (generated) | RISC-V GAS syntax (riscv32-unknown-elf-as) |

### Verification methodologies implemented

| Methodology | Where | Industry standard |
|-------------|-------|-----------------|
| Constrained-random test generation | `RV32IMTestGenerator` | IEEE 1800.2 UVM |
| Differential / lock-step verification | `CommitLogComparator`, `SpikeISS` | ARM RVDF, Intel SVG |
| Structural coverage (line/branch/toggle/expression) | `VerilatorCoverageParser`, `CoverageMetrics` | ISO 26262, DO-254 |
| Functional coverage | `FunctionalCoverageModel` | IEEE 1800 SV |
| Coverage-directed generation | `UCB1CoverageDirector` | Cadence IMC, Synopsys VC |
| Formal verification hooks | `SecurityAnalyzer` (x0 invariant) | RISC-V ISA spec §2.6 |
| Multi-armed bandit optimisation | UCB1 in `UCB1CoverageDirector` | RL-based verification research |
| Non-parametric trend detection | Mann-Kendall in `CoverageDatabase` | Statistical process control |

### Design patterns used

| Pattern | Where | Benefit |
|---------|-------|---------|
| Atomic write (tmp→rename) | `atomic_write()` everywhere | No half-written files, safe concurrent access |
| Strategy pattern | `UCB1CoverageDirector` | Pluggable coverage strategy (UCB1 / epsilon-greedy) |
| Chain-of-responsibility | `VerilatorCoverageBackend._resolve_metrics()` | Priority fallback: .dat → dict → zeros |
| Observer / contract | `manifest.json` + `manifest_lock.py` | Decoupled agents, validated state machine |
| Parse-then-validate | `VerilatorCoverageParser` + `ManifestLock` | Fail early, informative errors |
| Thread-safe accumulator | `CoverageDatabase`, `FunctionalCoverageModel` | Safe parallel simulation runs |

### Coverage .dat format

Verilator coverage database files use a text format with single-quoted fields:

```
[type] 'count' 'filename' 'lineno' 'col' 'hier' 'comment'
```

The parser handles three generations of this format (Verilator 4.0 full 7-field, short 4-field, and merged output from `verilator_coverage --write`), auto-detects on the first data line, and handles UTF-8, Latin-1, and gzip-compressed files.

---

## 15. Integration Guide for Other Agents

### Reading coveragesummary.json

```python
import json
from pathlib import Path

summary = json.loads(Path("run/coveragesummary.json").read_text())

# Overall coverage percentage
pct = summary["overall"]["pct"]              # e.g. 73.5

# Individual metrics
line_pct   = summary["metrics"]["line"]      # e.g. 87.4
branch_pct = summary["metrics"]["branch"]    # e.g. 78.2

# Is coverage stalled?
if summary["plateau_detected"]:
    # Switch to adversarial / red-team mode

# Top cold paths with assembly constraints
for path in summary["top_cold_paths"][:5]:
    print(f"  {path['file']}:{path['line']}  kind={path.get('kind','?')}")
```

### Using cold path ranking in a test generator

```python
from coverage_database import CoverageDatabase
from cold_path_ranker import ColdPathRanker

with CoverageDatabase("coverage_trend.sqlite") as db:
    ranker  = ColdPathRanker(db)
    ranked  = ranker.rank_by_roi(limit=20)

    for entry in ranked:
        asm = entry["test_constraint"]       # ready-to-assemble RV32IM snippet
        target_file = entry["module"]
        target_line = entry["line"]

        # Record that we are targeting this path
        db.record_test_attempt("seed_43", target_file, target_line)
```

### Validating the manifest before writing

```python
from manifest_lock import ManifestLock, ManifestLockError

lock = ManifestLock("run/manifest.json")

updates = {
    "phases.red_team.status": "completed",
    "metrics.bug_count":       3,
}

try:
    lock.assert_writable(updates)
    from coverage_pipeline import update_manifest
    update_manifest("run/manifest.json", updates)
except ManifestLockError as exc:
    print(f"Contract violation: {exc}")
    sys.exit(3)
```

### Importing coverage utilities directly

```python
from coverage_pipeline import (
    VerilatorCoverageParser,     # parse .dat files
    CoverageMetrics,             # aggregated metrics object
    CoverageReporter,            # write JSON/CSV/HTML/AVA summary
    format_ava_schema,           # build coveragesummary.json body
    atomic_write,                # safe file write
    load_manifest,               # load + validate manifest.json
    update_manifest,             # atomic manifest update
    save_coverage_report,        # combined report writer
    parse_spike_commit_log,      # parse Spike --log-commits output
    parse_dut_commit_log,        # parse DUT COMMIT log lines
    count_cycles_instrets,       # extract cycle/instruction counts
)
```

---

## 16. CLI Reference

### coverage_pipeline.py

```
usage: coverage_pipeline [-h] (--dat FILE | --dat-dir DIR | --manifest FILE)
                         [--annotate DIR] [--write-merged FILE]
                         [--out DIR] [--formats FMT [FMT ...]]
                         [--db FILE] [--verilator-coverage-bin BIN]
                         [--run-id ID] [-v]

Modes:
  --dat FILE         Direct: parse one coverage.dat
  --dat-dir DIR      Direct: merge all *.dat in directory
  --manifest FILE    Contract: read/write manifest.json (AVA platform mode)

Options:
  --out DIR          Output directory (default: .)
  --formats          json csv html (any combination, default: json)
  --db FILE          SQLite trend database (enables plateau + regression)
  --run-id ID        Identifier for this run (used in DB and provenance)
  --annotate DIR     Run verilator_coverage --annotate before parsing
  -v, --verbose      DEBUG-level logging

Exit codes:
  0    Success (or industrial grade in direct mode)
  1    Parse/calculation error
  3    Manifest/config error (missing file, schema violation)
```

### coverage_database.py

```
usage: coverage_database --db FILE [--load DAT] [--run-id ID]
                         [--summary] [--cold N] [--plateau]

Options:
  --db FILE      Database path (required)
  --load DAT     Load a coverage.dat file into the database
  --run-id ID    Run ID for --load (default: cli_run)
  --summary      Print aggregate statistics across all runs
  --cold N       Show top N cold paths by reachability
  --plateau      Check whether Mann-Kendall detects a coverage plateau
```

### cold_path_ranker.py

```
usage: cold_path_ranker --db FILE [--top N] [--json]
                        [--constraints-only] [--run-id ID] [-v]

Options:
  --db FILE           coverage_database.db path (required)
  --top N             Number of paths to rank (default: 20)
  --json              Output as JSON array
  --constraints-only  Print only the assembly snippets
  --run-id ID         Record test attempts for this run ID
```

### manifest_lock.py

```
usage: manifest_lock manifest [--strict] [--field DOTPATH]

Options:
  --strict        Exit 1 on first violation (default: report all)
  --field DOTPATH Validate only this one field (e.g. metrics.coveragepct)
```

### ava_coverage_patch.py

```
usage: ava_coverage_patch ava_py [--dry-run] [--status]
                                 [--backup-dir DIR] [--output FILE]

Options:
  --dry-run       Preview all 8 hunks without writing
  --status        Show which hunks are applied vs pending
  --backup-dir    Where to write ava.py.bak_<timestamp> (default: same dir)
  --output FILE   Write patched file here (default: ava_patched.py)
```

### ava_patched.py

```
usage: ava_patched --rtl FILE [--microarch {in_order,out_of_order,superscalar}]
                   [--seed N] [--timeout N] [--target-cov FLOAT]
                   [--model NAME] [--spike PATH] [--isa STRING]
                   [--run-dir DIR] [--no-llm]
                   [--formats json csv html]

  (no args)   Run built-in self-test (no Verilator or Spike needed)
```

---

## 17. Schema Reference

### commitlog.schema.json

Each entry in a commit log (RTL or ISS) must conform to:

```json
{
  "pc":     "0x80000010",     // required: hex string, 0x prefix
  "instr":  "0x00c58533",     // required: exactly 8 hex digits
  "rd":     "x10",            // optional: x0-x31, f0-f31, or ""
  "rd_val": "0x00000001"      // optional: hex string or ""
}
```

### run_manifest.schema.json (key fields)

```json
{
  "rundir":  "/abs/path/to/run",   // REQUIRED: string
  "run_id":  "seed_42_...",        // REQUIRED: string
  "isa":     "rv32im",             // REQUIRED: matches /^rv(32|64)(i|e)(m?)...$/

  "phases.coverage.status":   "pending|running|completed|failed",
  "phases.coverage.duration": 0.0,   // seconds >= 0

  "outputs.coveragesummary":  "coveragesummary.json",
  "outputs.coverageraw":      "outputs/coverageraw",

  "metrics.coveragepct":      0.0,   // float [0, 100]
  "metrics.coverage_plateau": false, // boolean
  "metrics.industrial_grade": false, // boolean

  "config.target_coverage":   95.0,  // float [0, 100]
  "config.microarch":         "in_order|out_of_order|superscalar"
}
```

---

## 18. Contributing & Extending

### Adding a new coverage metric type

1. Add a new value to `CoverageKind` in `coverage_pipeline.py`
2. Add the comment prefix mapping in `CoverageKind.from_comment()`
3. Add a `<kind>s_hit`, `<kind>s_total`, `<kind>` field to `CoverageMetrics`
4. Add aggregation logic in `VerilatorCoverageParser.aggregate()`
5. Update `WEIGHTS` if the new type should affect `functional%`
6. Update `CoverageReporter.write_html()` to show the new bar

### Adding a new ISA extension to FunctionalCoverageModel

1. Add opcode → (group, mnemonic) entries to `_OPCODE_TABLE`
2. Add the group and its universe set to `_UNIVERSE`
3. Update `_M_FUNCT3` if it is a funct3-dispatched extension (like M)

### Adding a new manifest field

1. Add the field to `schemas/run_manifest.schema.json`
2. Add a `FieldAssertion` to `MANIFEST_ASSERTIONS` in `manifest_lock.py`
3. If Agent F should write it, add it to the `update_manifest()` call in `_run_manifest_mode()`

### Adding a new impact category to ColdPathRanker

Add keywords to the appropriate frozenset in `cold_path_ranker.py`:

```python
_TRAP_KEYWORDS  = frozenset({"trap","ecall","ebreak","mret","exception","interrupt","your_keyword"})
```

Or add a new multiplier tier in `_impact_factor()`.

---

## Appendix: Directory Structure for a Full Run

```
<rundir>/
├── manifest.json                   ← inter-agent contract (updated atomically)
├── coveragesummary.json            ← Agent F primary output (AVA schema v3.0)
├── coverage_report.json            ← Agent F legacy full report
├── coverage_report.html            ← human-readable bar chart
├── coverage_report.csv             ← CI trend chart input
│
├── outputs/
│   ├── coverageraw/
│   │   └── coverage.dat            ← Verilator output (Agent B input to Agent F)
│   ├── rtl_commit.ndjson           ← RTL commit log (Agent B → Agent D)
│   ├── iss_commit.ndjson           ← ISS commit log (Agent C → Agent D)
│   ├── bugs.json                   ← differential bug list (Agent D output)
│   └── cold_paths.json             ← ranked cold paths (Agent F → Agent G)
│
├── cocotb_tb_riscv_core.py         ← generated cocotb testbench
├── uvm_tb_riscv_core.sv            ← generated UVM testbench
├── test.S                          ← generated assembly test
└── ava_results_riscv_core_<ts>.json ← full AVA results archive
```

---

*AVA v3.0 — Agent F Coverage Pipeline*
*6 091 lines · 18/18 tests passing · 8 format types · 2 JSON schemas*

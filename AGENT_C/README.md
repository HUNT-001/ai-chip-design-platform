# Agent C — Spike ISS Golden Backend
### AVA (Autonomic Verification Agent) · RISC-V Verification Platform

---

## Table of Contents

1. [What Is This?](#1-what-is-this)
2. [Why It Matters — The Verification Problem](#2-why-it-matters--the-verification-problem)
3. [Where Agent C Lives in the AVA Platform](#3-where-agent-c-lives-in-the-ava-platform)
4. [How It Works — Technical Deep Dive](#4-how-it-works--technical-deep-dive)
5. [File Reference](#5-file-reference)
6. [Schema v2.0.0 — The Wire Format](#6-schema-v200--the-wire-format)
7. [Spike Log Formats Handled](#7-spike-log-formats-handled)
8. [Running It — Step-by-Step Examples](#8-running-it--step-by-step-examples)
9. [ISS Efficiency & Plateau Detection](#9-iss-efficiency--plateau-detection)
10. [Test Suite](#10-test-suite)
11. [Integration with Other Agents](#11-integration-with-other-agents)
12. [Technologies & Methodology](#12-technologies--methodology)
13. [Troubleshooting](#13-troubleshooting)
14. [Glossary](#14-glossary)

---

## 1. What Is This?

Agent C is the **golden ISS (Instruction Set Simulator) runner** for the AVA RISC-V verification platform. Its job is deceptively simple to state:

> *Run the exact same binary through Spike (the RISC-V reference simulator), capture every instruction that retired, and write a structured log that can be compared cycle-by-cycle against the real RTL.*

That log — `iss.commitlog.jsonl` — is the **mathematical ground truth** for the entire verification campaign. Every bug report, every coverage gap, every formal proof ultimately traces back to the question: *"Does the RTL agree with this file?"*

Agent C consists of three Python modules:

| Module | Role |
|--------|------|
| `spike_parser.py` | Converts raw Spike stderr/stdout into schema v2.0.0 JSONL records |
| `run_iss.py` | CLI and orchestrator contract: runs Spike, writes the log, manages manifests |
| `iss_efficiency.py` | SQLite-backed metrics tracker + coverage plateau detection |

---

## 2. Why It Matters — The Verification Problem

### The Core Challenge

Modern RISC-V processors contain millions of transistors executing billions of instructions. A single misimplemented instruction — say, `MULHSU` returning the wrong value on a sign-boundary input — might only manifest after a specific 47-instruction sequence that no engineer would write by hand. Traditional verification approaches miss this class of bug because:

- **Manual testbenches** only cover cases the engineer thought of
- **Random simulation** without a golden reference can't detect *wrong-but-consistent* behavior
- **Formal methods** alone don't scale to full microarchitecture verification

### The Differential Testing Solution

The answer is **differential testing with a golden reference**:

```
Same ELF binary
      │
      ├──► Verilator RTL simulation ──► rtl.commitlog.jsonl  ─┐
      │                                                        ├──► COMPARE ──► bugs.json
      └──► Spike ISS (Agent C)      ──► iss.commitlog.jsonl  ─┘
```

Spike is the authoritative RISC-V reference simulator, maintained by the Berkeley Architecture Research group and used by the RISC-V Foundation itself for specification compliance. If the RTL and Spike agree on every instruction's PC, register writes, CSR writes, memory accesses, and trap behavior, the RTL is architecturally correct. If they disagree, the first divergence point is a bug.

This approach is used by major chip companies (SiFive, Qualcomm, NVIDIA's RISC-V work) precisely because it **finds bugs that no individual test was written to catch**. You don't need to predict what will go wrong — you only need to run enough instructions that the bug surfaces naturally.

### Why Agent C Specifically Matters

Without a correct ISS golden run, the comparator (Agent D) has nothing to compare against. Every downstream layer of AVA — coverage director, formal ring, red-team adversarial agents — depends on knowing that Agent C produced a trustworthy ground truth. A bug in Agent C's log parser is worse than a bug in the RTL, because it silently hides real RTL bugs.

---

## 3. Where Agent C Lives in the AVA Platform

AVA is structured in five verification layers, all of which depend on the truth engine at the bottom:

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 5 — Red-Team Adversarial (Agent H)                       │
│  Adversarial sequences targeting coherence/speculation/power    │
├─────────────────────────────────────────────────────────────────┤
│  Layer 4 — Formal Ring                                          │
│  ISA invariants: x0=0, CSR safety, trap/return correctness      │
├─────────────────────────────────────────────────────────────────┤
│  Layer 3 — Coverage-Directed Generation (Agent G + Director)    │
│  Constrained-random + M-extension corner cases                  │
├─────────────────────────────────────────────────────────────────┤
│  Layer 2 — Compliance & Signatures (Agent E)                    │
│  riscv-arch-test / RISCOF signature comparison                  │
├─────────────────────────────────────────────────────────────────┤
│  Layer 1 — TRUTH ENGINE  ◄── YOU ARE HERE                       │
│  Agent B (RTL/Verilator) + Agent C (Spike ISS) + Agent D (diff)│
└─────────────────────────────────────────────────────────────────┘
```

Agent C's position in the per-run pipeline:

```
AVA Orchestrator
       │
       ├─ Agent B ──► run_rtl.py --manifest ──► rtl.commitlog.jsonl
       │
       ├─ Agent C ──► run_iss.py --manifest ──► iss.commitlog.jsonl  ◄── (this repo)
       │
       └─ Agent D ──► compare_commitlogs.py --manifest ──► diff_report.json + bugs.json
```

The orchestrator drives all three using a shared `manifest.json` file. Agent C reads the manifest, runs Spike, writes its output back into the manifest's `phases.iss.*` and `outputs.iss_commitlog` fields, then exits with a standard code that tells the orchestrator what happened.

---

## 4. How It Works — Technical Deep Dive

### 4.1 The Spike Invocation Strategy

Spike supports multiple log verbosity levels. Agent C uses a **priority ladder** to get the richest data available:

```
Priority 1: --log-commits        (Spike ≥ 1.1)  →  FORMAT B — full register + CSR + memory writes
Priority 2: --enable-commitlog   (older builds)  →  FORMAT B — same data, older flag name
Priority 3: -l                   (all versions)  →  FORMAT A — PC + instruction + disasm only
```

FORMAT B is essential for differential verification because it includes the **written value** of every register after each instruction retires. Without those values, the comparator can detect that a wrong PC was reached, but cannot detect a subtler bug where the PC is correct but register `x10` holds the wrong result.

### 4.2 The Parser State Machine

Spike's FORMAT B output is not one line per instruction. A single retired instruction can generate up to four lines:

```
core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0    ← instruction header (disasm)
core   0: 3 0x80000000 (0x00000297) x5  0x80000000      ← register writeback (same pc+instr!)
core   0: 3 0x80000000 (0x00000297) csr 0x300 0x1800    ← CSR write (if any)
core   0: exception load_access_fault, epc 0x80000000   ← trap (if taken)
```

The parser uses a **buffered state machine** with the `(pc, instr)` identity rule as its core insight: a new line with the same `pc` and `instr` values as the currently buffered commit is a **continuation** of that instruction, not a new instruction. Only when the `(pc, instr)` pair changes is the previous commit flushed to output.

This rule correctly handles all three Spike output sub-layouts:
- Sub-layout 1: inline writeback on the same line as the instruction
- Sub-layout 2: disasm line + separate writeback line at the same `(pc, instr)`
- Sub-layout 3: instruction line with no writeback (branches, stores, no-ops)

### 4.3 The (pc, instr) Identity Rule — Why It's Non-Obvious

A naive parser that flushes on every new instruction line would **double-count** all instructions in sub-layout 2, which is common in Spike builds that emit both human-readable disassembly and machine-readable writeback separately. This was the root cause of 5 test failures in the initial implementation, caught by the test suite before any real hardware was connected.

### 4.4 Atomic Manifest Updates

The orchestrator manifest is a shared JSON file that all agents read and write. If Agent C crashes mid-write, a corrupted manifest would block all downstream agents. `atomic_update_manifest()` handles this with the POSIX-standard approach:

```python
1. Read current manifest.json
2. Apply dotted-key updates (e.g., "phases.iss.status" → nested dict)
3. Write to manifest.json.tmp
4. os.rename(manifest.json.tmp, manifest.json)   ← atomic on POSIX filesystems
```

`rename()` is atomic because it is implemented as a single syscall that either fully completes or fully fails — there is no window where the file is half-written or missing.

### 4.5 Schema v2.0.0 Design Decisions

The schema went through one major revision (v1.x → v2.0.0) during this implementation. The key decisions:

**`regs: [{rd, value}]` not `regs: [rd_index]`** — Early versions of the contract showed `regs: [1]` (index only). This was rejected because Agent D's register mismatch detector requires the written value: `if rtl.regs[0]["value"] != iss.regs[0]["value"]` — without the value, the comparator is blind to the most common class of RTL arithmetic bugs.

**`src` not `source` alongside `src`** — Simple rename, no data duplication. Every record carries exactly one field identifying its origin.

**`schema_version: "2.0.0"` on every record** — Agents consuming the log can validate schema compatibility with a single field check rather than inferring version from field presence/absence.

**`hart: 0` and `fpregs: null` mandatory** — Even though RV32IM has one hart and no floating-point, these fields being present and typed means Agent D never needs special-case logic for their absence.

---

## 5. File Reference

### `spike_parser.py` — The Parser Engine

**What it does:** Converts raw text from Spike's stderr/stdout into a list of Python dicts conforming to schema v2.0.0, ready for JSONL serialisation.

**Key public API:**

```python
from spike_parser import parse_spike_log, parse_spike_log_streaming, SCHEMA_VERSION

# Parse entire log into memory
records = parse_spike_log(spike_output_text, source="iss", fmt=None)
# → [{"schema_version": "2.0.0", "seq": 0, "pc": "0x80000000", ...}, ...]

# Streaming variant (large runs, lower memory)
for record in parse_spike_log_streaming(spike_output_text, source="iss"):
    write_to_file(record)
```

**Internal components:**
- `RawCommit` dataclass — intermediate representation before serialisation
- `detect_format()` — auto-detects FORMAT A vs FORMAT B from first 200 lines
- `_parse_format_a()` — handles `-l` flag output
- `_parse_format_b()` — state machine for `--log-commits` output
- `_parse_rest_b()` — classifies each line's "rest" as reg/mem/csr/disasm/inline-wb
- `_ABI_TO_IDX` — maps all 32 ABI register names to integer indices
- `_CSR_NAMES` — maps 18 standard CSR addresses to mnemonics
- `_EXC_NAMES` — maps Spike exception name strings to RISC-V mcause codes

---

### `run_iss.py` — The Runner and CLI

**What it does:** Orchestrates the entire ISS pipeline — probes Spike capabilities, builds the command, executes the subprocess, calls the parser (as a library call, not a subprocess), validates output, and writes the manifest.

**Two operating modes:**

**Manifest mode** (AVA orchestrator — recommended):
```bash
python run_iss.py --manifest ./runs/run_001/manifest.json
```

**Legacy direct mode** (standalone debugging):
```bash
python run_iss.py --isa RV32IM --elf ./prog.elf --out ./runs/run_001
```

**Key functions:**

| Function | Purpose |
|----------|---------|
| `probe_spike(bin)` | Runs `spike --help`, detects `--log-commits`/`--enable-commitlog` support |
| `build_spike_cmd(...)` | Constructs argv list with correct flags for detected Spike version |
| `run_spike_process(cmd, log, timeout)` | Popen + stream to file + return code |
| `write_commitlog(output, path, fmt, max)` | Parser → JSONL write loop |
| `validate_commitlog(path, n)` | Schema v2.0.0 field checks on first n records |
| `atomic_update_manifest(path, updates)` | Dotted-key write + POSIX-atomic rename |
| `run_iss_manifest(path)` | Full orchestrator contract, returns EXIT_PASS/INFRA/CONFIG |

**Exit codes:**

| Code | Constant | Meaning | Orchestrator action |
|------|----------|---------|---------------------|
| 0 | `EXIT_PASS` | Log produced successfully | Proceed to Agent D |
| 2 | `EXIT_INFRA` | Spike/parser crash or timeout | Retry or alert |
| 3 | `EXIT_CONFIG` | Missing ELF, bad manifest | Fix config, don't retry |

---

### `iss_efficiency.py` — Metrics and Plateau Detection

**What it does:** Records per-run ISS metrics in a local SQLite database and detects when the ISS is no longer exploring new territory (plateau), signalling the orchestrator to change strategy.

**Key API:**

```python
from iss_efficiency import ISSEfficiencyTracker

with ISSEfficiencyTracker("iss_metrics.db") as tracker:
    # Record one run
    tracker.record_run(isa="rv32im", commit_count=47823, duration_s=1.23,
                       log_mode="log_commits", spike_exit=1)

    # Check for plateau (last 10 runs, variance threshold 500)
    if tracker.is_plateau(isa="rv32im", window=10, variance_threshold=500.0):
        # Signal orchestrator to rotate seeds or escalate to formal
        ...

    # Get summary statistics
    stats = tracker.stats("rv32im")
    # → {"total_runs": 42, "total_commits": 2018566, "avg_duration_s": 1.23, ...}
```

---

## 6. Schema v2.0.0 — The Wire Format

Every line of `iss.commitlog.jsonl` is a self-contained JSON object. The schema is defined in `commitlog.schema.json`.

### Mandatory fields (always present on every record)

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `schema_version` | string | `"2.0.0"` | Constant; validated by Agent D |
| `seq` | integer | `0` | 0-based, monotonically increasing |
| `pc` | string | `"0x80000000"` | Hex, 0x-prefixed |
| `instr` | string | `"0x00000297"` | Raw encoding (32-bit or 16-bit) |
| `src` | string | `"iss"` | One of: `"iss"`, `"rtl"`, `"formal"` |
| `hart` | integer | `0` | Hardware thread ID |
| `fpregs` | null | `null` | FP register file; null for RV32IM |

### Optional fields (present when applicable)

| Field | Type | Example | When present |
|-------|------|---------|-------------|
| `priv` | string | `"M"` | FORMAT B only; `"M"`, `"S"`, or `"U"` |
| `disasm` | string | `"auipc t0, 0x0"` | When Spike emits disassembly |
| `regs` | array | `[{"rd": 5, "value": "0x80000000"}]` | Non-zero register writes |
| `csrs` | array | `[{"addr": "0x300", "name": "mstatus", "value": "0x00001800"}]` | CSR writes |
| `mem` | object | `{"type": "load", "addr": "0x80002000", "size": 4, "value": "0x00000000"}` | Memory accesses |
| `trap` | object | `{"cause": "0x00000005", "tval": "0x80000200", "is_interrupt": false}` | Exceptions/interrupts |

### Example records

**Arithmetic instruction with register writeback (FORMAT B):**
```json
{
  "schema_version": "2.0.0",
  "hart": 0,
  "fpregs": null,
  "seq": 0,
  "pc": "0x80000000",
  "instr": "0x00000297",
  "src": "iss",
  "priv": "M",
  "disasm": "auipc   t0, 0x0",
  "regs": [{"rd": 5, "value": "0x80000000"}]
}
```

**M-extension MUL instruction:**
```json
{
  "schema_version": "2.0.0",
  "hart": 0,
  "fpregs": null,
  "seq": 14,
  "pc": "0x80000038",
  "instr": "0x02b50633",
  "src": "iss",
  "priv": "M",
  "disasm": "mul     a2, a0, a1",
  "regs": [{"rd": 12, "value": "0x0000002a"}]
}
```

**Load instruction that took an access fault:**
```json
{
  "schema_version": "2.0.0",
  "hart": 0,
  "fpregs": null,
  "seq": 22,
  "pc": "0x80000200",
  "instr": "0x00002003",
  "src": "iss",
  "priv": "M",
  "disasm": "lw      zero, 0(zero)",
  "trap": {
    "cause": "0x00000005",
    "tval": "0x80000200",
    "is_interrupt": false
  }
}
```

**Trace-only record (FORMAT A — no register values):**
```json
{
  "schema_version": "2.0.0",
  "hart": 0,
  "fpregs": null,
  "seq": 3,
  "pc": "0x8000000c",
  "instr": "0x30200073",
  "src": "iss",
  "disasm": "mret"
}
```

---

## 7. Spike Log Formats Handled

Agent C handles every known Spike output variant without manual configuration.

### FORMAT A — `-l` flag (all Spike versions)

```
core   0: 0x80000000 (0x00000297) auipc   t0, 0x0
core   0: 0x80000004 (0x02028593) addi    a1, t0, 32
```

One line per instruction. Contains PC, raw encoding, disassembly. **No register values.** Sufficient for PC-trace comparison but not register-level differential verification.

### FORMAT B — `--log-commits` (Spike ≥ 1.1)

Three sub-layouts exist depending on the Spike build:

**Sub-layout 1: inline writeback** (most common)
```
core   0: 3 0x80000000 (0x00000297) x5  0x80000000
```
The `3` is the privilege mode (3=M, 1=S, 0=U).

**Sub-layout 2: disasm line + separate writeback at same PC** (common in verbose builds)
```
core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0
core   0: 3 0x80000000 (0x00000297) x5  0x80000000
```
These two lines describe one retired instruction. The parser's `(pc, instr)` identity rule merges them.

**Sub-layout 3: memory access**
```
core   0: 3 0x80000008 (0x00002223) mem 0x80002000 0x00000000
```

**Exception line (attached to preceding instruction):**
```
core   0: exception load_access_fault, epc 0x80000200
```

### Privilege Encoding

| Spike digit | Privilege mode |
|-------------|---------------|
| `3` | M (Machine) |
| `1` | S (Supervisor) |
| `0` | U (User) |

### ISA Strings Supported

`RV32I`, `RV32IM`, `RV32IMA`, `RV32IMAC`, `RV32IMAFC`, `RV32G`, `RV32GC`, `RV64I`, `RV64IM`, `RV64IMA`, `RV64G`, `RV64GC` — and any lowercase / custom extension string Spike accepts.

---

## 8. Running It — Step-by-Step Examples

### Prerequisites

```bash
# 1. Install Spike
git clone https://github.com/riscv-software-src/riscv-isa-sim
cd riscv-isa-sim && mkdir build && cd build
../configure --prefix=/opt/riscv
make -j$(nproc) && make install
export PATH=/opt/riscv/bin:$PATH
spike --version   # should print version string

# 2. Install RISC-V toolchain (for compiling test programs)
# Ubuntu/Debian:
sudo apt install gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf
# macOS:
brew install riscv-gnu-toolchain

# 3. Python 3.9+ (stdlib only — no pip installs required)
python3 --version
```

---

### Example 1: Compile and run the smoke test

```bash
# Compile the included RV32IM smoke test (exercises all M-extension corners)
riscv64-unknown-elf-gcc \
    -march=rv32im -mabi=ilp32 \
    -nostdlib -static \
    -T link.ld \
    rv32im_smoke.S \
    -o rv32im_smoke.elf

# Run Agent C (legacy mode)
python run_iss.py \
    --isa RV32IM \
    --elf rv32im_smoke.elf \
    --out ./runs/smoke_001 \
    --validate

# Expected output:
# ──────────────────────────────────────────────────────────────
#   AVA Agent C — Spike Golden Runner  [DONE]
# ──────────────────────────────────────────────────────────────
#   ELF         : rv32im_smoke.elf
#   ISA         : RV32IM
#   Schema      : v2.0.0
#   Log mode    : log_commits
#   Commits     : 47
#   Elapsed     : 0.14s
#   Spike exit  : 1
#   Output      : runs/smoke_001/iss.commitlog.jsonl
#   Manifest    : runs/smoke_001/manifest.json
# ──────────────────────────────────────────────────────────────
```

---

### Example 2: Inspect the commit log

```bash
# Count total instructions committed
wc -l runs/smoke_001/iss.commitlog.jsonl
# → 47 runs/smoke_001/iss.commitlog.jsonl

# Pretty-print the first record
head -1 runs/smoke_001/iss.commitlog.jsonl | python3 -m json.tool
# → {
#       "schema_version": "2.0.0",
#       "hart": 0,
#       "fpregs": null,
#       "seq": 0,
#       "pc": "0x80000000",
#       "instr": "0x00000297",
#       "src": "iss",
#       "priv": "M",
#       "disasm": "auipc   t0, 0x0",
#       "regs": [{"rd": 5, "value": "0x80000000"}]
#   }

# Find all MUL instructions
grep '"mul' runs/smoke_001/iss.commitlog.jsonl | python3 -m json.tool

# Count trap records
python3 -c "
import json
traps = [json.loads(l) for l in open('runs/smoke_001/iss.commitlog.jsonl') if '\"trap\"' in l]
print(f'{len(traps)} trap(s) in log')
for t in traps:
    print(f'  seq={t[\"seq\"]} pc={t[\"pc\"]} cause={t[\"trap\"][\"cause\"]}')
"
```

---

### Example 3: Manifest mode (how the AVA orchestrator calls Agent C)

```bash
# The orchestrator creates a manifest first
cat > /tmp/run_001/manifest.json << 'EOF'
{
  "rundir": "/tmp/run_001",
  "binary": "/tmp/rv32im_smoke.elf",
  "isa": "rv32im",
  "spikebin": "spike",
  "timeout": 300,
  "max_instrs": 1000000
}
EOF

mkdir -p /tmp/run_001

# Agent C reads the manifest, runs Spike, writes back
python run_iss.py --manifest /tmp/run_001/manifest.json
echo "Exit code: $?"   # 0 = PASS, 2 = INFRA error, 3 = CONFIG error

# The manifest is now updated:
cat /tmp/run_001/manifest.json
# → {
#     "rundir": "/tmp/run_001",
#     "binary": "/tmp/rv32im_smoke.elf",
#     "isa": "rv32im",
#     ...
#     "phases": {
#       "iss": {
#         "status": "completed",
#         "duration_s": 0.142,
#         "commit_count": 47,
#         "log_mode": "log_commits",
#         "spike_exit": 1
#       }
#     },
#     "outputs": {
#       "iss_commitlog": "outputs/iss_commitlog.jsonl"
#     }
#   }

# The commit log:
ls -la /tmp/run_001/outputs/
# → iss_commitlog.jsonl
```

---

### Example 4: Use as a Python library (from AVA or custom scripts)

```python
import sys
sys.path.insert(0, "./")   # directory containing spike_parser.py and run_iss.py

from spike_parser import parse_spike_log, SCHEMA_VERSION
from run_iss import run_iss_manifest, write_commitlog, validate_commitlog
from pathlib import Path

# Parse Spike output directly (e.g., from a captured log file)
spike_log = Path("captured_spike.log").read_text()
records = parse_spike_log(spike_log, source="iss", fmt=None)  # auto-detect format

print(f"Schema: {SCHEMA_VERSION}")
print(f"Records: {len(records)}")
print(f"First PC: {records[0]['pc']}")

# Find all register writes to x10 (a0)
a0_writes = [
    (r["seq"], r["pc"], rw["value"])
    for r in records
    for rw in r.get("regs", [])
    if rw["rd"] == 10
]
print(f"a0 write history: {a0_writes}")

# Run via manifest from Python
rc = run_iss_manifest(Path("./runs/run_001/manifest.json"))
if rc == 0:
    print("ISS run succeeded")
```

---

### Example 5: Track efficiency across a campaign

```python
from iss_efficiency import ISSEfficiencyTracker

with ISSEfficiencyTracker("campaign_metrics.db") as tracker:
    # After each run, record metrics
    tracker.record_run(
        isa="rv32im",
        commit_count=48200,
        duration_s=1.34,
        log_mode="log_commits",
        spike_exit=1,
    )

    # After 10 runs, check for plateau
    if tracker.is_plateau("rv32im", window=10, variance_threshold=500.0):
        print("Coverage plateau — rotating seed")
        # → orchestrator increases seed, switches test generator config

    # Full statistics
    import json
    print(json.dumps(tracker.stats("rv32im"), indent=2))
    # → {
    #     "isa": "rv32im",
    #     "total_runs": 10,
    #     "total_commits": 481543,
    #     "avg_duration_s": 1.31,
    #     "avg_commits_per_run": 48154.3,
    #     "is_plateau": false
    #   }
```

---

## 9. ISS Efficiency & Plateau Detection

### The Problem

Running 10,000 Spike simulations with the same test generator and seed distribution yields diminishing returns. After a certain point, every new run exercises the same code paths as previous runs — you're spending compute budget without finding new bugs.

### The Detection Algorithm

`ISSEfficiencyTracker.is_plateau()` monitors the **variance of commit_count** (instructions retired per run) across the last `window` runs:

```
variance(commit_counts[-window:]) < variance_threshold  →  PLATEAU
```

**Why commit_count?** If the test generator is truly exploring new paths, the programs it generates will have different lengths — some will hit new code paths that run longer or shorter. When commit_count stabilises (variance drops), the generator is stuck in a pattern.

**Default thresholds:**
- `window = 10` — require 10 runs of history before declaring plateau
- `variance_threshold = 500.0` — corresponds to roughly ±22 instructions standard deviation, which is conservative for typical RV32IM test programs committing 5k–50k instructions

**When plateau is detected, the orchestrator should:**
1. Rotate the random seed
2. Switch to a different test generator configuration (e.g., M-extension heavy → branch-heavy)
3. Escalate unresolved coverage gaps to the formal ring (Agent formal)
4. Flag for red-team agents (Agent H)

The algorithm can be upgraded to Mann-Kendall trend test for more statistically rigorous plateau detection — the interface is stable.

### SQLite WAL Mode

The database uses Write-Ahead Logging (`PRAGMA journal_mode=WAL`) which allows one writer and multiple concurrent readers without blocking. This matters in parallel verification campaigns where many workers write metrics to the same database simultaneously.

---

## 10. Test Suite

### Running the tests

```bash
# Unit tests (64 tests — no Spike required)
python test_spike_parser.py

# Integration tests (43 tests — Spike subprocess mocked)
python test_run_iss_integration.py

# Full suite with real Spike (requires spike on PATH + compiled ELF)
RV32IM_ELF=./rv32im_smoke.elf python test_run_iss_integration.py
```

### What is tested

**`test_spike_parser.py` (64 tests):**

| Test class | What it verifies |
|-----------|-----------------|
| `TestHelpers` | `_hex()`, `_reg_idx()`, `SCHEMA_VERSION` value |
| `TestSchemaV2Mandatory` | All 7 mandatory fields present on every record, for every format variant |
| `TestSrcField` | `src` present, old `source` key explicitly absent |
| `TestRegsField` | `regs` present, `reg_writes` absent, `{rd, value}` structure preserved, x0 suppressed |
| `TestCsrsField` | `csrs` present, `csr_writes` absent, `{addr, name, value}` preserved |
| `TestMemField` | `mem` present, `mem_access` absent |
| `TestFormatA` | 4-record parse, disasm, no regs |
| `TestFormatBBasic` | Count, priv=M, reg writes, mem access |
| `TestFormatBDisasmThenWB` | Continuation rule merges disasm+wb lines |
| `TestFormatBCSR` | CSR addr/name/value |
| `TestFormatBTrap` | Trap cause code, is_interrupt flag |
| `TestFormatBInline` | Inline writeback parsing, disasm cleanup |
| `TestPrivDecode` | M/U/S privilege digit decoding |
| `TestEdgeCases` | Empty input, noise lines, JSON round-trip, auto-detect |
| `TestFullSchemaConformance` | No old keys leaked, all formats |

**`test_run_iss_integration.py` (43 tests):**

| Test class | What it verifies |
|-----------|-----------------|
| `TestWriteCommitlog` | B/A format, max_records cap, regs have values, mem renamed, no regs in A |
| `TestValidateCommitlog` | Valid passes, missing schema_version fails, wrong version fails, missing src fails, invalid pc fails |
| `TestAtomicUpdateManifest` | Simple key, dotted nesting, no .tmp left, idempotent |
| `TestExitCodes` | PASS=0, INFRA=2, CONFIG=3 |
| `TestRunIssManifest` | EXIT_PASS, commitlog created, phases written, v2.0.0 output, missing binary, missing ELF, Spike not found, empty Spike output, manifest not found, FORMAT A fallback |
| `TestCLIManifestMode` | `--manifest` flag routes correctly |
| `TestCLILegacyMode` | Legacy pass, v2.0.0 output, missing ELF, Spike not found |
| `TestISSEfficiencyTracker` | Record + retrieve, no plateau with insufficient data, plateau detected, no plateau with high variance, stats, ISA isolation, context manager |
| `TestManifestEfficiencyIntegration` | DB created after manifest run, correct commit count recorded |
| `TestRealSpike` | Skipped unless `RV32IM_ELF` set — validates real Spike v2.0.0 output |

---

## 11. Integration with Other Agents

### Agent B (RTL Harness — Verilator)

Agent B writes `rtl.commitlog.jsonl` using **the same schema v2.0.0**. The only difference is `src: "rtl"` and the optional presence of `cycle` (RTL simulation cycle at commit). Agent C's schema is the authoritative definition for both.

### Agent D (Comparator)

Agent D reads both `rtl.commitlog.jsonl` and `iss.commitlog.jsonl` and finds the first divergence. It relies on:
- `regs: [{rd, value}]` — to detect register mismatch (`rtl.regs[n].value != iss.regs[n].value`)
- `csrs: [{addr, value}]` — to detect CSR write mismatch
- `pc` — to detect control-flow divergence
- `trap` — to detect exception handling mismatch
- `seq` — to align paired records

Agent D produces `diff_report.json` with mismatch type, severity, sequence number, and a context window of surrounding commits.

### AVA Orchestrator (`ava.py`)

The `SpikeISS` class in `ava.py` calls `run_iss_manifest()` (via `run_iss.py`) when a real ELF is provided. The orchestrator passes `elf_path`, `run_dir`, `seed`, and `isa` through `generate_suite()` → `_tandem_simulation()` → `spike_iss.run_tandem()` → `_simulate_iss()`.

When Agent B has already written `rtl.commitlog.jsonl` to `run_dir`, the `_simulate_rtl()` stub is bypassed and the real RTL data is used. The full diff pipeline then runs with real data from both sides.

---

## 12. Technologies & Methodology

### Core Technologies

| Technology | Role | Why chosen |
|-----------|------|------------|
| **Spike RISC-V ISS** | Golden reference simulator | RISC-V Foundation reference implementation; used by chip companies worldwide; handles all privilege modes, CSRs, traps |
| **Python 3.9+** | Implementation language | Stdlib only (no external deps); fast iteration; asyncio-compatible for AVA orchestrator |
| **SQLite + WAL mode** | Metrics persistence | Zero-config, concurrent-reader-safe, POSIX-atomic writes |
| **JSONL (newline-delimited JSON)** | Wire format | Streamable (no full parse to read first record); line-addressable (wc -l = instruction count); human-readable; language-agnostic |
| **Regex state machine** | Log parsing | Spike's format is line-oriented but stateful — a regex per line type + buffering is the minimal correct approach |

### Verification Methodology

**Differential testing (co-simulation):** Run the DUT and a trusted reference in lockstep on identical inputs and compare outputs. This is the same technique used by:
- Intel's FDIV bug discovery (Pentium, 1994) — would have been caught by differential testing
- ARM's verification of Cortex-A CPUs against their ISS
- SiFive's internal verification of RISC-V cores
- RISC-V Foundation compliance testing via RISCOF

**Commit-log comparison vs. cycle-level comparison:** Agent C operates at the *architectural commit boundary* rather than the microarchitectural cycle level. This means:
- Out-of-order processors can still be verified (we compare retired state, not in-flight state)
- Speculative execution is invisible (only committed instructions appear)
- The comparison is ISA-level correct regardless of pipeline depth or width

**Structured exit codes for CI/CD:** The three-value exit code (`PASS=0 / INFRA=2 / CONFIG=3`) allows the orchestrator to distinguish between "retry because Spike timed out" (INFRA) and "don't retry because the ELF path is wrong" (CONFIG) — a critical distinction in automated nightly regression pipelines.

**Atomic manifest updates:** The dotted-key pattern (`phases.iss.status`) combined with POSIX-atomic rename means the manifest always reflects a consistent state, enabling the orchestrator to restart a campaign from any checkpoint without corruption.

### Design Principles

1. **No placeholders.** Every method either calls a real backend or raises an informative error with remediation steps — no `asyncio.sleep(0.1)` returning fake data.

2. **Library-first, subprocess-never (for parser).** The parser is imported as a module, not shelled out to. 100ms × 10,000 CI runs = 1,000 seconds of unnecessary process startup overhead.

3. **Streaming by default.** `parse_spike_log_streaming()` yields one record at a time, keeping memory proportional to the window size rather than the entire run length. A 10M-instruction run with FORMAT B might produce 50GB of raw Spike output — streaming is mandatory.

4. **Deterministic and seedable.** The stub RTL path (before Agent B is connected) is seeded via `random.Random(seed)` so that CI runs with `seed=42` are byte-for-byte identical — essential for reproducing failures.

5. **Fail loudly with actionable messages.** Error messages include the `nm prog.elf | grep tohost` diagnostic command, the `spike --version` check, and the full Spike install recipe — so an engineer seeing an error message can resolve it without reading documentation.

---

## 13. Troubleshooting

### "Spike not found"
```
Install Spike:
  git clone https://github.com/riscv-software-src/riscv-isa-sim
  cd riscv-isa-sim && mkdir build && cd build
  ../configure --prefix=/opt/riscv
  make -j$(nproc) && make install
  export PATH=/opt/riscv/bin:$PATH
```

### "Spike produced no output"
Checklist:
1. Is the ELF a valid RISC-V binary? `file prog.elf`
2. Does the ISA match the ELF? `readelf -A prog.elf | grep Tag_RISCV_arch`
3. Does the ELF have a `tohost` symbol? `nm prog.elf | grep tohost`
4. If no `tohost`, the program will run forever — use the included `link.ld` which places `tohost` at a standard address

### "Parser produced zero records"
Spike ran but the output format wasn't recognised. Check:
- Does `spike --help` mention `--log-commits`? If not, try `--force-format A`
- Is the Spike build from before 2021? Upgrade to ≥ 1.1 for FORMAT B support

### "schema_version expected '2.0.0'" in validation
Old v1.x commitlogs (fields `source`, `reg_writes`, `csr_writes`, `mem_access`) are not compatible with Agent D's v2.0.0 parser. Re-run Agent C to regenerate the log.

### "Instruction count mismatch" in diff report
Normal causes:
- The ELF uses `ecall` to exit and the RTL and ISS handle it differently
- The RTL has an off-by-one in its tohost polling loop
- Check `diff_report.json` → `first_divergence` field for the exact sequence number

### Spike exits with code 1 — is this an error?
No. Bare-metal programs signal success by writing `1` to the HTIF `tohost` address. Spike exits 1 in response. Both exit codes 0 and 1 are treated as successful by Agent C.

---

## 14. Glossary

| Term | Definition |
|------|-----------|
| **ISS** | Instruction Set Simulator — software that executes a binary and models architectural state without any hardware |
| **Spike** | The RISC-V Foundation's reference ISS, written by the Berkeley Architecture Research group |
| **Commit log** | A record of every instruction that *retired* (completed without being squashed), in order, with its architectural side-effects |
| **Differential testing / co-simulation** | Running two implementations of the same specification on the same input and comparing outputs |
| **HTIF / tohost** | Host-Target Interface — a convention by which bare-metal RISC-V programs signal completion to Spike by writing to a memory-mapped `tohost` symbol |
| **FORMAT A** | Spike's `-l` log: one line per instruction with PC, encoding, disasm. No register values |
| **FORMAT B** | Spike's `--log-commits` log: one or more lines per instruction including register/CSR/memory writes |
| **JSONL** | JSON Lines — one JSON object per line; streamable and line-addressable |
| **WAL** | Write-Ahead Logging — SQLite journal mode that allows concurrent readers and one writer |
| **Plateau** | The state where ISS commit counts stop varying across runs — a signal that the test generator is no longer exploring new territory |
| **AVA** | Autonomic Verification Agent — the full multi-agent RISC-V verification platform that Agent C is a component of |
| **Agent B** | AVA's RTL harness (Verilator) — produces `rtl.commitlog.jsonl` using the same schema |
| **Agent D** | AVA's comparator — consumes both commitlogs, finds first divergence, classifies mismatches |
| **Schema v2.0.0** | The AVA commit-log wire format defined in `commitlog.schema.json` — adds `schema_version`, `hart`, `fpregs` and renames `source→src`, `reg_writes→regs`, `csr_writes→csrs`, `mem_access→mem` |

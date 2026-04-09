# Agent E — RISC-V Architectural Compliance Runner

> **Part of the AVA (Automated Verification Architecture) multi-agent verification platform.**
> Agent E is the compliance gate: it proves that a RISC-V CPU implementation executes every instruction exactly as the ISA specification demands, before any higher-level verification layer runs.

---

## Table of Contents

1. [What Is This?](#1-what-is-this)
2. [Why It Matters](#2-why-it-matters)
3. [How It Works — End to End](#3-how-it-works--end-to-end)
4. [Architecture](#4-architecture)
5. [Files in This Module](#5-files-in-this-module)
6. [Quick Start](#6-quick-start)
7. [Example — Full Run with Output](#7-example--full-run-with-output)
8. [Manifest Mode (AVA Contract)](#8-manifest-mode-ava-contract)
9. [Agent B Integration (RTL DUT)](#9-agent-b-integration-rtl-dut)
10. [Embedded Test Suite](#10-embedded-test-suite)
11. [Signature Mechanism Explained](#11-signature-mechanism-explained)
12. [Exit Codes](#12-exit-codes)
13. [Configuration Reference](#13-configuration-reference)
14. [Output Files](#14-output-files)
15. [Technology & Methodology](#15-technology--methodology)
16. [Test Suite](#16-test-suite)
17. [Extending Agent E](#17-extending-agent-e)
18. [Where Agent E Sits in the AVA Pipeline](#18-where-agent-e-sits-in-the-ava-pipeline)
19. [Troubleshooting](#19-troubleshooting)
20. [Glossary](#20-glossary)

---

## 1. What Is This?

Agent E is a **self-contained RISC-V architectural compliance test runner** written in Python. It implements the [RISCOF](https://riscof.readthedocs.io/) / [riscv-arch-test](https://github.com/riscv-non-isa/riscv-arch-test) *signature comparison* methodology and integrates it into the AVA multi-agent verification platform.

In plain terms: given a RISC-V CPU design (the Device Under Test, or DUT), Agent E:

1. **Builds** small assembly test programs that exercise every RV32I/RV32M instruction.
2. **Runs** each program through two simulators — a trusted golden reference (Spike ISS) and the DUT.
3. **Compares** the *signature* — a block of memory words written by the test — between the two.
4. **Reports** pass/fail per test and publishes structured results back to the AVA orchestrator.

If any word differs, the test fails and Agent E reports exactly *which word* mismatches and at *which memory offset*, giving engineers a precise, reproducible reproduction point.

---

## 2. Why It Matters

### The problem compliance testing solves

A CPU simulator or RTL implementation may *appear* to work on application code — a `printf("hello")` might execute correctly — while silently computing wrong answers for edge cases like:

- **Signed integer overflow** in `ADD`/`SUB`
- **Divide-by-zero** behaviour for `DIV`/`DIVU` (the RISC-V spec mandates specific values, not a trap)
- **Signed multiply high word** (`MULHSU`) with mixed sign operands
- **Arithmetic vs logical right shift** (`SRA` vs `SRL`)
- **Sign extension** on byte and halfword loads (`LB`, `LH`)
- **Branch direction** for unsigned comparisons (`BLTU`, `BGEU`) near `0x80000000`

These bugs are invisible to functional tests but cause real failures in production software. The RISC-V ISA specification defines the *exact* expected output for every instruction and every edge case. Compliance testing compares your implementation against that exact definition.

### Why this approach is rigorous

The **signature comparison** approach is the same technique used by the official [RISC-V International compliance working group](https://github.com/riscv-non-isa/riscv-arch-test). The golden reference (Spike, the official RISC-V ISA simulator) is the spec made executable. Any deviation — even in an "obscure" corner case — is a real bug.

### Where Agent E fits in AVA

```
Orchestrator
├── Agent A  — System Architect (interfaces, schemas)
├── Agent B  — RTL Harness + Verilator Backend      ← DUT runner
├── Agent C  — Spike/Sail Golden Backend
├── Agent D  — Commit-log Comparator + Triage
├── Agent E  — Compliance Runner (this module)       ← compliance gate
├── Agent F  — Coverage Pipeline + Director
├── Agent G  — Test Generation (RV32IM)
└── Agent H  — Phase-2 Red Team Integration
```

Agent E is the **first gate** a DUT must pass. Until Agent E reports `status: passed`, higher-level agents (coverage, red-teaming) are not meaningfully useful — you cannot trust coverage metrics from a CPU that computes wrong answers.

---

## 3. How It Works — End to End

```
┌─────────────────────────────────────────────────────────────────────┐
│                      ComplianceRunner.run()                         │
│                                                                     │
│  1. COLLECT                                                         │
│     ├─ Instantiate 9 embedded test cases (ADD, SUB, LOGICAL,       │
│     │   SHIFT, BRANCH, LOAD-STORE, MUL, DIV, DIVU-REMU)           │
│     └─ Optionally discover tests from riscv-arch-test repo         │
│                                                                     │
│  2. BUILD  (CPU-bound ThreadPoolExecutor, build_workers threads)    │
│     For each test:                                                  │
│     ├─ Write .S source (macros inlined, no header deps)            │
│     ├─ Check SHA-256 build cache → skip if source unchanged        │
│     └─ riscv32-unknown-elf-gcc → ELF (with retry + back-off)       │
│                                                                     │
│  3. RUN  (I/O-bound ThreadPoolExecutor, run_workers threads)        │
│     For each test:                                                  │
│     ├─ GOLDEN: spike --isa=rv32im --signature=golden.sig test.elf  │
│     │          Spike writes begin_signature..end_signature          │
│     │          as one 32-bit hex word per line                     │
│     └─ DUT:    SpikeDUTBackend (self-test)                         │
│                OR ExternalDUTBackend → run_rtl_adapter.py           │
│                   └─ Agent B's run_rtl.py --manifest ...           │
│                      --sig-out dut.sig --sig-begin ... --sig-end …  │
│                                                                     │
│  4. COMPARE                                                         │
│     compare_signatures(golden_words, dut_words, max_mismatches=3)  │
│     ├─ PASS: every word identical                                  │
│     ├─ FAIL: first mismatch at word[N]: golden=0x... dut=0x...    │
│     └─ ERROR: simulator crash, timeout, empty signature, etc.      │
│                                                                     │
│  5. REPORT                                                          │
│     ├─ compliance_report.json     (machine-readable, full detail)  │
│     ├─ compliance_report.html     (human-readable, colour-coded)   │
│     └─ compliance_report_junit.xml (CI/CD: GitHub Actions, Jenkins)│
│                                                                     │
│  6. PATCH MANIFEST  (AVA contract mode only)                        │
│     patch_manifest(manifest.json, {                                 │
│         phases.compliance: {status, elapsed_sec, pass, fail, ...}, │
│         outputs.signaturedir, outputs.compliance_result,           │
│         compliance.result: {schemaversion: "2.0.0", failedlist},  │
│         status: "passed" | "failed"                                │
│     })                                                              │
└─────────────────────────────────────────────────────────────────────┘
```

### The signature mechanism in detail

Every test program follows this memory layout, set by the linker script Agent E generates:

```
0x80000000  .text.init       ← test code (_start entry point)
            .data.string     ← test data (e.g. scratch_word for load/store)
            .tohost          ← ALIGN(64); Spike's HTIF exit signal
            .bss
            ALIGN(8)
begin_signature:             ← 64 bytes of zero-filled memory
  word[0] ... word[15]       ← test results written by WRITE_SIG macro
end_signature:
```

The test code:
1. Executes instructions under test, storing results with `WRITE_SIG reg`.
2. Signals completion by writing `1` to `tohost` (HTIF). Spike sees this and exits.
3. Spike dumps `begin_signature`..`end_signature` as one hex word per line to `--signature=golden.sig`.

The DUT does the same, producing `dut.sig`. Agent E then compares word-by-word.

---

## 4. Architecture

### Class diagram

```
RunConfig (dataclass, frozen config)
    │
    ▼
ComplianceRunner
    ├── probe_spike()          → (found: bool, version: str)
    ├── probe_toolchain()      → (gcc_path, objdump_path)
    ├── RetryPolicy            → exponential back-off wrapper
    ├── BuildCache             → SHA-256 source hash → cached ELF
    ├── DUTBackend (ABC)
    │   ├── SpikeDUTBackend    → Spike as DUT (self-test mode)
    │   └── ExternalDUTBackend → run_rtl_adapter.py (Agent B)
    │
    ├── _collect()   → List[TestRecord]
    ├── _build_all() → CPU-bound ThreadPoolExecutor(build_workers)
    ├── _run_all()   → I/O-bound ThreadPoolExecutor(run_workers)
    │   └── _run_single(tc)
    │       ├── run_golden()         → golden.sig via Spike
    │       ├── DUTBackend.run()     → dut.sig via DUT
    │       ├── parse_signature()    → List[str] (8-char hex words)
    │       └── compare_signatures() → (passed, first_idx, mismatches)
    │
    └── _make_report() + _write_reports()
            ├── RunReport (frozen dataclass)
            ├── render_html()
            ├── render_junit_xml()
            └── patch_manifest()    → atomic deep-merge (AVA contract)

run_rtl_adapter.py  (standalone bridge script)
    ├── _find_run_rtl()          → locate Agent B's run_rtl.py
    ├── _nm_sig_addresses()      → riscv*-nm → begin/end_signature hex
    ├── _write_manifest()        → Agent B manifest JSON (atomic)
    ├── run_adapter()            → invoke run_rtl.py, resolve sig path
    └── _validate_sig_content()  → ≥1 valid 32-bit hex word
```

### Data flow

```
manifest.json (read)
      │
      ▼
RunConfig ──► ComplianceRunner
                     │
            ┌────────┴────────┐
            ▼                 ▼
      [build pool]       [run pool]
      GCC → ELF          Spike golden → golden.sig
                         DUT (adapter) → dut.sig
                         compare_signatures()
                              │
                    ┌─────────┴──────────┐
                    ▼                    ▼
             TestRecord.result      TestRecord.mismatch_idx
             PASS/FAIL/ERROR        first differing word
                    │
                    ▼
             RunReport ──► compliance_report.json
                       ──► compliance_report.html
                       ──► compliance_report_junit.xml
                       ──► patch_manifest(manifest.json)
                               phases.compliance.status
                               compliance.result.failedlist
                               status: "passed"/"failed"
```

---

## 5. Files in This Module

| File | Lines | Role |
|---|---|---|
| `run_compliance.py` | 2302 | Main compliance runner — everything from build to report |
| `run_rtl_adapter.py` | 489 | Agent E ↔ Agent B bridge; translates `--sig` contract to Agent B's manifest API |
| `test_compliance_runner.py` | 1040 | 110-test suite across 20 test classes; runs without real hardware |

---

## 6. Quick Start

### Prerequisites

```bash
# RISC-V GNU toolchain (any of these prefixes works)
sudo apt install gcc-riscv64-unknown-elf        # Ubuntu/Debian
brew install riscv-gnu-toolchain               # macOS

# Spike RISC-V ISA simulator
git clone https://github.com/riscv-software-src/riscv-isa-sim
cd riscv-isa-sim && mkdir build && cd build
../configure --prefix=/usr/local && make -j$(nproc) && sudo make install

# Python 3.10+ (no third-party packages required)
python --version   # 3.10+
```

### Self-test mode (Spike as both golden and DUT)

This is the fastest way to verify Agent E is working — golden and DUT are both Spike, so all tests should pass:

```bash
python run_compliance.py --isa RV32IM
```

### With Agent B (Verilator RTL DUT)

```bash
python run_compliance.py \
    --isa    RV32IM \
    --dut-sim run_rtl_adapter.py
```

### With the official riscv-arch-test suite

```bash
git clone https://github.com/riscv-non-isa/riscv-arch-test ~/riscv-arch-test

python run_compliance.py \
    --isa            RV32IM \
    --arch-test-repo ~/riscv-arch-test \
    --dut-sim        run_rtl_adapter.py
```

### Run the test suite (no hardware required)

```bash
python test_compliance_runner.py -v
# → Ran 110 tests in 0.890s  OK (skipped=5)
```

---

## 7. Example — Full Run with Output

### Command

```bash
python run_compliance.py \
    --isa     RV32IM \
    --spike   spike \
    --out-dir results/ci_run_001 \
    -j 4 \
    -v
```

### Terminal output

```
────────────────────────────────────────────────────────────────────
  RISC-V Compliance  ISA=RV32IM  spike=Spike RISC-V ISA Simulator 1.1.1
────────────────────────────────────────────────────────────────────
  ✓  ADD-01                     RV32I    PASS [cached]
  ✓  SUB-01                     RV32I    PASS [cached]
  ✓  LOGICAL-01                 RV32I    PASS
  ✓  SHIFT-01                   RV32I    PASS
  ✓  BRANCH-01                  RV32I    PASS
  ✓  LOAD-STORE-01              RV32I    PASS
  ✓  MUL-01                     RV32M    PASS
  ✓  DIV-01                     RV32M    PASS
  ✓  DIVU-REMU-01               RV32M    PASS
────────────────────────────────────────────────────────────────────
  PASS=9  FAIL=0  ERROR=0  total=9  (100.0%)
  HTML  → results/ci_run_001/20250115_143022/compliance_report.html
  JSON  → results/ci_run_001/20250115_143022/compliance_report.json
  JUnit → results/ci_run_001/20250115_143022/compliance_report_junit.xml
────────────────────────────────────────────────────────────────────
```

### JSON report (excerpt)

```json
{
  "timestamp": "2025-01-15T14:30:22+00:00",
  "isa": "RV32IM",
  "spike_version": "Spike RISC-V ISA Simulator 1.1.1",
  "toolchain": "riscv32-unknown-elf-gcc",
  "summary": {
    "total": 9, "pass": 9, "fail": 0, "error": 0,
    "pass_rate_pct": 100.0
  },
  "tests": [
    {
      "name": "DIV-01",
      "isa_subset": "RV32M",
      "result": "PASS",
      "mismatch_idx": -1,
      "golden_words": ["00000004", "fffffffc", "ffffffff",
                       "0000002a", "80000000", "00000000",
                       "00000003", "00000002"],
      "dut_words":    ["00000004", "fffffffc", "ffffffff",
                       "0000002a", "80000000", "00000000",
                       "00000003", "00000002"],
      "build_time_s": 0.412,
      "run_time_s": 0.083,
      "cache_hit": true
    }
  ]
}
```

### What the DIV-01 golden words mean

```
word[0] = 0x00000004   →  20 /  5 = 4                (plain division)
word[1] = 0xFFFFFFFC   → -20 /  5 = -4               (signed negative dividend)
word[2] = 0xFFFFFFFF   →  42 /  0 = -1               (div-by-zero: spec mandates -1)
word[3] = 0x0000002A   →  42 rem 0 = 42              (rem-by-zero: spec mandates dividend)
word[4] = 0x80000000   →  INT_MIN / -1 = INT_MIN      (overflow: spec mandates INT_MIN)
word[5] = 0x00000000   →  INT_MIN rem -1 = 0          (overflow remainder: spec mandates 0)
word[6] = 0x00000003   →  17 /  5 = 3
word[7] = 0x00000002   →  17 rem 5 = 2
```

If a DUT incorrectly traps on divide-by-zero instead of returning `-1`, word[2] would differ — Agent E catches it immediately.

### Example of a FAIL

```
  ✗  DIV-01                     RV32M    FAIL
       └─ Sig mismatch at word[2]: golden=0xffffffff  dut=0x00000000  (1 total diff)
```

This tells the engineer: *your DUT returns 0 for division-by-zero, but the RISC-V spec mandates −1.*

---

## 8. Manifest Mode (AVA Contract)

The AVA orchestrator runs Agent E via a single manifest flag. This is the production entrypoint — it reads config from the manifest, runs, and writes structured results back.

### Invocation

```bash
python run_compliance.py --manifest runs/run_042/run_manifest.json
```

### Manifest fields read by Agent E

```json
{
  "rundir":   "runs/run_042",
  "binary":   "runs/run_042/test.elf",
  "isa":      "RV32IM",
  "spikebin": "spike",
  "workers":  8,
  "compliance": {
    "suitepath": "/home/ci/riscv-arch-test",
    "dutsim":    "run_rtl_adapter.py"
  }
}
```

### What Agent E writes back (atomic deep-merge)

```json
{
  "phases": {
    "compliance": {
      "status":      "completed",
      "elapsed_sec": 12.4,
      "timestamp":   "2025-01-15T14:30:35+00:00",
      "pass": 9, "fail": 0, "error": 0, "total": 9
    }
  },
  "outputs": {
    "signaturedir":      "compliance/signatures",
    "compliance_result": "compliance.result.json"
  },
  "compliance": {
    "result": {
      "schemaversion": "2.0.0",
      "total": 9, "pass": 9, "fail": 0, "error": 0,
      "pass_pct": 100.0,
      "failedlist": []
    }
  },
  "status": "passed"
}
```

When a test fails, `failedlist` contains one entry per failing test:

```json
"failedlist": [
  {
    "test":          "DIV-01",
    "isa_subset":    "RV32M",
    "mismatch_word": 2,
    "error_class":   "signature_compare",
    "message":       "Sig mismatch at word[2]: golden=0xffffffff dut=0x00000000"
  }
]
```

`mismatch_word` is the index of the **first** differing 32-bit word in the signature region — giving engineers the exact line in the test to investigate.

### Atomicity guarantee

All manifest writes use `os.replace(tmp, target)` — a POSIX-atomic rename. No other agent ever sees a partial write. This mirrors Agent B's `patch_manifest()` exactly, so the orchestrator can read either agent's manifest output without special handling.

---

## 9. Agent B Integration (`run_rtl_adapter.py`)

`run_rtl_adapter.py` is the bridge between Agent E's signature contract and Agent B's Verilator RTL simulation API. It is what you pass as `--dut-sim`.

### The problem it solves

Agent E's `ExternalDUTBackend` expects:
```
python <script> --elf test.elf --sig dut.sig --isa RV32IM
```

Agent B's `run_rtl.py` expects:
```
python run_rtl.py --manifest manifest.json --sig-out dut.sig --sig-begin 0x... --sig-end 0x...
```

The adapter translates between these two APIs so neither agent needs to change its interface.

### Flow

```
ExternalDUTBackend
      │
      │  python run_rtl_adapter.py --elf test.elf --sig dut.sig --isa RV32IM
      ▼
run_rtl_adapter.py
      │
      ├─ 1. riscv*-nm test.elf → find begin_signature / end_signature addresses
      │      (avoids hardcoded address assumptions; works for any test size)
      │
      ├─ 2. Write adapter_manifest.json:
      │      { runid, seed, binary: "test.elf", dut: "rv32im_core", rundir: ... }
      │
      ├─ 3. python run_rtl.py --manifest adapter_manifest.json
      │                        --sig-out dut.sig
      │                        --sig-begin 0x80002000 --sig-end 0x80002040
      │      [Agent B: builds Verilator model, simulates ELF, dumps sig]
      │
      ├─ 4. Read updated manifest → outputs.signature path
      │      (Agent B writes this; adapter uses it to find the file)
      │
      ├─ 5. Validate: ≥1 valid 32-bit hex word
      │
      └─ 6. Copy to dut.sig (Agent E's expected location)
            Clean up ephemeral work directory
```

### Locating Agent B's `run_rtl.py`

The adapter searches in priority order:
1. Same directory as `run_rtl_adapter.py`
2. `../backends/run_rtl.py` (monorepo layout)
3. `AGENT_B_RTL` environment variable
4. `--agent-b` CLI flag (explicit override)

### Signature address resolution

Instead of hardcoding `--sig-begin` / `--sig-end`, the adapter can resolve addresses from the ELF symbol table:

```bash
python run_rtl_adapter.py \
    --elf test.elf --sig dut.sig \
    --sig-begin auto --sig-end auto
```

This calls `riscv*-nm test.elf`, finds the `begin_signature` and `end_signature` symbols, and passes the exact addresses to Agent B. This is important because the signature region's address varies slightly between tests depending on code and data sizes.

---

## 10. Embedded Test Suite

Agent E ships **9 built-in compliance tests** covering every required RV32IM instruction group. No external test suite is needed for basic operation.

| Test | ISA | Instructions Covered | Key Corner Cases |
|---|---|---|---|
| `ADD-01` | RV32I | `ADD` | Signed overflow wrapping, `x0` identity, `−1+1=0` |
| `SUB-01` | RV32I | `SUB` | Underflow wrapping, `x−x=0`, `0x80000000−1` |
| `LOGICAL-01` | RV32I | `AND`, `OR`, `XOR`, `XORI` | NOT-via-XORI, AND-with-zero, OR-with-`−1` |
| `SHIFT-01` | RV32I | `SLL`, `SRL`, `SRA` | Shift by 0, shift by 31, shift mod 32, arithmetic vs logical |
| `BRANCH-01` | RV32I | `BEQ`, `BNE`, `BLT`, `BGE`, `BLTU`, `BGEU` | Taken and not-taken for all 6 types; signed vs unsigned at `0x80000000` |
| `LOAD-STORE-01` | RV32I | `SW`, `LW`, `SB`, `LBU`, `SH`, `LHU`, `LH`, `LB` | Sign extension on `LH`/`LB`, unsigned zero-extension on `LHU`/`LBU` |
| `MUL-01` | RV32M | `MUL`, `MULH`, `MULHU`, `MULHSU` | Zero operand, negative × positive, all four high-word variants |
| `DIV-01` | RV32M | `DIV`, `REM` | Divide-by-zero (→ −1), rem-by-zero (→ dividend), `INT_MIN / −1` overflow |
| `DIVU-REMU-01` | RV32M | `DIVU`, `REMU` | Unsigned divide-by-zero (→ 0xFFFFFFFF), zero dividend |

### Assembly macro system

Each test uses a small set of GAS macros that are **inlined into the generated source** — no external header files are needed. This makes the test suite hermetic and portable.

```asm
/* Initialise x29 (t4) as the moving signature write pointer */
.macro SIG_INIT
    la      x29, begin_signature
.endm

/* Store one result word and advance the pointer */
.macro WRITE_SIG reg
    sw      \reg, 0(x29)
    addi    x29, x29, 4
.endm

/* Signal pass via HTIF tohost — Spike monitors this address */
.macro RVTEST_PASS
    li      t0, 1
    la      t1, tohost
    sw      t0, 0(t1)
.Lpass_spin:
    j       .Lpass_spin    /* spin until Spike terminates us */
.endm
```

**Why HTIF and not `ecall`?** Spike's `--signature` mode monitors the `tohost` symbol via the Host-Target Interface (HTIF). When the simulation writes `1` to `tohost`, Spike dumps the signature region and exits. Using `ecall` would require a trap handler stub, adding complexity and a potential bug source.

### Adding more tests

You can add tests to the `_EMBEDDED_TESTS` list in `run_compliance.py`:

```python
_EMBEDDED_TESTS.append((
    "SLT-01", "RV32I",
    r"""
        SIG_INIT
        li      t0, -1
        li      t1,  1
        slt     t2, t0, t1    /* -1 < 1 (signed) = 1 */
        WRITE_SIG t2
        sltu    t2, t0, t1    /* 0xFFFFFFFF > 1 (unsigned) = 0 */
        WRITE_SIG t2
        RVTEST_PASS
    """,
    "",  # data body (empty for this test)
))
```

Or point at the official suite: `--arch-test-repo ~/riscv-arch-test`

---

## 11. Signature Mechanism Explained

The signature mechanism is the core of RISCOF-style compliance testing. Here is what happens step by step for `DIV-01`:

```
Test ELF in memory (0x80000000):
┌──────────────────────┐
│  .text.init          │  ← SIG_INIT, li instructions, div/rem, WRITE_SIG, RVTEST_PASS
│  .tohost  (ALIGN 64) │  ← 0x00000000 initially; RVTEST_PASS writes 1 here
│  .bss                │
│  begin_signature:    │  ← 0x80002000 (example)
│    word[0]: 00000004 │  ← result of 20/5
│    word[1]: FFFFFFFC │  ← result of -20/5
│    word[2]: FFFFFFFF │  ← result of 42/0 (div-by-zero → -1 per spec)
│    word[3]: 0000002A │  ← result of 42 rem 0 (→ dividend=42 per spec)
│    word[4]: 80000000 │  ← result of INT_MIN/-1 (overflow → INT_MIN per spec)
│    word[5]: 00000000 │  ← result of INT_MIN rem -1 (→ 0 per spec)
│    word[6]: 00000003 │  ← result of 17/5
│    word[7]: 00000002 │  ← result of 17 rem 5
│    word[8..15]: 0    │  ← zero-filled (FILL(0x00000000) in linker script)
│  end_signature:      │
└──────────────────────┘

After RVTEST_PASS:
  Spike detects tohost=1, dumps begin_signature..end_signature to golden.sig:
      00000004
      fffffffc
      ffffffff
      0000002a
      80000000
      00000000
      00000003
      00000002

DUT runs same ELF → produces dut.sig (ideally identical)

compare_signatures(golden_words, dut_words, max_mismatches=3):
  word[0]: 00000004 == 00000004 ✓
  word[1]: fffffffc == fffffffc ✓
  word[2]: ffffffff == ffffffff ✓   ← DUT correctly returns -1 for div-by-zero
  ...all match → PASS
```

---

## 12. Exit Codes

Agent E uses a **4-level exit code scheme** that distinguishes between different categories of failure. This lets CI/CD pipelines and the AVA orchestrator respond differently to each case.

| Code | Constant | Meaning | When it happens |
|---|---|---|---|
| `0` | `EXIT_PASS` | All tests passed | Every test's golden sig == DUT sig |
| `1` | `EXIT_FAIL` | ≥1 signature mismatch | DUT computed a wrong answer |
| `2` | `EXIT_CRASH` | Infrastructure failure | Build failed, simulator crashed, timeout, empty sig |
| `3` | `EXIT_TOOL` | Required tool not found | `spike` or `riscv-gcc` not on PATH |

**Priority order** (when multiple categories apply): `EXIT_TOOL` > `EXIT_CRASH` > `EXIT_FAIL` > `EXIT_PASS`

### CI/CD usage

```yaml
# GitHub Actions example
- name: Run compliance check
  run: python run_compliance.py --manifest manifest.json
  # Exit 0 → proceed to coverage. Exit 1 → block PR. Exit 2/3 → alert infra team.
```

---

## 13. Configuration Reference

### `RunConfig` fields

| Field | Type | Default | Description |
|---|---|---|---|
| `isa` | `str` | `"RV32IM"` | ISA string. `"RV32I"` skips M-extension tests. |
| `spike_bin` | `str` | `"spike"` | Spike binary name or absolute path. |
| `dut_sim` | `Optional[str]` | `None` | DUT adapter script. `None` = Spike-as-DUT (self-test). |
| `arch_test_repo` | `Optional[Path]` | `None` | Path to `riscv-arch-test` checkout. |
| `out_dir` | `Path` | `"compliance_results"` | Root output directory. |
| `workers` | `int` | `4` | Base worker count (see below). |
| `build_workers` | `int` | `0` | CPU-bound GCC workers. `0` = `workers`. |
| `run_workers` | `int` | `0` | I/O-bound simulator workers. `0` = `workers × 2`. |
| `max_mismatches` | `int` | `3` | Stop comparison after N mismatches. `0` = unlimited. |
| `timeout_build_s` | `int` | `120` | Per-test build timeout (seconds). |
| `timeout_run_s` | `int` | `60` | Per-test simulator timeout (seconds). |
| `retry_max` | `int` | `2` | Subprocess retry attempts. |
| `retry_delay_s` | `float` | `0.5` | Initial retry delay (doubles each attempt). |
| `verbose` | `bool` | `False` | Enable DEBUG logging. |
| `use_cache` | `bool` | `True` | Enable SHA-256 build cache. |

### CLI flags

```
--manifest PATH        AVA contract mode (reads all config from manifest)
--isa STRING           RV32I or RV32IM (default: RV32IM)
--spike SPIKE_BIN      Spike binary (default: spike)
--dut-sim SCRIPT       DUT adapter (default: Spike-as-DUT)
--arch-test-repo DIR   riscv-arch-test checkout
--out-dir DIR          Output root (default: compliance_results)
-j / --workers N       Base parallelism (default: 4)
--build-workers N      CPU-bound build workers (default: j)
--run-workers N        I/O-bound run workers (default: j*2)
--max-mismatches N     Stop after N mismatches per test (default: 3)
--timeout-build SEC    Build timeout (default: 120)
--timeout-run SEC      Simulator timeout (default: 60)
--retry N              Subprocess retries (default: 2)
--no-cache             Disable SHA-256 build cache
-v / --verbose         DEBUG logging
```

---

## 14. Output Files

For each run, a timestamped subdirectory is created under `--out-dir`:

```
compliance_results/
├── compliance_report.json       ← symlink to latest run
├── compliance_report.html       ← symlink to latest run
├── compliance_report_junit.xml  ← symlink to latest run
├── .build_cache/                ← persistent SHA-256 ELF cache
│   ├── cache_index.json
│   └── <sha256_prefix>.elf
└── 20250115_143022/             ← timestamped run directory
    ├── compliance.log           ← full run log
    ├── compliance_report.json   ← machine-readable results
    ├── compliance_report.html   ← human-readable results
    ├── compliance_report_junit.xml
    ├── compliance.ld            ← generated linker script
    ├── src/                     ← generated .S source files
    │   ├── ADD-01.S
    │   ├── DIV-01.S
    │   └── ...
    ├── build/                   ← compiled ELF files
    │   ├── ADD-01.elf
    │   └── ...
    └── signatures/              ← signature files per test
        ├── ADD-01/
        │   ├── golden.sig       ← Spike reference output
        │   └── dut.sig          ← DUT output
        └── ...
```

### HTML report features

- Pass rate progress bar (green/amber/red based on %)
- Per-test signature diff table with mismatching words highlighted in red
- Build time, run time, and cache-hit indicators per test
- Tool error warnings section
- Fully self-contained (no external CSS/JS dependencies)

### JUnit XML (for CI/CD)

Compatible with GitHub Actions `dorny/test-reporter`, Jenkins JUnit plugin, GitLab CI `junit` artifact, and any other CI system that reads JUnit XML.

```xml
<testsuite name="RISCV-Compliance-RV32IM" tests="9" failures="0" errors="0">
  <testcase classname="compliance.RV32M" name="DIV-01" time="0.083"/>
  <testcase classname="compliance.RV32M" name="MUL-01" time="0.071"/>
  ...
</testsuite>
```

---

## 15. Technology & Methodology

### Methodology: RISCOF signature comparison

Agent E implements the [RISC-V architectural test framework](https://github.com/riscv-non-isa/riscv-arch-test/blob/main/spec/TestFormatSpec.adoc) signature comparison methodology. The key insight is:

> *Rather than checking every register and memory state at every cycle, write selected results to a contiguous memory region. Compare that region between the golden ISS and the DUT. Any semantic difference in instruction execution appears as a word mismatch.*

This approach is:
- **ISS-agnostic**: works with any golden simulator that can dump memory (Spike, Sail, QEMU).
- **DUT-agnostic**: works with any RTL simulator, FPGA, or physical chip that can run ELF binaries.
- **Stable**: the signature is defined by the test program, not by the comparison tool.

### Concurrency model

```
Phase 1: Build (CPU-bound)
    ThreadPoolExecutor(max_workers=build_workers)
    GCC processes spawn as subprocesses → GIL is released → true parallelism

Phase 2: Run (I/O-bound)
    ThreadPoolExecutor(max_workers=run_workers = workers × 2)
    Spike/DUT processes block on I/O → more threads than CPUs is beneficial
```

The two pools are separate because:
- **Building** is CPU-bound (GCC optimises C++ or compiles assembly). More threads than cores is wasteful.
- **Simulating** is I/O-bound (subprocess waiting on disk reads for ELF loading, HTIF polling). More threads than cores is beneficial — `run_workers` defaults to `2 × workers`.

### SHA-256 build cache

```
Key:   SHA-256(str(source_path.resolve()))[:32]   → cache ELF filename
Value: SHA-256(source_file_bytes)                 → content hash in index
```

If `source_content_hash == index[key]` and the cached ELF exists, the build is skipped entirely. This makes re-runs after code changes (e.g. linker script tweak) near-instant for unchanged tests. MD5 is avoided throughout — SHA-256 is used for both filename generation and content hashing for consistency and collision resistance.

### Incremental signature comparison

```python
compare_signatures(golden, dut, max_mismatches=3)
```

When a DUT is severely broken (e.g. all instructions produce zero), comparing all 16 words per test × 9 tests generates 144 mismatch log lines — overwhelming engineers. `max_mismatches=3` stops after 3 differences per test. The pass/fail decision is still correct (any mismatch is a FAIL); only the *number of reported differences* is capped.

### Atomic manifest writes

```python
# write to .tmp via os.fdopen, then os.replace → POSIX-atomic rename
def _atomic_write(path, content):
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)
```

`os.replace()` is a POSIX rename — atomic on all POSIX filesystems. No other agent ever reads a partially written file.

### Retry with exponential back-off

```python
RetryPolicy(max_attempts=2, base_delay_s=0.5)
```

Wraps every subprocess call. On failure, waits `0.5s`, then `1.0s`, then raises. This handles transient issues (file system latency on shared NFS, process table limits during parallel builds) without hanging indefinitely.

### Pluggable DUT backends

```python
class DUTBackend:          # abstract base
    def run(elf, sig, isa, timeout): ...

class SpikeDUTBackend:     # self-test (Spike == DUT)
class ExternalDUTBackend:  # Agent B via run_rtl_adapter.py
```

Adding a new DUT (e.g. QEMU, ModelSim) requires only implementing `DUTBackend.run()`. The rest of the pipeline is unchanged.

### Tools used

| Tool | Version | Role |
|---|---|---|
| [Spike ISS](https://github.com/riscv-software-src/riscv-isa-sim) | ≥1.1.0 | Golden reference simulator; `--signature` mode |
| [riscv-gnu-toolchain](https://github.com/riscv-collab/riscv-gnu-toolchain) | ≥12.x | Assembles and links test programs |
| Python | ≥3.10 | Runner, adapter, tests; zero third-party deps |
| [Verilator](https://www.veripool.org/verilator/) | ≥5.x | RTL simulation backend (Agent B, optional) |
| [riscv-arch-test](https://github.com/riscv-non-isa/riscv-arch-test) | any | Additional test sources (optional) |

---

## 16. Test Suite

The test suite (`test_compliance_runner.py`) has **110 tests across 20 classes**. It runs entirely without hardware — simulators and compilers are mocked where needed.

### Test classes

| Class | Tests | What it tests |
|---|---|---|
| `TestExitCodes` | 8 | `EXIT_*` constants; `compute_exit_code()` priority logic |
| `TestManifestHelpers` | 6 | `_deep_merge()` correctness; `patch_manifest()` atomicity |
| `TestManifestEntrypoint` | 9 | `run_compliance_manifest()`: missing file, invalid JSON, no tools, schema v2.0.0, `failedlist.mismatch_word`, runner crash handling |
| `TestAdapterCLI` | 5 | ELF pre-flight (missing, empty, bad magic); no Agent B; sig parent creation |
| `TestAdapterFlow` | 8 | Valid sig → pass; crash → EXIT_CRASH; timeout; empty sig; `outputs.signature` manifest field; nm address forwarding; work dir cleanup |
| `TestSplitWorkerPools` | 9 | `build_workers` / `run_workers` defaults and overrides; pool constructor args verified by mock interception |
| `TestIncrementalSigCmp` | 7 | `max_mismatches` parameter: stops at 3, 1, unlimited; pass unaffected; zero config |
| `TestBuildCacheSHA256` | 6 | Cache filename is SHA-256[:32] (not MD5); index hash is full 64-char SHA-256; hit/miss/corrupted index |
| `TestExceptions` | 2 | Exception hierarchy |
| `TestRunConfig` | 3 | ISA uppercasing; worker validation; `max_mismatches=0` valid |
| `TestAtomicWrite` | 3 | Creates file; no `.tmp` leftover; deep parent creation |
| `TestRetryPolicy` | 3 | Success first try; success after retry; exhaustion |
| `TestLinkerScript` | 6 | `.tohost` section; `begin/end_signature`; `FILL(0)`; `ALIGN(64)` for tohost; link address |
| `TestSourceGeneration` | 4 | HTIF not ecall in `RVTEST_PASS`; `tohost`/`fromhost` in data; scratch_word in data body |
| `TestSignatureParsing` | 6 | Basic parsing; `0x` prefix exact strip; non-hex skipped; file not found; zero-padding; uppercase normalisation |
| `TestSignatureComparison` | 6 | Identical; mismatch; trailing zeros match; DUT extra nonzero; empty; all-diffs without cap |
| `TestEmbeddedTestCoverage` | 8 | ≥9 tests total; ≥5 RV32I; ≥3 RV32M; no duplicates; all 4 div ops; all mul ops; all branch ops; sign extension |
| `TestJUnitXML` | 4 | Parseable XML; failure/error elements; pass has no failure |
| `TestRunnerNoTools` | 3 | Reports written with missing tools; exit code is `EXIT_TOOL`; AVA hook keys |
| `TestFullPipeline` | 5 | End-to-end (skipped without tools): ≥5 pass; exit code 0; split pools same result; manifest mode; schema |

### Running the tests

```bash
# All tests (no hardware needed; hardware tests auto-skip)
python test_compliance_runner.py -v

# Hardware tests only (requires spike + riscv-gcc on PATH)
python -m pytest test_compliance_runner.py -v -k "TestFullPipeline"

# Run a specific class
python -m pytest test_compliance_runner.py -v -k "TestManifestEntrypoint"

# With pytest and coverage
pip install pytest pytest-cov
pytest test_compliance_runner.py --cov=run_compliance --cov-report=html
```

---

## 17. Extending Agent E

### Adding a new DUT backend

```python
from run_compliance import DUTBackend, SimulationError

class QEMUBackend(DUTBackend):
    name = "qemu"

    def __init__(self, qemu_bin: str = "qemu-riscv32"):
        self.qemu_bin = qemu_bin

    def run(self, elf: Path, sig: Path, isa: str, timeout: int) -> None:
        # QEMU doesn't have --signature; use a semihosting write instead
        # or extract sig region from a memory dump
        cmd = [self.qemu_bin, "-M", "spike", str(elf)]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if not (sig.exists() and sig.stat().st_size > 0):
            raise SimulationError(f"QEMU produced no signature: {elf.name}")
```

Then inject it:

```python
runner = ComplianceRunner(cfg)
runner.dut_backend = QEMUBackend()
report = runner.run()
```

### Adding tests from a custom test suite

```python
cfg = RunConfig(
    isa            = "RV32IM",
    arch_test_repo = Path("/path/to/my-riscv-tests"),  # v2 or v3 layout
)
```

The test discovery supports both v3 (`riscv-test-suite/rv32i_m/{I,M}/src/*.S`) and v2 (`arch-test/{RV32I,RV32M}/src/*.S`) layouts automatically.

### Calling from Python

```python
from run_compliance import run_compliance_for_ava

result = run_compliance_for_ava(
    isa       = "RV32IM",
    spike_bin = "spike",
    dut_sim   = "run_rtl_adapter.py",
    out_dir   = "results/",
    workers   = 8,
    timeout   = 60,
)

print(result["compliance_pass"])       # True / False
print(result["compliance_pass_pct"])   # 100.0
print(result["compliance_report"])     # "results/compliance_report.json"
```

---

## 18. Where Agent E Sits in the AVA Pipeline

```
CI/CD trigger (git push / nightly)
        │
        ▼
   Orchestrator reads run_manifest.json
        │
        ├──► Agent A  ← (one-time) spec & interface contracts
        │
        ├──► Agent B  ── runs RTL simulation → rtl.commitlog.jsonl
        │                                    → coverage.dat
        │                                    → signature.hex   ┐
        │                                                       │
        ├──► Agent E ◄─────────────────────────────────────────┘
        │    run_compliance.py --manifest manifest.json
        │    ├─ reads: binary, isa, spikebin, compliance.suitepath
        │    ├─ invokes: run_rtl_adapter.py → Agent B for each test ELF
        │    ├─ compares: golden (Spike) vs DUT signatures
        │    └─ writes: phases.compliance, compliance.result, status
        │
        │    IF status == "failed":
        │        → Block: do not proceed to coverage or red-team
        │        → Report: HTML + JUnit to CI dashboard
        │
        │    IF status == "passed":
        │        │
        ├──► Agent C  ← ISS golden backend (Spike/Sail commit logs)
        ├──► Agent D  ← Commit-log comparator (PC/reg/CSR diff)
        ├──► Agent F  ← Coverage pipeline (line/branch/toggle/functional)
        ├──► Agent G  ← Test generation (constrained-random RV32IM)
        └──► Agent H  ← Phase-2 red team (adversarial sequences)
```

**Agent E is a gate, not just a reporter.** The orchestrator checks `status: "passed"` before enabling coverage and red-team agents. Spending CPU time on coverage of a CPU that computes wrong answers is wasted work.

---

## 19. Troubleshooting

### `Spike not found`

```
TOOL ERROR: Spike ISS not found: 'spike'. Install from https://github.com/riscv-software-src/riscv-isa-sim
```

Build and install Spike, or provide its path:
```bash
python run_compliance.py --spike /opt/riscv/bin/spike
```

### `No RISC-V GCC toolchain found`

```
TOOL ERROR: No RISC-V GCC toolchain found. Expected one of: riscv32-unknown-elf-gcc, ...
```

Install the toolchain:
```bash
# Ubuntu
sudo apt install gcc-riscv64-unknown-elf

# or build from source
git clone https://github.com/riscv-collab/riscv-gnu-toolchain
```

### `Empty golden signature`

```
ERROR: Spike golden run produced no signature for ADD-01.elf
```

Spike's `--signature` mode requires the `tohost` symbol in the ELF. Verify the linker script has a `.tohost` section and the test code calls `RVTEST_PASS` (not `ecall`).

Check ELF symbols:
```bash
riscv32-unknown-elf-nm build/ADD-01.elf | grep -E "tohost|begin_sig|end_sig"
```

### `Build failed: _zicsr not found`

Older GCC versions don't know `rv32i_zicsr`. Agent E automatically falls back:
```bash
# If fallback also fails, force older march:
python run_compliance.py --isa RV32IM  # uses rv32im without _zicsr suffix
```

### DUT signature differs from golden

```
FAIL  DIV-01  Sig mismatch at word[2]: golden=0xffffffff dut=0x00000000
```

This is a real CPU bug. Word index 2 in DIV-01 is the div-by-zero result. The RISC-V spec (§M.2) mandates `-1` (0xFFFFFFFF); your DUT returns `0`. Check your division unit's handling of the divisor=0 case.

### Manifest mode: `status` stays `"error"`

Agent E caught an unexpected exception. Check `compliance/compliance.log` in the `rundir` for the full traceback. Common causes:
- `rundir` is not writable
- `binary` path in manifest does not exist
- Agent B's `run_rtl.py` is not on the `AGENT_B_RTL` path

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **Signature** | A contiguous block of 32-bit memory words written by a test program, used as the comparison unit between golden and DUT |
| **Golden ISS** | The trusted Instruction Set Simulator (Spike) that defines "correct" behaviour per the RISC-V specification |
| **DUT** | Device Under Test — the RTL simulation, FPGA bitstream, or physical chip being verified |
| **HTIF** | Host-Target Interface — the mechanism Spike uses to communicate with the host; writing `1` to `tohost` signals successful completion |
| **RISCOF** | RISC-V Compatibility Framework — the official compliance test methodology that Agent E implements |
| **riscv-arch-test** | The official repository of architectural compliance tests for RISC-V |
| **Manifest** | A `run_manifest.json` file read and written by AVA agents to pass state between pipeline phases |
| **mismatch_word** | The 0-based index of the first differing 32-bit word in the signature; reported in `compliance.result.failedlist` |
| **AVA** | Automated Verification Architecture — the multi-agent RISC-V verification platform of which Agent E is one component |
| **build_workers** | Number of parallel threads used for the CPU-bound GCC compilation phase |
| **run_workers** | Number of parallel threads used for the I/O-bound simulation phase; defaults to `2 × build_workers` |
| **EXIT_PASS/FAIL/CRASH/TOOL** | Standardised exit codes (0/1/2/3) indicating pass, sig mismatch, infrastructure failure, or missing tool |

# AVA — Autonomic Verification Agent
### State-of-the-Art RISC-V Hardware Verification Platform · v2.0.0

---

## Table of Contents

1. [What AVA Is](#1-what-ava-is)
2. [Why It Matters](#2-why-it-matters)
3. [How It Works — The Big Picture](#3-how-it-works--the-big-picture)
4. [Architecture Deep-Dive](#4-architecture-deep-dive)
   - 4.1 [The Orchestrator (AVA core)](#41-the-orchestrator-ava-core)
   - 4.2 [Agent B — RTL Runner (this repo)](#42-agent-b--rtl-runner-this-repo)
   - 4.3 [Agent C — ISS Golden Model](#43-agent-c--iss-golden-model)
   - 4.4 [Agent D — Differential Comparator](#44-agent-d--differential-comparator)
   - 4.5 [Agent E — Compliance Gate](#45-agent-e--compliance-gate)
   - 4.6 [Agent F — Coverage Director](#46-agent-f--coverage-director)
5. [The Commit Log — AVA's Truth Currency](#5-the-commit-log--avas-truth-currency)
6. [Technology Stack](#6-technology-stack)
7. [Project Layout](#7-project-layout)
8. [Prerequisites](#8-prerequisites)
9. [Quick-Start — End-to-End Example](#9-quick-start--end-to-end-example) 
10. [CLI Reference](#10-cli-reference)
11. [The Manifest System](#11-the-manifest-system)
12. [Coverage Pipeline](#12-coverage-pipeline)
13. [RISCOF Compliance Integration](#13-riscof-compliance-integration)
14. [Connecting Your Own RTL](#14-connecting-your-own-rtl)
15. [Performance Notes](#15-performance-notes)
16. [Methodology — Why This Approach Wins](#16-methodology--why-this-approach-wins)
17. [Known Limitations & Roadmap](#17-known-limitations--roadmap)

---

## 1. What AVA Is

AVA is a **multi-agent hardware verification platform** purpose-built for RISC-V processors. It automates the full verification workflow that a team of hardware engineers would otherwise run manually:

- It simulates your RTL design cycle-accurately through **Verilator**
- It runs the same program through a formally correct **reference ISS** (Spike)
- It compares every single retired instruction between the two — PC, register writes, CSR updates, memory accesses, and traps — at the cycle where it happens
- It measures **real coverage** (line, branch, toggle) and steers future tests toward the gaps
- It produces a **RISCOF-compatible signature** so compliance test results can be compared against the official golden reference
- It does all of this through a **shared manifest system** so any number of agents can run in parallel or sequence without stepping on each other

The result: a single command takes you from RTL source files to a full differential correctness verdict, a coverage report, and actionable bug reports — without any manual testbench writing.

---

## 2. Why It Matters

### The verification bottleneck in processor design

Writing and running processor verification today is expensive. Industry-standard flows (Synopsys VCS + UVM, Cadence Xcelium + Incisive) require:
- Manually written SystemVerilog testbenches that take weeks per subsystem
- A dedicated verification team maintaining coverage models
- Expensive EDA tool licences
- Days of regression runs per RTL change

Bugs that slip through cost enormously more to fix after tape-out than before it. The 1993 Intel Pentium FDIV bug cost $475 million in recalls. Modern out-of-order cores have millions of corner cases — branch predictor interactions, CSR side effects, trap/return sequences, multiply-divide edge cases — that hand-written tests simply cannot enumerate exhaustively.

### What AVA does differently

**Differential lock-step verification** is the gold standard used by ARM, IBM, and RISC-V core vendors internally. Rather than asking "does the DUT produce the right answer for this hand-crafted stimulus?", it asks: "does the DUT agree with a formally correct ISS on every single instruction of arbitrary programs?" This is fundamentally more powerful because:

- You don't need to know in advance what the right answer is — the ISS knows
- You can run any ELF binary (generated, random, real firmware) as a test
- A divergence anywhere in the instruction stream is an automatic bug report
- Coverage feedback drives the generator toward untested corner cases automatically

AVA makes this approach **accessible and automated** — you point it at RTL files and an ELF, and it does the rest.

---

## 3. How It Works — The Big Picture

```
┌─────────────────────────────────────────────────────────────────┐
│                        AVA Orchestrator                          │
│  1. Create run_manifest.json  (runid, seed, elf, dut, rundir)   │
│  2. Invoke agents in sequence / parallel                         │
│  3. Collect results, produce final verdict                       │
└──────────────┬──────────────────────────────┬───────────────────┘
               │                              │
               ▼                              ▼
   ┌─────────────────────┐        ┌─────────────────────┐
   │   Agent B           │        │   Agent C           │
   │   RTL Runner        │        │   ISS (Spike)       │
   │                     │        │                     │
   │  ELF ──► Verilator  │        │  ELF ──► Spike      │
   │  DUT sim (RTL)      │        │  Golden reference   │
   │         │           │        │         │           │
   │  rtl.commitlog.jsonl│        │  iss.commitlog.jsonl│
   │  coverage.dat       │        │                     │
   └─────────┬───────────┘        └──────────┬──────────┘
             │                               │
             └──────────────┬────────────────┘
                            ▼
               ┌─────────────────────┐
               │   Agent D           │
               │   Comparator        │
               │                     │
               │  RTL log vs ISS log │
               │  per-instruction:   │
               │  PC / regs / csrs / │
               │  memwrites / traps  │
               │         │           │
               │  bugs/*.json        │
               └─────────┬───────────┘
                         │
             ┌───────────┼───────────┐
             ▼           ▼           ▼
   ┌──────────────┐  ┌────────┐  ┌──────────────┐
   │  Agent E     │  │Agent F │  │  Agent G     │
   │  RISCOF      │  │Coverage│  │  Test Gen    │
   │  Compliance  │  │Director│  │  (feedback)  │
   └──────────────┘  └────────┘  └──────────────┘
```

Every agent reads from and writes to the **shared manifest** (`run_manifest.json`). This is the coordination backbone — no agent talks directly to another.

---

## 4. Architecture Deep-Dive

### 4.1 The Orchestrator (AVA core)

**File:** `ava.py`

The orchestrator (`class AVA`) is the top-level controller. It runs the full verification pipeline as an `async` task graph:

```
_semantic_analysis()        → SemanticMap
_generate_tb_suite()        → tb_suite dict (contains elf_path, rtl_files, runid)
_tandem_simulation()        → VerificationResult
_performance_cop()          → perf_analysis dict
_security_injector()        → security_report dict
coverage_director.adapt_cold_paths() → adaptive_stimulus list
```

The orchestrator optionally uses an **LLM** (via Ollama, default `qwen2.5-coder:32b`) to parse RTL source and extract a semantic map — signal names, pipeline stage names, CSR identifiers, interface types. If no LLM is available it falls back to regex-based rule extraction. Either way, the downstream pipeline is identical.

**Key design decision:** the orchestrator never simulates anything itself. It delegates entirely to Agent B (RTL) and Agent C (ISS) via subprocess calls, then assembles results from the artefacts they write.

---

### 4.2 Agent B — RTL Runner (this repo)

**Files:** `backends/run_rtl.py`, `backends/sim/sim_main.cpp`, `backends/sim/elf_loader.h`, `rtl/example_cpu/rv32im_core.v`, `rtl/example_cpu/cpu_top.v`

Agent B is the **Verilator-based RTL simulation backend**. This is the most technically complex component. It has three layers:

#### Layer 1: `run_rtl.py` — Python orchestration layer

The Python CLI handles everything outside the simulation itself:
- Parses the orchestrator manifest (`--manifest` mode) or CLI arguments (standalone mode)
- Invokes Verilator to compile the RTL + C++ harness into a binary
- Runs that binary with the correct arguments
- Parses the resulting `coverage.dat` file into human-readable ratios
- Atomically patches the manifest with `phases.build`, `phases.rtl`, and `outputs.*`

Manifest patching is atomic: it writes to a `.tmp` file then calls `os.replace()`, which is a single syscall on POSIX — no other agent can read a half-written manifest.

#### Layer 2: `sim_main.cpp` — C++ Verilator harness

This is the simulation runtime. It runs inside the Verilator-generated simulator binary and:

**Memory model:** A flat 64 MB byte array starting at `0x80000000`. The ELF loader (`elf_loader.h`) walks all `PT_LOAD` segments, zero-fills BSS, and copies file content into the array. The harness drives the DUT's memory bus: on each `mem_req_valid` pulse it either writes to the array (store) or reads from it (load) and drives `mem_resp_rdata` combinationally.

**Commit monitor:** Every cycle, after driving the memory response, the harness samples `commit_valid`. When it is high, it reads the 13 commit signals (`commit_pc`, `commit_instr`, `commit_rd_we`, `commit_rd_addr`, `commit_rd_data`, `commit_priv_mode`, `commit_trap_valid`, `commit_trap_cause`, `commit_trap_epc`, `commit_trap_tvec`, `commit_trap_tval`, `commit_is_mret`) and serialises them into a v2.0.0-schema JSON record, written to the commit log.

**Memory read/write tracking:** The harness records the pending memory transaction (`MemTxn`) on each clock cycle and associates it with the next `commit_valid` pulse — distinguishing data accesses from instruction fetches by comparing the request address to `commit_pc`. This produces the `memreads` and `memwrites` arrays in the commit log.

**Crash-safe flushing:** `--flush-every N` calls `commit_f.flush()` every N instructions. Without this, a Verilator crash or OOM kill would lose the entire commit log buffer. With it, at most N records are lost.

**RISCOF signature:** At teardown, `Memory::dump_signature()` reads `mem[sig_begin..sig_end)` and writes one 8-digit hex word per line — the exact format RISCOF's `compare_signature()` expects.

**FST tracing:** When `--trace` is given, Verilator is built with `--trace-fst` and the harness uses `VerilatedFstC`. FST (Fast Signal Trace) is Verilator's native compressed format — 5–20× smaller than VCD for typical designs, and natively supported by GTKWave.

#### Layer 3: `rv32im_core.v` — Example DUT

A complete, synthesisable, single-cycle RV32IM processor provided as the default DUT. It implements:
- All 37 RV32I base instructions
- All 8 M-extension instructions (MUL, MULH, MULHSU, MULHU, DIV, DIVU, REM, REMU) with correct divide-by-zero and signed-overflow semantics per spec
- Machine-mode CSR file (mstatus, misa, mie, mtvec, mscratch, mepc, mcause, mtval, mcycle, minstret)
- ECALL, EBREAK, MRET
- Full AVA v2.0.0 commit interface including `commit_trap_epc` and `commit_trap_tvec`

**FSM optimisation (v2.0.0):** In v1 the FSM had 6 states including a separate `S_WRITEBACK`. In v2, ALU instructions (LUI, AUIPC, JAL, JALR, BRANCH, OP-IMM, OP-REG, FENCE, CSR) compute the result, write the register file, and emit the commit record all within `S_EXECUTE` — eliminating one clock cycle per ALU instruction. `S_WRITEBACK` is replaced by `S_LOADWB` which is used only for load instructions (which need to wait for memory data). This recovers approximately 20% throughput for ALU-heavy verification workloads (fewer cycles = faster simulation = more instructions verified per second).

---

### 4.3 Agent C — ISS Golden Model

**File:** `ava.py` → `SpikeISS._simulate_iss()`

Agent C runs the same ELF under **Spike**, the official RISC-V reference ISS developed alongside the ISA specification. Spike's output is parsed line-by-line into the same v2.0.0 commit log schema as Agent B, so Agent D can compare them field-by-field.

Spike is run with `--isa=rv32im --log-commits` (or `-l` as fallback for older versions). The commit log parser handles both Spike log variants using regex.

The critical property of Spike: it is the **oracle**. Its behaviour is, by definition, what the RISC-V spec says the processor should do. Any deviation between RTL and Spike is a real bug.

---

### 4.4 Agent D — Differential Comparator

**File:** `ava.py` → `SpikeISS._compare_results()`

Agent D receives the two commit logs (RTL and ISS) and walks them instruction by instruction. For each `seq` index it checks:

1. **PC** — if PCs diverge, all further comparison is invalid (different instruction streams); it stops and reports a `pc` mismatch
2. **Register writes** (`regs`) — checks every register written by both sides
3. **CSR writes** (`csrs`) — checks every CSR updated by both sides
4. **Memory writes** (`memwrites`) — checked if both sides emitted them
5. **Memory reads** (`memreads`) — checked if both sides emitted them
6. **Trap presence** (`trap`) — mismatch if one side trapped and the other didn't

Each mismatch is classified by type (`pc`, `reg`, `csr`, `mem`, `trap`, `count`) and severity (`critical`, `high`). The first divergence sequence number is recorded so the developer knows exactly where to look in the waveform.

---

### 4.5 Agent E — Compliance Gate

Agent E runs the official [riscv-arch-test](https://github.com/riscv-non-isa/riscv-arch-test) suite against the DUT. Agent B's `--sig-out` provides a direct Verilator path: instead of re-running the simulation through Spike for a signature, Agent B dumps the signature region of RAM at teardown. Agent E compares this against the golden reference signatures distributed with the test suite.

This provides a fast pass/fail gate before the longer differential verification runs.

---

### 4.6 Agent F — Coverage Director

**File:** `ava.py` → `CoverageDirector`

Agent F reads `coverage.dat` from the run directory (the convention is `<rundir>/coverage.dat` — this is why the filename was standardised in v2.0.0) and identifies coverage gaps. It maps each gap to a targeted instruction category:

| Coverage gap | Focused instruction set |
|---|---|
| Branch coverage low | BEQ, BNE, BLT, BGE, BLTU, BGEU, JAL, JALR |
| Line coverage low | LW, SW, LB, SB, LH, SH, LBU, LHU |
| Toggle coverage low | SLLI, SRLI, SRAI, AND, OR, XOR |
| Functional coverage low | MUL, MULH, DIV, REM, DIVU, REMU, ECALL, MRET |

These constraint descriptors are passed back to Agent G (test generator) which produces a new ELF that biases its instruction mix toward the gap. The loop continues until all coverage goals are met or the iteration budget is exhausted.

---

## 5. The Commit Log — AVA's Truth Currency

Every record in `rtl.commitlog.jsonl` and `iss.commitlog.jsonl` conforms to `schemas/commitlog.schema.json` v2.0.0. A single retired instruction looks like this:

```jsonc
// Normal ALU instruction — ADD x10, x5, x3
{
  "schemaversion": "2.0.0",
  "runid":      "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "hart":       0,
  "seq":        42,
  "pc":         "0x80000108",
  "instr":      "0x003282b3",
  "instrwidth": 4,
  "priv":       "M",
  "src":        "rtl",
  "regs":       { "x10": "0x0000002a" },
  "csrs":       {},
  "fpregs":     null,
  "memwrites":  [],
  "memreads":   []
}
```

```jsonc
// Store instruction — SW x10, 0(x5)
{
  "schemaversion": "2.0.0",
  "runid":    "a1b2c3d4-...",
  "hart":     0,
  "seq":      55,
  "pc":       "0x80000140",
  "instr":    "0x00a2a023",
  "instrwidth": 4,
  "priv":     "M",
  "src":      "rtl",
  "regs":     {},
  "csrs":     {},
  "fpregs":   null,
  "memwrites": [{ "addr": "0x80001000", "data": "0x0000002a", "size": 4, "strb": "0xf" }],
  "memreads":  []
}
```

```jsonc
// ECALL — trap entry
{
  "schemaversion": "2.0.0",
  "runid":    "a1b2c3d4-...",
  "hart":     0,
  "seq":      87,
  "pc":       "0x80000200",
  "instr":    "0x00000073",
  "instrwidth": 4,
  "priv":     "M",
  "src":      "rtl",
  "regs":     {},
  "csrs":     {},
  "fpregs":   null,
  "memwrites": [],
  "memreads":  [],
  "trap": {
    "cause":       "0x0000000b",
    "epc":         "0x80000200",
    "tvec":        "0x80000004",
    "tval":        "0x00000000",
    "isinterrupt": false,
    "is_ret":      false
  }
}
```

**Why JSONL?** One record per line means any downstream tool can stream the log without loading it entirely into memory. A 10-million-instruction run produces roughly 2–3 GB of commit log — streaming is essential.

**Why the `runid` field?** The RTL and ISS logs of the same run share a `runid` UUID. Agent D can verify it is comparing the correct pair of logs even if the files are moved or renamed.

---

## 6. Technology Stack

| Component | Technology | Why |
|---|---|---|
| RTL simulation | **Verilator 4.034+** | Fastest open-source RTL simulator; C++ model; native coverage and FST tracing |
| Reference ISS | **Spike** (riscv-isa-sim) | Official RISC-V ISA reference; formally tied to the spec |
| Orchestrator language | **Python 3.10+** (asyncio) | Async agent coordination; subprocess management; JSON parsing |
| Commit log format | **JSONL** (JSON Lines) | Streaming-friendly; human-readable; schema-validatable |
| Schema | **JSON Schema draft-07** | Machine-checkable; shared single source of truth |
| Waveform format | **FST** (Fast Signal Trace) | Verilator native; 5–20× smaller than VCD; GTKWave compatible |
| Coverage | Verilator **--coverage** | Line + branch + expression + toggle; zero-overhead compile-time instrumentation |
| Compliance | **RISCOF / riscv-arch-test** | Official RISC-V International test suite |
| Optional LLM | **Ollama** (qwen2.5-coder:32b) | RTL semantic parsing; testbench generation |
| Toolchain | **riscv32-unknown-elf-gcc** | ELF assembly and linking for test programs |
| DUT language | **Verilog-2001** | Maximum synthesiser and simulator compatibility |
| Manifest coordination | **Atomic file rename** (`os.replace`) | Race-free manifest patching between parallel agents |

---

## 7. Project Layout

```
ava/
├── ava.py                          # Orchestrator + all agent coordination
│
├── backends/
│   ├── run_rtl.py                  # Agent B CLI — build, simulate, patch manifest
│   └── sim/
│       ├── sim_main.cpp            # Verilator C++ harness (memory, commit monitor)
│       └── elf_loader.h            # Header-only ELF32 loader (no libelf needed)
│
├── rtl/
│   └── example_cpu/
│       ├── rv32im_core.v           # Example DUT: full RV32IM core
│       └── cpu_top.v               # Top-level wrapper (Verilator --top)
│
├── schemas/
│   └── commitlog.schema.json       # v2.0.0 canonical schema — all agents import this
│
├── docs/
│   └── interfaces.md               # Agent contracts, manifest spec, field tables
│
└── tests/
    ├── add_loop.S                  # End-to-end example: add loop + M-extension
    └── link.ld                     # Bare-metal linker script (loads at 0x80000000)
```

---

## 8. Prerequisites

### Required

```bash
# Verilator (simulation + coverage)
sudo apt install verilator          # Ubuntu 22.04 gives ≥ 4.034
verilator --version

# RISC-V 32-bit bare-metal toolchain
sudo apt install gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf
# (riscv64-unknown-elf-gcc supports -march=rv32im -mabi=ilp32)

# Python 3.10+
python3 --version
```

### Optional

```bash
# Spike ISS (for differential verification)
# Build from source: https://github.com/riscv-software-src/riscv-isa-sim
spike --version

# GTKWave (waveform viewer for .fst files)
sudo apt install gtkwave

# Ollama (LLM for semantic analysis)
# https://ollama.ai
ollama pull qwen2.5-coder:32b

# RISCOF (compliance testing)
pip install riscof
```

---

## 9. Quick-Start — End-to-End Example

This walks through a complete run: compile test → RTL simulation → commit log inspection → coverage report.

### Step 1: Compile the bundled test program

```bash
riscv64-unknown-elf-gcc \
    -march=rv32im -mabi=ilp32 -nostdlib \
    -T tests/link.ld \
    tests/add_loop.S \
    -o tests/add_loop.elf

# Verify the ELF
riscv64-unknown-elf-objdump -d tests/add_loop.elf | head -50
```

The test program (`add_loop.S`) exercises:
- ADD loop (sum 1..10 = 55) — tests basic ALU and branching
- Store + load round-trip — tests memory path
- MUL, DIV, REM — tests M-extension
- MULH, MULHSU, MULHU — tests upper-word multiply variants
- Divide-by-zero (result must be `0xFFFFFFFF` per spec)
- Signed overflow: `INT_MIN / -1` (result must be `INT_MIN` per spec)
- Byte and halfword loads (`LB`, `LBU`, `LH`, `LHU`)
- ECALL halt

### Step 2: Run the RTL simulation (standalone mode)

```bash
python backends/run_rtl.py \
    --rtl rtl/example_cpu/rv32im_core.v \
          rtl/example_cpu/cpu_top.v \
    --top cpu_top \
    --elf tests/add_loop.elf \
    --seed 42 \
    --out  runs/run_42
```

Expected terminal output:

```
[ava] runid=f3a2b1c0  seed=42
[ava] elf=tests/add_loop.elf
[ava] out=/path/to/runs/run_42
[build] verilator --cc --exe --build --coverage --coverage-underscore \
        --assert -O2 --top-module cpu_top --Mdir runs/run_42/build ...
[build] Done in 8.3s — runs/run_42/build/Vcpu_top
[mem] ELF entry=0x80000000 base=0x80000000
[sim] runid=f3a2b1c0 max_insns=100000 flush_every=1000 seed=42
[sim] Done: retired=87 cycles=261 tohost=1

============================================================
  PASS  retired=87  cycles≈261
  cov line=74.1%  branch=63.2%  toggle=48.7%
============================================================
[ava] commit log  → runs/run_42/rtl.commitlog.jsonl
[ava] coverage    → runs/run_42/coverage.dat
[ava] cov report  → runs/run_42/coverage_report.json
[ava] manifest    → runs/run_42/run_manifest.json
```

### Step 3: Inspect the commit log

```bash
# Pretty-print the first 5 records
head -5 runs/run_42/rtl.commitlog.jsonl | python3 -m json.tool
```

```json
{
  "schemaversion": "2.0.0",
  "runid": "f3a2b1c0",
  "hart": 0,
  "seq": 0,
  "pc": "0x80000000",
  "instr": "0x00004137",
  "instrwidth": 4,
  "priv": "M",
  "src": "rtl",
  "regs": { "x2": "0x80400000" },
  "csrs": {},
  "fpregs": null,
  "memwrites": [],
  "memreads": []
}
```

```bash
# Find all instructions that wrote to x10
grep '"x10"' runs/run_42/rtl.commitlog.jsonl | python3 -m json.tool | head -20

# Find all trap records
python3 -c "
import json, sys
for line in open('runs/run_42/rtl.commitlog.jsonl'):
    r = json.loads(line)
    if 'trap' in r:
        print(json.dumps(r, indent=2))
"
```

### Step 4: Full orchestrator run (RTL + ISS + diff)

```python
# run_ava.py
import asyncio
from ava import AVA

async def main():
    ava = AVA(
        seed        = 42,
        rtl_files   = ["rtl/example_cpu/rv32im_core.v",
                        "rtl/example_cpu/cpu_top.v"],
        rtl_top     = "cpu_top",
        elf         = "tests/add_loop.elf",
        run_base_dir = "runs",
        enable_llm  = False,   # set True if Ollama is running
    )
    results = await ava.generate_suite(
        rtl_spec  = "rtl/example_cpu/rv32im_core.v",
        microarch = "in_order",
    )
    print(f"Status:  {results['status']}")
    print(f"Bugs:    {len(results['initial_results']['bugs'])}")
    print(f"Coverage: {results['initial_results']['coverage']}")

asyncio.run(main())
```

### Step 5: Enable waveform and RISCOF signature

```bash
python backends/run_rtl.py \
    --rtl rtl/example_cpu/rv32im_core.v rtl/example_cpu/cpu_top.v \
    --top cpu_top \
    --elf tests/add_loop.elf \
    --seed 42 \
    --out  runs/run_42_full \
    --trace \
    --sig-out  runs/run_42_full/signature.hex \
    --sig-begin 0x80001000 \
    --sig-end   0x80001040

# Open waveform
gtkwave runs/run_42_full/rtl.fst &

# Inspect signature
cat runs/run_42_full/signature.hex
# 00000037
# 00000055
# 0000006e
# ...
```

### Step 6: Run with orchestrator manifest

```bash
# Orchestrator creates manifest
cat > runs/run_43/run_manifest.json << 'EOF'
{
  "schemaversion": "2.0.0",
  "runid":   "deadbeef-0000-0000-0000-000000000043",
  "seed":    43,
  "binary":  "tests/add_loop.elf",
  "dut":     "cpu_top",
  "rundir":  "runs/run_43",
  "xlen":    32,
  "isa":     "rv32im",
  "phases":  {},
  "outputs": {}
}
EOF

mkdir -p runs/run_43

# Agent B consumes it
python backends/run_rtl.py \
    --manifest runs/run_43/run_manifest.json \
    --rtl rtl/example_cpu/rv32im_core.v rtl/example_cpu/cpu_top.v

# Check what Agent B wrote back
python3 -c "import json; d=json.load(open('runs/run_43/run_manifest.json')); print(json.dumps(d['phases'], indent=2)); print(json.dumps(d['outputs'], indent=2))"
```

---

## 10. CLI Reference

### `backends/run_rtl.py`

```
python backends/run_rtl.py [OPTIONS]

MANIFEST MODE (recommended):
  --manifest PATH       Orchestrator manifest; reads seed/binary/dut/rundir,
                        writes phases.* and outputs.* atomically.

STANDALONE MODE:
  --rtl FILE [FILE ...]  Verilog/SV source files
  --top MODULE           Verilator top module name
  --elf FILE             ELF binary to simulate
  --seed N               RNG seed (default: 42)
  --out DIR              Output run directory

SIMULATION OPTIONS:
  --max-insns N          Retire limit (default: 100000)
  --flush-every N        Flush commit log every N instructions (default: 1000)
  --mem-base HEX         RAM base address (default: 0x80000000)
  --mem-size BYTES       RAM size (default: 67108864 = 64 MiB)

TRACING:
  --trace                Enable FST waveform (output: <rundir>/rtl.fst)

RISCOF SIGNATURE:
  --sig-out PATH         Write RISCOF hex signature to PATH
  --sig-begin HEX        Signature region start (default: 0x80002000)
  --sig-end HEX          Signature region end   (default: 0x80002040)

BUILD:
  --jobs N               Parallel make jobs (default: 4)
  --verilator PATH       Verilator binary (default: verilator)
  --rebuild              Force Verilator rebuild

OUTPUT:
  --verbose              Print every retire record to stderr
```

---

## 11. The Manifest System

The manifest (`run_manifest.json`) is the **coordination backbone** of the multi-agent platform. No agent calls another directly — they all read from and write to the manifest.

```
Orchestrator creates manifest
         │
         ├──► Agent B reads: runid, seed, binary, dut, rundir
         │          writes:  phases.build, phases.rtl
         │                   outputs.rtlcommitlog, outputs.coverageraw,
         │                   outputs.waveform, outputs.signature, outputs.totalcycles
         │
         ├──► Agent C reads: runid, seed, binary
         │          writes:  phases.iss
         │                   outputs.isscommitlog
         │
         ├──► Agent D reads: outputs.rtlcommitlog, outputs.isscommitlog
         │          writes:  phases.compare
         │                   outputs.bugreports
         │
         └──► Agent F reads: outputs.coverageraw (= coverage.dat)
                    writes:  phases.coverage
                             outputs.coveragereport
```

**Atomicity:** All manifest writes go through `patch_manifest()` which does `write → tmp file → os.replace()`. On Linux/macOS, `os.replace()` is a single atomic `rename(2)` syscall. An agent reading the manifest will never see a partially-written file.

**Idempotency:** If an agent is re-run (e.g., after a crash), `patch_manifest()` deep-merges — it only updates the keys the agent owns. Other agents' data is preserved.

---

## 12. Coverage Pipeline

### What Verilator instruments

When built with `--coverage --coverage-underscore`, Verilator inserts counters at:
- Every RTL **line** (statement coverage)
- Every **branch** of every `if`, `case`, and ternary (branch coverage)
- Every signal **toggle** (0→1 and 1→0 transitions — toggle coverage)

These counters are written to `coverage.dat` at simulation teardown via `ctx->coveragep()->write()`.

### How AVA parses coverage

`run_rtl.py::parse_coverage()` reads the Verilator `.dat` file line by line. Each record starts with `C` and contains a type token (`'line'`, `'b0'`/`'b1'`, `'tgl0'`/`'tgl1'`) and a counter value. The parser buckets these into hit/total counts per type and computes ratios.

### Coverage report format

`coverage_report.json`:
```json
{
  "line":       0.741,
  "branch":     0.632,
  "toggle":     0.487,
  "functional": 0.0,
  "raw": {
    "line":   { "hit": 197, "total": 266 },
    "branch": { "hit": 120, "total": 190 },
    "toggle": { "hit": 380, "total": 780 }
  }
}
```

Values are ratios `0.0..1.0`. The orchestrator multiplies by 100 for display. `functional` is populated by Agent F's higher-level functional coverage harness (not yet in this release).

### Inspecting coverage with Verilator tools

```bash
# Text report per-file
verilator_coverage runs/run_42/coverage.dat

# Annotated HTML report
verilator_coverage --annotate runs/run_42/annotated_cov runs/run_42/coverage.dat
# open runs/run_42/annotated_cov/rv32im_core.v
```

---

## 13. RISCOF Compliance Integration

Agent B provides a direct Verilator path to RISCOF compliance testing, eliminating the need to run Spike separately for signature generation.

### How it works

1. Compile a RISCOF test ELF (e.g., from `rv32i_m/M/src/mul-01.S`)
2. Run Agent B with `--sig-out`, `--sig-begin`, `--sig-end` pointing to the test's signature region
3. Agent B runs the DUT and dumps `mem[sig_begin..sig_end)` as hex words
4. Agent E compares the output against the golden reference from riscv-arch-test

### Example

```bash
# Compile a RISCOF compliance test
riscv64-unknown-elf-gcc -march=rv32im -mabi=ilp32 -nostdlib \
    -T riscv-arch-test/riscv-test-env/p/link.ld \
    riscv-arch-test/riscv-test-suite/rv32i_m/M/src/mul-01.S \
    -o tests/mul-01.elf

# Run with signature dump
python backends/run_rtl.py \
    --rtl rtl/example_cpu/rv32im_core.v rtl/example_cpu/cpu_top.v \
    --top cpu_top \
    --elf tests/mul-01.elf \
    --seed 0 \
    --out  runs/compliance_mul01 \
    --sig-out   runs/compliance_mul01/signature.hex \
    --sig-begin 0x80002000 \
    --sig-end   0x80002100

# Compare against golden reference
diff runs/compliance_mul01/signature.hex \
     riscv-arch-test/riscv-test-suite/rv32i_m/M/references/mul-01.reference_output
# (no output = pass)
```

### Signature format

`signature.hex` contains one 32-bit word per line as 8 lowercase hex digits, no `0x` prefix:

```
00000000
000000c8
ffffffff
80000000
00000000
00000000
```

This is the exact format RISCOF's Python `compare_signature()` function reads.

---

## 14. Connecting Your Own RTL

To verify your own RV32IM (or any RISC-V) processor, add the AVA commit-monitor interface to your DUT's top-level module.

### Required ports

```verilog
// Add these outputs to your top-level module
output wire        commit_valid,       // pulse high for one cycle per retire
output wire [31:0] commit_pc,          // PC of retiring instruction
output wire [31:0] commit_instr,       // instruction word
output wire [4:0]  commit_rd_addr,     // dest register (0 = none)
output wire [31:0] commit_rd_data,     // value written to rd
output wire        commit_rd_we,       // 1 if rd is written
output wire [1:0]  commit_priv_mode,   // 2'b11=M  2'b01=S  2'b00=U
output wire        commit_trap_valid,  // 1 if this retire caused/was a trap
output wire [31:0] commit_trap_cause,  // mcause
output wire [31:0] commit_trap_epc,    // mepc (PC saved on trap)
output wire [31:0] commit_trap_tvec,   // mtvec (handler jumped to)
output wire [31:0] commit_trap_tval,   // mtval
output wire        commit_is_mret,     // 1 for mret/sret/uret
```

And the memory bus interface:

```verilog
output wire        mem_req_valid,
output wire        mem_req_we,
output wire [31:0] mem_req_addr,
output wire [31:0] mem_req_wdata,
output wire [3:0]  mem_req_wstrb,
input  wire [31:0] mem_resp_rdata,
input  wire        mem_resp_ready,
```

### Then run

```bash
python backends/run_rtl.py \
    --rtl  src/my_cpu_top.v src/alu.v src/regfile.v src/decoder.v \
    --top  my_cpu_top \
    --elf  tests/add_loop.elf \
    --seed 42 \
    --out  runs/my_cpu_run_01
```

The harness is completely DUT-agnostic — it only reads the commit signals at the top level. Your internal microarchitecture (pipelined, out-of-order, superscalar) does not matter as long as the commit interface presents one retired instruction per cycle on a `commit_valid` pulse.

---

## 15. Performance Notes

### Simulation speed

| Configuration | Typical throughput |
|---|---|
| Verilator + coverage + no trace | ~20–50 million cycles/sec |
| Verilator + coverage + FST trace | ~5–15 million cycles/sec |
| Verilator + coverage + VCD trace | ~2–5 million cycles/sec |

The bundled `rv32im_core.v` retires one instruction every 3 cycles on average (ALU) to 5 cycles (load). At 30 MHz simulated clock, 100,000 instructions run in under 0.1 seconds.

### Commit log size

| Instructions | Approx log size |
|---|---|
| 100,000 | ~30 MB |
| 1,000,000 | ~300 MB |
| 10,000,000 | ~3 GB |

For large runs, consider `--max-insns 1000000` and streaming the log rather than loading it entirely.

### FST vs VCD

FST (Fast Signal Trace) is Verilator's native waveform format. It uses LZ4 compression internally and only records signal changes (not values at every clock). For a typical RV32IM core with ~200 signals:

- VCD: ~500 MB for 1M cycles
- FST: ~25 MB for 1M cycles (20× smaller)
- GTKWave reads both formats identically

### Build caching

The Verilator build step (compiling RTL → C++ → binary) takes 5–30 seconds depending on design size. Agent B caches the binary under `<rundir>/build/V<top>` and skips the build on subsequent runs with the same RTL files. Use `--rebuild` to force a fresh build when RTL changes.

---

## 16. Methodology — Why This Approach Wins

### Differential verification vs. assertion-based verification

Traditional verification writes **assertions**: "the output should equal X for input Y". This requires knowing the correct answer in advance, and scales poorly — you need one assertion per corner case.

Differential verification asks: "does the DUT agree with the oracle for all inputs?" The oracle (Spike) handles correctness; you only need to generate interesting inputs. This scales to:
- Randomly generated instruction streams (millions of unique programs)
- Real firmware binaries (if the program runs on Spike it can run on AVA)
- Specifically crafted corner-case sequences (from Agent G)

### Why Spike as the oracle?

Spike is not just "another simulator". It is:
- Co-developed with the RISC-V ISA specification
- The reference implementation used to validate the spec itself
- Used by dozens of commercial RISC-V implementors (SiFive, Western Digital, ETH Zurich PULP)
- Continuously tested against the riscv-arch-test compliance suite

When Spike and your RTL disagree, Spike is right. Always.

### Coverage-directed generation vs. random testing

Pure random testing is inefficient: most random instruction streams exercise the same common paths (simple ALU ops, forward branches) and miss rare but critical paths (taken backward branches with forwarding hazards, CSR interactions during traps, multiply-divide pipeline interlocks).

Coverage-directed generation uses the real coverage data from previous runs to steer the next run toward the uncovered paths. Agent F's `adapt_cold_paths()` maps coverage gaps to instruction categories — if branch coverage is low, the next generator run will have higher branch density. This is a closed-loop feedback system.

### Why the commit log format matters

The commit log is the interface between Agent B (RTL), Agent C (ISS), and Agent D (comparator). Getting the format wrong means:
- Agent D comparing misaligned records (different instruction numbering)
- Missing trap fields causing false negatives on trap-related bugs
- Field name mismatches between agents causing silent failures

The v2.0.0 schema (`schemas/commitlog.schema.json`) is the single source of truth. It is machine-checkable with any JSON Schema validator. The mandatory fields (`schemaversion`, `runid`, `hart`, `seq`, `priv`, `instrwidth`, `fpregs`, `src`) ensure that every consumer can verify it is reading a compatible record before processing it.

### The manifest pattern

Rather than having agents call each other through RPC or message queues, AVA uses a **manifest file** as the coordination primitive. This design choice has several advantages:
- **Debuggability**: you can `cat run_manifest.json` at any point and see exactly what every agent did and found
- **Restartability**: if an agent fails, the orchestrator can restart just that agent — the manifest preserves the outputs of successful agents
- **Parallelism**: agents that don't depend on each other's outputs can run simultaneously — they each write to different keys in the manifest
- **Simplicity**: no message broker, no RPC framework, no network configuration required

---

## 17. Known Limitations & Roadmap

### Current limitations

- **Single hart only**: the memory model and commit interface support one hardware thread. Multi-hart and multi-core verification requires extending the manifest and commit log schema.
- **Machine mode only**: the bundled `rv32im_core.v` implements only M-mode. S-mode and U-mode privilege transitions, PMP, and virtual memory are not yet in the example DUT.
- **No RVC**: compressed instructions (C extension) require `instrwidth: 2` handling in the comparator and 16-bit instruction parsing in the ELF loader.
- **No floating point**: the `fpregs` field exists in the schema but is always `null`. F/D extensions require additional commit signals.
- **Functional coverage**: `functional` in `coverage_report.json` is always `0.0`. Functional coverage (e.g., "was every M-extension opcode exercised?") requires a separate UVM/SystemVerilog coverage model or a Python post-processor on the commit log.

### Planned (Phase 2)

- **Agent G — Test Generator**: seedable constrained-random RV32IM instruction stream generator producing ELF binaries directly, with configurable branch density, memory density, and M-extension corner-case sequences.
- **Agent E — Full RISCOF integration**: automated RISCOF plugin flow for all `rv32i_m/I` and `rv32i_m/M` test suites.
- **Red-team adversarial agents**: agents that consume real traces and generate worst-case sequences for coherence protocols, speculative execution paths, and CSR side-effect chains.
- **Out-of-order DUT support**: the commit interface already supports out-of-order retirement (the DUT can retire instructions in any order as long as each `commit_valid` pulse is correct). The comparator needs an extension to handle in-flight instruction windows.
- **S-mode and U-mode**: privilege transition sequences, trap delegation, and PMP verification.


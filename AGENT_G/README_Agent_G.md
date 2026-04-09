# rv32im_testgen — Agent G · RV32IM Adaptive Test Generator

> **Part of the AVA (Adaptive Verification Architecture) platform.**
> Agent G generates, evolves, and delivers test binaries to all other AVA agents.
> Version 3.0.0 · Python 3.8+ · ISA: RV32IM

---

## Table of Contents

1. [What It Does](#1-what-it-does)
2. [Why It Matters](#2-why-it-matters)
3. [Where It Lives in the AVA Platform](#3-where-it-lives-in-the-ava-platform)
4. [Architecture Overview](#4-architecture-overview)
5. [Technology and Methodology](#5-technology-and-methodology)
6. [File Reference](#6-file-reference)
7. [Quick Start](#7-quick-start)
8. [Usage — Baseline Mode](#8-usage--baseline-mode)
9. [Usage — Manifest Mode (AVA Feedback Loop)](#9-usage--manifest-mode-ava-feedback-loop)
10. [Usage — Python API](#10-usage--python-api)
11. [Directed Test Catalogue](#11-directed-test-catalogue)
12. [Genetic Engine Deep Dive](#12-genetic-engine-deep-dive)
13. [Assembly Harness Specification](#13-assembly-harness-specification)
14. [Output Schema](#14-output-schema)
15. [Integration Guide for Other Agents](#15-integration-guide-for-other-agents)
16. [Configuration Reference](#16-configuration-reference)
17. [Adding New Tests](#17-adding-new-tests)
18. [Troubleshooting](#18-troubleshooting)

---

## 1. What It Does

`rv32im_testgen` produces bare-metal RISC-V test programs that exercise the
**M-extension** (integer multiply/divide/remainder) of an RV32IM processor
under verification. It operates in two modes:

| Mode | Input | Output |
|------|-------|--------|
| **Baseline** | CLI flags (seed, count) | 20 directed + 50 random `.S` + `.elf` files |
| **Manifest** | AVA `manifest.json` from Agents D and F | 50 evolved `.elf` files targeting cold coverage paths |

Every output file is a **complete, self-contained bare-metal program** that:

- Boots at `0x80000000` (standard Spike/QEMU M-mode entry)
- Sets up a trap vector, stack pointer, and safe memory region
- Executes the test instructions
- Performs self-checking register assertions (directed tests only)
- Reports PASS or FAIL via the HTIF `tohost` protocol

---

## 2. Why It Matters

Hardware verification of an arithmetic unit is fundamentally hard. The M
extension contains eight opcodes (`MUL`, `MULH`, `MULHSU`, `MULHU`, `DIV`,
`DIVU`, `REM`, `REMU`), each with its own corner cases precisely defined by the
RISC-V specification (§7.1–7.2). A bug anywhere — wrong sign, wrong truncation
direction, missing divide-by-zero handling, incorrect INT_MIN overflow
behaviour — will silently corrupt computation and is extraordinarily difficult
to discover through random testing alone.

**Static testing is not enough.** A fixed test suite covers known corners.
Unknown corners — RTL paths that no existing test reaches — remain dark.
The AVA platform discovers these through coverage feedback (Agent F) and bug
hypotheses (Agent D), then Agent G *evolves new tests specifically aimed at
those dark paths*.

The result is a verification loop that tightens automatically:

```
Simulation -> Coverage -> Cold Paths -> Evolved Tests -> Simulation -> ...
```

Each iteration, the test population becomes more precisely targeted at the
remaining uncovered RTL. This is analogous to how fuzzing with coverage
guidance (AFL, LibFuzzer) outperforms purely random fuzzing — but applied to
hardware verification at the instruction-sequence level.

---

## 3. Where It Lives in the AVA Platform

```
+-------------------------------------------------------------------+
|                        AVA PLATFORM                               |
|                                                                   |
|  +---------+   ELFs    +---------+  commit   +---------+         |
|  | Agent G | --------> | Agent B |   logs    | Agent C |         |
|  | (tests) |           |  (RTL)  | --------> | (Spike) |         |
|  +---------+           +---------+           +---------+         |
|       ^                                            |              |
|       | evolved                                    | compare      |
|       | ELFs                                       v              |
|  +---------+           +---------+           +---------+         |
|  | manifest| <-------- | Agent F | <-------- | Agent D |         |
|  |  .json  | cold_paths|(coverage)| mismatches| (bugs)  |         |
|  +---------+           +---------+           +---------+         |
|                                                    |              |
|                                             +---------+           |
|                                             | Agent E |           |
|                                             |(complian)|           |
|                                             +---------+           |
+-------------------------------------------------------------------+
```

**Agent G's contract with each neighbour:**

| Agent | G produces for them | G consumes from them |
|-------|---------------------|----------------------|
| **B** (RTL harness) | `.elf` files at `test_binaries/` | nothing |
| **C** (Spike ISS) | `.elf` files | nothing |
| **D** (Bug hunter) | `.elf` files | `bugreport.json` — `hypotheses[]` |
| **E** (Compliance) | `.elf` files | nothing |
| **F** (Coverage) | `.elf` files | `coveragesummary.json` — `cold_paths[]` |

---

## 4. Architecture Overview

```
rv32im_testgen/
├── directed_tests.py   -- 48 hand-crafted edge-case tests with verified math
├── random_gen.py       -- Seedable RV32IM instruction stream generator
├── asm_builder.py      -- Bare-metal .S renderer + GCC assembler wrapper
├── genetic_engine.py   -- Generational GA: evolves populations against constraints
├── generate_tests.py   -- CLI entry point (baseline + manifest mode)
└── __init__.py         -- Public API surface
```

### Data flow — baseline mode

```
directed_tests.py
      |  DirectedTest objects
      v
asm_builder.build_directed_asm()  ->  .S source
      |
      v
asm_builder.write_test()  ->  .S + .meta.json + .elf

random_gen.RV32IMRandomGenerator.generate()  ->  asm lines
      |
asm_builder.build_random_asm()  ->  .S source
      |
asm_builder.write_test()  ->  .S + .meta.json + .elf
```

### Data flow — manifest / evolution mode

```
manifest.json ---> coveragesummary.json  (Agent F cold_paths)
              \--> bugreport.json         (Agent D hypotheses)
                         |
                         v  unified constraints list
              GeneticEngine.evolve()
                         |
          +--------------+-----------------+
          |                                |
          v                                v
  seed population                   breed new children
  (20 directed +                    (crossover + mutation
   80 biased random)                 targeted at constraints)
          |                                |
          +--------------+-----------------+
                         |  fitness ranking (10 generations)
                         v
              top-50 Individuals
                         |
          asm_builder.build_evolved_asm()  ->  .S
                         |
          asm_builder.write_test(evolution_meta=...)  ->  .S + .meta.json + .elf
                         |
          manifest["phases"]["generator"]  updated
```

---

## 5. Technology and Methodology

### 5.1 RISC-V M-Extension Semantics

All expected register values in directed tests are **computed by code**, not
typed by hand. The module implements the exact RISC-V spec semantics (§7.1–7.2,
Table 7.1) for all eight M-extension opcodes, including every spec-mandated
corner case:

| Opcode | Corner case | Spec-mandated result |
|--------|-------------|---------------------|
| `DIV`  | divisor = 0 | −1 (all-ones, 0xFFFFFFFF) |
| `DIV`  | INT_MIN ÷ (−1) | INT_MIN (overflow; no trap) |
| `DIVU` | divisor = 0 | UINT_MAX (0xFFFFFFFF) |
| `REM`  | divisor = 0 | dividend unchanged |
| `REM`  | INT_MIN % (−1) | 0 (overflow companion) |
| `REMU` | divisor = 0 | dividend unchanged |

**Sign convention:** `DIV`/`REM` use **truncation toward zero**, not floor
division. This is a common source of RTL bugs because Python's `//` operator
uses floor division. The test generator explicitly uses `int(a/b)` to match the
spec and catches this subtle distinction. For example, `−7 ÷ 2 = −3` (truncated
toward zero), not `−4` (floor). The directed test `div_truncate_toward_zero_negative`
catches exactly this class of error.

### 5.2 Generational Genetic Algorithm

The `GeneticEngine` implements a **steady-state generational GA** with these
operators:

#### Representation

Each *Individual* is a list of RISC-V assembly instruction strings
(`asm_lines`). A population of 100 such individuals is maintained.

#### Fitness Function

```
fitness(individual, constraints) =
    sum_over_constraints(
        1.0  if constraint.module appears in asm_lines or targets
      + 0.5  if reachability < 0.2  (cold-path bonus)
    )
  + 0.2 * count_of_unique_M_extension_opcodes_present
  - 0.1 * duplication_ratio * total_lines  (repetition penalty)
```

#### Selection

**Truncation selection with elitism**: the top 20% of individuals by fitness
are copied unchanged into the next generation. This guarantees the best
discovered sequences are never lost.

#### Crossover (60% of offspring)

**Single-point crossover**: given two elite parents P1 and P2, a split point
k is chosen uniformly from `[1, min(|P1|, |P2|) − 1]`. The child receives
`P1[0:k] + P2[k:]`. This splices instruction patterns from two high-fitness
parents, potentially combining a useful prefix from one with a useful suffix
from the other.

#### Mutation (applied to all offspring)

Three operators are chosen uniformly per mutation site:

- **Replace**: swap one instruction line for a new constraint-biased one
- **Insert**: add a new instruction at a random position
- **Delete**: remove one instruction (minimum 1 line preserved)

The number of mutation sites per offspring is drawn from a geometric
distribution with mean `mutation_rate × len(asm_lines)` (default 15%). This
produces a heavy-tailed distribution — most mutations are small, but occasional
large restructurings maintain population diversity.

#### Constraint-Biased Instruction Generation

When mutating or building biased random sequences, instruction opcodes are
chosen from `_MODULE_OPS` — a lookup table mapping Agent F/D module keywords
to the relevant opcodes:

```python
_MODULE_OPS = {
    "mul":      ["mul", "mulh", "mulhsu", "mulhu"],
    "div":      ["div", "divu"],
    "overflow": ["mul", "mulh", "div", "rem"],
    "zero":     ["div", "divu", "rem", "remu"],
    "sign":     ["mul", "mulh", "mulhsu", "div", "rem"],
    ...
}
```

70% of biased sequences use targeted opcodes with boundary operand values
(`0`, `-1`, `INT_MIN`, `INT_MAX`, `1`, `2`). 30% are fully random to preserve
population diversity and prevent premature convergence.

### 5.3 Bare-Metal Test Harness

Every generated test uses an identical harness compatible with:

- **Spike** RISC-V ISS (`spike --isa=rv32im <elf>`)
- **QEMU** (`qemu-system-riscv32 -M virt -bios none -kernel <elf>`)
- **Verilator** RTL testbenches polling `tohost`
- **Synopsys VCS / Cadence Xcelium** with HTIF-aware wrappers

Key harness properties:

- Entry point at `0x80000000` (standard M-mode reset vector)
- `mtvec` pointed at `_trap_vec` before any test instructions run
- Stack pointer initialised to `_stack_top` (8 KiB above BSS)
- `t6` loaded with `_mem_region` base before test body (all loads/stores safe)
- All working registers zero-initialised before test body (eliminates uninitialised-read artefacts in tandem comparison)
- HTIF `tohost`/`fromhost` at 64-byte alignment (HTIF specification requirement)
- Exit protocol: `tohost = (exit_code << 1) | 1`; code 0 = PASS, 1 = FAIL, 0xDEAD = unexpected trap

### 5.4 Memory Safety

All random and evolved load/store instructions are bounded to a 256-byte BSS
region (`_mem_region`) through the `t6` base register. Offsets are:

- Non-negative
- Alignment-correct for the access width (4-byte for `lw`/`sw`, 2-byte for `lh`/`sh`, 1-byte for `lb`/`sb`)
- Within `[0, 256 - access_width]`

This guarantees no generated test can corrupt the stack, code, or HTIF symbols,
and that tandem comparison between RTL and Spike always starts from a clean state.

### 5.5 Reproducibility

Every random and evolved test is **fully deterministic** given the same seed.
The seed derivation for the i-th random test is:

```
seed_i = (base_seed XOR ((i+1) * 0x9E3779B9)) AND 0xFFFFFFFF
```

This uses Knuth's multiplicative hash constant, giving uniform distribution
across the 32-bit space without aliasing between consecutive indices. Running
the CLI twice with `--seed 0xDEADBEEF` produces byte-identical `.S` files.

### 5.6 Assembly Toolchain

The generator detects RISC-V GCC in preference order:

1. `riscv32-unknown-elf-gcc`
2. `riscv64-unknown-elf-gcc` (with `-march=rv32im -mabi=ilp32`)
3. `riscv32-linux-gnu-gcc`
4. `riscv64-linux-gnu-gcc`

GCC flags used: `-march=rv32im -mabi=ilp32 -mno-relax -nostdlib -static`

If no toolchain is found, `.S` source files are still produced and all other
functionality (fitness scoring, evolution, manifest writing) works normally.

---

## 6. File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `directed_tests.py` | 1122 | 48 hand-crafted M-extension tests with spec-verified expected values. `DirectedTest` dataclass with `fitness`, `targets`, `evaluate_fitness()`, `with_fitness()`. Eager `verify_all()` at import. |
| `random_gen.py` | 546 | `RV32IMRandomGenerator` with standard `generate()` and constraint-biased `generate_biased()`. `GeneratorConfig` dataclass with validation. `InstructionMix` statistics. |
| `asm_builder.py` | 539 | `build_directed_asm()`, `build_random_asm()`, `build_evolved_asm()`. `write_test()` with optional `evolution_meta`. HTIF harness templates. GCC wrapper. Spike runner. |
| `genetic_engine.py` | 619 | `GeneticEngine` class — full generational GA. `Individual` dataclass. Crossover, mutation, fitness scoring, elitism, generation statistics, `summary()`. |
| `generate_tests.py` | 713 | CLI entry point. Baseline mode and manifest mode. `generate_from_manifest()`. Result collection. Summary printing. `write_manifest()`. |
| `__init__.py` | 92 | Public API exports. Version 3.0.0. |

---

## 7. Quick Start

```bash
# Python 3.8+ required. No non-stdlib dependencies.

# Optionally install a RISC-V GCC toolchain for ELF output:
#   Ubuntu/Debian:  sudo apt install gcc-riscv64-unknown-elf
#   macOS:          brew install riscv-gnu-toolchain

# Place the rv32im_testgen/ directory alongside your project, then:

# Generate 20 directed + 50 random tests (source only, no GCC needed)
python -m rv32im_testgen.generate_tests --no-assemble

# With GCC: produces .elf files ready for Spike or RTL simulation
python -m rv32im_testgen.generate_tests

# Verify output
ls tests_out/directed/   # 20 x (.S + .meta.json [+ .elf])
ls tests_out/random/     # 50 x (.S + .meta.json [+ .elf])
cat tests_out/manifest.json
```

---

## 8. Usage — Baseline Mode

```
python -m rv32im_testgen.generate_tests [options]

Options:
  --seed SEED            Base seed (hex or decimal).     Default: 0
  --random N             Number of random tests.         Default: 50
  --directed N           Number of directed tests.       Default: 20
  --length INSTRS        Instructions per random test.   Default: 200
  --outdir DIR           Output directory.               Default: tests_out/
  --trap-rate RATE       Trap injection probability.     Default: 0.0
  --m-weight W           M-extension weight (0 to 1).   Default: 0.30
  --extended-directed    Use full 48-test pool.
  --no-assemble          Skip GCC; produce .S only.
  --verify-spike         Run ELFs through Spike.
  --verbose / -v         Enable DEBUG logging.
```

### Example: custom seed with trap injection

```bash
python -m rv32im_testgen.generate_tests \
    --seed 0xDEADBEEF \
    --random 50 \
    --directed 20 \
    --m-weight 0.40 \
    --trap-rate 0.02 \
    --outdir ./my_tests
```

**Sample output:**

```
15:45:09 [INFO   ] ============================================================
15:45:09 [INFO   ]   Agent G — RV32IM Test Generator
15:45:09 [INFO   ] ============================================================
15:45:09 [INFO   ]   Base seed  : 0xDEADBEEF
15:45:09 [INFO   ]   Directed   : 20  (pool: 20)
15:45:09 [INFO   ]   Random     : 50  (length=200)
15:45:09 [INFO   ]   Trap rate  : 2.0%
15:45:09 [INFO   ]   M-ext wt   : 40%
15:45:09 [INFO   ]   Toolchain  : riscv64-unknown-elf-
15:45:09 [INFO   ] ============================================================
15:45:09 [INFO   ] Generating 20 directed tests
15:45:09 [INFO   ]   [01/20] mul_neg_one                              [elf+asm]
15:45:09 [INFO   ]   [02/20] mul_overflow_wrap                        [elf+asm]
  ...
15:45:10 [INFO   ]   [01/50] seed=0x409A5961  M-ext=54/200 (27%)  [elf+asm]
  ...
============================================================
  AGENT G — GENERATION COMPLETE
============================================================
  Wall time      : 4.21s
  Directed tests :  20  (20 ELFs produced)
  Random tests   :  50  (50 ELFs produced)
  Avg M-ext      : 57.8/200 (28.9%)
  Manifest       : ./my_tests/manifest.json
============================================================
```

---

## 9. Usage — Manifest Mode (AVA Feedback Loop)

This mode is triggered by `--manifest` and activated by the AVA orchestrator
after Agents B–F have completed a simulation round.

### Prerequisites

The manifest must have a `rundir` pointing to a directory containing:

```
<rundir>/
  coveragesummary.json    written by Agent F
  bugreport.json          written by Agent D
```

**`coveragesummary.json` (Agent F format):**
```json
{
  "cold_paths": [
    {"module": "mul",          "reachability": 0.04, "line": "rtl/mul.sv:142"},
    {"module": "div_overflow", "reachability": 0.01, "line": "rtl/div.sv:89"},
    {"module": "rem_zero",     "reachability": 0.06}
  ]
}
```

**`bugreport.json` (Agent D format):**
```json
{
  "hypotheses": [
    {"module": "div",      "hypothesis": "div-by-zero result wrong",       "confidence": 0.9, "step": 1234},
    {"module": "overflow", "hypothesis": "INT_MIN/-1 overflow unhandled",  "confidence": 0.7, "step": 5678}
  ]
}
```

**`manifest.json` (minimal):**
```json
{
  "rundir": "/path/to/simulation/run",
  "seed":   42,
  "phases": {
    "coverage":  {"status": "completed"},
    "bugfinder": {"status": "completed"}
  }
}
```

### Running manifest mode

```bash
python -m rv32im_testgen.generate_tests --manifest /path/to/manifest.json
```

### What happens, step by step

1. Reads `cold_paths` from `coveragesummary.json`
2. Reads `hypotheses` from `bugreport.json`
3. Unifies into a single constraints list (e.g. 8 entries)
4. Builds generation-0 seed population: 20 baseline directed + 80 biased random = 100 individuals
5. Runs 10 generations of elitism + crossover + mutation
6. Writes top-50 individuals to `<rundir>/outputs/test_binaries/`
7. Writes `evolution_summary.json` with per-generation fitness statistics
8. Updates `manifest["phases"]["generator"]` with status and statistics

**Sample output:**

```
15:45:26 [INFO   ] Manifest mode: /run/manifest.json
15:45:26 [INFO   ]   Agent F cold paths: 6
15:45:26 [INFO   ]   Agent D hypotheses: 2
15:45:26 [INFO   ]   Total constraints:  8
15:45:26 [INFO   ] GeneticEngine: seed=42 pop=100 gen=10 constraints=8
15:45:26 [INFO   ]   gen 01/10  pop=100  best=6.30  mean=3.88
15:45:26 [INFO   ]   gen 02/10  pop=100  best=6.70  mean=5.27
15:45:26 [INFO   ]   gen 03/10  pop=100  best=6.90  mean=6.11
  ...
15:45:26 [INFO   ]   gen 10/10  pop=100  best=7.10  mean=7.02
15:45:26 [INFO   ] GeneticEngine: 50 tests written, avg_fitness=7.100
```

**Updated `manifest["phases"]["generator"]`:**

```json
"generator": {
  "status":               "completed",
  "evolved_population":   50,
  "constraints_targeted": 8,
  "avg_fitness":          7.1,
  "seed_tests":           70,
  "output_dir":           "/run/outputs/test_binaries",
  "completed_at":         "2025-04-09T12:00:00+00:00"
}
```

---

## 10. Usage — Python API

### Directed tests

```python
import rv32im_testgen as pkg

# Access test pools
tests     = pkg.ALL_DIRECTED_TESTS           # curated 20
all_tests = pkg.ALL_DIRECTED_TESTS_EXTENDED  # full 48
t = pkg.get_test_by_name("div_by_zero_nonzero_dividend")

# Score against constraints
constraints = [
    {"module": "div",  "reachability": 0.04},
    {"module": "mul",  "hypothesis":   "overflow corner case"},
]
score = t.evaluate_fitness(constraints)

# Return an immutable updated copy (dataclass is frozen)
t_scored = t.with_fitness(score, targets=["div", "mul"])
print(t_scored.fitness)   # 1.0
print(t_scored.targets)   # ('div', 'mul')
```

### Assembly

```python
from pathlib import Path
from rv32im_testgen import build_directed_asm, write_test

asm = build_directed_asm(t)
artifacts = write_test(
    name=t.name,
    asm_src=asm,
    metadata={"type": "directed"},
    outdir=Path("out/"),
    assemble=True,
)
# artifacts == {"S": "out/name.S", "meta": "out/name.meta.json", "elf": "out/name.elf or None"}
```

### Random generation

```python
from rv32im_testgen import RV32IMRandomGenerator, GeneratorConfig

cfg = GeneratorConfig(seed=0xDEADBEEF, length=200, trap_injection_rate=0.02)
gen = RV32IMRandomGenerator(cfg)

# Standard (unbiased)
lines, mix = gen.generate()
print(f"M-ext: {mix.alu_m}/{mix.total} ({mix.to_dict()['alu_m_pct']}%)")

# Constraint-biased
biased = gen.generate_biased(population=80, constraints=constraints, bias_ratio=0.70)
# Returns: [{"sequence": [...asm lines...], "targets": ["div"], "seed": 12345}, ...]
```

### Genetic engine

```python
from rv32im_testgen import GeneticEngine

engine = GeneticEngine(
    seed=42,
    population_size=100,
    generations=10,
    output_count=50,
)

results = engine.evolve(
    constraints=constraints,
    outdir=Path("evolved/"),
    assemble=True,
)

# Each result:
# {
#   "name":       "evo_cx_00145",
#   "fitness":    7.10,
#   "targets":    ["mul", "div_overflow"],
#   "generation": 8,
#   "parents":    ["dir_00001", "rnd_00032"],
#   "artifacts":  {"S": "...", "meta": "...", "elf": "..."}
# }

print(engine.summary())
# {"generations_run": 10, "peak_best_fitness": 7.1, "final_mean_fitness": 7.02, ...}
```

---

## 11. Directed Test Catalogue

### Core 20 tests (always generated)

| # | Name | Opcode | Edge Case | Expected Result |
|---|------|--------|-----------|----------------|
| 1 | `mul_neg_one` | MUL | x × (−1) | two's complement negation |
| 2 | `mul_overflow_wrap` | MUL | INT_MAX × 2 | 0xFFFFFFFE (lower 32 wrap) |
| 3 | `mul_int_min_squared` | MUL | INT_MIN² | 0 (product is 2^62) |
| 4 | `mulh_int_max_squared` | MULH | INT_MAX² upper | 0x3FFFFFFF |
| 5 | `mulh_int_min_squared` | MULH | INT_MIN² upper | 0x40000000 |
| 6 | `mulh_negative_times_positive` | MULH | INT_MIN×2 upper | 0xFFFFFFFF (−1) |
| 7 | `mulhsu_int_min_times_uint_max` | MULHSU | signed×unsigned mixed sign | 0x80000000 |
| 8 | `mulhsu_zero_rs1` | MULHSU | 0×UINT_MAX | 0 |
| 9 | `mulhu_uint_max_squared` | MULHU | UINT_MAX² upper | 0xFFFFFFFE |
| 10 | `mulhu_two_times_int_min_boundary` | MULHU | 2×2^31 carry-out | 1 |
| 11 | `div_by_zero_nonzero_dividend` | DIV | 42÷0 | 0xFFFFFFFF (no trap) |
| 12 | `div_by_zero_zero_dividend` | DIV | 0÷0 | 0xFFFFFFFF (no trap) |
| 13 | `div_overflow_int_min_by_neg_one` | DIV | INT_MIN÷(−1) | INT_MIN (no trap) |
| 14 | `div_truncate_toward_zero_negative` | DIV | −7÷2 | −3 (NOT −4) |
| 15 | `divu_by_zero` | DIVU | 42÷0 | 0xFFFFFFFF (no trap) |
| 16 | `divu_int_min_bit_pattern` | DIVU | 0x80000000÷2 | 0x40000000 |
| 17 | `rem_by_zero_returns_dividend` | REM | 42%0 | 42 (unchanged) |
| 18 | `rem_overflow_int_min_neg_one_is_zero` | REM | INT_MIN%(−1) | 0 |
| 19 | `remu_by_zero_returns_dividend` | REMU | 42%0 | 42 (unchanged) |
| 20 | `combined_rem_div_euclidean_identity` | REM+DIV+MUL+ADD | a=(a÷b)×b+(a%b) | 37 |

### Sample directed test — annotated assembly

```asm
    .section .text.init, "ax"
    .global  _start

/* Trap vector — any exception writes 0xDEAD exit code */
_trap_vec:
    la   t0, tohost
    li   a0, 0xDEAD
    slli a0, a0, 1
    ori  a0, a0, 1
    sw   a0, 0(t0)
_halt:
    j    _halt

_start:
    la   t0, _trap_vec
    csrw mtvec, t0          /* point trap vector */
    la   sp, _stack_top     /* init stack */
    la   t6, _mem_region    /* safe mem base */
    li   t0, 0              /* zero all working regs */
    /* ... */

/* DIRECTED TEST: div_by_zero_nonzero_dividend
 * DIV: 42 / 0 -> -1 (0xFFFFFFFF); no exception per spec
 * Spec ref: section 7.2 Table 7.1
 */
    li   t0, 42
    li   t1, 0
    div  t2, t0, t1         /* <- instruction under test */

/* Self-check assertion */
    li      a4, 4294967295  /* expected: 0xFFFFFFFF */
    bne     t2, a4, _fail   /* branch to FAIL if wrong */

/* PASS path */
    li   a0, 0
    j    _exit

_fail:
    li   a0, 1

_exit:
    la   t0, tohost
    slli a0, a0, 1
    ori  a0, a0, 1
    sw   a0, 0(t0)          /* HTIF: (exit_code << 1) | 1 */
    j    _halt
```

### Extended pool: additional 28 tests

Use `--extended-directed` to access: `mul_zero_operand`, `mul_identity`,
`mul_both_negative`, `mul_x0_destination`, `mulh_small_positive_zero_upper`,
`mulh_int_min_times_neg_one`, `mulhsu_positive_times_uint_max`,
`mulhu_zero_operand`, `mulhu_one_times_uint_max`, all basic
`div`/`divu`/`rem`/`remu` variants, and three combined multi-instruction
identity tests.

---

## 12. Genetic Engine Deep Dive

### Convergence example (10 generations, 6 constraints)

```
gen 01: best=6.30  mean=3.88   <- diverse seed population
gen 02: best=6.70  mean=5.27   <- crossover combining good prefixes
gen 03: best=6.90  mean=6.11   <- elites carry forward
gen 04: best=7.10  mean=6.40   <- near-optimal reached
gen 05: best=7.10  mean=6.45   <- diversity maintained via mutation
gen 06: best=7.10  mean=6.67
gen 07: best=7.10  mean=6.78
gen 08: best=7.10  mean=6.95
gen 09: best=7.10  mean=6.94
gen 10: best=7.10  mean=7.02   <- population converges
```

### Individual lifecycle

```
Generation 0 (seed): baseline directed test
  asm_lines = ["li t0, 0x80000000", "li t1, 2", "mulh t2, t0, t1"]
  fitness   = 0.0  (not yet scored)
  parents   = []

After fitness ranking:
  fitness   = 4.50  (matches "mul" constraint + mul bonus)

Generation 3 crossover child:
  asm_lines = [
    "li  t0, 0x80000000",   <- prefix from parent 1 (crossover at k=2)
    "li  t1, -1",           <- prefix from parent 1
    "div t2, t0, t1",       <- suffix from parent 2 (hits "div" constraint)
    "divu t3, t2, t4",
  ]
  fitness          = 7.10
  parents          = ["dir_00001", "rnd_00032"]
  crossover_point  = 2
  generation       = 3
```

### Evolution metadata in `.meta.json`

```json
{
  "type":       "evolved",
  "name":       "evo_cx_00145",
  "fitness":    7.10,
  "targets":    ["mul", "div_overflow"],
  "generation": 8,
  "parents":    ["dir_00001", "rnd_00032"],
  "evolution": {
    "fitness":            7.10,
    "targets":            ["mul", "div_overflow"],
    "evolved_generation": 8,
    "evolved_from":       ["dir_00001", "rnd_00032"],
    "crossover_point":    2
  }
}
```

This metadata enables Agent F to **attribute coverage improvements** to
specific evolved tests, and Agent D to **track which hypotheses were proven**
by which test across simulation rounds.

---

## 13. Assembly Harness Specification

**Memory map after linking:**

```
0x80000000  _trap_vec      trap vector (direct mode, mtvec set here)
            _halt          spin loop
            _start         entry point
            [test body]    directed or random instructions
            _fail          exit with code 1
            _exit          HTIF tohost write
[64-byte aligned]
            tohost         HTIF register (64-bit, lower 32 used)
            fromhost       HTIF register (64-bit)
[BSS]
            _mem_region    256-byte safe load/store scratch (t6 base)
[Stack]
            _stack_base
            ...            8 KiB (grows downward)
            _stack_top     initial sp value
```

**HTIF exit codes:**

| `tohost` value | Meaning |
|---------------|---------|
| `0x00000001` | PASS (exit_code 0) |
| `0x00000003` | FAIL (exit_code 1) |
| `0x0001BD5B` | Unexpected trap (0xDEAD) |

---

## 14. Output Schema

### Directory layout

```
tests_out/
+-- manifest.json               master index of all tests
+-- directed/
|   +-- link.ld                 shared linker script
|   +-- mul_neg_one.S
|   +-- mul_neg_one.meta.json
|   +-- mul_neg_one.elf         (if toolchain available)
|   +-- ...                     x 20 tests
+-- random/
    +-- link.ld
    +-- rand_s409A5961_l200.S
    +-- rand_s409A5961_l200.meta.json
    +-- rand_s409A5961_l200.elf
    +-- ...                     x 50 tests
```

### `manifest.json` top-level keys

```json
{
  "agent":          "G — RV32IM Test Generator",
  "generated_at":   "2025-04-09T12:00:00+00:00",
  "base_seed":      "0xDEADBEEF",
  "toolchain":      "riscv64-unknown-elf-",
  "elf_produced":   true,
  "directed_count": 20,
  "random_count":   50,
  "wall_seconds":   4.21,
  "errors":         [],
  "generator": {
    "seed_tests":           70,
    "evolved_population":   0,
    "constraints_targeted": 0,
    "avg_fitness":          0.0
  },
  "directed": [
    {
      "name": "mul_neg_one", "category": "mul.sign",
      "fitness": 0.0, "targets": [],
      "asm": "tests_out/directed/mul_neg_one.S",
      "elf": "tests_out/directed/mul_neg_one.elf",
      "meta": "tests_out/directed/mul_neg_one.meta.json",
      "spike": null
    }
  ],
  "random": [ ... ]
}
```

---

## 15. Integration Guide for Other Agents

### Agent B — consuming baseline ELFs

```python
import json
from pathlib import Path

manifest = json.loads(Path("tests_out/manifest.json").read_text())
for entry in manifest["directed"] + manifest["random"]:
    if entry["elf"]:
        run_rtl_simulation(entry["elf"])
```

### Agent B — consuming evolved ELFs (manifest mode)

```python
summary = json.loads(
    Path("rundir/outputs/test_binaries/evolution_summary.json").read_text()
)
for tb in summary["test_binaries"]:
    path = tb["path"]
    if path and path.endswith(".elf"):
        run_rtl_simulation(path, extra_tags={"fitness": tb["fitness"], "targets": tb["targets"]})
```

### Agent C — running Spike ISS

```bash
for elf in tests_out/directed/*.elf tests_out/random/*.elf; do
    spike --isa=rv32im --log-commits "$elf" > "${elf%.elf}.commitlog" 2>&1
done
```

Or via the Python API:

```python
from rv32im_testgen import run_spike
code = run_spike(Path("tests_out/directed/div_by_zero_nonzero_dividend.elf"))
# 0 = PASS, None = Spike not installed / timeout
```

### Agent D — reading evolved test provenance

```python
for meta_file in Path("rundir/outputs/test_binaries").glob("*.meta.json"):
    meta = json.loads(meta_file.read_text())
    if meta.get("type") == "evolved":
        print(f"{meta['name']}: targets={meta['targets']}, fitness={meta['fitness']:.2f}")
```

### Agent F — writing cold paths (format consumed by Agent G)

```json
{
  "cold_paths": [
    {"module": "mul",          "reachability": 0.04},
    {"module": "div_overflow", "reachability": 0.01},
    {"module": "rem_zero",     "reachability": 0.06}
  ]
}
```

Supported `"module"` keywords: `mul`, `mulh`, `mulhsu`, `mulhu`, `div`,
`divu`, `rem`, `remu`, `load`, `store`, `branch`, `alu`, `shift`, `overflow`,
`zero`, `sign`, `boundary`. Partial substring matching is also performed (e.g.
`"mul_overflow"` matches the `"overflow"` key).

### Programmatic API summary

```python
from rv32im_testgen import (
    # Test data
    ALL_DIRECTED_TESTS, ALL_DIRECTED_TESTS_EXTENDED,
    get_test_by_name, verify_all,
    # Generation
    RV32IMRandomGenerator, GeneratorConfig, InstructionMix,
    # Assembly
    build_directed_asm, build_random_asm, build_evolved_asm, write_test,
    run_spike, TOOLCHAIN_PREFIX,
    # Evolution
    GeneticEngine, Individual,
)
```

---

## 16. Configuration Reference

### `GeneratorConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `seed` | int | 0 | RNG seed — same seed gives identical output |
| `length` | int | 200 | Instruction groups per test |
| `mem_size` | int | 256 | Safe scratch region size in bytes (must be >= 8) |
| `trap_injection_rate` | float | 0.0 | Fraction of slots replaced by traps [0, 1] |
| `weights` | dict | DEFAULT_WEIGHTS | Per-group instruction mix (normalised internally) |
| `reserved_dest_regs` | list | [] | Registers excluded from destinations |

### DEFAULT_WEIGHTS

| Group | Weight | Opcodes covered |
|-------|--------|----------------|
| `alu_m` | 0.30 | mul, mulh, mulhsu, mulhu, div, divu, rem, remu |
| `alu_r` | 0.20 | add, sub, sll, slt, sltu, xor, srl, sra, or, and |
| `alu_i` | 0.12 | addi, slti, sltiu, xori, ori, andi |
| `branch` | 0.10 | beq, bne, blt, bge, bltu, bgeu |
| `load` | 0.08 | lw, lh, lhu, lb, lbu |
| `store` | 0.08 | sw, sh, sb |
| `shift_i` | 0.05 | slli, srli, srai |
| `lui_auipc` | 0.04 | lui, auipc |
| `nop` | 0.03 | nop |

### `GeneticEngine`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `seed` | 42 | Base RNG seed |
| `population_size` | 100 | Individuals per generation |
| `generations` | 10 | Number of evolutionary cycles |
| `elite_fraction` | 0.20 | Fraction of top individuals preserved unchanged |
| `mutation_rate` | 0.15 | Mean fraction of instructions mutated per offspring |
| `crossover_rate` | 0.60 | Fraction of offspring produced by crossover |
| `output_count` | 50 | Number of top individuals to write as output files |

---

## 17. Adding New Tests

To add a directed test to the extended pool:

```python
# In directed_tests.py, append to the appropriate list
# (MUL_TESTS, DIV_TESTS, REM_TESTS, COMBINED_TESTS, etc.)

REM_TESTS.append(
    DirectedTest.make(
        name="rem_large_values",         # unique, [a-z0-9_] only
        description="REM: 0x7FFFFFFF % 100 = 47",
        category="rem.basic",
        asm_body=[
            "li   t0, 0x7FFFFFFF",
            "li   t1, 100",
            "rem  t2, t0, t1",
        ],
        # Always compute with spec functions -- never hand-type
        expected_regs={"t2": _rv_rem(0x7FFFFFFF, 100)},
        spec_ref="§7.2",
    )
)
```

`verify_all()` runs at import time and will immediately catch:
- Duplicate names
- Invalid register values (outside unsigned 32-bit range)
- Empty `asm_body`
- Invalid characters in name
- Negative or NaN fitness values

---

## 18. Troubleshooting

### No ELF files produced

```
Warning: No RISC-V GCC toolchain found. Assembly sources (.S) only.
```

Install a RISC-V cross-toolchain:

```bash
# Ubuntu/Debian
sudo apt install gcc-riscv64-unknown-elf

# macOS
brew tap riscv-software-src/riscv
brew install riscv-gnu-toolchain

# Verify
riscv64-unknown-elf-gcc --version
```

### Manifest mode: `coveragesummary.json` not found

Agent F has not yet written its output, or `rundir` in the manifest points
to the wrong directory. Generation proceeds with an empty constraint list —
the engine runs but produces unbiased (unguided) evolved tests. This is logged
as a warning, not an error.

### Spike reports FAIL for a directed test

This means either (a) a real RTL bug was found, or (b) the expected value in
the directed test is wrong. To distinguish, run the same ELF on Spike:

```bash
spike --isa=rv32im tests_out/directed/div_by_zero_nonzero_dividend.elf
echo "Spike exit: $?"   # 0 = PASS (test wrote tohost=1)
```

If Spike says PASS but RTL says FAIL, Agent D should log it as a mismatch
hypothesis for the next evolutionary cycle.

### `AssertionError: Directed test validation found N error(s)`

This fires at import time if `directed_tests.py` has been edited with an
out-of-range expected value or a duplicate name. The error message lists every
failing test and the specific problem. Fix the listed tests and re-import.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All tests generated successfully |
| `1` | Fatal error (bad arguments, I/O error, import failure) |
| `2` | One or more Spike verification failures (only with `--verify-spike`) |
| `3` | Manifest read or write failed (manifest mode only) |

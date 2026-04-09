# AVA Verilator Backend — RV32IM RTL Runner

Single-command end-to-end: Verilator build → ELF simulation → commit log → coverage report.

## Prerequisites

```bash
# Ubuntu / Debian
sudo apt install verilator gcc-riscv32-unknown-elf binutils-riscv32-unknown-elf

# Verify
verilator --version     # ≥ 4.034
riscv32-unknown-elf-gcc --version
```

## Project layout

```
backends/
  run_rtl.py              # CLI entry-point  ← run this
  sim/
    sim_main.cpp          # Verilator C++ harness (memory model + commit monitor)
    elf_loader.h          # Header-only ELF32 parser (no libelf needed)
rtl/
  example_cpu/
    rv32im_core.v         # Single-cycle RV32IM core with AVA commit interface
    cpu_top.v             # Top-level wrapper (what --top points to)
tests/
  add_loop.S              # Example: add loop + full M-extension corner cases
  link.ld                 # Bare-metal linker script (loads at 0x80000000)
  build_tests.sh          # Helper to compile test ELFs
```

## End-to-end example

### Step 1 — Compile the test ELF

```bash
riscv32-unknown-elf-gcc \
    -march=rv32im -mabi=ilp32 -nostdlib \
    -T tests/link.ld \
    tests/add_loop.S \
    -o tests/add_loop.elf

# Confirm it looks right
riscv32-unknown-elf-objdump -d tests/add_loop.elf | head -40
```

### Step 2 — Run the Verilator backend

```bash
python backends/run_rtl.py \
    --rtl rtl/example_cpu/rv32im_core.v \
          rtl/example_cpu/cpu_top.v \
    --top cpu_top \
    --elf tests/add_loop.elf \
    --seed 42 \
    --out runs/run_42
```

Expected output:

```
[ava]  Run dir   : /path/to/runs/run_42
[ava]  ELF       : tests/add_loop.elf
[ava]  Top       : cpu_top
[ava]  Seed      : 42
[build] Verilator command:
  verilator --cc --exe --build --coverage --coverage-underscore ...
[build] Binary ready: runs/run_42/build/Vcpu_top
[mem] ELF loaded: entry=0x80000000 base=0x80000000 top=0x80000120
[sim] Starting simulation (max_insns=100000, seed=42)
[sim] Retired 87 instructions in 0.01s

============================================================
  STATUS     : PASS
  Retired    : 87
  Wall time  : 0.01s
  Cov line   : 73.2%
  Cov branch : 61.5%
  Cov toggle : 42.8%
============================================================
[ava]  Commit log → runs/run_42/rtl.commitlog.jsonl
[ava]  Coverage   → runs/run_42/coverage_report.json
[ava]  Manifest   → runs/run_42/manifest.json
```

### Step 3 — Inspect the commit log

```bash
head -5 runs/run_42/rtl.commitlog.jsonl | python3 -m json.tool
```

Sample output:

```json
{
  "seq": 0,
  "pc": "0x80000000",
  "instr": "0x00000137",
  "mode": "M",
  "rd": { "x2": "0x80400000" }
}
{
  "seq": 1,
  "pc": "0x80000004",
  "instr": "0x00010113",
  "mode": "M",
  "rd": { "x2": "0x80400000" }
}
```

### Step 4 — Optional: enable VCD waveform

```bash
python backends/run_rtl.py \
    --rtl rtl/example_cpu/rv32im_core.v rtl/example_cpu/cpu_top.v \
    --top cpu_top \
    --elf tests/add_loop.elf \
    --seed 42 \
    --out runs/run_42_vcd \
    --vcd

# Open in GTKWave
gtkwave runs/run_42_vcd/rtl.vcd &
```

---

## DUT commit interface contract

Your own DUT must expose these ports (same names, same widths) for the harness to work:

```verilog
input  wire        clk
input  wire        rst_n

// Memory bus
output wire        mem_req_valid
output wire        mem_req_we
output wire [31:0] mem_req_addr
output wire [31:0] mem_req_wdata
output wire [3:0]  mem_req_wstrb
input  wire [31:0] mem_resp_rdata
input  wire        mem_resp_ready      // tie to 1 in harness

// Commit monitor — one-cycle pulse per retired instruction
output wire        commit_valid
output wire [31:0] commit_pc
output wire [31:0] commit_instr
output wire [4:0]  commit_rd_addr     // 0 = no writeback
output wire [31:0] commit_rd_data
output wire        commit_rd_we
output wire [1:0]  commit_priv_mode   // 2'b11=M 2'b01=S 2'b00=U
output wire        commit_trap_valid
output wire [31:0] commit_trap_cause
output wire [31:0] commit_trap_tval
output wire        commit_is_mret
```

---

## Verilator coverage flags explained

| Flag | Type enabled | Description |
|---|---|---|
| `--coverage` | line, branch, expression | Core coverage counters |
| `--coverage-underscore` | (all) | Also instrument `_`-prefixed signals |
| `--trace` | — | Enables `VerilatedVcdC` tracing support |
| `-O2` | — | Optimise generated C++ (~3× faster sim) |

Coverage output written to `rtl.coverage.dat` (Verilator binary format),
parsed into `coverage_report.json` by `run_rtl.py`.
To inspect raw counters:

```bash
verilator_coverage runs/run_42/rtl.coverage.dat
```

---

## Run manifest

Every run writes `manifest.json` in the run directory — machine-readable
record of inputs, artefact paths, and result:

```json
{
  "run_id": "a3f2...",
  "timestamp": "2025-01-01T00:00:00+00:00",
  "config": { "xlen": 32, "extensions": ["I","M"], "seed": 42, ... },
  "artifacts": {
    "rtl_commitlog": "runs/run_42/rtl.commitlog.jsonl",
    "rtl_coverage":  "runs/run_42/rtl.coverage.dat",
    "coverage_report": "runs/run_42/coverage_report.json"
  },
  "result": { "status": "pass", "instructions_retired": 87, ... }
}
```

---

## CLI reference

```
python backends/run_rtl.py --help

  --rtl FILE [FILE ...]   Verilog/SV source files  [required]
  --top MODULE            Top module name           [required]
  --elf FILE              ELF to simulate           [required]
  --seed N                RNG seed                  [default: 42]
  --out DIR               Output run directory      [required]
  --max-insns N           Retire limit              [default: 100000]
  --mem-base HEX          RAM base address          [default: 0x80000000]
  --mem-size BYTES        RAM size                  [default: 67108864]
  --vcd                   Enable VCD waveform dump
  --jobs N                Make parallelism          [default: 4]
  --verilator PATH        Verilator binary          [default: verilator]
  --verbose               Print every retire to stderr
  --rebuild               Force Verilator rebuild
```

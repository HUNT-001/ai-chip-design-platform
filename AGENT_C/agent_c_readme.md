# Agent C — Spike ISS Golden Backend

**AVA Verification Platform | Phase 1 | RV32IM**

---

## What this delivers

| File | Purpose |
|------|---------|
| `sim/run_iss.py` | CLI entry-point: runs Spike, writes `iss.commitlog.jsonl` |
| `sim/spike_parser.py` | Converts raw Spike log text → schema-compliant JSONL records |
| `schemas/commitlog.schema.json` | JSON Schema for every commit record (shared with Agent B) |
| `tests/test_spike_parser.py` | 49 unit tests for the parser (all format variants) |
| `tests/test_run_iss_integration.py` | 19 integration tests (mocked Spike + real-Spike gate) |
| `tests/asm/rv32im_smoke.S` | Bare-metal RV32IM test: integer, MUL/DIV/REM, edge cases |
| `tests/asm/link.ld` | Linker script for Spike bare-metal layout |

---

## Quick start

### 1. Install Spike

```bash
git clone https://github.com/riscv-software-src/riscv-isa-sim
cd riscv-isa-sim && mkdir build && cd build
../configure --prefix=/opt/riscv
make -j$(nproc) && make install
export PATH=/opt/riscv/bin:$PATH
spike --version
```

### 2. Compile the smoke test

```bash
riscv32-unknown-elf-gcc \
    -march=rv32im -mabi=ilp32 \
    -nostdlib -static \
    -T tests/asm/link.ld \
    tests/asm/rv32im_smoke.S \
    -o tests/asm/rv32im_smoke.elf
```

### 3. Run the golden ISS

```bash
python sim/run_iss.py \
    --isa RV32IM \
    --elf tests/asm/rv32im_smoke.elf \
    --out runs/smoke_001 \
    --validate
```

Expected terminal output:
```
──────────────────────────────────────────────────────────────
  AVA Agent C — Spike Golden Runner  [DONE]
──────────────────────────────────────────────────────────────
  ELF         : rv32im_smoke.elf
  ISA         : RV32IM
  Log mode    : log_commits
  Commits     : 47
  Elapsed     : 0.12s
  Spike exit  : 1
  Output      : runs/smoke_001/iss.commitlog.jsonl
  Manifest    : runs/smoke_001/manifest.json
──────────────────────────────────────────────────────────────
```

### 4. Inspect output

```bash
# Count commits (must match RTL commit log line count for same ELF)
wc -l runs/smoke_001/iss.commitlog.jsonl

# Pretty-print first record
head -1 runs/smoke_001/iss.commitlog.jsonl | python3 -m json.tool
```

Example record (FORMAT B, --log-commits):
```json
{
  "seq": 0,
  "pc": "0x80000000",
  "instr": "0x00000297",
  "disasm": "auipc   t0, 0x0",
  "priv": "M",
  "source": "iss",
  "reg_writes": [{"rd": 5, "value": "0x80000000"}]
}
```

### 5. Run all tests (no Spike required)

```bash
python tests/test_spike_parser.py       # 49 unit tests
python tests/test_run_iss_integration.py # 19 integration tests (mocked)
```

### 6. Run real-Spike gate (requires Spike + compiled ELF)

```bash
RV32IM_ELF=tests/asm/rv32im_smoke.elf \
    python tests/test_run_iss_integration.py
```

---

## CLI reference

```
run_iss.py --isa ISA --elf ELF --out DIR [options]

Required:
  --isa       ISA string, e.g. RV32IM (case-insensitive)
  --elf       Input ELF binary
  --out       Output run directory (created if absent)

Spike control:
  --spike     Spike binary name or full path  [default: spike]
  --pk        Proxy kernel path (omit for bare-metal ELFs with tohost)
  --max-instrs  Max committed instructions     [default: 10,000,000]
  --timeout   Spike process timeout (seconds)  [default: 300]
  --extra-spike-args  Additional Spike flags (quote whole string)

Log format:
  --force-format {A,B}
              A = -l trace (no register values)
              B = --log-commits (full register + CSR + memory writes)
              Default: auto-detected from Spike capabilities

Output control:
  --max-records   Cap written commit records   [default: unlimited]
  --seed          Random seed stored in manifest
  --validate      Validate first 200 records against schema
  --verbose / -v  Debug logging
```

---

## Spike log format handling

Agent C automatically detects and handles **three distinct Spike output sub-formats**:

### FORMAT A — `-l` flag (all Spike versions)
```
core   0: 0x80000000 (0x00000297) auipc   t0, 0x0
```
Carries: PC, instruction encoding, disassembly. **No register values.**
Parser output: `{seq, pc, instr, disasm, source}` — suitable for PC-trace comparison only.

### FORMAT B — `--log-commits` / `--enable-commitlog` (Spike ≥ 1.1)

**Sub-layout 1: inline writeback** (most common)
```
core   0: 3 0x80000000 (0x00000297) x5  0x80000000
```

**Sub-layout 2: disasm + separate writeback on same PC** (some builds)
```
core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0
core   0: 3 0x80000000 (0x00000297) x5  0x80000000
```
The parser uses the *(pc, instr) identity rule*: a second line with the same pc+instr is a **continuation** of the previous commit, not a new instruction.

**Memory access:**
```
core   0: 3 0x80000008 (0x00002223) mem 0x00000004 0x00000000
```

**CSR write:**
```
core   0: 3 0x80000100 (0x30200073) csr 0x300 0x00001800
```

**Exception:**
```
core   0: exception load_access_fault, epc 0x80000200
```
Attached as `trap` field to the preceding commit record.

---

## Output schema

Every record in `iss.commitlog.jsonl` conforms to `schemas/commitlog.schema.json`.

### Mandatory fields (always present)

| Field | Type | Example |
|-------|------|---------|
| `seq` | int | `0` |
| `pc` | string | `"0x80000000"` |
| `instr` | string | `"0x00000297"` |
| `source` | string | `"iss"` |

### Optional fields (present when applicable)

| Field | Present when |
|-------|--------------|
| `priv` | FORMAT B (privilege digit in log) |
| `disasm` | Disassembly available in log line |
| `reg_writes` | Register written at commit (x0 suppressed) |
| `csr_writes` | CSR written at commit |
| `mem_access` | Load or store executed |
| `trap` | Exception or interrupt taken |

---

## Run directory layout

```
<run_dir>/
  iss.commitlog.jsonl     ← primary deliverable
  manifest.json           ← run metadata, config, status, stats
  logs/
    iss.log               ← raw Spike stdout+stderr
```

`manifest.json` after a successful run:
```json
{
  "run_id": "a3f1...",
  "timestamp": "2025-01-15T10:30:00Z",
  "config": {"xlen": 32, "isa": "RV32IM", "priv": ["M"], "seed": 42},
  "inputs":  {"elf": "/path/to/prog.elf"},
  "outputs": {"iss_commitlog": "/path/to/run/iss.commitlog.jsonl"},
  "status": "pass",
  "iss_stats": {
    "commit_count": 47,
    "elapsed_s": 0.12,
    "log_mode": "log_commits",
    "spike_exit": 1,
    "spike_version": "Spike RISC-V ISA Simulator 1.1.0"
  }
}
```

---

## Wiring into AVA pipeline

Agent C sits between **Agent B (RTL runner)** and **Agent D (Comparator)**:

```
ELF ──┬──► Agent B (Verilator)  ──► rtl.commitlog.jsonl ──┐
      │                                                     ├──► Agent D (compare_commitlogs.py)
      └──► Agent C (run_iss.py) ──► iss.commitlog.jsonl ──┘
```

**Programmatic use:**

```python
from sim.run_iss import main as run_iss

rc = run_iss([
    "--isa", "RV32IM",
    "--elf", str(elf_path),
    "--out", str(run_dir),
    "--seed", str(seed),
])
assert rc == 0, "Spike golden run failed"
# Agent D now has both commitlogs in run_dir/
```

**Definition of done (per spec):**
> For the same ELF as the RTL runner, `iss.commitlog.jsonl` has matching
> instruction count in trivial tests.

Verify:
```bash
wc -l runs/run_001/rtl.commitlog.jsonl   # from Agent B
wc -l runs/run_001/iss.commitlog.jsonl   # from Agent C (this module)
# Both lines must match for deterministic bare-metal programs
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Spike not found` | Binary not on PATH | `export PATH=/opt/riscv/bin:$PATH` |
| `Zero records parsed` | ELF has no `tohost` symbol | Use `--pk` (proxy kernel) or add tohost via link.ld |
| `log_mode: trace_only` | Old Spike without `--log-commits` | Upgrade Spike; reg values unavailable |
| Spike exit code 1 | Normal for bare-metal tohost=1 (success) | Not an error |
| Spike exit code -9 | Timeout | Increase `--timeout` or `--max-instrs` |
| Count mismatch vs RTL | Trap handling difference | Check CSR init; compare first divergence with Agent D |

# AVA v2.0.0 — Agent Interface Contracts

> **Single source of truth.** All agents MUST conform to this document.  
> Do not keep local schema copies — import from `schemas/commitlog.schema.json`.

---

## 1. Commit log format (`schemas/commitlog.schema.json`)

Every backend writes **one JSON object per line** (JSONL) to `<rundir>/rtl.commitlog.jsonl`
or `<rundir>/iss.commitlog.jsonl`.

### Mandatory fields (every record)

| Field           | Type    | Example              | Notes                                           |
|-----------------|---------|----------------------|-------------------------------------------------|
| `schemaversion` | string  | `"2.0.0"`            | Always literal `"2.0.0"`.                       |
| `runid`         | string  | `"a1b2c3d4"`         | UUID from orchestrator manifest.                |
| `hart`          | integer | `0`                  | Hardware thread index.                          |
| `seq`           | integer | `0`                  | 0-based retire counter per hart.                |
| `pc`            | string  | `"0x80000000"`       | Hex, no zero-padding.                           |
| `instr`         | string  | `"0x00000013"`       | 32-bit word (16 for RVC).                       |
| `instrwidth`    | integer | `4`                  | Bytes: `4` or `2` (RVC).                        |
| `priv`          | string  | `"M"`                | `"M"` / `"S"` / `"U"` / `"H"`.                 |
| `src`           | string  | `"rtl"`              | `"rtl"` or `"iss"`.                             |

### Optional fields (omit if empty/null)

| Field       | Type            | Notes                                                       |
|-------------|-----------------|-------------------------------------------------------------|
| `regs`      | object          | `{"x5": "0x42"}` — only written registers. **Not `rd`.**   |
| `csrs`      | object          | `{"mstatus": "0x1800"}` — only updated CSRs. **Not `csr`.**|
| `fpregs`    | object \| null  | FP register writes; `null` when F/D absent.                 |
| `memwrites` | array           | Store transactions. **Not `mem`.**                          |
| `memreads`  | array           | Load transactions. Agent D uses these when both emit them.  |
| `trap`      | object          | See trap sub-schema below.                                  |

### `memwrites` / `memreads` item schema

```json
{ "addr": "0x80001000", "data": "0x0000002a", "size": 4, "strb": "0xf" }
```
`strb` is required for `memwrites`, omitted for `memreads`.

### `trap` sub-schema (ALL fields mandatory when `trap` is present)

| Field         | Type    | Notes                                              |
|---------------|---------|----------------------------------------------------|
| `cause`       | string  | `mcause` hex (bit 31 = interrupt flag).            |
| `epc`         | string  | `mepc` — PC saved on trap entry.                   |
| `tvec`        | string  | `mtvec` — handler address jumped to.               |
| `tval`        | string  | `mtval` hex.                                       |
| `isinterrupt` | boolean | `true` when `cause[31]=1`.                         |
| `is_ret`      | boolean | `true` for mret/sret/uret.                         |

---

## 2. Orchestrator manifest (`run_manifest.json`)

The **orchestrator** creates this file before invoking any agent.  
Agents **read** `seed`, `binary`, `dut`, `rundir` and **write only** their own `phases.*` / `outputs.*` sections. Atomic write: write to `.tmp` then rename.

### Orchestrator-written fields

```jsonc
{
  "schemaversion": "2.0.0",
  "runid":   "a1b2c3d4-...",         // UUID v4
  "seed":    42,
  "binary":  "tests/add_loop.elf",   // ELF path
  "dut":     "cpu_top",              // Verilator top module
  "rundir":  "runs/run_42",          // all output goes here
  "xlen":    32,
  "isa":     "rv32im",
  "phases":  {},                     // agents fill this in
  "outputs": {}                      // agents fill this in
}
```

### Agent B (RTL Runner) — writes back

```jsonc
{
  "phases": {
    "build": {
      "status":      "pass",          // "pass" | "fail" | "skip"
      "elapsed_sec": 5.21,
      "timestamp":   "2025-01-01T00:00:00Z"
    },
    "rtl": {
      "status":      "pass",
      "elapsed_sec": 1.08,
      "retired":     87,
      "cycles":      435,
      "timestamp":   "2025-01-01T00:00:01Z"
    }
  },
  "outputs": {
    "rtlcommitlog": "runs/run_42/rtl.commitlog.jsonl",
    "coverageraw":  "runs/run_42/coverage.dat",        // NOT rtl.coverage.dat
    "waveform":     "runs/run_42/rtl.fst",             // FST, not VCD
    "signature":    "runs/run_42/signature.hex",       // RISCOF sig (if --sig-out)
    "totalcycles":  435
  }
}
```

### Agent C (ISS) — writes back

```jsonc
{
  "phases": {
    "iss": { "status": "pass", "elapsed_sec": 0.04, "retired": 87 }
  },
  "outputs": {
    "isscommitlog": "runs/run_42/iss.commitlog.jsonl"
  }
}
```

### Agent D (Comparator) — writes back

```jsonc
{
  "phases": {
    "compare": { "status": "pass", "divergence_seq": null }
  },
  "outputs": {
    "bugreports": []
  }
}
```

### Agent F (Coverage) — reads

Agent F's `extract_coverage_from_run()` looks for `coverage.dat` (not `rtl.coverage.dat`) in `rundir`. Convention: `<rundir>/coverage.dat`.

---

## 3. Run directory layout

```
<rundir>/
├── run_manifest.json        # orchestrator-created; agents patch phases/outputs
├── rtl.commitlog.jsonl      # Agent B output
├── iss.commitlog.jsonl      # Agent C output
├── coverage.dat             # Verilator raw counters (Agent B) — NOT rtl.coverage.dat
├── coverage_report.json     # Parsed ratios {line, branch, toggle, functional}
├── rtl.fst                  # FST waveform (Agent B, optional)
├── signature.hex            # RISCOF signature (Agent B, optional)
├── bugs/
│   └── bug_<seq>.json
└── build/
    └── Vcpu_top             # Verilator binary
```

---

## 4. DUT commit-monitor port contract

Any RTL DUT plugged into the harness must expose **all** of these ports:

```verilog
// Clock / reset
input  wire        clk
input  wire        rst_n

// Memory bus (single-cycle synchronous req, combinational resp)
output wire        mem_req_valid
output wire        mem_req_we
output wire [31:0] mem_req_addr
output wire [31:0] mem_req_wdata
output wire [3:0]  mem_req_wstrb
input  wire [31:0] mem_resp_rdata
input  wire        mem_resp_ready     // harness always drives 1

// Commit monitor — one-cycle pulse per retired instruction
output wire        commit_valid
output wire [31:0] commit_pc
output wire [31:0] commit_instr
output wire [4:0]  commit_rd_addr     // 0 = no writeback
output wire [31:0] commit_rd_data
output wire        commit_rd_we
output wire [1:0]  commit_priv_mode   // 2'b11=M 2'b01=S 2'b00=U
output wire        commit_trap_valid
output wire [31:0] commit_trap_cause  // mcause
output wire [31:0] commit_trap_epc    // mepc (NEW in v2.0.0)
output wire [31:0] commit_trap_tvec   // mtvec (NEW in v2.0.0)
output wire [31:0] commit_trap_tval   // mtval
output wire        commit_is_mret
```

---

## 5. RISCOF signature format

`signature.hex` written by Agent B `--sig-out`:
- One 32-bit word per line, 8 lowercase hex digits, no `0x` prefix.
- Region: `mem[sig_begin .. sig_end)`, default `0x80002000..0x80002040`.
- Compatible with RISCOF `compare_signature()` directly.

```
deadbeef
00000055
0000006e
...
```

---

## 6. Field-name migration table (v1 → v2)

| Old field (v1) | New field (v2.0.0) | Notes                   |
|----------------|--------------------|-------------------------|
| `mode`         | `priv`             | Privilege mode string   |
| `rd`           | `regs`             | Register writes object  |
| `csr`          | `csrs`             | CSR writes object       |
| `mem`          | `memwrites`        | Store transactions      |
| *(absent)*     | `memreads`         | Load transactions (new) |
| *(absent)*     | `schemaversion`    | Always `"2.0.0"`        |
| *(absent)*     | `runid`            | From manifest           |
| *(absent)*     | `hart`             | Always `0` for RV32IM   |
| *(absent)*     | `instrwidth`       | Always `4` for RV32IM   |
| *(absent)*     | `fpregs`           | Always `null` for RV32IM|
| *(absent)*     | `src`              | `"rtl"` or `"iss"`      |
| `trap.is_ret`  | `trap.is_ret`      | Unchanged               |
| *(absent)*     | `trap.epc`         | mepc (NEW)              |
| *(absent)*     | `trap.tvec`        | mtvec (NEW)             |
| *(absent)*     | `trap.isinterrupt` | cause[31] (NEW)         |

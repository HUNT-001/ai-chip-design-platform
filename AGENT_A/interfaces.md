# AVA Interface Contracts — v2.0.0

**Platform:** Autonomous RISC-V Verification Architecture (AVA)  
**Scope:** RV32IM, M-mode only, single hart (multi-hart reserved in §12)  
**Schema version:** `2.0.0` (see §11 for versioning rules)  
**Authoritative schemas:** `schemas/commitlog.schema.json` · `schemas/run_manifest.schema.json`  
**Last updated:** 2024-03-15

---

## Table of Contents

1. [Guiding principles](#1-guiding-principles)
2. [The commit log (JSONL)](#2-the-commit-log-jsonl)
3. [Run manifest](#3-run-manifest)
4. [Run directory layout](#4-run-directory-layout)
5. [Agent ownership table](#5-agent-ownership-table)
6. [Comparison rules (Agent D)](#6-comparison-rules-agent-d)
7. [Coverage summary interface (Agent F)](#7-coverage-summary-interface-agent-f)
8. [Compliance / signature interface (Agent E)](#8-compliance--signature-interface-agent-e)
9. [Formal verification interface](#9-formal-verification-interface)
10. [Error handling and recovery protocols](#10-error-handling-and-recovery-protocols)
11. [Versioning and backward compatibility](#11-versioning-and-backward-compatibility)
12. [Multi-hart extension path](#12-multi-hart-extension-path)
13. [CI integration guide](#13-ci-integration-guide)
14. [Schema validation](#14-schema-validation)
15. [End-to-end example](#15-end-to-end-example)
16. [Troubleshooting](#16-troubleshooting)
17. [Glossary](#17-glossary)

---

## 1. Guiding principles

**P1 — Differential correctness is the ground truth.**  
Every component produces or consumes a commit log. The comparator (Agent D) is the sole arbiter of pass/fail. No other signal — waveform, coverage counter, simulation exit code — overrides a comparison result.

**P2 — x0 is structurally immutable.**  
x0 never appears in any `regs` delta. This is enforced at the schema level (`propertyNames` pattern excludes `x0`), at the producer level (harness code must never emit it), and at the comparator level (Agent D raises `X0_WRITTEN` and halts immediately). Any system that accepts an x0 write is non-conforming and its outputs are undefined.

**P3 — Logs are minimal but complete.**  
Only changed state is emitted per commit record. An empty `regs: {}` is valid and common. Completeness means: every retired instruction appears exactly once, in order, with every state change recorded. No instruction is skipped, even NOP, FENCE, or failed STORE-CONDITIONAL.

**P4 — Bit-exact reproducibility.**  
Given the same `seed` and `binary`, both the RTL harness and the ISS must produce commit logs whose diff is empty. Non-determinism anywhere in the pipeline is a bug to be fixed, not a tolerance to be designed around.

**P5 — Schema drift is a breaking change.**  
Any addition of a `required` field, change of type, removal of a field, or change of a `const` bumps the minor version. Any removal of a required field bumps the major version. Agents reject manifests and logs whose `schema_version` they were not compiled against.

**P6 — Agent boundaries are inviolable.**  
No agent writes to another agent's output fields. The ownership table in §5 is normative. Violations break the manifest as a coordination mechanism.

**P7 — All timestamps are UTC.**  
ISO-8601 with `Z` suffix. Sub-second precision is optional but allowed. No local time zones, no Unix timestamps at the JSON layer (Unix timestamps are fine in metrics fields).

**P8 — All hex values use lowercase with `0x` prefix.**  
`0x0000002a`, not `0X2A`, not `2a`, not `0x0000002A`. Schema patterns enforce this. Tools that output uppercase hex must run through a normalization filter before writing logs.

---

## 2. The commit log (JSONL)

### 2.1 Format

Each run produces two JSONL files — one record per line, UTF-8, LF line endings, no BOM, no trailing comma, no enclosing array.

```
<run_dir>/rtl_commit.jsonl   ←  Agent B
<run_dir>/iss_commit.jsonl   ←  Agent C
```

The authoritative schema is `schemas/commitlog.schema.json`. Every record must pass schema validation before the comparator reads it.

### 2.2 Field reference

| Field | Type | Required | RTL value | ISS value | Comparator action |
|-------|------|----------|-----------|-----------|-------------------|
| `schema_version` | string const `"2.0.0"` | ✓ | `"2.0.0"` | `"2.0.0"` | Must match schema |
| `hart` | integer 0–255 | ✓ | 0 | 0 | Must match |
| `seq` | integer ≥ 0 | ✓ | auto-increments from 0 | auto-increments from 0 | Must be gapless; checked before diff |
| `cycle` | integer or null | — | real cycle count | **null** | **Ignored** |
| `pc` | hex8 | ✓ | real PC | real PC | Must match exactly |
| `instr` | hex4 or hex8 | ✓ | real encoding | real encoding | Must match |
| `instr_width` | 2 or 4 | ✓ | 4 for RV32IM | 4 for RV32IM | Must match |
| `disasm` | string or absent | — | optional | optional | **Ignored** |
| `priv` | `"M"` | ✓ | `"M"` | `"M"` | Must match |
| `trap` | object or null | — | see §2.4 | see §2.4 | Checked: cause, tval, epc |
| `regs` | object (delta) | ✓ | delta only | delta only | Shadow-expanded then compared |
| `csrs` | object (delta) | ✓ | delta only | delta only | Shadow-expanded then compared |
| `mem_writes` | array or null | — | present for stores | present for stores | addr, size, data all checked |
| `mem_reads` | array or null | — | null unless enabled | null unless enabled | Compared only if both enabled |
| `fp_regs` | null | — | null | null | Must be null in v2 |
| `_src` | `"rtl"` / `"iss"` | ✓ | `"rtl"` | `"iss"` | **Stripped before hash** |
| `_run_id` | string | — | from manifest | from manifest | **Ignored** |

### 2.3 Ordering invariants

1. Records are in **strict retirement order**: seq 0, 1, 2, …, N-1.
2. **Every committed instruction appears exactly once.** There is no "skip on misaligned" or "skip on replay".
3. Both logs must have the **same record count** for a passing run. A count mismatch before any field comparison is a `LENGTH_MISMATCH` error.
4. For a run that terminates via the end-of-test sentinel (`tohost` write or trap to address `0xFFFFFFFF`), the sentinel instruction is the last record in both logs.
5. Producers must **flush and close** the JSONL file before the comparator is invoked. The orchestrator is responsible for sequencing; it must not invoke Agent D until both Agent B and Agent C have exited cleanly.

### 2.4 Trap record semantics

A trap record is emitted for the **instruction that causes the trap**, not for the first instruction of the trap handler.

- The instruction's normal architectural side-effects **may or may not** be committed depending on the processor implementation. Producers must document their behavior in `docs/producer_notes.md`. The comparator checks `regs` and `csrs` deltas on the trap record as it would any other record — RTL and ISS must agree.
- The trap handler's first instruction is the **next record** (seq+1), with `pc` equal to the trap handler entry.
- Interrupt handling: if an interrupt is taken between two instructions, the "interrupted" instruction still retires normally. The interrupt trap record is the **next** record, with `instr` being whatever instruction the handler begins at, and `trap.is_interrupt: true`.
- `mret` is not a trap — it retires normally with the usual CSR delta (`mepc`, `mstatus` changes appear in `csrs`).

### 2.5 Complete JSONL example

```jsonl
{"schema_version":"2.0.0","hart":0,"seq":0,"cycle":10,"pc":"0x80000000","instr":"0x00000513","instr_width":4,"disasm":"li a0, 0","priv":"M","trap":null,"regs":{"x10":"0x00000000"},"csrs":{},"mem_writes":null,"mem_reads":null,"fp_regs":null,"_src":"rtl","_run_id":"20240315T142300Z_42_ab3f9c01"}
{"schema_version":"2.0.0","hart":0,"seq":1,"cycle":14,"pc":"0x80000004","instr":"0x02a50533","instr_width":4,"disasm":"mul a0, a0, a1","priv":"M","trap":null,"regs":{"x10":"0x0000002a"},"csrs":{},"mem_writes":null,"mem_reads":null,"fp_regs":null,"_src":"rtl","_run_id":"20240315T142300Z_42_ab3f9c01"}
{"schema_version":"2.0.0","hart":0,"seq":2,"cycle":18,"pc":"0x80000008","instr":"0x00a12023","instr_width":4,"disasm":"sw a0, 0(sp)","priv":"M","trap":null,"regs":{},"csrs":{},"mem_writes":[{"addr":"0x80001000","size":4,"data":"0x0000002a"}],"mem_reads":null,"fp_regs":null,"_src":"rtl","_run_id":"20240315T142300Z_42_ab3f9c01"}
{"schema_version":"2.0.0","hart":0,"seq":3,"cycle":22,"pc":"0x8000000c","instr":"0x02054533","instr_width":4,"disasm":"div a0, a0, zero","priv":"M","trap":null,"regs":{"x10":"0xffffffff"},"csrs":{},"mem_writes":null,"mem_reads":null,"fp_regs":null,"_src":"rtl","_run_id":"20240315T142300Z_42_ab3f9c01"}
{"schema_version":"2.0.0","hart":0,"seq":4,"cycle":26,"pc":"0x80000010","instr":"0x00000073","instr_width":4,"disasm":"ecall","priv":"M","trap":{"cause":"0x0000000b","tval":"0x00000000","tvec":"0x80000080","epc":"0x80000010","is_interrupt":false,"exception_code":11},"regs":{},"csrs":{"mepc":"0x80000010","mcause":"0x0000000b","mtval":"0x00000000","mstatus":"0x00001800"},"mem_writes":null,"mem_reads":null,"fp_regs":null,"_src":"rtl","_run_id":"20240315T142300Z_42_ab3f9c01"}
```

---

## 3. Run manifest

### 3.1 Location

```
<run_dir>/manifest.json
```

### 3.2 Lifecycle state machine

```
                    ┌──────────┐
              ┌────►│ pending  │
              │     └────┬─────┘
              │          │ orchestrator writes manifest
              │          ▼
              │     ┌──────────┐
              │     │ building │◄──── Agent B compiles RTL
              │     └────┬─────┘
              │          │ build OK
              │     ┌────▼──────────┐      ┌─────────────┐
              │     │ running_iss   │      │ build_error │◄── non-zero exit
              │     └────┬──────────┘      └─────────────┘
              │          │ ISS exits 0
              │     ┌────▼──────────┐      ┌─────────────┐
              │     │ running_rtl   │      │  iss_crash  │  (via infra_error)
              │     └────┬──────────┘      └─────────────┘
              │          │ RTL exits 0
              │     ┌────▼──────────┐      ┌─────────┐
              │     │  comparing    │      │ timeout │◄── cycles > limit
              │     └────┬──────────┘      └─────────┘
              │          │
              │   ┌───────▼────────┐
              │   │ analyzing_cov  │ (parallel with compare)
              │   └───────┬────────┘
              │           │ all phases done
              │     ┌─────▼──────┐   ┌────────┐
              └─────│   passed   │   │ failed │◄── comparator found mismatch
       retry        └────────────┘   └────────┘
  (recoverable
    errors only)
```

**Terminal states** (orchestrator must not attempt transitions out of these):  
`passed` · `failed` · `timeout` · `build_error` · `infra_error` · `cancelled`

**State transition rules:**

- Any agent that encounters an unexpected error must write `status: "infra_error"` and populate `error.*` before exiting.
- Transitions must be made atomically using a file lock on `manifest.json`.
- The orchestrator is the only process permitted to write `status: "pending"`, `"building"`, or terminal states. Individual agents write intermediate states for their own phase.

### 3.3 Manifest write protocol

```
LOCK manifest.json (flock or equivalent)
  read current manifest
  validate against schema
  apply field updates
  validate result against schema
  write atomically (write to manifest.json.tmp, rename)
UNLOCK
```

Never write partial manifests. A partially-written manifest will fail schema validation and cause all downstream agents to abort with `SCHEMA_INVALID`.

---

## 4. Run directory layout

```
<run_dir>/
│
├── manifest.json               # Run manifest (read/written by orchestrator + agents)
├── manifest.json.lock          # Advisory file lock (created by agents, deleted on unlock)
│
├── test.elf                    # Test binary (produced by Agent G)
│
├── rtl_commit.jsonl            # RTL commit log (produced by Agent B)
├── iss_commit.jsonl            # ISS commit log (produced by Agent C)
│
├── coverage.dat                # Verilator raw coverage database (produced by Agent B)
├── coverage_summary.json       # Parsed coverage summary (produced by Agent F)
│
├── bug_report.json             # Bug report if mismatch (produced by Agent D; absent if pass)
│
├── signatures/                 # RISCOF DUT signature files (produced by Agent E)
│   ├── rv32im-add-01.sig
│   └── ...
│
├── sim.fst                     # Waveform dump (produced by Agent B; optional)
│
├── build/                      # Compiled Verilator model
│   ├── Vcore_tb                # Compiled simulator executable
│   └── ...
│
└── logs/
    ├── build.log               # Verilator compilation stdout/stderr
    ├── rtl_sim.log             # RTL simulation stdout/stderr
    ├── iss_run.log             # ISS stdout/stderr
    ├── compare.log             # Agent D output
    ├── coverage.log            # Agent F output
    └── compliance.log          # Agent E output (compliance runs only)
```

**Rules:**
- All paths stored in the manifest are relative to `run_dir`.
- No agent creates files outside its `run_dir`. Cross-run references use `run_id` in the manifest, not filesystem paths.
- `build/` may be shared across runs via a build cache keyed on `dut.build_cache_key` (see manifest schema). The orchestrator is responsible for cache invalidation.

---

## 5. Agent ownership table

Each agent owns exactly the manifest fields and output files listed here. Writing to another agent's fields without being that agent is a contract violation.

| Agent | Reads from manifest | Writes to manifest | Produces files |
|-------|--------------------|--------------------|----------------|
| **Orchestrator** | all | `run_id`, `status`, `created_at`, `completed_at`, `environment`, `isa.isa_string` | `manifest.json` |
| **A (Architect)** | — | — | `schemas/*.json`, `docs/interfaces.md` |
| **B (RTL harness)** | `seed`, `binary`, `dut`, `memory_map`, `run_dir` | `phases.build`, `phases.rtl`, `outputs.rtl_commitlog`, `outputs.coverage_raw`, `outputs.waveform`, `outputs.total_cycles` | `rtl_commit.jsonl`, `coverage.dat`, `sim.fst` (optional) |
| **C (ISS backend)** | `seed`, `binary`, `iss`, `memory_map`, `run_dir` | `phases.iss`, `outputs.iss_commitlog`, `outputs.total_instrs` | `iss_commit.jsonl` |
| **D (Comparator)** | `outputs.rtl_commitlog`, `outputs.iss_commitlog` | `phases.compare`, `outputs.bug_report`, `outputs.first_divergence`, `status`, `error` | `bug_report.json` (on failure) |
| **E (Compliance)** | `binary`, `dut`, `iss`, `compliance`, `run_dir` | `phases.compliance`, `outputs.signature_dir`, `compliance.result`, `status` | `signatures/*.sig` |
| **F (Coverage)** | `outputs.coverage_raw`, `dut.coverage_config` | `phases.coverage`, `outputs.coverage_summary`, `metrics.coverage_*_pct` | `coverage_summary.json` |
| **G (Test gen)** | `isa`, `seed` | `binary.*` | `test.elf` |
| **Red-team (Phase 2)** | all outputs, coverage summary | `parent_run_id`, `tags`, `run_type` (for spawned runs) | spawns new manifests |

---

## 6. Comparison rules (Agent D)

### 6.1 Pre-flight checks

Before opening the log files, Agent D must:

1. Verify `manifest.status` is `"running_rtl"` or later — abort with `INFRA_UNKNOWN` if still `"running_iss"`.
2. Verify both log files exist on disk and are non-empty.
3. Validate the first record of each log against `schemas/commitlog.schema.json`. If either record fails validation, raise `SCHEMA_INVALID` and write a bug report containing the offending record.
4. Confirm `_run_id` in both logs matches `manifest.run_id` (if present).
5. Confirm `seq=0` is the first record in both logs. If not, raise `SEQ_GAP`.

### 6.2 Shadow state

Agent D maintains a shadow state for each hart (in Phase 1, just hart 0):

```python
shadow = {
    "regs": {f"x{i}": 0 for i in range(32)},  # x0 fixed at 0 always
    "csrs": { ... reset values per spec ... }
}
```

On each record:
1. Apply `regs` delta to `shadow["regs"]`. If any key is `"x0"`, immediately raise `X0_WRITTEN`.
2. Apply `csrs` delta to `shadow["csrs"]`.
3. Compare RTL shadow vs ISS shadow for all modified registers and CSRs.

Applying deltas before comparing ensures that a register written and re-read within the same instruction is compared at its *post-instruction* value.

### 6.3 Mismatch classification

| Code | Trigger condition |
|------|------------------|
| `PC_MISMATCH` | `rtl[n].pc != iss[n].pc` |
| `INSTR_MISMATCH` | PCs match but `rtl[n].instr != iss[n].instr` |
| `REG_MISMATCH` | Shadow register file differs after applying both deltas |
| `CSR_MISMATCH` | Shadow CSR map differs after applying both deltas |
| `MEM_MISMATCH` | `mem_writes` differ in addr, size, or data |
| `TRAP_MISMATCH` | One side has `trap != null`, the other has `trap == null`; or `cause`/`tval`/`epc` differ |
| `LENGTH_MISMATCH` | `len(rtl_log) != len(iss_log)` |
| `SEQ_GAP` | `seq[n+1] != seq[n] + 1` in either log |
| `X0_WRITTEN` | `"x0"` appears in any `regs` delta |
| `ALIGNMENT_ERROR` | PC is not 2-byte aligned (or not 4-byte aligned when `instr_width=4`) |
| `SCHEMA_INVALID` | Record fails JSON schema validation |
| `BINARY_HASH_MISMATCH` | `binary.sha256` does not match actual file hash |

**First-failure semantics:** Agent D stops at the first mismatch, records the seq index, and writes the bug report. It does not attempt to re-synchronize and continue — doing so produces spurious cascading mismatches that mislead triage.

### 6.4 Bug report format

`bug_report.json`:

```json
{
  "schema_version":        "2.0.0",
  "run_id":                "<string>",
  "mismatch_class":        "REG_MISMATCH",
  "first_divergence_seq":  47,
  "context_window_before": 5,
  "context_window_after":  5,
  "rtl_context": [ /* 11 records from rtl log centered on seq=47 */ ],
  "iss_context": [ /* 11 records from iss log centered on seq=47 */ ],
  "details": {
    "register":  "x10",
    "rtl_value": "0x0000002b",
    "iss_value": "0x0000002a"
  },
  "shadow_at_divergence": {
    "rtl": { "regs": { ... }, "csrs": { ... } },
    "iss": { "regs": { ... }, "csrs": { ... } }
  },
  "repro_cmd": "python tools/run_rtl.py --manifest <run_dir>/manifest.json"
}
```

Agent D writes `bug_report.json` to `run_dir`, sets `outputs.bug_report = "bug_report.json"`, sets `outputs.first_divergence = 47`, sets `status = "failed"`, and exits with code 1.

### 6.5 Fields explicitly ignored during comparison

`cycle` · `disasm` · `_src` · `_run_id`

Any field not listed in §2.2 under "Must match" is ignored.

---

## 7. Coverage summary interface (Agent F)

### 7.1 Input

Agent F reads `manifest.dut.coverage_config.output_file` (Verilator `.dat` file) using `verilator_coverage`.

### 7.2 Output: `coverage_summary.json`

```json
{
  "schema_version": "2.0.0",
  "run_id":         "<string>",
  "tool":           "verilator",
  "tool_version":   "5.014",
  "line":       { "hit": 1420, "total": 1600, "pct": 88.75 },
  "branch":     { "hit":  310, "total":  400, "pct": 77.50 },
  "toggle":     { "hit": 2100, "total": 2800, "pct": 75.00 },
  "expression": { "hit":    0, "total":    0, "pct": 0.0 },
  "functional": {
    "mul_executed":         true,
    "mulh_signed":          false,
    "mulhsu_executed":      false,
    "mulhu_unsigned":       false,
    "div_executed":         true,
    "div_by_zero":          true,
    "div_overflow_int_min": false,
    "rem_executed":         true,
    "rem_by_zero":          true,
    "divu_executed":        false,
    "remu_executed":        false,
    "trap_ecall":           true,
    "trap_ebreak":          false,
    "trap_load_fault":      false,
    "trap_store_fault":     false,
    "trap_misalign_load":   false,
    "trap_misalign_store":  false,
    "mret_executed":        true,
    "csr_write_mstatus":    true,
    "csr_write_mtvec":      false
  },
  "cold_paths": [
    { "module": "rtl/alu.sv", "line": 142, "type": "branch", "description": "DIV overflow path never hit" },
    { "module": "rtl/trap.sv", "line": 88,  "type": "line",   "description": "misaligned load handler" }
  ],
  "generated_at": "2024-03-15T14:23:47Z"
}
```

**Rules:**
- `functional` keys must never be absent — use `false` for not-yet-observed events. `null` is not a valid functional coverage value.
- `cold_paths` is the primary feed to `CoverageDirector.adapt_cold_paths()`. Each entry provides enough context (module, line, type, description) for the director to generate targeted tests.
- `pct` values are rounded to 2 decimal places.
- Agent F must not write partial files — write to `coverage_summary.json.tmp` and rename atomically.

---

## 8. Compliance / signature interface (Agent E)

### 8.1 Signature file format

RISCOF/riscv-arch-test standard:
- One 32-bit word per line.
- **No `0x` prefix.** 8 lowercase hex digits, zero-padded. Example: `0000002a`.
- Lines correspond to consecutive words in the signature memory region, starting at `begin_signature` and ending before `end_signature`.
- Trailing newline required.

```
deadbeef
0000002a
00000000
cafef00d
```

Agent E extracts the signature region from DUT output memory by reading the ELF symbols `begin_signature` and `end_signature`.

### 8.2 Comparison protocol

1. Extract DUT signature from RTL simulation memory dump.
2. Run ISS on the same binary to produce reference signature.
3. Diff word-by-word. First mismatch is recorded as `compliance.result.failed_list[0]`.
4. Report pass/fail per test and aggregate.

### 8.3 RISCOF plugin integration

When `compliance.framework = "riscof"`, Agent E invokes:

```bash
riscof run \
  --config riscof_config.ini \
  --suite <compliance.suite_path> \
  --env   <compliance.suite_path>/env \
  2>&1 | tee logs/compliance.log
```

The DUT plugin at `compliance.dut_plugin` must implement `runTests()` to:
1. Compile and run the RTL simulator.
2. Dump memory contents to a `.sig` file.
3. Return the `.sig` path.

---

## 9. Formal verification interface

For `run_type = "formal"`, Agent D is replaced by the formal tool. The manifest `formal.properties` array defines the SVA assertions to check. Results are written back into `formal.result` and `formal.properties[*].result`.

**Mandatory property set for RV32IM M-mode:**

| Property name | Assertion |
|--------------|-----------|
| `x0_never_written` | `always (reg_write && (rd == 5'b0)) == 1'b0` |
| `pc_always_aligned` | `always (pc[1:0] == 2'b00)` |
| `mstatus_mpp_valid` | `always mstatus.MPP inside {2'b11}` (M-only) |
| `csr_writes_stable` | `always @(posedge clk) mstatus_we → (mstatus_new != 32'hX)` |
| `trap_mepc_equals_pc` | `always trap → (mepc == pc_of_trapping_instr)` |
| `div_result_spec` | `(divisor == 0) → (quotient == 32'hFFFFFFFF) && (remainder == dividend)` |

A formal run that returns any `cex_found` result sets `status = "failed"` and `error.code = "FORMAL_CEX"`.

---

## 10. Error handling and recovery protocols

### 10.1 Error codes and recommended actions

| Code | Phase | Recoverable | Recommended action |
|------|-------|-------------|-------------------|
| `PC_MISMATCH` | compare | No | File bug; reduce seed to minimal reproducer; bisect RTL commits |
| `REG_MISMATCH` | compare | No | File bug; check M-extension ALU (MUL/DIV); inspect shadow state dump |
| `CSR_MISMATCH` | compare | No | Check trap/return CSR update sequence; compare against spec |
| `MEM_MISMATCH` | compare | No | Check store alignment, forwarding, and write-back logic |
| `TRAP_MISMATCH` | compare | No | Compare mcause/mepc update logic in RTL vs Spike behavior |
| `LENGTH_MISMATCH` | compare | No | Check end-of-test sentinel; DUT may have halted early or looped |
| `SEQ_GAP` | compare | No | Producer bug — harness dropped a record; check flush logic |
| `X0_WRITTEN` | compare | No | Critical RTL bug — register file write-back gating for rd=0 |
| `ALIGNMENT_ERROR` | compare | No | Check branch target computation; PC update logic |
| `SCHEMA_INVALID` | compare/validate | No | Producer harness bug — fix emit code; re-run |
| `BINARY_HASH_MISMATCH` | pre-flight | Yes (1x) | Re-copy binary; check disk/NFS corruption |
| `BUILD_FAILED` | build | Yes (up to `max_retries`) | Check RTL syntax errors; common on first run of new RTL |
| `ISS_CRASH` | iss | Yes (up to `max_retries`) | Check ISS flags; Spike memory map mismatch is common |
| `RTL_CRASH` | rtl | No | Segfault in Verilator model — likely uninitialized register or X-state escape |
| `ISS_TIMEOUT` | iss | No | ISS in infinite loop — binary likely has no termination condition |
| `RTL_TIMEOUT` | rtl | No | DUT stalled — check for deadlock in pipeline |
| `DISK_FULL` | any | Yes (after cleanup) | Clean stale run directories; alert on-call |
| `OOM` | any | Yes (with lower parallelism) | Reduce parallel run count; profile peak memory |
| `INFRA_UNKNOWN` | any | Yes (up to `max_retries`) | Check host health; escalate if retry fails |

### 10.2 Retry protocol

```python
for attempt in range(manifest.retry_policy.max_retries + 1):
    result = run_phase(phase)
    if result.exit_code == 0:
        break
    if not is_recoverable(result.error_code):
        set_status("failed")
        break
    if attempt < manifest.retry_policy.max_retries:
        manifest.retry_policy.retry_count += 1
        time.sleep(manifest.retry_policy.backoff_s * (2 ** attempt))  # exponential backoff
    else:
        set_status("infra_error")
```

### 10.3 Partial failure handling

If the ISS phase fails but the RTL phase succeeds (or vice versa), the comparator cannot run. The orchestrator must:
1. Set `status = "infra_error"` (not "failed" — a partial log is not evidence of a RTL bug).
2. Populate `error.phase` with the failed phase name.
3. Preserve all partial outputs for debugging.
4. Do **not** run Agent D on partial logs.

### 10.4 Disk safety

All multi-kilobyte files must be written atomically:
1. Write to `<target>.tmp` in the same directory.
2. Rename `<target>.tmp` → `<target>` (POSIX rename is atomic on the same filesystem).
3. Never leave a `.tmp` file on crash — orchestrator cleanup sweeps remove them on startup.

Agent B must flush the JSONL log every `commit_log_config.flush_every` records to bound data loss. A simulation that crashes after 500,000 instructions with `flush_every=1000` loses at most 999 records, not the entire run.

---

## 11. Versioning and backward compatibility

### 11.1 Version string format

`MAJOR.MINOR.PATCH`

| Change | Version bump |
|--------|-------------|
| Remove a required field | **MAJOR** |
| Add a new required field | **MINOR** |
| Change a field's type or valid values | **MINOR** |
| Add a new optional field | PATCH |
| Fix a bug in a pattern/description without changing semantics | PATCH |

### 11.2 Producer rules

- Every record and every manifest must include `schema_version` set to the exact version the producer was compiled against.
- Producers must never emit `schema_version` values they were not compiled against.

### 11.3 Consumer rules

- Consumers must reject records/manifests whose `schema_version` major version differs from their compiled version.
- Consumers must warn (but may accept) records whose minor version is higher than their compiled version — the record may contain fields the consumer does not know about.
- Consumers must never silently drop unknown fields — they must log a warning.

### 11.4 Migration

When bumping to a new MINOR version:
1. Update both schema files and bump `$id` and all `const` version fields.
2. Update all producer code (Agents B and C) to emit the new version.
3. Update Agent D's pre-flight version check.
4. Update this document's header.
5. Tag the git commit as `schema-v{MAJOR}.{MINOR}.{PATCH}`.
6. Add a `CHANGELOG.md` entry describing the change.

---

## 12. Multi-hart extension path

Multi-hart support is **reserved for Phase 2** and must not be implemented against this v2.0.0 contract. This section documents the intended extension to prevent incompatible interim designs.

**Commit log changes (v3.0.0):**
- `hart` values 1..N become valid.
- Records from multiple harts are interleaved in a single JSONL file in global cycle order (requires `cycle` to be non-null for all RTL records).
- Agent D sorts by `(hart, seq)` before comparison; it maintains separate shadow states per hart.

**Manifest changes (v3.0.0):**
- `isa.num_harts: integer ≥ 1` added as required field.
- `memory_map` gains `hart_mask` per region for NUMA-style partitioning.

**Compatibility note:** Single-hart runs must continue to work unchanged in v3.0.0. The `hart=0` + `seq` monotonic contract is preserved.

---

## 13. CI integration guide

### 13.1 Exit codes

All AVA tools must use the following exit codes:

| Code | Meaning |
|------|---------|
| 0 | Phase completed successfully |
| 1 | Logical failure (mismatch, compliance fail, formal CEX) — file a bug |
| 2 | Infrastructure error (build failed, disk full, tool crash) — retry |
| 3 | Configuration error (bad manifest, schema invalid) — fix config |
| 130 | Killed by signal (SIGINT/SIGTERM) — treat as cancelled |

CI pipelines must treat exit code 1 as a blocking failure, code 2 as a retry-eligible failure, and code 3 as a pipeline configuration error requiring human review.

### 13.2 Minimal CI pipeline

```yaml
# Example: GitHub Actions
jobs:
  verify:
    steps:
      - name: Validate schemas
        run: python tools/validate_schemas.py

      - name: Generate tests
        run: python tools/gen_test.py --seed ${{ github.run_number }} --count 5000

      - name: Run ISS
        run: python tools/run_iss.py --manifest manifest.json

      - name: Run RTL
        run: python tools/run_rtl.py --manifest manifest.json
        timeout-minutes: 10

      - name: Compare
        run: python tools/compare_commitlogs.py --manifest manifest.json

      - name: Coverage report
        run: python tools/parse_coverage.py --manifest manifest.json

      - name: Upload artifacts
        if: always()
        uses: actions/upload-artifact@v3
        with:
          path: |
            manifest.json
            bug_report.json
            coverage_summary.json
```

### 13.3 Parallelism

Runs are independent by `run_id`. The orchestrator may launch N runs in parallel. Recommended parallelism: `min(cpu_count // 4, 8)` for RTL simulation (Verilator is multi-threaded); ISS runs are fast enough to parallelize more aggressively.

Do not share `run_dir` between runs. Do share the compiled Verilator binary via `build_cache_key`.

---

## 14. Schema validation

### 14.1 Validating commit log records

```bash
# Validate every record in a JSONL file
python -c "
import json, jsonschema, sys
schema = json.load(open('schemas/commitlog.schema.json'))
validator = jsonschema.Draft202012Validator(schema)
for i, line in enumerate(open(sys.argv[1])):
    record = json.loads(line)
    errors = list(validator.iter_errors(record))
    if errors:
        print(f'Record {i}: {errors[0].message}')
        sys.exit(3)
print(f'All {i+1} records valid.')
" rtl_commit.jsonl
```

### 14.2 Validating the run manifest

```bash
python -c "
import json, jsonschema
schema   = json.load(open('schemas/run_manifest.schema.json'))
manifest = json.load(open('manifest.json'))
jsonschema.validate(manifest, schema, cls=jsonschema.Draft202012Validator)
print('Manifest valid.')
"
```

### 14.3 Pre-commit hook

Add to `.git/hooks/pre-commit`:

```bash
#!/bin/bash
python tools/validate_schemas.py schemas/commitlog.schema.json schemas/run_manifest.schema.json
if [ $? -ne 0 ]; then
  echo "Schema validation failed. Fix before committing."
  exit 1
fi
```

---

## 15. End-to-end example

```bash
SEED=42
RUNDIR=/tmp/ava_runs/${SEED}

# 1. Generate test binary (Agent G)
python tools/gen_test.py \
  --isa rv32im --seed $SEED --count 4096 \
  --out ${RUNDIR}/test.elf

# 2. Write run manifest (orchestrator)
python tools/make_manifest.py \
  --seed $SEED \
  --binary ${RUNDIR}/test.elf \
  --run-dir ${RUNDIR} \
  --run-type differential \
  --out ${RUNDIR}/manifest.json

# 3. Validate manifest
python tools/validate_manifest.py ${RUNDIR}/manifest.json

# 4. Run ISS (Agent C)
python tools/run_iss.py --manifest ${RUNDIR}/manifest.json
# → writes iss_commit.jsonl, updates phases.iss in manifest

# 5. Build + run RTL (Agent B)
python tools/run_rtl.py --manifest ${RUNDIR}/manifest.json
# → writes rtl_commit.jsonl, coverage.dat, updates phases.build + phases.rtl

# 6. Compare (Agent D)
python tools/compare_commitlogs.py --manifest ${RUNDIR}/manifest.json
# exit 0 → "PASS: 4096 instructions matched"
# exit 1 → writes bug_report.json, sets status=failed

# 7. Parse coverage (Agent F)
python tools/parse_coverage.py --manifest ${RUNDIR}/manifest.json
# → writes coverage_summary.json, updates manifest metrics

# 8. Inspect results
jq '.status, .metrics.coverage_line_pct, .outputs.first_divergence' \
  ${RUNDIR}/manifest.json
# → "passed", 88.75, null

# On failure — inspect bug report:
# jq '.mismatch_class, .details, .repro_cmd' ${RUNDIR}/bug_report.json
```

---

## 16. Troubleshooting

### Spike produces no commit log

Spike version < 1.0.0 does not support `--log-commits`. Check `iss.version` in the manifest and upgrade, or use `--enable-commitlog` for intermediate versions. Run `spike --help | grep commit` to confirm the flag name.

### Verilator coverage.dat is empty

The DUT top-level module must be compiled with `--coverage`. Check that `dut.coverage_config.enabled: true` is in the manifest and that the Verilator flags in `dut.verilator_flags` do not accidentally override it. Run `verilator --version` and confirm ≥ 4.200 (coverage toggle API changed in 4.200).

### LENGTH_MISMATCH but run completed

The most common cause is a missing end-of-test sentinel. The test binary must write a non-zero value to the `tohost` address and the RTL harness must stop logging at that point. If the ISS exits via ECALL but the RTL harness runs until `timeout_cycles`, the logs will have different lengths. Check the test binary's termination sequence.

### REG_MISMATCH on x10 after DIV

Verify the divide-by-zero semantics: RV32IM spec (§M-extension) requires `DIV x, x, x0 → -1 (0xFFFFFFFF)`, `REM x, x, x0 → dividend`, `DIVU x, x, x0 → 0xFFFFFFFF`, `REMU x, x, x0 → dividend`. If the RTL ALU returns 0 or throws a trap, that is a bug. The ISS (Spike) implements the spec correctly and is the reference.

### SCHEMA_INVALID on CSR name

Only CSR names in the `csr_name` enum in `commitlog.schema.json` are accepted. If the RTL harness emits a non-standard CSR name (e.g. `"0x300"` instead of `"mstatus"`), it must be normalized by the harness wrapper before writing the JSONL line.

### Manifest write collisions in parallel runs

Each run has its own `run_dir` and `manifest.json`. Parallel runs never share a manifest. If you see collision errors, the orchestrator has a bug in its directory-naming logic — check the `run_id` uniqueness guarantee.

### `binary.sha256` mismatch after NFS mount

The binary was modified between when the orchestrator computed the hash and when the agent loaded it. This is an infrastructure problem (NFS caching, concurrent write). Set `retry_policy.max_retries: 1` and `retry_policy.retry_on_timeout: false`. If it persists, use a local disk for run directories.

---

## 17. Glossary

| Term | Definition |
|------|-----------|
| **Commit / retire** | The point at which an instruction's architectural state changes become visible. In an in-order pipeline, this is when the instruction exits the execute stage. In an OoO pipeline, this is when it exits the reorder buffer. Both RTL and ISS log at this point. |
| **Delta** | The set of state changes produced by a single committed instruction. Only changed registers/CSRs appear in a delta. |
| **Shadow state** | Agent D's in-memory copy of the architectural register file and CSR map, updated by applying deltas from each log in turn. Used to detect accumulated divergence even when individual deltas look correct. |
| **Seq** | The per-hart monotonic retirement sequence number. Starts at 0. Provides total order and detects log truncation. |
| **JSONL** | JSON Lines: one complete JSON object per line, UTF-8, LF endings. |
| **Sentinel** | A magic value written to the `tohost` MMIO address by the test binary to signal completion. The harness stops logging when it detects this write. |
| **Cold path** | A coverage hole — a branch, line, or toggle that has not been exercised by the current test suite. Reported in `coverage_summary.cold_paths` and consumed by the Coverage Director to generate targeted tests. |
| **RISCOF** | RISC-V Compliance Framework. Orchestrates compliance test compilation, execution, and signature comparison. Agent E implements the DUT RISCOF plugin. |
| **Signature** | A fixed-size memory dump written by a compliance test, compared word-by-word against the reference ISS output to verify instruction semantics. |
| **mret** | Machine-mode return instruction. Restores PC from mepc and privilege mode from mstatus.MPP. Not a trap — logged as a normal instruction with CSR deltas. |
| **tohost** | A memory-mapped register (typically at 0x80001000 in RISC-V bare-metal test environments) used to communicate test completion or failure status to the simulator host. |

---

*End of AVA Interface Contracts v2.0.0. All changes require a schema version bump, a CHANGELOG entry, and simultaneous update of all agent implementations and this document.*

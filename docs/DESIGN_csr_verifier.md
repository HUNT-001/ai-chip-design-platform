# Design & Build Plan — T25 Zicsr / Zifencei Semantics Verifier

**Status:** Implemented & tested (AVA v2.3.0, 2026-06-26)
**Module:** `AGENT_H/csr_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

CSR bugs are subtle and high-impact: a control register that latches the wrong
value, a read-only register that silently accepts a write, or an atomic
set/clear that disturbs bits it shouldn't can corrupt privilege state,
interrupt masking and trap delegation without diverging a general-purpose
register until much later. The temporal checker only verifies that a written
CSR becomes *visible* (`CsrReadAfterWrite`); nothing verified that the
read-modify-write was *correct*. This agent closes that gap, and adds a basic
Zifencei (`FENCE.I`) instruction-stream-sync check.

---

## 2. Golden model — recovering `old` from the write-back

The trick that makes single-record checking possible: a CSR instruction writes
the **old** CSR value into `rd`. So the verifier reads `old` straight from
`record["regs"][rd]`, even on the first access to that CSR, and then checks the
post-state (`record["csrs"][name]`) against `f(old, operand)`:

```
CSRRW rd, csr, rs1   csr ← rs1            rd ← old
CSRRS rd, csr, rs1   csr ← old | rs1      rd ← old   (rs1=x0 ⇒ no write)
CSRRC rd, csr, rs1   csr ← old & ~rs1     rd ← old   (rs1=x0 ⇒ no write)
*I variants          operand = zimm[4:0]
```

`operand` comes from a shadow register file (folded from each record's
write-back, exactly like the atomics verifier) for register variants, or from
the parsed immediate for the `*I` variants. A shadow CSR file carries the
authoritative post-state forward so later reads can be cross-checked.

The decoder handles the real instructions plus the common pseudo-instructions a
disassembler emits: `csrr`, `csrw`, `csrs`, `csrc`, `csrwi/si/ci`.

---

## 3. Checks

| Check | Severity | Catches |
|---|---|---|
| `csr_writeback` | HIGH | post-state CSR ≠ `op(old, operand)` |
| `csr_spurious_write` | HIGH | CSRRS/CSRRC with an x0 source changed the CSR |
| `csr_readonly_write` | HIGH | write to a read-only CSR with no illegal-instr trap |
| `csr_read_value` | MEDIUM | read-back old value disagrees with the tracked model |
| `fencei_missing` | LOW | executing a just-written code word with no `FENCE.I` |

**Read-only detection** uses a curated CSR table (machine/supervisor/user
registers with addresses + access) *and* the read-only-by-encoding rule
(address bits `[11:10] == 0b11`), so raw hex CSR operands are handled too.

---

## 4. Report & integration

`run()` returns the standard schema v2.1.0 report
(`schema_version, agent, records_checked, csr_ops, stats, total_violations,
severity_score, band, pass, violations[]`), band `CLEAN→CRITICAL` (any HIGH ⇒
CRITICAL). Wired into `_run_extended_pipeline` (`_csr` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `csr_report.json`, records `reports["csr"]`)
and exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/csr_verifier.py --rtl rtl_commit.jsonl`.

---

## 5. Test coverage

`tests/test_agents.py::TestCSRVerifier` — 12 cases: decode (incl. pseudos &
immediates), read-only table, clean CSRRW / CSRRS, write-back bug, read-only
write, spurious write on x0, rd old-value mismatch, FENCE.I missing, report
schema, manifest round-trip.

**Full suite: 85 passed**, `compileall` clean.

---

## 6. Limitations / next steps

- WARL/WPRI field masking is not modelled per-CSR (e.g. `misa` legal subsets,
  `mstatus` reserved bits). Add a field-mask layer to turn `csr_writeback` into
  a field-aware check and catch illegal WARL writes.
- Privilege-level access faults (accessing an M-mode CSR from U-mode) need a
  current-privilege field in the commit record; the table already stores the
  encoded privilege bits to support this later.
- The `FENCE.I` check is intentionally conservative (LOW severity); a fuller
  model would track the instruction-cache contents.

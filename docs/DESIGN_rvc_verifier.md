# Design & Build Plan — T26 RV32C Compressed Instruction Verifier

**Status:** Implemented & tested (AVA v2.4.0, 2026-06-26)
**Module:** `AGENT_H/rvc_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

The compressed ("C") extension is high coverage value at low risk: it is widely
implemented (PicoRV32, Ibex, most embedded cores) and its bugs are a distinct
class from arithmetic mismatches. A compressed instruction is a 16-bit alias
that *expands* to a 32-bit base instruction, so ordinary tandem diffing already
catches a wrong *result* — but it does **not** reliably catch the RVC-specific
failure modes:

- mis-sizing a 16-bit instruction so the PC advances by 4 and an instruction is
  silently skipped;
- executing a reserved/illegal compressed encoding instead of trapping;
- decoding a "prime" form with a register outside the legal `x8`–`x15` field.

This agent targets exactly those three.

---

## 2. Detecting compressed instructions

`is_compressed(rec)` decides in priority order: an explicit `insn_len`/`ilen`
field (== 2), a truthy `compressed` flag, a raw `insn`/`encoding` hex field
(≤ 4 hex digits ⇒ 16-bit, 8 ⇒ 32-bit), or a `c.` disassembly prefix. This keeps
the agent useful whether or not the trace carries an explicit length field.

---

## 3. Checks

| Check | Severity | Catches |
|---|---|---|
| `rvc_pc_stride` | HIGH | compressed instr followed by `pc + 4` (instruction skipped) |
| `rvc_reserved` | HIGH | reserved/illegal encoding executed without an illegal-instr trap |
| `rvc_reg_constraint` | HIGH | prime form (`c.lw`, `c.sw`, `c.and`, …) naming a register outside `x8`–`x15` |

**PC stride** is the headline check and is deliberately precise: it only fires
when a *non-control-transfer* compressed instruction is followed by a sequential
`pc + 4` (a trap on the next record is excluded, since a trap redirects the PC).
A `+2` delta is correct; anything else (taken branch, interrupt) is skipped — so
the check has essentially no false positives.

**Reserved encodings** covered: the all-zero halfword, `c.addi4spn` /
`c.lui` / `c.addi16sp` with a zero immediate, and `c.lwsp` / `c.jr` with `x0`.
If the trace shows such an encoding *and* a correct illegal-instruction trap
(cause 2), no violation is raised.

**Register constraint** enforces the 3-bit prime field on the forms that use it.

---

## 4. Report & integration

`run()` returns the standard schema v2.1.0 report
(`schema_version, agent, records_checked, compressed_seen, stats,
total_violations, severity_score, band, pass, violations[]`), band
`CLEAN→CRITICAL`. Wired into `_run_extended_pipeline` (`_rvc` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `rvc_report.json`, records `reports["rvc"]`)
and exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/rvc_verifier.py --rtl rtl_commit.jsonl`.

---

## 5. Test coverage

`tests/test_agents.py::TestRVCVerifier` — 11 cases: `is_compressed` heuristic,
clean stride, +4 stride bug, branch form skipped, register-constraint
caught/ok, reserved encoding caught, reserved-but-trapped ok, report schema,
manifest round-trip.

**Full suite: 96 passed**, `compileall` clean.

---

## 6. Limitations / next steps

- The expansion check is structural (PC stride + encoding constraints), not a
  full bit-accurate decode of the 16-bit encoding into its 32-bit form. A
  follow-up can add a golden `c.* → base` expander and compare the committed
  register/memory effect against the expanded instruction's expected effect.
- `IALIGN=16` branch-target alignment (compressed control transfers to odd
  addresses) is a natural next check once target addresses are exposed.
- HINT encodings (e.g. `c.addi x0`, `c.li x0`) are currently treated as benign;
  a strict mode could surface them.

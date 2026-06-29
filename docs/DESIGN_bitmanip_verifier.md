# Design & Build Plan — T28 RV32B Bit-Manipulation Verifier

**Status:** Implemented & tested (AVA v2.6.0, 2026-06-26)
**Module:** `AGENT_H/bitmanip_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

The B (bit-manipulation) extension is increasingly standard (Ibex ships Zbb,
many cores add Zba/Zbs) and is a clean, high-coverage, low-risk verification
target: every B instruction is a **pure deterministic function** of its integer
operands, so the golden model is exact — no rounding, no microarchitectural
state, no false-positive surface. The value is a dense battery of golden checks
that ordinary tandem diffing covers only as far as its random stimulus reaches.

---

## 2. Coverage

| Group | Instructions |
|---|---|
| **Zba** (address gen) | sh1add, sh2add, sh3add |
| **Zbb** (basic) | andn, orn, xnor, clz, ctz, cpop, min, max, minu, maxu, sext.b, sext.h, zext.h, rol, ror, rori, orc.b, rev8 |
| **Zbc** (carry-less mul) | clmul, clmulh, clmulr |
| **Zbs** (single-bit) | bclr, bclri, bext, bexti, binv, binvi, bset, bseti |

All arithmetic is 32-bit; shift/rotate/single-bit amounts use the low 5 bits of
the operand (RV32). `clmul*` is computed from the full carry-less product:
`clmul = P[31:0]`, `clmulh = P[63:32]`, `clmulr = P[62:31]`.

---

## 3. Method

A shadow register file (folded from each record's `regs` write-back, `x0`
pinned to 0) supplies `rs1`/`rs2`; immediates are parsed from the disassembly.
For each B instruction the verifier evaluates the exact golden function and
compares against the committed `rd`:

| Check | Severity | Catches |
|---|---|---|
| `bitmanip_result` | HIGH | committed `rd` ≠ golden value |

Writes to `x0` are ignored (discarded by the architecture), and a check is
*skipped* (counted in `stats.skipped`) when an operand value isn't present in
the trace — so the agent never produces a false positive from missing data.

---

## 4. Report & integration

`run()` returns the standard schema v2.1.0 report plus `bitmanip_ops`, band
`CLEAN→CRITICAL`. Wired into `_run_extended_pipeline` (`_bitmanip` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `bitmanip_report.json`, records
`reports["bitmanip"]`) and exported from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/bitmanip_verifier.py --rtl rtl_commit.jsonl`.

---

## 5. Test coverage

`tests/test_agents.py::TestBitmanipVerifier` — 34 cases: **28 golden vectors**
(one per representative instruction, validating the math: e.g.
`clz(0x00010000)=15`, `rev8(0x01020304)=0x04030201`, `clmul(3,3)=5`,
`orc.b(0x00ff0001)=0x00ff00ff`, signed `min(-1,1)=-1`), plus decode, an
`andn` result bug (CRITICAL), a `clz` result bug, report schema, and the
manifest round-trip.

**Full suite: 144 passed**, `compileall` clean.

---

## 6. Limitations / next steps

- RV64-only `.uw` address-generation forms (`add.uw`, `sh1add.uw`, …) and
  64-bit `rev8`/`clz`/`cpop` widths are out of scope until the RV64 widening
  lands; the golden functions are already parameterisable by width.
- `Zbkb`/`Zbkc`/`Zbkx` (the scalar-crypto bit-manip siblings: `pack`, `brev8`,
  `zip`/`unzip`, `xperm`) are natural follow-ups using the same dispatch table.
- Operand recovery relies on the shadow register file; instructions whose source
  was set before the trace window are skipped rather than guessed.

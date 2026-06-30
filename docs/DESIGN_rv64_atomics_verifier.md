# Design & Build Plan — T38 RV64 Atomics Verifier (RV64 widening, phase 3)

**Status:** Implemented & tested (AVA v2.16.0, 2026-06-30)
**Module:** `AGENT_H/rv64_atomics_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

Phase 1 gave AVA the 64-bit datapath and phase 2 gave it Sv39/Sv48 virtual
memory; this phase completes the concurrency primitive that every multi-core
RV64 system depends on — the 64-bit atomics. `LR.D`/`SC.D` and the nine
`AMO*.D` operations are the basis of locks, semaphores and lock-free data
structures, and their signed/unsigned 64-bit corner cases are a classic bug
source that a 32-bit model cannot see.

---

## 2. Design — complement, don't duplicate

The RV32 `atomics_verifier` already owns the `.W` width and contains the shared
`decode_atomic` decoder (which already recognises the `.d` suffix). Rather than
widen that tested module invasively, this agent **complements** it:

- it imports and reuses `decode_atomic`;
- it acts **only on `.D` atomics**, so the two modules never overlap;
- it runs the same checks as the RV32 verifier but at 64-bit width.

A single small, safe change was made to `atomics_verifier`: it now detects RV64
(a register value wider than 32 bits) and, on an RV64 trace, **stops flagging
legal `.D` atomics as `rv32_illegal_d`** — handing them to this module. RV32
behaviour is unchanged (verified by a test both ways).

---

## 3. The golden core

`amo_compute64(op, old, src)` is the 64-bit AMO reference: `swap/add/and/or/xor`,
signed `min/max` (via 64-bit sign interpretation), unsigned `minu/maxu`, with
64-bit wrap on `add`. It is unit-tested against hand-computed vectors —
e.g. `amomin.d(1, −1) == 0xFFFFFFFFFFFFFFFF` (signed) versus
`amominu.d(0xFFFFFFFFFFFFFFFF, 1) == 1` (unsigned), the exact pair that exposes
a width/signedness bug.

The verifier maintains a 64-bit shadow register file, shadow memory, and a
single-hart reservation set, mirroring the RV32 atomics model.

---

## 4. Checks

| Check | Severity | Catches |
|---|---|---|
| `amod_rd_value` | HIGH | AMO/LR.D destination ≠ old 64-bit memory value |
| `amod_writeback` | HIGH | AMO.D write-back ≠ `f(old, rs2)` (64-bit golden math) |
| `scd_success_*` / `scd_store_value` | HIGH | SC.D with a live reservation: must write `rs2` and return 0 |
| `scd_fail_*` | HIGH | SC.D without a reservation: must not write and return ≠ 0 |
| `amod_alignment` | HIGH | a `.D` atomic not 8-byte aligned without a misaligned trap |

---

## 5. Report & integration

`run()` returns the standard schema v2.1.0 report plus `atomics_d_examined`,
band `CLEAN→CRITICAL`. Wired into `_run_extended_pipeline` (`_rv64atom` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `rv64_atomics_report.json` only when `.D`
atomics are present, records `reports["rv64_atomics"]`) and exported from
`AGENT_H/__init__.py`. Standalone:
`python AGENT_H/rv64_atomics_verifier.py --rtl rtl_commit.jsonl`.

---

## 6. Test coverage

`tests/test_agents.py::TestRV64AtomicsVerifier` — 13 cases: 64-bit golden
vectors, clean `AMO.D`, signed `amomin.d`, write-back bug (verifying the 64-bit
expected value), `LR.D`/`SC.D` success, spurious `SC.D`, misalignment, the
`atomics_verifier` RV64-guard **both ways** (no flag on RV64, still flags on
RV32), malformed-input robustness, report schema, manifest round-trip.

**Full suite: 332 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 7. RV64 widening — status

| Phase | Item | Status |
|---|---|---|
| 1 | RV64 datapath (64-bit ALU + W-op sign-extension) | ✅ `rv64_verifier` |
| 2 | Sv39 / Sv48 virtual memory | ✅ `sv_mmu_verifier` |
| 3 | **RV64 atomics (AMO.D / LR.D / SC.D)** | ✅ this module |
| 4 | RV64 M-extension W-ops (`mulw/divw/divuw/remw/remuw`) | next — extend `rv64_verifier` |
| 5 | 64-bit addresses in PMP / cache / bus (xlen parameter) | widen address masks |

### Next steps for this module

- **Multi-hart reservations** — the reservation model is single-hart; a
  `hart_id` field extends it to true SMP (the Level-9 multicore frontier).
- **`.D` AMO ordering (`.aq`/`.rl`)** is parsed by the decoder; pairing with the
  RVWMO agent (`AGENT_I`) would check the acquire/release ordering effects.

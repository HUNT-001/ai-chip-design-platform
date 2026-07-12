# Design & Build Plan — T54 Scalar Cryptography Checker

**Status:** Implemented & tested (AVA v2.41.0, 2026-07-09)
**Module:** `AGENT_H/crypto_verifier.py`

---

## 1. Why this level

Cryptographic datapaths are a uniquely unforgiving verification target: a wrong
bit doesn't crash — it silently produces a value that still *looks* random,
breaking security in a way that's nearly impossible to spot downstream. The
RISC-V scalar-crypto extension's SHA / SM3 transform instructions are, happily,
**pure single-source bit functions** with an *exact* golden model, so they can
be checked to the bit.

## 2. Modelled instructions

Single source `rs1` → `rd`:

| Group | Instructions | Width |
|---|---|---|
| SHA-256 (Zknh) | `sha256sig0/sig1/sum0/sum1` | 32 |
| SHA-512 (Zknh, RV64) | `sha512sig0/sig1/sum0/sum1` | 64 |
| SM3 (Zksh) | `sm3p0`, `sm3p1` | 32 |

The golden recipes (standard):

```
sha256 Σ0 = ROTR2  ^ ROTR13 ^ ROTR22      σ0 = ROTR7  ^ ROTR18 ^ SHR3
sha256 Σ1 = ROTR6  ^ ROTR11 ^ ROTR25      σ1 = ROTR17 ^ ROTR19 ^ SHR10
sha512 Σ0 = ROTR28 ^ ROTR34 ^ ROTR39      σ0 = ROTR1  ^ ROTR8  ^ SHR7
sha512 Σ1 = ROTR14 ^ ROTR18 ^ ROTR41      σ1 = ROTR19 ^ ROTR61 ^ SHR6
sm3    P0 = x ^ ROTL9  ^ ROTL17           P1 = x ^ ROTL15 ^ ROTL23
```

`crypto_golden(mnem, x)` is exported so other agents (and tests) can reuse the
reference.

## 3. Operand recovery + check

The commit log records the *written* register (`rd`); the source `rs1` is
recovered from a **golden shadow register file** the agent maintains across the
trace (updated from every record's register writes, read *before* the
instruction's own write). For each crypto instruction the agent parses `rd,rs1`
from the disassembly, computes the golden transform of `shadow[rs1]`, and
compares to the committed `rd` (masked to the op width).

- **crypto_result** (HIGH) — committed `rd` ≠ golden.

## 4. Soundness

The golden model is validated in the test suite against an **independently
recomputed** reference (the test re-derives ROTR/ROTL/SHR itself and asserts the
module agrees), so the check isn't circular. ABI register names are resolved;
`x0` is ignored; non-crypto records are skipped; malformed records don't crash.

## 5. Integration & tests

Wired into `_run_extended_pipeline` as a commit-log verifier (`_crypto`, runs on
`rtl_log`, writes `crypto_report.json` when `crypto_active`). Exported from
`AGENT_H/__init__.py`.
`tests/test_extended_agents.py::TestCryptoVerifier` — 6 cases (validated
standalone: 10): independent-reference cross-check, SHA-256 clean/bug, SHA-512 +
SM3, ABI names + metrics, no-op/robustness/schema, manifest. All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 5a. Zbkb / Zbkx — bit-manipulation for cryptography (v2.42.0)

Exact bit functions, validated in the tests against independently-recomputed
references (and a round-trip identity `unzip(zip(x)) == x`):

- **Zbkb**: `pack`/`packh`/`packw` (combine register halves/bytes), `brev8`
  (reverse bits within each byte), `zip`/`unzip` (RV32 even/odd bit interleave).
- **Zbkx**: `xperm8`/`xperm4` — permute the bytes/nibbles of `rs1` according to
  the index vector in `rs2` (out-of-range index ⇒ 0).

`zbk_one(mnem,x,w)` and `zbk_two(mnem,a,b,w)` are exported. Two-source operands
are read from the shadow register file; XLEN is auto-detected (RV32/RV64). Same
`crypto_result` check.

## 6. Limitations / next steps

- **AES** (Zkne/Zknd) — `aes64es/esm/ds/dsm` and key-schedule (`aes64ks1i/ks2`):
  needs the S-box table + GF(2⁸) MixColumns and operates on a 128-bit state
  across two registers. Higher-value but heavier; the S-box/GF core is the next
  build.
- **SM4** (Zksed) — `sm4ed`/`sm4ks` (S-box + linear transform).
- **RV32 SHA-512** — the `sha512sig0h/l` / `sha512sum0r` two-register split
  forms.
- **Carry-less multiply for GHASH** (`clmul`/`clmulh`) — already covered by
  `bitmanip_verifier` (Zbc); cross-link for Zkg.

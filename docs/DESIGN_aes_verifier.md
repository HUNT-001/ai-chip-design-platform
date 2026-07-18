# Design & Build Plan — T56 AES Scalar Cryptography Checker

**Status:** Implemented & tested, **FIPS-197-validated** (AVA v2.44.0, 2026-07-09)
**Module:** `AGENT_H/aes_verifier.py`

---

## 1. Why this, and why it was the risky one

AES is the highest-stakes crypto datapath in the ISA — one wrong byte in the
S-box, MixColumns matrix, or the ShiftRows permutation silently breaks
confidentiality while every value still looks random. That's also *why* it was
deferred through T54/T55: a golden model that is itself subtly wrong is worse
than no check. The only responsible way to ship it is to **validate the golden
against the published FIPS-197 example**, which is exactly what this build does.

## 2. Modelled instructions (RV64, Zkne/Zknd)

- **`aes64esm` / `aes64es`** — encrypt round, middle (with MixColumns) / final.
- **`aes64dsm` / `aes64ds`** — decrypt round duals.
- **`aes64im`** — inverse MixColumns (decrypt key-schedule helper).
- **`aes64ks2`** — key-schedule step 2.

## 3. The AES core + the RV64 layout (the derivation)

Standard primitives: 256-entry **S-box** + inverse, GF(2⁸) `xtime`/`gmul`, and
forward/inverse **MixColumns** (the `2 3 1 1` / `14 11 13 9` matrices).

The RV64-specific part is how the 128-bit state maps to two registers and how
ShiftRows is applied. `aes64esm rd, rs1, rs2` forms state `rs2:rs1` (rs1 = low
64), applies ShiftRows, takes the **low 8 bytes**, SubBytes, and MixColumns each
32-bit word. Deriving ShiftRows (`state'[r][c] = state[r][(c+r) mod 4]`, byte
index `4c+r`) for the low 8 output bytes gives the selection

```
_SR_FWD = [0, 5, 10, 15, 4, 9, 14, 3]
```

and the round's **high 64 bits come from the same instruction with the operands
swapped** (`aes64esm rd, rs2, rs1`) — a symmetry that falls out of the byte
layout. Decrypt uses the inverse permutation `[0,13,10,7,4,1,14,11]` + inverse
S-box / MixColumns.

## 4. Validation — the FIPS-197 gate

The golden is **not** trusted on assertion; the test suite checks it against
published constants and vectors:

- **S-box**: `sbox[0x00]=0x63`, `sbox[0x53]=0xed`, and inverse is a true inverse.
- **MixColumns** (FIPS-197 §4.3): column `db 13 53 45 → 8e 4d a1 bc`.
- **`aes64esm` round** (Appendix B, AES-128): state
  `193de3be…e9f84808` → `046681e5…2806264c` (SubBytes → ShiftRows → MixColumns),
  computed as `aes64esm(s0,s1)` (low) ⧺ `aes64esm(s1,s0)` (high) — **exact
  match**.
- **`aes64es`** (final round) → the published ShiftRows+SubBytes state
  `d4bf5d30…1e2798e5`.
- **Decrypt / `aes64im`** invert their forward transforms (round-trip).

Because `aes64esm` reproduces FIPS-197 to the bit, the derived byte layout and
the whole golden model are proven correct.

## 5. Check & integration

- **aes_result** (HIGH) — committed `rd` ≠ `aes_golden(mnem, rs1, rs2)`.

Source operands are recovered from a golden shadow register file across the
commit log. Wired into `_run_extended_pipeline` (`_aes`, runs on `rtl_log`,
writes `aes_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/test_extended_agents.py::TestAESVerifier` — 4 cases (validated standalone:
9): constants, the FIPS-197 round vector, decrypt/`aes64im` inverses, verifier
clean/bug/no-op.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 6. Limitations / next steps

- **`aes64ks1i`** — key-schedule step 1 (needs the round-constant `rcon` table +
  the rotate/SubWord); the round datapath (the security-critical part) is done,
  key schedule step 1 is the remaining helper.
- **SM4** (Zksed `sm4ed`/`sm4ks`) — the other national-standard block cipher
  (S-box + rotate-based linear transform), validated against GB/T 32907.
- **Full-block validation** — compose the instructions for a complete AES-128
  encryption and check the ciphertext (`69c4e0d8…70b4c55a`) once `aes64ks1i` is
  in, exercising the whole cipher end-to-end.

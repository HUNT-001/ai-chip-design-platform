# Design & Build Plan — T62 Vector SM4 Checker (Zvksed)

**Status:** Implemented & tested, GB/T-32907-validated (AVA v2.51.0, 2026-07-09)
**Module:** `AGENT_H/vsm4_verifier.py`

---

## 1. Scope — completes the vector-crypto suite

The RISC-V vector-crypto SM4 sub-extension (Zvksed) adds two instructions, both
on 128-bit / 4-element groups (SEW=32):

| Instruction | Role |
|---|---|
| `vsm4r.[vv,vs]` | four SM4 cipher rounds (encryption/decryption round fn) |
| `vsm4k.vi`      | four SM4 key-expansion rounds, group by the `rnd` immediate |

With this, the vector-crypto tier is complete: **Zvkned (AES) + Zvknh (SHA-2) +
Zvksh (SM3) + Zvksed (SM4)**.

## 2. Golden — sail transcription

Both goldens transcribe the authoritative RISC-V sail (`vsm4r.adoc`,
`vsm4k.adoc`) and reuse the scalar SM4 S-box (`sm4_verifier`):

- **`vsm4r_golden`** (state `[x0..x3]`, round keys `[rk0..rk3]`): for `i∈0..3`,
  `B = x[i+1]⊕x[i+2]⊕x[i+3]⊕rk[i]`, `S = SubWord(B)`,
  `x[i+4] = x[i] ⊕ L(S)` with the cipher linear
  `L(S)=S⊕S⋘2⊕S⋘10⊕S⋘18⊕S⋘24`; result `[x4,x5,x6,x7]`.
- **`vsm4k_golden`** (`[rk0..rk3]`, group `rnd∈0..7`): same shape with the CK
  round-constant table and the key linear `L'(S)=S⊕S⋘13⊕S⋘23`; result
  `[rk4,rk5,rk6,rk7]`. `SubWord` applies the SM4 S-box to each byte.

## 3. Why it's trustworthy — GB/T 32907 end-to-end

The test **composes a full SM4 block cipher** from the two goldens and checks
the standard GB/T 32907 vector:

- Key expansion: `K[i] = MK[i] ⊕ FK[i]`, then 8 `vsm4k` groups → 32 round keys.
- Encryption: plaintext → 8 `vsm4r` groups (32 rounds) → reverse (`R`) transform.
- `key = plaintext = 0123456789abcdeffedcba9876543210` →
  **`681edf34d206965e86b3e94f536e4246`** (the published ciphertext) — exact
  match on the first run.
- **Decryption** (same rounds, round keys reversed) round-trips an arbitrary
  block, validating the round function in both directions.

A wrong S-box, linear constant, CK entry, or element index would break the
block-cipher match, so a passing full encryption is a strong joint proof.

## 4. Check & integration

- **vsm4_result** (HIGH) — computed group ≠ the reported result group.

Additive `vsm4_trace.jsonl` — `op`, 4-word `vd`/`vs2`/`result` groups (0x-hex or
ints), and `rnd` for `vsm4k`. Wired into `_run_extended_pipeline` (`_vsm4`,
`run_from_manifest` → `vsm4_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/…::TestVSM4Verifier` — 5 cases.

> Build note: stdlib-only; reuses the scalar SM4 S-box. Additive change;
> existing agents unaffected.

## 5. Status — vector-crypto suite complete

Zvkned/Zvknh/Zvksh/Zvksed all covered, each golden validated against a published
standard vector (FIPS-197 / GB/T 32905 / GB/T 32907). Remaining optional polish:
reading element groups directly from real `vregs`, and the GHASH extensions
(Zvkg: `vghsh`/`vgmul`) if GCM verification is ever wanted.

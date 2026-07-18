# Design & Build Plan — T57 SM4 Scalar Cryptography Checker

**Status:** Implemented & tested, **GB/T-32907-validated** (AVA v2.46.0, 2026-07-09)
**Module:** `AGENT_H/sm4_verifier.py`

---

## 1. Why this

SM4 (GB/T 32907-2016) is the Chinese national block cipher and the companion to
AES in the RISC-V scalar-crypto extension (Zksed). Like AES, its correctness is
security-critical and a wrong S-box byte or transform bit is invisible in the
output — so the golden model is only trustworthy if it's **validated against the
published test vector**, which this build does.

## 2. Instructions & golden

`sm4ed rd, rs1, rs2, bs` (cipher round) and `sm4ks rd, rs1, rs2, bs`
(key-schedule round):

1. select byte `bs` (0..3) of `rs2`, apply the SM4 **S-box**;
2. zero-extend to 32 bits and apply the **linear transform**
   - `sm4ed`: `L(x)  = x ⊕ (x⋘2) ⊕ (x⋘10) ⊕ (x⋘18) ⊕ (x⋘24)`
   - `sm4ks`: `L'(x) = x ⊕ (x⋘13) ⊕ (x⋘23)`;
3. rotate left by `8·bs` and XOR into `rs1`.

Because `L`/`L'` are linear and rotation commutes with them, **chaining the four
byte positions** (`bs=0..3`, `rs1` accumulating) computes the SM4 T-function
`T(A) = L(τ(A))` on a 32-bit word — the property the full-cipher validation
uses.

## 3. Validation — the GB/T gate

The test suite does not trust the golden on assertion; it:

- checks the **S-box** is the standard permutation (`sbox[0]=0xd6`,
  `sbox[0xff]=0x48`, 256 distinct values);
- checks the **T-function composition** (`sm4ed` chain == independent `τ` + `L`);
- **runs a full SM4-128 encryption** of the GB/T 32907-2016 example
  (key = plaintext = `0123456789abcdeffedcba9876543210`), building the 32-round
  cipher + key schedule *entirely* from the module's `sm4ed`/`sm4ks`, and
  asserts the ciphertext equals the published
  **`681edf34d206965e86b3e94f536e4246`**.

Reproducing the standard vector proves the S-box, both linear transforms, and
the byte/word conventions are all correct end-to-end.

## 4. Check & integration

- **sm4_result** (HIGH) — committed `rd` ≠ `sm4_golden(mnem, rs1, rs2, bs)`.

Source operands via the golden shadow register file; rides the standard commit
log. Wired into `_run_extended_pipeline` (`_sm4`, writes `sm4_report.json`).
Exported from `AGENT_H/__init__.py`. `tests/test_extended_agents.py::TestSM4Verifier`
— 4 cases (standalone: 6): S-box + T-composition, GB/T full vector, verifier
clean/bug/no-op.

With SM4 done, the scalar-crypto suite is complete: **AES + SHA-256/512 + SM3 +
SM4 + Zbkb/Zbkx**.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 6. Limitations / next steps

- **`aes64ks1i`** (AES key-schedule step 1, with `rcon`) remains the one AES
  helper not yet modelled.
- **Vector crypto (Zvk)** — the vector-length equivalents (`vaes*`, `vsha2*`,
  `vsm4*`), reusing these scalar golden cores per element.

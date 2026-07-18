# Design & Build Plan — T58 Vector AES Checker (Zvkned)

**Status:** Implemented & tested, FIPS-197-validated core (AVA v2.47.0, 2026-07-09)
**Module:** `AGENT_H/vaes_verifier.py`

---

## 1. Why this — and the reuse

The RISC-V vector-crypto extension (Zvk) provides *vector* AES (Zvkned): the same
AES round applied to every **128-bit element group** of a vector register, so a
core can encrypt many blocks in parallel. This is the first vector-crypto agent,
and it deliberately **reuses the FIPS-197-validated scalar AES core** from
`aes_verifier` (S-box, GF(2⁸) MixColumns) — so the golden inherits that
validation rather than re-deriving anything.

## 2. Instructions & round golden

Each op acts on one 128-bit group (16 bytes, byte 0 = leftmost):

| Op | Round |
|---|---|
| `vaesem` | `MixColumns(ShiftRows(SubBytes(state))) ⊕ key` (encrypt middle) |
| `vaesef` | `ShiftRows(SubBytes(state)) ⊕ key` (encrypt final) |
| `vaesdm` | inverse cipher middle (see §3) |
| `vaesdf` | inverse cipher final |
| `vaesz`  | `state ⊕ key` (round-key XOR only) |

Because vector AES uses the *full* 128-bit state (not the RV64 two-register
split of the scalar `aes64es`), the round is the textbook full-state
SubBytes/ShiftRows/MixColumns — the full-128 ShiftRows permutation
(`out[4c+r] = in[4·((c+r) mod 4)+r]`).

## 3. Encrypt validated, decrypt as the exact inverse

- **Encrypt** (`vaesem`/`vaesef`) is checked against the **FIPS-197 round
  vector**: state `193de3be…e9f84808` → `046681e5…2806264c` (SB+SR+MC, key 0),
  and `d4bf5d30…1e2798e5` for the final round. Exact match.
- **Decrypt** (`vaesdm`/`vaesdf`) is the standard **inverse cipher** round —
  AddRoundKey → InvMixColumns → InvShiftRows → InvSubBytes. A build-time bug (I
  first had the key/InvMixColumns order wrong) was caught by a **round-trip
  test**: `vaesd*(vaese*(x, k), k) == x` must hold for both the final and middle
  rounds; the corrected order makes it hold, which validates the decrypt path.

## 4. Check & integration

- **vaes_result** (HIGH) — computed group ≠ the reported result group.

Additive `vaes_trace.jsonl` — 128-bit `state`/`key`/`result` as 32-hex-char
strings (or 16-int byte lists). Wired into `_run_extended_pipeline` (`_vaes`,
`run_from_manifest` → `vaes_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/test_extended_agents.py::TestVAESVerifier` — 4 cases (standalone: 7):
FIPS-197 round + final, decrypt round-trip + key XOR, verifier clean/bug/no-op,
manifest.

> Build note: stdlib-only apart from importing the sibling `aes_verifier`
> primitives (package-or-standalone fallback). Additive change; existing agents
> unaffected.

## 5. Limitations / next steps

- **`vaeskf1` / `vaeskf2`** — vector AES key-schedule (needs `rcon` + SubWord),
  the vector analog of the still-missing scalar `aes64ks1i`.
- **Vector SHA-2 (Zvknha/b)** — `vsha2ms`/`vsha2ch`/`vsha2cl`, reusing the
  scalar SHA σ/Σ core across the 4-word message-schedule groups.
- **Vector SM4 (Zvksed)** — `vsm4r`/`vsm4k`, reusing the scalar SM4 core.
- **Element-group extraction from real `vregs`** — read groups directly from the
  RVV element arrays (as `vector_verifier` does) instead of an explicit trace.

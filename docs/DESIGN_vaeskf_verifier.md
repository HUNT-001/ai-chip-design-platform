# Design & Build Plan — T61 AES Key-Schedule Checkers (Zkne / Zvkned)

**Status:** Implemented & tested, FIPS-197-validated (AVA v2.50.0, 2026-07-09)
**Modules:** `AGENT_H/vaeskf_verifier.py` (vector) + `aes64ks1i` in
`AGENT_H/aes_verifier.py` (scalar)

---

## 1. Scope — the last crypto key-schedule gap

The AES round datapaths (scalar `aes64es*`/`ds*`, vector `vaes*`) were already
covered, but key *expansion* was only partially checked (`aes64ks2`). This task
adds the three forward key-schedule instructions that derive round keys:

| Instruction | Extension | Role |
|---|---|---|
| `aes64ks1i rd, rs1, rnum` | Zkne/Zknd (RV64) | scalar: RotWord/SubWord/Rcon on `rs1[63:32]` |
| `vaeskf1.vi vd, vs2, uimm` | Zvkned | vector AES-128 key-schedule round |
| `vaeskf2.vi vd, vs2, uimm` | Zvkned | vector AES-256 key-schedule round |

## 2. Goldens — sail transcription

- **`aes64ks1i`** (in `aes_verifier`): `tmp1 = rs1[63:32]`;
  `tmp2 = (rnum==0xA) ? tmp1 : ror32(tmp1,8)`; `tmp3 = SubWord(tmp2)`;
  `result = (tmp3 ^ rc) @ (tmp3 ^ rc)` with `rc = Rcon[rnum]` (0 for `rnum==0xA`,
  the AES-256 no-Rcon path). Legal `rnum ∈ 0x0..0xA`; out-of-range → skipped.
  Rides the commit log with the shadow regfile (immediate parsed from disasm).
- **`vaeskf1_golden`**: `w0 = SubWord(RotWord(K[3])) ⊕ Rcon[rnd-1] ⊕ K[0]`,
  `w[i] = w[i-1] ⊕ K[i]`. `K` = current round-key group (element 3 = MS word).
- **`vaeskf2_golden`**: even round → `w0 = SubWord(RotWord(K[3])) ⊕
  Rcon[rnd/2-1] ⊕ B[0]`; odd round → `w0 = SubWord(K[3]) ⊕ B[0]`; `B` = previous
  round key (`vd`). Both include the spec's **out-of-range immediate
  projection** (invert `uimm[3]` when the round number is out of range).

## 3. Why it's trustworthy — FIPS-197 end-to-end

Each golden is validated by composing a **complete AES key expansion** and
comparing against FIPS-197:

- **Scalar** `aes64ks1i` + `aes64ks2`, driven over rounds 0-9 in RV64 register
  packing, reproduces the AES-128 expanded key (`w4=a0fafe17 … w43=b6630ca6`).
- **`vaeskf1`** iterated over rounds 1-10 reproduces the same AES-128 key.
- **`vaeskf2`** iterated over rounds 2-14 reproduces a full **AES-256** (Nk=8)
  expansion, checked word-for-word against a textbook reference expansion.

Byte-order note: the RISC-V AES key schedule uses `RotWord = ror32(·,8)` with the
round constant in the low byte (the little-endian element convention). The tests
bridge to FIPS big-endian words with a per-word byteswap; a wrong rotate, rcon
position, or word index breaks the full-expansion match, so a passing expansion
is a strong joint proof.

## 4. Integration

- Scalar: `aes64ks1i` folded into `AESVerifier` (`aes_result`, HIGH) — no new
  agent, exported from `AGENT_H`.
- Vector: `AGENT_H/vaeskf_verifier.py` → `_run_extended_pipeline` (`_vaeskf`,
  `run_from_manifest` → `vaeskf_report.json`), additive `vaeskf_trace.jsonl`.
- `tests/…::TestAESKeySchedule` — 6 cases + the `AESVerifier` commit-log path.

> Build note: stdlib-only; `vaeskf` reuses the `aes_verifier` S-box. Additive
> change; existing agents unaffected.

## 5. Status

With T58-T61 the AES/SHA-2/SM3 vector-crypto tier and the AES key schedule are
complete. Remaining optional crypto polish: vector SM4 round/key (`vsm4r`/
`vsm4k`, Zvksed) and reading element groups directly from real `vregs`.

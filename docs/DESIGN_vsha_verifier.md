# Design & Build Plan — T59 Vector SHA-2 Checker (Zvknha/Zvknhb)

**Status:** Implemented & tested, hashlib-validated (AVA v2.48.0, 2026-07-09)
**Module:** `AGENT_H/vsha_verifier.py`

---

## 1. Scope

The RISC-V vector-crypto SHA-2 sub-extensions (Zvknha = SHA-256, Zvknhb =
SHA-256 + SHA-512) add three instructions, all on 4-element groups:

| Instruction | Role |
|---|---|
| `vsha2ms.vv` | message-schedule expansion — 4 new schedule words per group |
| `vsha2cl.vv` | hash compression, two rounds, using the **low** two `vs1` words |
| `vsha2ch.vv` | hash compression, two rounds, using the **high** two `vs1` words |

`SEW` selects the algorithm: **SEW=32 → SHA-256**, **SEW=64 → SHA-512**. Per the
spec, the Zvknh compression instructions do **not** add the round constant `K`;
software adds it (with `vadd.vv`) before the compression instruction, so the
golden receives the already-`W+K` words in `vs1`.

## 2. Golden

The primitives are the standard SHA-2 functions (SEW-parameterised): σ₀/σ₁ for
the message schedule and Σ₀/Σ₁/Ch/Maj for compression.

- **`vsha2ms_golden`** implements `Wt = σ₁(W[t-2]) + W[t-7] + σ₀(W[t-15]) +
  W[t-16]` for the four output words, with element layout
  `vd=[W0..W3]`, `vs2=[W4,W9,W10,W11]`, `vs1=[W12..W15]` → `[W16..W19]`.
- **`vsha2c_golden`** runs two SHA-2 rounds over the state split
  `vs2=[f,e,b,a]`, `vd=[h,g,d,c]`, consuming two `W+K` words (the low pair for
  `cl`, the high pair for `ch`) and returning the new `[f,e,b,a]`.

## 3. Why it's trustworthy — end-to-end hashlib validation

Rather than validate the two instructions in isolation (which leaves the
element-group *layout* unproven), the test **composes a complete SHA-2 hash**
purely from `vsha2ms` + `vsha2c` and compares to Python's `hashlib`:

- Full **multi-block SHA-256** of `b"abc"`, `b""`, `b"hello world"`, `b"a"*100`
  → matches `hashlib.sha256` exactly (`ba7816bf…` for `abc`).
- Full **SHA-512** of `b"abc"` (SEW=64, 80 rounds, 256-bit groups) → matches
  `hashlib.sha512` (`ddaf35a1…`).

A wrong index in either the schedule or the compression layout would corrupt the
digest, so a passing full-hash is a strong joint proof of arithmetic **and**
packing. Supplementary tests check the message-schedule against the raw
recurrence and confirm `cl`/`ch` really select the low/high `vs1` words
(`cl[w0,w1] == ch[·,·,w0,w1]`).

## 4. Check & integration

- **vsha_result** (HIGH) — computed group ≠ the reported result group.

Additive `vsha_trace.jsonl` — each record carries `op`, `sew` (32/64), and the
4-word `vd`/`vs2`/`vs1`/`result` groups (0x-hex or ints). Wired into
`_run_extended_pipeline` (`_vsha`, `run_from_manifest` → `vsha_report.json`).
Exported from `AGENT_H/__init__.py`. `tests/…::TestVSHAVerifier` — 6 cases.

> Build note: stdlib-only; the σ/Σ recipe mirrors the scalar SHA core. Additive
> change, existing agents unaffected.

## 5. Limitations / next steps

- **Vector SM4 (Zvksh)** — `vsm3me`/`vsm3c` (SM3), reusing the scalar SM3 core;
  note SM3 vector ops byte-swap input/result for endianness.
- **`vaeskf1`/`vaeskf2`** — vector AES key-schedule (still open, with scalar
  `aes64ks1i`).
- **Element-group extraction from real `vregs`** — read groups directly from the
  RVV element arrays instead of an explicit trace.

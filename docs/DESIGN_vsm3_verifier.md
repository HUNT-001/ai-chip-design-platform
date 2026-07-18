# Design & Build Plan — T60 Vector SM3 Checker (Zvksh)

**Status:** Implemented & tested, GB/T-32905-validated (AVA v2.49.0, 2026-07-09)
**Module:** `AGENT_H/vsm3_verifier.py`

---

## 1. Scope

The RISC-V vector-crypto SM3 sub-extension (Zvksh) adds two instructions, both
on 256-bit / 8-element groups (SEW=32):

| Instruction | Role |
|---|---|
| `vsm3me.vv` | SM3 message expansion — 8 new schedule words per group |
| `vsm3c.vi`  | SM3 compression, **two rounds**, selected by the `rnds` immediate |

Per the spec, every 32-bit input/output word is byte-swapped (`rev8`) between
big and little endian so software can feed message bytes directly; the golden
reproduces that.

## 2. Golden — exact sail transcription

Both goldens transcribe the authoritative RISC-V crypto sail pseudocode
(`vsm3me.adoc`, `vsm3c.adoc`) exactly:

- **`vsm3me_golden`** — `W[j] = P1(W[j-16] ⊕ W[j-9] ⊕ (W[j-3]⋘15)) ⊕ (W[j-13]⋘7)
  ⊕ W[j-6]`, with `P1(x)=x⊕(x⋘15)⊕(x⋘23)`; vs1=W[7:0], vs2=W[15:8] → vd=W[23:16].
- **`vsm3c_golden`** — two SM3 rounds using `SS1/SS2/TT1/TT2`, `FF_j/GG_j`
  (XOR form for rounds ≤15, majority/select form for ≥16), `T_j` constant, and
  `P0(x)=x⊕(x⋘9)⊕(x⋘17)`. State `vd={H,G,F,E,D,C,B,A}`; messages
  `vs2={-,-,w5,w4,-,-,w1,w0}` with `W'[k]=w[k]⊕w[k+4]`; result is the rolled
  `{G1,G2,E1,E2,C1,C2,A1,A2}` packing the hardware feeds into the next `vsm3c`.

## 3. Why it's trustworthy — end-to-end GB/T validation

The rolled state packing and the endianness swaps make per-instruction spot
checks fragile, so the test **composes a complete SM3 hash** purely from
`vsm3me` + `vsm3c` and compares to the **GB/T 32905-2016** published digests:

- single-block `"abc"` → `66c7f0f4…8f4ba8e0`
- multi-block `"abcd"×16` → `debe9ff9…9c0c5732`
- empty string → `1ab21d83…5082aa2b`

All three match on transcription (no tuning of the round math was needed — the
`"abc"` vector matched on the first run). A wrong index, a missed `rev8`, or a
mis-ordered pack would corrupt the digest, so a passing full-hash jointly proves
the arithmetic, the byte-swaps and the element-group layout. Supplementary tests
check the message-expansion recurrence and that `rnds` correctly switches the
`FFj/GGj/Tj` behaviour between rounds 0-15 and 16-63.

## 4. Check & integration

- **vsm3_result** (HIGH) — computed group ≠ the reported result group.

Additive `vsm3_trace.jsonl` — each record carries `op`, the 8-word `vs1`/`vs2`/
`vd`/`result` groups (0x-hex or ints), and `rnds` for `vsm3c`. Wired into
`_run_extended_pipeline` (`_vsm3`, `run_from_manifest` → `vsm3_report.json`).
Exported from `AGENT_H/__init__.py`. `tests/…::TestVSM3Verifier` — 5 cases.

> Build note: stdlib-only; self-contained (no dependency on the scalar SM3
> module). Additive change, existing agents unaffected.

## 5. Status — vector-crypto tier complete

With T58 (Zvkned vector AES), T59 (Zvknh vector SHA-2) and T60 (Zvksh vector
SM3), the vector-crypto hashing/cipher tier is covered. Remaining crypto gaps
are the key-schedule helpers: scalar `aes64ks1i` and vector `vaeskf1`/`vaeskf2`.

# Design & Build Plan — T41 RISC-V Vector (RVV) Verifier

**Status:** Implemented & tested (AVA v2.20.0, 2026-06-30)
**Module:** `AGENT_H/vector_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this is the widest gap

The RISC-V Vector extension (RVV 1.0) is the single largest hole in most
verification flows. Unlike scalar ISA, RVV correctness is dominated by **dynamic
configuration state** — the current `vtype` (SEW, LMUL, tail/mask policy, vill)
and `vl` (application vector length). A plain scalar tandem-diff compares
architectural x-registers and never inspects this state, so the entire class of
"wrong vector length / wrong element width / wrong tail handling" bugs sails
straight through. This agent closes exactly that class.

## 2. The checks (sound, conservatively gated)

| Check | Severity | What it catches |
|---|---|---|
| `vset_vl` | HIGH | wrong `vl` computed by `vsetvli/vsetivli/vsetvl` |
| `vtype_vill` | HIGH | illegal `vtype` accepted, or `vl≠0` while `vill` set |
| `velem` | HIGH | wrong element result in a vector ALU op |
| `vtail` | MEDIUM | destination tail disturbed under `vta=0` |
| `vmem_addr` | HIGH | wrong per-element load/store address (unit/strided/indexed) |
| `vmem_count` | HIGH | wrong number of memory accesses (spurious / missing / masked-off accessed) |
| `vmem_eew` | MEDIUM | memory access size ≠ encoded element width (EEW) |
| `vmem_value` | HIGH | loaded/stored element ≠ memory value at that address |

### 2.1 `vset_vl` — the crux, done to spec

`VLMAX = LMUL · VLEN / SEW`, with **fractional LMUL** (1/8, 1/4, 1/2) fully
supported. The vsetvl spec does *not* pin `vl` to a single value in every case,
so the checker asserts only the spec-*guaranteed* relationships and never a
point value in the ambiguous region — a compliant design is therefore never
falsely flagged:

```
AVL ≤ VLMAX            → vl == AVL
AVL ≥ 2·VLMAX          → vl == VLMAX
VLMAX < AVL < 2·VLMAX  → ceil(AVL/2) ≤ vl ≤ VLMAX   (impl-defined band)
always                 → 0 ≤ vl ≤ VLMAX
```

### 2.2 `vtype_vill`

An unsupported configuration — reserved LMUL encoding, `SEW > ELEN`, or
`VLMAX < 1` (fractional LMUL with too-large SEW) — must set `vill` and force
`vl = 0`. The golden `vill` is recomputed from SEW/LMUL and compared against the
DUT; a design that silently accepts an illegal `vtype`, or leaves `vl ≠ 0` with
`vill` set, is flagged.

### 2.3 `velem` — element-wise golden ALU

For a modelled vector ALU op the checker recomputes **every active, unmasked
element** at the configured SEW and compares to the DUT's destination register.
Modelled: `vadd/vsub/vrsub`, `vand/vor/vxor`, `vsll/vsrl/vsra` (arithmetic vs
logical shift correct by signedness), `vmul/vmulh/vmulhu`, `vmin/vmax` (signed)
and `vminu/vmaxu` (unsigned), `vmerge`, `vmv`, across `.vv` (vector-vector),
`.vx` (vector-scalar from an x-reg) and `.vi` (vector-immediate) forms.

**Soundness by construction:** elements at `i ≥ vl` (tail) and mask-off elements
are **skipped** — RVV permits agnostic tail/mask policies, so pinning them would
produce false positives. Only architecturally-determined elements are asserted.
For `vmerge`, the mask-*off* element is checked against the "false" operand
(`vs2`), which *is* architecturally pinned.

### 2.4 `vtail`

When `vta = 0` (tail-undisturbed) the destination elements `[vl, VLMAX)` must
equal their prior value; checked only when the log exposes `vstate_prev`.

### 2.5 Vector load/store (added in the same version)

`decode_vmem` recognises unit-stride (`vle{N}/vse{N}`), strided
(`vlse{N}/vsse{N}`) and indexed (`vl{u,o}xei{N}/vs{u,o}xei{N}`) forms. For each
**active, unmasked** element the checker generates the golden byte address:

```
unit    : addr_i = base + i · (EEW/8)
strided : addr_i = base + i · stride
indexed : addr_i = base + zext(index_vreg[i], index_EEW)
```

and compares against the DUT's observed accesses (an explicit `vmem.addrs`
list, else the record's `mem_reads`/`mem_writes`). Address correctness is an
**order-independent set comparison** (no assumption about access ordering),
while `vmem_count` separately asserts exactly one access per active element —
so a design that generates a **spurious access for a masked-off or tail
element**, or drops an access, is caught. `vmem_eew` checks the access size
equals the encoded EEW (independent of SEW), and `vmem_value` cross-checks the
loaded/stored element against the memory value at its address. All gated: the
check runs only when base + an access source are available.

## 3. Metrics (never fail the run)

Vector-instruction and `vset` counts, mean `vl`, **SEW** and **LMUL**
histograms, masked-op count, and total active elements checked — the RVV-shape
telemetry a scalar flow can't produce.

## 4. Trace contract (all additive / optional)

```
record may carry:
  "vtype": {"sew":32,"lmul":1,"vta":1,"vma":1,"vill":false}   # or encoded int/hex
  "vl": <int>   "avl": <int>   "vlen": <int, default 128>
  "vregs": {"v1":["0x..",...], ...}   element arrays
  "vmask": [bool,...]                 (or v0 element LSBs)
  "vstate_prev": {"v1":[...]}         dest pre-state for the tail check
```

`decode_vtype` accepts either a structured dict or an **encoded** `vtype` integer
(`vlmul[2:0]`, `vsew[5:3]`, `vta[6]`, `vma[7]`, `vill[31]`). The agent is a clean
no-op on any trace with no vector activity.

## 5. Integration

Wired into `_run_extended_pipeline` (`_vector` import,
`EXTENDED_AGENTS_AVAILABLE`, writes `vector_report.json` and records
`reports["vector"]` when `vector_active`). Exported from `AGENT_H/__init__.py`.
Standalone: `python AGENT_H/vector_verifier.py --manifest run_manifest.json`.

## 6. Test coverage

`tests/test_agents.py::TestVectorVerifier` + `TestVectorMemory` — 24 cases:
vtype decode (dict + encoded), fractional VLMAX, element golden math, `vset_vl`
clean / exceeds-VLMAX / ambiguous-band (both legal endpoints + illegal), `vill`
on illegal config, `velem` clean/bug/vx/vi, tail-and-mask skipping, `vtail`
disturbance, non-vector no-op, malformed-input robustness, report schema,
manifest round-trip; plus `decode_vmem` modes, unit/strided/indexed addressing
(clean + wrong), spurious-access count, mask-suppressed accesses, EEW mismatch,
load/store value consistency, mem metrics + gating. All pass (validated
standalone: 40 in the isolated harness).

> Build note: stdlib-only and self-contained, so validated in isolation; the
> workspace mount truncates recently-grown files, so the full in-repo suite runs
> against the real repo. The change is additive (new module + lazy pipeline
> hook + new test class), so existing agents are unaffected.

## 7. Limitations / next steps

- **Segment** loads/stores (`vlseg{n}e{N}`) and **fault-on-first** (`vle{N}ff`)
  `vl`-trimming semantics — the `ff` flag is decoded but the trimmed-`vl`
  cross-check is not yet modelled.
- **Widening / narrowing** ops (`vwadd`, `vnsrl`, …) and **reductions**
  (`vredsum`, …) — different SEW in/out; add once the widened-register element
  layout is exposed.
- **Fixed-point / float vector** (`vsadd`, `vfadd`, …) — reuse `fp_verifier`'s
  IEEE core per element.
- **Mask-producing compares** (`vmseq`, `vmslt`, …) — check the produced mask
  register bit-exactly.
- **LMUL register grouping** — validate that `vd`/`vs` register numbers are
  LMUL-aligned.

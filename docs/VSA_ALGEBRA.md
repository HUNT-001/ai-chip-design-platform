# Verification State Algebra (VSA)

**Working title** (supersedes the "World Foundation" framing — the object is no
longer only the *world*, it is the algebra of verification reasoning). This
document is the **grammar**: the ontology (nouns) is treated as settled; here we
define ownership, the object operations, and the invariance principle. Timeless
by intent — no reference to any implementation architecture. (Engineering
mappings live in a separate implementation chapter.)

---

## 1. Ownership: knowledge owns its evidence (`E ⊢ K`)

Evidence is not a global pool. A knowledge item is a **judgment** — a
proposition carried *together with* the evidence that warrants it and the
warrant's *type*:

```
    e ⊢ φ : κ            "evidence e warrants proposition φ with warrant κ"
    κ ∈ { definitional, deductive, inductive }        (temporal = a modality)
```

The knowledge state is a **context** (dependent record) of such judgments:

```
    K  =  { (φⱼ , eⱼ , κⱼ) }ⱼ          each φⱼ owns its own eⱼ .
```

**Consequence — Accountability becomes structural, not a stored object.** The
justification of a belief *is* its evidence component `eⱼ`; the witness travels
inside the judgment. So the sixth closure role (Accountability / `J`) is
*realised by the type discipline* — "every judgment carries its warrant" — rather
than added as a separate primitive. It survives only as a requirement on
*decisions* (a chosen action must still emit a utility-witness). This **tightens
the closure** from six stored objects to five + one pervasive discipline.

---

## 2. The object algebra (the four grammar questions, answered)

### 2.1 Evidence equivalence — `e ≈_φ e'`
Two evidences are equivalent for `φ` iff they induce the same warrant on it,
**typed by κ**:
- **deductive:** *proof irrelevance* — any two proofs of `φ` are equivalent; only
  existence matters (`ℛ=0` either way).
- **inductive:** equivalent iff same **sufficient statistic** `(n_eff, coverage
  class)` — two test sets hitting the same relevant bins with the same
  pass-counts are indistinguishable for risk.
- **definitional:** equivalent iff the same structural fact.

### 2.2 Knowledge merge — `K ⊔ K'`
A **partial commutative monoid.** Defined iff the contexts are *consistent* (no
`φ` proven true in one and disproven in the other). Then merge = union of
judgments, with beliefs on shared `φ` combined by the confluent update
(Struct-1, so order-independent), and **deductive dominance** on conflict (a
proof or counterexample overrides accumulated inductive belief; Sem-1).
*Inconsistency is not an error to swallow — it is a finding:* a proof-vs-
counterexample clash means the design, the specification, or a tool is unsound.
Merge failure = a bug report.

### 2.3 Projection / forgetting — `K ↓ S`
Forgetting is projection with a **sufficiency floor**:
- **Sound (risk-neutral) forgetting** discards data while retaining the *minimal
  sufficient statistic*. Since inductive `ℛ` depends only on `(n_eff)`, dropping
  individual traces but keeping `n_eff` is *lossless for risk* — this is exactly
  why episodic memory may be compressed into semantic memory without losing
  verification power.
- **Lossy forgetting** (dropping `n_eff` or a proof) may only *raise* residual
  risk, never lower it. Forgetting is therefore a **risk-monotone** morphism.

### 2.4 Memory vs evidence — when does `M` subsume `E`?
Semantic memory supplies **priors**; evidence supplies **likelihood/warrant**.
By the prior-robustness law (Safe-1), the residual risk `ℛ` is *prior-
independent*. Therefore:

> **Memory can subsume Evidence for the Bayesian (prior-dependent) belief — it
> guides the planner — but NEVER for the prior-robust risk `ℛ`.** No amount of
> remembered experience lowers `ℛ`; only fresh evidence (`n_eff`) or proof does.

This is the correct and non-negotiable safety property: *reuse informs, it does
not certify.* You cannot remember a new design into being verified. It falls out
of Safe-1 — the algebra is *forced* by the laws, not chosen.

---

## 3. Struct-5 — the invariance principle (solved & validated)

**Definition.** Let the specification induce a **failure σ-algebra**: the atoms
are distinct violating behaviours, and each property `P` is the measurable set
`fail(P)` of behaviours that violate it. Define residual risk as a **measure**:

```
    ℛ(P)  =  μ( unresolved part of fail(P) )      (each atom counted once, weighted)
```

**Theorem (basis invariance).** Refining `P` into `{P₁,…,Pₙ}` with
`fail(P) = ⋃ᵢ fail(Pᵢ)` gives

```
    ℛ(P) = F( ℛ(P₁),…,ℛ(Pₙ) ),   F = inclusion–exclusion (Möbius) composition,
```

which is **invariant to the choice of refinement/cover**. If `Φ` is presented in
a **partition basis** (disjoint atoms), `F` collapses to plain addition. Hence
verification depends on the *meaning* (the failure σ-algebra) of the
specification, not on how it is described — the coordinate-free / invariance
principle, analogous to coordinate-independence in physics.

**Validated** (`docs/vws_struct5_validation.py`): two different overlapping
refinements X, Y of the same property give **measure-risk 7.0 = 7.0 = direct
7.0** (invariant), while naive additive gives **9.0 ≠ 8.0** (the double-counting
bug); inclusion–exclusion reproduces the measure exactly on both covers.

**Cost, stated honestly.** Invariance requires the risk to be a *measure* and
`Φ` to be normalisable to a partition basis (overlapping properties decomposed
to disjoint atoms). Spec normalisation is a real precondition — the invariance
holds *for the failure σ-algebra*, and two descriptions inducing the same
σ-algebra are guaranteed equal risk.

---

## 4. What this settles, and what remains

**Settled:** ownership (`E⊢K`), the four object operations (equivalence, merge,
forget, memory-subsumption), and Struct-5 (basis invariance via measure +
Möbius). Accountability is now a type discipline, tightening the closure.

**Remaining before v1.0 freeze:**
1. **Closure — external attack.** Five candidate seventh objects (Goals,
   Planner, World-model, Representation, Uncertainty) all reduce; the strongest
   escalation found only a missing *relation* (ownership), now absorbed. Closure
   looks strong but should be attacked by **outside** verification researchers —
   self-attack has hit diminishing returns, and independent falsification is the
   real validation.
2. **Naming.** "World Foundation" undersells it; candidates: Verification State
   Algebra (VSA), Verification Reasoning Algebra, Autonomous Verification
   Calculus. Deferred — not load-bearing.
3. **Timelessness pass.** Purge implementation comparisons from the mathematical
   chapters; state only mathematical facts (e.g. *Prediction ≠ Hypothesis
   Generation*), and relocate all "X implements Y" statements to a separate
   implementation chapter.

**Remaining genuinely open (honest):** hypothesis *ranking* has no soundness
guarantee — it reaches into unknown-unknowns, so it is heuristic by necessity.
That boundary is stated, not hidden.

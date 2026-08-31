# Verification State Algebra — v1.0 (Consolidated Foundation)

A mathematical foundation for autonomous, accountable hardware verification.
Consolidates and supersedes `VWS_FRAMEWORK`, `VWS_FOUNDATION`,
`VWS_ONTOLOGY_v0.9`, the creation-layer addendum, and `VSA_ALGEBRA`. Timeless by
intent: **§2–§9 name no AI architecture.** All "X implements Y" statements are
confined to the Appendix. Every quantitative claim is validated by a committed
script (`docs/vws_*_validation.py`).

---

## 1. Scope and assumptions

**Object.** Two coupled worlds: a **Design World `D`** (RTL `M`, specification
`Φ`, criticality weights `w`, commit lineage) that owns the *hidden* correctness
`θ* ∈ {0,1}^m` (`θ*ⱼ=1 ⟺ M ⊨ φⱼ`); and a **Verification World `V`**, an evolving,
accountable body of knowledge *about* `D`.

**Thesis.** Verification is the sequential, budget-bounded reduction of
**prior-robust residual risk** about whether `D ⊨ Φ`, where every belief owns its
warrant, simulation and formal methods are commensurable evidence, and the
verifier may *create* new properties, concepts, and policies — not only update
beliefs.

**Standing assumptions** (limits made explicit up front, revisited in §8):
`Φ` is given and possibly incomplete; inductive evidence is
stimulus-distribution-conditional; criticality weights are external; cross-commit
soundness reduces to cone-of-influence soundness; hypothesis *ranking* is
irreducibly heuristic.

---

## 2. Mathematical ontology (objects)

An accountable bounded-rational verifier maintains six functional roles; every
object reduces to one (closure discussed in §8).

| Role | Object | Content |
|---|---|---|
| World | `D` | design, spec `Φ`, hidden `θ*`, commit lineage |
| Knowledge | `K` | judgments about `D` (§3), typed by warrant |
| Possibility | `H` | candidate properties not yet in `Φ` (the generative frontier) |
| Memory | `M = (M_e, M_s, M_p)` | episodic trajectories, semantic abstractions, procedural policies |
| Value | `ℛ, 𝒰, X` (derived) | residual risk, utility, exploration value |
| Accountability | — | *a discipline, not a stored object* (§3): every judgment owns its warrant |

Knowledge is typed by **epistemic warrant** `κ`, which determines how a fact
enters risk: **definitional** (structure — gates *relevance*), **deductive**
(proof — drives risk to 0), **inductive** (observation — an evidential bound,
never 0). *Temporal* is a modality qualifying the latter two.

**Belief is a capability of `K`, not a fixed representation.** Any `K` must
expose `{Inference, Update, Calibration, Query, Prediction}`, and is *admissible*
only if the laws (§6) can hold on it: confluent `Update`, a prior-robust risk
`Query`, a checkable `Calibration`. (Probabilistic, credal, evidential, or
latent representations are candidate implementations; the laws filter them.)

---

## 3. State algebra

### 3.1 Ownership — `E ⊢ K`
A knowledge item is a **judgment** carrying its warrant inseparably:

```
    e ⊢ φ : κ         K = { (φⱼ, eⱼ, κⱼ) }ⱼ      each φⱼ owns its evidence eⱼ .
```

*Consequence:* **unjustified knowledge is unrepresentable** — Accountability is a
type discipline (like a type system forbidding ill-typed terms), not a stored
witness. This tightens the ontology to five stored objects + one pervasive
discipline.

### 3.2 Operations
- **Evidence equivalence** `e ≈_φ e'`, typed: *deductive* → proof irrelevance
  (only existence matters); *inductive* → equal sufficient statistic
  `(n_eff, coverage class)`; *definitional* → equal structural fact.
- **Merge** `K ⊔ K'` — a **partial commutative monoid**: defined iff consistent;
  shared beliefs combined by the confluent update; deductive dominance on
  conflict. *Inconsistency (proof ∧ counterexample) is a finding — a bug in
  design, spec, or tool — not an error to swallow.*
- **Projection / forgetting** `K ↓ S` — risk-monotone: discarding data down to
  the **minimal sufficient statistic** (`n_eff`) is risk-neutral; anything more
  may only raise risk. (This is why episodic memory compresses into semantic
  memory losslessly for risk.)
- **Memory vs evidence** — by prior-robustness (§6 Safe-1), **memory never
  reduces `ℛ`**; it supplies priors that guide the planner, but *reuse informs,
  it does not certify.*

---

## 4. Verification dynamics

Two disjoint kinds. **Update** acts within a fixed index set (sound,
law-bound). **Create** extends the index set (generative, admissibly heuristic).

```
   UPDATE   observe : A → O ;  update : K×E×O → K×E      (Bayes/robust; confluent)
            act/Δ   : A×(D,V) → (D,V)                    (world dynamics; commit mutates D)
            invalidate : commit → V                      (drop stale K within the sound COI)
   CREATE   Γ : (K, M) → H          hypothesis generation  (interface, §4.2)
            abstract : M_e → M_s     concept formation
            proceduralise : M_e → M_p   policy formation
            refine : φ → {φᵢ}        re-basing Φ (§6 Struct-5)
```

### 4.1 The epistemic cycle (first-class)
The verifier is a **closed-loop discrete dynamical system**
`W_{t+1} = Φ_step(W_t)`:

```
   observe → update(evidence) → derive(ℛ) → derive(𝒰, X) → plan(π) → act → observe …
```

Actions produce observations that change knowledge that change risk that changes
the policy that chooses the next action. The loop, not any single morphism, is
the mathematical form of autonomy.

### 4.2 The creation interface
Hypothesis generation is specified only by its **interface and firewall**, never
its mechanism:

```
    Γ : (K, M) → H        Γ may be heuristic, stochastic, learned, symbolic, or
                          human-guided — the foundation is agnostic to which.
```

**Firewall (Safety law, §6 Fire-1):** a created object enters sign-off risk only
after *adjudication that emits a witness*. Generation is untrusted; only
witnessed conclusions are admissible. A generated hypothesis adds a
maximal-uncertainty dimension — so **creation can only raise `ℛ` until evidence
resolves it** (§6 Sem-2′).

---

## 5. Derived functionals (never stored — §7 P1)

- **Belief** `q_j = Query(K, φⱼ)` — confidence that `φⱼ` holds, computed on demand.
- **Residual risk** `ℛ = Σⱼ wⱼ · UBα(n_eff,ⱼ)` over the failure σ-algebra (§6),
  with `UBα(n)=1−(1−α)^{1/n}`, `UBα(0)=1`, deductive ⇒ 0. Prior-robust; the
  unique such functional up to `α` (§6 representation theorem).
- **Exploration value** `X(a) = 𝔼[ Δ(width of the admissible-risk interval) ]`,
  where width = `sup_{π∈Π} risk(π|e) − inf_{π∈Π} risk(π|e)` over the admissible
  (credal) prior set `Π`. `X` measures *how much the still-unearned prior choice
  changes the verdict*; it collapses with **evidence**, not with belief entropy —
  a framework-native exploration term, **not** information gain. Validated:
  width `0.60 → 0.008` with evidence; `X ≥ 0` always
  (`docs/vws_exploration_validation.py`).
- **Utility** `𝒰(a) = value(Δℛ_up(a)) + λ_X·X(a) − cost(a) − time − priority`.
  Exploitation (`Δℛ`) and exploration (`X`) are distinct axes; **`ℛ` is one
  input to `𝒰`, not the objective.** The policy is `π = argmax 𝒰` under budget.

---

## 6. Invariance principles and laws

**Semantic** — the meaning of verification:
- **Sem-1 Formal dominance.** Proof ⇒ `ℛⱼ=0`; counterexample ⇒ known bug.
- **Sem-2′ Monotone-except-at-commits-or-gaps.** `ℛ` never rises under evidence;
  rises only on (a) a commit or (b) hypothesis creation (discovering `Φ` was
  incomplete). *Validated:* `EIG≥0` ⇒ expected `ℛ` non-increasing.
- **Wit-1 Witnessed sign-off.** No sign-off conclusion is admissible without a
  checkable witness.

**Structural** — representation & composition:
- **Struct-1 Confluence.** Independent evidence commutes.
- **Struct-2 Derivation.** `ℛ, 𝒰, X` are functionals of state, never stored.
- **Struct-3 Locality.** Composite state = parts + interface (sheaf gluing).
- **Struct-4 Representation invariance.** Semantically-equivalent representations
  of `D` induce identical decisions.
- **Struct-5 Basis invariance (measure principle).** `ℛ` is a **measure on the
  failure σ-algebra** of `Φ`; refining a property `P → {Pᵢ}` gives
  `ℛ(P) = F(ℛ(P₁),…)` with `F` = inclusion–exclusion (Möbius) composition,
  **invariant to the refinement**; a partition basis ⇒ `F` = addition.
  *Properties are coordinate systems; failures are the invariant reality.*
  *Validated:* two overlapping refinements give risk `7.0 = 7.0 = direct`, naive
  additive gives `9.0 ≠ 8.0` (`docs/vws_struct5_validation.py`).

**Safety** — anti-gaming / assurance:
- **Safe-1 Prior-robustness.** `ℛ` valid over *all* admissible priors — the axiom
  that separates it from entropy/confidence. *Validated:* over-optimistic prior
  gamed entropy/mean-risk but not `ℛ` (`docs/vws_stress_tests.py`).
- **Safe-2 Criticality integrity.** `w` external; `ℛ` reported per criticality
  class, never aggregated across classes.
- **Fire-1 Generation–adjudication firewall.** Created objects enter risk only
  after witnessed adjudication.

**Learning** — self-change:
- **Learn-1 Calibration preservation.** Learning may not manufacture systematic
  over-confidence. *Validated:* misspecified prior → +0.03–0.09 over-confidence,
  hence the law.
- **Learn-2 Warranted transfer.** Cross-design priors used only where structural
  relevance is established, else safe-uninformative default.

**Representation theorem (the `ℛ` characterization).** Under
{additivity-by-expected-count, formal dominance, evidence monotonicity,
redundancy-invariance (via `n_eff`), prior-robustness, tightness, criticality
homogeneity}, the residual-risk functional is **unique up to the assurance level
`α`** and equals the weighted sum of Clopper–Pearson zero-failure bounds with
proven properties at 0. Dropping prior-robustness yields the (gameable)
entropy/confidence family; replacing additivity by weakest-link yields the max
sibling. *Validated:* CP bound is the tightest valid distribution-free bound
(exceedance `0.0198 ≤ 0.05`; any tighter under-covers).

---

## 7. Architectural principles

Not mathematical laws — *implementation fidelity constraints* that keep any
executor faithful to §2–§6:

1. **Derived quantities are never stored** (only recomputed from state).
2. **Every stored object owns its provenance** (`E ⊢ K`).
3. **Every update preserves soundness** (no evidence step may raise confidence
   beyond its warrant).
4. **Every policy decision is reproducible from state** (a re-derivable witness).
5. **Learning may improve efficiency but never invalidate evidence** (Memory ⊄
   certification; Safe-1/Learn-1).

---

## 8. Honest boundaries and assumptions

- **Specification completeness is assumed, not certified.** `ℛ` bounds risk over
  the *given* `Φ`; a bug with no property is invisible. `H`/`Γ` *search* for
  missing properties but cannot self-certify completeness.
- **Inductive knowledge is stimulus-distribution-conditional.** Simulation `ℛ`
  is valid for the sampled distribution; distribution-free assurance needs
  deductive knowledge (`ℛ=0`).
- **Criticality weights are external** (Safe-2).
- **Cross-commit safety ⟺ cone-of-influence soundness.** *Validated:* sound COI
  → P(ship post-commit bug)=0.000; unsound → 0.497 (`docs/vws_stress_tests.py`).
- **Hypothesis ranking has no soundness guarantee** — heuristic by necessity.
- **Closure is provisional.** After repeated adversarial analysis, *no additional
  irreducible object has been identified* (candidates Goals, Planner,
  World-model, Representation, Uncertainty all reduce; the strongest escalation
  found only the missing *ownership relation*, now absorbed). This is a
  defensible engineering claim, **not** a proof of completeness, and invites
  external attack.

---

## 9. Generic execution model

A verifier is *expressible within this foundation* iff: its state is a
law-admissible knowledge object (§2–§3); its changes are the §4 morphisms; its
decisions maximise `𝒰` (§5); and it satisfies §6 and §7. Every such verifier
satisfies the laws; verifiers violating a law are excluded *and the violated law
names the defect* (e.g. a coverage-reward optimiser violates Safe-1, which is
*why* it over-claims). The claim is **universal within these assumptions, not
over all conceivable paradigms.**

---

## Appendix — implementation mappings (non-normative)

*The only place architectures are named.* Each is one implementation of a
primitive; none is the foundation.

- Structural knowledge / `K` definitional layer ← a graph model.
- Hypothesis generation `Γ` ← a language model (proposes; never certifies).
- World dynamics `Δ` ← a learned predictor (predicts; does not hypothesise).
- Policy `π` / `M_p` ← a planning/RL method.
- Deductive adjudication ← a formal engine (emits proof-object witnesses).
- Inductive adjudication ← simulation (emits `n_eff` evidence).

Within AVA specifically: `rtl_graph` (structure), the golden verifiers +
`coverage_collector` (evidence), `formal_engine`/`formal_analysis` (deduction +
COI), `confidence_scorer` (belief, to be upgraded to a posterior),
`self_evolving_engine` (policy, reward → `𝒰`), `verification_twin`
(stopping/readiness), `economics_engine` (cost).

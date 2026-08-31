# Verification World Foundation — Ontology & Algebra (Version 0.9)

**Purpose.** A *stabilising* specification of the foundation's ontology and
algebra — deliberately **not** an implementation. It answers only the six
questions that must stop changing before any `VerificationState` object is
built. A foundation is not an algorithm that uses mathematics, nor a framework
that organises algorithms; it **defines what verifiers are allowed to exist.**

Two coupled worlds. The **Design World `D`** exists independently of any
verifier; the hidden correctness lives here. The **Verification World `V`** is a
verifier's evolving knowledge *about* `D`. The foundation begins at the universe,
not at the verifier:

```
    Universe  →  Design State D  →  Verification State V  →  Interaction  →  Decision
```

Four layers: **A Ontology** (what exists), **B State Algebra** (how it is
represented), **C Dynamics** (how it evolves), **D Laws** (what is forbidden).
Laws are stated over *capabilities*, so they are independent of representation.

---

## Q1. What entities exist? (Layer A — Ontology)

- **Design World `D`** = (RTL/`M`, specification `Φ={φⱼ}`, criticality weights
  `w`, commit lineage `G₀→G₁→…`). Owns the **hidden correctness** `θ* ∈ {0,1}^m`
  (`θ*ⱼ=1 ⟺ M ⊨ φⱼ`). `θ*` is real, fixed per revision, and **not observable**.
- **Instruments `Ω`** = evidence channels (simulator, formal engine, assertion
  checker, waveform, engineer). Each channel emits **observations** with a
  declared **epistemic warrant** (Q2).
- **Actions `A`** = anything that changes `V` or `D`: run-stimulus, run-formal,
  mine-assertion (extends `Φ`), rerun-regression, inspect, request-engineer,
  and **commit** (the only action that mutates `D`).
- **Observations `O`** = the outputs of `Ω` under an action.
- **Verification World `V`** = the verifier's state (Layer B).

## Q2. What information does each entity own? (Layer B — State Algebra)

`D` owns `θ*` (hidden) and the structural form `G`. `V` owns a **Knowledge
State `K`**, an **Evidence ledger `E`**, a **Memory `M`**, and **Resources `C`**
(remaining compute / time / engineer budget).

**Knowledge is typed by epistemic warrant** — the axis that determines how a
fact enters the risk functional:

| Warrant | Knowledge (examples) | Source | Force on risk |
|---|---|---|---|
| **Definitional** | structure: hierarchy, dependencies, cones | `rtl_graph` | *gates relevance* (which evidence counts for which `φⱼ`) |
| **Deductive** | proofs, entailments (certain) | `formal_engine` | drives `ℛⱼ → 0` |
| **Inductive** | observed executions, coverage counts (evidential) | simulation, `coverage_collector` | yields the evidential bound (never 0) |
| *(modality)* **Temporal** | orderings, sequences | `temporal_checker` | qualifies deductive & inductive facts |

**Belief is a *capability of `K`*, not a stored Bayesian primitive.** Any `K`
must expose:

```
    Query      : K → (φⱼ ↦ confidence/plausibility)
    Update     : K × O → K
    Calibration: K → (test of stated vs realised correctness)
    Inference  : K → deductive closure
    Prediction : K → distribution over next observations
```

Bayesian posteriors, Dempster–Shafer masses, credal sets and neural latents are
all candidate implementations. **Admissibility is filtered by the laws** (Layer
D): a `K` is legal only if its `Update` is confluent (Struct-1), its `Query`
supports a prior-robust risk functional (Safe-1), and its `Calibration` is
preserved under learning (Learn-1). An uncalibrated latent is *inadmissible*
until wrapped.

## Q3. What transformations are legal? (Layer C — Dynamics)

Six morphisms, nothing else:

```
    observe : A → O                         (Ω runs the action, emits evidence)
    update  : K × E × O → K × E             (evidence changes knowledge; Layer-D laws apply)
    act/Δ   : A × (D,V) → (D,V)             (world dynamics; commit mutates D)
    invalidate : commit → V                 (stale knowledge dropped within the sound COI)
    plan/π  : V → A     = argmax 𝒰          (decision; utility, not risk, is maximised)
    learn   : M × history → (priors, models) (self-change; calibration-preserving)
```

## Q4. What laws constrain them? (Layer D — categorised)

**Semantic laws** (the *meaning* of verification):
- **Sem-1 Formal dominance.** A sound proof ⇒ `ℛⱼ=0`; a counterexample ⇒ `φⱼ`
  is a *known bug* and leaves the residual pool.
- **Sem-2 Monotone-except-at-commits.** Valid evidence never raises residual
  risk; **only a commit can inject it.** (The single asymmetry in the system.)

**Structural laws** (representation & composition):
- **Struct-1 Confluence.** Independent evidence commutes; belief is
  path-independent.
- **Struct-2 Derivation.** Risk `ℛ` and utility `𝒰` are *functionals of the
  state*, never independently stored.
- **Struct-3 Locality.** A composite state is its parts plus their interface
  (sheaf-style gluing).
- **Struct-4 Representation invariance (NEW).** Semantically-equivalent
  representations of `D` (AST ≡ graph ≡ hypergraph, same `M`/`θ*`) must induce
  **identical** verification decisions. The foundation factors through the
  *semantic* equivalence class, not syntax.
- **Struct-5 Refinement consistency (NEW, non-trivial).** A sound refinement
  `φ ⟺ ⋀φᵢ` must preserve total residual risk **under the aggregation rule**.
  *Open coherence condition:* additive risk over refined parts **double-counts**
  when one root cause fails several parts; consistency therefore requires
  disjoint failure modes or an inclusion–exclusion correction. Refinement
  changes representation, not verification, *only if* this holds.

**Safety laws** (anti-gaming / assurance):
- **Safe-1 Prior-robustness.** `ℛ` must be valid over *all* admissible priors —
  no assumption may shrink it. This is the law that distinguishes RVR from
  entropy/confidence.
- **Safe-2 Criticality integrity.** Weights `w` are set by an authority external
  to the verifier; `ℛ` is reported **per criticality class**, never aggregated
  across classes (a critical property cannot be hidden in a total).

**Learning laws** (self-change):
- **Learn-1 Calibration preservation.** The `learn` map may not manufacture
  systematic over-confidence (the mis-specified-prior failure mode, promoted to
  a law on `M`).
- **Learn-2 Warranted transfer.** Cross-commit / cross-project priors may be
  used only where domain relevance is established; absent that, default to the
  safe uninformative prior.

## Q5. What is derived vs stored? (the non-redundancy discipline)

- **Stored:** `D` (external), and in `V`: `K` (typed knowledge), `E` (evidence),
  `M` (memory), `C` (resources).
- **Derived (never stored as independent state):**
  - **Residual risk** `ℛ(K,E;w)` — the RVR characterised by the representation
    theorem (weighted sum of prior-robust zero-failure bounds; proof ⇒ 0). One
    functional, obeying Sem-1/Sem-2/Safe-1.
  - **Utility** `𝒰(value(Δℛ), cost, time, priority)` — the decision objective.
    **RVR is one input to `𝒰`, not the objective** (risk↓ with runtime 100× is a
    bad plan). `plan` maximises `𝒰` under the `C` budget.
  - **Belief queries and predictions** — computed from `K` on demand.

## Q6. What assumptions define the limits? (honest boundary)

The foundation is **universal within these assumptions, not over all conceivable
paradigms** (this also weakens the genericity claim as required):

1. **Specification completeness is assumed, not certified.** `ℛ` bounds risk
   over the *given* `Φ`; a bug with no property (Spectre-class) is invisible. No
   law repairs this; a separate spec-completeness signal (mutation/assertion
   mining) is required and cannot be self-certified.
2. **Inductive knowledge is stimulus-distribution-conditional.** Simulation-
   derived `ℛ` is valid only for the sampled distribution; distribution-free
   assurance requires deductive (formal) knowledge.
3. **Criticality weights are external** (Safe-2).
4. **Cross-commit safety reduces to COI soundness.** Sem-2 + `invalidate` are
   safe iff the cone-of-influence is sound (over-approximating).

**Genericity (scoped).** *Every verifier expressible within this foundation —
state = a law-admissible knowledge capability, changes = the six morphisms,
decision = a utility-max policy — satisfies Layer-D laws; and existing
approaches are expressible instances* (coverage-RL = a policy whose `ℛ` violates
Safe-1, which is *why* it over-claims; formal sign-off = a deductive `Ω` channel;
LLM assertion-gen = an action extending `Φ`; GNN/transformer = a representation
of `K`; Dreamer-style world model = a learned `Δ`). The AI is the **runtime that
executes the foundation**, not the foundation.

---

## Stabilisation checklist (when to freeze v1.0)

Freeze and implement the `VerificationState` object only when these stop moving:
the knowledge-type basis (Q2), the six morphisms (Q3), the law set + categories
(Q4), the derived/stored split (Q5), and the assumption boundary (Q6). Current
open items: **Struct-5** (refinement/aggregation coherence) and whether the
knowledge-warrant basis is complete (does "resource/economic" knowledge warrant
a fifth row, or is it a parameter of `𝒰`?).

# Verification World Foundation — Addendum toward v1.0: The Creation Layer

**Extends** `VWS_ONTOLOGY_v0.9.md`. Adds the objects and dynamics that let a
verifier *create* knowledge, not only *update* it — and proposes a **closure
argument** so the ontology can stop growing. Still a specification, not an
implementation.

**The core deficiency of v0.9.** Its dynamics move probability *within a fixed
space* `Φ`. That is a Bayesian filter, not a reasoner. Reasoning **grows the
space**: it invents properties, concepts, and policies that were in no prior
belief. This addendum makes space-growth first-class.

---

## New objects

### `H` — Hypothesis Space (second-order belief over the specification)
`Φ` is no longer fixed/given; it is the *currently-believed* property set. `H`
is the space of candidate properties `h` **not yet in `Φ`**, each carrying a new
epistemic quantity distinct from correctness belief:

```
   θⱼ  = P(φⱼ holds)                     — belief about the DESIGN (had it before)
   σ(h) = P(h SHOULD be a property)      — belief about the SPECIFICATION (new)
```

`σ` addresses the spec-completeness limit (v0.9 Q6.1): it cannot be *certified*,
but it can be *actively searched and ranked*. Estimated from mutation survival
(behaviour no property constrains), structural novelty (uncovered cones), and
intent alignment — **necessarily heuristic**, because it reaches into
unknown-unknowns.

### `M` splits into `(M_e, M_s, M_p)` — by mathematical type
- **`M_e` Episodic** — `(state, action, obs)` trajectories. *Extensional.*
- **`M_s` Semantic** — abstractions keyed by structural signature
  (`rtl_graph` embedding) → priors, property-templates, likelihoods.
  *Intensional.* Where cross-design transfer lives; governed by Learn-1/Learn-2.
- **`M_p` Procedural** — policies (design-class → effective actions); the
  amortised planner.

### `J` — Justification / Witness (proof-carrying computation)
Every derived value is paired with a checkable derivation:
`belief ↦ evidence trace`, `decision ↦ utility computation + beaten
alternatives`, `risk ↦ {deductive proof-object | inductive (n_eff, CP-bound)}`.
`J` makes "why" a mathematical object, and it underwrites certification
traceability (DO-254 / ISO-26262).

---

## New dynamics — creation vs update (answers Q7)

The state space is itself dynamic. Two disjoint dynamics:

```
   UPDATE  : acts WITHIN a fixed index set        — sound, contractive, law-bound
             (evidence ⊕ belief; v0.9 morphisms)
   CREATE  : EXTENDS the index set                — generative, AI-driven, untrusted
             hypothesise : V → H         (grow Φ — LLM's justified role)
             abstract    : M_e → M_s     (grow concepts — generalisation)
             proceduralise: M_e → M_p    (grow policies)
             refine      : φ → {φᵢ}       (re-base Φ; subject to v0.9 Struct-5)
```

## New / amended laws

**Fire-1 Generation–adjudication firewall (SAFETY, new).** A created object
(hypothesis, abstraction, policy) enters the sign-off risk **only after
adjudication that produces a witness `J`**. Generation is untrusted; only
witnessed, adjudicated conclusions are admissible. *This is what lets an LLM
operate in the loop without corrupting soundness.*

**Wit-1 Witnessed sign-off (SEMANTIC, new).** No sign-off-relevant conclusion is
admissible without a checkable `J`. An unwitnessed decision may *propose*, never
*conclude*.

**Sem-2′ Monotone-except-at-commits-or-gaps (AMENDED).** Residual risk is
non-increasing under evidence, and rises **only** on (a) a commit (design
change) or (b) hypothesis creation (discovering `Φ` was incomplete). Both are
the verifier honestly learning it knew less than it thought.

**Abstraction obeys calibration.** `abstract : M_e → M_s` is a *creation* op but
its transfer is still bound by Learn-1 (no manufactured over-confidence) and
Learn-2 (warranted transfer) — a new-but-similar cache inherits priors only where
structural relevance is established, else defaults to safe-uninformative.

---

## The AI runtime (where models finally attach)

Specialised reasoning engines, all operating on the *same* state, each an
implementation of a primitive — none is "the framework":

```
   GNN            → structural reasoning (represents K's definitional layer)
   LLM            → specification reasoning (drives `hypothesise`; proposes h ∈ H)
   World model Δ  → prediction (a learned dynamics; prediction ≠ hypothesis)
   RL / MCTS      → planning (amortised into M_p)
   Symbolic/formal→ deduction (adjudicates; emits proof-object witnesses)
   Simulation     → induction (adjudicates; emits n_eff evidence)
```

`Δ` (the world model) *predicts*; it does **not** hypothesise. Dreamer predicts;
the `hypothesise` morphism is what makes this a reasoner rather than a predictor.

---

## Closure argument — why the ontology can now stop growing

A foundation needs a reason it is *complete*, or it is just an ever-growing list.
**Proposed closure:** an *accountable bounded-rational verifier* requires exactly
six functional roles, and every object above reduces to one:

| Role | Object(s) | Why irreducible |
|---|---|---|
| **World** | `D` | the thing being reasoned about (holds hidden `θ*`) |
| **Knowledge** | `K` (+ `E` its warrant) | what is currently held about the world |
| **Possibility** | `H` | what could be true beyond the current model (the generative frontier) |
| **Memory** | `M_e, M_s, M_p` | experience, concepts, policies over time |
| **Value** | `ℛ, 𝒰` (derived) | how to rank states and actions |
| **Accountability** | `J` | why any conclusion is admissible (the verification-specific role) |

Resources `C` parameterise Value. The claim: **{World, Knowledge, Possibility,
Memory, Value, Accountability} is a closed basis** — any proposed new object
either reduces to one of these or is a *representation* of one (an
implementation detail), not a new primitive.

**This claim is meant to be attacked.** The falsification test: *name a
mathematical object an autonomous, accountable verifier must maintain that does
not reduce to these six roles.* Until such an object is exhibited, the ontology
is complete and v1.0 may freeze. (Accountability — `J` — is the role most agent
frameworks omit and the one hardware verification most needs; its presence is
what makes this a *verification* foundation rather than a generic agent schema.)

---

## Status

- Objects added: `H`, `M`→`(M_e,M_s,M_p)`, `J`. Q7 answered (create vs update).
- Laws added/amended: Fire-1, Wit-1, Sem-2′; abstraction under Learn-1/2.
- **Open before freezing v1.0:** (i) resolve Struct-5 (refinement/aggregation
  coherence — still the load-bearing pure-math gap); (ii) stress-test the
  **closure argument** by trying to exhibit a seventh irreducible object;
  (iii) decide if hypothesis-ranking's heuristic core admits *any* soundness
  guarantee (likely: none — and that boundary should be stated, not hidden).
- **Not** to be implemented until (i)–(ii) stop changing.

# AVA-VWS: Verification as Sequential Knowledge Acquisition
### A formal state-based framework for autonomous hardware verification

**Status:** theory / research formulation (v0.1, 2026-07-26)
**Scope:** the mathematics only. No implementation choice (GNN / RL / diffusion)
is made here; those are optimizers that must *serve* this formulation, not
define it.

---

## 0. Thesis

Every existing formulation optimises a **proxy** (coverage), and defines the
state as that proxy. We reframe:

> Verification is **sequential Bayesian acquisition of knowledge about whether a
> design satisfies its specification**, under a compute budget. The quantity
> being maximised is *information about correctness per unit cost* — not
> coverage.

Coverage, assertions, formal proofs and regressions are then not the objective;
they are **evidence-producing actions** that update a belief. This single shift
makes simulation and formal methods commensurable (both produce bits of the same
currency), gives uncertainty a precise meaning, and lets us state invariants and
prove convergence.

Notation: random variables in bold or capitalised; `𝔼` expectation; `H` Shannon
entropy; `I` mutual information; `H₂(p) = −p·log₂p − (1−p)·log₂(1−p)`.

---

## 1. The verification environment

A **verification environment** is a triple

```
    E = (D, Φ, Ω)
```

- **D** — the design under verification, modelled as a transition system
  `M = (S, S₀, T, L)` (states, initial states, transition relation, labelling).
  `M` is only *partially* known to the verifier.
- **Φ = {φ₁, …, φ_m}** — the specification: a finite set of properties
  (temporal-logic formulae / SVA / architectural intents) the design must
  satisfy. Each `φⱼ` may carry a criticality weight `wⱼ > 0`
  (a security or coherence property outweighs a cosmetic one).
- **Ω** — a set of **instruments** (oracles): simulator, formal engine,
  assertion checker, regression runner, waveform inspector, … Each instrument,
  when invoked, returns *evidence* about Φ, never the ground truth directly.

### 1.1 The hidden ground truth

Define the **correctness vector**

```
    θ* ∈ {0,1}^m ,     θ*ⱼ = 1  ⟺  D ⊨ φⱼ .
```

`θ*` is fixed (for a given design revision) but **hidden**. Classical formal
verification tries to *compute* `θ*` exactly; classical simulation tries to
*raise coverage*. We instead try to **know** `θ*` — to drive a belief over it to
certainty — which subsumes both.

---

## 2. The Verification World State (VWS)

```
    W_t = ( G_t , β_t , E_t , H_t )
```

| Symbol | Name | Content |
|---|---|---|
| `G_t` | structural context | RTL graph, module hierarchy, active commit, cone-of-influence map |
| `β_t` | **belief** | a distribution over the hidden `θ*` — the epistemic core |
| `E_t` | evidence ledger | coverage bins hit, assertion pass/fail, formal verdicts + depths, counterexamples |
| `H_t` | history | commit lineage and the sequence `(β_0, a_0, o_0, β_1, …)` |

The **belief** is the state that matters:

```
    β_t : {0,1}^m → [0,1] ,     Σ_θ β_t(θ) = 1 .
```

The full joint is `2^m`-dimensional. We adopt an explicit **mean-field
factorisation** (independence across properties) as the working approximation:

```
    β_t(θ) ≈ Π_j  p_{t,j}^{θⱼ} · (1 − p_{t,j})^{1−θⱼ} ,
    p_{t,j} := P(θ*ⱼ = 1 | evidence ≤ t) ∈ [0,1] .
```

`p_{t,j}` is the **confidence** that property `j` holds. (This is exactly what
AVA's `confidence_scorer` gestures at; here it is a calibrated posterior, not a
heuristic score. Correlations between properties can be reintroduced later via a
structured belief — a factor graph over `G_t` — at extra cost; §11.)

> **Design decision.** The state is the *belief about correctness*, not the
> coverage. Coverage enters only through `E_t` as one kind of evidence that
> updates `β_t`.

---

## 3. Knowledge and uncertainty (information-theoretic)

Per-property uncertainty is binary entropy; total uncertainty is the
criticality-weighted sum:

```
    u_{t,j} = H₂(p_{t,j}) ,       U_t = Σ_j wⱼ · u_{t,j} .
```

`u_{t,j}` is maximal (`wⱼ`) at `p = ½` (know nothing) and `0` at `p ∈ {0,1}`
(certain — proven or disproven). Define **knowledge** relative to the prior:

```
    K_t = U_0 − U_t   ( bits of weighted correctness-uncertainty removed ).
```

This gives the roadmap's informal "Knowledge(t)" a precise, non-arbitrary
meaning: **knowledge is uncertainty eliminated about `θ*`.** A test that raises
coverage but tells you nothing new about whether a property holds yields
`ΔK = 0` — correctly valued at zero.

---

## 4. Action space

```
    A = { a } ,   each with a cost  c(a) ≥ 0  (compute + engineer time).
```

Actions are **any** knowledge-changing verification operation, not just stimulus:

```
  a ∈ {  run_stimulus(x),  run_formal(φⱼ, depth d),  check_assertion(ψ),
         mine_assertion(region),  rerun_regression(suite),
         inspect_waveform(sig),  refine_abstraction(module),
         request_engineer(query),  … }
```

Heterogeneity is the point: a formal proof and a simulation run are both actions
whose *outcomes are evidence in the same belief-update calculus* (§6), so the
planner can trade them off on one axis (§7).

---

## 5. Observation model (POMDP kernel)

The verifier never sees `θ*`; it sees an **observation** `o` drawn from an
action-conditioned kernel:

```
    o ~ P( o | W_t , a ) ,     with per-property likelihood
    L_j(o | θ*ⱼ , a) = P( observing o | property j holds / fails, action a ).
```

The likelihood encodes how much an outcome tells you about correctness:

- **Simulation** of behaviour relevant to `φⱼ` that **passes** →
  `L_j(pass | 1) > L_j(pass | 0)`, but *bounded away from certainty*
  (simulation is incomplete: passing raises `p` toward, but never to, 1).
- **Complete formal proof** (depth `d ≥` completeness threshold for `φⱼ`) →
  irrefutable evidence: `o` sets `p → 1`.
- **Counterexample** (sim or formal) → definitive disproof: `p → 0`.
- **Bounded formal proof** (depth `d` below threshold) → partial evidence:
  raises `p` by an amount increasing in `d` (no reachable violation within `d`
  steps makes a violation less likely, not impossible).

> **Key separation.** In §6 the *dynamics are exact Bayes' rule*; the only object
> that need be **learned** from data is the likelihood `L_j(o | θ, a)` — "how
> informative is this action's outcome about correctness." This is far more
> defensible (and less prone to fabrication) than learning an end-to-end
> next-state map `F`.

---

## 6. Transition = Bayesian belief update

The roadmap asked for a learned `W_{t+1} = F(W_t, a_t)`. We sharpen this: the
transition **is Bayes' theorem**; learning is confined to the likelihood.

```
    β_{t+1}(θ) = P(θ | o, a, β_t) = L(o | θ, a) · β_t(θ) / Z ,   Z = Σ_θ L·β_t .
```

Under the mean-field factorisation this is a per-property scalar update:

```
                       L_j(o | 1, a) · p_{t,j}
    p_{t+1,j}  =  ─────────────────────────────────────────────── .
                  L_j(o|1,a)·p_{t,j} + L_j(o|0,a)·(1 − p_{t,j})
```

The rest of the state updates deterministically: `G_{t+1}` changes only on a
commit; `E_{t+1} = E_t ∪ {(a, o)}`; `H_{t+1}` appends `(a, o, β_{t+1})`.

**Commit handling (belief carry-over).** When `G` changes by a commit `Δ`
touching a set of modules `μ(Δ)`, beliefs are *invalidated only within the cone
of influence* of `μ(Δ)` (computed by AVA's `cone_of_influence`): for `φⱼ`
depending on `μ(Δ)`, reset `p_{t,j}` toward its prior; all others persist. This
is how history `H_t` produces value — proofs and evidence survive across commits
except where logically disturbed.

---

## 7. Objective: expected information gain per unit cost

Define the **expected information gain** (value of information) of an action:

```
    EIG(a | W_t) = U_t − 𝔼_{o ~ P(o|W_t,a)} [ U_{t+1} | a, o ]
                 = I( θ ; O_a | W_t )        (weighted mutual information).
```

`EIG(a)` is the expected number of weighted bits of correctness-uncertainty the
action removes. The campaign objective, under a budget `B`:

```
    maximise   Σ_t ΔK_t      subject to   Σ_t c(a_t) ≤ B .
```

Equivalently, a Lagrangian utility that recovers the roadmap's `R = αI + βB +
γC − δT − εE` as a *special case* (with information `I` primary and the rest as
cost/criticality weights):

```
    J(π) = 𝔼[ Σ_t ( ΔK_t − λ · c(a_t) ) ] .
```

**Planner (Bayesian experimental design).** The greedy information-per-cost rule:

```
    a*_t = argmax_{a ∈ A}   EIG(a | W_t) / c(a) .
```

This unifies test generation, formal invocation and regression scheduling under
one criterion. It is the principled replacement for "reward = coverage": the
planner spends the next compute-dollar wherever it buys the most certainty about
correctness. (AVA's `self_evolving_engine` is the natural host — upgrade its
bandit reward from coverage-delta to `EIG/cost`.)

---

## 8. Invariants

Stated as theorems with the honest qualifier (expectation vs. almost-sure).

- **(I1) Belief validity.** `β_t` remains a valid distribution for all `t`
  (`0 ≤ p_{t,j} ≤ 1`, normalised). *Bayes preserves the simplex.* ∎
- **(I2) Non-negative expected knowledge.** For any *sound* likelihood,
  `EIG(a) = I(θ;O_a) ≥ 0`, hence `𝔼[U_{t+1}] ≤ U_t`. Expected knowledge is
  non-decreasing. (A single *surprising* observation may momentarily raise
  uncertainty; the roadmap's "knowledge never decreases" holds correctly **in
  expectation**, which is the precise and honest statement.)
- **(I3) Proof permanence.** If a complete proof sets `p_{t,j} = 1`, then
  `p_{s,j} = 1` for all `s > t` until a commit enters `φⱼ`'s cone of influence.
- **(I4) Counterexample permanence.** Symmetric: a real violation sets and holds
  `p = 0` until the offending logic changes.
- **(I5) Calibration.** If likelihoods are calibrated, beliefs are calibrated:
  among properties assigned `p ≈ q`, a fraction `≈ q` truly hold. (This is the
  testable honesty condition; §10.)

---

## 9. Theoretical properties

**Convergence.** Under the `EIG/cost`-greedy policy, `U_t` is a non-negative
supermartingale (`𝔼[U_{t+1} | ℱ_t] ≤ U_t` by I2, `U_t ≥ 0`). By the martingale
convergence theorem `U_t` converges almost surely. Add the **informativeness
assumption** — every unresolved property has some action with `EIG > ε` — and
`U_t → 0`: the campaign resolves every property in finite expected budget.
*Interpretation:* verification terminates in the knowledge sense, not merely
"runs out of tests."

**Optimality gap.** Greedy `EIG/cost` is the classical knapsack-VoI heuristic;
because `U_t` is not in general submodular across correlated properties, greedy
is not exactly optimal, but under the mean-field (independent) belief the
per-step objective **is** modular and greedy is optimal per step. State this
limitation plainly; the correlated case (§11) trades tractability for tightness.

**Complexity.** Exact belief is `O(2^m)`. The mean-field update is
`O(|COI(a)|)` per action (only properties in the action's influence change), and
one planning step is `O(|A| · c_EIG)` where `c_EIG` is the cost of an EIG
estimate (closed-form under mean-field; sampled otherwise). This is what makes
the framework runnable at chip scale.

**Strict improvement over random.** Random stimulus corresponds to `𝔼[EIG]`
averaged over `A`; the `argmax` policy has `EIG(a*) ≥ 𝔼_a[EIG(a)]`, so expected
knowledge-per-cost is provably ≥ random, with equality only when all actions are
equally informative (a degenerate design). This is the formal version of "better
than random testing" — provable in expectation, to be *demonstrated
empirically* in magnitude.

---

## 10. What is genuinely new — and what is borrowed (honest)

**Borrowed, established:** POMDP framing; value of information / Bayesian
experimental design; entropy as uncertainty; transition systems and temporal
logic; active learning; coverage-directed generation.

**The contribution is the composite:**
1. A single **correctness-belief state** `β` over `θ*` that makes **simulation
   and formal evidence commensurable** in one Bayesian update.
2. Objective = **expected information gain per unit compute over a heterogeneous
   action space** (sim + formal + assertion-gen + regression), not coverage,
   not stimulus-only.
3. **Invariants + convergence proved on that state** (I1–I5, §9) — the roadmap
   is right that these are almost never stated in verification work.
4. The **exact-dynamics / learned-likelihood split** (§5–6): learn *how
   informative outcomes are*, keep the *update* provably Bayesian.

**Honest claim boundary.** This is a principled, testable *framework*. It is
**not** yet demonstrated to beat CDG/RL verification in practice — that requires
the empirical study in §12. Claiming superiority before that would be exactly
the kind of unearned result this project has refused to ship.

---

## 11. Extensions (deliberately deferred)

- **Correlated belief:** replace mean-field with a factor graph over `G_t`
  (properties sharing logic are dependent); update by loopy BP. Tighter,
  costlier.
- **Continuous confidence per structural unit:** beliefs per module/interface,
  not just per property, giving the doc's "confidence per module."
- **Non-stationary `θ*`:** designs change; treat commits as controlled
  interventions (do-calculus) rather than resets.
- **Multi-agent:** several planners sharing one `W_t` (assertion-gen agent,
  formal agent, stimulus agent) as cooperative VoI maximisers.

---

## 12. Mapping to AVA + the empirical validation path

The framework is not detached theory — AVA already instantiates most components:

| VWS element | Existing AVA piece | Gap to close |
|---|---|---|
| `G_t` structural context | `rtl_graph` | none (done) |
| Evidence `E_t` | 70 golden verifiers, `coverage_collector`, `formal_engine` | wrap outcomes as likelihood evidence |
| Belief `β_t` | `confidence_scorer` | upgrade heuristic score → calibrated posterior |
| Proof permanence / COI | `formal_analysis.cone_of_influence` | none (done) |
| Likelihood `L_j(o\|θ,a)` | — | **learn from AVA-generated `(action→outcome)` logs** |
| Planner `argmax EIG/cost` | `self_evolving_engine` | reward: coverage-delta → `EIG/cost` |
| Cost model `c(a)` | `economics_engine`, `regression_intelligence` | none (done) |

**The one real dependency** is the same as before: to *learn the likelihood
model* and to *empirically show `EIG/cost` beats the current bandit*, you need a
dataset of `(W_t, a, o)` tuples — generatable by AVA's own generators + goldens
under **Verilator** (open-source), needing CPU-hours, no proprietary tools, no
hardware. Everything up to that — the state object, the exact Bayesian update,
the entropy/knowledge metrics, the EIG planner with a *hand-specified* likelihood
— is implementable now and testable on the corpus.

**Falsifiable success criterion.** On a fixed compute budget across corpus cores
(Ibex, cv32e40p), the `EIG/cost` planner reaches a target *knowledge* level
`K_target` in fewer simulation-seconds than (a) random and (b) the coverage-reward
bandit — and its beliefs are calibrated (§I5). If it does not, the framework is
falsified for that setting, and we report that.

---

## 13. Immediate next artifacts (theory-first order)

1. This document — the formal framework. **(done, v0.1)**
2. A reference implementation of the **VWS state object + Bayesian update +
   entropy/EIG** with a *specified* (not learned) likelihood — pure Python,
   testable on the corpus, no data needed.
3. A **data-generation harness** (Verilator + AVA generators + goldens) emitting
   `(W_t, a, o)` tuples — the input the learned likelihood and the empirical
   study need.
4. Only then: choose the optimiser (GNN/transformer/RL) for the likelihood, and
   run the §12 falsifiable study.
```

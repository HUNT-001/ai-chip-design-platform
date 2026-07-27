# A Mathematical Foundation for Verification as Sequential Knowledge Acquisition

**Version 1.0 — 2026-07-26.** Supersedes the sketch in `VWS_FRAMEWORK.md`.
**Status:** theory, *numerically validated* (§7). No optimiser (GNN/RL/…) is
chosen here; those must serve this formulation.

> **One-sentence claim.** Verification is the sequential reduction of a
> **calibrated Bayesian belief's entropy about whether a design meets its
> specification**, where simulation and formal methods are heterogeneous
> evidence-producing actions selected to **maximise information gained per unit
> compute**, and where "done" is an **optimal-stopping** decision on that
> entropy.

Every quantitative claim below is checked by Monte-Carlo in §7; the code is
`docs/vws_validation.py`.

---

## 1. Honest positioning (read this first)

This is a **reformulation**, and its value depends on being precise about what
is old and what is new. The individual tools are established:

- MDP/RL for **coverage-directed** test generation (reward = coverage)
  [Pan–Mishra; StimulusRL; UF survey 2023].
- **Bayesian networks** for coverage-directed generation [Fine–Ziv].
- **Value of information / Bayesian experimental design** in other engineering
  fields [ORNL milling; VoI reviews].
- **Zero-failure reliability**: confidence from passing tests, the *rule of
  three* [Miller et al., *Estimating the Probability of Failure When Testing
  Reveals No Failures*, IEEE TSE 1992].
- Industry **merging of formal + simulation coverage** into a combined *coverage*
  grade [Cadence/Synopsys sign-off flows].
- **Active-information-acquisition POMDPs** / info-gain reward shaping in
  robotics & agentic-AI validation.

**The gap this framework fills — the novel composite:**

1. A single **posterior over the correctness vector** `θ*` that fuses
   **simulation evidence (bounded, reliability-style) and formal evidence
   (proof ⇒ certainty, counterexample ⇒ disproof) in one Bayesian calculus.**
   Industry merges *coverage numbers*; reliability theory handles simulation
   only; the prior-art search surfaced no *calibrated correctness posterior*
   unifying both.
2. Objective = **expected information gain per unit compute over a heterogeneous
   action space** (run-sim vs run-formal vs mine-assertion) — the planner
   *trades formal against simulation on one information axis*, not a coverage
   axis with stimulus-only actions.
3. **Cross-commit belief carry-over via cone-of-influence** (proof permanence).
4. An **optimal-stopping tape-out rule** on the uncertainty supermartingale.
5. **Invariants and convergence proved** on that unified state, then validated.

**Claim boundary.** This is a *principled, testable* framework. It is **not yet
shown to beat** coverage-directed RL in practice — that needs the empirical
study of §9, which needs data (§10). Asserting superiority now would be
unearned.

---

## 2. Survey of candidate mathematical foundations (and the choice)

| # | Framework | What it offers | Verdict |
|---|---|---|---|
| A | MDP/RL, reward = coverage | scalable stimulus policies | **Rejected as the foundation.** A Markov reward cannot credit "knowing more"; coverage is a proxy. |
| B | **POMDP over a correctness belief + Bayesian experimental design** | hidden truth, heterogeneous evidence, information objective | **Chosen core.** |
| C | Optimal stochastic control (belief-MDP Bellman) | non-myopic optimal planning | **Folded in** as the planner's optimality theory; EIG-greedy is its 1-step approximation. |
| D | Optimal stopping | when to *stop* verifying | **Folded in** as the tape-out rule (§6.5). |
| E | PAC / statistical learning theory | sample-complexity bounds | **Folded in**; the rule of three is the sim-only PAC bound (§5.3). |
| F | Dempster–Shafer / imprecise (credal) probability | separates "no evidence" from "balanced evidence" | **Considered, not primary.** The spike-and-slab belief (§4) already separates them (concentration = evidence mass). Kept as an extension for adversarial unknown-unknowns. |
| G | Information geometry (Fisher metric on the belief simplex) | EIG-greedy ≈ steepest-entropy-descent | **Noted refinement**, not required. |
| H | Game theory (verifier vs. adversarial bug-placer) | worst-case/robust guarantees | **Extension** for security properties. |

Foundation = **B**, with **C** (planner), **D** (stopping), **E** (sample
complexity). F/G/H are documented alternatives.

---

## 3. The verification environment

`E = (D, Φ, Ω)`: design `D` (a partially-known transition system
`M=(S,S₀,T,L)`), specification `Φ={φ₁,…,φ_m}` with criticality weights `wⱼ>0`,
and instruments `Ω` (simulator, formal engine, assertion checker, …).

**Hidden ground truth.** `θ* ∈ {0,1}^m`, `θ*ⱼ = 1 ⟺ D ⊨ φⱼ`. Fixed per design
revision, hidden. Goal: *know* `θ*` — drive a calibrated belief to certainty —
not maximise coverage.

---

## 4. The generative model (derived, not posited)

The earlier sketch *posited* likelihoods `L_j(o|θ,a)`. We instead **derive** the
belief dynamics from a generative model, which makes the confidence updates
principled and, as a bonus, reduces to classical reliability theory (§5.3).

For each property `φⱼ`, define the **violation rate**

```
    ρⱼ = P( a single relevant random stimulus exposes a violation of φⱼ ) ∈ [0,1],
    θ*ⱼ = 1  ⟺  ρⱼ = 0 .
```

**Prior — spike-and-slab** (the key modelling choice):

```
    ρⱼ ~ πⱼ · δ₀  +  (1−πⱼ) · Beta(aⱼ, bⱼ)      on [0,1],
```

a point mass `πⱼ` at `ρ=0` ("property holds exactly") plus a continuous "buggy
with some rate" density. The spike-vs-slab split distinguishes *true ignorance*
(diffuse slab, low `π`) from *balanced evidence* — the objection that motivates
Dempster–Shafer, handled inside Bayes.

**Evidence.** `n` independent *relevant* passing tests have likelihood
`P(n passes | ρ) = (1−ρ)^n`. A single failure is a definitive counterexample.

**Posterior mass on "holds"** — the confidence `qⱼ = P(θ*ⱼ=1 | evidence)`:

```
                          πⱼ
    qⱼ(n) = ───────────────────────────────── ,   Mⱼ(n) = 𝔼_Beta(aⱼ,bⱼ)[(1−ρ)^n]
             πⱼ + (1−πⱼ)·Mⱼ(n)                            = B(aⱼ, bⱼ+n) / B(aⱼ,bⱼ)
```

with `B` the Beta function (closed form via `lgamma`). A **counterexample** sets
`qⱼ=0`; a **complete formal proof** sets `qⱼ=1` directly (§6.3).

> Coverage now has a precise role: it decides whether a test is *relevant*
> evidence for `φⱼ`. A test not exercising `φⱼ`'s cone gives `n += 0` for `φⱼ` —
> zero information — which is exactly why "coverage up, knowledge flat" tests are
> valued at `ΔK = 0`.

---

## 5. State, knowledge, and the belief update

### 5.1 Verification World State
`W_t = (G_t, β_t, E_t, H_t)`: structural context `G_t` (RTL graph, commit, COI
map), belief `β_t = {(πⱼ,aⱼ,bⱼ,nⱼ, provenⱼ)}`, evidence ledger `E_t`, history
`H_t`. Under the mean-field (per-property independence) approximation the belief
is the vector `q_t = (q_{t,1},…,q_{t,m})`.

### 5.2 Uncertainty and knowledge
```
    u_{t,j} = H₂(q_{t,j}),     U_t = Σⱼ wⱼ·u_{t,j},     K_t = U_0 − U_t .
```
`H₂` = binary entropy; `U_t` = weighted correctness-uncertainty (bits);
`K_t` = knowledge = uncertainty eliminated.

### 5.3 Reduction to classical reliability (a sanity anchor)
With `π→0⁺` and a uniform slab `Beta(1,1)`, the posterior on `ρ` after `n`
zero-failure relevant tests is `Beta(1, n+1)`, so the 95% upper credible bound
solves `(1−x)^{n+1}=0.05`, giving `x ≈ 3/n` — the **rule of three**. *Validated*
(§7, Test 1). The framework is therefore *consistent with established
reliability theory in the sim-only limit*; the novelty is the unification, not
the belief in isolation.

---

## 6. Actions, evidence, objective, stopping

### 6.1 Action space
`A`, each `a` with cost `c(a)≥0`: `run_stimulus(x)`, `run_formal(φⱼ,d)`,
`mine_assertion`, `rerun_regression`, `inspect_waveform`, `request_engineer`, …
Heterogeneous by design.

### 6.2 Observation & transition = Bayes
Applying `a` yields evidence `o`; the belief updates by the closed forms of §4
(Bayes' rule). **Dynamics are exact Bayes; only the per-action relevance/failure
likelihood is empirical** — a far smaller, more honest learning target than an
end-to-end next-state map.

### 6.3 Formal evidence
`run_formal(φⱼ, d)`: a **complete** proof (`d ≥` completeness threshold, from
AVA's `formal_engine`) sets `qⱼ=1` (invariant I3); a **counterexample** sets
`qⱼ=0` (I4); a **bounded** proof with no violation multiplies the slab moment by
the depth-`d` survival factor — partial evidence, `qⱼ` rises but not to 1.

### 6.4 Objective and planner
```
    EIG(a | W_t) = U_t − 𝔼_{o}[ U_{t+1} | a,o ] = Σⱼ wⱼ · I(θⱼ ; O_a) ,
    a*_t = argmax_{a∈A}  EIG(a | W_t) / c(a) ,
    campaign objective:  max Σ_t ΔK_t   s.t.  Σ_t c(a_t) ≤ B .
```
This is the **Bellman-optimal belief-MDP policy's myopic (1-step) approximation**
(framework C); the non-myopic optimum solves the belief-MDP but is intractable,
so greedy VoI is the principled tractable choice.

### 6.5 Tape-out = optimal stopping
Continue while the best information-per-cost exceeds the marginal value of
certainty; **stop** when
```
    max_a EIG(a|W_t)/c(a)  <  λ*   (the sign-off information price),
```
or when weighted uncertainty on all *critical* properties is below tolerance
(`Σ_{j: wⱼ≥w_crit} wⱼ u_{t,j} < τ`). An optimal-stopping rule on the
supermartingale `U_t` (framework D); it gives `verification_twin`'s tape-out
readiness a decision-theoretic basis.

---

## 7. Theorems — with proofs and numerical validation

All validated by `docs/vws_validation.py` (fixed seed).

**T1 (Belief validity, I1).** `q_{t,j}∈[0,1]` and `β_t` is a valid distribution
∀t. *Proof:* Bayes maps the simplex to itself. ∎

**T2 (Non-negative expected knowledge, I2).**
`EIG(a)=Σⱼ wⱼ I(θⱼ;O_a) ≥ 0`, so `𝔼[U_{t+1}|ℱ_t]=U_t−EIG(a_t) ≤ U_t`. *Proof:*
conditional mutual information ≥ 0; `wⱼ>0`. ∎ Knowledge is non-decreasing **in
expectation** — the correct statement (a single surprising observation can
momentarily raise `u_j`; the mean cannot). *Validated:* worst EIG over 20,000
random states = **0.0**.

**T3 (Supermartingale convergence).** `(U_t)` is a non-negative supermartingale
(T2 + `U_t≥0`), so `U_t→U_∞` a.s. (Doob). Under the **informativeness
assumption** (∃ε>0: every unresolved property admits an action with
`EIG/cost ≥ ε`), greedy drives `U_∞=0`. *Proof sketch:* `𝔼[U_t−U_{t+1}] =
𝔼[EIG(a_t)] → 0`; greedy maximises EIG ⇒ `max_a EIG → 0`; informativeness then
forces every property resolved. ∎ *Validated:* greedy, large budget → **0.018**.

**T4 (Calibration, I5).** Under a well-specified prior, `𝔼[𝟙(θⱼ=1) |
q_{t,j}=q] = q`. *Proof:* Bayesian self-consistency under the generating model.
∎ *Validated:* across all nine populated bins in [0,1], `|mean q − empirical| ≤
0.009`. **Caveat in §8.**

**T5 (Proof/counterexample permanence, I3/I4).** Once `q_{t,j}∈{0,1}` by a
complete proof / counterexample, it persists until a commit enters `φⱼ`'s cone
of influence. *Proof:* by §6.3 + the COI dependency; no simulation evidence can
move a certified bit. ∎

**T6 (Strict improvement over random).** `EIG(a*) ≥ 𝔼_{a∼unif}[EIG(a)]`, equality
only if all actions equally informative. *Proof:* max ≥ mean. ∎ *Validated:*
greedy residual uncertainty **9.03** vs random **11.97** vs round-robin **13.03**.

**T7 (Complexity).** Exact joint belief `O(2^m)`; mean-field per-action update
`O(|COI(a)|)`; one planning step `O(|A|·c_EIG)` with `c_EIG` closed-form `O(1)`
per property. Chip-scale tractable.

### Validation results (`vws_validation.py`, seed fixed)

| Test | Claim | Result |
|---|---|---|
| 1 | reduce to rule of three | Bayes 95% UB {.057,.029,.0099,.00299} ≈ 3/n {.06,.03,.01,.003} ✓ |
| 2 | calibration (well-specified) | max \|mean q − empirical\| = **0.009** across [0,1] ✓ |
| 3 | EIG ≥ 0 | worst over 20k states = **0.0** ✓ |
| 4 | greedy beats random/round-robin | **9.03 < 11.97 < 13.03** residual U ✓ |
| 5 | convergence `U→0` | greedy → **0.018** ✓ |
| 6 | misspecification sensitivity | over-optimistic prior → **+0.03–0.09 over-confidence** (§8) |

---

## 8. The one caveat that matters: calibration needs the prior

T4 holds **only under a reasonable prior.** The misspecification sweep (Test 6):

| true π | assumed π | high-confidence bias (q ≥ 0.9) |
|---|---|---|
| 0.6 | 0.6 | −0.0002 (calibrated) |
| 0.6 | 0.8 | **+0.028 (over-confident)** |
| 0.6 | 0.4 | −0.023 (under-confident, safe) |
| 0.3 | 0.7 | **+0.091 (badly over-confident)** |

An **over-optimistic prior yields over-confidence in exactly the decision-relevant
region** — the framework would sign off correctness it has not earned. This is
the failure mode a verification method must never hide, so it is a first-class
result, not a footnote.

**Mitigation (and why it ties to the data question).** Do not *assume* `πⱼ`;
**learn it by empirical/hierarchical Bayes** from historical outcomes across
modules and commits (bug history, reopen data, per-module defect rates — the
same corpus git-history signal in `DATA_AND_HARDWARE_REQUIREMENTS.md`).
Under-confidence (pessimistic prior) is the safe failure direction and the
correct default before enough history accrues.

---

## 9. Falsifiable empirical criterion

On corpus cores (Ibex, cv32e40p) under a fixed simulation-second budget:

> **Claim to test.** The `EIG/cost` planner reaches a target *knowledge* level
> `K_target` in fewer simulation-seconds than (a) uniform-random stimulus and
> (b) the coverage-reward bandit; **and** its beliefs are calibrated (Test-2
> criterion) on held-out properties.

If it does not, the framework is falsified for that setting **and we report
that.** §7 demonstrates the *mathematical* claim (greedy > random) on a
correct-by-construction model; §9 is the *empirical* claim on real RTL, which is
what still requires data (§10).

---

## 10. What is buildable now vs. gated

**Buildable now (pure-Python, no data, testable on the corpus):** the VWS state
object, the closed-form spike-and-slab Bayesian update, the entropy/knowledge
metrics, the EIG planner with a *specified* likelihood, the optimal-stopping
rule, invariants as runtime assertions. A faithful executable of §4–§7.

**Gated on data (Verilator-generated `(W_t,a,o)` tuples; CPU-hours, open-source,
no hardware):** learning the per-action likelihood and `πⱼ` (empirical Bayes,
§8), and running the §9 study.

**Gated on nothing we can get here:** none. No proprietary EDA tool, no LLM, no
silicon is required for the foundation or its first implementation.

## 11. Mapping to AVA

`G_t`←`rtl_graph`; evidence←the 70 golden verifiers + `coverage_collector` +
`formal_engine`; belief `q`←`confidence_scorer` (upgrade heuristic→posterior);
proof permanence/COI←`formal_analysis.cone_of_influence`; planner←
`self_evolving_engine` (reward: coverage-Δ → `EIG/cost`); cost `c(a)`←
`economics_engine`/`regression_intelligence`; stopping/readiness←
`verification_twin`. The framework is a reformulation AVA's parts already
half-instantiate — which is why it is implementable, not merely elegant.

---

## Sources (prior-art basis for §1–§2)

- [Directed Test Generation for Hardware Validation: A Survey (ACM CSUR 2023)](https://dl.acm.org/doi/10.1145/3638046)
- [Coverage-directed test generation using Bayesian networks (Fine & Ziv)](https://www.researchgate.net/publication/4027133_Coverage_directed_test_generation_for_functional_verification_using_Bayesian_networks)
- [Estimating the Probability of Failure When Testing Reveals No Failures (Miller et al., IEEE TSE 1992)](https://experts.illinois.edu/en/publications/estimating-the-probability-of-failure-when-testing-reveals-no-fai)
- [The statistical rule of three](https://www.statology.org/a-concise-guide-to-the-statistical-rule-of-three/)
- [Value-of-information-based experimental design (ORNL)](https://impact.ornl.gov/en/publications/value-of-information-based-experimental-design-application-to-pro/)
- [Bayesian inference & information theory for experimental design (PMC7514425)](https://pmc.ncbi.nlm.nih.gov/articles/PMC7514425/)
- [Using verification coverage with formal analysis (EE Times)](https://www.eetimes.com/using-verification-coverage-with-formal-analysis/)
- [A Coverage-Driven Formal Methodology for Verification Sign-off (DVCon)](https://dvcon-proceedings.org/wp-content/uploads/a-coverage-driven-formal-methodology-for-verification-sign-off.pdf)
- [Learning Q-network for Active Information Acquisition (arXiv 1910.10754)](https://arxiv.org/pdf/1910.10754)

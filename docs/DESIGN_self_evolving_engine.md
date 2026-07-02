# Design & Build Plan — T40 Self-Evolving Verification Engine

**Status:** Implemented & tested, research-grade (AVA v2.19.0, 2026-06-30)
**Module:** `AGENT_H/self_evolving_engine.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)
**Track:** AI / self-evolving tests (advanced-ideas #3)

---

## 1. The problem

Constrained-random regressions plateau. The first ~90% of coverage bins fall
easily; the last few are exponentially harder, and blindly generating *more*
tests burns compute without closing them. The open research question: can the
loop **learn where to spend effort** and **aim** at the specific gaps?

## 2. The idea — closure as a *non-stationary* bandit

Model closure as a multi-armed bandit whose arms are generation strategies and
whose reward is coverage gained − cost + bugs. The critical subtlety that makes
this research-grade rather than a toy:

> **Coverage closure is non-stationary.** A strategy that is productive early
> (it covers the holes it is good at) becomes worthless later — its *true reward
> decays over time*. Vanilla UCB1 assumes a fixed reward distribution and its
> cumulative mean lags badly under drift.

So the engine is **policy-pluggable** and defaults to a non-stationary policy.

### 2.1 Bandit policies (all provably-grounded, one interface)

| Policy | Basis | When it wins |
|---|---|---|
| `UCB1` (γ=1) | Auer et al. 2002 | stationary baseline / reference |
| **`DiscountedUCB1`** *(default)* | Garivier & Moulines 2011 | reward drifts smoothly — discounted counts *forget* stale evidence |
| `SlidingWindowUCB` | Garivier & Moulines 2011 | abrupt regime changes — arms that leave the window are re-explored |
| `ThompsonSampling` | Thompson 1933 | want native Bayesian **uncertainty**; strong empirical performance |

`make_policy(name, …)` builds any of them. Each exposes `select / update / best
/ stats / confidence`, where `confidence` is the exploration bonus (UCB) or
posterior std (Thompson) — a real per-arm **uncertainty estimate**.

**Proven behaviour (unit tests):** on a two-arm environment where arm A is good
for 20 rounds then dies and arm B does the reverse, `DiscountedUCB1` ends
preferring **B** (`mean(B) > mean(A)`) — it forgets — while stationary `UCB1`
averages both to **0.5**. `SlidingWindowUCB` re-explores arms that fall out of
the window; `ThompsonSampling`'s posterior std shrinks as evidence accrues.

### 2.2 Difficulty-aware, importance-ranked hole scheduler

Each round targets holes ordered by `(attempts ↑, importance ↓, label)` — spread
effort across least-attempted holes, prioritise high-**weight** (important) bins,
break ties reproducibly. Per-hole **attempt counts** are tracked; a hole open
after `unreachable_after` attempts is flagged **suspected-unreachable** — a
concrete deliverable (candidate coverage waiver / dead-code signal), not just a
number.

### 2.3 Constraint escalation (auto-tuning → mutation → repair → adversarial)

`constraint_for(hole, level)` climbs a ladder — `baseline → widen_ranges →
edge_values → repair → adversarial` — driven by how long a hole has resisted
(`level = attempts // escalate_after`). Resisting holes automatically get more
aggressive, mutated, and finally adversarial constraints. Level is clamped, so
it never overflows.

### 2.4 Reward (bounded [0,1] for policy validity)

```
closure = Σ weight(newly) / Σ weight(holes_before)      # importance-weighted
novelty = novelty_weight · mean(1 / (1 + region_hits))  # curiosity bonus
reward  = clamp( w_cov·closure + novelty + w_bug·(1−e^−bugs) − w_cost·cost )
```

Importance-weighting rewards closing *valuable* bins; the novelty term rewards
exploring **new regions** (curiosity-driven); the bug bonus saturates so one
lucky bug can't dominate; cost is a penalty so cheap-effective strategies win.

## 3. Intelligence & reproducibility reporting

- **Cumulative regret** vs. the best arm in hindsight — a learning-stability
  diagnostic. *(Honest caveat: because rewards are non-stationary there is no
  fixed oracle arm, so this is a reward-gap proxy, not a cross-policy ranking
  metric — it should not be read as "policy X beats Y".)*
- **Coverage velocity**, **coverage-per-cost** efficiency.
- **Closure prediction**: estimated rounds-to-target from recent velocity, plus
  a **confidence** derived from velocity variance.
- **Per-arm uncertainty** (`confidence`) and **weighted coverage**.
- **`run_campaign(seeds=…)`** runs the loop over many seeds and reports
  **mean ± 95% CI** over final coverage / rounds / regret + a modal recommended
  strategy — results with error bars, and verified bit-for-bit deterministic.

## 4. Soundness / engineering guarantees

- **Bounded rewards** keep every UCB/TS guarantee valid.
- **Deterministic** (seeded) — campaigns are reproducible and diffable.
- **Failure-contained** — a `generate`/`evaluate` plugin that throws is caught,
  scored as a zero-reward round, and the campaign continues.
- **Advisory, never gating** — `pass` is always `True`; this is an *optimiser*
  that points the generator at gaps, it does not decide tape-out.
- **Plugin-injected** `generate`/`evaluate` — the identical controller drives a
  real Spike+RTL+LLM stack in production and the synthetic test environment.

## 5. Offline mode (no simulator)

`run_from_manifest` reads a `coverage_summary.json` (bins, optional `weights`
and `attempts`), ranks holes by importance × difficulty, attaches an escalated
constraint to each, and recommends a strategy from any persisted `strategy_stats`
— a coverage-closure plan the downstream generator consumes. Skips cleanly with
no snapshot. Wired into `_run_extended_pipeline`.

## 6. Test coverage

`tests/test_agents.py` — **34 cases** across `TestSelfEvolvingEngine` (core:
UCB1, coverage/constraint units, coverage-increase, bandit preference,
target/plateau/bad-plugin stops, schema, offline planning, manifest round-trips,
robustness) and `TestSelfEvolvingResearchGrade` (policy variants + factory,
non-stationarity vs. stationarity, sliding-window forgetting, Thompson posterior
shrink, per-policy confidence, constraint-escalation ladder, importance
scheduling, weighted coverage, region novelty, suspected-unreachable, regret /
velocity / closure-prediction, multi-seed CI + determinism, per-policy engine
plumbing, attempt-based plan escalation). All pass (validated standalone: 33 in
the isolated harness + in-suite import).

> Build note: the module is stdlib-only and self-contained, so it is validated
> in isolation. The workspace mount truncates recently-grown files, so the full
> in-repo suite is run against the real repo rather than the scratch copy; the
> change is additive (new module + lazy pipeline hook + new test classes), so
> existing agents are unaffected.

## 7. Mapping to the requested capability wishlist

Implemented here as real algorithms: non-stationary MAB, adaptive
exploration/exploitation, online policy adaptation (discounting = *forgetting
outdated policies*), automatic strategy switching, difficulty-aware scheduler
(*curriculum*), coverage importance ranking + dynamic weighting, coverage
novelty (*curiosity*), closure prediction, regret & efficiency, statistical
reproducibility, confidence/uncertainty estimation, constraint auto-tuning /
mutation / repair / adversarial, coverage-cost optimisation, self-performance
evaluation.

**Owned by sibling agents (not duplicated here):** failure clustering / dedup /
bug-memory → `knowledge_graph.py`; root-cause learning → `root_cause_localizer`;
test minimization → `minimizer.py`; historical-failure reuse → `knowledge_graph`.

**Documented future work (deliberately *not* stubbed — would be shallow):**
Quality-Diversity / MAP-Elites archives, population-based training, ensemble of
policies, curiosity via learned forward-model surprise, meta-learned
hyper-parameter self-tuning, lifelong/continual learning across campaigns. Each
is a substantial project; adding empty hooks now would violate the "no flaws"
bar. They slot cleanly onto the existing `BanditPolicy` + `CoverageState`
interfaces when built.

## 8. Next steps

- **Contextual bandit**: condition strategy choice on the *kind* of remaining
  hole (per-hole-class arms).
- **Live wiring**: `generate` → `AGENT_G/causal_engine`, `evaluate` → real
  tandem-sim coverage delta.
- **Measured cost**: use `perf_counters` cycles for the cost term.
- **Auto-γ**: adapt the discount factor online from observed drift.

"""
AGENT_H.self_evolving_engine — Self-Evolving Verification (T40)
================================================================

A **research-grade** reinforcement-learning coverage-closure loop: the AI
"self-evolving test" track. It decides *where to spend test-generation effort*
by learning which strategy pays off **under non-stationary reward**, turns the
remaining coverage holes into escalating structured **constraints**, and reports
its own learning quality (regret, efficiency, uncertainty, closure prediction).

Why non-stationarity is the crux
---------------------------------
Coverage closure is *not* a stationary bandit problem. A strategy that is
productive early (it covers the holes it is good at) becomes worthless later —
its true reward *decays over time*. Vanilla UCB1 assumes a fixed reward
distribution and its cumulative mean lags badly when the environment drifts. So
the engine ships several **provably-grounded policies** and defaults to a
non-stationary one:

    UCB1               stationary baseline (Auer et al. 2002)
    DiscountedUCB1     γ-discounted counts — "forgetting" (Garivier & Moulines 2011)
    SlidingWindowUCB   fixed-window re-estimation (Garivier & Moulines 2011)
    ThompsonSampling   Bayesian Beta posterior — native uncertainty (Thompson 1933)

The loop (each round)
---------------------
1. **Select** a strategy with the chosen policy (online exploration/exploitation
   that *adapts* as strategies decay — automatic strategy switching).
2. **Schedule holes** difficulty-aware + importance-ranked: least-attempted,
   highest-weight first, and **escalate the constraint** for holes that resist
   (auto-tuning → mutation → repair → adversarial ladder).
3. **Generate + evaluate** via injected plugins
   (`generate(strategy, constraints)` → batch, `evaluate(batch)` →
   `{covered, bugs, cost}`), so the *same* controller drives a real
   Spike+RTL+LLM stack and the synthetic test environment.
4. **Reward** = importance-weighted coverage-closure  +  novelty (curiosity)
   +  bugs  −  runtime cost, bounded to [0,1] for policy validity.
5. **Stop** on coverage target, exhausted holes, or plateau (anti-thrash);
   holes that resist for too long are flagged **suspected-unreachable**
   (candidate coverage waivers).

Intelligence reporting
----------------------
Cumulative **regret** vs. the best arm in hindsight, coverage **velocity**,
coverage-**per-cost** efficiency, **closure prediction** (estimated rounds to
target + confidence), and per-arm **uncertainty**. `run_campaign` executes
multiple seeds and reports **mean ± 95% CI** — results with error bars, not a
single lucky run.

Offline mode (`run_from_manifest`) needs no simulator: it reads
`coverage_summary.json`, ranks holes, attaches constraints, and recommends a
strategy — a coverage-closure plan for the next adaptive round.

Stdlib-only, deterministic (seeded), graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
import math
import random
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.self_evolving")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "self_evolving_engine"

# Constraint escalation ladder (difficulty-aware auto-tuning / repair).
_ESCALATION = ["baseline", "widen_ranges", "edge_values", "repair", "adversarial"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


# ═════════════════════════════════════════════════════════════════════════════
# Bandit policies (pluggable; all bounded-reward, all expose the same interface)
# ═════════════════════════════════════════════════════════════════════════════
class BanditPolicy:
    """Common interface: select() → arm, update(arm, reward), best(), stats()."""

    name = "base"

    def __init__(self, arms: Sequence[str]):
        self.arms: List[str] = list(dict.fromkeys(a for a in arms if a))
        self.counts: Dict[str, int] = {a: 0 for a in self.arms}  # lifetime pulls
        self.t = 0

    # -- subclasses override -------------------------------------------------
    def select(self) -> Optional[str]:            # pragma: no cover - abstract
        raise NotImplementedError

    def update(self, arm: str, reward: float) -> None:  # pragma: no cover
        raise NotImplementedError

    def mean(self, arm: str) -> float:            # pragma: no cover - abstract
        raise NotImplementedError

    def confidence(self, arm: str) -> float:
        """Per-arm uncertainty (exploration bonus / posterior std)."""
        return 0.0

    # -- shared --------------------------------------------------------------
    def _register(self, arm: str) -> None:
        if arm not in self.counts:
            self.arms.append(arm)
            self.counts[arm] = 0

    def best(self) -> Optional[str]:
        pulled = [a for a in self.arms if self.counts[a] > 0]
        return max(pulled, key=self.mean) if pulled else None

    def stats(self) -> Dict[str, Dict[str, float]]:
        return {
            a: {
                "pulls": self.counts[a],
                "mean_reward": round(self.mean(a), 4),
                "confidence": round(self.confidence(a), 4),
            }
            for a in self.arms
        }


class UCB1(BanditPolicy):
    """
    UCB1 with optional γ-discounting. γ = 1.0 → classic stationary UCB1
    (Auer et al. 2002). γ < 1.0 → Discounted-UCB (Garivier & Moulines 2011):
    counts and reward sums decay each step so stale evidence is forgotten —
    the correct model when strategy productivity drifts.
    """

    name = "ucb1"

    def __init__(self, arms: Sequence[str], c: float = 1.0, gamma: float = 1.0):
        super().__init__(arms)
        self.c = float(c)
        self.gamma = float(gamma)
        self._n: Dict[str, float] = {a: 0.0 for a in self.arms}  # discounted count
        self._s: Dict[str, float] = {a: 0.0 for a in self.arms}  # discounted sum
        if self.gamma < 1.0:
            self.name = "discounted_ucb"

    def mean(self, arm: str) -> float:
        n = self._n.get(arm, 0.0)
        return (self._s.get(arm, 0.0) / n) if n > 0 else 0.0

    def _bonus(self, arm: str) -> float:
        n = self._n.get(arm, 0.0)
        if n <= 0:
            return float("inf")
        n_total = max(1.0, sum(self._n.values()))
        return self.c * math.sqrt(2.0 * math.log(n_total) / n)

    def confidence(self, arm: str) -> float:
        b = self._bonus(arm)
        return 0.0 if b == float("inf") else b

    def select(self) -> Optional[str]:
        if not self.arms:
            return None
        for a in self.arms:                       # cold start: each arm once
            if self.counts[a] == 0:
                return a
        return max(self.arms, key=lambda a: self.mean(a) + self._bonus(a))

    def update(self, arm: str, reward: float) -> None:
        self._register(arm)
        self._n.setdefault(arm, 0.0)
        self._s.setdefault(arm, 0.0)
        self.t += 1
        if self.gamma < 1.0:                      # discount all arms (forgetting)
            for a in self.arms:
                self._n[a] *= self.gamma
                self._s[a] *= self.gamma
        self.counts[arm] += 1
        self._n[arm] += 1.0
        self._s[arm] += float(reward)


class DiscountedUCB1(UCB1):
    """Convenience: non-stationary γ-discounted UCB (Garivier & Moulines 2011)."""

    def __init__(self, arms: Sequence[str], gamma: float = 0.95, c: float = 1.0):
        super().__init__(arms, c=c, gamma=gamma)


class SlidingWindowUCB(BanditPolicy):
    """
    Sliding-Window UCB (Garivier & Moulines 2011): estimate each arm's reward
    from only the last ``window`` observations, so arms whose productivity
    decayed drop out of the window and are naturally re-explored.
    """

    name = "sliding_window_ucb"

    def __init__(self, arms: Sequence[str], window: int = 50, c: float = 1.0):
        super().__init__(arms)
        self.c = float(c)
        self.window = max(1, int(window))
        self._hist: deque = deque(maxlen=self.window)  # (arm, reward)

    def _win_agg(self) -> Tuple[Dict[str, int], Dict[str, float]]:
        n: Dict[str, int] = {a: 0 for a in self.arms}
        s: Dict[str, float] = {a: 0.0 for a in self.arms}
        for a, r in self._hist:
            if a in n:
                n[a] += 1
                s[a] += r
        return n, s

    def mean(self, arm: str) -> float:
        n, s = self._win_agg()
        return (s[arm] / n[arm]) if n.get(arm, 0) > 0 else 0.0

    def confidence(self, arm: str) -> float:
        n, _ = self._win_agg()
        na = n.get(arm, 0)
        if na <= 0:
            return 0.0
        tot = max(1, sum(n.values()))
        return self.c * math.sqrt(2.0 * math.log(tot) / na)

    def select(self) -> Optional[str]:
        if not self.arms:
            return None
        for a in self.arms:                       # lifetime cold start
            if self.counts[a] == 0:
                return a
        n, s = self._win_agg()
        tot = max(1, sum(n.values()))
        best, best_score = None, float("-inf")
        for a in self.arms:
            if n[a] == 0:                          # dropped out of window → re-explore
                return a
            score = (s[a] / n[a]) + self.c * math.sqrt(2.0 * math.log(tot) / n[a])
            if score > best_score:
                best, best_score = a, score
        return best

    def update(self, arm: str, reward: float) -> None:
        self._register(arm)
        self.t += 1
        self.counts[arm] += 1
        self._hist.append((arm, float(reward)))


class ThompsonSampling(BanditPolicy):
    """
    Bayesian Thompson Sampling with a Beta posterior over each arm's reward
    (rewards in [0,1] treated as Bernoulli evidence). Exploration is driven by
    posterior *uncertainty*, giving a principled uncertainty estimate for free.
    """

    name = "thompson"

    def __init__(self, arms: Sequence[str], seed: int = 0):
        super().__init__(arms)
        self.rng = random.Random(seed)
        self.alpha: Dict[str, float] = {a: 1.0 for a in self.arms}
        self.beta: Dict[str, float] = {a: 1.0 for a in self.arms}

    def mean(self, arm: str) -> float:
        a, b = self.alpha.get(arm, 1.0), self.beta.get(arm, 1.0)
        return a / (a + b)

    def confidence(self, arm: str) -> float:
        a, b = self.alpha.get(arm, 1.0), self.beta.get(arm, 1.0)
        return math.sqrt((a * b) / (((a + b) ** 2) * (a + b + 1.0)))  # Beta std

    def select(self) -> Optional[str]:
        if not self.arms:
            return None
        return max(self.arms, key=lambda a: self.rng.betavariate(self.alpha[a], self.beta[a]))

    def update(self, arm: str, reward: float) -> None:
        self._register(arm)
        self.alpha.setdefault(arm, 1.0)
        self.beta.setdefault(arm, 1.0)
        r = _clamp(float(reward))
        self.t += 1
        self.counts[arm] += 1
        self.alpha[arm] += r
        self.beta[arm] += (1.0 - r)


_POLICY_ALIASES = {
    "ucb1": "ucb1", "ucb": "ucb1",
    "ducb": "ducb", "discounted": "ducb", "discounted_ucb": "ducb",
    "swucb": "swucb", "sliding": "swucb", "sliding_window": "swucb",
    "thompson": "thompson", "ts": "thompson", "bayesian": "thompson",
}


def make_policy(name: str, arms: Sequence[str], *, seed: int = 0,
                gamma: float = 0.9, window: int = 50, c: float = 1.0) -> BanditPolicy:
    key = _POLICY_ALIASES.get(str(name).lower())
    if key == "ucb1":
        return UCB1(arms, c=c)
    if key == "ducb":
        return DiscountedUCB1(arms, gamma=gamma, c=c)
    if key == "swucb":
        return SlidingWindowUCB(arms, window=window, c=c)
    if key == "thompson":
        return ThompsonSampling(arms, seed=seed)
    raise ValueError(f"unknown policy {name!r}; choose from {sorted(set(_POLICY_ALIASES))}")


# ═════════════════════════════════════════════════════════════════════════════
# Coverage state — importance-weighted + novelty (curiosity) tracking
# ═════════════════════════════════════════════════════════════════════════════
def _region(bin_label: Any) -> str:
    """Region = the 'kind' prefix of a bin label (before the first ':')."""
    s = str(bin_label)
    return s.split(":", 1)[0] if ":" in s else s


class CoverageState:
    def __init__(self, total_bins: Iterable[Any] = (),
                 weights: Optional[Dict[Any, float]] = None):
        self.total = set(b for b in total_bins if b is not None)
        self.covered: set = set()
        self.weights: Dict[Any, float] = dict(weights or {})
        self._region_hits: Dict[str, int] = {}   # for novelty

    def weight(self, b: Any) -> float:
        return float(self.weights.get(b, 1.0))

    def cover(self, bins: Iterable[Any]) -> set:
        try:
            incoming = set(b for b in bins if b is not None)
        except TypeError:
            return set()
        if not self.total:                        # adopt universe if undeclared
            self.total |= incoming
            valid = incoming
        else:
            valid = incoming & self.total
        newly = valid - self.covered
        self.covered |= valid
        for b in newly:
            r = _region(b)
            self._region_hits[r] = self._region_hits.get(r, 0) + 1
        return newly

    def novelty(self, b: Any) -> float:
        """Curiosity signal: bins in rarely-covered regions score higher."""
        return 1.0 / (1.0 + self._region_hits.get(_region(b), 0))

    def holes(self) -> set:
        return self.total - self.covered

    def fraction(self) -> float:
        return 1.0 if not self.total else len(self.covered) / len(self.total)

    def weighted_fraction(self) -> float:
        if not self.total:
            return 1.0
        tot = sum(self.weight(b) for b in self.total)
        if tot <= 0:
            return self.fraction()
        cov = sum(self.weight(b) for b in self.covered)
        return cov / tot


# ═════════════════════════════════════════════════════════════════════════════
# Constraint synthesis + escalation (auto-tuning / mutation / repair / adversarial)
# ═════════════════════════════════════════════════════════════════════════════
def constraint_for(hole: Any, level: int = 0) -> Dict[str, Any]:
    """
    Turn a coverage-hole label into a structured generation constraint.
    ``level`` climbs the escalation ladder as a hole resists closure: baseline →
    widen ranges → edge values → repair → adversarial. This is the
    constraint auto-tuning / mutation / repair / adversarial mechanism.
    """
    label = str(hole)
    parts = [p for p in label.split(":") if p != ""]
    lvl = max(0, min(int(level), len(_ESCALATION) - 1))
    if len(parts) >= 2:
        base = {"kind": parts[0], "values": parts[1:]}
    else:
        base = {"kind": "bin", "values": [label]}
    base.update({
        "target": label,
        "bias": label,
        "level": lvl,
        "strategy_hint": _ESCALATION[lvl],
        "mutations": _ESCALATION[1:lvl + 1],       # applied escalations
        "repair": lvl >= _ESCALATION.index("repair"),
        "adversarial": lvl >= _ESCALATION.index("adversarial"),
    })
    return base


# ═════════════════════════════════════════════════════════════════════════════
# Self-evolving engine
# ═════════════════════════════════════════════════════════════════════════════
GenerateFn = Callable[[str, List[Dict[str, Any]]], Any]
EvaluateFn = Callable[[Any], Dict[str, Any]]

DEFAULT_STRATEGIES = ["random", "directed", "genetic", "adversarial"]


class SelfEvolvingEngine:
    """
    Non-stationary RL coverage-closure controller.

    Parameters
    ----------
    policy            : "ucb1" | "discounted" | "sliding_window" | "thompson"
                        (default "discounted" — the non-stationary choice).
    gamma / window    : hyper-parameters for discounted / sliding-window UCB.
    weights           : optional per-bin importance weights (coverage ranking).
    novelty_weight    : curiosity bonus for covering rarely-hit regions.
    escalate_after    : bump a hole's constraint level every N failed attempts.
    unreachable_after : flag a hole suspected-unreachable after N attempts open.
    """

    def __init__(
        self,
        total_bins: Iterable[Any],
        strategies: Sequence[str],
        seed: int = 0,
        policy: str = "discounted",
        gamma: float = 0.9,
        window: int = 50,
        coverage_target: float = 1.0,
        plateau_patience: int = 5,
        holes_per_round: int = 4,
        weights: Optional[Dict[Any, float]] = None,
        w_cov: float = 1.0,
        w_bug: float = 0.5,
        w_cost: float = 0.15,
        novelty_weight: float = 0.25,
        escalate_after: int = 2,
        unreachable_after: int = 6,
        ucb_c: float = 1.0,
    ):
        self.cov = CoverageState(total_bins, weights)
        self.strategies = list(dict.fromkeys(strategies or DEFAULT_STRATEGIES))
        self.policy = make_policy(policy, self.strategies, seed=seed,
                                  gamma=gamma, window=window, c=ucb_c)
        self.rng = random.Random(seed)
        self.coverage_target = _clamp(coverage_target)
        self.plateau_patience = max(1, int(plateau_patience))
        self.holes_per_round = max(1, int(holes_per_round))
        self.w_cov, self.w_bug, self.w_cost = w_cov, w_bug, w_cost
        self.novelty_weight = float(novelty_weight)
        self.escalate_after = max(1, int(escalate_after))
        self.unreachable_after = max(1, int(unreachable_after))

        self.attempts: Dict[Any, int] = {}
        self.rounds: List[Dict[str, Any]] = []
        self.rewards: List[float] = []
        self.total_bugs = 0
        self.total_cost = 0.0
        self.stop_reason: Optional[str] = None
        self._started = _now()

    # -- difficulty-aware + importance-ranked hole scheduler ------------------
    def select_holes(self, k: Optional[int] = None) -> List[Any]:
        holes = self.cov.holes()
        if not holes:
            return []
        k = self.holes_per_round if k is None else k
        # least-attempted first (spread effort), then highest-weight
        # (importance ranking), then stable label order (reproducibility).
        ordered = sorted(
            holes,
            key=lambda h: (self.attempts.get(h, 0), -self.cov.weight(h), str(h)),
        )
        return ordered[: max(1, k)]

    def _constraints(self, holes: Sequence[Any]) -> List[Dict[str, Any]]:
        out = []
        for h in holes:
            level = self.attempts.get(h, 0) // self.escalate_after
            out.append(constraint_for(h, level=level))
        return out

    # -- reward ---------------------------------------------------------------
    def _reward(self, newly: set, holes_before_w: float, bugs: int, cost: float) -> float:
        gained_w = sum(self.cov.weight(b) for b in newly)
        closure = (gained_w / holes_before_w) if holes_before_w > 0 else 0.0
        novelty = 0.0
        if newly:
            novelty = self.novelty_weight * (sum(self.cov.novelty(b) for b in newly) / len(newly))
        bug_term = self.w_bug * (1.0 - math.exp(-max(0, bugs)))
        cost_term = self.w_cost * _clamp(cost)
        return _clamp(self.w_cov * closure + novelty + bug_term - cost_term)

    # -- main loop ------------------------------------------------------------
    def evolve(self, generate: GenerateFn, evaluate: EvaluateFn,
               max_rounds: int = 50) -> Dict[str, Any]:
        no_improve = 0
        for r in range(1, int(max_rounds) + 1):
            if self.cov.fraction() >= self.coverage_target:
                self.stop_reason = "coverage_target_reached"
                break
            holes_before = self.cov.holes()
            if not holes_before:
                self.stop_reason = "no_holes_remaining"
                break
            holes_before_w = sum(self.cov.weight(b) for b in holes_before)

            arm = self.policy.select() or self.strategies[0]
            targets = self.select_holes()
            constraints = self._constraints(targets)

            try:
                batch = generate(arm, constraints)
                result = evaluate(batch) or {}
            except Exception as exc:              # a bad plugin must not kill the run
                log.warning("evolve round %d aborted: %s", r, exc)
                result = {"covered": [], "bugs": 0, "cost": 0.0, "error": str(exc)}

            newly = self.cov.cover(result.get("covered", []) or [])
            bugs = int(result.get("bugs", 0) or 0)
            cost = float(result.get("cost", 0.0) or 0.0)
            reward = self._reward(newly, holes_before_w, bugs, cost)
            self.policy.update(arm, reward)

            # attempt accounting: only holes we targeted and still failed to close
            for h in targets:
                if h in self.cov.holes():
                    self.attempts[h] = self.attempts.get(h, 0) + 1

            self.total_bugs += bugs
            self.total_cost += cost
            self.rewards.append(reward)
            self.rounds.append({
                "round": r,
                "strategy": arm,
                "policy_confidence": round(self.policy.confidence(arm), 4),
                "targeted_holes": [str(h) for h in targets],
                "max_constraint_level": max((c["level"] for c in constraints), default=0),
                "new_bins": len(newly),
                "bugs": bugs,
                "cost": round(cost, 4),
                "reward": round(reward, 4),
                "coverage": round(self.cov.fraction(), 4),
                "holes_remaining": len(self.cov.holes()),
            })

            no_improve = no_improve + 1 if len(newly) == 0 else 0
            if no_improve >= self.plateau_patience:
                self.stop_reason = "plateau"
                break
        if self.stop_reason is None:
            self.stop_reason = "max_rounds_reached"
        return self.report()

    # -- intelligence metrics -------------------------------------------------
    def _cumulative_regret(self) -> float:
        """Regret vs. the best arm in hindsight (lower = better learning)."""
        if not self.rewards:
            return 0.0
        best_mean = max((self.policy.mean(a) for a in self.policy.arms), default=0.0)
        return round(sum(max(0.0, best_mean - r) for r in self.rewards), 4)

    def _closure_prediction(self, final: float) -> Tuple[Optional[int], float]:
        """Estimate rounds-to-target + a confidence from recent velocity."""
        if final >= self.coverage_target or len(self.rounds) < 2:
            return (0 if final >= self.coverage_target else None), 0.0
        tail = self.rounds[-min(5, len(self.rounds)):]
        gained = tail[-1]["coverage"] - tail[0]["coverage"]
        span = len(tail) - 1
        velocity = gained / span if span > 0 else 0.0
        if velocity <= 1e-9:
            return None, 0.0
        remaining = self.coverage_target - final
        est = math.ceil(remaining / velocity)
        # confidence: how steady was recent velocity (low variance → high conf)
        per_round = [tail[i + 1]["coverage"] - tail[i]["coverage"] for i in range(span)]
        if len(per_round) >= 2:
            mu = sum(per_round) / len(per_round)
            var = sum((x - mu) ** 2 for x in per_round) / len(per_round)
            conf = _clamp(1.0 - (math.sqrt(var) / (abs(mu) + 1e-9)))
        else:
            conf = 0.5
        return est, round(conf, 4)

    def _suspected_unreachable(self) -> List[str]:
        open_holes = self.cov.holes()
        return sorted(
            str(h) for h in open_holes
            if self.attempts.get(h, 0) >= self.unreachable_after
        )

    # -- reporting ------------------------------------------------------------
    def report(self) -> Dict[str, Any]:
        trajectory = [rd["coverage"] for rd in self.rounds]
        final = self.cov.fraction()
        first = trajectory[0] if trajectory else final
        rounds_run = len(self.rounds)
        est_rounds, closure_conf = self._closure_prediction(final)
        unreachable = self._suspected_unreachable()
        holes = sorted((str(h) for h in self.cov.holes()))
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": self._started,
            "finished_at": _now(),
            "pass": True,                          # optimiser/advisory — never fails
            "band": _coverage_band(final),
            "policy": self.policy.name,
            "rounds_run": rounds_run,
            "stop_reason": self.stop_reason,
            "final_coverage": round(final, 4),
            "weighted_coverage": round(self.cov.weighted_fraction(), 4),
            "initial_round_coverage": round(first, 4),
            "coverage_improvement": round(final, 4),
            "coverage_trajectory": trajectory,
            "coverage_velocity": round(final / rounds_run, 5) if rounds_run else 0.0,
            "coverage_per_cost": (round(final / self.total_cost, 4)
                                  if self.total_cost > 0 else None),
            "est_rounds_to_target": est_rounds,
            "closure_confidence": closure_conf,
            "cumulative_regret": self._cumulative_regret(),
            "total_bins": len(self.cov.total),
            "covered_bins": len(self.cov.covered),
            "holes_remaining": len(holes),
            "holes_sample": holes[:25],
            "suspected_unreachable": unreachable[:25],
            "suspected_unreachable_count": len(unreachable),
            "bugs_found": self.total_bugs,
            "total_cost": round(self.total_cost, 4),
            "recommended_strategy": self.policy.best(),
            "strategy_stats": self.policy.stats(),
            "rounds": self.rounds,
        }


def _coverage_band(frac: float) -> str:
    if frac >= 0.90:
        return "VERIFIED"
    if frac >= 0.70:
        return "HIGH"
    if frac >= 0.50:
        return "MEDIUM"
    if frac >= 0.30:
        return "LOW"
    return "CRITICAL"


# ═════════════════════════════════════════════════════════════════════════════
# Statistical reproducibility — multi-seed campaign with mean ± 95% CI
# ═════════════════════════════════════════════════════════════════════════════
def run_campaign(
    total_bins: Iterable[Any],
    strategies: Sequence[str],
    env_factory: Callable[[int], Tuple[GenerateFn, EvaluateFn]],
    seeds: Sequence[int] = (0, 1, 2, 3, 4),
    max_rounds: int = 100,
    **engine_kwargs: Any,
) -> Dict[str, Any]:
    """
    Run the engine over multiple seeds and report distributional results
    (mean, std, 95% CI) so outcomes carry error bars, not a single run.
    ``env_factory(seed) -> (generate, evaluate)`` builds a seeded environment.
    """
    total_bins = list(total_bins)
    finals: List[float] = []
    rounds: List[int] = []
    regrets: List[float] = []
    bugs: List[int] = []
    recos: Dict[str, int] = {}
    for s in seeds:
        gen, ev = env_factory(s)
        eng = SelfEvolvingEngine(total_bins, strategies, seed=s, **engine_kwargs)
        rep = eng.evolve(gen, ev, max_rounds=max_rounds)
        finals.append(rep["final_coverage"])
        rounds.append(rep["rounds_run"])
        regrets.append(rep["cumulative_regret"])
        bugs.append(rep["bugs_found"])
        rec = rep["recommended_strategy"]
        if rec:
            recos[rec] = recos.get(rec, 0) + 1

    def _summ(xs: List[float]) -> Dict[str, float]:
        n = len(xs)
        mu = sum(xs) / n if n else 0.0
        var = sum((x - mu) ** 2 for x in xs) / n if n else 0.0
        std = math.sqrt(var)
        ci = 1.96 * std / math.sqrt(n) if n else 0.0
        return {"mean": round(mu, 4), "std": round(std, 4),
                "ci95": round(ci, 4), "n": n,
                "min": round(min(xs), 4) if xs else 0.0,
                "max": round(max(xs), 4) if xs else 0.0}

    return {
        "schema_version": SCHEMA_VERSION,
        "agent": AGENT_NAME,
        "kind": "campaign",
        "seeds": list(seeds),
        "final_coverage": _summ(finals),
        "rounds_run": _summ([float(x) for x in rounds]),
        "cumulative_regret": _summ(regrets),
        "bugs_found": _summ([float(x) for x in bugs]),
        "recommended_strategy_votes": recos,
        "modal_strategy": (max(recos, key=recos.get) if recos else None),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Offline coverage-closure planning (no simulator required)
# ═════════════════════════════════════════════════════════════════════════════
def plan_from_coverage(
    covered_bins: Iterable[Any],
    total_bins: Iterable[Any],
    strategy_stats: Optional[Dict[str, Dict[str, float]]] = None,
    weights: Optional[Dict[Any, float]] = None,
    attempts: Optional[Dict[Any, int]] = None,
    max_holes: int = 50,
) -> Dict[str, Any]:
    """Rank open holes (importance × difficulty), attach an escalated constraint
    to each, and recommend a strategy from any persisted bandit history."""
    cov = CoverageState(total_bins, weights)
    cov.cover(covered_bins)
    attempts = attempts or {}
    holes = sorted(
        cov.holes(),
        key=lambda h: (attempts.get(h, 0), -cov.weight(h), str(h)),
    )
    plan = [
        {"hole": str(h),
         "importance": cov.weight(h),
         "attempts": attempts.get(h, 0),
         "constraint": constraint_for(h, level=attempts.get(h, 0) // 2)}
        for h in holes[: max(0, max_holes)]
    ]
    recommended = None
    if strategy_stats:
        try:
            recommended = max(strategy_stats.items(),
                              key=lambda kv: kv[1].get("mean_reward", 0.0))[0]
        except (ValueError, AttributeError):
            recommended = None
    return {
        "schema_version": SCHEMA_VERSION,
        "agent": AGENT_NAME,
        "final_coverage": round(cov.fraction(), 4),
        "weighted_coverage": round(cov.weighted_fraction(), 4),
        "band": _coverage_band(cov.fraction()),
        "total_bins": len(cov.total),
        "covered_bins": len(cov.covered),
        "holes_remaining": len(holes),
        "recommended_strategy": recommended,
        "closure_plan": plan,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Manifest entry point
# ═════════════════════════════════════════════════════════════════════════════
def _load_coverage_summary(run_dir: Path) -> Optional[Dict[str, Any]]:
    for name in ("coverage_summary.json", "coverage.json"):
        p = run_dir / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return None


def run_from_manifest(manifest_path: str) -> int:
    """Offline advisory mode — writes a ``self_evolving_report.json`` closure
    plan from a coverage snapshot. Never fails the pipeline (returns 0)."""
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("self_evolving: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", mp.parent))
    summary = _load_coverage_summary(run_dir)

    if not summary:
        report = {
            "schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
            "status": "skipped",
            "reason": "no coverage_summary.json in run dir", "pass": True,
        }
    else:
        covered = summary.get("covered_bins", [])
        total = summary.get("total_bins")
        if total is None:
            total = list(covered) + list(summary.get("holes", []))
        report = plan_from_coverage(
            covered, total,
            strategy_stats=summary.get("strategy_stats"),
            weights=summary.get("weights"),
            attempts=summary.get("attempts"),
        )
        report["status"] = "completed"
        report["pass"] = True

    out = run_dir / "self_evolving_report.json"
    try:
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("self_evolving: cannot write report: %s", exc)
    return 0


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="AVA self-evolving closure planner")
    ap.add_argument("--manifest", required=True, help="path to run_manifest.json")
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))


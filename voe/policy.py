"""Verification policies as DATA, not as hard-coded roles.

The earlier workers hard-coded human cognitive styles — `explorer`, `skeptic` —
with constants chosen by a person (`EXPLORE_BUDGET = 4`). That makes human
engineering behaviour a *design constraint*, which is exactly backwards: the
kernel should constrain **correctness**, not **style**. Human practice belongs in
the evaluation baseline, not in the architecture.

So a policy here is a parameter set that can be enumerated, mutated, compared and
selected on measured evidence. The familiar archetypes survive only as two points
in that space — the incumbent to beat, not the shape of the answer.

A policy decides two things and nothing else:

    ORDER   which open obligation to attack next
    METHOD  which evidence channel to spend on it

It never decides what is true. Evidence does that, and the kernel adjudicates it.
A policy that games the metric by claiming more than it earned cannot exist:
`Judgment` construction requires a witness, and the vacuity gate refuses
uncertifiable proofs regardless of who proposed the action.
"""
from __future__ import annotations
import random
from dataclasses import dataclass, field

from board import ACTION_COST
from workers import Worker, Proposal


@dataclass(frozen=True)
class Policy:
    """A point in verification-strategy space."""
    name: str
    order: str = "utility"        # utility | random | cheapest | weight
    method_bias: str = "balanced" # balanced | sim | formal | random
    explore_budget: int = 4       # sim runs before escalating to formal
    formal_first: bool = False
    adaptive: bool = False        # re-weight method choice from realised payoff
    obligation_conditioned: bool = False   # condition on Omega_i, not just channel
    diagnostic: bool = False               # may spend a cheap probe to decide
    uncertainty_aware: bool = False        # decide by P(a is best) and VoD

    def describe(self) -> str:
        bits = [f"order={self.order}", f"method={self.method_bias}"]
        if self.formal_first: bits.append("formal-first")
        if self.uncertainty_aware: bits.append("UNCERTAINTY-AWARE (VoD)")
        elif self.obligation_conditioned: bits.append("OBLIGATION-CONDITIONED")
        elif self.adaptive:   bits.append("ADAPTIVE (design-level)")
        else:                 bits.append(f"explore={self.explore_budget}")
        return ", ".join(bits)


# ---- the comparison set (their experiment A-E) ----------------------------- #
OBLIGATION  = Policy("F-obligation", order="utility",  method_bias="balanced",
                     adaptive=True, obligation_conditioned=True)
DIAGNOSTIC  = Policy("G-diagnostic", order="utility",  method_bias="balanced",
                     adaptive=True, obligation_conditioned=True, diagnostic=True)
UNCERTAINTY = Policy("H-uncertainty", order="utility", method_bias="balanced",
                     adaptive=True, obligation_conditioned=True, diagnostic=True,
                     uncertainty_aware=True)
LOOKAHEAD   = Policy("I-lookahead",  order="lookahead", method_bias="balanced",
                     adaptive=True, obligation_conditioned=True, diagnostic=True)
RANDOM      = Policy("A-random",     order="random",   method_bias="random")
CHEAPEST    = Policy("B-cheapest",   order="cheapest", method_bias="sim",
                     explore_budget=8)
ENGINEER    = Policy("C-engineer",   order="utility",  method_bias="balanced")
HUMAN_ORG   = Policy("D-human-org",  order="utility",  method_bias="balanced",
                     explore_budget=4, formal_first=False)
ADAPTIVE    = Policy("E-adaptive",   order="utility",  method_bias="balanced",
                     adaptive=True)
# The recommended default, decided by a PRE-REGISTERED experiment rather than by
# preference. `H-uncertainty` (posteriors + priced Value-of-Diagnosis) was tested
# against `G-diagnostic` (obligation conditioning + a one-line probe heuristic)
# across 12 independent seeds and 6 design families, with the rule committed and
# hashed before any campaign ran:
#
#     G  E = 1.256 +/- 0.000
#     H  E = 1.291 +/- 0.038      +2.7% relative, threshold was 5%
#     VERDICT: NOT MET — and H's worst run (1.229) falls below G's mean
#
# So the Bayesian machinery does not buy efficiency here, and the committed
# obligation was to SIMPLIFY rather than to re-litigate. G is the default.
# UNCERTAINTY is retained because per-decision confidence and ambiguity are
# genuinely auditable — but that is a separate argument, not an efficiency one,
# and it must not be smuggled back in as performance.
RECOMMENDED = DIAGNOSTIC

POLICY_SET = [RANDOM, CHEAPEST, ENGINEER, HUMAN_ORG, ADAPTIVE]
POLICY_SET_V2 = POLICY_SET + [OBLIGATION]
POLICY_SET_V3 = POLICY_SET_V2 + [DIAGNOSTIC]
POLICY_SET_V4 = POLICY_SET_V3 + [UNCERTAINTY]


class PolicyWorker(Worker):
    """A worker whose behaviour is supplied by a Policy rather than an archetype."""

    def __init__(self, name, policy: Policy, kern, formal, sim, nvec=20000,
                 static=None, rng=None, seed=2026):
        super().__init__(name, policy.name, kern, formal, sim, nvec=nvec, static=static)
        self.policy = policy
        # the seed varies across repeats so campaign variance can be measured
        self.rng = rng or random.Random(seed)
        # ...and it must reach the SIMULATION seed too. Previously `_seed`
        # depended only on the worker name, so every repeat simulated
        # identically and the reported std was structurally 0.000 — a statistic
        # certifying an absence of variance over an axis that was never varied.
        # Once the process-level randomness was removed, a single simulation
        # seed silently decided the H-vs-G comparison, and it flipped.
        self._seed = (self._seed + 977 * (seed % 1000)) % 100000 + 1
        # realised payoff per method, learned during the campaign (adaptive only)
        self.payoff = {"sim": [0.0, 0.0], "formal": [0.0, 0.0]}   # [gain, cost]

    # -- learning from outcomes (adaptive policies only) -------------------- #
    def observe(self, method, gain, cost, phi=None, closed=None):
        if method in self.payoff:
            self.payoff[method][0] += max(0.0, gain)
            self.payoff[method][1] += cost
        # obligation-level: record the yield against this obligation's signature
        if hasattr(self, "yields") and phi is not None:
            self.yields.observe(self._sig(phi), method, gain, cost)
        # belief layer: did this action actually CLOSE the obligation?
        #
        # `closed` must come from the KERNEL (proven/disproven), never from
        # gain > 0. A simulation PASS produces positive gain — it lowers risk by
        # raising n_eff — while settling nothing. Reading gain as closure made
        # simulation look successful every time, so the policy ran 162 sim
        # passes in one campaign and never learned to reach for a proof. This is
        # the same inductive-shaving-vs-discharge conflation that was fixed in
        # the efficiency metric, recurring one layer up.
        if hasattr(self, "belief") and phi is not None and method in ("sim", "formal"):
            self.belief.observe(self._sig(phi), method,
                                closed=bool(closed), phi=phi)

    def _rate(self, method):
        g, c = self.payoff[method]
        return (g / c) if c > 0 else None      # None = untried

    def _pick_task(self, cands, ks, weights):
        p = self.policy
        if p.order == "random":
            return self.rng.choice(cands)
        if p.order == "cheapest":
            return min(cands, key=lambda t: t.weight)
        if p.order == "weight":
            return max(cands, key=lambda t: t.weight)
        if p.order == "lookahead":
            # Value an obligation by its own weight PLUS the weight it unlocks.
            # A one-step expected-value rule sees only the first term, so a cheap
            # lemma that carries little weight itself but enables most of the
            # board is invisible to it — which is the failure mode this order
            # exists to test, not to assume.
            def val(t):
                immediate = self.k.utility(ks, weights, t.phi)[0]
                unlocked = sum(weights.get(e, 0.0) for e in getattr(t, "enables", ()))
                return immediate + 0.8 * unlocked
            return max(cands, key=val)
        return max(cands, key=lambda t: self.k.utility(ks, weights, t.phi)[0])

    def _pick_method(self, phi, ks, ledger):
        p = self.policy
        can_sim = self.sim.covers(phi)
        affordable = [m for m in ("sim", "formal") if ledger.can_afford(m)]
        if not affordable:
            return None
        if not can_sim:
            return "formal" if "formal" in affordable else None
        if p.formal_first:
            return "formal" if "formal" in affordable else "sim"
        if p.method_bias == "random":
            return self.rng.choice(affordable)
        if p.method_bias == "formal":
            return "formal" if "formal" in affordable else "sim"
        if p.adaptive:
            # exploit the channel with the better realised risk-per-cost so far;
            # try each at least once before committing to a preference.
            rs, rf = self._rate("sim"), self._rate("formal")
            if rs is None and "sim" in affordable:
                return "sim"
            if rf is None and "formal" in affordable:
                return "formal"
            best = "sim" if (rs or 0) >= (rf or 0) else "formal"
            return best if best in affordable else affordable[0]
        n = ks.n_eff(phi)
        if p.method_bias == "sim":
            return "sim" if ("sim" in affordable and n < p.explore_budget) else \
                   ("formal" if "formal" in affordable else "sim")
        if n < p.explore_budget and "sim" in affordable:
            return "sim"
        return "formal" if "formal" in affordable else "sim"

    # ---- obligation-level conditioning (Omega_i) --------------------------- #
    def attach_features(self, feature_map):
        """feature_map: phi -> ObligationFeatures. Enables pi(a | Omega_i)."""
        from obligation_state import YieldModel
        from regime import RegimeBelief
        self.features = feature_map
        self.yields = YieldModel()
        self._probed = set()          # one diagnostic probe per obligation
        # posterior over "does this action close an obligation like this one",
        # carrying its own spread. A signature is an OBSERVATION consistent with
        # several regimes, not a regime.
        self.belief = RegimeBelief(seed=self.rng.randint(1, 1 << 30))
        self.decisions = []           # audit trail: what it knew when it acted

    def _sig(self, phi):
        f = getattr(self, "features", {}).get(phi)
        return f.signature() if f else None

    def _weight_of(self, phi):
        return getattr(self, "_weights", {}).get(phi, 5.0)

    def _uncertain(self, sig):
        """Is this signature's outcome genuinely in doubt?

        Not 'do I lack a model' but 'have obligations that look like this one
        behaved BOTH ways'. That is the case where a cheap probe buys something:
        the ambiguity between a true property (needs an expensive proof) and a
        false one (needs a cheap counterexample) is not resolvable from
        structure, only from evidence.
        """
        s = self.yields.stats.get(sig, {})
        if not s:
            return True                      # never seen: worth a cheap look
        closed_any = any(v[0] > 0 for v in s.values())
        spent_any = any(v[1] > 0 and v[0] == 0 for v in s.values())
        return closed_any and spent_any      # mixed history for this class

    def _pick_method_conditioned(self, phi, ledger):
        """Choose by YIELD for obligations that look like this one.

        Falls back to the global prior for an unseen signature, and tries an
        untried channel once so a whole class is never written off on no
        evidence. This is the difference between 'formal is good' and 'formal
        is usually effective for this kind of property under these structural
        conditions'.
        """
        affordable = [m for m in ("sim", "formal") if ledger.can_afford(m)]
        if not affordable:
            return None
        if not self.sim.covers(phi):
            return "formal" if "formal" in affordable else None
        sig = self._sig(phi)

        # ---- uncertainty-aware branch: decide by P(a is best), not by a mean --
        if self.policy.uncertainty_aware and hasattr(self, "belief"):
            weight = self._weight_of(phi)
            costs = {m: ACTION_COST[m] for m in affordable}
            info = self.belief.explain(sig, affordable, weight, costs,
                                       ACTION_COST["probe"], phi=phi)
            self.decisions.append((phi, info))
            # Probe only when it PAYS: positive Value of Diagnosis. Not "when
            # uncertain" — uncertainty that no probe can reduce is not worth
            # buying, and a probe that costs more than the information is worth
            # is just overhead.
            if (self.policy.diagnostic and phi not in self._probed
                    and ledger.can_afford("probe") and info["vod"] > 0):
                self._probed.add(phi)
                return "probe"
            return info["best"] if info["best"] in affordable else affordable[0]

        # Diagnostic step: when this class of obligation has behaved both ways,
        # buy information cheaply before committing to an expensive action.
        if (self.policy.diagnostic and self._uncertain(sig)
                and phi not in self._probed and ledger.can_afford("probe")):
            self._probed.add(phi)
            return "probe"
        rates = {m: self.yields.rate(sig, m) for m in affordable}
        untried = [m for m, r in rates.items() if r is None]
        if untried:
            return untried[0]
        return max(affordable, key=lambda m: rates[m] or 0.0)

    def propose(self, ks, board, ledger):
        cands = [t for t in board.actionable(ks) if t.phi not in self.skip]
        if not cands:
            return None
        best = self._pick_task(cands, ks, board.weights())
        if best.kind == "structural" and self.static is not None:
            if not ledger.can_afford("static"):
                return None
            return Proposal(self, best.phi, "static",
                            self.k.utility(ks, board.weights(), best.phi)[0],
                            ACTION_COST["static"])
        self._weights = board.weights()          # for utility-weighted beliefs
        if self.policy.obligation_conditioned and hasattr(self, "yields"):
            method = self._pick_method_conditioned(best.phi, ledger)
        else:
            method = self._pick_method(best.phi, ks, ledger)
        if method is None:
            return None
        return Proposal(self, best.phi, method,
                        self.k.utility(ks, board.weights(), best.phi)[0],
                        ACTION_COST[method])

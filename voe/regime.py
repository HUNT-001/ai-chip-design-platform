"""Regime belief, ambiguity, and the Value of Diagnosis.

The diverse-regime benchmark produced a result that inverts the obvious reading
of Experiment 3: **obligation-level conditioning performed WORSE than
design-level conditioning** (86.0% vs 91.4% of the realisable ceiling). The
cause is visible in the class table — one signature covered 8 true and 3 false
obligations, so its per-class average blended two populations that want opposite
actions, and acting on that average was worse than acting on a global rate.

The wrong conclusion is "obligation conditioning was a bad idea". The right one
is **uncertainty-blind conditioning was a bad idea**:

    a signature is not a regime.
    It is an observation consistent with several regimes.

So the estimate here is not a point rate. It is a posterior over "does this
action close an obligation that looks like this one", carrying its own spread —
and the spread is what decides whether the policy may commit or must first buy
information.

Three quantities follow:

    P(a is best)   sampled from the posteriors, not read off a mean
    ambiguity      1 - max_a P(a is best); how unsure the RANKING is
    VoD(d)         expected utility gained by probing, minus the probe's cost

The rule that falls out is the architectural principle this whole sequence has
been converging on:

    never increase specialisation without increasing diagnosability.

A richer representation may only drive more specific decisions if the system can
also detect and resolve the ambiguity that representation exposes.

--------------------------------------------------------------------------------
STATUS: THIS MODULE FAILED ITS OWN PRE-REGISTERED TEST. It is NOT the default.

The principle above is supported (diagnosis beat blind conditioning by +11.6%,
far outside the spread). What is NOT supported is that *this* machinery is the
right way to implement it. Tested against `G-diagnostic` — obligation
conditioning plus a one-line probe heuristic — over 12 seeds and 6 design
families, under a rule committed and hashed before any campaign ran:

    G-diagnostic    E = 1.256 +/- 0.000   probe cost 1.00
    H-uncertainty   E = 1.291 +/- 0.038   probe cost 2.75
    +2.7% relative against a 5% threshold  ->  NOT MET
    (and H's worst run, 1.229, falls below G's mean)

Nearly three times the probing cost for a gain that does not clear the committed
bar. `policy.RECOMMENDED` is therefore NOT `UNCERTAINTY`. (It was `DIAGNOSTIC`
until Experiment 11 promoted `STATIC_CACHED` over it on real tools; this module
is off the default path either way.)

This module is retained for ONE reason: it reports per-decision confidence,
ambiguity and Value of Diagnosis, which a realised-rate heuristic cannot. That
is genuine auditability — a policy that can say "best=formal, confidence=0.51,
ambiguity=0.49" is stating something a point estimate cannot. But auditability
must be argued on its own terms and must not be reintroduced as an efficiency
claim the data does not carry.

Do not build a predictive world model on top of this layer. Its motivating gap —
"the planner needs a better model of which probe is worth running" — is exactly
what the measurement did not find: a one-line heuristic already selects probes as
well as a priced VoD does.
--------------------------------------------------------------------------------
"""
from __future__ import annotations
import random
from dataclasses import dataclass, field


@dataclass
class BetaStat:
    """Closed / did-not-close counts for one (signature, action) pair."""
    s: float = 0.0      # closed the obligation
    f: float = 0.0      # spent budget, closed nothing

    @property
    def n(self):
        return self.s + self.f


class RegimeBelief:
    """P(action closes | signature), pooled global <- class <- obligation.

    Partial pooling is the direct fix for the failure observed: a hard partition
    treats 11 obligations that share a signature as interchangeable, while no
    pooling at all makes every obligation a stranger. Borrowing strength from the
    global rate keeps a thin class honest, and the class evidence takes over as
    it accumulates.
    """

    def __init__(self, pool: float = 2.0, seed: int = 11, local_weight: float = 4.0):
        self.pool = pool                      # prior weight of the global rate
        self.local_weight = local_weight      # how loudly THIS obligation speaks
        self.by_sig: dict = {}                # sig -> {action: BetaStat}
        self.by_obl: dict = {}                # phi -> {action: BetaStat}
        self.glob: dict = {}                  # action -> BetaStat
        self.rng = random.Random(seed)

    # -- evidence ----------------------------------------------------------- #
    def observe(self, sig, action, closed: bool, phi=None):
        d = self.by_sig.setdefault(sig, {})
        st = d.setdefault(action, BetaStat())
        g = self.glob.setdefault(action, BetaStat())
        if closed:
            st.s += 1; g.s += 1
        else:
            st.f += 1; g.f += 1
        # OBLIGATION-LOCAL evidence — the level the first version omitted.
        # Without it, three failed simulations on one obligation barely move a
        # class-wide mean, so the policy re-attempts the same losing action and
        # thrashes. Local evidence is what stops it: this obligation has now
        # said something about itself, and it speaks louder than its class.
        if phi is not None:
            o = self.by_obl.setdefault(phi, {})
            ost = o.setdefault(action, BetaStat())
            if closed:
                ost.s += 1
            else:
                ost.f += 1

    # Structural priors, taken from the KERNEL's warrant asymmetry rather than
    # tuned to any board. Simulation is inductive: it closes an obligation only
    # by counterexample, i.e. only when the property is FALSE — which is the
    # minority case in mature RTL. Formal is deductive: it closes by proof OR by
    # counterexample, so it closes far more often.
    #
    # A uniform 0.5 prior for both is not neutral, it is wrong, and combined with
    # cost-normalised utility (w*p/cost) it hands the cheap channel a permanent
    # 4x advantage: simulation is chosen forever, formal is never sampled, and
    # its posterior never leaves the prior. That is exactly what the first
    # uncertainty-aware run did — best=sim at confidence 0.94-1.00 everywhere.
    PRIOR_CLOSE = {"sim": 0.20, "formal": 0.70, "probe": 0.20, "static": 0.50}

    def _global_p(self, action):
        g = self.glob.get(action)
        p0 = self.PRIOR_CLOSE.get(action, 0.5)
        if not g or g.n == 0:
            return p0
        # the structural prior is worth ~2 observations, then evidence takes over
        return (g.s + 2.0 * p0) / (g.n + 2.0)

    def posterior(self, sig, action, phi=None):
        """(alpha, beta) for P(closes) — global <- class <- this obligation.

        Partial pooling, not partitioning: a thin class borrows strength from the
        global rate, class evidence takes over as it accumulates, and evidence
        from THIS obligation dominates both. That ordering is what prevents the
        two failure modes seen so far — treating 11 obligations that share a
        signature as interchangeable, and treating each as a stranger.
        """
        st = self.by_sig.get(sig, {}).get(action, BetaStat())
        gp = self._global_p(action)
        al = 1.0 + st.s + self.pool * gp
        be = 1.0 + st.f + self.pool * (1.0 - gp)
        if phi is not None:
            o = self.by_obl.get(phi, {}).get(action)
            if o:
                al += self.local_weight * o.s
                be += self.local_weight * o.f
        return al, be

    # -- decision-level uncertainty ----------------------------------------- #
    def p_best(self, sig, actions, weight, costs, draws: int = 400, phi=None):
        """P(each action is the best) by sampling the posteriors.

        Sampling rather than comparing means is the whole point: two actions
        with means 1.12 and 1.08 but wide spreads are NOT a decision, and a
        point estimate cannot say so.
        """
        wins = {a: 0 for a in actions}
        for _ in range(draws):
            best, bv = None, -1.0
            for a in actions:
                al, be = self.posterior(sig, a, phi)
                p = self.rng.betavariate(al, be)
                v = weight * p / costs[a]
                if v > bv:
                    best, bv = a, v
            wins[best] += 1
        return {a: wins[a] / draws for a in actions}

    def ambiguity(self, sig, actions, weight, costs, draws: int = 400, phi=None):
        pb = self.p_best(sig, actions, weight, costs, draws, phi)
        return 1.0 - max(pb.values()), pb

    def expected_utility(self, sig, action, weight, cost, phi=None):
        al, be = self.posterior(sig, action, phi)
        return weight * (al / (al + be)) / cost

    # -- Value of Diagnosis -------------------------------------------------- #
    def vod(self, sig, actions, weight, costs, probe_cost, probe_action="sim", phi=None):
        """VoD(d) = E_o[ max_a E[U(a) | o] ] - max_a E[U(a)] - c(d).

        The probe is a very short run of `probe_action`. Its informativeness is
        modelled by the two outcomes that matter operationally:

            it finds a counterexample -> the obligation is FALSE, so the cheap
                                         channel closes it and the choice is settled
            it finds nothing          -> evidence shifts against the cheap channel

        Note what this does NOT claim: the probe does not reveal whether the
        property is true. It reveals something about which STRATEGY will pay,
        which is the reducible half of the uncertainty. World uncertainty stays
        where it is.
        """
        v_blind = max(self.expected_utility(sig, a, weight, costs[a], phi) for a in actions)

        al, be = self.posterior(sig, probe_action, phi)
        p_cex = al / (al + be)                 # chance the cheap channel closes it

        # outcome 1: probe hits a counterexample -> cheap channel settles it
        v_hit = weight / costs[probe_action]

        # outcome 2: probe finds nothing -> update that channel downward, re-rank
        shadow = RegimeBelief(self.pool, seed=self.rng.randint(1, 1 << 30))
        shadow.by_sig = {k: {a: BetaStat(v.s, v.f) for a, v in d.items()}
                         for k, d in self.by_sig.items()}
        shadow.glob = {a: BetaStat(v.s, v.f) for a, v in self.glob.items()}
        shadow.by_obl = {k: {a: BetaStat(v.s, v.f) for a, v in d.items()}
                         for k, d in self.by_obl.items()}
        shadow.observe(sig, probe_action, closed=False, phi=phi)
        v_miss = max(shadow.expected_utility(sig, a, weight, costs[a], phi) for a in actions)

        v_diag = p_cex * v_hit + (1.0 - p_cex) * v_miss
        return v_diag - v_blind - probe_cost, p_cex

    # -- reporting ----------------------------------------------------------- #
    def explain(self, sig, actions, weight, costs, probe_cost, phi=None):
        amb, pb = self.ambiguity(sig, actions, weight, costs, phi=phi)
        vod, p_cex = self.vod(sig, actions, weight, costs, probe_cost, phi=phi)
        best = max(pb, key=pb.get)
        return {"best": best, "confidence": pb[best], "ambiguity": amb,
                "vod": vod, "p_cheap_closes": p_cex, "p_best": pb}

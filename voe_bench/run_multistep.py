"""Experiment 8 — GROUNDED multi-step diagnosis, pre-registered.

The first revisit condition that could legitimately resurrect the rejected
belief/VoD family. The question is narrow and falsifiable:

    Can the value of an action depend on information a LATER action will reveal?

If yes, a one-step expected-value rule is structurally insufficient and lookahead
(eventually, a learned dynamics model) has a derived justification. If no, the
current heuristic is sufficient across another regime — which is an equally
useful result and means the world model should NOT be built.

**Both diagnostic actions are REAL tools.** Nothing here is modelled — that is
the difference from Experiments 6 and 7, whose dependency structure was declared:

    probe   a short Verilator run (0.25) — is there a counterexample?
    static  rtl_graph structural analysis (0.5) — does this design contain a
            datapath multiply or deep state, which predicts solver cost?

The sequence matters, and that is the whole point: the structural probe is worth
running ONLY if the simulation probe came back clean. A counterexample settles
the obligation and makes the structural question moot. A one-step rule cannot
represent "this action is worth taking because of what the next one will tell
me".

    G-diagnostic   one-step: one probe, then commit
    K-multistep    probe -> interpret -> structural probe -> commit
    realisable     best fixed action per signature, pre-action information only

Pre-registered in `prereg_multistep.json` before any campaign runs, with the same
discipline that rejected the Bayesian layer: if K does not clear the committed
bar, it is not promoted, and the honest reading is that the simple policy is
sufficient here too.

    python run_multistep.py --mock | --real [--seeds N]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from board import Task
from evidence_channels import StaticChannel
from evaluation import run_campaign, aggregate
from policy import DIAGNOSTIC, MULTISTEP, PolicyWorker
from preregistration import Criteria, Preregistration
from voe import VOE
from obligation_state import ObligationFeatures
from registry import KIND
from run_benchmark import (TASKS, FormalRouter, SimRouter, build_features,
                           outcome_table, realisable, BUDGET)


def unknown_features():
    """Both arms start structurally IGNORANT.

    An earlier version handed every policy the probed structure up front, so the
    multi-step arm paid 0.5 for facts it already held and the experiment could
    not test its own hypothesis. Structure is now something a policy must SPEND
    to learn — and only the multi-step arm can spend on it.
    """
    return {t.phi: ObligationFeatures(KIND[t.phi], False, False, "unknown")
            for t in TASKS}
from registry import DESIGNS, OWNER, summary

PREREG = os.path.join(HERE, "prereg_multistep.json")
DEFAULT_SEEDS = 12

CRITERIA = Criteria(
    question="Can a SEQUENCE of cheap real diagnostic actions (short simulation, "
             "then structural analysis) allocate verification effort better than "
             "a one-step diagnostic policy, on real RTL with real tools?",
    treatment="K-multistep",
    control="G-diagnostic",
    min_seeds=DEFAULT_SEEDS,
    min_design_families=3,
    min_effect=0.05,
    noise_multiple=2.0,
    max_ci_width=0.10,
)


class StaticRouter:
    """Real rtl_graph analysis, routed to the design each obligation belongs to."""

    def __init__(self, mock):
        self.ch = {n: StaticChannel(d["rtl"], mock=mock) for n, d in DESIGNS.items()}
        self.current = None

    def for_phi(self, phi):
        return self.ch[OWNER[phi]]

    def rtl_for(self, phi):
        return DESIGNS[OWNER[phi]]["rtl"] if phi in OWNER else None

    def check(self, prop="comb_loops"):
        return self.ch[OWNER[self.current]].check(prop) if self.current else \
            next(iter(self.ch.values())).check(prop)


class RoutedStaticWorker(PolicyWorker):
    """Tells the static router which obligation is being probed."""

    def execute(self, ks, board, phi, method):
        if method == "static" and hasattr(self.static, "current"):
            self.static.current = phi
        return super().execute(ks, board, phi, method)


def build(policy, mock, routers, static, seed, feats):
    formal, sim = routers
    v = VOE([Task(**vars(t)) for t in TASKS], budget=BUDGET, mock=mock,
            formal=formal, sim=sim)
    w = RoutedStaticWorker(policy.name, policy, v.k, formal, sim,
                           seed=seed, static=static)
    if policy.obligation_conditioned:
        w.attach_features(feats)
    v.workers = [w]
    return v


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    prereg = Preregistration(CRITERIA, PREREG,
                             notes="Both probes are real tools (Verilator, "
                                   "rtl_graph). Nothing modelled.").commit()
    print("=== Experiment 8: grounded multi-step diagnosis ===")
    print(f"    {summary()}")
    print(f"    question : {prereg.criteria.question}")
    print(f"    rule     : {prereg.criteria.rule()}")
    print(f"    committed: sha256:{prereg.digest}  "
          f"{'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds    : {seeds}   mode = {'MOCK' if mock else 'REAL'}\n")

    feats = build_features()          # ground truth, for the oracle only
    routers = (FormalRouter(mock), SimRouter(mock))
    static = StaticRouter(mock)
    families = len({OWNER[t.phi] for t in TASKS})
    real_E, *_ = realisable(outcome_table(routers, feats))

    results = []
    for p in (DIAGNOSTIC, MULTISTEP):
        for i in range(seeds):
            results.append(run_campaign(
                build(p, mock, routers, static, 7000 + i, unknown_features()),
                "bench", p.name))
        print(f"    {p.name:14s} {seeds} seeds done", flush=True)

    aggs = {a.policy: a for a in aggregate(results)}
    g, k = aggs[DIAGNOSTIC.name], aggs[MULTISTEP.name]

    print(f"\n  {'arm':14s} {'mean E':>7s} {'std':>7s} {'worst':>7s} "
          f"{'probe':>7s} {'%realis':>8s}")
    for a in (g, k):
        print(f"  {a.policy:14s} {a.mean_E:7.3f} {a.std_E:7.3f} {a.worst_E:7.3f} "
              f"{a.probe_cost:7.2f} {a.mean_E/real_E:7.1%}")
    print(f"  {'realisable':14s} {real_E:7.3f}")

    verdict, reasons = prereg.decide(k.mean_E, k.std_E, g.mean_E, g.std_E,
                                     seeds, families)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")

    print("\n=== what this obliges ===")
    if verdict == "MET":
        print("  The value of an action DOES depend on what a later action will")
        print("  reveal. A one-step rule is structurally insufficient here, and")
        print("  multi-step planning — eventually a learned dynamics model — now")
        print("  has a derived justification rather than an assumed one.")
        print("  Re-open the rejected capability record under MULTI-STEP DIAGNOSIS.")
    elif verdict == "NOT MET":
        print("  Chaining diagnostics buys nothing here. The one-step heuristic")
        print("  is sufficient across a THIRD regime, which is a real result:")
        print("  do NOT build the world model. Record it and move to the next")
        print("  revisit condition rather than re-running until it wins.")
    else:
        print("  The experiment cannot answer the question. NOT a negative")
        print("  result. Add seeds or design families and change nothing else.")


if __name__ == "__main__":
    main()

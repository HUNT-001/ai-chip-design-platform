"""Experiment 9 — ABLATION: is it the sequencing, or just the information?

Experiment 8 returned MET (+15.0%, real tools, 12 seeds). Read carelessly, that
licenses multi-step planning and, eventually, a learned dynamics model. Read
carefully, it does not — because the control never obtained structural
information AT ALL. K beat G while doing two things G could not do:

    1. it acquired real structural facts about the design, and
    2. it acquired them CONDITIONALLY, only where a prior probe left the
       decision open.

Only (2) is evidence for multi-step machinery. (1) is an argument for reading the
RTL, which needs no planner, no lookahead and no world model. A comparison that
confounds the two cannot decide what to build next, and "what to build next" is
the entire purpose of the question.

So L-static-onestep buys exactly the same information, from the same real tool,
at the same price — unconditionally, in one step, with no probe first and no
gating. The three arms then separate cleanly:

    G   no structure                        (incumbent)
    L   structure, no sequencing            (information only)
    K   structure, conditionally acquired   (information + sequencing)

    K vs L is the ONLY comparison that isolates sequencing.

Committed rule, fixed before any campaign runs:

    K must beat L by the same bar Experiment 8 used. If it does not, the honest
    conclusion is that Experiment 8 measured the value of READING THE DESIGN, the
    one-step architecture is sufficient after all, and the world model is still
    unjustified — the rejected capability record stays closed.

    python run_ablation.py --mock | --real [--seeds N]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from evaluation import run_campaign, aggregate
from institutional_memory import promotion_verdict
from policy import DIAGNOSTIC, MULTISTEP, STATIC_ONE
from preregistration import Criteria, Preregistration
from registry import OWNER, summary
from run_benchmark import realisable, outcome_table, build_features
from run_multistep import build, unknown_features, StaticRouter
from run_benchmark import FormalRouter, SimRouter, TASKS

PREREG = os.path.join(HERE, "prereg_ablation.json")
DEFAULT_SEEDS = 12

CRITERIA = Criteria(
    question="Does the SEQUENCING earn Experiment 8's gain, or does merely "
             "obtaining structural information earn it? K-multistep is compared "
             "against L-static-onestep, which buys the same real structural "
             "evidence at the same price without any conditional chain.",
    treatment="K-multistep",
    control="L-static-onestep",
    min_seeds=DEFAULT_SEEDS,
    min_design_families=3,
    min_effect=0.05,
    noise_multiple=2.0,
    max_ci_width=0.10,
)


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    prereg = Preregistration(
        CRITERIA, PREREG,
        notes="Ablation of Experiment 8. L receives the SAME structural channel "
              "and the same 0.5 price as K; the only difference is that K "
              "acquires it conditionally on a prior probe. Any gain K shows over "
              "L is attributable to sequencing and to nothing else.").commit()

    print("=== Experiment 9: ablation — sequencing vs information ===")
    print(f"    {summary()}")
    print(f"    question : {prereg.criteria.question}")
    print(f"    rule     : {prereg.criteria.rule()}")
    print(f"    committed: sha256:{prereg.digest}  "
          f"{'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds    : {seeds}   mode = {'MOCK' if mock else 'REAL'}\n")

    routers = (FormalRouter(mock), SimRouter(mock))
    static = StaticRouter(mock)
    families = len({OWNER[t.phi] for t in TASKS})
    real_E, *_ = realisable(outcome_table(routers, build_features()))

    results = []
    for p in (DIAGNOSTIC, STATIC_ONE, MULTISTEP):
        for i in range(seeds):
            results.append(run_campaign(
                build(p, mock, routers, static, 7000 + i, unknown_features()),
                "bench", p.name))
        print(f"    {p.name:18s} {seeds} seeds done", flush=True)

    aggs = {a.policy: a for a in aggregate(results)}
    g, l, k = (aggs[DIAGNOSTIC.name], aggs[STATIC_ONE.name], aggs[MULTISTEP.name])

    print(f"\n  {'arm':18s} {'mean E':>7s} {'std':>7s} {'worst':>7s} "
          f"{'probe':>7s} {'%realis':>8s}")
    for a in (g, l, k):
        print(f"  {a.policy:18s} {a.mean_E:7.3f} {a.std_E:7.3f} {a.worst_E:7.3f} "
              f"{a.probe_cost:7.2f} {a.mean_E / real_E:7.1%}")
    print(f"  {'realisable':18s} {real_E:7.3f}")

    # how much of Experiment 8's gain survives once the control can read the RTL?
    gain_vs_g = (k.mean_E - g.mean_E) / g.mean_E
    gain_vs_l = (k.mean_E - l.mean_E) / l.mean_E
    info_share = (l.mean_E - g.mean_E) / (k.mean_E - g.mean_E) if k.mean_E != g.mean_E else 0.0
    print(f"\n  K over G (Experiment 8's claim) : {gain_vs_g:+.1%}")
    print(f"  K over L (sequencing ALONE)     : {gain_vs_l:+.1%}")
    print(f"  share of the gain that is just information : {info_share:.0%}")

    verdict, reasons = prereg.decide(k.mean_E, k.std_E, l.mean_E, l.std_E,
                                     seeds, families)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")

    # Complexity charge. K's extra diagnostic spend (probe_cost) is ALREADY in
    # the denominator of E, so adding it to the bar would charge it twice and
    # understate the effect. The charge that is NOT yet priced is architectural:
    # a chained planner carries per-obligation state (_probed, _probe_clean,
    # _structprobed, _simcount), and every one of those four fields was the site
    # of a defect in Experiment 8. That is paid for here by requiring the effect
    # to clear the measured spread, which is what the committed rule already
    # does — so the explicit extra charge is 0.0, stated rather than assumed.
    spread_rel = max(k.std_E, l.std_E) / max(l.mean_E, 1e-9)
    ok, why = promotion_verdict(gain_vs_l, 0.05, spread_rel, 0.0)
    print(f"    diagnostic spend: K {k.probe_cost:.2f} vs L {l.probe_cost:.2f} "
          f"— already inside E, not charged twice")
    print(f"    {why}")

    print("\n=== what this obliges ===")
    if verdict == "MET" and ok:
        print("  The CONDITIONALITY is what pays, not merely reading the RTL.")
        print("  Sequencing has an earned justification. Multi-step diagnosis may")
        print("  be promoted, and a learned dynamics model becomes the next")
        print("  hypothesis to TEST — not yet a component to build.")
    elif verdict == "MET":
        print("  K clears the statistical bar but not the complexity charge.")
        print("  Keep L: same information, less machinery.")
    else:
        print("  Experiment 8 measured the value of READING THE DESIGN, not of")
        print("  chaining diagnostics. One structural read, unconditionally, gets")
        print("  the same result with no planner. The one-step architecture is")
        print("  sufficient across this regime too, the rejected capability record")
        print("  STAYS CLOSED, and the world model remains unjustified.")


if __name__ == "__main__":
    main()

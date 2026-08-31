"""Experiment 10 — should L-static-onestep replace G-diagnostic as the DEFAULT?

Experiment 9 produced, as a side observation, the largest and cleanest effect
this project has measured: L reached 1.083 against G's 0.998 (+8.5%) while
spending NOTHING on diagnosis. That observation cannot promote anything, because
Experiment 9's committed treatment/control pair was K vs L — reading a different
contrast out of the same data afterwards is the retrospective-oracle move this
project exists to refuse. So the comparison is re-run under its own committed
rule, and this file is that rule.

What makes L unusual among everything tested so far: it is SIMPLER than the
incumbent, not more complex. It carries no probe machinery, no per-obligation
diagnostic state, no chain. It reads each design's structure once with a real
tool and commits. Every previous candidate (H's posteriors, K's chain) had to
overcome a complexity charge; L would REMOVE machinery, and the charge is
therefore 0.0 — stated here rather than silently omitted, because a discount
applied quietly is indistinguishable from a bar that moved to fit the result.

HELD-OUT PROTOCOL. The policies were developed against toy_alu, ibex_alu, fifo
and multiplier. lfsr and mv_filter were held out. Both splits are reported, and
the committed rule below requires the full-benchmark effect to clear the bar AND
the held-out split not to contradict it. The held-out split has only TWO design
families, which is below the three this project normally demands — that is a
real weakness of the available corpus, recorded here rather than papered over by
lowering the primary bar to match it.

    python run_default.py --mock | --real [--seeds N]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from board import Task
from evaluation import run_campaign, aggregate
from institutional_memory import promotion_verdict
from policy import DIAGNOSTIC, STATIC_ONE, PolicyWorker
from preregistration import Criteria, Preregistration
from registry import DESIGNS, OWNER, summary
from run_benchmark import (TASKS, FormalRouter, SimRouter, build_features,
                           outcome_table, realisable, BUDGET)
from run_multistep import StaticRouter, RoutedStaticWorker, unknown_features
from voe import VOE

PREREG = os.path.join(HERE, "prereg_default.json")
DEFAULT_SEEDS = 24

HELD_OUT = tuple(n for n, d in DESIGNS.items() if "voe_heldout" in d["rtl"])
DEV = tuple(n for n in DESIGNS if n not in HELD_OUT)

CRITERIA = Criteria(
    question="Should L-static-onestep (one real structural read per obligation, "
             "no probe, no chain) replace G-diagnostic as the recommended "
             "default policy, on real RTL with real tools?",
    treatment="L-static-onestep",
    control="G-diagnostic",
    min_seeds=DEFAULT_SEEDS,
    min_design_families=3,
    min_effect=0.05,
    noise_multiple=2.0,
    max_ci_width=0.10,
)

NOTES = (
    "Committed before any campaign. THREE conditions, all required:\n"
    " (1) the full-benchmark comparison clears the rule above;\n"
    " (2) the HELD-OUT split (lfsr, mv_filter — only 2 design families, below "
    "     this project's normal 3, and that is a corpus weakness not a relaxed "
    "     bar) does not show L WORSE than G;\n"
    " (3) L closes at least as much weight as G. A policy that spends less by "
    "     settling less is cheaper, not better, and closed_weight is the metric "
    "     that already caught that exact gaming once.\n"
    "Complexity charge is 0.0 because L REMOVES machinery rather than adding "
    "it. If the sign were reversed the charge would apply."
)


def build(policy, mock, routers, static, seed, tasks):
    formal, sim = routers
    v = VOE([Task(**vars(t)) for t in tasks], budget=BUDGET, mock=mock,
            formal=formal, sim=sim)
    w = RoutedStaticWorker(policy.name, policy, v.k, formal, sim,
                           seed=seed, static=static)
    if policy.obligation_conditioned:
        w.attach_features({t.phi: unknown_features()[t.phi] for t in tasks})
    v.workers = [w]
    return v


def campaign_set(policy, mock, routers, static, seeds, tasks, label):
    out = []
    for i in range(seeds):
        out.append(run_campaign(build(policy, mock, routers, static, 7000 + i,
                                      tasks), label, policy.name))
    return out


def closed_wt(a):
    """Mean weight actually DISCHARGED, read from the campaigns.

    `Aggregate` exposes efficiency but not closure, and efficiency alone cannot
    tell a better strategy from a cheaper one that settles less — the exact
    conflation that once produced E=5.0 with zero proofs.
    """
    return sum(r.closed_weight for r in a.runs) / len(a.runs) if a.runs else 0.0


def report(title, aggs, real_E=None):
    g, l = aggs
    print(f"\n  --- {title} ---")
    print(f"  {'arm':18s} {'mean E':>7s} {'std':>7s} {'worst':>7s} "
          f"{'probe':>7s} {'closed wt':>10s}")
    for a in (g, l):
        print(f"  {a.policy:18s} {a.mean_E:7.3f} {a.std_E:7.3f} {a.worst_E:7.3f} "
              f"{a.probe_cost:7.2f} {closed_wt(a):10.1f}")
    if real_E:
        print(f"  {'realisable':18s} {real_E:7.3f}")
    return (l.mean_E - g.mean_E) / g.mean_E if g.mean_E else 0.0


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    prereg = Preregistration(CRITERIA, PREREG, notes=NOTES).commit()
    print("=== Experiment 10: should the DEFAULT policy change? ===")
    print(f"    {summary()}")
    print(f"    dev      : {', '.join(DEV)}")
    print(f"    held out : {', '.join(HELD_OUT)}")
    print(f"    question : {prereg.criteria.question}")
    print(f"    rule     : {prereg.criteria.rule()}")
    print(f"    committed: sha256:{prereg.digest}  "
          f"{'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds    : {seeds}   mode = {'MOCK' if mock else 'REAL'}")

    routers = (FormalRouter(mock), SimRouter(mock))
    static = StaticRouter(mock)
    real_E, *_ = realisable(outcome_table(routers, build_features()))

    held = [t for t in TASKS if OWNER[t.phi] in HELD_OUT]
    dev = [t for t in TASKS if OWNER[t.phi] in DEV]

    res = {}
    for label, tasks in (("full", TASKS), ("dev", dev), ("held-out", held)):
        rows = []
        for p in (DIAGNOSTIC, STATIC_ONE):
            rows += campaign_set(p, mock, routers, static, seeds, tasks, label)
        res[label] = {a.policy: a for a in aggregate(rows)}
        print(f"    {label:9s} done", flush=True)

    pairs = {k: (v[DIAGNOSTIC.name], v[STATIC_ONE.name]) for k, v in res.items()}
    gain_full = report("FULL BENCHMARK (6 designs)", pairs["full"], real_E)
    report("DEV (policies were developed here)", pairs["dev"])
    gain_held = report("HELD OUT (2 families — thin, see notes)", pairs["held-out"])

    g, l = pairs["full"]
    families = len({OWNER[t.phi] for t in TASKS})
    verdict, reasons = prereg.decide(l.mean_E, l.std_E, g.mean_E, g.std_E,
                                     seeds, families)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")

    spread_rel = max(l.std_E, g.std_E) / max(g.mean_E, 1e-9)
    priced, why = promotion_verdict(gain_full, 0.05, spread_rel, 0.0)
    print(f"    {why}")
    print(f"    complexity charge 0.0 — L removes machinery (no probe, no chain)")

    # the two side conditions, committed above and checked here
    hg, hl = pairs["held-out"]
    c2 = hl.mean_E >= hg.mean_E
    c3 = closed_wt(l) >= closed_wt(g)
    print(f"\n    (2) held-out does not contradict : {'PASS' if c2 else 'FAIL'} "
          f"(L {hl.mean_E:.3f} vs G {hg.mean_E:.3f}, {gain_held:+.1%})")
    print(f"    (3) L closes >= G's weight       : {'PASS' if c3 else 'FAIL'} "
          f"(L {closed_wt(l):.1f} vs G {closed_wt(g):.1f})")

    print("\n=== what this obliges ===")
    if verdict == "MET" and priced and c2 and c3:
        print("  Change RECOMMENDED from G-diagnostic to L-static-onestep.")
        print("  The cheapest real improvement to this system was not a planner,")
        print("  a posterior or a chain — it was READING THE DESIGN ONCE with a")
        print("  tool that already existed. Record it, and note that three")
        print("  successive attempts to add machinery were beaten by removing it.")
    elif verdict in ("MET",) and not (c2 and c3):
        print("  The headline effect holds but a committed side condition FAILED.")
        print("  Do not change the default. Diagnose the failing condition first —")
        print("  a policy that wins on the full set while losing on held-out data")
        print("  has been fitted to the designs it was developed against.")
    elif verdict == "UNDERPOWERED":
        print("  Cannot answer. Add seeds; change nothing else.")
    else:
        print("  Keep G-diagnostic. Experiment 9's +8.5% did not survive its own")
        print("  pre-registered re-run, which is exactly why side observations")
        print("  are not permitted to promote anything.")


if __name__ == "__main__":
    main()

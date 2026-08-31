"""Resolve the one undecided comparison — under criteria fixed in advance.

Everything else is frozen. No world model, no extra agents, no new modalities,
no new feature classes. One variable is isolated:

    G-diagnostic    probes on a one-line heuristic ("has this class behaved
                    both ways?"), then acts on a realised-rate estimate
    H-uncertainty   maintains posteriors, samples P(a is best), prices a Value
                    of Diagnosis, probes only when VoD > 0

Both condition on the obligation. Both diagnose. The ONLY difference is whether
the decision machinery is explicit and Bayesian. If H does not beat G, the
machinery is not buying anything and the honest move is to simplify rather than
to keep it because it is elegant.

The rule is committed to `prereg_H_vs_G.json` BEFORE any campaign runs, and the
analysis re-reads it, verifies its hash, and applies exactly that rule. This is
the control the project did not have: every previous defect was caught after a
number was reported, and "run it twenty more times" would eventually have
produced a run where +2.2% looked decisive.

Three outcomes, all informative:

    MET          the machinery earns its complexity — proceed to build on it
    NOT MET      it does not — simplify, and drop the belief layer
    UNDERPOWERED the experiment cannot tell — do NOT read it either way

    python run_prereg.py --mock | --real [--seeds N]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from board import Task
from evaluation import run_campaign, aggregate
from policy import DIAGNOSTIC, UNCERTAINTY, PolicyWorker
from preregistration import Criteria, Preregistration
from voe import VOE
from run_benchmark import (TASKS, FormalRouter, SimRouter, build_features, BUDGET)
from registry import DESIGNS, OWNER

PREREG = os.path.join(HERE, "prereg_H_vs_G.json")
DEFAULT_SEEDS = 12

CRITERIA = Criteria(
    question="Does explicit Bayesian belief + Value-of-Diagnosis outperform a "
             "one-line diagnostic heuristic, holding conditioning and diagnosis "
             "constant?",
    treatment="H-uncertainty",
    control="G-diagnostic",
    min_seeds=DEFAULT_SEEDS,
    min_design_families=3,
    min_effect=0.05,        # 5% relative — below this it is not worth the code
    noise_multiple=2.0,
    max_ci_width=0.10,
)


def build(policy, mock, routers, seed, feats):
    formal, sim = routers
    v = VOE([Task(**vars(t)) for t in TASKS], budget=BUDGET, mock=mock,
            formal=formal, sim=sim)
    w = PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)
    if policy.obligation_conditioned:
        w.attach_features(feats)
    v.workers = [w]
    return v


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    # ---- commit the rule BEFORE any data exists --------------------------- #
    prereg = Preregistration(CRITERIA, PREREG,
                             notes="Frozen: no LWM, no extra agents, no new "
                                   "features. Only the decision machinery differs."
                             ).commit()
    print("=== pre-registered comparison ===")
    print(f"    question : {prereg.criteria.question}")
    print(f"    rule     : {prereg.criteria.rule()}")
    print(f"    committed: {PREREG}  sha256:{prereg.digest}"
          f"  {'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds    : {seeds}   mode = {'MOCK' if mock else 'REAL'}\n")

    families = len({OWNER[t.phi] for t in TASKS})
    feats = build_features()
    routers = (FormalRouter(mock), SimRouter(mock))

    results = []
    for p in (DIAGNOSTIC, UNCERTAINTY):
        for i in range(seeds):
            results.append(run_campaign(build(p, mock, routers, 3000 + i, feats),
                                        "bench", p.name))
        print(f"    {p.name:15s} {seeds} seeds done", flush=True)

    aggs = {a.policy: a for a in aggregate(results)}
    g, h = aggs[DIAGNOSTIC.name], aggs[UNCERTAINTY.name]

    print(f"\n  {'arm':16s} {'mean E':>7s} {'std':>7s} {'worst':>7s} {'probe':>7s}")
    for a in (g, h):
        print(f"  {a.policy:16s} {a.mean_E:7.3f} {a.std_E:7.3f} "
              f"{a.worst_E:7.3f} {a.probe_cost:7.2f}")

    verdict, reasons = prereg.decide(h.mean_E, h.std_E, g.mean_E, g.std_E,
                                     seeds, families)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")

    print("\n=== what each verdict obliges ===")
    if verdict == "MET":
        print("  The belief/VoD layer earns its complexity. It is a justified")
        print("  foundation, and a predictive world model may now be built ON it")
        print("  — the next component would sit on something that has proven")
        print("  itself rather than on an assumption.")
    elif verdict == "NOT MET":
        print("  The machinery is not buying efficiency. The honest response is")
        print("  to SIMPLIFY: keep diagnosis (which is established), drop the")
        print("  posterior layer, and re-argue it only on auditability if that")
        print("  is wanted for its own sake. Do not build a world model on top")
        print("  of a component that failed its own test.")
    else:
        print("  The experiment cannot answer the question. This is NOT a")
        print("  negative result and must not be read as one. Add seeds or")
        print("  design families until it is powered, and change nothing else —")
        print("  re-running until a margin looks decisive is the exact failure")
        print("  the pre-registration exists to prevent.")


if __name__ == "__main__":
    main()

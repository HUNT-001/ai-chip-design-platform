"""Controlled experiment: does uncertainty-aware conditioning fix the regression?

The diverse-regime benchmark produced the finding this experiment exists to
test. Obligation-level conditioning scored WORSE than design-level conditioning
(86.0% vs 91.4% of the realisable ceiling), because a signature covering 8 true
and 3 false obligations was treated as one population and its mean drove the
action choice.

The claim under test is NOT "conditioning was a mistake". It is:

    uncertainty-BLIND conditioning was the mistake.

A finer representation narrows the hypothesis; it does not resolve it. If the
policy acts on the narrowed mean without measuring what remains unresolved, it
has bought false confidence — which is exactly what the numbers showed.

Four arms, same board, same budget, same evidence:

    A  D-human-org     fixed heuristic (the incumbent)
    B  F-obligation    conditioning only, acts on the class mean
    C  H-uncertainty   conditioning + P(a is best) + Value of Diagnosis:
                       probes only when the probe PAYS, not merely when unsure
    D  realisable      best fixed action per class, pre-action information only

Reported alongside efficiency, because the mechanism matters more than the
score:

    wrong commitments   expensive actions that returned no verdict at all
    premature loss      budget lost to them — the cost of specialising early
    probe cost          what diagnosis charged for preventing that

    python run_controlled.py --mock | --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from board import Task
from evaluation import run_campaign, aggregate
from policy import (HUMAN_ORG, ADAPTIVE, OBLIGATION, DIAGNOSTIC, UNCERTAINTY,
                    PolicyWorker)
from voe import VOE
from run_benchmark import (TASKS, FormalRouter, SimRouter, build_features,
                           outcome_table, realisable, retrospective, BUDGET)
from registry import summary

REPEATS = 5
ARMS = [HUMAN_ORG, ADAPTIVE, OBLIGATION, DIAGNOSTIC, UNCERTAINTY]


def build(policy, mock, routers, seed, feats):
    formal, sim = routers
    v = VOE([Task(**vars(t)) for t in TASKS], budget=BUDGET, mock=mock,
            formal=formal, sim=sim)
    w = PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)
    if policy.obligation_conditioned:
        w.attach_features(feats)
    v.workers = [w]
    return v, w


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    print("=== Controlled: is uncertainty-aware conditioning the fix? ===")
    print(f"    {summary()}")
    print(f"    budget = {BUDGET}   mode = {'MOCK' if mock else 'REAL'}\n")

    feats = build_features()
    routers = (FormalRouter(mock), SimRouter(mock))
    rows = outcome_table(routers, feats)
    real_E, *_ = realisable(rows)
    retro_E, *_ = retrospective(rows)

    results, last_worker = [], {}
    for p in ARMS:
        for i in range(REPEATS):
            v, w = build(p, mock, routers, 1000 + i, feats)
            results.append(run_campaign(v, "bench", p.name))
            last_worker[p.name] = w
        print(f"    {p.name:15s} done", flush=True)

    aggs = {a.policy: a for a in aggregate(results)}

    print("\n  arm               E    +/- std   worst   %realisable   wrong  premature  probe")
    for p in ARMS:
        a = aggs[p.name]
        print(f"  {p.name:15s} {a.mean_E:5.3f} +/-{a.std_E:5.3f}  {a.worst_E:5.3f}"
              f"   {a.mean_E/real_E:8.1%}   {a.wrong_commitments:4.1f}  "
              f"{a.premature_loss:8.1f}  {a.probe_cost:5.2f}")
    print("  (std over repeats that now vary the SIMULATION seed as well as the")
    print("   policy seed — a margin smaller than these spreads is not a result)")
    print(f"  {'realisable':15s} {real_E:5.3f}      100.0%")
    print(f"  {'retrospective':15s} {retro_E:5.3f}")

    b, c = aggs[OBLIGATION.name], aggs[UNCERTAINTY.name]
    print("\n=== the regression, and whether it is fixed ===")
    print(f"  B conditioning-only        E={b.mean_E:.3f}  "
          f"({b.mean_E/real_E:.1%} of realisable)")
    print(f"  C + uncertainty/diagnosis  E={c.mean_E:.3f}  "
          f"({c.mean_E/real_E:.1%} of realisable)")
    delta = (c.mean_E - b.mean_E) / b.mean_E if b.mean_E else 0.0
    print(f"  change: {delta:+.1%}")
    print(f"  wrong high-cost commitments: {b.wrong_commitments:.1f} -> "
          f"{c.wrong_commitments:.1f}")
    print(f"  budget lost to premature specialisation: {b.premature_loss:.1f} -> "
          f"{c.premature_loss:.1f}   (diagnosis charged {c.probe_cost:.2f})")

    # what the policy knew when it acted — the auditable part
    w = last_worker.get(UNCERTAINTY.name)
    if w is not None and getattr(w, "decisions", None):
        print("\n=== what H knew when it committed (first few) ===")
        seen = set()
        for phi, info in w.decisions:
            if phi in seen:
                continue
            seen.add(phi)
            print(f"  {phi:18s} best={info['best']:6s} conf={info['confidence']:.2f} "
                  f"ambiguity={info['ambiguity']:.2f} VoD={info['vod']:+.3f}")
            if len(seen) >= 6:
                break
        print("  A policy that reports 'best=formal, confidence=0.55,")
        print("  ambiguity=0.45' is saying something an averaged rate cannot:")
        print("  it knows it does not know, which is what makes diagnosis")
        print("  a decision rather than a habit.")

    # The comparison that actually matters: does the added machinery beat the
    # SIMPLEST alternative that also diagnoses? Comparing H only against the arm
    # it was designed to beat lets sophistication declare victory over a
    # strawman. G probes on a one-line heuristic ("has this class behaved both
    # ways?"); H maintains posteriors, samples P(a is best) and prices a Value
    # of Diagnosis. If H does not beat G, the extra machinery is not earning it.
    g = aggs[DIAGNOSTIC.name]
    print("\n=== does the machinery earn its complexity? ===")
    print(f"  G-diagnostic  (heuristic probing) E={g.mean_E:.3f}  "
          f"probe cost {g.probe_cost:.2f}")
    print(f"  H-uncertainty (posteriors + VoD)  E={c.mean_E:.3f}  "
          f"probe cost {c.probe_cost:.2f}")
    over_g = (c.mean_E - g.mean_E) / g.mean_E if g.mean_E else 0.0
    spread = max(c.std_E, g.std_E)
    h_beats_g = (c.mean_E - g.mean_E) > 2 * spread      # must clear the noise
    print(f"  H over G: {over_g:+.1%}   (largest std among the two: {spread:.3f})")
    if not h_beats_g:
        print("  ** That difference is INSIDE the noise. This comparison already")
        print("     flipped sign once when a single simulation seed changed, so")
        print("     treat it as undecided rather than as a narrow win.")

    # Every claim below must clear the measured spread. A fixed percentage
    # threshold is what let an earlier version of this script announce that the
    # machinery "earns its complexity" in the same breath as declaring the
    # comparison undecided — two sections of one report disagreeing because only
    # one of them consulted the variance.
    b_spread = max(b.std_E, c.std_E)
    diagnosis_helps = (c.mean_E - b.mean_E) > 2 * b_spread

    print("\n=== principle under test ===")
    if diagnosis_helps:
        print("  SUPPORTED (clears the spread): conditioning became useful only")
        print("  once the policy could resolve the ambiguity it exposed.")
        print("  Never increase specialisation without increasing diagnosability.")
    else:
        print("  UNDECIDED: the diagnosing arm's margin over the blind one does")
        print("  not clear the measured spread on this board.")
    print()
    if h_beats_g:
        print("  The posterior/VoD machinery also beats the one-line heuristic,")
        print("  by more than the noise. It earns its complexity here.")
    else:
        print("  The posterior/VoD machinery does NOT clear the noise against a")
        print("  one-line heuristic, and spends more on probes. Its case rests on")
        print("  auditability — reporting confidence and ambiguity per decision —")
        print("  which is a real property but must be argued on its own terms,")
        print("  not smuggled in on an efficiency claim the data does not carry.")


if __name__ == "__main__":
    main()

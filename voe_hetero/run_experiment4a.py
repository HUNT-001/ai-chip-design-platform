"""Experiment 4A — the realisable ceiling, and buying information cheaply.

Experiment 3 measured the learner against a RETROSPECTIVE oracle, which knows
each property's truth value in advance. That is not a valid ceiling for a
pre-action planner: `mul.equiv` is expensive because it is TRUE (needs a proof)
and `mul.bug` is cheap because it is FALSE (needs a counterexample), and which
is which is precisely what a verification campaign exists to discover. Measuring
against an oracle that already knows inflates the gap and credits the policy for
information it could not have had.

So this experiment adds two things.

**A realisable oracle.** It sees exactly what the learner sees before acting:
the obligation signature and the population statistics of that class. It picks
the best FIXED action per signature — it cannot pick per obligation, because
distinguishing members of a class requires the hidden truth. That is the honest
ceiling for a pre-action policy.

**A diagnostic action.** A very short simulation (cost 0.25 against formal's
4.0) whose purpose is not to discharge anything but to find out which action is
worth taking. `G-diagnostic` spends it only where the class has behaved BOTH
ways — where the true/false ambiguity actually bites. This is the behaviour a
senior engineer has and the earlier policies do not: not knowing the answer, but
knowing the cheap experiment that reveals where to spend.

The two questions:
  1. how much of the Experiment-3 gap was ever reducible?
  2. can the agent reduce its own uncertainty before committing real budget?

    python run_experiment4a.py --mock | --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))

from board import Task, ACTION_COST
from evaluation import run_campaign, aggregate, compare_aggregates
from policy import (POLICY_SET_V3, HUMAN_ORG, ADAPTIVE, OBLIGATION, DIAGNOSTIC,
                    PolicyWorker)
from voe import VOE
from run_experiment3 import (TASKS, OWNER, KIND, DESIGNS, FormalRouter, SimRouter,
                             build_features, oracle)

REPEATS = 5


def build(policy, mock, budget, routers, seed):
    formal, sim = routers
    v = VOE([Task(**vars(t)) for t in TASKS], budget=budget, mock=mock,
            formal=formal, sim=sim)
    w = PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)
    if policy.obligation_conditioned:
        w.attach_features(build_features(mock))
    v.workers = [w]
    return v


def outcome_table(routers, feats):
    """What each action ACTUALLY does for every obligation, and its signature.

    Built once by running each channel on each obligation. The retrospective
    oracle may read this row by row; the realisable oracle may only read it
    grouped by signature.
    """
    formal, sim = routers
    rows = {}
    for t in TASKS:
        closes = {}
        if sim.covers(t.phi):
            ev = sim.run(inject_bug=t.inject_bug, seed=7, nvec=20000, phi=t.phi)
            closes["sim"] = (ev.status == "counterexample")
            closes["probe"] = (ev.status == "counterexample")
        ev = formal.prove(t.formal_task)
        closes["formal"] = ev.status in ("proved", "counterexample")
        rows[t.phi] = {"sig": feats[t.phi].signature(), "w": t.weight, "closes": closes}
    return rows


def realisable_oracle(rows):
    """Best FIXED action per signature, chosen from class statistics only.

    This is the ceiling a pre-action planner can actually reach: it knows how
    obligations of this KIND tend to behave, never which side of the class this
    particular one falls on.
    """
    by_sig = {}
    for phi, r in rows.items():
        by_sig.setdefault(r["sig"], []).append(r)
    choice, closed, cost = {}, 0.0, 0.0
    for sig, members in by_sig.items():
        best, best_v = None, -1.0
        for m in ("sim", "formal"):
            w_closed = sum(r["w"] for r in members if r["closes"].get(m))
            c = ACTION_COST[m] * len(members)
            v = w_closed / c if c else 0.0
            if v > best_v:
                best, best_v = m, v
        choice[sig] = best
        closed += sum(r["w"] for r in members if r["closes"].get(best))
        cost += ACTION_COST[best] * len(members)
    return (closed / cost if cost else 0.0), closed, cost, choice


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    budget = 60.0
    print("=== Experiment 4A: realisable ceiling + diagnostic actions ===")
    print(f"    budget = {budget}   mode = {'MOCK' if mock else 'REAL'}\n")

    routers = (FormalRouter(mock), SimRouter(mock))
    feats = build_features(mock)
    rows = outcome_table(routers, feats)

    retro_E, r_closed, r_cost, r_plan = oracle(routers, mock)
    real_E, x_closed, x_cost, x_choice = realisable_oracle(rows)

    print("=== what each obligation actually admits ===")
    for phi, r in rows.items():
        c = ", ".join(f"{m}:{'closes' if v else '-'}" for m, v in r["closes"].items()
                      if m != "probe")
        print(f"    {phi:16s} sig={str(r['sig']):34s} {c}")

    print("\n=== two ceilings ===")
    print(f"  retrospective oracle  E={retro_E:.3f}  (knows each truth value in advance)")
    for phi, m in r_plan.items():
        print(f"      {phi:16s} -> {m}")
    print(f"  REALISABLE oracle     E={real_E:.3f}  (class statistics only)")
    for sig, m in x_choice.items():
        print(f"      {str(sig):34s} -> {m}")
    print(f"  the difference is the value of information nobody has yet: "
          f"{retro_E - real_E:.3f}")

    results = []
    for p in POLICY_SET_V3:
        for i in range(REPEATS):
            results.append(run_campaign(build(p, mock, budget, routers, 1000 + i),
                                        "hetero", p.name))
        print(f"    {p.name:14s} done", flush=True)

    aggs = aggregate(results)
    print()
    print(compare_aggregates(aggs, incumbent=HUMAN_ORG.name))

    by = {a.policy: a for a in aggs}
    print("\n=== measured against the ceiling that is actually reachable ===")
    for name in (HUMAN_ORG.name, ADAPTIVE.name, OBLIGATION.name, DIAGNOSTIC.name):
        a = by[name]
        print(f"  {name:14s} E={a.mean_E:.3f}   "
              f"{a.mean_E/real_E:6.1%} of realisable   "
              f"{a.mean_E/retro_E:6.1%} of retrospective")
    print(f"  {'realisable':14s} E={real_E:.3f}")
    print(f"  {'retrospective':14s} E={retro_E:.3f}")

    d, o = by[DIAGNOSTIC.name], by[OBLIGATION.name]
    print("\n=== did buying information pay? ===")
    if d.mean_E > o.mean_E * 1.02:
        print(f"  Yes: {d.mean_E:.3f} vs {o.mean_E:.3f}. Spending 0.25 to learn which")
        print("  action is worth 4.0 beat committing blind — the agent reduced its")
        print("  own uncertainty before spending, rather than guessing well.")
    elif d.mean_E < o.mean_E * 0.98:
        print(f"  No: {d.mean_E:.3f} vs {o.mean_E:.3f}. The probe cost more than the")
        print("  information was worth on this board. That is a real result about")
        print("  WHEN diagnosis pays, not evidence that diagnosis is useless.")
    else:
        print(f"  Neutral: {d.mean_E:.3f} vs {o.mean_E:.3f} — the probe roughly paid")
        print("  for itself. On a board with more ambiguous classes it should win;")
        print("  here only one class is genuinely in doubt.")


if __name__ == "__main__":
    main()

"""Does the win REPLICATE?

`E-adaptive` beat the hand-designed organisation by 54% on one held-out design.
One design cannot distinguish "this policy is better" from "this policy happened
to suit that block". So the same five policies now face two unseen designs:

    lfsr_8bit   8-way cache-replacement LFSR   3 obligations
    mv_filter   majority-vote filter           2 obligations

Reported per design AND pooled, because those answer different questions:

    per design  did the advantage appear on BOTH, or only where it was found?
    pooled      what is the overall effect, weighting designs equally?

A policy that wins on one design and loses on the other has not improved
anything — it has fitted. The promotion rule is therefore applied per design and
only accepted if it holds on every one.

    python run_multidesign_experiment.py --mock
    python run_multidesign_experiment.py --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from board import Task
from evidence_channels import FormalChannel, SimChannel
from evaluation import (run_campaign, aggregate, compare_aggregates,
                        promote_aggregate)
from policy import POLICY_SET, HUMAN_ORG, PolicyWorker
from voe import VOE

REPEATS = 7
DEV_DESIGNS = ["ibex_alu"]

RTL, FORMAL, SIM = (os.path.join(HERE, d) for d in ("rtl", "formal", "sim"))

# ---- design 1: the LFSR ---------------------------------------------------- #
LFSR = {
    "name": "lfsr_8bit",
    "sby": os.path.join(FORMAL, "lfsr.sby"),
    "control": "bug_onehot",
    "top": "tb_lfsr",
    "src": [os.path.join(RTL, f) for f in
            ("lfsr_8bit.sv", "lfsr_8bit_mut.sv", "lfsr_wrap.sv", "lfsr_wrap_mut.sv")]
           + [os.path.join(SIM, "tb_lfsr.sv")],
    "wrap": ("lfsr_wrap", "lfsr_wrap_mut"),
    "covers": r"lfsr\.(onehot|consistent)$",
    "tasks": [
        Task("lfsr.onehot", 7.0, formal_task="prove_onehot"),
        Task("lfsr.consistent", 5.0, formal_task="prove_consistent"),
        Task("lfsr.stable", 4.0, formal_task="prove_stable"),
    ],
}

# ---- design 2: the majority-vote filter ------------------------------------ #
MVF = {
    "name": "mv_filter",
    "sby": os.path.join(FORMAL, "mvf.sby"),
    "control": "bug_sticky",
    "top": "tb_mvf",
    "src": [os.path.join(RTL, f) for f in
            ("mv_filter.sv", "mv_filter_mut.sv", "mvf_wrap.sv", "mvf_wrap_mut.sv")]
           + [os.path.join(SIM, "tb_mvf.sv")],
    "wrap": ("mvf_wrap", "mvf_wrap_mut"),
    "covers": r"mvf\.(sticky|clear)$",
    "tasks": [
        Task("mvf.sticky", 6.0, formal_task="prove_sticky"),
        Task("mvf.clear", 4.0, formal_task="prove_clear"),
    ],
}

DESIGNS = [LFSR, MVF]


def make_channels(d, mock):
    """One channel pair per design, shared across all campaigns on it."""
    good, mut = d["wrap"]
    formal = FormalChannel(sby_file=d["sby"], mock=mock, combinational=False,
                           negative_control=d["control"])
    sim = SimChannel(mock=mock, sources=d["src"], top=d["top"],
                     defines_for=lambda bug, g=good, m=mut: [f"DUT={m if bug else g}"],
                     covers=d["covers"])
    return formal, sim


def build(d, policy, mock, budget, channels, seed):
    formal, sim = channels
    v = VOE([Task(**vars(t)) for t in d["tasks"]], budget=budget, mock=mock,
            formal=formal, sim=sim)
    v.workers = [PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)]
    return v


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    budget = 40.0
    print("=== Does the win replicate? Two held-out designs ===")
    print(f"    developed on : {DEV_DESIGNS}")
    print(f"    evaluated on : {[d['name'] for d in DESIGNS]}")
    print(f"    {REPEATS} campaigns per policy per design   "
          f"mode = {'MOCK' if mock else 'REAL'}\n")

    per_design, verdicts = {}, {}
    for d in DESIGNS:
        ch = make_channels(d, mock)
        results = []
        for p in POLICY_SET:
            for i in range(REPEATS):
                results.append(run_campaign(
                    build(d, p, mock, budget, ch, seed=1000 + i), d["name"], p.name))
        aggs = aggregate(results)
        per_design[d["name"]] = {a.policy: a for a in aggs}
        print(f"--- {d['name']} ---")
        print(compare_aggregates(aggs, incumbent=HUMAN_ORG.name))
        inc = per_design[d["name"]][HUMAN_ORG.name]
        best = max(aggs, key=lambda a: a.mean_E)
        v = promote_aggregate(best, inc, DEV_DESIGNS, min_runs=5)
        verdicts[d["name"]] = (best.policy, v)
        print(f"  best: {best.policy}  ->  {v.reason}\n")

    # ---- pooled ------------------------------------------------------------ #
    print("=== pooled across designs (equal weight per design) ===")
    names = [p.name for p in POLICY_SET]
    pooled = {n: sum(per_design[d["name"]][n].mean_E for d in DESIGNS) / len(DESIGNS)
              for n in names}
    for n in sorted(names, key=lambda n: -pooled[n]):
        mark = "  <- incumbent" if n == HUMAN_ORG.name else ""
        per = "  ".join(f"{d['name']}={per_design[d['name']][n].mean_E:.3f}"
                        for d in DESIGNS)
        print(f"  {n:14s} pooled E={pooled[n]:6.3f}   {per}{mark}")

    # ---- the replication test ---------------------------------------------- #
    print("\n=== replication verdict ===")
    winners = {name: w for name, (w, v) in verdicts.items()}
    accepted = {name: v.accepted for name, (w, v) in verdicts.items()}
    print(f"  winner per design : {winners}")
    if len(set(winners.values())) == 1 and all(accepted.values()):
        w = next(iter(winners.values()))
        print(f"  REPLICATED: '{w}' beat the incumbent on every held-out design.")
        print("  Two designs is a replication, still not a generalisation claim.")
    else:
        print("  NOT REPLICATED: the advantage did not hold on every design.")
        print("  A policy that wins on one block and not another has fitted that")
        print("  block, not improved verification. Nothing is promoted.")


if __name__ == "__main__":
    main()

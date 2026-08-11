"""The first honest promotion decision.

Every policy in the comparison set was developed against `ibex_alu`. This runs
them on a design none of them has seen — the real pulp `lfsr_8bit`, an 8-way
cache-replacement LFSR — so `promote()` can finally do something other than
refuse.

The decision is now genuinely two-sided:

    the candidate wins on a design it was never tuned against  -> PROMOTED
    it does not                                                -> REJECTED

Either outcome is informative. A rejection here is worth more than a victory on
`ibex_alu`, because it would mean the earlier margin was fitting rather than
skill — precisely the thing the held-out rule exists to detect.

    python run_heldout_experiment.py --mock
    python run_heldout_experiment.py --real
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

REPEATS = 7          # campaigns per policy, so variance is observable

LFSR_SBY = os.path.join(HERE, "formal", "lfsr.sby")
LFSR_SRC = [os.path.join(HERE, "rtl", "lfsr_8bit.sv"),
            os.path.join(HERE, "rtl", "lfsr_8bit_mut.sv"),
            os.path.join(HERE, "rtl", "lfsr_wrap.sv"),
            os.path.join(HERE, "rtl", "lfsr_wrap_mut.sv"),
            os.path.join(HERE, "sim", "tb_lfsr.sv")]

DEV_DESIGNS = ["ibex_alu"]          # what every policy was developed against
DESIGN = "lfsr_8bit"                # never seen by any policy

# formal_task must be the EXACT sby task name. (Mock accepts anything, so a
# mismatch here passes in --mock and fails only against real tools — which is
# precisely what happened: 'onehot' vs the real task 'prove_onehot'.)
TASKS = [
    Task("lfsr.onehot", 7.0, formal_task="prove_onehot",
         note="way-select must always be one-hot (two ways = corrupt refill)"),
    Task("lfsr.consistent", 5.0, formal_task="prove_consistent",
         note="one-hot and binary encodings of the selection must agree"),
    Task("lfsr.stable", 4.0, formal_task="prove_stable",
         note="with the enable low the LFSR must not advance"),
]


def make_channels(mock):
    """ONE set of channels for the whole experiment.

    Tool results are deterministic, so re-creating channels per campaign meant
    Verilator rebuilt the testbench and sby re-proved everything 35 times over —
    minutes of identical work. Sharing them (with the memoisation inside each
    channel) makes a repeated experiment tractable without changing a single
    verdict: the same task returns the same evidence and the same witness.
    """
    formal = FormalChannel(sby_file=LFSR_SBY, mock=mock, combinational=False,
                           negative_control="bug_onehot")
    # tb_lfsr checks the one-hot invariant and the oh/bin agreement, so those
    # two obligations have a real simulation option. It does NOT exercise the
    # enable-stability property, so that scope is excluded rather than letting a
    # pass be miscredited to a property the TB never examines.
    sim = SimChannel(mock=mock, sources=LFSR_SRC, top="tb_lfsr",
                     defines_for=lambda bug: ["DUT=lfsr_wrap_mut"] if bug else ["DUT=lfsr_wrap"],
                     covers=r"lfsr\.(onehot|consistent)$")
    return formal, sim


def build(policy, mock, budget, channels, seed=2026):
    formal, sim = channels
    v = VOE([Task(**vars(t)) for t in TASKS], budget=budget, mock=mock,
            formal=formal, sim=sim)
    v.workers = [PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)]
    return v


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    budget = 40.0
    print("=== HELD-OUT evaluation: policies meet an unseen design ===")
    print(f"    developed on : {DEV_DESIGNS}")
    print(f"    evaluated on : {DESIGN}  (real pulp lfsr_8bit, 8-way LFSR)")
    print(f"    budget = {budget}   mode = {'MOCK' if mock else 'REAL (sby + z3)'}\n")

    channels = make_channels(mock)
    print("  warming the channels (each tool result is computed once)...", flush=True)
    results = []
    for p in POLICY_SET:
        for i in range(REPEATS):
            results.append(run_campaign(build(p, mock, budget, channels, seed=1000 + i),
                                        DESIGN, p.name))
        print(f"    {p.name:14s} done ({REPEATS} campaigns)", flush=True)
    aggs = aggregate(results)
    print(f"  {REPEATS} campaigns per policy\n")
    print(compare_aggregates(aggs, incumbent=HUMAN_ORG.name))

    by = {a.policy: a for a in aggs}
    inc = by[HUMAN_ORG.name]
    best = max(aggs, key=lambda a: a.mean_E)

    print("\n=== promotion decision ===")
    v = promote_aggregate(best, inc, DEV_DESIGNS, min_runs=5)
    print(f"  candidate : {best.policy}  mean E={best.mean_E:.3f} "
          f"(worst {best.worst_E:.3f})")
    print(f"  incumbent : {inc.policy}  mean E={inc.mean_E:.3f}")
    print(f"  {v.reason}")

    print("\n=== reading this honestly ===")
    print("  Repeats separate a lucky campaign from a real effect — the")
    print("  single-run version of this experiment promoted a RANDOM ordering")
    print("  policy on one fortunate sample. Still open: one held-out design")
    print("  cannot show generalisation, and the accept rule (worst run must")
    print("  beat the incumbent's mean) is a stated convention, not a")
    print("  statistical protocol.")


if __name__ == "__main__":
    main()

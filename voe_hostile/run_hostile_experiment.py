"""Experiment 2: does the adaptive policy re-allocate when the regime flips?

Every board so far rewarded going straight to proof. Formal was cheap and
decisive; simulation raised effective-sample counts and settled nothing. The
adaptive policy learned exactly that, and beat the hand-designed organisation by
54-78% on two unseen designs.

That learning is only valuable if it is CONTINGENT. This board inverts the
economics:

    mul.equiv   32x32 behavioural multiply vs a shift-and-add reference.
                TRUE, and out of reach for the solver inside its time budget.
                Formal here costs 4 units and returns NOTHING.

    mul.bug     the mutated multiplier corrupts the product when srcB[3:0]==0xF.
                Roughly 1 random vector in 16 hits it, so simulation closes this
                obligation by counterexample almost immediately, for 1 unit.

The prediction under test:

    a policy that always reaches for proof should burn its budget and close
    little; a policy that samples should close the bug cheaply; and the ADAPTIVE
    policy should notice that formal has stopped paying and shift toward
    simulation.

If E-adaptive does NOT shift, it did not learn a strategy — it learned a habit
that happened to suit the designs it was measured on. That failure is the result
this experiment is built to expose, and it would matter more than the original
54-78% did.

    python run_hostile_experiment.py --mock
    python run_hostile_experiment.py --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from board import Task
from evidence_channels import FormalChannel, SimChannel
from evaluation import run_campaign, aggregate, compare_aggregates
from policy import POLICY_SET, HUMAN_ORG, ADAPTIVE, PolicyWorker
from voe import VOE

REPEATS = 7
MUL_SBY = os.path.join(HERE, "formal", "mul.sby")
MUL_SRC = [os.path.join(HERE, "rtl", "mul_dut.sv"),
           os.path.join(HERE, "sim", "tb_mul.sv")]

TASKS = [
    Task("mul.equiv", 6.0, formal_task="prove_equiv", inject_bug=False,
         note="behavioural multiply == shift-add reference (out of solver reach)"),
    Task("mul.bug", 6.0, formal_task="bug_equiv", inject_bug=True,
         note="mutated multiplier; dense defect, cheap to find by sampling"),
]


def make_channels(mock):
    # mock_timeouts models the hostile regime in --mock. Without it mock formal
    # answers instantly, the board is not hostile at all, and the experiment
    # quietly measures nothing. Only --real tests whether the solver truly
    # exceeds its budget on a 32x32 multiply.
    formal = FormalChannel(sby_file=MUL_SBY, mock=mock, combinational=True,
                           negative_control="bug_equiv",
                           mock_timeouts=("prove_equiv",))
    # mock_finds_bug: the defect here is DENSE (1 vector in 16), unlike the toy
    # board's 1-in-2^32 bug that mock models as unreachable. Declaring it lets
    # --mock represent the regime; --real just runs the testbench.
    sim = SimChannel(mock=mock, sources=MUL_SRC, top="tb_mul",
                     defines_for=lambda bug: ["DUT=mul_wrap_mut"] if bug else ["DUT=mul_wrap"],
                     covers=r"mul\.", mock_finds_bug=True)
    return formal, sim


def build(policy, mock, budget, channels, seed):
    formal, sim = channels
    v = VOE([Task(**vars(t)) for t in TASKS], budget=budget, mock=mock,
            formal=formal, sim=sim)
    v.workers = [PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)]
    return v


def channel_mix(voe):
    """How the budget was actually split — the quantity this experiment is about."""
    spend = {}
    for e in voe.action_log:
        spend[e["method"]] = spend.get(e["method"], 0.0) + e["cost"]
    total = sum(spend.values()) or 1.0
    return {m: c / total for m, c in spend.items()}


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    budget = 40.0
    print("=== Experiment 2: a FORMAL-HOSTILE regime ===")
    print("    formal is expensive and returns no verdict; simulation finds a")
    print("    dense defect cheaply. Does the adaptive policy notice?")
    print(f"    budget = {budget}   mode = {'MOCK' if mock else 'REAL (sby + verilator)'}\n")

    channels = make_channels(mock)
    results, mixes = [], {}
    for p in POLICY_SET:
        for i in range(REPEATS):
            v = build(p, mock, budget, channels, seed=1000 + i)
            results.append(run_campaign(v, "multiplier", p.name))
            if i == 0:
                mixes[p.name] = channel_mix(v)
        print(f"    {p.name:14s} done", flush=True)

    aggs = aggregate(results)
    print()
    print(compare_aggregates(aggs, incumbent=HUMAN_ORG.name))

    print("\n=== where each policy actually spent its budget ===")
    for name, mix in mixes.items():
        parts = "  ".join(f"{m}={f:.0%}" for m, f in sorted(mix.items()))
        print(f"  {name:14s} {parts or '(nothing spent)'}")

    print("\n=== the question this board asks ===")
    ad = mixes.get(ADAPTIVE.name, {})
    sim_share = ad.get("sim", 0.0)
    inc_share = mixes.get(HUMAN_ORG.name, {}).get("sim", 0.0)
    print(f"  E-adaptive simulation share here : {sim_share:.0%}")
    print(f"  incumbent  simulation share here : {inc_share:.0%}")
    print("  On the formal-friendly boards E-adaptive went formal-first almost")
    print("  exclusively, so the comparison that settles contingency is that")
    print("  share versus this one — not an absolute threshold.")
    print()
    print("  Read it strictly: a higher simulation share here than on the")
    print("  friendly boards shows the allocation TRACKS the regime. It does")
    print("  not show the allocation is optimal, and a policy can still win on")
    print("  E while mis-allocating, because this board has only two")
    print("  obligations and one of them nothing can close.")


if __name__ == "__main__":
    main()

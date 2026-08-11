"""The key experiment: can the organisation outperform its own baseline?

Five strategies, identical DUT, identical budget, identical evidence channels.
Only the ALLOCATION POLICY differs — which obligation to attack next, and which
channel to spend on it. Nothing here can affect what is *true*: judgments still
require witnesses and the vacuity gate still refuses uncertifiable proofs, so a
policy can only win by spending better, never by claiming more.

    A  random          pick anything, any method
    B  cheapest-first  lowest-weight obligations, simulation-heavy
    C  single engineer utility-ordered generalist (the Phase-3 behaviour)
    D  human org       the hand-designed archetypes (the incumbent to beat)
    E  adaptive        re-weights channel choice from realised risk-per-cost

Primary measure  E = dR / cost  — weighted risk discharged per unit spent.

    python run_policy_experiment.py --mock
    python run_policy_experiment.py --real

**What this does and does not establish.** It compares allocation strategies on
ONE design. That is a within-design result, not evidence of generalisation, and
`promote()` refuses to certify any policy on a design it was developed against.
Turning this into a real claim needs held-out designs — the corpus has 1795
files and two are verified, so this is rung 1 of the ladder, honestly labelled.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voe"))
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from board import Task
from evidence_channels import FormalChannel, SimChannel
from evaluation import run_campaign, compare, promote
from policy import POLICY_SET, HUMAN_ORG, PolicyWorker
from voe import VOE
from run_voe_ibex import TASKS, IBEX_SBY, IBEX_SRC

DESIGN = "ibex_alu"
DEV_DESIGNS = ["ibex_alu"]          # what the policies were developed against


def build(policy, mock, budget):
    formal = FormalChannel(sby_file=IBEX_SBY, mock=mock, combinational=True,
                           negative_control="bug_logic")
    sim = SimChannel(mock=mock, sources=IBEX_SRC, top="tb_ibex_alu",
                     defines_for=lambda bug: ["DUT=ibex_alu_mut"] if bug else ["DUT=ibex_alu"])
    tasks = [Task(**vars(t)) for t in TASKS]
    v = VOE(tasks, budget=budget, mock=mock, formal=formal, sim=sim)
    v.workers = [PolicyWorker(policy.name, policy, v.k, formal, sim)]
    return v


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    budget = 40.0
    print("=== Can the organisation outperform its own baseline? ===")
    print(f"    design = {DESIGN}   budget = {budget}   "
          f"mode = {'MOCK' if mock else 'REAL (verilator + sby)'}\n")
    for p in POLICY_SET:
        print(f"    {p.name:14s} {p.describe()}")
    print()

    results = []
    for p in POLICY_SET:
        results.append(run_campaign(build(p, mock, budget), DESIGN, p.name))

    print(compare(results, incumbent=HUMAN_ORG.name))

    by = {r.policy: r for r in results}
    inc = by[HUMAN_ORG.name]
    best = max(results, key=lambda r: r.efficiency)

    print(f"\n  incumbent : {inc.policy}  E={inc.efficiency:.3f}")
    print(f"  best      : {best.policy}  E={best.efficiency:.3f}")

    print("\n=== promotion decision (the falsifiability gate) ===")
    v = promote(best, inc, DEV_DESIGNS)
    print(f"  {v.reason}")
    if not v.accepted:
        print("  Nothing enters organisational memory. An improvement measured")
        print("  only on the design it was tuned against is not an improvement;")
        print("  it is the organisational form of a vacuous proof.")

    print("\n=== what a real claim would require ===")
    print("  * held-out designs the policy was never developed against")
    print("  * repeats, to separate a real effect from campaign variance")
    print("  * an agreed minimum effect size and significance protocol")
    print("  Those are open evaluation-design questions, not settled ones.")


if __name__ == "__main__":
    main()

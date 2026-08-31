"""Experiment 7 — coupled obligations: what happens when decisions stop being independent?

The second revisit condition from the rejected-capability record. Every board so
far treated obligations as separable: an action aimed at one property produced
evidence about that property alone, so per-obligation planning was sound.

Real SoCs are not like that. A shared bus, a protocol assumption, a common
interface — exercising it produces evidence bearing on many properties at once,
from the same run and the same witness:

    axi.protocol_ok  --- one simulation of the shared interface also evidences
                         8 dependent properties, each worth 6

This is DIFFERENT from Experiment 6's horizon effect. There, value was
sequential: prove a lemma, then dependents become provable. Here it is
simultaneous: a single action moves many obligations at once. The optimisation
problem changes shape —

    pi(a_i | Omega_i)        per-obligation, what the current policy does
    pi(a   | Omega_1..n)     coupled, what this board rewards

A per-obligation planner values the shared action by its effect on ONE property
and systematically under-buys it.

    G-diagnostic  the current default: ranks by per-obligation utility
    J-coupled     identical except it values an action by the weight of EVERY
                  obligation that action evidences

The coupling is DECLARED, as a shared-interface contract is declared by an
engineer — so a planner may use it. The extra judgments cite the same witness
and pass through the bus like any other, so nothing bypasses adjudication.

    python run_coupled.py --mock | --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))

from board import Task
from evidence_channels import FormalChannel, SimChannel, Evidence
from evaluation import run_campaign, aggregate
from obligation_state import ObligationFeatures
from policy import DIAGNOSTIC, COUPLED, PolicyWorker
from voe import VOE

SEEDS = 8
BUDGET = 40.0
FIFO_SBY = os.path.join(HERE, "..", "voe_fifo", "formal", "fifo_prove.sby")

SHARED_USERS = [f"iface.p{i}" for i in range(1, 9)]      # 8 x weight 6 = 48
INDEPENDENT = [f"local.q{i}" for i in range(1, 9)]       # 8 x weight 5 = 40

TASKS = [
    # the shared-infrastructure obligation: modest on its own, evidences 8 others
    Task("axi.protocol_ok", 2.0, formal_task="prove_cnt",
         shares_evidence_with=tuple(SHARED_USERS),
         note="SHARED: one run of the common interface evidences 8 properties"),
] + [
    Task(phi, 6.0, formal_task="dep_ok",
         note="uses the shared interface; also closable on its own, expensively")
    for phi in SHARED_USERS
] + [
    Task(phi, 5.0, formal_task="local_ok",
         note="independent; individually more attractive than the shared action")
    for phi in INDEPENDENT
]


class CoupledFormal:
    """Real proof for the shared obligation; modelled discharge for the rest.

    `axi.protocol_ok` is discharged by an actual `sby` run. The others are
    modelled as provable, so what this experiment measures is the PLANNER's
    ability to value coupling — not tool behaviour. Stated plainly because the
    distinction has mattered before.
    """

    def __init__(self, mock):
        self.real = FormalChannel(sby_file=FIFO_SBY, mock=mock,
                                  combinational=False, negative_control="bug_cnt")

    def prove(self, task):
        if task == "prove_cnt":
            return self.real.prove(task)
        return Evidence("formal", "proved", witness=f"<modelled>/{task}/PASS",
                        detail="modelled: independently provable")

    def gate_status(self):
        return self.real.gate_status()


def build(policy, mock, seed):
    formal = CoupledFormal(mock)
    sim = SimChannel(mock=mock, covers=lambda phi: False)
    v = VOE([Task(**vars(t)) for t in TASKS], budget=BUDGET, mock=mock,
            formal=formal, sim=sim)
    w = PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)
    if policy.obligation_conditioned:
        w.attach_features({t.phi: ObligationFeatures("invariant", False, True)
                           for t in TASKS})
    v.workers = [w]
    return v


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    shared = next(t for t in TASKS if t.shares_evidence_with)
    coupled_w = sum(t.weight for t in TASKS if t.phi in shared.shares_evidence_with)
    indep_w = sum(t.weight for t in TASKS
                  if not t.shares_evidence_with and t.phi not in shared.shares_evidence_with)
    print("=== Experiment 7: coupled obligations ===")
    print("    second revisit condition from the rejected-capability record")
    print(f"    shared action weight {shared.weight:g} also evidences "
          f"{len(shared.shares_evidence_with)} obligations worth {coupled_w:g}")
    print(f"    {len(INDEPENDENT)} independent obligations worth {indep_w:g} "
          f"compete for the same budget")
    print(f"    budget {BUDGET:g}   seeds {SEEDS}   mode = {'MOCK' if mock else 'REAL'}\n")

    results = []
    for p in (DIAGNOSTIC, COUPLED):
        for i in range(SEEDS):
            results.append(run_campaign(build(p, mock, 5000 + i), "coupled", p.name))
        print(f"    {p.name:14s} done", flush=True)

    aggs = {a.policy: a for a in aggregate(results)}
    g, c = aggs[DIAGNOSTIC.name], aggs[COUPLED.name]

    print(f"\n  {'arm':14s} {'mean E':>7s} {'std':>7s} {'closed':>7s} {'cost':>7s}")
    for a in (g, c):
        r0 = a.runs[0]
        print(f"  {a.policy:14s} {a.mean_E:7.3f} {a.std_E:7.3f} "
              f"{r0.discharged:7.1f} {r0.cost:7.1f}")

    spread = max(g.std_E, c.std_E)
    gain = (c.mean_E - g.mean_E) / g.mean_E if g.mean_E else 0.0
    print(f"\n  coupling-aware over per-obligation: {gain:+.1%}  "
          f"(spread {spread:.3f})")

    print("\n=== does coupling break the per-obligation heuristic? ===")
    if c.mean_E - g.mean_E > 2 * spread and gain > 0.05:
        print("  YES. When one action evidences many obligations, valuing it by")
        print("  its effect on a single property systematically under-buys it.")
        print("  This is the SECOND regime found where the current default")
        print("  fails materially — and unlike the horizon case, the failure is")
        print("  simultaneous rather than sequential, so lookahead does not fix")
        print("  it. The decision problem is pi(a | Omega_1..n), not per-Omega_i.")
    else:
        print("  NO. The per-obligation policy handles this board. Either the")
        print("  shared action was attractive enough on its own weight, or the")
        print("  independent obligations did not compete hard enough for budget.")
        print("  Sharpen the board before concluding that coupling is benign.")


if __name__ == "__main__":
    main()

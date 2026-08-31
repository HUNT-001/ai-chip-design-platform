"""Experiment 6 — the horizon stress test: where does the simple heuristic break?

`G-diagnostic` won the pre-registered comparison, so the default policy is now
the simple one. The institutional record of that rejection lists five conditions
under which the more sophisticated machinery should be reconsidered. This tests
the first: **LONG HORIZON** — the best first action has low immediate value but
unlocks most of the board.

The structure is assume-guarantee, which is how real verification decomposes: a
LEMMA is proved once, and dependent properties may then assume it. Until the
lemma closes, its dependents cannot be discharged at all — their proofs would
have to assume something nobody has established.

    fifo.cnt_bound   the lemma. Weight 3 — modest in itself.
                     Unlocks five dependents worth 30 in total.
    subsystem.*      each weight 6, and unprovable until the lemma closes.

A one-step expected-value rule ranks by `w * P(close) / cost` and therefore sees
only the 3. A planner that reads the declared dependency structure sees the 30
behind it.

**What this experiment is and is not.** The dependency structure is DECLARED, as
an assume-guarantee contract is declared by an engineer — so a planner is
entitled to use it, and the question "can a one-step rule value delayed
opportunity?" is a question about the PLANNER. The lemma itself is discharged by
a real `sby` proof. But the dependents' unlocking is modelled, not measured, so
this is a planner experiment on a partly modelled environment, and the numbers
are not a claim about tool behaviour.

    python run_horizon.py --mock | --real
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))

from board import Task, ACTION_COST
from evidence_channels import FormalChannel, SimChannel, Evidence
from evaluation import run_campaign, aggregate
from obligation_state import ObligationFeatures
from policy import DIAGNOSTIC, LOOKAHEAD, PolicyWorker
from voe import VOE

SEEDS = 8
BUDGET = 40.0          # tight: not everything can be done
FIFO_SBY = os.path.join(HERE, "..", "voe_fifo", "formal", "fifo_prove.sby")
DEPENDENTS = [f"subsystem.p{i}" for i in range(1, 9)]
DISTRACTORS = [f"local.q{i}" for i in range(1, 9)]

# The first version of this board blocked EVERYTHING except the lemma, so the
# greedy policy proved it by elimination — it had nothing else to buy — and the
# experiment measured nothing. A horizon test needs the greedy policy to have
# attractive alternatives that keep it away from the lemma.
TASKS = [
    # the lemma: low weight of its own, unlocks 48
    Task("fifo.cnt_bound", 1.0, formal_task="prove_cnt",
         enables=tuple(DEPENDENTS),
         note="LEMMA: occupancy never exceeds DEPTH. Weight 1 by itself."),
] + [
    Task(phi, 6.0, formal_task="dep_ok", requires=("fifo.cnt_bound",),
         note="assumes the lemma; undischargeable until it closes")
    for phi in DEPENDENTS
] + [
    # immediately closable and individually more attractive than the lemma,
    # so a one-step rule spends here first and may never reach the lemma
    Task(phi, 5.0, formal_task="local_ok",
         note="closable now; weight 5 outranks the lemma's 1 under a one-step rule")
    for phi in DISTRACTORS
]


class LemmaGatedFormal:
    """Real proof for the lemma; modelled discharge for what it unlocks.

    The lemma is proved by an actual `sby` run. The dependents are modelled:
    once the lemma is closed they prove cheaply, which is what assuming an
    established invariant buys you. That modelling is the honest boundary of
    this experiment — see the module docstring.
    """

    def __init__(self, mock):
        self.real = FormalChannel(sby_file=FIFO_SBY, mock=mock,
                                  combinational=False, negative_control="bug_cnt")
        self.lemma_closed = False

    def prove(self, task):
        if task == "prove_cnt":
            ev = self.real.prove(task)
            if ev.status in ("proved", "counterexample"):
                self.lemma_closed = True
            return ev
        if task == "local_ok":
            return Evidence("formal", "proved", witness="<modelled>/local/PASS",
                            detail="independent local property")
        if not self.lemma_closed:
            return Evidence("formal", "timeout", witness="",
                            detail="dependent property: the lemma it assumes is "
                                   "not established, so nothing can discharge it")
        return Evidence("formal", "proved",
                        witness="<modelled>/dependent/PASS",
                        detail="discharged by assuming the established lemma")

    def gate_status(self):
        return self.real.gate_status()


def build(policy, mock, seed):
    formal = LemmaGatedFormal(mock)
    sim = SimChannel(mock=mock, covers=lambda phi: False)   # formal-only board
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
    print("=== Experiment 6: horizon stress test ===")
    print("    the first revisit condition from the rejected-capability record")
    # derived from the board, never restated by hand — an earlier version
    # printed a weight the board no longer had, which is the smallest possible
    # version of a report disagreeing with its own data
    lemma = next(t for t in TASKS if t.enables)
    unlocked = sum(t.weight for t in TASKS if t.phi in lemma.enables)
    distract = sum(t.weight for t in TASKS
                   if not t.enables and not t.requires)
    print(f"    lemma weight {lemma.weight:g} unlocks {len(lemma.enables)} "
          f"dependents worth {unlocked:g}; distractors worth {distract:g} "
          f"are closable now")
    print(f"    budget {BUDGET}   seeds {SEEDS}   "
          f"mode = {'MOCK' if mock else 'REAL (sby for the lemma)'}\n")

    results = []
    for p in (DIAGNOSTIC, LOOKAHEAD):
        for i in range(SEEDS):
            results.append(run_campaign(build(p, mock, 4000 + i), "horizon", p.name))
        print(f"    {p.name:14s} done", flush=True)

    aggs = {a.policy: a for a in aggregate(results)}
    g, l = aggs[DIAGNOSTIC.name], aggs[LOOKAHEAD.name]

    print(f"\n  {'arm':14s} {'mean E':>7s} {'std':>7s} {'closed':>7s} {'cost':>7s}")
    for a in (g, l):
        r0 = a.runs[0]
        print(f"  {a.policy:14s} {a.mean_E:7.3f} {a.std_E:7.3f} "
              f"{r0.discharged:7.1f} {r0.cost:7.1f}")

    spread = max(g.std_E, l.std_E)
    gain = (l.mean_E - g.mean_E) / g.mean_E if g.mean_E else 0.0
    print(f"\n  lookahead over one-step: {gain:+.1%}  (spread {spread:.3f})")

    print("\n=== does the horizon condition actually break the heuristic? ===")
    if l.mean_E - g.mean_E > 2 * spread and gain > 0.05:
        print("  YES. The one-step rule cannot value an action whose payoff is")
        print("  delayed, and this is the first regime found where the simple")
        print("  policy fails materially. That is a DERIVED justification for")
        print("  multi-step planning — the failure came first, the mechanism")
        print("  second. Re-open the rejected capability record.")
    else:
        print("  NO. The one-step heuristic handles this board too. The lemma")
        print("  was reachable without lookahead — probably because a blocked")
        print("  board leaves the greedy policy nothing else to spend on, so it")
        print("  proves the lemma by elimination rather than by foresight.")
        print("  A sharper board would give the greedy policy attractive")
        print("  alternatives that keep it away from the lemma.")


if __name__ == "__main__":
    main()

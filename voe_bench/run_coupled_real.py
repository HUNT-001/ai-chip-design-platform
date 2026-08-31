"""Experiment 13 — coupled obligations, GROUNDED in a measured dependency.

Experiment 7 reported +72.4% for coupling-aware ordering, but its dependency
structure was DECLARED: the harness was told which obligations unlocked which.
That makes it a synthetic structural stress test, not a result. The coupling
probe (voe_fifo/formal/fifo_coupling.sby) replaced the declaration with a
measurement on the real cv32e40p_fifo, under SymbiYosys and z3:

    state_match      PASS  (k-induction)   the lemma, proved standalone
    integrity_state  PASS  (k-induction)   the property, GIVEN the lemma
    integrity        UNKNOWN               base case passes 12 steps,
                                           induction fails — true, not inductive
    integrity_lemma  UNKNOWN               `cnt <= DEPTH` provably does NOT help
    integrity_mut    FAIL at step 7        the model detects a broken FIFO
    tap_control      PASS                  the observation ports are real

So this board carries a dependency the solver confirmed rather than one this
file asserts. `fifo.integrity` cannot be proved directly; proving
`fifo.state_match` first makes it provable.

WHAT IS BEING COMPARED. Only the ORDER. Both arms use the same channels, the
same lemma-aware routing, and the same re-attempt rule, so neither is handed an
ability the other lacks:

    M-static-cached  the current default; orders by immediate utility
    N-lemmafirst     values an obligation by what it UNLOCKS as well

The mechanism that makes this fair, and that had to be added to BOTH arms:
an obligation abandoned as inconclusive is re-opened when a lemma it depends on
is later proved. Without it, whichever arm attacked `integrity` first would lose
the obligation permanently and the experiment would be measuring a punishment
rule of my own invention rather than the cost of a wasted proof attempt.

    python run_coupled_real.py --mock | --real [--seeds N]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from board import Task
from evaluation import run_campaign, aggregate
from evidence_channels import FormalChannel
from institutional_memory import promotion_verdict
from policy import STATIC_CACHED, LEMMA_FIRST, PolicyWorker
from preregistration import Criteria, Preregistration
from voe import VOE

PREREG = os.path.join(HERE, "prereg_coupled_real.json")
DEFAULT_SEEDS = 12
BUDGET = 40.0

COUPLING_SBY = os.path.join(HERE, "..", "voe_fifo", "formal", "fifo_coupling.sby")
FIFO_SBY = os.path.join(HERE, "..", "voe_fifo", "formal", "fifo_prove.sby")

LEMMA = "fifo.state_match"
DEPENDENT = "fifo.integrity"

# weight, formal task, kind
BOARD = [
    ("fifo.cnt_bound", 7.0, "prove_cnt", "invariant"),
    ("fifo.flags", 4.0, "prove_flags", "invariant"),
    ("fifo.no_overflow", 5.0, "prove_overflow", "invariant"),
    (LEMMA, 3.0, "state_match", "invariant"),
    (DEPENDENT, 9.0, "integrity", "invariant"),
]

CRITERIA = Criteria(
    question="On a board carrying a MEASURED lemma dependency (proving "
             "fifo.state_match is what makes fifo.integrity provable at all), "
             "does ordering by what an obligation UNLOCKS beat ordering by "
             "immediate utility?",
    treatment="N-lemmafirst",
    control="M-static-cached",
    min_seeds=DEFAULT_SEEDS,
    min_design_families=1,
    min_effect=0.05,
    noise_multiple=2.0,
    max_ci_width=0.10,
)

NOTES = (
    "Committed before any campaign. THREE conditions, all required:\n"
    " (1) the comparison clears the rule above;\n"
    " (2) both arms close the same weight. Ordering must change WHEN work is "
    "     done, not how much gets settled; if the treatment closes less it is "
    "     cheaper by settling less, which closed_weight already caught once;\n"
    " (3) INSTRUMENT CHECK: the direct attempt on fifo.integrity must actually "
    "     come back inconclusive in at least one campaign. If it never does, the "
    "     coupling is not being exercised and any difference is unrelated to the "
    "     dependency this experiment exists to test.\n"
    "Only one design family here (fifo), below this project's usual three, "
    "because only one design has a measured lemma dependency. That is a corpus "
    "limit and it caps how far the result generalises — stated, not hidden."
)


class LemmaRouter:
    """Routes fifo.integrity to the lemma-assisted proof ONLY once the lemma holds.

    This is the measured dependency, wired into the evidence layer: attacking
    the dependent property before the lemma is closed runs the `integrity` task,
    which the solver returns UNKNOWN for. After the lemma is proved, the same
    obligation routes to `integrity_state`, which k-induction closes.
    """

    def __init__(self, mock):
        # `integrity` is the task the solver cannot close; mock must reproduce
        # that or mock mode would silently lack the very structure under test.
        self.coupling = FormalChannel(COUPLING_SBY, mock=mock,
                                      negative_control="integrity_mut",
                                      mock_timeouts=("integrity",))
        self.plain = FormalChannel(FIFO_SBY, mock=mock, negative_control="bug_cnt")
        self.ks = None
        self.direct_attempts = 0
        self.inconclusive = 0

    def prove(self, task):
        if task in ("state_match", "integrity", "integrity_state"):
            if task == "integrity":
                lemma_proved = self.ks is not None and self.ks.proven(LEMMA)
                if lemma_proved:
                    task = "integrity_state"
                else:
                    self.direct_attempts += 1
            ev = self.coupling.prove(task)
            if task == "integrity" and ev.status in ("inconclusive", "timeout"):
                self.inconclusive += 1
            return ev
        return self.plain.prove(task)

    def covers(self, phi):
        """No simulation testbench exists for these obligations.

        Returning False sends every action to the formal channel, which is what
        makes this board a clean test of ORDER: both arms face identical method
        choices and differ only in which obligation they attack next. It also
        means `structural_first`/`cache_structure` are inert here — carried by
        both policies, exercised by neither, so they cannot confound the result.
        """
        return False

    def control_status(self):
        return self.coupling.control_status()

    def gate_status(self):
        """The vacuity gate is the COUPLING channel's, since that is where every
        claim in this experiment comes from. `integrity_mut` must fail for any
        proof here to be certified."""
        return self.coupling.gate_status()


class ReattemptWorker(PolicyWorker):
    """Re-opens an obligation when a lemma it depends on is proved.

    Available to BOTH arms. Without it the arm that happens to attack the
    dependent property first would lose it forever, and the experiment would be
    measuring a rule I invented rather than the cost of a wasted proof.
    """

    def execute(self, ks, board, phi, method):
        if hasattr(self.formal, "ks"):
            self.formal.ks = ks
        ev, j = super().execute(ks, board, phi, method)
        if phi == LEMMA and j is not None:
            self.skip.discard(DEPENDENT)      # the dependent one is worth another try
        return ev, j


def build(policy, mock, seed):
    router = LemmaRouter(mock)
    tasks = [Task(phi=p, weight=w, formal_task=t, kind=k,
                  enables=[DEPENDENT] if p == LEMMA else [])
             for p, w, t, k in BOARD]
    v = VOE(tasks, budget=BUDGET, mock=mock, formal=router, sim=router)
    w = ReattemptWorker(policy.name, policy, v.k, router, router, seed=seed,
                        static=None)
    v.workers = [w]
    return v, router


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    prereg = Preregistration(CRITERIA, PREREG, notes=NOTES).commit()
    print("=== Experiment 13: coupled obligations, grounded ===")
    print(f"    dependency : {LEMMA}  ->  {DEPENDENT}   (MEASURED, not declared)")
    print(f"    question   : {prereg.criteria.question}")
    print(f"    rule       : {prereg.criteria.rule()}")
    print(f"    committed  : sha256:{prereg.digest}  "
          f"{'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds      : {seeds}   mode = {'MOCK' if mock else 'REAL'}\n")

    results, direct, inconc = {}, {}, {}
    for p in (STATIC_CACHED, LEMMA_FIRST):
        rows, d, i = [], 0, 0
        for k in range(seeds):
            v, router = build(p, mock, 9000 + k)
            rows.append(run_campaign(v, "fifo-coupled", p.name))
            d += router.direct_attempts
            i += router.inconclusive
        results[p.name] = aggregate(rows)[0]
        direct[p.name], inconc[p.name] = d, i
        print(f"    {p.name:16s} {seeds} seeds done", flush=True)

    m, n = results[STATIC_CACHED.name], results[LEMMA_FIRST.name]

    def closed(a):
        return sum(r.closed_weight for r in a.runs) / len(a.runs)

    print(f"\n  {'arm':16s} {'mean E':>7s} {'std':>7s} {'worst':>7s} "
          f"{'closed':>8s} {'wasted direct':>14s}")
    for a in (m, n):
        print(f"  {a.policy:16s} {a.mean_E:7.3f} {a.std_E:7.3f} {a.worst_E:7.3f} "
              f"{closed(a):8.1f} {direct[a.policy]:14d}")

    gain = (n.mean_E - m.mean_E) / m.mean_E if m.mean_E else 0.0
    verdict, reasons = prereg.decide(n.mean_E, n.std_E, m.mean_E, m.std_E,
                                     seeds, 1)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")
    spread = max(n.std_E, m.std_E) / max(m.mean_E, 1e-9)
    priced, why = promotion_verdict(gain, 0.05, spread, 0.0)
    print(f"    {why}")

    c2 = closed(n) >= closed(m)
    c3 = (inconc[STATIC_CACHED.name] + inconc[LEMMA_FIRST.name]) > 0
    # HONESTY CHECK on the statistics themselves. This board is deterministic:
    # every action is formal and memoised, and neither ordering uses randomness,
    # so N campaigns are ONE campaign repeated N times. That is not replication,
    # and the committed noise gate (absolute > 2x std) is vacuous when std is
    # structurally zero. The effect is exact rather than estimated — which is a
    # different, narrower claim than "measured across N samples".
    # NOT `std == 0.0`: variance over 12 identical values still returns
    # 2.3e-16 of floating-point residue, so the exact test silently never fired
    # and this whole warning stayed invisible. Count DISTINCT outcomes instead —
    # that is the question actually being asked.
    distinct = len({round(r.efficiency, 12) for r in m.runs} |
                   {round(r.efficiency, 12) for r in n.runs})
    deterministic = distinct <= 2
    if deterministic:
        print(f"\n    NOTE: std is 0.000 on BOTH arms because this board is")
        print(f"    deterministic. {seeds} campaigns are 1 campaign repeated "
              f"{seeds} times,")
        print(f"    NOT {seeds} independent samples. The noise criterion in the")
        print(f"    committed rule is therefore vacuous here. The gain is an EXACT")
        print(f"    consequence of one wasted proof attempt (M: 6 formal actions,")
        print(f"    N: 5), not a sampled estimate.")

    print(f"\n    (2) treatment closes >= control : {'PASS' if c2 else 'FAIL'} "
          f"({closed(n):.1f} vs {closed(m):.1f})")
    print(f"    (3) coupling actually exercised : {'PASS' if c3 else 'FAIL'} "
          f"({inconc[STATIC_CACHED.name]}+{inconc[LEMMA_FIRST.name]} inconclusive "
          f"direct attempts)")

    print("\n=== what this obliges ===")
    if not c3:
        print("  The dependency was never hit, so nothing here is about coupling.")
        print("  Ignore E entirely and fix the board before reading any number.")
    elif verdict == "MET" and priced and c2:
        print("  Lemma-aware ordering earns its place on a dependency that was")
        print("  MEASURED, not declared. This is the first coupling result in the")
        print("  project that rests on real solver behaviour — Experiment 7's")
        print("  +72.4% does not, and should still be read as a structural stress")
        print("  test. Note the single design family: this shows the effect is")
        print("  real, NOT that it generalises.")
    elif verdict == "UNDERPOWERED":
        print("  Cannot answer. Add seeds; change nothing else.")
    else:
        print("  Ordering by what an obligation unlocks does NOT pay here, even")
        print("  with a real dependency present. That would make Experiment 7's")
        print("  +72.4% an artifact of its declared structure, and the honest")
        print("  move is to record coupling-aware ordering as REJECTED.")


if __name__ == "__main__":
    main()

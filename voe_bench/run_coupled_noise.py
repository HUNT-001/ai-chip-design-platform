"""Experiment 15 — the same coupling question, on a board that actually varies.

Experiments 13 and 14 both carried a caveat I could not remove at the time:
their boards were formal-only, every channel deterministic and memoised, so N
campaigns were ONE campaign repeated N times. The gains (+20.0%, +22.2%) were
exact arithmetic rather than sampled estimates, and the committed noise
criterion — absolute gain > 2x std — was VACUOUS because std was structurally
zero. That was recorded as the revisit condition, and this file discharges it.

WHAT CHANGES. mv_filter has a real Verilator testbench covering `mvf.sticky` and
`mvf.clear`. Wiring it in makes those obligations simulable, so the simulation
seed varies which vectors run, which varies n_eff, which varies E across
campaigns. The lemma dependency itself stays formal on both families — the
coupling under test is untouched. The board becomes noisy WITHOUT the mechanism
being altered.

WHY THIS MATTERS MORE THAN A BIGGER GAIN. A deterministic +22.2% and a noisy
+22.2% are different claims. The first says "on this board, this ordering costs
one fewer proof attempt". The second says "the advantage survives the variance a
real campaign actually has". Only the second is evidence about verification
rather than about arithmetic.

INSTRUMENT CHECK, INVERTED. Every previous experiment here checked that noise
did not swamp the effect. This one must first check that noise EXISTS: if the
campaigns still come out identical, the board did not become stochastic, the
committed criterion is still vacuous, and the run answers nothing. That is
condition (1) and it is read before any number.

    python run_coupled_noise.py --mock | --real [--seeds N]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from board import Task
from evaluation import run_campaign, aggregate
from institutional_memory import promotion_verdict
from policy import STATIC_CACHED, LEMMA_FIRST
from preregistration import Criteria, Preregistration
from run_benchmark import SimRouter
from run_coupled_two import (BOARD, COUPLINGS, TwoFamilyRouter, ReattemptWorker)
from voe import VOE

PREREG = os.path.join(HERE, "prereg_coupled_noise.json")
DEFAULT_SEEDS = 24
BUDGET = 80.0

# mv_filter's testbench covers exactly these; everything else stays formal, so
# the lemma dependency is unchanged by making the board noisy.
SIMULABLE = ("mvf.sticky", "mvf.clear", "mvf.bug")

# `mvf.bug` is a MUTANT-REFUTED obligation, added deliberately and named here
# rather than slipped into the board. Without it every obligation is a TRUE
# property, simulation passes on every seed, and the board stays deterministic
# even with a testbench wired in — the first draft of this experiment had
# exactly that flaw and would have reported "still deterministic" without
# revealing why. A property that random vectors sometimes catch and sometimes
# miss is the only thing that makes campaign variance real.
EXTRA = ("mvf.bug", 6.0, "bug_sticky", "mvf")

CRITERIA = Criteria(
    question="Does the lemma-ordering advantage survive on a board with REAL "
             "campaign variance, rather than only on the deterministic "
             "formal-only boards of Experiments 13 and 14?",
    treatment="N-lemmafirst",
    control="M-static-cached",
    min_seeds=DEFAULT_SEEDS,
    min_design_families=2,
    min_effect=0.05,
    noise_multiple=2.0,
    max_ci_width=0.10,
)

NOTES = (
    "Committed before any campaign. FOUR conditions, all required:\n"
    " (1) INVERTED INSTRUMENT CHECK, read FIRST: the campaigns must actually "
    "     DIFFER from one another. Experiments 13/14 were deterministic, so "
    "     their seed counts were repetition and their noise criterion was "
    "     vacuous. If this board is still deterministic it answers nothing, "
    "     whatever E says;\n"
    " (2) the comparison clears the committed rule, whose noise term is now "
    "     meaningful because std is no longer structurally zero;\n"
    " (3) both arms close the same weight;\n"
    " (4) the coupling is still exercised on BOTH families.\n"
    "Simulation is enabled ONLY for mvf.sticky and mvf.clear, which is what "
    "mv_filter's testbench actually covers. The lemma dependency stays formal "
    "on both families, so the mechanism under test is unchanged — the board "
    "gains variance, not a different question."
)


class NoisyRouter(TwoFamilyRouter):
    """Two-family lemma routing, plus real simulation where a testbench exists."""

    def __init__(self, mock):
        super().__init__(mock)
        self.sim = SimRouter(mock)

    def covers(self, phi):
        # Only what mv_filter's testbench genuinely checks. Claiming coverage
        # the TB does not have is the defect that once credited a passing run to
        # a property it never examined.
        return phi in SIMULABLE and self.sim.covers(phi)

    def run(self, **kw):
        return self.sim.run(**kw)


def build(policy, mock, seed):
    router = NoisyRouter(mock)
    rows = list(BOARD) + [EXTRA]
    tasks = [Task(phi=p, weight=w, formal_task=t, kind="invariant",
                  inject_bug=(p == "mvf.bug"),
                  enables=[d for d, c in COUPLINGS.items() if c[0] == p])
             for p, w, t, _fam in rows]
    v = VOE(tasks, budget=BUDGET, mock=mock, formal=router, sim=router)
    v.workers = [ReattemptWorker(policy.name, policy, v.k, router, router,
                                 seed=seed, static=None)]
    return v, router


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    prereg = Preregistration(CRITERIA, PREREG, notes=NOTES).commit()
    print("=== Experiment 15: does the effect survive real variance? ===")
    print(f"    simulable : {', '.join(SIMULABLE)}  (mv_filter testbench)")
    print("    mvf.bug is mutant-refuted: random vectors sometimes catch it,")
    print("    which is what makes campaign outcomes actually differ")
    print("    the lemma dependency stays FORMAL on both families")
    print(f"    rule      : {prereg.criteria.rule()}")
    print(f"    committed : sha256:{prereg.digest}  "
          f"{'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds     : {seeds}   mode = {'MOCK' if mock else 'REAL'}\n")

    res, inc = {}, {}
    for p in (STATIC_CACHED, LEMMA_FIRST):
        rows, i = [], {"fifo": 0, "mvf": 0}
        for k in range(seeds):
            v, r = build(p, mock, 13000 + k)
            rows.append(run_campaign(v, "coupled-noise", p.name))
            for fam in i:
                i[fam] += r.inconclusive[fam]
        res[p.name], inc[p.name] = aggregate(rows)[0], i
        print(f"    {p.name:16s} {seeds} seeds done", flush=True)

    m, n = res[STATIC_CACHED.name], res[LEMMA_FIRST.name]

    def cw(a):
        return sum(r.closed_weight for r in a.runs) / len(a.runs)

    def distinct(a):
        return len({round(r.efficiency, 9) for r in a.runs})

    print(f"\n  {'arm':16s} {'mean E':>7s} {'std':>7s} {'worst':>7s} "
          f"{'closed':>8s} {'distinct':>9s}")
    for a in (m, n):
        print(f"  {a.policy:16s} {a.mean_E:7.3f} {a.std_E:7.3f} {a.worst_E:7.3f} "
              f"{cw(a):8.1f} {distinct(a):9d}")

    # (1) FIRST: did the board actually become stochastic?
    c1 = distinct(m) > 1 or distinct(n) > 1
    print(f"\n    (1) board is genuinely NOISY   : {'PASS' if c1 else 'FAIL'} "
          f"({distinct(m)} and {distinct(n)} distinct outcomes)")
    if not c1 and mock:
        print("\n    EXPECTED IN MOCK: mock simulation returns a fixed verdict,")
        print("    so mock campaigns are deterministic by construction. This")
        print("    check is only informative under --real, where the simulation")
        print("    seed decides which vectors run. Not a result either way.")
        return
    if not c1:
        print("\n    STOP. The campaigns are still identical, so this board did")
        print("    not become stochastic and the committed noise criterion is")
        print("    still vacuous. Nothing below is an answer to the question")
        print("    this experiment was written to ask. Do not record a verdict.")
        return

    gain = (n.mean_E - m.mean_E) / m.mean_E if m.mean_E else 0.0
    verdict, reasons = prereg.decide(n.mean_E, n.std_E, m.mean_E, m.std_E,
                                     seeds, 2)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")
    spread = max(n.std_E, m.std_E) / max(m.mean_E, 1e-9)
    priced, why = promotion_verdict(gain, 0.05, spread, 0.0)
    print(f"    {why}")

    c3 = cw(n) >= cw(m)
    c4 = inc[STATIC_CACHED.name]["fifo"] > 0 and inc[STATIC_CACHED.name]["mvf"] > 0
    print(f"\n    (3) treatment closes >= control : {'PASS' if c3 else 'FAIL'} "
          f"({cw(n):.1f} vs {cw(m):.1f})")
    print(f"    (4) coupling on BOTH families  : {'PASS' if c4 else 'FAIL'} "
          f"(fifo {inc[STATIC_CACHED.name]['fifo']}, "
          f"mvf {inc[STATIC_CACHED.name]['mvf']})")

    print("\n=== what this obliges ===")
    if verdict == "MET" and priced and c3 and c4:
        print("  The advantage survives real campaign variance. The noise")
        print("  criterion is now meaningful rather than vacuous, and the")
        print("  determinism caveat carried by Experiments 13 and 14 is")
        print("  DISCHARGED — those numbers were exact; this one is estimated,")
        print("  and it holds. Lemma-aware ordering is a measured result about")
        print("  verification, not an arithmetic consequence of one board.")
    elif verdict == "UNDERPOWERED":
        print("  Now that the board is noisy the effect cannot be resolved at")
        print("  this sample size. Add seeds; change nothing else. Note this is")
        print("  a WEAKER position than Experiments 13/14, not a stronger one:")
        print("  their exactness came from having no variance to resolve.")
    else:
        print("  The advantage does NOT survive real variance. Experiments 13")
        print("  and 14 then measured an arithmetic property of deterministic")
        print("  boards, and the honest move is to narrow both claims to that.")


if __name__ == "__main__":
    main()

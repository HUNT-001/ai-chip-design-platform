"""Experiment 14 — lemma-aware ordering across TWO design families.

Experiment 13 found +20.0% for ordering by what an obligation unlocks, on a
dependency the solver confirmed. Its recorded limit was blunt: one design
family, so the effect was shown to be REAL but not to GENERALISE. That was
written into the ledger as the revisit condition, and the mv_filter coupling
probe has now discharged it:

    FIFO (cv32e40p_fifo)          lemma = pointers + count + memory
      state_match      PASS       integrity        UNKNOWN
      integrity_state  PASS       integrity_mut    FAIL @7

    mv_filter (pulp, HELD OUT)    lemma = one scalar counter
      mvf_state        PASS       mvf_equiv        UNKNOWN
      mvf_equiv_lemma  PASS       mvf_equiv_mut    FAIL @13

Two designs, two measured dependencies, two DIFFERENT lemma shapes. So this
board carries the phenomenon rather than one invariant that happened to work
twice, and mv_filter is held out — no policy was developed against it.

WHAT IS STILL TRUE AND IS NOT FIXED BY THIS. Both boards are formal-only and
therefore DETERMINISTIC: no simulation, no randomness in either ordering rule.
Seeds here are repetition, not replication, and the committed noise criterion
stays vacuous. Adding a second family answers "does it generalise across
designs", not "is the estimate stable under noise". Those are different
questions and only the first is being asked.

    python run_coupled_two.py --mock | --real [--seeds N]
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

PREREG = os.path.join(HERE, "prereg_coupled_two.json")
DEFAULT_SEEDS = 12
BUDGET = 80.0

FIFO_COUPLING = os.path.join(HERE, "..", "voe_fifo", "formal", "fifo_coupling.sby")
FIFO_PLAIN = os.path.join(HERE, "..", "voe_fifo", "formal", "fifo_prove.sby")
MVF_COUPLING = os.path.join(HERE, "..", "voe_heldout", "formal", "mvf_coupling.sby")
MVF_PLAIN = os.path.join(HERE, "..", "voe_heldout", "formal", "mvf.sby")

# obligation -> (lemma, direct task, lemma-assisted task, sby)
COUPLINGS = {
    "fifo.integrity": ("fifo.state_match", "integrity", "integrity_state", "fifo"),
    "mvf.equiv": ("mvf.state", "mvf_equiv", "mvf_equiv_lemma", "mvf"),
}
LEMMA_TASK = {"fifo.state_match": ("state_match", "fifo"),
              "mvf.state": ("mvf_state", "mvf")}

BOARD = [
    ("fifo.cnt_bound", 7.0, "prove_cnt", "fifo"),
    ("fifo.flags", 4.0, "prove_flags", "fifo"),
    ("fifo.no_overflow", 5.0, "prove_overflow", "fifo"),
    ("fifo.state_match", 3.0, "state_match", "fifo"),
    ("fifo.integrity", 9.0, "integrity", "fifo"),
    ("mvf.sticky", 6.0, "prove_sticky", "mvf"),
    ("mvf.clear", 4.0, "prove_clear", "mvf"),
    ("mvf.state", 3.0, "mvf_state", "mvf"),
    ("mvf.equiv", 9.0, "mvf_equiv", "mvf"),
]

CRITERIA = Criteria(
    question="Across TWO design families each carrying a MEASURED lemma "
             "dependency, with different lemma shapes and one family held out, "
             "does ordering by what an obligation UNLOCKS beat ordering by "
             "immediate utility?",
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
    " (1) the comparison clears the rule above;\n"
    " (2) both arms close the same weight;\n"
    " (3) INSTRUMENT CHECK: the direct attempt must come back inconclusive on "
    "     BOTH families, not just one. A win driven entirely by the FIFO would "
    "     be Experiment 13 again with extra obligations attached;\n"
    " (4) the effect must hold on the HELD-OUT family (mv_filter) considered "
    "     alone. Reported separately below.\n"
    "NOT fixed by this experiment: both boards are formal-only and "
    "deterministic, so seeds remain repetition rather than replication and the "
    "noise criterion is still vacuous. This asks whether the effect generalises "
    "across designs, not whether it is stable under noise."
)


class TwoFamilyRouter:
    """Lemma-aware routing across both designs, with per-family bookkeeping."""

    def __init__(self, mock):
        self.ch = {
            ("fifo", "coupling"): FormalChannel(FIFO_COUPLING, mock=mock,
                                                negative_control="integrity_mut",
                                                mock_timeouts=("integrity",)),
            ("fifo", "plain"): FormalChannel(FIFO_PLAIN, mock=mock,
                                             negative_control="bug_cnt"),
            ("mvf", "coupling"): FormalChannel(MVF_COUPLING, mock=mock,
                                               negative_control="mvf_equiv_mut",
                                               mock_timeouts=("mvf_equiv",)),
            ("mvf", "plain"): FormalChannel(MVF_PLAIN, mock=mock,
                                            negative_control="bug_sticky"),
        }
        self.task_of = {phi: (t, fam) for phi, _w, t, fam in BOARD}
        self.ks = None
        self.inconclusive = {"fifo": 0, "mvf": 0}

    def prove(self, task):
        entry = next(((phi, c) for phi, c in COUPLINGS.items()
                      if c[1] == task), None)
        if entry is not None:
            phi, (lemma, direct, assisted, fam) = entry
            if self.ks is not None and self.ks.proven(lemma):
                return self.ch[(fam, "coupling")].prove(assisted)
            ev = self.ch[(fam, "coupling")].prove(direct)
            if ev.status in ("inconclusive", "timeout"):
                self.inconclusive[fam] += 1
            return ev
        for phi, (lt, fam) in LEMMA_TASK.items():
            if task == lt:
                return self.ch[(fam, "coupling")].prove(task)
        fam = "mvf" if task.startswith("prove_s") or task == "prove_clear" else "fifo"
        return self.ch[(fam, "plain")].prove(task)

    def covers(self, phi):
        return False

    def control_status(self):
        return self.ch[("fifo", "coupling")].control_status()

    def gate_status(self):
        armed_f, why_f = self.ch[("fifo", "coupling")].gate_status()
        armed_m, why_m = self.ch[("mvf", "coupling")].gate_status()
        return (armed_f and armed_m), f"fifo: {why_f} | mvf: {why_m}"


class ReattemptWorker(PolicyWorker):
    """Re-opens a dependent obligation once its lemma is proved. BOTH arms."""

    def execute(self, ks, board, phi, method):
        if hasattr(self.formal, "ks"):
            self.formal.ks = ks
        ev, j = super().execute(ks, board, phi, method)
        if j is not None:
            for dep, (lemma, *_rest) in COUPLINGS.items():
                if phi == lemma:
                    self.skip.discard(dep)
        return ev, j


def build(policy, mock, seed, only=None):
    router = TwoFamilyRouter(mock)
    rows = [b for b in BOARD if only is None or b[3] == only]
    tasks = [Task(phi=p, weight=w, formal_task=t, kind="invariant",
                  enables=[d for d, c in COUPLINGS.items() if c[0] == p])
             for p, w, t, _fam in rows]
    v = VOE(tasks, budget=BUDGET, mock=mock, formal=router, sim=router)
    v.workers = [ReattemptWorker(policy.name, policy, v.k, router, router,
                                 seed=seed, static=None)]
    return v, router


def campaign(policy, mock, seeds, only=None):
    rows, inc = [], {"fifo": 0, "mvf": 0}
    for k in range(seeds):
        v, r = build(policy, mock, 11000 + k, only)
        rows.append(run_campaign(v, "coupled2", policy.name))
        for fam in inc:
            inc[fam] += r.inconclusive[fam]
    return aggregate(rows)[0], inc


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    prereg = Preregistration(CRITERIA, PREREG, notes=NOTES).commit()
    print("=== Experiment 14: lemma ordering across TWO families ===")
    print("    fifo      lemma = pointers + count + memory   (dev)")
    print("    mv_filter lemma = one scalar counter          (HELD OUT)")
    print(f"    rule      : {prereg.criteria.rule()}")
    print(f"    committed : sha256:{prereg.digest}  "
          f"{'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds     : {seeds}   mode = {'MOCK' if mock else 'REAL'}\n")

    res, inc = {}, {}
    for p in (STATIC_CACHED, LEMMA_FIRST):
        res[p.name], inc[p.name] = campaign(p, mock, seeds)
        print(f"    {p.name:16s} both families done", flush=True)
    held = {p.name: campaign(p, mock, seeds, only="mvf")[0]
            for p in (STATIC_CACHED, LEMMA_FIRST)}

    m, n = res[STATIC_CACHED.name], res[LEMMA_FIRST.name]
    hm, hn = held[STATIC_CACHED.name], held[LEMMA_FIRST.name]

    def cw(a):
        return sum(r.closed_weight for r in a.runs) / len(a.runs)

    print(f"\n  {'arm':16s} {'mean E':>7s} {'closed':>8s} "
          f"{'inconclusive fifo/mvf':>24s}")
    for a in (m, n):
        i = inc[a.policy]
        print(f"  {a.policy:16s} {a.mean_E:7.3f} {cw(a):8.1f} "
              f"{str(i['fifo']) + '/' + str(i['mvf']):>24s}")
    print(f"\n  HELD-OUT FAMILY ONLY (mv_filter)")
    for a in (hm, hn):
        print(f"  {a.policy:16s} {a.mean_E:7.3f} {cw(a):8.1f}")

    gain = (n.mean_E - m.mean_E) / m.mean_E if m.mean_E else 0.0
    gain_held = (hn.mean_E - hm.mean_E) / hm.mean_E if hm.mean_E else 0.0
    verdict, reasons = prereg.decide(n.mean_E, n.std_E, m.mean_E, m.std_E,
                                     seeds, 2)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")
    spread = max(n.std_E, m.std_E) / max(m.mean_E, 1e-9)
    priced, why = promotion_verdict(gain, 0.05, spread, 0.0)
    print(f"    {why}")

    distinct = len({round(r.efficiency, 12) for r in m.runs} |
                   {round(r.efficiency, 12) for r in n.runs})
    if distinct <= 2:
        print(f"\n    NOTE: still DETERMINISTIC — {seeds} campaigns are 1 campaign")
        print(f"    repeated {seeds} times. The noise criterion remains vacuous.")
        print(f"    Two families answers GENERALISATION, not stability.")

    c2 = cw(n) >= cw(m)
    c3 = inc[STATIC_CACHED.name]["fifo"] > 0 and inc[STATIC_CACHED.name]["mvf"] > 0
    c4 = gain_held > 0
    print(f"\n    (2) treatment closes >= control     : {'PASS' if c2 else 'FAIL'}")
    print(f"    (3) coupling exercised on BOTH      : {'PASS' if c3 else 'FAIL'} "
          f"(fifo {inc[STATIC_CACHED.name]['fifo']}, "
          f"mvf {inc[STATIC_CACHED.name]['mvf']})")
    print(f"    (4) holds on HELD-OUT family alone  : {'PASS' if c4 else 'FAIL'} "
          f"({gain_held:+.1%})")

    print("\n=== what this obliges ===")
    if not c3:
        print("  The dependency was not exercised on both families. Whatever the")
        print("  number says, this is not a two-family result. Fix the board.")
    elif verdict == "MET" and priced and c2 and c4:
        print("  Lemma-aware ordering generalises across two designs with")
        print("  DIFFERENT lemma shapes, one of them held out. Experiment 13's")
        print("  single-family caveat is discharged. Experiment 7's +72.4%")
        print("  remains a declared-structure stress test and is NOT revived.")
        print("  Determinism is untouched: this is about generalisation only.")
    else:
        print("  A committed condition failed. Do not widen the claim beyond")
        print("  Experiment 13's single family.")


if __name__ == "__main__":
    main()

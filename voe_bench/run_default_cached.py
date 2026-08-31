"""Experiment 11 — M-static-cached vs G-diagnostic, after diagnosing L's failure.

Experiment 10 met its headline bar (+8.5%) and FAILED its committed held-out
condition (-5.6%). The obligation was to diagnose before doing anything else.

DIAGNOSIS. Not overfitting. L paid 0.5 for a structural read once per
OBLIGATION, but structure is a property of a DESIGN. Six of L's eleven reads on
the full board were repeat purchases of a fact it already held, and four of its
six on the held-out board. That overhead amortises across twenty obligations and
dominates across seven — which is precisely why L won the full benchmark and lost
the small held-out split. The evidence channel memoises the tool invocation, so
no tool ran twice; the LEDGER still charged for the action, because the action
was still taken. M takes it once per design.

This is the third appearance of one recurring defect — being charged for
information already held. It first cost Experiment 8 a -52.6% result, then hid
inside L as something that looked like overfitting. It is now a permanent test.

MULTIPLICITY, stated rather than buried. This is the THIRD candidate proposed for
the default (H rejected, L rejected, now M), and testing candidates until one
wins is how false positives are manufactured. Two things bear on it. First, M is
not a new hypothesis: it is L with a defect removed, and the defect was diagnosed
from the failure BEFORE the fix was written. Second, and more important, the
held-out condition is the guard against exactly this, and it is committed again
below unchanged. If M's advantage is an artifact of repeated attempts, the split
it already failed once is where that should surface.

    python run_default_cached.py --mock | --real [--seeds N]
"""
import collections, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from evaluation import run_campaign, aggregate
from institutional_memory import promotion_verdict
from policy import DIAGNOSTIC, STATIC_CACHED
from preregistration import Criteria, Preregistration
from registry import OWNER, summary
from run_benchmark import (TASKS, FormalRouter, SimRouter, build_features,
                           outcome_table, realisable)
from run_default import DEV, HELD_OUT, build, closed_wt, report
from run_multistep import StaticRouter

PREREG = os.path.join(HERE, "prereg_default_cached.json")
DEFAULT_SEEDS = 24

CRITERIA = Criteria(
    question="Should M-static-cached (one real structural read per DESIGN, no "
             "probe, no chain) replace G-diagnostic as the recommended default?",
    treatment="M-static-cached",
    control="G-diagnostic",
    min_seeds=DEFAULT_SEEDS,
    min_design_families=3,
    min_effect=0.05,
    noise_multiple=2.0,
    max_ci_width=0.10,
)

NOTES = (
    "Committed before any campaign. FOUR conditions, all required:\n"
    " (1) the full-benchmark comparison clears the rule above;\n"
    " (2) the HELD-OUT split (lfsr, mv_filter) shows M NOT WORSE than G. This is "
    "     the condition L failed, kept identical and un-weakened;\n"
    " (3) M closes at least as much weight as G — cheaper by settling less is "
    "     not better;\n"
    " (4) INSTRUMENT CHECK: zero redundant structural reads, verified during the "
    "     run. If M still repeat-purchases design facts, the diagnosis was wrong "
    "     and the comparison is meaningless whatever E says.\n"
    "Complexity charge 0.0: M removes machinery relative to G (no probe) and "
    "relative to L (fewer reads). Third default candidate tested; multiplicity is "
    "discussed in the module docstring and the held-out condition is the guard."
)


def run_arm(policy, mock, routers, static, seeds, tasks, label):
    """Campaigns plus an instrument check on redundant structural purchases."""
    rows, reads = [], []
    for i in range(seeds):
        v = build(policy, mock, routers, static, 7000 + i, tasks)
        w = v.workers[0]
        seen, orig = [], w.execute

        def ex(ks, b, phi, m, _o=orig, _s=seen):
            if m == "static":
                _s.append(OWNER[phi])
            return _o(ks, b, phi, m)

        w.execute = ex
        rows.append(run_campaign(v, label, policy.name))
        c = collections.Counter(seen)
        reads.append(len(seen) - len(c))          # redundant purchases this run
    return rows, max(reads) if reads else 0


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    prereg = Preregistration(CRITERIA, PREREG, notes=NOTES).commit()
    print("=== Experiment 11: M-static-cached vs G-diagnostic ===")
    print(f"    {summary()}")
    print(f"    dev      : {', '.join(DEV)}")
    print(f"    held out : {', '.join(HELD_OUT)}")
    print(f"    question : {prereg.criteria.question}")
    print(f"    rule     : {prereg.criteria.rule()}")
    print(f"    committed: sha256:{prereg.digest}  "
          f"{'INTACT' if prereg.intact else 'MODIFIED — INVALID'}")
    print(f"    seeds    : {seeds}   mode = {'MOCK' if mock else 'REAL'}")

    routers = (FormalRouter(mock), SimRouter(mock))
    static = StaticRouter(mock)
    real_E, *_ = realisable(outcome_table(routers, build_features()))

    splits = (("full", TASKS),
              ("dev", [t for t in TASKS if OWNER[t.phi] in DEV]),
              ("held-out", [t for t in TASKS if OWNER[t.phi] in HELD_OUT]))

    res, redundant = {}, 0
    for label, tasks in splits:
        rows = []
        for p in (DIAGNOSTIC, STATIC_CACHED):
            r, red = run_arm(p, mock, routers, static, seeds, tasks, label)
            rows += r
            if p is STATIC_CACHED:
                redundant = max(redundant, red)
        res[label] = {a.policy: a for a in aggregate(rows)}
        print(f"    {label:9s} done", flush=True)

    pairs = {k: (v[DIAGNOSTIC.name], v[STATIC_CACHED.name]) for k, v in res.items()}
    gain_full = report("FULL BENCHMARK (6 designs)", pairs["full"], real_E)
    report("DEV (policies were developed here)", pairs["dev"])
    gain_held = report("HELD OUT — the condition L failed", pairs["held-out"])

    g, m = pairs["full"]
    families = len({OWNER[t.phi] for t in TASKS})
    verdict, reasons = prereg.decide(m.mean_E, m.std_E, g.mean_E, g.std_E,
                                     seeds, families)
    print(f"\n=== verdict by the COMMITTED rule: {verdict} ===")
    for r in reasons:
        print(f"    {r}")

    spread = max(m.std_E, g.std_E) / max(g.mean_E, 1e-9)
    priced, why = promotion_verdict(gain_full, 0.05, spread, 0.0)
    print(f"    {why}")

    hg, hm = pairs["held-out"]
    c2, c3, c4 = hm.mean_E >= hg.mean_E, closed_wt(m) >= closed_wt(g), redundant == 0
    print(f"\n    (2) held-out not worse      : {'PASS' if c2 else 'FAIL'} "
          f"(M {hm.mean_E:.3f} vs G {hg.mean_E:.3f}, {gain_held:+.1%})")
    print(f"    (3) M closes >= G's weight  : {'PASS' if c3 else 'FAIL'} "
          f"(M {closed_wt(m):.1f} vs G {closed_wt(g):.1f})")
    print(f"    (4) zero redundant reads    : {'PASS' if c4 else 'FAIL'} "
          f"(worst run repeat-bought {redundant} design fact(s))")

    print("\n=== what this obliges ===")
    if verdict == "MET" and priced and c2 and c3 and c4:
        print("  Change RECOMMENDED from G-diagnostic to M-static-cached.")
        print("  Read each design's structure ONCE with a tool that already")
        print("  existed, then commit. No probe, no posterior, no chain, no world")
        print("  model. Four attempts to add machinery (H, I/J, K, L) were beaten")
        print("  by one that removes it — and note the held-out margin is thin,")
        print("  so the honest claim is 'better on the full board, NOT WORSE on")
        print("  held-out', not 'better everywhere'.")
    elif not c4:
        print("  The instrument check FAILED: M still repeat-buys design facts,")
        print("  so the diagnosis of L was wrong. Ignore E entirely and re-open")
        print("  the diagnosis — a number from an instrument known to be broken")
        print("  is the one thing this project never records.")
    elif not c2:
        print("  Held-out fails AGAIN, with the redundancy removed. That kills the")
        print("  structural-read idea as a default rather than one implementation")
        print("  of it: record L and M both REJECTED and stop proposing variants.")
    else:
        print("  A committed condition failed. Do not change the default.")


if __name__ == "__main__":
    main()

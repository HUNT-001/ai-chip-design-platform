"""S2 — do sat_mac and pfifo constitute TWO stochastic regimes, or one?

WRITTEN BEFORE S1 WAS RUN. That ordering is deliberate and it is the only reason
this file's comparison can be trusted. If it had been written after seeing
pfifo's detection curve, every choice in it — which statistic, which model,
which threshold — would have been made with the answer visible, and "we compared
curve shapes" would be indistinguishable from "we looked for a statistic that
separated them". The commitment below is hashed, and the run prints whether the
hash still matches.

THE QUESTION, STATED SO IT CAN FAIL
------------------------------------
Having two boards that each satisfy 0 < P(detect) < 1 is NOT two stochastic
regimes. If both boards' detection behaviour follows the same law, then the
corpus contains one phenomenon sampled twice, and a mechanism tuned on one would
transfer to the other for reasons that say nothing about verification in
general. That is the Experiment 10 / policy L failure mode, moved up a level
from policies to benchmarks.

So the test is not "are both non-degenerate". It is:

    Do the two boards' detection-versus-campaign-length curves have DIFFERENT
    SHAPES, in a way that a single law cannot account for?

THE LAW BEING TESTED, AND WHY IT IS THE RIGHT NULL
---------------------------------------------------
sat_mac's corner is memoryless by construction: each vector independently hits
(-128) x (-128) with probability p, so

    P(detect | N vectors) = 1 - (1 - p)^N

exactly. This is the null hypothesis for BOTH boards. It is the right null
precisely because it is the shape the existing corpus already has — the question
is whether pfifo adds anything, and "anything" means "a curve this law cannot
fit".

pfifo should not fit it, for structural reasons stated before any measurement:
detection needs the occupancy random walk to sit at the last read-pointer slot
when a rare flush_but_first arrives, and then needs the corruption to survive to
an output. Those events are correlated across cycles, and there is a warm-up
period in which the corner is unreachable at all. A memoryless law has no warm-up
and no correlation, so the prediction is a curve that rises LATER and more
sharply than the best-fitting exponential.

If pfifo fits the memoryless law anyway, that is a real and reportable negative:
it would say the structural argument above is wrong, the two boards are one
regime, and the Stochastic Generalization Gate does not pass. This file is
written to be able to say that.

    python run_stoch_transfer.py --mock | --real [--seeds N]
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from binomial import clopper_pearson, non_degenerate
from evidence_channels import SimChannel
from preregistration import Commitment

COMMIT_PATH = os.path.join(HERE, "commit_stoch_transfer.json")
DEFAULT_SEEDS = 24
SEED_BASE = 7000

STOCH = os.path.join(HERE, "..", "voe_stoch")
STOCH2 = os.path.join(HERE, "..", "voe_stoch2")

# ---- per-board grids, both committed ------------------------------------- #
# DIFFERENT grids on purpose, and this needs defending rather than hiding.
# sat_mac's corner is 1 in 65536 and pfifo's control space is 42 states; a
# shared grid would put one board entirely in its degenerate region, and
# comparing a real curve against a flat line at 0 or 1 would "find a difference"
# that is only a statement about the grid.
#
# What makes the comparison legitimate despite that is that the statistic is
# SCALE-FREE. Fitting 1 - (1-p)^N absorbs the rate p entirely; what is left is
# the SHAPE, and shape does not care what units N is in. Two memoryless
# processes with wildly different rates both fit perfectly. So a grid chosen per
# board to span its own transition region is the correct design, not a
# concession.
BOARDS = {
    "sat_mac": {
        "dir": STOCH,
        "sources": ["rtl/sat_mac.sv", "rtl/sat_mac_mut.sv",
                    "rtl/satmac_wrap.sv", "rtl/satmac_wrap_mut.sv",
                    "sim/tb_satmac.sv"],
        "top": "tb_satmac",
        "good": "satmac_wrap", "bad": "satmac_wrap_mut",
        "phi": "satmac.acc",
        "grid": (5000, 10000, 20000, 40000, 80000, 160000),
        "why": "corner is 1 in 65536; this brackets P = 0.07 to 0.91",
    },
    "pfifo": {
        "dir": STOCH2,
        "sources": ["rtl/pfifo.sv", "rtl/pfifo_mut.sv",
                    "rtl/pfifo_wrap.sv", "rtl/pfifo_wrap_mut.sv",
                    "sim/tb_pfifo.sv"],
        "top": "tb_pfifo",
        "good": "pfifo_wrap", "bad": "pfifo_wrap_mut",
        "phi": "pfifo.order",
        "grid": (64, 128, 256, 512, 1024, 2048, 4096),
        "why": "~42-state control space; 1.5 to 100 coverings",
    },
}

SPEC = {
    "question": "do the two boards' detection-vs-campaign-length curves have "
                "different shapes, or does one memoryless law fit both?",
    "null": "P(detect | N) = 1 - (1-p)^N, p fitted per board by maximum "
            "likelihood over that board's whole grid",
    "statistic": "number of grid points whose Clopper-Pearson 95% interval "
                 "excludes the best-fitting memoryless curve",
    "gate": "the two boards are DISTINCT regimes only if the law fits one and "
            "is excluded by the other at >= 1 grid point",
    "boards": {k: {"grid": list(v["grid"]), "why": v["why"]}
               for k, v in BOARDS.items()},
    "seed_base": SEED_BASE,
    "seeds": DEFAULT_SEEDS,
}

NOTES = (
    "Committed before pfifo was ever simulated. Grids differ per board and the "
    "justification is that the statistic is scale-free: fitting 1-(1-p)^N "
    "absorbs the rate, leaving only the shape.\n"
    "KNOWN LIMITATION, stated in advance rather than discovered afterwards. "
    "Points on one board's curve share seeds, and a short campaign is a PREFIX "
    "of a long one under the same seed, so the points are positively "
    "correlated. The Clopper-Pearson interval at each point is still exact for "
    "that point, but the points are not independent observations, so this is "
    "NOT a calibrated global goodness-of-fit test and no p-value is reported "
    "for the curve as a whole. A single point excluding the fitted law is "
    "evidence; counting excluded points is description, not inference.\n"
    "If the law fits BOTH boards, the gate does not pass and the corpus "
    "contains one stochastic phenomenon sampled twice. That outcome is "
    "reportable and is not a reason to look for another statistic.\n"
    "SEED COUNT IS COMMITTED AT 24 AND MUST NOT BE RAISED. This is the "
    "opposite of the usual advice and it was measured, not assumed: "
    "`--calibrate` simulates a known-memoryless board and a known-warm-up "
    "board and counts how often each is flagged. At 24 seeds the false "
    "exclusion rate is about 5% with essentially full power against a warm-up "
    "alternative; at 48 seeds the false exclusion rate rises to about 14%. "
    "More data makes this statistic WORSE calibrated, because the "
    "Clopper-Pearson intervals shrink faster than a one-parameter fit to "
    "correlated points can track a true curve. So 'add seeds' is not an "
    "available response to an ambiguous result here, and an UNDERPOWERED "
    "reading must be answered some other way."
)


def channel(cfg, mock):
    srcs = [os.path.join(cfg["dir"], s) for s in cfg["sources"]]
    return SimChannel(mock=mock, sources=srcs, top=cfg["top"],
                      defines_for=lambda bug: [
                          f"DUT={cfg['bad'] if bug else cfg['good']}"],
                      covers=lambda phi: True, mock_finds_bug=True)


def curve(cfg, mock, seeds):
    """(N, hits, seeds) at each committed grid point for one board."""
    out = []
    for nvec in cfg["grid"]:
        ch = channel(cfg, mock)          # fresh per point: the binary differs
        hits = 0
        for s in range(seeds):
            ev = ch.run(inject_bug=True, seed=SEED_BASE + s, nvec=nvec,
                        phi=cfg["phi"])
            hits += (ev.status == "counterexample")
        out.append((nvec, hits, seeds))
        print(f"      n={nvec:<7} {hits}/{seeds}", flush=True)
    return out


def fit_memoryless(points):
    """Maximum-likelihood p for P(N) = 1-(1-p)^N over the whole curve.

    A coarse-to-fine grid search on log p rather than an optimiser: the
    likelihood is one-dimensional and smooth, and a search that can be read line
    by line is worth more here than a fast one that cannot.
    """
    import math

    def loglik(p):
        if not (0.0 < p < 1.0):
            return -math.inf
        tot = 0.0
        for n_cyc, k, n in points:
            q = 1.0 - (1.0 - p) ** n_cyc
            q = min(max(q, 1e-12), 1.0 - 1e-12)
            tot += k * math.log(q) + (n - k) * math.log1p(-q)
        return tot

    lo, hi = -12.0, -0.001          # log10 p
    best = lo
    for _ in range(6):
        step = (hi - lo) / 200.0
        cands = [lo + i * step for i in range(201)]
        best = max(cands, key=lambda e: loglik(10 ** e))
        lo, hi = best - step, best + step
    return 10 ** best, loglik(10 ** best)


def assess(name, points):
    """Print one board's curve against its best-fitting memoryless law."""
    p, _ = fit_memoryless(points)
    print(f"\n  {name}: best-fit memoryless rate p = {p:.3e} "
          f"(1 in {1/p:,.0f} cycles)")
    print(f"  {'cycles':>8} {'hits':>8} {'observed':>9} {'95% CI':>18} "
          f"{'law says':>9} {'verdict':>10}")
    excluded = []
    for n_cyc, k, n in points:
        obs = k / n
        lo, hi = clopper_pearson(k, n)
        pred = 1.0 - (1.0 - p) ** n_cyc
        out = not (lo <= pred <= hi)
        if out:
            excluded.append(n_cyc)
        print(f"  {n_cyc:>8} {k:>4}/{n:<3} {obs:>9.3f}   [{lo:.3f}, {hi:.3f}] "
              f"{pred:>9.3f} {'EXCLUDES' if out else 'consistent':>10}")
    # the warm-up signature: the shortest campaign that ever detects
    first = next((n for n, k, _ in points if k > 0), None)
    print(f"  first detecting campaign length : {first}")
    return excluded, p


def calibrate(seed_counts=(12, 24, 48), reps=200):
    """Can this statistic actually tell the two shapes apart, and how often is
    it wrong when it says yes?

    Every gate this project has added came from a control that was present,
    correct, and unable to observe — four separate times. So before the shape
    statistic is allowed to decide anything, it is run against two boards whose
    answers are known by construction: one exactly memoryless, one with a
    warm-up and no memoryless description. The first must almost never be
    flagged; the second must almost always be.

    This is a simulation of the STATISTIC, not of either DUT. It uses no
    Verilator and makes no claim about pfifo. Its only purpose is to stop a
    verdict being read off an instrument whose error rate nobody measured.
    """
    import random
    print("=== calibration of the shape statistic (no DUT involved) ===")
    print("    board A: exactly memoryless, p = 1/65536 -> must NOT be flagged")
    print("    board B: unreachable below 200 cycles, then linear rise")
    print("             -> has no memoryless description, so MUST be flagged\n")
    p0 = 1.0 / 65536.0
    ga = BOARDS["sat_mac"]["grid"]
    gb = BOARDS["pfifo"]["grid"]

    def warm(n):
        return 0.0 if n < 200 else min(1.0, (n - 200) / 1500.0)

    def flagged(points):
        p, _ = fit_memoryless(points)
        for n_cyc, k, tot in points:
            lo, hi = clopper_pearson(k, tot)
            if not (lo <= 1.0 - (1.0 - p) ** n_cyc <= hi):
                return True
        return False

    print(f"  {'seeds':>6} {'false alarm (A)':>16} {'power (B)':>12}")
    for n in seed_counts:
        fa = fb = 0
        for r in range(reps):
            random.seed(r)
            A = [(c, sum(random.random() < 1 - (1 - p0) ** c for _ in range(n)), n)
                 for c in ga]
            B = [(c, sum(random.random() < warm(c) for _ in range(n)), n)
                 for c in gb]
            fa += flagged(A)
            fb += flagged(B)
        print(f"  {n:>6} {fa / reps:>15.0%} {fb / reps:>11.0%}")
    print("\n  NOTE the direction of the false-alarm column. It gets WORSE with")
    print("  more seeds: the exact intervals shrink faster than a")
    print("  one-parameter fit to correlated points can follow, so a true")
    print("  memoryless curve starts falling outside its own fit. This is why")
    print("  the seed count is committed in advance and why 'run it with more")
    print("  seeds' is not an available response to an ambiguous verdict.")


def main():
    if "--calibrate" in sys.argv:
        calibrate()
        return
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    commit = Commitment("stochastic transfer S2", COMMIT_PATH,
                        dict(SPEC, seeds=seeds), NOTES).commit()
    print("=== S2: one stochastic regime, or two? ===")
    print("    null      : P(detect | N) = 1 - (1-p)^N, p fitted per board")
    print("    gate      : distinct regimes only if the law fits one board and")
    print("                is excluded by the other")
    print(commit.render())
    print(f"    seeds     : {seeds}   mode = {'MOCK' if mock else 'REAL'}\n")
    if not commit.intact:
        print("  The commitment was modified after hashing. INVALID.")
        return
    if mock:
        print("  MOCK returns a fixed verdict, so every curve is flat at 1.0")
        print("  and nothing here is a measurement. Use --real.\n")

    results = {}
    for name, cfg in BOARDS.items():
        print(f"    {name}: {cfg['why']}")
        results[name] = curve(cfg, mock, seeds)

    # ---- eligibility of the COMPARISON, before the comparison --------------
    # Each board must itself be non-degenerate somewhere on its grid, or its
    # "shape" is a flat line and the fit is meaningless. This is the same gate
    # Experiment 15 produced, applied to a comparison rather than a policy.
    print("\n  --- admission of each board ---")
    usable = {}
    for name, pts in results.items():
        ok_pts = [n for n, k, tot in pts if non_degenerate(k, tot, 2)[0]]
        usable[name] = ok_pts
        print(f"    {name:<10} non-degenerate at: "
              f"{ok_pts if ok_pts else 'NOWHERE on its grid'}")
    if not all(usable.values()):
        print("\n=== verdict: NOT EVALUABLE ===")
        print("  At least one board is degenerate everywhere on its committed")
        print("  grid, so it has no curve to compare and the transfer question")
        print("  was never asked. This is not a NOT MET. Extending that board's")
        print("  grid until a point qualifies is the Experiment 15 refusal")
        print("  wearing a different hat; the honest move is a different mutant")
        print("  or design, committed before it is measured.")
        return

    print("\n  --- shape comparison against the memoryless law ---")
    verdicts = {name: assess(name, pts) for name, pts in results.items()}

    exc = {k: v[0] for k, v in verdicts.items()}
    fits = [k for k, v in exc.items() if not v]
    breaks = [k for k, v in exc.items() if v]

    print("\n=== verdict ===")
    print(f"    fits the memoryless law      : {fits if fits else 'neither'}")
    print(f"    excluded by its own data     : {breaks if breaks else 'neither'}")

    if fits and breaks:
        print("\n  DISTINCT REGIMES. One board's detection behaviour is the")
        print("  memoryless law and the other's is not, so the corpus contains")
        print("  two different stochastic phenomena rather than one sampled")
        print("  twice. The Stochastic Generalization Gate PASSES.")
        print("\n  What that licenses, exactly: the minimal uncertainty-aware")
        print("  mechanism can now be tested on two boards whose uncertainty")
        print("  has different structure, so an advantage on both is evidence")
        print("  about verification rather than about one design. It does NOT")
        print("  say any such mechanism will help. The incumbent")
        print("  M-static-cached remains the adversary, and five previous")
        print("  attempts to add machinery lost to rules that remove it.")
    elif not breaks:
        print("\n  ONE REGIME. The same memoryless law fits both boards, so the")
        print("  second board adds a design but not a phenomenon. The gate does")
        print("  NOT pass, and the structural argument for pfifo's curve —")
        print("  correlated events plus a warm-up — is wrong or too weak to")
        print("  show at this sample size. H stays closed.")
        print("\n  Do not respond by hunting for a statistic that separates")
        print("  them. Respond by building a board whose detection genuinely")
        print("  is not memoryless, and committing it before measuring.")
    else:
        print("\n  NEITHER board fits the memoryless law. That is a statement")
        print("  about the null, not about transfer: a law that fits nothing")
        print("  cannot distinguish the two curves. The comparison is NOT")
        print("  EVALUABLE as specified, and replacing the null now would be")
        print("  choosing a model after seeing the data.")


if __name__ == "__main__":
    main()

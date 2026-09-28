"""S1 — is dfifo a genuinely REACHABILITY-LIMITED stochastic regime?

This is the third stochastic board and the first one that can be wrong in an
interesting way. sat_mac and pfifo could only be checked against
"0 < P(detect) < 1"; both passed, and the Stochastic Generalization Gate still
failed because one memoryless law fitted both. The diagnosis was quantitative:
pfifo's occupancy chain mixed in ~12 cycles while its corner arrived once in
~1556, so the state was forgotten many times over between opportunities and
successive trials were effectively independent.

dfifo is built to put that ratio at O(1) — E[T_hit]/t_mix = 4.7 — so detection
is a FIRST-PASSAGE event rather than a Bernoulli trial. That buys something the
earlier boards did not have: a full predicted distribution. The empirical curve
can disagree with theory.py, and if it does the benchmark is broken rather than
merely uninteresting.

This script computes no efficiency, compares no policies, and promotes nothing.
It answers three questions:

    1. is the board non-degenerate at the committed budget?
    2. does the measured first-passage distribution match the kernel?
    3. does the COLD-START regime differ from the NEAR-STATIONARY one?

WHAT WAS COMMITTED, AND WHEN
-----------------------------
The campaign budget B = 668 is ceil(E[T_hit | q0 = 0]) computed in theory.py by
exact linear solve. It was fixed BEFORE the RTL was written and long before any
seed ran, which is the whole defence against the move Experiment 15 refused by
name. A budget taken from the kernel cannot have been tuned toward a detection
rate nobody had yet measured. Theory predicts P(T_hit <= B) = 0.630; whether the
board delivers that is the measurement, not the assumption.

The initial-state distribution is part of the committed design, not an
implementation detail, because it decides which question is being asked:

    q0 = 0                  first passage  — time to physically REACH the rare
                            region, with a genuine dead zone
    q0 ~ Uniform{0..24}     recurrence     — how often an already-mixed chain
                            revisits a rare state

Those are different stochastic regimes. Measuring the second and reporting it as
the first would be a category error, so both are run and reported separately.

    python characterise.py [--seeds N] [--mock]
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

import theory
from binomial import clopper_pearson, non_degenerate
from eligibility import STOCHASTIC, admit
from evidence_channels import SimChannel
from preregistration import Commitment

RTL = os.path.join(HERE, "rtl")
SIM = os.path.join(HERE, "sim")
SOURCES = [os.path.join(RTL, f) for f in
           ("dfifo.sv", "dfifo_mut.sv", "dfifo_wrap.sv", "dfifo_wrap_mut.sv")]
SOURCES.append(os.path.join(SIM, "tb_dfifo.sv"))

# S1b. The first commitment (commit_dfifo_s1.json) is KEPT, not edited: it
# recorded a real run whose verdict was NOT ADMITTED, and deleting it would
# erase the fact that the criterion was wrong. This is a new pre-registration
# with a corrected model, not a revision of the old one.
COMMIT_PATH = os.path.join(HERE, "commit_dfifo_s1b.json")

B         = theory.BUDGET_DETECT   # 724 = ceil(E[T_detect]), corrected kernel
SHAPE     = theory.SHAPE_HORIZON   # 3000
# FRESH SEEDS. Seeds 9000-9023 have been seen; reusing them would make this a
# test of a model fitted to data already in hand. Everything else about the
# corrected commitment could be argued to have been shaped by that run, so the
# seed set is the one protection that cannot be argued with.
SEED_BASE = 11000
DEFAULT_SEEDS = 24
MIN_EACH  = 2

SPEC = {
    "design": "dfifo — depth-24 FIFO, balanced push/pop occupancy walk",
    "mutant": "full comparison >= written as > (one character)",
    "activation": "iff occupancy reaches DEPTH; designs bit-identical below it",
    "kernel": {
        "depth": theory.D,
        "push_threshold": theory.PUSH_THRESHOLD,
        "pop_threshold": theory.POP_THRESHOLD,
        "p_push": theory.P_PUSH, "p_pop": theory.P_POP, "p_idle": theory.P_IDLE,
    },
    "budget_B": B,
    "budget_derivation": "ceil(E[T_detect | q0=0]) from the DETECTION kernel, "
                         "by exact linear solve",
    "observed_quantity": "T_detect — the first push accepted at full. Reaching "
                         "DEPTH is necessary but NOT sufficient: the next draw "
                         "is as likely to be a pop, which returns the walk to "
                         "DEPTH-1 where the designs are bit-identical.",
    "supersedes": "commit_dfifo_s1.json, whose coherence criterion asserted "
                  "detection iff reaching DEPTH. That is false in one "
                  "direction and the run came back NOT ADMITTED on a seed "
                  "that reached the boundary without a subsequent push.",
    "predicted_P_detect_at_B": None,   # filled below, from the corrected kernel
    "shape_horizon": SHAPE,
    "conditions": {
        "primary": "cold start, q0 = 0 (first passage)",
        "control": "q0 ~ Uniform{0..24}, the exact stationary law (recurrence)",
    },
    "seed_base": SEED_BASE,
    "seeds": DEFAULT_SEEDS,
    "admission": {
        "min_detections": MIN_EACH,
        "min_misses": MIN_EACH,
        "positive_control": "good DUT passes on every seed in every condition",
        "coherence": "detection IMPLIES the occupancy reached DEPTH (one "
                     "direction only). The converse is false and its rate is "
                     "predicted, not forbidden.",
        "stimulus_identity": "T_hit must agree between variants on the same seed",
    },
}

NOTES = (
    "Committed before any dfifo campaign ran. B comes from the transition "
    "kernel, not from observed seeds; theory.py was solved first and the RTL "
    "written afterwards.\n"
    "The initial-state distribution is committed because it selects the "
    "question: cold start measures first passage, stationary start measures "
    "recurrence of a rare state. Reporting one as the other would be a category "
    "error, so both run.\n"
    "FALSIFIABLE IN A WAY THE EARLIER BOARDS WERE NOT: the kernel predicts a "
    "full first-passage distribution, four level milestones scaling as k^2, and "
    "P(T_hit <= B) = 0.630. Any of those can come out wrong, and if they do the "
    "board is broken rather than uninteresting. Extending B or redrawing the "
    "stimulus to rescue a disagreement is the Experiment 15 refusal wearing a "
    "different hat."
)


def channel(mock, stationary=False):
    """Fresh channel per condition. Campaign length is compiled in via -DNVEC
    and the start mode via -DSTATIONARY, so each combination is a different
    binary; SimChannel keys its build cache on (variant, nvec), and the start
    mode is separated by using a distinct channel rather than trusting that."""
    def defines(bug):
        d = [f"DUT={'dfifo_wrap_mut' if bug else 'dfifo_wrap'}"]
        if stationary:
            d.append("STATIONARY=1")
        return d
    return SimChannel(mock=mock, sources=SOURCES, top="tb_dfifo",
                      defines_for=defines, covers=r"^dfifo\.",
                      mock_finds_bug=True)


_STIM2 = re.compile(r"STIM2 npush=(\d+) npop=(\d+) nidle=(\d+) cross=(-?\d+)")

_STIM = re.compile(r"STIM q0=(-?\d+) thit=(-?\d+) tdet=(-?\d+) maxq=(\d+) "
                   r"lvl6=(-?\d+) lvl12=(-?\d+) lvl18=(-?\d+) of (\d+)")


def stim(ev):
    """Parse the instrumentation line. Returns None when absent rather than
    substituting zeros — a missing measurement and a measurement of zero are
    different things, and conflating them is how a broken harness reads as a
    healthy board."""
    m = _STIM.search(ev.raw or "")
    if not m:
        return None
    q0, thit, tdet, maxq, l6, l12, l18, n = (int(g) for g in m.groups())
    out = dict(q0=q0, thit=thit, tdet=tdet, maxq=maxq,
               lvl6=l6, lvl12=l12, lvl18=l18, n=n)
    m2 = _STIM2.search(ev.raw or "")
    if m2:
        npu, npo, nid, cr = (int(g) for g in m2.groups())
        out.update(npush=npu, npop=npo, nidle=nid, cross=cr)
    return out


def report_stimulus(rows, label):
    """Is the stimulus the chain the theory solves?

    Two assumptions underpin every predicted number: the three step
    probabilities, and the steps being INDEPENDENT. The marginals are checked
    against the committed thresholds; the lag-1 correlation checks the second,
    which is the one that bit sat_mac -- there the marginals were exactly
    uniform and the JOINT rate ran 2x its prediction.

    Runs unconditionally. A control that fires only when a derived statistic
    already looks wrong never confirms the healthy case, and that pattern has
    now appeared five separate times in this project.
    """
    have = [r for r in rows if r.get("g") and "npush" in r["g"]]
    if not have:
        print(f"    {label}: no STIM2 line — rebuild the testbench")
        return None
    npu = sum(r["g"]["npush"] for r in have)
    npo = sum(r["g"]["npop"] for r in have)
    nid = sum(r["g"]["nidle"] for r in have)
    cr  = sum(r["g"]["cross"] for r in have)
    n   = npu + npo + nid
    pairs = n - len(have)          # one lost pair boundary per campaign
    exp_u = theory.P_PUSH
    exp_i = theory.P_IDLE
    se = lambda pr: (pr * (1 - pr) / n) ** 0.5
    print(f"    {label}  ({n:,} steps over {len(have)} campaigns)")
    for name, cnt, exp in (("push", npu, exp_u), ("pop", npo, theory.P_POP),
                           ("idle", nid, exp_i)):
        obs = cnt / n
        z = (obs - exp) / se(exp)
        print(f"      p_{name:<5} = {obs:.5f}   committed {exp:.5f}   "
              f"{z:+.2f} sigma")
    mean = (npu - npo) / n
    var  = (npu + npo) / n - mean * mean
    cov  = cr / pairs - mean * mean
    r1   = cov / var if var else 0.0
    z1   = r1 * (pairs ** 0.5)
    print(f"      lag-1 corr of the step sequence = {r1:+.4f}   "
          f"({z1:+.2f} sigma)")
    ok = abs(z1) < 3.0
    if not ok:
        print("      -> the steps are NOT independent. The kernel assumes they")
        print("         are, so every predicted number above describes a")
        print("         different chain than the one that ran.")
    return dict(n=n, r1=r1, z1=z1, ok=ok)


def run_condition(mock, seeds, nvec, stationary, both=True):
    """One condition. Returns per-seed rows."""
    ch = channel(mock, stationary)
    rows = []
    for s in range(seeds):
        seed = SEED_BASE + s
        g = ch.run(inject_bug=False, seed=seed, nvec=nvec, phi="dfifo.order")
        row = {"seed": seed, "good": g.status, "g": stim(g)}
        if both:
            d = ch.run(inject_bug=True, seed=seed, nvec=nvec, phi="dfifo.order")
            row["mut"] = d.status
            row["m"] = stim(d)
            row["caught"] = (d.status == "counterexample")
        rows.append(row)
    return rows


def ks_against_theory(hits, horizon):
    """Max deviation between the empirical and predicted first-passage CDFs.

    Reported with the 95% Kolmogorov-Smirnov critical value for this n. This is
    description backed by a threshold, not a calibrated p-value: the samples are
    independent across seeds, but with n = 24 the test has limited power and a
    pass is weak evidence of agreement rather than proof of it. Said here so the
    number is not over-read later.
    """
    C = theory.hitting_cdf(horizon)
    obs = sorted(h for h in hits if h is not None and h >= 0)
    n = len(obs)
    if n == 0:
        return None
    worst, at = 0.0, 0
    for i, t in enumerate(obs):
        pred = C[min(t, horizon) - 1] if t >= 1 else 0.0
        for emp in (i / n, (i + 1) / n):
            if abs(emp - pred) > worst:
                worst, at = abs(emp - pred), t
    crit = 1.36 / (n ** 0.5)
    return dict(d=worst, at=at, n=n, crit=crit, ok=worst < crit)


def main():
    mock = "--mock" in sys.argv
    seeds = DEFAULT_SEEDS
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])

    C = theory.hitting_cdf(SHAPE)
    spec = dict(SPEC, seeds=seeds)
    CD = theory.detection_cdf(SHAPE)
    spec["predicted_P_detect_at_B"] = round(CD[B - 1], 4)
    commit = Commitment("dfifo S1 characterisation", COMMIT_PATH, spec,
                        NOTES).commit()

    print("=== S1: dfifo as a REACHABILITY-LIMITED stochastic benchmark ===")
    print(theory.summary())
    print()
    print(commit.render())
    print(f"    seeds     : {seeds} (from {SEED_BASE})   "
          f"mode = {'MOCK' if mock else 'REAL'}")
    if not commit.intact:
        print("\n  The committed configuration was modified after hashing. INVALID.")
        return
    if mock:
        print("\n    NOTE: mock returns a fixed verdict and emits no STIM line,")
        print("          so it cannot characterise anything. Use --real.")

    # ---- PRIMARY: cold start, at the committed budget ----------------------
    print(f"\n  [1/3] cold start, B = {B} cycles ...", flush=True)
    cold = run_condition(mock, seeds, B, stationary=False)

    bad_ctrl = [r["seed"] for r in cold if r["good"] != "pass"]
    print(f"\n  positive control (good DUT)      : "
          f"{'PASS' if not bad_ctrl else f'FAIL on {bad_ctrl[:3]}'}")
    if bad_ctrl:
        first = next(r for r in cold if r["good"] != "pass")
        print(f"    first failure status = {first['good']}")
        print("\n  The checker is unvalidated, so nothing below means anything.")
        print("  NOT ADMITTED.")
        return

    have = [r for r in cold if r["g"] and r.get("m")]
    if not have:
        print("\n  No STIM lines parsed — the harness did not report. Rebuild.")
        print("  NOT ADMITTED.")
        return

    # Stimulus identity. The two variants are the same design below the top, so
    # on a given seed the occupancy trajectory must be identical. If it is not,
    # the mutation reaches further than one character's worth and T_hit stops
    # being a property of the stimulus alone — which would invalidate using the
    # good variant's trajectory to explain the mutant's detections.
    mismatched = [r["seed"] for r in have if r["g"]["thit"] != r["m"]["thit"]]
    print(f"  T_hit agrees across variants     : "
          f"{'PASS' if not mismatched else f'FAIL on {mismatched[:3]}'}")

    print("\n  STIMULUS CHARACTERISATION (always run, not only on suspicion)")
    stim_ok = report_stimulus(cold, "cold start")

    hits   = [r["g"]["thit"] for r in have]
    caught = sum(1 for r in have if r["caught"])
    n      = len(have)
    p      = caught / n
    lo, hi = clopper_pearson(caught, n)
    pred   = CD[B - 1]
    print(f"\n  P(detect | B={B})                : {p:.3f}  "
          f"[{lo:.3f}, {hi:.3f}]   theory says {pred:.3f}"
          f"   (detection kernel)")
    print(f"  for contrast, P(reach depth)     : theory {C[B-1]:.3f} — the "
          f"quantity the FIRST commitment wrongly used")
    print(f"  campaigns reaching q=DEPTH       : "
          f"{sum(1 for h in hits if h >= 0)}/{n}")

    # coherence: detection iff the occupancy reached the top
    bad_det  = [r["seed"] for r in have if r["caught"] and r["g"]["thit"] < 0]
    bad_miss = [r["seed"] for r in have if not r["caught"] and r["g"]["thit"] >= 0]
    print(f"  detected WITHOUT reaching depth  : {bad_det if bad_det else 'none'}"
          f"   <- a real failure; the designs are identical below the boundary")
    exp_miss = (sum(1 for r in have if r["g"]["thit"] >= 0)) * \
               (1 - CD[B - 1] / C[B - 1]) if C[B - 1] else 0.0
    print(f"  reached depth but NOT detected   : {len(bad_miss)} "
          f"(predicted ~{exp_miss:.1f})   <- EXPECTED, not a failure: the "
          f"horizon can end between reaching the boundary and a push landing "
          f"there")

    lags = [r["m"]["tdet"] - r["m"]["thit"]
            for r in have if r["caught"] and r["m"]["thit"] >= 0
            and r["m"]["tdet"] >= 0]
    if lags:
        print(f"  detection lag after activation   : mean {sum(lags)/len(lags):.1f}, "
              f"max {max(lags)} cycles   (first-passage scale ~{theory.expected_first_passage()[0]:.0f})")

    ok, why = non_degenerate(caught, n, MIN_EACH)
    print(f"  non-degenerate                   : {'yes' if ok else 'no'} — {why}")

    # ---- level milestones vs the k^2 prediction ---------------------------
    print("\n  [2/3] level milestones (balanced walk predicts E[T] ~ k^2)")
    lv = theory.level_first_passage()
    print(f"    {'level':>6} {'measured mean':>15} {'theory':>10} {'ratio':>8}")
    for key, k in (("lvl6", 6), ("lvl12", 12), ("lvl18", 18), ("thit", 24)):
        vals = [r["g"][key] for r in have if r["g"][key] >= 0]
        if not vals:
            print(f"    {k:>6} {'never reached':>15}")
            continue
        mean = sum(vals) / len(vals)
        print(f"    {k:>6} {mean:>15.1f} {lv[k]:>10.1f} {mean/lv[k]:>8.2f}"
              f"   (n={len(vals)})")
    print("    NOTE: means over campaigns that reached the level are CENSORED at")
    print("    B, so the higher levels read low by construction. The uncensored")
    print("    comparison is the shape run below.")

    # ---- SHAPE: long horizon, good variant only ---------------------------
    print(f"\n  [3/3] first-passage distribution, horizon {SHAPE} ...", flush=True)
    shape = run_condition(mock, seeds, SHAPE, stationary=False, both=False)
    sh = [r["g"]["thit"] for r in shape if r["g"]]
    reached = [t for t in sh if t >= 0]
    if reached:
        mean = sum(reached) / len(reached)
        et = theory.expected_first_passage()[0]
        print(f"    reached depth : {len(reached)}/{len(sh)}  "
              f"(theory: {C[SHAPE-1]:.3f})")
        print(f"    mean T_hit    : {mean:,.1f}   theory {et:,.1f}   "
              f"ratio {mean/et:.2f}")
        ks = ks_against_theory(sh, SHAPE)
        if ks:
            print(f"    KS deviation  : D = {ks['d']:.3f} at t = {ks['at']}, "
                  f"95% critical {ks['crit']:.3f} -> "
                  f"{'consistent' if ks['ok'] else 'INCONSISTENT'}")

        # CROSS-RUN CONSISTENCY, free and surprisingly strong. The B run and
        # this one use the same seeds and the same stimulus; only the loop
        # count differs. So wherever the short run observed a hit, the long run
        # must report the SAME T_hit. If they disagree, campaign length is
        # changing the stimulus -- which would mean nvec is not just a horizon
        # and every comparison across horizons is meaningless. This is the
        # defect that made a build cache key wrong once already.
        by_seed = {r["seed"]: r["g"]["thit"] for r in shape if r["g"]}
        disagree = [r["seed"] for r in have
                    if r["g"]["thit"] >= 0
                    and by_seed.get(r["seed"]) != r["g"]["thit"]]
        print(f"    T_hit matches the B run : "
              f"{'PASS' if not disagree else f'FAIL on {disagree[:3]}'}")

    # ---- CONTROL: near-stationary start -----------------------------------
    print(f"\n  control: near-stationary start, same budget B = {B} ...",
          flush=True)
    stat = run_condition(mock, seeds, B, stationary=True)
    sg = [r for r in stat if r["g"] and r.get("m")]
    if sg:
        sc = sum(1 for r in sg if r["caught"])
        slo, shi = clopper_pearson(sc, len(sg))
        print(f"    P(detect | stationary start)  : {sc/len(sg):.3f}  "
              f"[{slo:.3f}, {shi:.3f}]")
        q0s = [r["g"]["q0"] for r in sg]
        print(f"    q0 drawn                      : mean {sum(q0s)/len(q0s):.1f} "
              f"of a uniform {{0..24}} (expected 12.0)")
        print("    The two conditions are DIFFERENT QUESTIONS. A higher rate here")
        print("    is expected and is not evidence about first passage: starting")
        print("    mid-chain removes most of the dead zone. Reported so the")
        print("    primary number cannot be quietly replaced by this one.")

    # ---- admission ---------------------------------------------------------
    # Only the forbidden direction counts. A campaign that reached the boundary
    # and ended before a push landed there is predicted by the kernel, not a
    # defect -- treating it as one is what made the previous run NOT ADMITTED.
    coherent = not bad_det and not mismatched
    if stim_ok is not None and not stim_ok["ok"]:
        coherent = False
    elig = admit("dfifo board", STOCHASTIC,
                 distinct_outcomes=2 if ok else 1,
                 controls_armed=(not bad_ctrl) and coherent)
    print()
    print(elig.render())

    print("\n=== what this obliges ===")
    if elig.eligible:
        print(f"  ADMITTED as a stochastic benchmark at the COMMITTED budget.")
        print("  Detection here is a first-passage event, not a Bernoulli trial:")
        print("  the hazard depends on where the occupancy currently sits, so")
        print("  two campaigns of equal elapsed time have materially different")
        print("  remaining detection probability.")
        print("\n  That is what makes the next step worth doing. It does NOT say")
        print("  the Stochastic Generalization Gate passes — that is S2's")
        print("  question, and it needs the memoryless law to FIT sat_mac and be")
        print("  EXCLUDED here. H stays closed until then.")
    else:
        print("  NOT ADMITTED — see the reasons above.")
        print("\n  The honest response is not to extend B or redraw the stimulus.")
        print("  B came from the kernel before any seed ran; moving it now to")
        print("  rescue the result is the refused move wearing a different hat.")


if __name__ == "__main__":
    main()

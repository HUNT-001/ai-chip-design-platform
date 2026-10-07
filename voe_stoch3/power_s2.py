r"""How many first-passage samples S2 needs to refute the memoryless law.

WHY THIS FILE EXISTS, AND WHY IT EXISTS BEFORE S2 RUNS. S1 established that the
depth-24 occupancy walk has an exact first-passage law with a dead zone, and
that a geometric fit is the wrong shape for it. That is a statement about two
distributions. It is not yet a statement about an experiment, because an
experiment has a finite sample size, and a shape difference I cannot resolve at
the N I can afford is a shape difference I have no business claiming.

So this file answers the only question that decides S2's sample size:

    at what N independent first-passage samples does the early-window hazard
    statistic separate the exact law from a fitted geometric, at alpha = 1e-3,
    with power >= 0.9?

and it answers it from the kernel alone. THIS SCRIPT READS NO BOARD. It does not
import the RTL, does not invoke a simulator, does not open the S1 commit files,
and never samples from a run. Both arms of the Monte-Carlo are drawn from
theoretical distributions: the alternative from theory.hitting_cdf, the null from
the geometric fitted to that law's mean. That ordering is the point. An N derived
from observed data is an N that could have been walked up until the result
appeared, which is the move Experiment 15 refused by name. An N derived from the
kernel before the first seed is run cannot have been.

THE NULL. "Memoryless" is not a single distribution, it is a family, so the null
has to be the member a practitioner would actually fit. Fitting a geometric by
maximum likelihood to first-passage data sets 1/p to the sample mean, so the
null here is Geometric(p0) with p0 = 1 / E[T_hit] = 1/667.8. This is the
strongest honest null: it already agrees with the exact law on the mean, so no
part of the power below comes from a location difference. All of it comes from
shape.

THE STATISTIC. The exact law cannot produce a sample below t = D = 24 at all --
reaching the top needs 24 net upward moves -- and stays far below the fitted
geometric for a long while after. A geometric has constant hazard by
construction. So the discriminating quantity is the hazard inside an early
window, measured against the hazard the fit asserts everywhere:

    Lambda = [ S / sum_i min(T_i, W) ]  /  [ N / sum_i T_i ]
             \_______ early-window _______/   \___ fitted ___/
                     hazard                    global hazard

with S the number of samples at or below W. Under the geometric null the
memoryless property puts Lambda at ~1 whatever p is. Under the exact law the
dead zone drives it toward 0. Lambda is a ratio of two rates estimated from the
same sample, so it is scale-free: it does not require knowing the mean, and the
test does not become a test of the mean by the back door.

THE WINDOW IS A COMMITMENT, NOT A FREE PARAMETER. W is fixed by a rule stated
before any search over N -- the window in which the EXACT law places 1% of its
mass, W = 86 cycles -- and the reported N is the one that rule yields. I print a
sensitivity table over neighbouring W afterwards because a reader is entitled to
know whether the answer hangs on that choice, but the committed N is the one
from the committed W. Picking W to minimise N would be the same sin as picking
the horizon to maximise a detection rate.

CALIBRATION. alpha = 1e-3 cannot be read off a normal approximation here: S is a
small count, so Lambda is coarsely discrete and has an atom at 0 (every sample
with S = 0). The critical value is therefore the exact Monte-Carlo one -- the
largest threshold on the simulated null whose attained size is still <= alpha --
and I report the attained size next to it, because with an atom that large the
attained size can sit well under the nominal one, and a test that is quietly
more conservative than advertised needs stating rather than rounding away.

    /usr/bin/python3 power_s2.py          # prints the committed N

NOTE ON THE INTERPRETER. The repo's `python3` is oss-cad-suite `tabbypy3` and has
no numpy. This script needs it for the Monte-Carlo, so run it with
`/usr/bin/python3`, which has numpy 1.26.
"""
from __future__ import annotations

import sys

try:
    import numpy as np
except ImportError:                                    # pragma: no cover
    sys.exit("power_s2.py needs numpy for the Monte-Carlo; the repo's python3 "
             "(oss-cad-suite tabbypy3) has none. Run it with /usr/bin/python3.")

import theory

# --------------------------------------------------------------------------- #
# Pre-registered constants. Every one of these is fixed before the search.     #
# --------------------------------------------------------------------------- #
ALPHA        = 1e-3        # one-sided, lower tail of Lambda
TARGET_POWER = 0.90
WINDOW_MASS  = 0.01        # the rule that fixes W: 1% of the EXACT law's mass
HORIZON      = 20_000      # exact CDF support; residual tail is 2.1e-14
NULL_REPS    = 200_000     # ~200 draws below the 1e-3 quantile
ALT_REPS     = 400_000    # power SE ~ 0.0005 at the crossover: ~1/26 of
                          # the power gap between consecutive N, so the
                          # committed N is not a Monte-Carlo coin flip
SEED         = 20260407    # fixed so the committed N is reproducible
N_CEILING    = 4_000       # if the rule needs more than this, S2 is infeasible
CENSOR_DFIFO = theory.SHAPE_HORIZON   # 3000; where the theory puts 99.5% of mass


def exact_cdf(horizon: int = HORIZON):
    """theory.hitting_cdf as a numpy array. The only source of the alternative."""
    return np.asarray(theory.hitting_cdf(horizon), dtype=np.float64)


def window_from_mass(cdf, mass: float = WINDOW_MASS) -> int:
    """The smallest W with F_exact(W) >= mass. The committed window rule."""
    return int(np.searchsorted(cdf, mass, side="left") + 1)


def draw_exact(rng, cdf, reps: int, n: int):
    """`reps` x `n` samples from the exact law, by inverting its CDF.

    The residual tail above HORIZON (2e-14) lands on HORIZON itself. That is a
    rounding of the far tail; the statistic reads the early window, so it cannot
    matter here, but it is a truncation and not a sample from the true law.
    """
    u = rng.random((reps, n))
    return np.searchsorted(cdf, u, side="left") + 1


def draw_geometric(rng, p0: float, reps: int, n: int):
    """`reps` x `n` samples from the fitted memoryless null."""
    return rng.geometric(p0, size=(reps, n))


def hazard_ratio(T, w: int, c: int):
    """Lambda for each row of T, with the campaign censored at `c`.

    Both rates are right-censored exponential MLEs, D / sum(min(T_i, horizon)),
    which is what an experiment can actually compute: a campaign that ends at c
    does not observe T_i > c, it observes only that T_i exceeded c. The earlier
    draft used N / sum(T_i) for the denominator, which silently assumed every
    seed detected. For dfifo at c = SHAPE_HORIZON that assumption is nearly true
    (0.5% censored) and the numbers do not move. For sat_mac it is false by a
    mile -- 74% of seeds never detect -- so the uncensored form would have
    overstated the global hazard by ~4x and flattered the test.

    Returns (Lambda, D). Where D = 0 the fitted hazard is 0, Lambda is 0/0, and
    the row gets NaN: the statistic is not computable on a campaign that
    detected nothing, and I would rather carry that through and report it than
    map it to a number that implies the test ran.
    """
    T = T.astype(np.float64, copy=False)
    s = (T <= w).sum(axis=1)
    risk = np.minimum(T, w).sum(axis=1)
    d = (T <= c).sum(axis=1)
    expo = np.minimum(T, c).sum(axis=1)
    out = np.full(T.shape[0], np.nan)
    live = d > 0
    out[live] = (s[live] / risk[live]) / (d[live] / expo[live])
    return out, d


def _chunks(reps: int, n: int, budget: int = 4_000_000):
    """Row blocks keeping reps*n under `budget` cells, so memory stays flat."""
    per = max(1, budget // max(n, 1))
    done = 0
    while done < reps:
        take = min(per, reps - done)
        yield take
        done += take


def simulate(rng, draw, reps: int, n: int, w: int, censor: int):
    """Lambda over `reps` campaigns, and the mean detection count D."""
    lam, dets = [], []
    for block in _chunks(reps, n):
        L, d = hazard_ratio(draw(rng, block, n), w, censor)
        lam.append(L)
        dets.append(d)
    return np.concatenate(lam), float(np.concatenate(dets).mean())


def critical_value(null_lambda, alpha: float = ALPHA):
    """Largest threshold whose attained size is still <= alpha, and that size.

    Lambda is discrete with an atom at 0, so I take the threshold from the
    simulated null's own distinct values rather than interpolating a quantile
    that falls inside the atom and overstates the size.
    """
    null_lambda = null_lambda[~np.isnan(null_lambda)]
    if null_lambda.size == 0:
        return None, 0.0
    order = np.sort(null_lambda)
    uniq = np.unique(order)
    attained = np.searchsorted(order, uniq, side="right") / order.size
    ok = attained <= alpha
    if not ok.any():
        return None, 0.0            # no rejection region exists at this N
    i = int(np.flatnonzero(ok)[-1])
    return float(uniq[i]), float(attained[i])


def power_at(n: int, w: int, cdf, p0: float, seed: int = SEED,
             censor: int = None):
    """(power, attained alpha, critical value) at sample size n."""
    censor = CENSOR_DFIFO if censor is None else censor
    rng = np.random.default_rng(seed + n)
    null, _ = simulate(rng, lambda r, c, k: draw_geometric(r, p0, c, k),
                       NULL_REPS, n, w, censor)
    crit, attained = critical_value(null)
    if crit is None:
        return 0.0, 0.0, None
    alt, _ = simulate(rng, lambda r, c, k: draw_exact(r, cdf, c, k),
                      ALT_REPS, n, w, censor)
    return float(np.nansum(alt <= crit) / alt.size), attained, crit


def smallest_n(w: int, cdf, p0: float, verbose: bool = True):
    """Least n with power >= TARGET_POWER: bracket by doubling, then bisect.

    Power is not perfectly monotone in n -- the discreteness of S makes it
    jagged -- so after bisecting I confirm the neighbourhood above the crossover
    rather than trusting a single point.
    """
    lo, hi = 1, 8
    while hi <= N_CEILING:
        pw = power_at(hi, w, cdf, p0)[0]
        if verbose:
            print(f"    bracket N={hi:<5} power={pw:.4f}")
        if pw >= TARGET_POWER:
            break
        lo, hi = hi, hi * 2
    else:
        return None

    while lo + 1 < hi:
        mid = (lo + hi) // 2
        pw = power_at(mid, w, cdf, p0)[0]
        if verbose:
            print(f"    bisect  N={mid:<5} power={pw:.4f}")
        if pw >= TARGET_POWER:
            hi = mid
        else:
            lo = mid
    return hi


def dfifo_arm() -> int:
    cdf = exact_cdf()
    mean_exact = theory.expected_first_passage()[0]
    p0 = 1.0 / mean_exact
    w = window_from_mass(cdf)

    print("S2 sample-size pre-registration -- theory only, no board read")
    print("=" * 62)
    print(f"alternative      exact first-passage law, theory.hitting_cdf")
    print(f"null             Geometric(p0), p0 = 1/E[T_hit] = {p0:.6f}")
    print(f"E[T_hit]         {mean_exact:,.2f} cycles   (dead zone: no mass below "
          f"t = {theory.D})")
    print(f"committed window W = {w} cycles   "
          f"(rule: F_exact(W) >= {WINDOW_MASS:.0%}, fixed before the search)")
    print(f"                 F_exact(W) = {cdf[w-1]:.5f}   "
          f"F_geom(W) = {1-(1-p0)**w:.5f}")
    print(f"test             one-sided on Lambda, alpha = {ALPHA:g}, "
          f"target power = {TARGET_POWER:.0%}")
    print(f"Monte-Carlo      {NULL_REPS:,} null reps / {ALT_REPS:,} alternative "
          f"reps, seed {SEED}")
    print()
    print("search")
    n = smallest_n(w, cdf, p0)
    if n is None:
        print(f"\nno N <= {N_CEILING:,} reaches power {TARGET_POWER:.0%}.")
        return 1

    pw, attained, crit = power_at(n, w, cdf, p0)
    se = (pw * (1.0 - pw) / ALT_REPS) ** 0.5
    below = power_at(n - 1, w, cdf, p0)[0]
    gap = pw - below
    print()
    print(f"committed N      {n}")
    print(f"  power          {pw:.4f} +/- {se:.4f} (MC SE)   "
          f"(>= {TARGET_POWER:.0%})")
    print(f"  margin         {(pw - TARGET_POWER) / se:.1f} SE above the "
          f"{TARGET_POWER:.0%} target")
    print(f"  attained alpha {attained:.2e}  (nominal {ALPHA:g}; "
          f"reject when Lambda <= {crit:.4f})")
    print(f"  N-1 = {n-1:<5}    power {below:.4f}  <- below target, so N is tight")
    print(f"  gap to N-1     {gap:.4f} = {gap / se:.0f} x SE   "
          f"(the crossover is resolved, not noise)")
    print()
    print("  neighbourhood above the crossover (jaggedness check):")
    for k in range(n, n + 6):
        p_k, a_k, _ = power_at(k, w, cdf, p0)
        flag = "" if p_k >= TARGET_POWER else "   <- DIPS BELOW TARGET"
        print(f"    N={k:<5} power={p_k:.4f}  alpha={a_k:.2e}{flag}")
    print()
    print("  sensitivity to W (reported, NOT used to choose N):")
    for mass in (0.002, 0.005, 0.01, 0.02, 0.05):
        w_m = window_from_mass(cdf, mass)
        n_m = smallest_n(w_m, cdf, p0, verbose=False)
        mark = "   <- committed" if abs(mass - WINDOW_MASS) < 1e-12 else ""
        print(f"    mass={mass:<6} W={w_m:<5} N={n_m}{mark}")
    print()
    print(f"N = {n}")
    return n


# --------------------------------------------------------------------------- #
# The sat_mac arm: what is a "fit" verdict on that board actually worth?       #
# --------------------------------------------------------------------------- #
# sat_mac passed S2 by fitting the memoryless law. That is a failure to reject,
# and a failure to reject is only as informative as the test's power -- which
# nobody had computed. So I compute it here, on the same statistic, with
# sat_mac's own numbers and no board read.
#
# THE CAMPAIGN IS NOT A FREE PARAMETER. C = 20000 vectors is the project default
# and tuning it is refused on the record; the ledger says so for sat_mac in
# as many words. So C stays at 20000 even though it is the thing that cripples
# the arm, and the crippling gets reported instead of designed away.
#
# WHAT GOES WRONG. The corner is 1 in 65536 but the campaign is 20000 vectors,
# so the mean first detection sits at 3.3x the horizon and only 26% of seeds
# detect at all. The hazard statistic needs detections in an early window; here
# the whole campaign IS the early window.
#
# THE ALTERNATIVE. "How big a dead zone could have hidden there?" needs a
# one-parameter family of shape deviations, so I use the dead-zone geometric:
# no mass below d, memoryless after it, with the rate re-fitted to hold the mean
# at 65536. The deviation is then purely a dead zone -- delta = d / mean -- and
# none of the power comes from a location difference, exactly as in the dfifo
# arm. The window comes from the SAME committed rule, applied to the same place
# it was applied for dfifo: 1% of the ALTERNATIVE's mass.
P_SAT      = 1.0 / 65536.0     # the corner: (-128)x(-128), 1 pair in 65536
CENSOR_SAT = 20_000            # project default vector count. NOT tunable.
NULL_REPS_SAT = 200_000
ALT_REPS_SAT  = 100_000        # power SE ~ 0.00095 at the crossover


def sat_window(d: int) -> int:
    """The committed rule on the dead-zone alternative: smallest W with
    F_alt(W) >= WINDOW_MASS."""
    p = 1.0 / (65536.0 - d)
    return d + int(np.ceil(np.log1p(-WINDOW_MASS) / np.log1p(-p)))


def draw_deadzone(rng, d: int, reps: int, n: int):
    """T = d + Geometric(p'), p' set so E[T] stays at 1/P_SAT."""
    return d + rng.geometric(1.0 / (65536.0 - d), size=(reps, n))


def sat_power(d: int, n: int, seed: int = SEED):
    """(power, attained alpha, crit, W, E[D] null, E[D] alt, undefined frac)."""
    w = sat_window(d)
    rng = np.random.default_rng(seed + d)
    null, d_null = simulate(rng, lambda r, c, k: draw_geometric(r, P_SAT, c, k),
                            NULL_REPS_SAT, n, w, CENSOR_SAT)
    crit, attained = critical_value(null)
    if crit is None:
        return 0.0, 0.0, None, w, d_null, float("nan"), float("nan")
    alt, d_alt = simulate(rng, lambda r, c, k: draw_deadzone(r, d, c, k),
                          ALT_REPS_SAT, n, w, CENSOR_SAT)
    undef = float(np.isnan(alt).mean())
    return (float(np.nansum(alt <= crit) / alt.size), attained, crit, w,
            d_null, d_alt, undef)


def sat_arm(n: int) -> None:
    mean_sat = 1.0 / P_SAT
    print()
    print("sat_mac arm -- what a 'fit' verdict is worth there")
    print("=" * 62)
    print(f"null             Geometric(1/65536), mean {mean_sat:,.0f} cycles")
    print(f"campaign C       {CENSOR_SAT:,} vectors (project default; tuning it "
          f"is refused on the record)")
    print(f"seeds N          {n}  (from the dfifo arm)")
    print(f"  C / mean       {CENSOR_SAT / mean_sat:.3f}  <- the campaign ends at "
          f"a third of the mean")
    print(f"  E[D] under null {n * (1 - (1 - P_SAT) ** CENSOR_SAT):.2f} of {n} "
          f"seeds detect  (P = "
          f"{1 - (1 - P_SAT) ** CENSOR_SAT:.4f})")
    print()
    print("  detectable dead zone (alternative: no mass below d, mean held at "
          "65536):")
    print(f"  {'delta':>7} {'d':>8} {'W':>8} {'F_null(W)':>10} {'power':>8} "
          f"{'alpha':>9} {'E[D] alt':>9}")
    grid = [0.02, 0.05, 0.08, 0.10, 0.12, 0.13, 0.15, 0.18, 0.20, 0.25,
            0.28, 0.30, 0.35]
    rows = []
    for delta in grid:
        d = int(round(delta * mean_sat))
        pw, att, crit, w, dn, da, undef = sat_power(d, n)
        rows.append((delta, d, w, pw))
        fn = 1 - (1 - P_SAT) ** w
        note = ""
        if w > CENSOR_SAT:
            note = "   <- W past the campaign: blind"
        elif crit is None:
            note = "   <- no rejection region at this alpha"
        da_s = "n/a" if da != da else f"{da:.1f}"
        print(f"  {delta:>7.2f} {d:>8,} {w:>8,} {fn:>10.4f} {pw:>8.4f} "
              f"{att:>9.1e} {da_s:>9}{note}")

    inside = [r for r in rows if r[3] >= TARGET_POWER]
    if not inside:
        print()
        print(f"  NO dead zone is detectable at alpha = {ALPHA:g} with power "
              f"{TARGET_POWER:.0%} at N = {n}.")
        return

    lo_grid = max((r for r in rows if r[3] < TARGET_POWER
                   and r[0] < inside[0][0]), key=lambda r: r[0])
    a, b = lo_grid[0], inside[0][0]
    for _ in range(12):
        mid = 0.5 * (a + b)
        pw = sat_power(int(round(mid * mean_sat)), n)[0]
        if pw >= TARGET_POWER:
            b = mid
        else:
            a = mid
    d_star = int(round(b * mean_sat))
    pw, att, crit, w, dn, da, undef = sat_power(d_star, n)
    se = (pw * (1 - pw) / ALT_REPS_SAT) ** 0.5
    print()
    print(f"  smallest detectable dead zone")
    print(f"    delta        {b:.4f}  = {b:.1%} of the mean  (d = {d_star:,} "
          f"cycles)")
    print(f"    power        {pw:.4f} +/- {se:.4f} (MC SE)")
    print(f"    attained a   {att:.1e}  (nominal {ALPHA:g}; reject when "
          f"Lambda <= {crit:.4f})")
    print(f"    window W     {w:,} cycles   ({w / CENSOR_SAT:.0%} of the "
          f"campaign)")
    print(f"    undefined    {undef:.1%} of alternative campaigns detect "
          f"nothing (Lambda not computable)")
    upper = [r for r in rows if r[0] > b and r[3] < TARGET_POWER]
    if upper:
        du, wu = upper[0][0], upper[0][2]
        cause = ("the window has left the campaign entirely"
                 if wu > CENSOR_SAT else
                 f"the window fills {wu / CENSOR_SAT:.0%} of the campaign and "
                 f"the detections run out")
        print(f"    UPPER EDGE   power falls back below {TARGET_POWER:.0%} by "
              f"delta = {du:.2f} (W = {wu:,} of {CENSOR_SAT:,}):")
        print(f"                 {cause}.")
        print(f"                 So detectability is a WINDOW, not a threshold: "
              f"a dead zone can be too BIG to see,")
        print(f"                 because the evidence for one is detections "
              f"INSIDE the campaign and a large")
        print(f"                 dead zone leaves none.")
    print()
    print(f"  what the fit verdict is worth: it excludes dead zones of roughly "
          f"{b:.0%} of the mean")
    print(f"  and larger, up to the point the window leaves the campaign. "
          f"Anything smaller is invisible")
    print(f"  at N = {n}, and 'sat_mac fits the memoryless law' says nothing "
          f"about it.")


def main() -> int:
    which = [a for a in sys.argv[1:] if a in ("--dfifo", "--sat")]
    n = None
    if not which or "--dfifo" in which:
        n = dfifo_arm()
    if not which or "--sat" in which:
        sat_arm(n if n is not None else 79)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

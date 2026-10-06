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


def hazard_ratio(T, w: int):
    """Lambda for each row of T. See THE STATISTIC above."""
    T = T.astype(np.float64, copy=False)
    s = (T <= w).sum(axis=1)
    risk = np.minimum(T, w).sum(axis=1)
    total = T.sum(axis=1)
    n = T.shape[1]
    return (s / risk) / (n / total)


def _chunks(reps: int, n: int, budget: int = 4_000_000):
    """Row blocks keeping reps*n under `budget` cells, so memory stays flat."""
    per = max(1, budget // max(n, 1))
    done = 0
    while done < reps:
        take = min(per, reps - done)
        yield take
        done += take


def simulate(rng, draw, reps: int, n: int, w: int):
    return np.concatenate([hazard_ratio(draw(rng, c, n), w)
                           for c in _chunks(reps, n)])


def critical_value(null_lambda, alpha: float = ALPHA):
    """Largest threshold whose attained size is still <= alpha, and that size.

    Lambda is discrete with an atom at 0, so I take the threshold from the
    simulated null's own distinct values rather than interpolating a quantile
    that falls inside the atom and overstates the size.
    """
    order = np.sort(null_lambda)
    uniq = np.unique(order)
    attained = np.searchsorted(order, uniq, side="right") / order.size
    ok = attained <= alpha
    if not ok.any():
        return None, 0.0            # no rejection region exists at this N
    i = int(np.flatnonzero(ok)[-1])
    return float(uniq[i]), float(attained[i])


def power_at(n: int, w: int, cdf, p0: float, seed: int = SEED):
    """(power, attained alpha, critical value) at sample size n."""
    rng = np.random.default_rng(seed + n)
    null = simulate(rng, lambda r, c, k: draw_geometric(r, p0, c, k),
                    NULL_REPS, n, w)
    crit, attained = critical_value(null)
    if crit is None:
        return 0.0, 0.0, None
    alt = simulate(rng, lambda r, c, k: draw_exact(r, cdf, c, k),
                   ALT_REPS, n, w)
    return float((alt <= crit).mean()), attained, crit


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


def main() -> int:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

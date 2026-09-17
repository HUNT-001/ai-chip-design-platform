"""Exact binomial intervals, shared by every stochastic benchmark.

WHY THIS EXISTS AS A MODULE rather than a few lines in each characterisation
script. The cross-design transfer question — is pfifo's detection behaviour
genuinely different from sat_mac's, or does it only look different? — is
answered by comparing intervals. If the two boards computed their intervals
differently, any apparent difference would be partly an artifact of the
arithmetic. Sharing one implementation makes the comparison about the designs.

WHY CLOPPER-PEARSON rather than the Wald interval used in the first sat_mac
characterisation. Wald is p +/- z*sqrt(p(1-p)/n), and it is actively misleading
exactly where these benchmarks live: at p = 0 or p = 1 it reports a
zero-width interval, which would let a board that missed the bug on every one of
12 seeds claim P(detect) = 0.000 +/- 0.000 and be recorded as a certainty. The
sat_mac script labelled its Wald interval "rough" and that was honest, but rough
is not good enough to carry an admission decision or a cross-design claim.
Clopper-Pearson is exact and conservative, and at 0/12 it correctly reports
[0.000, 0.265] — an interval that refuses to rule out a perfectly usable rate.

No scipy: the corpus runs wherever Verilator does, and adding a numerical
dependency to a project whose whole argument is about trustworthy evidence would
be a poor trade. The incomplete beta below is a standard continued fraction,
inverted by bisection, and tests/test_failure_memory.py pins it against values
computed independently.
"""
from __future__ import annotations

_TINY = 1e-300


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    import math
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log1p(-x))
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(q: float, a: float, b: float) -> float:
    """Inverse of betainc in x, by bisection. Slow and obviously correct."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple:
    """Exact (lo, hi) interval for a binomial proportion k/n.

    Degenerate counts are handled by the closed forms rather than by the
    continued fraction: at k = 0 the lower bound is exactly 0, at k = n the
    upper bound is exactly 1, and inventing a symmetric interval there is the
    specific error this function exists to avoid.
    """
    if n <= 0:
        return 0.0, 1.0
    alpha = 1.0 - conf
    lo = 0.0 if k == 0 else _beta_ppf(alpha / 2.0, k, n - k + 1)
    hi = 1.0 if k == n else _beta_ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return lo, hi


def non_degenerate(k: int, n: int, min_each: int = 2) -> tuple:
    """Are there enough hits AND enough misses to call this stochastic?

    Returns (ok, reason). The threshold is a COUNT, not a probability: a point
    estimate of 0.08 from 1 hit in 12 seeds is one lucky campaign away from
    zero, and admitting a board on it would be admitting noise. Requiring at
    least `min_each` of both outcomes is the weakest statement of "the same
    action genuinely sometimes succeeds and sometimes does not" that survives a
    single seed changing its mind.
    """
    misses = n - k
    if k < min_each and misses < min_each:
        return False, (f"neither outcome occurs {min_each} times "
                       f"({k} hits, {misses} misses of {n})")
    if k < min_each:
        return False, (f"only {k} detection(s) in {n} seeds — need >= {min_each}. "
                       f"The bug is too rare to sample at this campaign length, "
                       f"which is a statement about the configuration, not a "
                       f"licence to shorten it until it is not")
    if misses < min_each:
        return False, (f"only {misses} miss(es) in {n} seeds — need >= {min_each}. "
                       f"The board is behaving as a deterministic classifier, "
                       f"which is the state that made Experiment 15 NOT EVALUABLE")
    return True, f"{k} detections and {misses} misses in {n} seeds"

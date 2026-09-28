"""The transition kernel of the depth-24 occupancy walk, solved exactly.

WHY THIS FILE EXISTS, AND WHY IT EXISTS FIRST. The campaign budget B for this
board is derived HERE, from the stochastic model, before any RTL was written and
long before any seed was run. That ordering is the whole defence against the
move Experiment 15 refused by name: shrinking the horizon until the bug is
sometimes missed. A budget taken from the kernel cannot have been tuned toward a
detection rate nobody had measured yet.

It also makes the board falsifiable in a way sat_mac and pfifo were not. Those
two could only be checked against "0 < P(detect) < 1". This one has a full
predicted first-passage distribution, so the empirical curve can disagree with
the theory — and if it does, the benchmark is wrong rather than merely
uninteresting.

THE CHAIN. Occupancy q_t on {0..D} with mutually exclusive draws:

    push  p_u = 115/256   q -> q+1   (refused, hence a hold, at q = D)
    pop   p_d = 115/256   q -> q-1   (a no-op, hence a hold, at q = 0)
    idle  p_i =  26/256   q -> q

The idle probability is not decoration. A pure +/-1 walk is periodic, so its
second eigenvalue is -1 in magnitude and "mixing time" is undefined; the holding
probability makes the chain aperiodic while leaving the O(D^2) first-passage
scale intact.

WHY D = 24 AND WHY BALANCED. Both first passage to the top and the mixing time
of a balanced one-dimensional walk scale as O(D^2), so

    E[T_hit] / t_mix = O(1)

which is precisely the regime the corpus lacked. pfifo failed the Stochastic
Generalization Gate because its ratio was ~130: the occupancy chain forgot its
state many times over between rare-event opportunities, so successive trials
were effectively independent and the memoryless law fit. Here the two timescales
are comparable by construction, so the state cannot be forgotten between
opportunities and there is a genuine dead zone — from q = 0 the walk CANNOT
reach q = D before enough net upward movement has accumulated. An exponential
hazard has no such structural constraint.

    python theory.py          # prints everything the pre-registration cites
"""
from __future__ import annotations

D      = 24
# The testbench draws one byte and thresholds it, so the achievable
# probabilities are multiples of 1/256. Writing 0.45 here and 115/256 in the
# RTL would make the theory and the design describe different chains, and any
# disagreement between predicted and measured curves would then be an artifact
# of the arithmetic rather than a fact about the board. 115/256 = 0.44921875.
PUSH_THRESHOLD = 115          # push  if  byte <  115
POP_THRESHOLD  = 230          # pop   if  115 <= byte < 230
P_PUSH = PUSH_THRESHOLD / 256.0
P_POP  = (POP_THRESHOLD - PUSH_THRESHOLD) / 256.0
P_IDLE = 1.0 - P_PUSH - P_POP


def kernel(depth: int = D, pu: float = P_PUSH, pd: float = P_POP):
    """The (depth+1) x (depth+1) transition matrix.

    Boundary behaviour is the DESIGN's, not a modelling convenience: a push at
    full is refused by the FIFO and a pop at empty is a no-op, and both show up
    here as holds. Rejecting those draws instead — resampling until a legal
    transition appears — would change the chain into something the RTL does not
    implement, and the empirical curve would then disagree with the theory for
    reasons that have nothing to do with the design.
    """
    n = depth + 1
    P = [[0.0] * n for _ in range(n)]
    for q in range(n):
        up = pu if q < depth else 0.0
        dn = pd if q > 0 else 0.0
        P[q][min(q + 1, depth)] += up
        P[q][max(q - 1, 0)] += dn
        P[q][q] += 1.0 - up - dn
    return P


def expected_first_passage(depth: int = D, pu: float = P_PUSH, pd: float = P_POP):
    """E[T] to reach `depth` from every starting occupancy, solved exactly.

    h_q = 1 + sum_{j < D} P[q][j] h_j, by Gaussian elimination rather than by
    simulation: a Monte-Carlo estimate of the thing the experiment is supposed
    to test against would make the comparison circular.
    """
    P = kernel(depth, pu, pd)
    A = [[(1.0 if i == j else 0.0) - P[i][j] for j in range(depth)]
         for i in range(depth)]
    M = [row[:] + [1.0] for row in A]
    for c in range(depth):
        p = max(range(c, depth), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(depth):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, depth + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][depth] / M[i][i] for i in range(depth)] + [0.0]


def hitting_cdf(horizon: int, q0: int = 0, depth: int = D,
                pu: float = P_PUSH, pd: float = P_POP):
    """P(T_hit <= t) for t = 1..horizon, by propagating the distribution.

    Exact, with `depth` made absorbing. This is the curve the measured campaign
    outcomes are compared against.
    """
    P = kernel(depth, pu, pd)
    v = [0.0] * (depth + 1)
    v[q0] = 1.0
    out = []
    for _ in range(horizon):
        w = [0.0] * (depth + 1)
        for q in range(depth):
            if v[q]:
                for j, pr in enumerate(P[q]):
                    if pr:
                        w[j] += v[q] * pr
        w[depth] += v[depth]
        v = w
        out.append(v[depth])
    return out


def mixing_time(depth: int = D, pu: float = P_PUSH, pd: float = P_POP):
    """1 / (1 - |lambda_2|). Returns None without numpy rather than guessing."""
    try:
        import numpy as np
    except ImportError:
        return None
    ev = sorted(abs(np.linalg.eigvals(np.array(kernel(depth, pu, pd)))))[::-1]
    return 1.0 / (1.0 - ev[1])


def stationary(depth: int = D):
    """Uniform on {0..D}, exactly, when p_up == p_down.

    Detailed balance gives pi_q * p_u = pi_{q+1} * p_d, so with p_u = p_d every
    state carries equal mass. This matters for the near-stationary control
    condition: the initial occupancy can be drawn from the TRUE stationary law
    rather than approximated by a warm-up of guessed length.
    """
    n = depth + 1
    return [1.0 / n] * n


def level_first_passage(depth: int = D):
    """E[T] from 0 to each level k. Should scale as k^2 for a balanced walk.

    The testbench records the first time each milestone level is reached, so
    this prediction is checked at four points on the way up rather than only at
    the top. A board that hit the top at the right time by accident would still
    have to get the interior milestones right.
    """
    return {k: expected_first_passage(k)[0] for k in (6, 12, 18, depth)}


# --------------------------------------------------------------------------- #
# The committed campaign budget                                               #
# --------------------------------------------------------------------------- #
# B is the expected first-passage time from a cold start, rounded up. Chosen
# because it is the natural scale of the kernel and for no other reason: it was
# computed before the RTL existed and no detection rate had been observed.
#
# The second horizon is where the theory puts 99.5% of the mass. The shape
# comparison needs the tail; the admission claim does not. Keeping them separate
# stops a wish for a longer curve from quietly becoming a longer B.
BUDGET          = 668      # ceil(E[T_hit | q0 = 0]) = ceil(667.8)
SHAPE_HORIZON   = 3000     # theory: P(T_hit <= 3000) ~ 0.995


def summary() -> str:
    h = expected_first_passage()
    tmix = mixing_time()
    C = hitting_cdf(SHAPE_HORIZON)
    import bisect
    med = bisect.bisect_left(C, 0.5) + 1
    lines = [
        f"depth D              = {D}",
        f"draws                = push {P_PUSH}, pop {P_POP}, idle {P_IDLE:.2f}",
        f"E[T_hit | q0=0]      = {h[0]:,.1f} cycles   (D^2 = {D*D}, ratio {h[0]/(D*D):.2f})",
        f"median T_hit         = {med:,} cycles",
        f"t_mix                = {tmix:,.0f} cycles" if tmix else "t_mix = (numpy absent)",
        f"E[T_hit]/t_mix       = {h[0]/tmix:.2f}   <- O(1) is the point" if tmix else "",
        f"stationary law       = uniform on 0..{D} (exact, p_up == p_down)",
        "",
        f"COMMITTED budget B   = {BUDGET}   theory says P(T_hit <= B) = {C[BUDGET-1]:.3f}",
        f"shape horizon        = {SHAPE_HORIZON}  theory says P = {C[SHAPE_HORIZON-1]:.3f}",
        "",
        "level milestones (E[T] from 0, should go as k^2):",
    ]
    for k, v in level_first_passage().items():
        lines.append(f"    q={k:<3} E[T] = {v:8,.1f}   E[T]/k^2 = {v/(k*k):.2f}")
    return "\n".join(l for l in lines if l != "")




# --------------------------------------------------------------------------- #
# The DETECTION kernel — the quantity the experiment actually observes         #
# --------------------------------------------------------------------------- #
# The first commitment for this board used T_hit as a proxy for detection and
# asserted that detection occurs IFF the occupancy reaches DEPTH. That "iff" was
# an unchecked assumption about the DUT, and it is false in one direction.
#
# The mutant differs from the good design only when a push is ACCEPTED at full.
# Reaching q = D is necessary but not sufficient: the very next draw is as
# likely to be a pop, which returns the walk to D-1 where the two designs are
# bit-identical again. Detection therefore needs a push drawn WHILE AT the
# boundary, and that requires repeated returns to D — each costing O(D).
#
# The lag is not negligible, which is what the first commitment assumed. It is a
# second first-passage problem stacked on the first:
#
#     E[T_hit]    = 667.8
#     E[T_detect] = 723.5        lag 55.7, not the ~2 cycles I predicted
#
# and one campaign in 24 reached the boundary with the horizon ending before a
# push landed there — exactly the case the false "iff" ruled out.
def detection_kernel(depth: int = D, pu: float = P_PUSH, pd: float = P_POP):
    """States 0..depth plus an absorbing DETECTED state.

    From the boundary, a push draw is the over-push the mutant accepts, so it
    absorbs; a pop leaves the boundary; idle holds.
    """
    n = depth + 2
    absorb = depth + 1
    P = [[0.0] * n for _ in range(n)]
    for q in range(depth):
        P[q][q + 1] += pu
        P[q][max(q - 1, 0)] += pd          # a pop at q=0 is a no-op, so it holds
        P[q][q] += 1.0 - pu - pd
    P[depth][absorb] += pu
    P[depth][depth - 1] += pd
    P[depth][depth] += 1.0 - pu - pd
    P[absorb][absorb] = 1.0
    return P


def expected_detection(depth: int = D, pu: float = P_PUSH, pd: float = P_POP):
    """E[T_detect | q0 = 0], exactly."""
    P = detection_kernel(depth, pu, pd)
    a = depth + 1
    M = [[(1.0 if i == j else 0.0) - P[i][j] for j in range(a)] + [1.0]
         for i in range(a)]
    for c in range(a):
        piv = max(range(c, a), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        for r in range(a):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, a + 1):
                    M[r][k] -= f * M[c][k]
    return M[0][a] / M[0][0]


def detection_cdf(horizon: int, depth: int = D,
                  pu: float = P_PUSH, pd: float = P_POP):
    """P(T_detect <= t) for t = 1..horizon, exactly."""
    P = detection_kernel(depth, pu, pd)
    n, a = depth + 2, depth + 1
    v = [0.0] * n
    v[0] = 1.0
    out = []
    for _ in range(horizon):
        w = [0.0] * n
        for q in range(a):
            if v[q]:
                for j, pr in enumerate(P[q]):
                    if pr:
                        w[j] += v[q] * pr
        w[a] += v[a]
        v = w
        out.append(v[a])
    return out


def rows_are_stochastic(P) -> bool:
    """Every row sums to 1. Trivial, and it would have caught a real error.

    The first draft of detection_kernel() added the pop mass at q=0 to the hold
    AND then added an idle term that already contained it, so rows summed above
    1 and the "probabilities" grew without bound -- P(T <= 668) came out as
    4e51. The number was so absurd it was obvious, which is the only reason it
    was caught. A kernel that was wrong by 2% would have looked like a finding.
    """
    return all(abs(sum(row) - 1.0) < 1e-12 for row in P)


# ceil(E[T_detect | q0 = 0]) = 724. Derived from the corrected kernel, which is
# the quantity the campaign actually observes.
BUDGET_DETECT = 724


if __name__ == "__main__":
    print(summary())
    print()
    C = detection_cdf(2000)
    print(f"E[T_detect | q0=0]   = {expected_detection():,.1f}   "
          f"(E[T_hit] = {expected_first_passage()[0]:,.1f}, "
          f"lag {expected_detection() - expected_first_passage()[0]:,.1f})")
    print(f"BUDGET_DETECT        = {BUDGET_DETECT}   "
          f"theory says P(T_detect <= B') = {C[BUDGET_DETECT-1]:.3f}")
    print(f"kernels row-stochastic: hit={rows_are_stochastic(kernel())}  "
          f"detect={rows_are_stochastic(detection_kernel())}")

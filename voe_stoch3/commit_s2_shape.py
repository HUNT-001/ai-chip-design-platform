r"""Commit S2's decision rule to disk, hashed, before the campaign runs.

WHY THIS FILE IS SEPARATE FROM power_s2.py. power_s2.py computes what the
instrument can see; this file freezes what will be concluded from it. Keeping
them apart matters because the second is the thing that must not move: a power
calculation can legitimately be re-run and refined, but a decision rule that is
re-run and refined after the data arrives is not a decision rule.

The numbers below come from power_s2.py and nowhere else. The cheap ones are
RECOMPUTED here at commit time rather than retyped -- W from the committed window
rule, the censoring horizons, sat_mac's detection probability -- so a drift
between the two files cannot hide in a transcription. The ones that cost a
four-minute Monte-Carlo are carried as recorded literals in MEASURED below, each
tagged with the commit that produced it, because recomputing them here would mean
the commitment's own digest depended on a fresh random draw.

ONE THING THE WINDOW RULE CANNOT DO, STATED PLAINLY. For dfifo the rule "W is the
smallest t with F(t) >= 1% of the board's own theoretical mass" has an exact law
to apply itself to, so W = 86 is fixed before anything is observed. sat_mac has
no such law under the alternative: it is memoryless by construction, so "1% of
the mass" has no alternative to be 1% of until an alternative family is named.
Applied to sat_mac's NULL the rule gives W = 659, and at that window no
rejection region exists at any N -- the test cannot exist. The committed
W_sat = 8865 is therefore the 1% window of the dead-zone alternative AT THE
DETECTABILITY BOUNDARY (d = 8,289), which is a quantity the power analysis
produced, not one the rule produced on its own. That is a weaker provenance than
dfifo's W and the record should not pretend otherwise.

    /usr/bin/python3 commit_s2_shape.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np

from voe.preregistration import Commitment

import power_s2 as pw
import theory

COMMIT_PATH = os.path.join(_HERE, "prereg_s2_shape.json")

# --------------------------------------------------------------------------- #
# Recomputed now, from power_s2.py's own rule and constants.                   #
# --------------------------------------------------------------------------- #
_CDF        = pw.exact_cdf()
W_DFIFO     = pw.window_from_mass(_CDF)              # the rule, applied: 86
C_DFIFO     = pw.CENSOR_DFIFO                        # theory.SHAPE_HORIZON
C_SAT       = pw.CENSOR_SAT                          # project default, 20000
MEAN_SAT    = 1.0 / pw.P_SAT
P_DETECT    = 1.0 - (1.0 - pw.P_SAT) ** C_SAT
C_OVER_MEAN = C_SAT / MEAN_SAT
ENVIRONMENT = (f"{sys.executable} {sys.version.split()[0]}, "
               f"numpy {np.__version__}")

# --------------------------------------------------------------------------- #
# Carried from the committed Monte-Carlo runs. Each is a measurement.          #
# --------------------------------------------------------------------------- #
# 86f155d  the N search at ALT_REPS = 400,000
# 4729648  the same, after the denominator was censored, plus the sat_mac arm
MEASURED = {
    "N":                  79,          # 4729648, power_s2.py --dfifo
    "dfifo_critical":     0.2030,      # 4729648 (was 0.2026 uncensored)
    "dfifo_power":        0.9031,      # 4729648 (was 0.9027 uncensored)
    "dfifo_power_se":     0.0005,
    "dfifo_gap_se_mult":  25,          # gap to N-1 as a multiple of the MC SE
    "sat_deadzone_d":     8_289,       # smallest detectable dead zone, cycles
    "sat_critical":       0.3015,      # 4729648, power_s2.py --sat
    "sat_power":          0.9026,
    "sat_power_se":       0.0009,
}

D_STAR  = MEASURED["sat_deadzone_d"]
W_SAT   = pw.sat_window(D_STAR)                      # the rule on that d: 8865
DELTA   = D_STAR / MEAN_SAT

spec = {
    "study": "S2 - cross-board shape comparison (Stochastic Generalization Gate)",
    "boards": ["sat_mac (voe_stoch)", "dfifo (voe_stoch3)"],
    "observable": "index of first detection: vector index on sat_mac, cycle "
                  "count T_detect on dfifo. Cold start only; dfifo's "
                  "near-stationary control is reported separately and never "
                  "pooled -- first passage and recurrence are different "
                  "questions, per commit_dfifo_s1b.json.",
    "null": "constant hazard (geometric), rate fitted per board as the "
            "right-censored exponential MLE D / sum(min(T_i, C)), D = "
            "detection count. Scale-free by construction: the rate is a "
            "nuisance parameter, so the boards' different means cannot carry "
            "the result.",
    "statistic": "Lambda = [S / sum(min(T_i, W))] / [D / sum(min(T_i, C))] -- "
                 "early-window hazard over fitted global hazard. Reject the "
                 "null when Lambda <= the board's committed critical value. "
                 "Lambda has an atom at 0 and that atom is load-bearing: a "
                 "dead zone makes S=0 a positive-probability event under the "
                 "alternative and near-impossible under the null, which is "
                 "where most of the power lives.",
    "window_rule": "W is the smallest t with F_exact(t) >= 1% of the board's "
                   "own theoretical mass. A round 1% -- fixed before any "
                   "search, and NOT chosen for the N it yields.",
    "window_rule_limit": "The rule is only self-sufficient where an "
                         "alternative law exists to apply it to. dfifo has "
                         f"one, so W = {W_DFIFO} is fixed before any "
                         "observation. sat_mac is memoryless by construction "
                         "and has no alternative law, so the rule applied to "
                         "its NULL gives W = 659, at which NO rejection "
                         "region exists at any N. The committed "
                         f"W = {W_SAT:,} is the 1% window of the dead-zone "
                         f"alternative at the detectability boundary "
                         f"(d = {D_STAR:,}), which the power analysis "
                         "produced -- not the rule acting alone. Weaker "
                         "provenance than dfifo's W, recorded as such.",
    "alpha": pw.ALPHA,
    "alpha_note": "One instrument, one threshold, both arms. Direction stated "
                  "plainly: a tighter alpha makes rejection harder, so 1e-3 is "
                  "the LENIENT choice for the fit arm. Symmetry here is not "
                  "neutrality. The repo's other S2 shape test uses per-point "
                  "Clopper-Pearson at 95% and states it reports no calibrated "
                  "p-value, so there was nothing to inherit.",
    "N": MEASURED["N"],
    "N_derivation": "Smallest N with power >= 0.90 against dfifo's exact "
                    "first-passage law, from theory.py and the geometric null "
                    "only -- no board was run, no simulator touched. Taken from "
                    "the rule as-is: not rounded up, no higher-margin neighbour "
                    "substituted. Raising N would make the EXCLUSION arm easier "
                    "to clear, i.e. tuning toward the hypothesis.",
    "seed_base": {"dfifo": 31000, "sat_mac": 32000},
    "arms": {
        "exclusion_dfifo": {
            "role": "the arm that can fail; all of the gate's content is here",
            "C": C_DFIFO,
            "W": W_DFIFO,
            "critical_Lambda": MEASURED["dfifo_critical"],
            "power": MEASURED["dfifo_power"],
            "power_mc_se": MEASURED["dfifo_power_se"],
            "attained_alpha": pw.ALPHA,
            "tightness": f"N-1 = {MEASURED['N'] - 1} is below target; gap to "
                         f"N-1 is {MEASURED['dfifo_gap_se_mult']}x the MC SE, "
                         f"so the crossover is resolved, not noise",
        },
        "fit_sat_mac": {
            "role": "SIZE CONTROL, not power. sat_mac's geometric is exact by "
                    "construction -- one operand pair in 65536 from two's-"
                    "complement arithmetic -- so there is no genuine "
                    "alternative to have power against. This arm shows the "
                    "instrument does not reject where the law is known-true.",
            "C": C_SAT,
            "W": W_SAT,
            "critical_Lambda": MEASURED["sat_critical"],
            "expected_detections": round(MEASURED["N"] * P_DETECT, 2),
            "P_detect": round(P_DETECT, 4),
            "C_over_mean": round(C_OVER_MEAN, 3),
            "declared_limitation":
                "Had sat_mac's hazard in fact departed from constant, this arm "
                f"would detect only a dead zone between ~{DELTA:.1%} "
                f"(d = {D_STAR:,}) and ~28% of the mean, under the dead-zone-"
                "geometric alternative family. Below delta ~8% NO rejection "
                "region exists at all -- attained size 0, power 0 against "
                "every alternative. Above ~30% detectability collapses again "
                "(0.843 at 0.28, 0.359 at 0.30, 0 at 0.35) because a large "
                "dead zone leaves no in-campaign detections: E[D] falls "
                "9.7 -> 2.7 -> 0.6. Detectability is a bounded WINDOW, not a "
                "threshold. A non-rejection here is therefore NOT positive "
                "evidence of memorylessness and must never be read as such.",
            "alternative_family": "dead-zone geometric, mean held at 65536 so "
                "the deviation is purely shape. delta depends on this family; "
                "a different one-parameter family gives a different number.",
            "campaign_not_tuned": "C stays at 20000, the project default. "
                "Raising it to buy uncensored samples is Experiment 15's "
                "refusal verbatim and is recorded as refused in the capability "
                "ledger. The crippling is reported, not designed away.",
        },
    },
    "stopping_rule": "Fixed N = 79 per board. No extension after looking, no "
                     "seed replacement, no re-run under a different W.",
    "environment": f"{ENVIRONMENT}; MC-exact critical values, not normal-"
                   "approximated. A re-run under a different numpy that "
                   "disagrees with the committed Lambda thresholds declares "
                   "itself rather than quietly replacing them.",
    "gate_passes_iff": "the null is NOT rejected on sat_mac AND IS rejected on "
                       "dfifo, both at alpha = 1e-3 with N = 79.",
    "losing_outcomes": "Each of these is NOT MET, reported as-is, and H stays "
                       "closed: (1) both boards fit; (2) both excluded; (3) the "
                       "roles invert. The response to any of them is a NEW "
                       "board with fresh seeds and a new commitment -- never a "
                       "widened window, a longer SHAPE_HORIZON, a larger N, or "
                       "a re-registered alpha.",
}

notes = (
    "THE GATE IS ONE-SIDED AND THE RECORD SHOULD SAY SO. sat_mac's geometric "
    "is exact by construction, so the fit arm cannot meaningfully fail; all "
    "falsifiable content sits on the dfifo exclusion arm. Reading this as a "
    "two-sided result would overstate it. The fit arm's job is size control, "
    "and its detection window (12.6%-28% of the mean) is recorded above as a "
    "declared limitation so a non-rejection cannot be mistaken for evidence."
    "\n\n"
    "BOARD PROVENANCE. dfifo was designed after pfifo failed the gate, from "
    "the E[T_hit]/t_mix ratio argument in theory.py -- pfifo's ratio was ~130, "
    "so its chain forgot its state between rare-event opportunities and "
    "successive trials were effectively independent. No pfifo detection "
    "numbers entered that choice. Theory-driven board design, not data-driven "
    "tuning."
    "\n\n"
    "WHAT WAS CORRECTED BEFORE COMMITMENT, on the record: the hazard "
    "denominator was first written N / sum(T_i), which assumes every seed "
    "detects. Negligible for dfifo (0.5% censoring; power 0.9027 -> 0.9031, "
    "critical value 0.2026 -> 0.2030, committed N unchanged) but it would have "
    "overstated sat_mac's global hazard ~4x and flattered the fit arm. See "
    "4729648."
    "\n\n"
    "THE WINDOW RULE DOES NOT REACH THE FIT ARM UNAIDED. See "
    "spec.window_rule_limit: sat_mac's committed W comes from the dead-zone "
    "alternative at the detectability boundary, not from the rule applied to a "
    "pre-specified law, because a board that is memoryless by construction "
    "offers the rule nothing to measure 1% of. The rule applied to its null "
    "gives a window at which the test cannot exist at any N."
)


def main() -> int:
    c = Commitment("S2 shape comparison", COMMIT_PATH, spec, notes).commit()
    print("=== S2 shape comparison: decision rule committed ===")
    print(f"    path      : {os.path.relpath(COMMIT_PATH, _ROOT)}")
    print(f"    N         : {spec['N']} seeds per board, alpha = {spec['alpha']:g}")
    print(f"    exclusion : dfifo   C = {C_DFIFO:,}  W = {W_DFIFO}  "
          f"reject if Lambda <= {MEASURED['dfifo_critical']}")
    print(f"    fit       : sat_mac C = {C_SAT:,}  W = {W_SAT:,}  "
          f"reject if Lambda <= {MEASURED['sat_critical']}")
    print(f"    recomputed: W_dfifo = {W_DFIFO} (rule), W_sat = {W_SAT:,} "
          f"(rule at d = {D_STAR:,}), P_detect = {P_DETECT:.4f}")
    print(f"    env       : {ENVIRONMENT}")
    print(c.render())
    if not c.intact:
        print("    the commitment file was modified after hashing. INVALID.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Experiment eligibility — can this environment OBSERVE what we mean to measure?

Every gate this project has added so far asks whether a RESULT is trustworthy:

    vacuity gate        evidence that cannot fail proves nothing
    positive control    a checker that always fails refutes nothing
    negative control    a model that passes on broken RTL checks nothing
    witness audit       a claim without provenance is not a claim
    pre-registration    criteria fixed before the data

Experiment 15 exposed a gate that comes BEFORE all of those. Its board was
wired with a real testbench and real tools, every control passed, and it still
could not answer its question — because the phenomenon it intended to measure
(campaign-to-campaign variance) did not exist in that corpus. Simulation
outcomes were seed-independent: true properties passed on every seed, the
mutant was caught on every seed. There was no variance to detect, so a
"no effect" reading would have been as wrong as a "large effect" one.

    An experiment must demonstrate that the phenomenon it intends to measure
    actually EXISTS in the chosen environment, before any metric is computed.

That is STATISTICAL IDENTIFIABILITY, and it produces a verdict this project did
not previously have:

    MET / NOT MET      the committed rule was applied and decided
    UNDERPOWERED       the rule could not decide at this sample size
    NOT EVALUABLE      the rule was never applicable — the environment cannot
                       exhibit the phenomenon, so no verdict exists to report

NOT EVALUABLE is not a weak NOT MET. A NOT MET says the effect is absent or too
small; a NOT EVALUABLE says the question was never asked. Conflating them is how
a benchmark mismatch gets recorded as a scientific finding.

THREE EXPERIMENT CLASSES, each with its own admission requirement:

    DETERMINISTIC   same action -> same outcome. Fine for formal-vs-simulation,
                    proof correctness, risk accounting, deterministic planning.
                    Requires: nothing extra. But seed counts on such a board are
                    REPETITION, not replication, and must never be reported as
                    sample size.

    STOCHASTIC      same action -> a DISTRIBUTION of outcomes. Needed for seed
                    sensitivity, adaptive sampling, probabilistic diagnosis,
                    posterior learning, coverage exploration.
                    Requires: Var(O | a) > 0, demonstrated on the actual board
                    under the declared stimulus distribution.

    SEQUENTIAL      an action changes what is worth doing next. Needed for
                    diagnosis, multi-step planning, long-horizon reasoning.
                    Requires: at least one action whose outcome changes the
                    admissible or productive action set.

The current corpus is good for DETERMINISTIC, demonstrated for SEQUENTIAL (the
measured lemma dependencies on fifo and mv_filter), and INSUFFICIENT for
STOCHASTIC. That is a benchmark property, not a framework weakness, and it is
why belief/world-model work should wait: those mechanisms exploit stochasticity
this corpus cannot present.
"""
from __future__ import annotations
from dataclasses import dataclass, field

DETERMINISTIC = "deterministic"
STOCHASTIC = "stochastic"
SEQUENTIAL = "sequential"

NOT_EVALUABLE = "NOT EVALUABLE"


@dataclass
class Eligibility:
    """The admission decision for one experiment on one board."""
    experiment: str
    regime: str
    eligible: bool
    reasons: list = field(default_factory=list)

    def render(self) -> str:
        head = "ELIGIBLE" if self.eligible else "NOT EVALUABLE"
        out = [f"  {self.experiment}  [{self.regime}]  -> {head}"]
        out += [f"      - {r}" for r in self.reasons]
        return "\n".join(out)


def outcome_variance(sample_fn, seeds) -> tuple:
    """Does the environment produce DIFFERENT outcomes for the same action?

    `sample_fn(seed)` must return a hashable outcome for one action repeated
    under a different seed. Returns (n_distinct, outcomes).

    This is deliberately a count of distinct outcomes, not a standard
    deviation: variance over identical values still returns floating-point
    residue (~2.3e-16 was observed), so `std == 0` silently never fires and the
    guard it protects becomes invisible. Distinctness is exact.
    """
    outs = [sample_fn(s) for s in seeds]
    return len(set(outs)), outs


def admit(experiment: str, regime: str, *,
          controls_armed: bool = True,
          distinct_outcomes: int | None = None,
          unlocking_actions: int | None = None,
          notes: str = "") -> Eligibility:
    """Decide whether an experiment may proceed to compute a metric at all.

    This runs BEFORE the pre-registered rule, and its failure is NOT a result:
    a board that cannot exhibit the phenomenon yields NOT EVALUABLE, which must
    be recorded as its own state rather than collapsed into NOT MET.
    """
    reasons = []
    if not controls_armed:
        reasons.append("controls not armed — vacuity/positive/negative gates "
                       "must pass before eligibility is even considered")

    if regime == STOCHASTIC:
        if distinct_outcomes is None:
            reasons.append("STOCHASTIC experiments must MEASURE outcome "
                           "variance on the board; none was supplied")
        elif distinct_outcomes <= 1:
            reasons.append(
                f"no stochastic treatment effect exists: the same action gives "
                f"{distinct_outcomes} distinct outcome across seeds. Seeds are "
                f"repetition, not replication, and the noise criterion in any "
                f"committed rule is vacuous here")
    elif regime == SEQUENTIAL:
        if not unlocking_actions:
            reasons.append("SEQUENTIAL experiments need at least one action "
                           "whose outcome changes what is worth doing next; "
                           "none was demonstrated")
    elif regime != DETERMINISTIC:
        reasons.append(f"unknown regime {regime!r}")

    if notes:
        reasons.append(notes)
    return Eligibility(experiment, regime, not any(
        r for r in reasons if not r.startswith("note:")), reasons)


def seeds_are_replication(distinct_outcomes: int) -> bool:
    """Is a seed count a SAMPLE count on this board?

    Only if outcomes actually differ. Reporting 'N seeds' for a deterministic
    board overstates the evidence: Experiments 13 and 14 each ran one campaign
    N times, and their gains were exact arithmetic rather than estimates.
    """
    return distinct_outcomes > 1

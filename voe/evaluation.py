"""Evaluation engine — the thing that makes "we improved" a claim with a witness.

Without this, statements like "the organisation discovered a better strategy" are
exactly the kind of unearned assertion the kernel exists to refuse. So the same
discipline is turned on the organisation itself:

    a policy is not better because it is cleverer, newer, or ours.
    It is better if it discharges more weighted risk per unit cost,
    measured on a design it was not developed against.

Primary measure, per the efficiency ratio:

    E(pi, D) = dR(pi, D) / C(pi, D)      risk discharged per unit cost

reported alongside the secondary signals that stop E from being gamed: proof
yield, bugs found, steps to first discharge, and cost sunk into obligations that
were never discharged.

**The held-out rule is enforced in code, not by intention.** `compare()` will
report a within-design result, but `promote()` REFUSES to certify a policy as an
improvement when the evaluation design is one of its development designs. A
system that could quietly train on its test set would eventually claim progress
it had not made, which is the organisational form of a vacuous proof.
"""
from __future__ import annotations
import io, contextlib
from dataclasses import dataclass, field

from board import TaskBoard, ResourceLedger


@dataclass
class CampaignResult:
    policy: str
    design: str
    risk_before: float = 0.0
    risk_after: float = 0.0
    cost: float = 0.0
    proofs: int = 0
    bugs: int = 0
    steps: int = 0
    steps_to_first_discharge: int | None = None
    undischarged: list = field(default_factory=list)
    wasted_cost: float = 0.0        # spent on obligations still open at the end
    gate_armed: bool = False

    closed_weight: float = 0.0      # weight of obligations actually CLOSED

    @property
    def risk_delta(self) -> float:
        """Total fall in residual risk — includes inductive shaving."""
        return self.risk_before - self.risk_after

    @property
    def discharged(self) -> float:
        """Risk removed by CLOSING obligations (proof or counterexample).

        This is the primary quantity, and it is deliberately NOT the raw fall in
        residual risk. Simulation lowers R by accumulating effective samples
        without settling anything, so a policy can shave risk indefinitely while
        proving nothing. That is not a hypothetical: on a board where the
        vacuity gate refused every formal result, a simulation-heavy policy
        scored E=5.0 with ZERO obligations closed and ranked first. Rewarding
        that would teach the organisation to nibble rather than to verify.
        """
        return self.closed_weight

    @property
    def efficiency(self) -> float:
        """E = risk DISCHARGED (obligations closed) per unit cost."""
        return self.discharged / self.cost if self.cost > 0 else 0.0

    @property
    def shaving_efficiency(self) -> float:
        """Secondary: total risk movement per cost, including inductive shaving.
        Reported for contrast — a large gap between this and `efficiency` means
        a policy is spending on evidence that never settles anything."""
        return self.risk_delta / self.cost if self.cost > 0 else 0.0

    def row(self) -> str:
        first = self.steps_to_first_discharge
        return (f"  {self.policy:14s} E={self.efficiency:6.3f}  "
                f"closed={self.discharged:6.2f}  cost={self.cost:5.1f}  "
                f"proofs={self.proofs}  bugs={self.bugs}  "
                f"first@{first if first is not None else '-':>3}  "
                f"open={len(self.undischarged)}  shave={self.shaving_efficiency:5.2f}")


def run_campaign(voe, design: str, policy_name: str, max_steps=120) -> CampaignResult:
    """Run one VOE campaign silently and measure it."""
    k, ks, w = voe.k, voe.ks, voe.board.weights()
    res = CampaignResult(policy_name, design)
    res.risk_before = k.R(ks, w)
    with contextlib.redirect_stdout(io.StringIO()):
        voe.run(max_steps=max_steps)
    res.risk_after = k.R(ks, w)
    res.cost = voe.ledger.spent
    res.steps = len(voe.log) if hasattr(voe, "log") else 0
    res.proofs = sum(1 for p in voe.board.tasks if ks.proven(p))
    res.bugs = sum(1 for p in voe.board.tasks if ks.disproven(p))
    res.undischarged = [p for p in voe.board.tasks
                        if not ks.proven(p) and not ks.disproven(p)]
    # weight actually CLOSED — the metric that cannot be gamed by sampling
    res.closed_weight = sum(wt for phi, wt in w.items()
                            if ks.proven(phi) or ks.disproven(phi))
    try:
        res.gate_armed = voe.formal.gate_status()[0]
    except Exception:
        res.gate_armed = False
    # cost sunk into obligations that were still open at the end (0 if none were)
    res.wasted_cost = _wasted(voe, set(res.undischarged))
    res.steps_to_first_discharge = _first_discharge(voe)
    return res


def _wasted(voe, open_set):
    """Cost spent on obligations that were still open at the end."""
    total = 0.0
    for entry in getattr(voe, "action_log", []):
        if entry.get("phi") in open_set:
            total += entry.get("cost", 0.0)
    return total


def _first_discharge(voe):
    for e in getattr(voe, "action_log", []):
        if e.get("status") in ("proved", "counterexample"):
            return e.get("step")
    return None


def compare(results, incumbent: str | None = None) -> str:
    """Rank policies by E, highlighting the incumbent."""
    lines = ["  policy           E=dR/cost      dR    cost  proofs bugs first open wasted"]
    for r in sorted(results, key=lambda r: -r.efficiency):
        mark = "  <- incumbent" if incumbent and r.policy == incumbent else ""
        lines.append(r.row() + mark)
    return "\n".join(lines)


@dataclass
class PromotionVerdict:
    accepted: bool
    reason: str
    incumbent_E: float = 0.0
    candidate_E: float = 0.0


@dataclass
class Aggregate:
    """A policy measured over repeated campaigns, so variance is visible.

    A single campaign cannot separate a real effect from luck — the first
    held-out run promoted a *random* ordering policy on one lucky sample, which
    is exactly the failure this exists to prevent.
    """
    policy: str
    design: str
    runs: list = field(default_factory=list)

    @property
    def n(self):
        return len(self.runs)

    @property
    def mean_E(self):
        return sum(r.efficiency for r in self.runs) / self.n if self.n else 0.0

    @property
    def std_E(self):
        if self.n < 2:
            return 0.0
        m = self.mean_E
        return (sum((r.efficiency - m) ** 2 for r in self.runs) / (self.n - 1)) ** 0.5

    @property
    def worst_E(self):
        return min((r.efficiency for r in self.runs), default=0.0)

    @property
    def gate_armed(self):
        return all(r.gate_armed for r in self.runs) if self.runs else False

    def row(self):
        return (f"  {self.policy:14s} E={self.mean_E:6.3f} +/-{self.std_E:5.3f}  "
                f"worst={self.worst_E:6.3f}  n={self.n}  "
                f"proofs={self.runs[0].proofs if self.runs else 0}")


def aggregate(results):
    """Group campaign results by policy."""
    out = {}
    for r in results:
        a = out.setdefault(r.policy, Aggregate(r.policy, r.design))
        a.runs.append(r)
    return list(out.values())


def compare_aggregates(aggs, incumbent=None):
    lines = ["  policy          mean E        std     worst   n  proofs"]
    for a in sorted(aggs, key=lambda a: -a.mean_E):
        mark = "  <- incumbent" if incumbent and a.policy == incumbent else ""
        lines.append(a.row() + mark)
    return "\n".join(lines)


def promote_aggregate(candidate: Aggregate, incumbent: Aggregate,
                      dev_designs, min_gain: float = 0.05, min_runs: int = 5):
    """Promotion on repeated evidence rather than one campaign.

    Adds two requirements beyond the single-run rule:
      * at least `min_runs` campaigns, so variance is observable at all
      * the candidate's WORST run must still beat the incumbent's MEAN

    That second condition is deliberately crude and is NOT presented as a
    settled statistical protocol — choosing the right test, effect size and
    correction across designs is an open evaluation-design problem. It is
    simply strong enough to reject a policy that won once by luck.
    """
    if candidate.design in set(dev_designs):
        return PromotionVerdict(False,
            f"REFUSED: '{candidate.design}' is a development design")
    if not candidate.gate_armed:
        return PromotionVerdict(False,
            "REFUSED: the vacuity gate was not armed in every campaign")
    if candidate.n < min_runs:
        return PromotionVerdict(False,
            f"REFUSED: {candidate.n} campaign(s), need >= {min_runs} to see variance")
    if all((r.proofs + r.bugs) == 0 for r in candidate.runs):
        return PromotionVerdict(False,
            "REFUSED: the candidate closed no obligations in any campaign — "
            "a strategy that settles nothing cannot be an improvement, however "
            "cheaply it moves the risk number")
    ce, ie = candidate.mean_E, incumbent.mean_E
    if ie <= 0:
        return PromotionVerdict(ce > 0, "incumbent discharged nothing", ie, ce)
    gain = (ce - ie) / ie
    if gain <= min_gain:
        return PromotionVerdict(False,
            f"REJECTED: mean E={ce:.3f} vs {ie:.3f} ({gain:+.1%}, needs > {min_gain:.0%})",
            ie, ce)
    if candidate.worst_E <= ie:
        return PromotionVerdict(False,
            f"REJECTED: mean E={ce:.3f} beats {ie:.3f} ({gain:+.1%}) but its WORST "
            f"campaign ({candidate.worst_E:.3f}) does not — the margin is not "
            f"reliable across runs", ie, ce)
    return PromotionVerdict(True,
        f"PROMOTED: mean E={ce:.3f} vs {ie:.3f} ({gain:+.1%}), "
        f"worst {candidate.worst_E:.3f} still ahead", ie, ce)


def promote(candidate: CampaignResult, incumbent: CampaignResult,
            dev_designs, min_gain: float = 0.05) -> PromotionVerdict:
    """Decide whether a candidate policy may enter organisational memory.

    Refuses on any of:
      * the evaluation design was a development design (training on the test set)
      * the vacuity gate was not armed (the evidence itself is uncertifiable)
      * the improvement does not exceed `min_gain` in relative terms

    `min_gain` is a placeholder threshold, deliberately NOT presented as settled:
    what constitutes a defensible improvement (significance, variance across
    designs, minimum effect size) is an evaluation-design problem still open.
    """
    if candidate.design in set(dev_designs):
        return PromotionVerdict(False,
            f"REFUSED: '{candidate.design}' is a development design — a policy "
            f"cannot be certified on a design it was tuned against")
    if not candidate.gate_armed:
        return PromotionVerdict(False,
            "REFUSED: vacuity gate was not armed — the campaign's own evidence "
            "is not certifiable, so its efficiency is meaningless")
    ce, ie = candidate.efficiency, incumbent.efficiency
    if ie <= 0:
        return PromotionVerdict(ce > 0, "incumbent discharged nothing", ie, ce)
    gain = (ce - ie) / ie
    if gain <= min_gain:
        return PromotionVerdict(False,
            f"REJECTED: E={ce:.3f} vs incumbent {ie:.3f} "
            f"({gain:+.1%}, needs > {min_gain:.0%})", ie, ce)
    return PromotionVerdict(True,
        f"PROMOTED: E={ce:.3f} vs incumbent {ie:.3f} ({gain:+.1%})", ie, ce)

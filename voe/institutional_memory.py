"""Institutional memory — what was tried, what it cost, and when to try again.

An organisation that remembers only its successes relearns its failures. This
records the other half: hypotheses that were tested and rejected, the evidence
that rejected them, and — the part that makes it useful rather than merely
tidy — **the conditions under which the idea should be reconsidered.**

The motivating case is real. `H-uncertainty` (Bayesian belief + priced Value of
Diagnosis) failed a pre-registered superiority test against a one-line heuristic:
+2.7% against a committed 5% threshold, at ~3x the probing cost. The right
record is not "rejected". It is:

    hypothesis   explicit belief + VoD beats a simple diagnostic heuristic
    evidence     12 seeds, 6 design families, +2.7% (threshold 5%), 3x cost
    decision     do not promote
    reason       the simple policy already meets the requirement
    revisit if   long-horizon, non-stationary, coupled or high-dimensional
                 environments appear — none of which this board contains

Without the last line, the idea is either lost or re-attempted blindly. With it,
the organisation knows precisely what would have to change for the answer to
change — which is also a research programme rather than a shelf.

**Promotion rule.** A capability enters the default path only if

    benefit > threshold + uncertainty + complexity cost

Complexity is charged explicitly, because a system that adopts every mechanism
that is not clearly worse accumulates exactly the unjustified complexity it is
built to prevent in the designs it verifies.
"""
from __future__ import annotations
import json, os, time
from dataclasses import dataclass, asdict, field


@dataclass
class Record:
    """One tested hypothesis and its disposition."""
    hypothesis: str
    treatment: str
    control: str
    evidence: str                      # what was measured, including spread
    decision: str                      # PROMOTED | REJECTED | UNDERPOWERED
    reason: str
    revisit_if: list = field(default_factory=list)
    complexity_note: str = ""
    at: float = field(default_factory=time.time)

    def render(self) -> str:
        lines = [f"  hypothesis : {self.hypothesis}",
                 f"  {self.treatment} vs {self.control}",
                 f"  evidence   : {self.evidence}",
                 f"  decision   : {self.decision}",
                 f"  reason     : {self.reason}"]
        if self.complexity_note:
            lines.append(f"  complexity : {self.complexity_note}")
        if self.revisit_if:
            lines.append("  revisit if :")
            lines += [f"      - {r}" for r in self.revisit_if]
        return "\n".join(lines)


class Ledger:
    """Append-only record of capability decisions."""

    def __init__(self, path):
        self.path = path
        self.records = []
        if os.path.exists(path):
            with open(path) as f:
                self.records = [Record(**r) for r in json.load(f)]

    def add(self, rec: Record):
        # append-only: a decision is not edited away when it becomes inconvenient
        self.records.append(rec)
        with open(self.path, "w") as f:
            json.dump([asdict(r) for r in self.records], f, indent=2)
        return rec

    def rejected(self):
        return [r for r in self.records if r.decision == "REJECTED"]

    def revisit_triggers(self):
        """Every condition that would justify re-opening a rejected idea."""
        out = {}
        for r in self.rejected():
            for cond in r.revisit_if:
                out.setdefault(cond, []).append(r.treatment)
        return out

    def render(self) -> str:
        return "\n\n".join(r.render() for r in self.records) or "  (empty)"


# --------------------------------------------------------------------------- #
# Promotion rule with an explicit complexity charge                           #
# --------------------------------------------------------------------------- #
def promotion_verdict(benefit_rel: float, threshold: float, spread_rel: float,
                      complexity_cost_rel: float):
    """benefit > threshold + uncertainty + complexity cost.

    `complexity_cost_rel` is the relative extra cost the capability imposes —
    here, extra probing spend. Charging it is the difference between "not
    clearly worse" and "demonstrably better", and it is what stops a system from
    accreting every mechanism that survives a null test.
    """
    bar = threshold + spread_rel + complexity_cost_rel
    ok = benefit_rel > bar
    return ok, (f"benefit {benefit_rel:+.1%} vs bar {bar:.1%} "
                f"(threshold {threshold:.0%} + uncertainty {spread_rel:.1%} "
                f"+ complexity {complexity_cost_rel:.1%})")

"""VOE task board + resource ledger.

The board holds verification *obligations* (properties to discharge). The ledger
enforces the economic layer: every action costs, the budget is finite, and an
action that would overspend is refused. Cost/value is the kernel's utility 𝒰
made operational — nothing here is a new primitive, it is bookkeeping over the
frozen kernel's notions of risk-reduction and cost.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Task:
    phi: str                        # the property / obligation identifier
    weight: float                   # criticality weight (feeds R)
    formal_task: str | None = None  # sby task that adjudicates it (None = no harness)
    inject_bug: bool = False        # which DUT variant it concerns
    signed_off: bool = False        # true only when discharged by witnessed evidence
    kind: str = "functional"        # "functional" | "structural"
    note: str = ""                  # provenance / why it exists
    requires: tuple = ()            # obligations that must close before this one
    enables: tuple = ()             # obligations this one unlocks when closed

    # `requires`/`enables` express assume-guarantee structure: a lemma proved
    # once lets dependent properties assume it, which is how real verification
    # decomposes. It is DECLARED by the engineer, exactly as an assume-guarantee
    # contract is — so a planner is entitled to use it. A one-step
    # expected-value rule cannot: the lemma may be expensive and carry little
    # weight of its own, while unlocking most of the board.

    def has_evidence_path(self) -> bool:
        """Can this obligation actually be discharged with what we have?

        An auto-derived obligation may be perfectly real and still have no
        checker yet. Naming a property is not checking it — such obligations
        stay on the board contributing risk, and are reported as *declared,
        unverifiable* rather than quietly dropped.
        """
        return self.kind == "structural" or self.formal_task is not None


class TaskBoard:
    def __init__(self, tasks):
        self.tasks = {t.phi: t for t in tasks}

    def weights(self):
        return {t.phi: t.weight for t in self.tasks.values()}

    def open_tasks(self, ks):
        """Obligations not yet discharged (proven) or refuted (bug logged)."""
        return [t for t in self.tasks.values()
                if not ks.proven(t.phi) and not ks.disproven(t.phi)]

    def actionable(self, ks):
        """Open obligations that something can actually be run against.

        An obligation whose prerequisites are not yet closed cannot be
        discharged — its proof would have to assume a lemma nobody has
        established. It stays on the board contributing risk, and becomes
        actionable the moment its lemma closes.
        """
        out = []
        for t in self.open_tasks(ks):
            if not t.has_evidence_path():
                continue
            if any(not (ks.proven(r) or ks.disproven(r)) for r in t.requires):
                continue                       # blocked on an unproved lemma
            out.append(t)
        return out

    def blocked(self, ks):
        """Open obligations waiting on a lemma."""
        return [t.phi for t in self.open_tasks(ks)
                if t.requires and any(not (ks.proven(r) or ks.disproven(r))
                                      for r in t.requires)]

    def unverifiable(self, ks):
        """Open obligations with NO checker — real work that nobody can do yet."""
        return [t.phi for t in self.open_tasks(ks) if not t.has_evidence_path()]

    def get(self, phi):
        return self.tasks[phi]


# Per-method costs (arbitrary units; formal is dearer than a sim run, static
# structural analysis is cheapest — it parses rather than executes or solves).
ACTION_COST = {"sim": 1.0, "formal": 4.0, "static": 0.5, "probe": 0.25}

# `probe` is a DIAGNOSTIC action: a very short simulation whose purpose is not to
# discharge an obligation but to find out which action is worth taking next. It
# is what a senior engineer does when they do not know the answer — run the cheap
# experiment that tells you where to spend. It is still real evidence: if it hits
# a counterexample the obligation closes, and if it passes it contributes (a
# little) inductive weight. Nothing about it is exempt from the kernel.


@dataclass
class ResourceLedger:
    budget: float
    spent: float = 0.0
    by_worker: dict = field(default_factory=dict)
    log: list = field(default_factory=list)

    def can_afford(self, method: str) -> bool:
        return self.spent + ACTION_COST[method] <= self.budget + 1e-9

    def charge(self, worker: str, method: str) -> float:
        c = ACTION_COST[method]
        self.spent += c
        self.by_worker[worker] = self.by_worker.get(worker, 0.0) + c
        self.log.append((worker, method, c))
        return c

    def remaining(self) -> float:
        return self.budget - self.spent

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
    phi: str                 # the property / obligation identifier
    weight: float            # criticality weight (feeds R)
    formal_task: str         # sby task that adjudicates it
    inject_bug: bool         # which DUT variant it concerns
    signed_off: bool = False # true only when discharged by witnessed evidence


class TaskBoard:
    def __init__(self, tasks):
        self.tasks = {t.phi: t for t in tasks}

    def weights(self):
        return {t.phi: t.weight for t in self.tasks.values()}

    def open_tasks(self, ks):
        """Obligations not yet discharged (proven) or refuted (bug logged)."""
        return [t for t in self.tasks.values()
                if not ks.proven(t.phi) and not ks.disproven(t.phi)]

    def get(self, phi):
        return self.tasks[phi]


# Per-method costs (arbitrary units; formal is dearer than a sim run).
ACTION_COST = {"sim": 1.0, "formal": 4.0}


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

"""VOE judgment bus — the typed, evidence-backed message layer.

Workers never write the canonical knowledge state directly. They PUBLISH typed
judgments here; the bus adjudicates and merges them into the single canonical
KnowledgeState. This is where three kernel laws become operational:

  * Wit-1 / Fire-1 : a Judgment cannot be constructed without a witness (the
    kernel enforces this at construction), so nothing unwitnessed can be
    published. Generation is separated from adjudication.
  * Struct-1 (confluence) : merge is order-independent — the authoritative
    belief on a property is a function of the evidence, not of who arrived
    first. Warrant precedence: a deductive proof or a counterexample dominates
    inductive passes; among inductive claims, more effective samples win.
  * Sem-1 (formal dominance) : a counterexample settles a property regardless
    of prior passing evidence, and flags the passers as miscalibrated (a signal
    the reputation service consumes).
"""
from __future__ import annotations
from dataclasses import dataclass, field


def _rank(kern, j) -> tuple:
    """Authority of a judgment: (strong?, n_eff). Strong = proof or counterexample."""
    strong = (j.warrant == kern.Warrant.DEDUCTIVE) or j.evidence.get("counterexample", False)
    return (1 if strong else 0, j.evidence.get("n_eff", 0))


@dataclass
class MergeResult:
    phi: str
    accepted: bool                 # did the canonical belief change?
    dominated_worker: str = ""     # a passer contradicted by a counterexample
    note: str = ""


class JudgmentBus:
    def __init__(self, kern, ks):
        self.k = kern
        self.ks = ks                       # the single canonical KnowledgeState
        self.contributor = {}              # phi -> worker owning the authoritative belief
        self.calibration_events = []       # (worker, phi): their pass was later refuted
        self.history = []                  # every publish, for audit

    def publish(self, worker: str, j) -> MergeResult:
        # Fire-1: j exists => it already owns a witness (kernel-enforced).
        phi = j.phi
        self.history.append((worker, phi, j.warrant.name,
                             bool(j.evidence.get("counterexample")), j.witness))
        incoming_cex = j.evidence.get("counterexample", False)

        # Detect a counterexample refuting an earlier inductive pass (miscalibration).
        dominated = ""
        if incoming_cex and phi in self.ks.K and not self.ks.disproven(phi) \
                and not self.ks.proven(phi):
            prev_worker = self.contributor.get(phi, "")
            if prev_worker and prev_worker != worker:
                dominated = prev_worker
                self.calibration_events.append((prev_worker, phi))

        # Confluence: keep whichever belief has higher authority.
        if phi not in self.ks.K:
            self.ks.believe(j); self.contributor[phi] = worker
            return MergeResult(phi, True, dominated, "new belief")
        if _rank(self.k, j) >= _rank(self.k, self.ks.K[phi]):
            self.ks.believe(j); self.contributor[phi] = worker
            return MergeResult(phi, True, dominated, "dominates prior")
        return MergeResult(phi, False, dominated, "dominated by existing")

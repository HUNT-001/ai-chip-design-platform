"""VOE reputation service — trust computed from evidence, never self-assigned.

A worker's reputation is a pure function of what its contributions actually did
to the canonical state: how much residual risk it discharged, whether it found
bugs or produced proofs, how efficiently (value per unit cost), and how well
calibrated it was (did its 'pass' judgments survive later formal scrutiny, or
were they refuted by a counterexample?). Every input is kernel-provided
provenance from the judgment bus and the ledger — the reputation layer invents
no trust of its own.
"""
from __future__ import annotations
from dataclasses import dataclass, field

BUG_BONUS   = 3.0     # finding a real bug (counterexample) is highly valued
PROOF_BONUS = 3.0     # an unbounded proof discharges risk permanently
MISCAL_PEN  = 2.0     # a 'pass' later refuted by a counterexample


@dataclass
class ReputationService:
    value:      dict = field(default_factory=dict)   # attributable risk reduction (ΣΔR)
    proofs:     dict = field(default_factory=dict)
    bugs:       dict = field(default_factory=dict)
    ind_claims: dict = field(default_factory=dict)   # inductive 'pass' contributions

    def _b(self, d, w): return d.get(w, 0.0)

    def record(self, worker, method, merge, dR, ev_status):
        self.value.setdefault(worker, 0.0)
        self.value[worker] += max(0.0, dR)
        if merge.accepted and ev_status == "proved":
            self.proofs[worker] = self.proofs.get(worker, 0) + 1
        if merge.accepted and ev_status == "counterexample":
            self.bugs[worker] = self.bugs.get(worker, 0) + 1
        if method == "sim" and ev_status == "pass":
            self.ind_claims[worker] = self.ind_claims.get(worker, 0) + 1

    def report(self, workers, ledger, bus):
        miscal = {w: 0 for w in workers}
        for prev_worker, _phi in bus.calibration_events:
            miscal[prev_worker] = miscal.get(prev_worker, 0) + 1
        raw = {}
        for w in workers:
            cost = max(ledger.by_worker.get(w, 0.0), 1.0)
            score = (self._b(self.value, w)
                     + BUG_BONUS   * self._b(self.bugs, w)
                     + PROOF_BONUS * self._b(self.proofs, w)
                     - MISCAL_PEN  * miscal[w])
            eff  = score / cost
            ind  = self.ind_claims.get(w, 0)
            calib = 1.0 - (miscal[w] / ind if ind else 0.0)
            raw[w] = max(0.0, eff * calib)
        top = max(raw.values()) if raw and max(raw.values()) > 0 else 1.0
        return {w: {"reputation": round(raw[w] / top, 3),
                    "value": round(self._b(self.value, w), 3),
                    "proofs": self.proofs.get(w, 0),
                    "bugs": self.bugs.get(w, 0),
                    "miscalibrations": miscal[w],
                    "cost": round(ledger.by_worker.get(w, 0.0), 1)}
                for w in workers}

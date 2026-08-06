"""VOE workers — heterogeneous cognition over one shared kernel state.

A Worker is a cognitive archetype, NOT a domain role. Every worker uses the
same evidence channels and the same frozen kernel; they differ only in
*strategy* — how they trade exploration (simulation) against proof (formal).
That heterogeneity is the point: the reputation service then measures, from
evidence alone, which cognitive style actually discharged risk here.

Archetypes shipped:
  * skeptic  — trusts only proofs; reaches for formal early.
  * explorer — samples broadly with simulation first; formal only to close.

Workers never mutate the canonical state. `execute` returns a witnessed
judgment which the VOE publishes to the bus for adjudication (Fire-1).
"""
from __future__ import annotations
from dataclasses import dataclass

from evidence_channels import Evidence

EXPLORE_BUDGET = 4      # explorer's sim appetite before it considers formal


@dataclass
class Proposal:
    worker: "Worker"
    phi: str
    method: str          # "sim" | "formal"
    utility: float
    cost: float


class Worker:
    def __init__(self, name, archetype, kern, formal, sim, nvec=20000, static=None):
        self.name, self.archetype = name, archetype
        self.k, self.formal, self.sim = kern, formal, sim
        self.static = static                  # optional structural channel
        self.nvec = nvec
        self.skip = set()                     # props this worker won't re-attack
        self._seed = (abs(hash(name)) % 1000) + 1

    def _seed_next(self):
        self._seed += 1
        return self._seed

    # ---- deliberation: propose this worker's single best next action ---------
    def propose(self, ks, board, ledger):
        from board import ACTION_COST
        weights = board.weights()
        # only bid on obligations something can actually be run against
        cands = [t for t in board.actionable(ks) if t.phi not in self.skip]
        if not cands:
            return None
        best = max(cands, key=lambda t: self.k.utility(ks, weights, t.phi)[0])
        phi = best.phi
        if best.kind == "structural" and self.static is not None:
            if not ledger.can_afford("static"):
                return None
            return Proposal(self, phi, "static",
                            self.k.utility(ks, weights, phi)[0], ACTION_COST["static"])
        n = ks.n_eff(phi)
        can_sim = self.sim.covers(phi)      # only sample what the TB checks
        if self.archetype == "skeptic" or not can_sim:
            method = "formal" if ledger.can_afford("formal") else "sim"
        else:  # explorer
            if n < EXPLORE_BUDGET and ledger.can_afford("sim"):
                method = "sim"
            elif ledger.can_afford("formal"):
                method = "formal"
            else:
                method = "sim"
        if not ledger.can_afford(method):
            return None
        u = self.k.utility(ks, weights, phi)[0]
        return Proposal(self, phi, method, u, ACTION_COST[method])

    # ---- action: gather real evidence, return a witnessed judgment -----------
    def execute(self, ks, board, phi, method):
        t = board.get(phi)
        if method == "static":
            ev = self.static.check("comb_loops")
            if ev.status == "proved":         # exhaustive over the parsed netlist
                j = self.k.Judgment(phi, self.k.Warrant.DEDUCTIVE,
                                    {"n_eff": ks.n_eff(phi)}, witness=ev.witness)
            elif ev.status == "counterexample":
                j = self.k.Judgment(phi, self.k.Warrant.INDUCTIVE,
                                    {"n_eff": ks.n_eff(phi), "counterexample": True},
                                    witness=ev.witness)
            else:
                return ev, None
            return ev, j
        if method == "sim":
            # Guard: never credit a testbench pass to a property it does not check.
            if not self.sim.covers(phi):
                return Evidence("sim", "unsupported", witness="",
                                detail=f"testbench does not check {phi}"), None
            ev = self.sim.run(inject_bug=t.inject_bug, seed=self._seed_next(), nvec=self.nvec)
            if ev.status == "pass":
                j = self.k.Judgment(phi, self.k.Warrant.INDUCTIVE,
                                    {"n_eff": ks.n_eff(phi) + 1}, witness=ev.witness)
            elif ev.status == "counterexample":
                j = self.k.Judgment(phi, self.k.Warrant.INDUCTIVE,
                                    {"n_eff": ks.n_eff(phi), "counterexample": True},
                                    witness=ev.witness)
            else:
                return ev, None
            return ev, j
        # formal
        ev = self.formal.prove(t.formal_task)
        if ev.status == "proved":
            j = self.k.Judgment(phi, self.k.Warrant.DEDUCTIVE,
                                {"n_eff": ks.n_eff(phi)}, witness=ev.witness)
        elif ev.status == "counterexample":
            j = self.k.Judgment(phi, self.k.Warrant.INDUCTIVE,
                                {"n_eff": ks.n_eff(phi), "counterexample": True},
                                witness=ev.witness)
        elif ev.status == "bounded_pass":
            j = self.k.Judgment(phi, self.k.Warrant.INDUCTIVE,
                                {"n_eff": ks.n_eff(phi) + 1}, witness=ev.witness)
            self.skip.add(phi)               # bounded formal won't discharge; stop escalating
        elif ev.status == "gate_failed":
            # Vacuity gate not armed: the run passed but cannot be certified.
            # Record NOTHING (no warrant is justified) and stop re-attacking.
            self.skip.add(phi)
            return ev, None
        elif ev.status == "inconclusive":
            # k-induction neither proved nor refuted it. No warrant is
            # justified; the property likely needs a strengthening invariant.
            self.skip.add(phi)
            return ev, None
        else:
            return ev, None
        return ev, j

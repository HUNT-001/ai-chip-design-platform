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

EXPLORE_BUDGET = 4      # explorer's sim appetite before it considers formal


@dataclass
class Proposal:
    worker: "Worker"
    phi: str
    method: str          # "sim" | "formal"
    utility: float
    cost: float


class Worker:
    def __init__(self, name, archetype, kern, formal, sim, nvec=20000):
        self.name, self.archetype = name, archetype
        self.k, self.formal, self.sim = kern, formal, sim
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
        cands = [t for t in board.open_tasks(ks) if t.phi not in self.skip]
        if not cands:
            return None
        phi = max(cands, key=lambda t: self.k.utility(ks, weights, t.phi)[0]).phi
        n = ks.n_eff(phi)
        if self.archetype == "skeptic":
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
        if method == "sim":
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
        else:
            return ev, None
        return ev, j

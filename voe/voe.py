"""VOE orchestrator — the Verification Operating Environment on the frozen kernel.

Ties the layers together: a shared canonical KnowledgeState, a judgment bus
(confluent merge), a resource ledger (finite budget), a reputation service, and
a set of heterogeneous workers. The scheduler realises 'attention = argmax
utility': each round every worker proposes its best action, and the environment
grants the action with the highest risk-reduction-per-cost that the budget can
afford. Nothing here extends the kernel — the OS-grade behaviours (transactions,
security gate, economics, multi-agent merge) are consequences of kernel laws,
per VSA_KERNEL_FREEZE_AND_ROADMAP.md.
"""
from __future__ import annotations
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "phase3"))

from engineer import load_kernel                       # frozen-kernel importer
from evidence_channels import FormalChannel, SimChannel
from board import TaskBoard, ResourceLedger
from bus import JudgmentBus
from reputation import ReputationService
from workers import Worker


class VOE:
    def __init__(self, tasks, budget=40.0, mock=False):
        self.k = load_kernel()
        self.board = TaskBoard(tasks)
        self.ks = self.k.KnowledgeState()
        self.bus = JudgmentBus(self.k, self.ks)
        self.ledger = ResourceLedger(budget=budget)
        self.rep = ReputationService()
        formal, sim = FormalChannel(mock=mock), SimChannel(mock=mock)
        self.workers = [
            Worker("skeptic",  "skeptic",  self.k, formal, sim),
            Worker("explorer", "explorer", self.k, formal, sim),
        ]

    def _density(self, p):
        # attention metric: expected risk-reduction per unit cost, skeptic wins ties
        return (p.utility / p.cost, -self.workers.index(p.worker))

    def run(self, max_steps=60):
        k, ks, w = self.k, self.ks, self.board.weights()
        prev = k.R(ks, w)
        print(f"  tasks={len(self.board.tasks)}  workers={[x.name for x in self.workers]}"
              f"  budget={self.ledger.budget}  initial R={prev:.3f}\n")
        for step in range(1, max_steps + 1):
            proposals = [p for p in (wk.propose(ks, self.board, self.ledger) for wk in self.workers) if p]
            proposals = [p for p in proposals if self.ledger.can_afford(p.method)]
            if not proposals:
                break
            pick = max(proposals, key=self._density)
            ev, j = pick.worker.execute(ks, self.board, pick.phi, pick.method)
            if ev.status == "error":
                print(f"  step {step:2d}  {pick.worker.name:8s} {pick.method:6s} {pick.phi:16s}"
                      f" -> ERROR: {ev.detail}")
                break
            self.ledger.charge(pick.worker.name, pick.method)
            Rbefore = k.R(ks, w)
            merge = self.bus.publish(pick.worker.name, j)
            Rafter = k.R(ks, w)
            self.rep.record(pick.worker.name, pick.method, merge, Rbefore - Rafter, ev.status)
            curR, laws = k.check_laws(ks, w, prev, "update")
            ok = all(v for _, v in laws)
            tag = f"  [refutes {merge.dominated_worker}'s pass]" if merge.dominated_worker else ""
            print(f"  step {step:2d}  {pick.worker.name:8s} {pick.method:6s} {pick.phi:16s}"
                  f" -> {ev.status:14s} R={curR:.3f} spent={self.ledger.spent:.0f}"
                  f" laws={'OK' if ok else 'VIOLATION'}{tag}")
            if not ok:
                print("  LAW VIOLATION:", laws); break
            prev = curR
        self._report()

    def _report(self):
        k, ks, w = self.k, self.ks, self.board.weights()
        for t in self.board.tasks.values():
            t.signed_off = ks.proven(t.phi)
        signed = [t.phi for t in self.board.tasks.values() if t.signed_off]
        bugs   = [phi for phi in self.board.tasks if ks.disproven(phi)]
        resid  = [phi for phi in self.board.tasks
                  if not ks.proven(phi) and not ks.disproven(phi)]
        print(f"\n  final R = {k.R(ks, w):.3f}   budget spent = {self.ledger.spent:.0f}/{self.ledger.budget:.0f}")
        print(f"  signed off (witnessed proof): {signed}")
        print(f"  bugs found: {bugs}")
        print(f"  residual (undischarged): {resid}")
        print("  reputation (from evidence only):")
        for name, r in self.rep.report([x.name for x in self.workers], self.ledger, self.bus).items():
            print(f"    {name:8s} rep={r['reputation']:.3f}  proofs={r['proofs']} bugs={r['bugs']}"
                  f" miscal={r['miscalibrations']} cost={r['cost']} value={r['value']}")

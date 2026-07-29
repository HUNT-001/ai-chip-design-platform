"""Phase-3 autonomous engineer — real evidence wired into the FROZEN VSA kernel.

This is the Phase-3 milestone from the roadmap: ONE autonomous engineer that
reads a property manifest, plans actions by kernel utility (exploit + explore -
cost), gathers REAL evidence from Verilator (simulation) and SymbiYosys
(formal), folds each result into the kernel as a typed, witnessed judgment, and
drives the residual risk R down — with every kernel law re-checked at each step.

Nothing here changes the kernel. `docs/vsa_reference.py` is imported unmodified;
the only swap versus its built-in demo is that the *simulated* DUT is replaced
by the real evidence channels. Hypothesis generation (Gamma) is heuristic /
manifest-driven — no LLM, exactly the Phase-3 plan.

The narrative it demonstrates on the shipped ALU slice:
  * a good property: sim accumulates inductive passes, formal then PROVES it
    (deductive) -> its risk goes to 0;
  * a buggy property: random sim keeps PASSING (it never hits the narrow
    DEAD_BEEF defect), yet formal returns a COUNTEREXAMPLE -> the bug is found.
    That is Sem-1 (formal dominance) happening on real tool output, not a story.

Run:
    python engineer.py --mock     # no toolchain needed (pipeline + law checks)
    python engineer.py --real     # invokes verilator + sby for real evidence
"""
from __future__ import annotations
import os, sys, io, contextlib, importlib.util
from dataclasses import dataclass

from evidence_channels import FormalChannel, SimChannel

HERE   = os.path.dirname(os.path.abspath(__file__))
KERNEL = os.path.normpath(os.path.join(HERE, "..", "docs", "vsa_reference.py"))


def load_kernel():
    """Import the frozen VSA kernel WITHOUT running its bottom-of-file demo."""
    spec = importlib.util.spec_from_file_location("vsa_kernel", KERNEL)
    k = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):   # silence the demo prints
        spec.loader.exec_module(k)
    return k


@dataclass
class Prop:
    phi: str
    weight: float
    formal_task: str      # sby task that adjudicates this property
    inject_bug: bool      # which DUT variant this property is about


# The heuristic hypothesis set (Gamma) for the shipped slice.
DEFAULT_MANIFEST = [
    Prop("alu_equiv[clean]", 5.0, formal_task="good",  inject_bug=False),
    Prop("alu_equiv[dut2]",  5.0, formal_task="buggy", inject_bug=True),
]

SIM_BUDGET = 3            # distinct-seed sim runs before formal-closing a property


class Engineer:
    def __init__(self, manifest=None, mock=False):
        self.k = load_kernel()
        self.props = list(manifest or DEFAULT_MANIFEST)
        self.spec = {p.phi: p for p in self.props}
        self.weights = {p.phi: p.weight for p in self.props}
        self.ks = self.k.KnowledgeState()
        self.formal = FormalChannel(mock=mock)
        self.sim = SimChannel(mock=mock)
        self.log = []

    # -- conservative n_eff: +1 effective sample per distinct-seed run --------
    # (coverage-weighted effective-N is future work; see attack-sheet 3.2.)
    def _sim_step(self, p: Prop, seed: int):
        ev = self.sim.run(inject_bug=p.inject_bug, seed=seed, nvec=20000)
        if ev.status == "pass":
            j = self.k.Judgment(p.phi, self.k.Warrant.INDUCTIVE,
                                 {"n_eff": self.ks.n_eff(p.phi) + 1}, witness=ev.witness)
            self.ks.believe(j)
        elif ev.status == "counterexample":
            j = self.k.Judgment(p.phi, self.k.Warrant.INDUCTIVE,
                                 {"n_eff": self.ks.n_eff(p.phi), "counterexample": True},
                                 witness=ev.witness)
            self.ks.believe(j)
        return ev

    def _formal_step(self, p: Prop):
        ev = self.formal.prove(p.formal_task)
        if ev.status == "proved":
            j = self.k.Judgment(p.phi, self.k.Warrant.DEDUCTIVE,
                                 {"n_eff": self.ks.n_eff(p.phi)}, witness=ev.witness)
            self.ks.believe(j)
        elif ev.status == "counterexample":
            j = self.k.Judgment(p.phi, self.k.Warrant.INDUCTIVE,
                                 {"n_eff": self.ks.n_eff(p.phi), "counterexample": True},
                                 witness=ev.witness)
            self.ks.believe(j)
        return ev

    def _candidates(self):
        return [p for p in self.props
                if not self.ks.proven(p.phi) and not self.ks.disproven(p.phi)]

    def _pick(self, cands):
        # planner = argmax kernel utility (exploit + explore - cost)
        return max(cands, key=lambda p: self.k.utility(self.ks, self.weights, p.phi)[0])

    def run_campaign(self, max_steps=40):
        k, ks, w = self.k, self.ks, self.weights
        prev = k.R(ks, w)
        print(f"  properties = {len(self.props)}   initial R = {prev:.3f}")
        seed = 1
        for step in range(1, max_steps + 1):
            cands = self._candidates()
            if not cands:
                break
            p = self._pick(cands)
            # strategy: explore cheaply with sim until budget, then formal-close
            if ks.n_eff(p.phi) < SIM_BUDGET:
                ev = self._sim_step(p, seed); seed += 1; action = "sim"
            else:
                ev = self._formal_step(p); action = "formal"
            curR, laws = k.check_laws(ks, w, prev, "update")
            ok = all(v for _, v in laws)
            self.log.append((step, action, p.phi, ev.status, curR, ok))
            print(f"  step {step:2d}  {action:6s} {p.phi:16s} -> {ev.status:14s}"
                  f"  R={curR:.3f}  laws={'OK' if ok else 'VIOLATION'}")
            if not ok:
                print("  LAW VIOLATION:", laws); break
            prev = curR
        finalR = k.R(ks, w)
        proven = [p.phi for p in self.props if ks.proven(p.phi)]
        bugs   = [p.phi for p in self.props if ks.disproven(p.phi)]
        print(f"\n  final R = {finalR:.3f}")
        print(f"  proven (deductive): {proven}")
        print(f"  bugs found (counterexample, left residual pool per Sem-1): {bugs}")
        _, laws = k.check_laws(ks, w, finalR, "update")
        print(f"  laws at halt: all-hold = {all(v for _, v in laws)}  {[n for n,_ in laws]}")
        # provenance: every belief owns a real witness (Wit-1)
        print("  witnesses (evidence provenance):")
        for phi, j in ks.K.items():
            print(f"    {phi:16s} [{j.warrant.name:10s}] <- {j.witness}")
        return finalR, proven, bugs


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    print("=== Phase-3 autonomous engineer on real evidence channels ===")
    print(f"    mode = {'MOCK (no toolchain)' if mock else 'REAL (verilator + sby)'}\n")
    Engineer(mock=mock).run_campaign()


if __name__ == "__main__":
    main()

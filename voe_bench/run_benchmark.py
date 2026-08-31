"""The diverse-regime benchmark — can diagnosis pay when ambiguity is real?

Experiment 4A left one question unanswerable: its board had a single ambiguous
class, so a diagnostic probe had almost nothing to buy and beat the blind policy
by ~1%. That was a statement about the benchmark, not about diagnosis.

This board has 20 obligations across 6 designs and 6 verification regimes, and —
the point — **every signature class mixes true properties with mutant-refuted
false ones**. Structure cannot separate them: they share RTL, property kind and
sequential character. Only evidence can. So a cheap probe now has genuine
ambiguity to resolve, on most of the board rather than one corner of it.

    python run_benchmark.py --mock | --real

Reported: every policy, the realisable ceiling (class statistics only) and the
retrospective ceiling (knows each truth value), plus a per-regime breakdown so a
single pooled number cannot hide a policy that is excellent on one regime and
useless on another.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("voe", "phase3"):
    sys.path.insert(0, os.path.join(HERE, "..", p))
sys.path.insert(0, HERE)

from board import Task, ACTION_COST
from evidence_channels import FormalChannel, SimChannel
from evaluation import run_campaign, aggregate, compare_aggregates
from obligation_state import ObligationFeatures, probe_structure
from policy import (POLICY_SET_V3, HUMAN_ORG, ADAPTIVE, OBLIGATION, DIAGNOSTIC,
                    PolicyWorker)
from voe import VOE
from registry import DESIGNS, OBLIGATIONS, OWNER, KIND, SBY_TASK, IS_MUTANT, summary

REPEATS = 5
BUDGET = 200.0

TASKS = [Task(phi, w, formal_task=task, inject_bug=mut)
         for phi, w, task, _d, _k, mut in OBLIGATIONS]


class FormalRouter:
    def __init__(self, mock):
        self.ch = {n: FormalChannel(sby_file=d["sby"], mock=mock,
                                    combinational=d["combinational"],
                                    negative_control=d["control"],
                                    mock_timeouts=d.get("mock_timeouts", ()))
                   for n, d in DESIGNS.items()}
        self._owner = {}
        for phi, task in SBY_TASK.items():
            self._owner[(OWNER[phi], task)] = OWNER[phi]
        self._task_to_design = {}
        for phi, task in SBY_TASK.items():
            self._task_to_design.setdefault(task, OWNER[phi])

    def prove(self, sby_task):
        return self.ch[self._task_to_design[sby_task]].prove(sby_task)

    def gate_status(self):
        bad = [(n, c.gate_status()) for n, c in self.ch.items()
               if not c.gate_status()[0]]
        if bad:
            return False, f"{bad[0][0]}: {bad[0][1][1]}"
        return True, "every design's negative control failed as required"


class SimRouter:
    def __init__(self, mock):
        self.ch = {}
        for n, d in DESIGNS.items():
            if not d["src"]:
                self.ch[n] = None            # formal-only regime (no testbench)
                continue
            defines = d["defines"]
            if defines is None and d["wrap"]:
                g, m = d["wrap"]
                defines = lambda bug, g=g, m=m: [f"DUT={m if bug else g}"]
            self.ch[n] = SimChannel(mock=mock, sources=d["src"], top=d["top"],
                                    defines_for=defines, covers=d["covers"],
                                    mock_finds_bug=d["finds_bug"])

    def _for(self, phi):
        return self.ch.get(OWNER.get(phi))

    def covers(self, phi):
        c = self._for(phi)
        return bool(c) and c.covers(phi)

    def run(self, inject_bug=False, seed=1, nvec=20000, phi=None):
        return self._for(phi).run(inject_bug=inject_bug, seed=seed, nvec=nvec)

    @property
    def _control_state(self):
        return None


def build_features():
    feats = {}
    for phi in OWNER:
        arith, seq, depth = probe_structure(DESIGNS[OWNER[phi]]["rtl"])
        feats[phi] = ObligationFeatures(KIND[phi], arith, seq, depth)
    return feats


def build(policy, mock, routers, seed, feats):
    formal, sim = routers
    v = VOE([Task(**vars(t)) for t in TASKS], budget=BUDGET, mock=mock,
            formal=formal, sim=sim)
    w = PolicyWorker(policy.name, policy, v.k, formal, sim, seed=seed)
    if policy.obligation_conditioned:
        w.attach_features(feats)
    v.workers = [w]
    return v


def outcome_table(routers, feats):
    formal, sim = routers
    rows = {}
    for t in TASKS:
        closes = {}
        if sim.covers(t.phi):
            ev = sim.run(inject_bug=t.inject_bug, seed=7, nvec=20000, phi=t.phi)
            closes["sim"] = (ev.status == "counterexample")
        else:
            closes["sim"] = False
        ev = formal.prove(t.formal_task)
        closes["formal"] = ev.status in ("proved", "counterexample")
        rows[t.phi] = {"sig": feats[t.phi].signature(), "w": t.weight,
                       "closes": closes, "design": OWNER[t.phi]}
    return rows


def retrospective(rows):
    closed = cost = 0.0
    for r in rows.values():
        opts = [(m, ACTION_COST[m]) for m in ("sim", "formal") if r["closes"].get(m)]
        if opts:
            _m, c = min(opts, key=lambda o: o[1])
            closed += r["w"]; cost += c
    return (closed / cost if cost else 0.0), closed, cost


def realisable(rows):
    """Best FIXED action per signature — class statistics only, no hidden truth."""
    by_sig = {}
    for r in rows.values():
        by_sig.setdefault(r["sig"], []).append(r)
    choice, closed, cost = {}, 0.0, 0.0
    for sig, members in by_sig.items():
        best, best_v = None, -1.0
        for m in ("sim", "formal"):
            wc = sum(r["w"] for r in members if r["closes"].get(m))
            c = ACTION_COST[m] * len(members)
            v = wc / c if c else 0.0
            if v > best_v:
                best, best_v = m, v
        choice[sig] = (best, len(members))
        closed += sum(r["w"] for r in members if r["closes"].get(best))
        cost += ACTION_COST[best] * len(members)
    return (closed / cost if cost else 0.0), closed, cost, choice


def main():
    mock = "--mock" in sys.argv or "--real" not in sys.argv
    print("=== Diverse-regime benchmark ===")
    print(f"    {summary()}")
    print(f"    budget = {BUDGET}   mode = {'MOCK' if mock else 'REAL'}\n")

    feats = build_features()
    sigs = {}
    for phi, f in feats.items():
        sigs.setdefault(f.signature(), []).append(phi)
    print("=== signature classes (ambiguity lives INSIDE these) ===")
    for sig, members in sigs.items():
        t = sum(1 for m in members if not IS_MUTANT[m])
        fl = sum(1 for m in members if IS_MUTANT[m])
        print(f"    {str(sig):34s} {len(members):2d} obligations "
              f"({t} true, {fl} false)")

    routers = (FormalRouter(mock), SimRouter(mock))
    rows = outcome_table(routers, feats)
    retro_E, _rc, _rk = retrospective(rows)
    real_E, _xc, _xk, choice = realisable(rows)

    print("\n=== ceilings ===")
    print(f"  realisable    E={real_E:.3f}   (best fixed action per class)")
    for sig, (m, n) in choice.items():
        print(f"      {str(sig):34s} -> {m}  ({n} obligations)")
    print(f"  retrospective E={retro_E:.3f}   (knows each truth value)")
    print(f"  value of information nobody has yet: {retro_E - real_E:.3f}")

    results = []
    for p in POLICY_SET_V3:
        for i in range(REPEATS):
            results.append(run_campaign(build(p, mock, routers, 1000 + i, feats),
                                        "bench", p.name))
        print(f"    {p.name:14s} done", flush=True)

    aggs = aggregate(results)
    print()
    print(compare_aggregates(aggs, incumbent=HUMAN_ORG.name))

    by = {a.policy: a for a in aggs}
    print("\n=== against the reachable ceiling ===")
    for name in (HUMAN_ORG.name, ADAPTIVE.name, OBLIGATION.name, DIAGNOSTIC.name):
        a = by[name]
        print(f"  {name:14s} E={a.mean_E:.3f}   {a.mean_E/real_E:6.1%} of realisable")
    print(f"  {'realisable':14s} E={real_E:.3f}")

    d, o = by[DIAGNOSTIC.name], by[OBLIGATION.name]
    delta = (d.mean_E - o.mean_E) / o.mean_E if o.mean_E else 0.0
    print("\n=== did buying information pay, on a board with real ambiguity? ===")
    print(f"  G-diagnostic {d.mean_E:.3f}  vs  F-obligation {o.mean_E:.3f}   "
          f"({delta:+.1%})")
    if delta > 0.05:
        print("  Yes. With ambiguity spread across most classes, spending a")
        print("  cheap probe to learn which action deserves the expensive one")
        print("  beat committing blind. Experiment 4A could not show this")
        print("  because its board had a single ambiguous class.")
    elif delta < -0.02:
        print("  No — the probes cost more than the information returned. Worth")
        print("  knowing: diagnosis is not free, and a policy that probes")
        print("  indiscriminately pays for information it cannot use.")
    else:
        print("  Still roughly neutral. Either the probe is priced wrong, or the")
        print("  ambiguity it resolves is not the binding constraint here.")


if __name__ == "__main__":
    main()

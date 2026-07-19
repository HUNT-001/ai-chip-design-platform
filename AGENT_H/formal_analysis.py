"""
AGENT_H.formal_analysis — Formal Coverage, Debug & Assertion Mining (T76)
==========================================================================

The analysis layer on top of `formal_engine`: what a proof actually covers, why
a counterexample happened, and where properties come from in the first place.

Formal coverage
---------------
- `cover_property()` — a *cover* asks the opposite question to an assert: is this
  scenario **reachable at all**? An unreachable cover means the stimulus can
  never exercise that case (dead scenario / over-constrained environment).
- `unreachable_states()` — enumerates declared states that no path reaches
  within the bound: dead code in the control logic.
- `cone_of_influence()` — the transitive fan-in of the property's variables
  through the transition relation. Everything outside the COI provably cannot
  affect the property, so it is sound to remove — this is the standard
  abstraction that makes model checking tractable, and the reduction ratio is
  reported.
- `proof_coverage()` — of the declared state variables and properties, how many
  are actually constrained by *some* proven property (a proof that touches
  nothing is worthless).

Formal debug
------------
- `detect_vacuity()` — a property like `G(a -> b)` passes **vacuously** if `a` is
  never true. The standard check: the property must still hold when strengthened
  to require the antecedent to occur; if `a` is unreachable, the pass is
  meaningless. Vacuous passes are the most dangerous result in formal, because
  they look like success.
- `minimize_counterexample()` — delta-debugging over trace steps and over
  variable assignments: repeatedly drop what is not needed to still violate the
  property, yielding the shortest explanation.
- `explain_counterexample()` — a human-readable causal walk: which variables
  changed at each step and which change first makes the property fail.
- `proof_core()` — the minimal subset of assumptions/constraints that makes the
  property hold (via `unsat_core`), i.e. what the proof actually depends on.

Assertion mining (AI-assisted property generation)
---------------------------------------------------
`mine_assertions()` infers candidate properties from observed traces in the
Daikon style — propose a large space of templates, then **discard every template
falsified by any observed sample**. What survives is consistent with all data.
Templates covered:
- constant / never-changes (`always x == c`)
- implication between signals (`always (a -> b)`)
- mutual exclusion (`never (a & b)`)
- one-hot / at-most-one over a signal group
- next-state relations (`always (a |=> b)`, i.e. a implies b in the next cycle)
- eventual response (`a |-> ##[0:k] b`) within a bounded window

Each candidate carries a **support count** (how many samples exercised it) and a
confidence, and `rank_properties()` orders them by support × specificity so an
engineer reviews the informative ones first. Mined assertions are *candidates*,
explicitly labelled as such — they describe observed behaviour, which is not the
same as intended behaviour.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Set, Tuple)

try:                                                # package or standalone
    from .formal_engine import (Expr, Var, Not, And, Or, Implies, Iff, Const,
                                TransitionSystem, big_and, big_or, satisfiable,
                                reachable, bmc_safety, to_cnf, solve,
                                unsat_core, shift, at)
except ImportError:                                 # pragma: no cover
    from formal_engine import (Expr, Var, Not, And, Or, Implies, Iff, Const,   # type: ignore
                               TransitionSystem, big_and, big_or, satisfiable,
                               reachable, bmc_safety, to_cnf, solve,
                               unsat_core, shift, at)

log = logging.getLogger("AGENT_H.formal_analysis")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "formal_analysis"
DEFAULT_DEPTH = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Formal coverage
# ─────────────────────────────────────────────────────────────────────────────
def cover_property(system: TransitionSystem, scenario: Expr,
                   depth: int = DEFAULT_DEPTH) -> Dict[str, Any]:
    """Is ``scenario`` reachable? An uncovered scenario is dead stimulus."""
    r = reachable(system, scenario, depth)
    return {
        "scenario": repr(scenario),
        "covered": bool(r.get("reachable")),
        "steps": r.get("steps"),
        "witness": r.get("witness"),
        "verdict": "covered" if r.get("reachable") else "unreachable",
        "note": None if r.get("reachable") else
                "scenario never occurs within the bound — dead code or an "
                "over-constrained environment",
    }


def unreachable_states(system: TransitionSystem,
                       states: Dict[str, Expr],
                       depth: int = DEFAULT_DEPTH) -> Dict[str, Any]:
    """Which declared states are never reached within the bound?"""
    reached, dead = [], []
    for name, pred in (states or {}).items():
        if reachable(system, pred, depth).get("reachable"):
            reached.append(name)
        else:
            dead.append(name)
    total = len(states or {})
    return {
        "reached": sorted(reached),
        "unreachable": sorted(dead),
        "reachable_count": len(reached),
        "total": total,
        "state_coverage": round(len(reached) / total, 4) if total else 1.0,
    }


def cone_of_influence(system: TransitionSystem, prop: Expr,
                      deps: Optional[Dict[str, Set[str]]] = None
                      ) -> Dict[str, Any]:
    """Transitive fan-in of the property's variables.

    ``deps`` maps a state variable to the variables its next-state function
    reads. When omitted it is derived conservatively from the transition
    relation: every variable appearing in `trans` is treated as a potential
    input to every next-state variable that also appears (sound but coarse).
    """
    prop_vars = {v.split("@")[0] for v in prop.vars()}
    if deps is None:
        tvars = {v.rstrip("'") for v in system.trans.vars()}
        deps = {v: set(tvars) for v in system.variables}
    coi: Set[str] = set(prop_vars)
    frontier = list(prop_vars)
    while frontier:
        v = frontier.pop()
        for d in deps.get(v, set()):
            if d not in coi:
                coi.add(d)
                frontier.append(d)
    coi &= set(system.variables) | prop_vars
    total = len(system.variables) or 1
    return {
        "property": repr(prop),
        "cone": sorted(coi),
        "cone_size": len(coi),
        "total_variables": len(system.variables),
        "removed": sorted(set(system.variables) - coi),
        "reduction_ratio": round(1 - (len(coi) / total), 4),
        "sound": True,
        "note": "variables outside the cone provably cannot affect the property",
    }


def proof_coverage(system: TransitionSystem,
                   results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How much of the design do the *passing* properties actually constrain?"""
    covered: Set[str] = set()
    proven = 0
    for r in results or []:
        if r.get("verdict") in ("proved", "bounded_proof"):
            proven += 1
            for v in system.variables:
                if v in str(r.get("property", "")):
                    covered.add(v)
    total = len(system.variables) or 1
    untouched = sorted(set(system.variables) - covered)
    return {
        "properties_total": len(results or []),
        "properties_proven": proven,
        "variables_constrained": sorted(covered),
        "variables_unconstrained": untouched,
        "proof_coverage": round(len(covered) / total, 4),
        "warning": ("some state variables are not mentioned by any proven "
                    "property" if untouched else None),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Formal debug
# ─────────────────────────────────────────────────────────────────────────────
def detect_vacuity(system: TransitionSystem, prop: Expr,
                   depth: int = DEFAULT_DEPTH) -> Dict[str, Any]:
    """A `G(a -> b)` that passes only because `a` never happens is vacuous."""
    if not isinstance(prop, Implies):
        # non-implication: vacuous iff the property is trivially valid
        triv = not satisfiable(Not(prop))[0] and not prop.vars()
        return {"property": repr(prop), "vacuous": bool(triv),
                "reason": "constant-true property" if triv else None}
    antecedent = prop.a
    r = reachable(system, antecedent, depth)
    vac = not r.get("reachable")
    return {
        "property": repr(prop),
        "antecedent": repr(antecedent),
        "antecedent_reachable": bool(r.get("reachable")),
        "vacuous": vac,
        "reason": ("antecedent is never satisfiable in any reachable state, so "
                   "the implication holds trivially" if vac else None),
        "severity": "HIGH" if vac else None,
    }


def minimize_counterexample(system: TransitionSystem, prop: Expr,
                            trace: Sequence[Dict[str, bool]]
                            ) -> Dict[str, Any]:
    """Delta-debug a counterexample: shortest prefix that still violates, then
    drop variable assignments that are not needed to explain the failure."""
    trace = [dict(s) for s in (trace or [])]
    if not trace:
        return {"minimized": [], "original_length": 0, "removed_steps": 0}

    def violates(t: Sequence[Dict[str, bool]]) -> bool:
        return any(not prop.eval(s) for s in t)

    # 1. shortest violating prefix
    short = trace
    for k in range(1, len(trace) + 1):
        if violates(trace[:k]):
            short = trace[:k]
            break
    # 2. drop irrelevant variables from the failing state
    failing = dict(short[-1])
    relevant = dict(failing)
    for v in list(relevant):
        trial = dict(relevant)
        trial.pop(v)
        probe = dict(failing)
        # a variable is irrelevant if flipping it keeps the property violated
        probe[v] = not probe[v]
        if not prop.eval(probe):
            relevant.pop(v, None)
    return {
        "minimized": short,
        "original_length": len(trace),
        "minimized_length": len(short),
        "removed_steps": len(trace) - len(short),
        "relevant_variables": sorted(relevant),
        "failing_state": failing,
    }


def explain_counterexample(prop: Expr,
                           trace: Sequence[Dict[str, bool]]) -> Dict[str, Any]:
    """Human-readable causal walk through a counterexample."""
    steps: List[Dict[str, Any]] = []
    prev: Optional[Dict[str, bool]] = None
    first_bad: Optional[int] = None
    for i, st in enumerate(trace or []):
        changed = ({k: (prev[k], v) for k, v in st.items()
                    if prev is not None and prev.get(k) != v}
                   if prev is not None else {})
        holds = prop.eval(st)
        if not holds and first_bad is None:
            first_bad = i
        steps.append({"step": i, "state": dict(st),
                      "changed": {k: f"{a}->{b}" for k, (a, b) in changed.items()},
                      "property_holds": holds})
        prev = st
    summary = (f"property first fails at step {first_bad}"
               if first_bad is not None else "property holds on this trace")
    trigger = {}
    if first_bad is not None and first_bad > 0:
        trigger = steps[first_bad]["changed"]
    return {"property": repr(prop), "steps": steps,
            "first_failure_step": first_bad, "trigger": trigger,
            "summary": summary}


def proof_core(system: TransitionSystem, prop: Expr,
               assumptions: Sequence[Expr],
               depth: int = 4) -> Dict[str, Any]:
    """Minimal subset of ``assumptions`` needed for the property to hold."""
    assumptions = list(assumptions or [])
    body = And(system.unroll(depth),
               big_or([Not(shift(prop, i)) for i in range(depth + 1)]))
    cnf, top = to_cnf(body)
    assume_lits: List[int] = []
    for a in assumptions:
        sub_cnf, lit = to_cnf(big_and([shift(a, i) for i in range(depth + 1)]),
                              cnf)
        assume_lits.append(lit)
    lits = [top] + assume_lits
    res = solve(cnf, lits)
    if res.sat:
        return {"property": repr(prop), "holds": False,
                "core": [], "note": "property does not hold under these "
                                    "assumptions — no proof core exists"}
    core_lits = set(unsat_core(cnf, lits))
    core = [repr(a) for a, l in zip(assumptions, assume_lits) if l in core_lits]
    return {
        "property": repr(prop),
        "holds": True,
        "core": core,
        "core_size": len(core),
        "assumptions_total": len(assumptions),
        "unused_assumptions": [repr(a) for a, l in zip(assumptions, assume_lits)
                               if l not in core_lits],
        "note": "the proof depends only on the listed assumptions",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Assertion mining
# ─────────────────────────────────────────────────────────────────────────────
def _samples(traces: Sequence[Sequence[Dict[str, bool]]]
             ) -> List[Sequence[Dict[str, bool]]]:
    out = []
    for t in traces or []:
        if isinstance(t, (list, tuple)) and t and isinstance(t[0], dict):
            out.append(t)
        elif isinstance(t, dict):
            out.append([t])
    return out


def mine_assertions(traces: Sequence[Sequence[Dict[str, bool]]],
                    signals: Optional[Sequence[str]] = None,
                    window: int = 3,
                    min_support: int = 1) -> List[Dict[str, Any]]:
    """Infer candidate assertions consistent with every observed trace.

    Daikon-style: propose templates, then eliminate any falsified by a sample.
    Survivors are *candidates*, not verified intent.
    """
    ts = _samples(traces)
    if not ts:
        return []
    sigs = sorted(signals or {k for t in ts for st in t for k in st})
    cands: List[Dict[str, Any]] = []

    def add(kind: str, expr_repr: str, support: int, detail: str,
            specificity: float) -> None:
        if support >= min_support:
            cands.append({"kind": kind, "assertion": expr_repr,
                          "support": support, "specificity": specificity,
                          "detail": detail, "status": "candidate"})

    # 1. constants
    for s in sigs:
        vals = {st[s] for t in ts for st in t if s in st}
        n = sum(1 for t in ts for st in t if s in st)
        if len(vals) == 1 and n:
            add("constant", f"always ({s} == {int(next(iter(vals)))})", n,
                f"{s} never changed across {n} samples", 0.4)

    # 2. implication and mutual exclusion over signal pairs
    for a, b in itertools.combinations(sigs, 2):
        imp_ab = imp_ba = mutex = True
        sup_a = sup_b = sup_m = 0
        for t in ts:
            for st in t:
                if a not in st or b not in st:
                    continue
                if st[a]:
                    sup_a += 1
                    if not st[b]:
                        imp_ab = False
                if st[b]:
                    sup_b += 1
                    if not st[a]:
                        imp_ba = False
                if not (st[a] and st[b]):
                    sup_m += 1
                else:
                    mutex = False
        if imp_ab and sup_a:
            add("implication", f"always ({a} -> {b})", sup_a,
                f"{a} held {sup_a}x, {b} always held with it", 0.8)
        if imp_ba and sup_b:
            add("implication", f"always ({b} -> {a})", sup_b,
                f"{b} held {sup_b}x, {a} always held with it", 0.8)
        if mutex and sup_m:
            add("mutual_exclusion", f"never ({a} && {b})", sup_m,
                f"{a} and {b} never held simultaneously in {sup_m} samples", 0.9)

    # 3. one-hot over the whole signal group
    onehot = True
    sup_oh = 0
    for t in ts:
        for st in t:
            present = [st.get(s, False) for s in sigs]
            if not present:
                continue
            if sum(1 for p in present if p) == 1:
                sup_oh += 1
            else:
                onehot = False
    if onehot and sup_oh and len(sigs) > 1:
        add("one_hot", f"always $onehot({{{', '.join(sigs)}}})", sup_oh,
            f"exactly one of {len(sigs)} signals held in every sample", 1.0)

    # 4. next-cycle implication a |=> b
    for a, b in itertools.permutations(sigs, 2):
        ok = True
        sup = 0
        for t in ts:
            for i in range(len(t) - 1):
                if t[i].get(a):
                    sup += 1
                    if not t[i + 1].get(b):
                        ok = False
                        break
            if not ok:
                break
        if ok and sup:
            add("next_implication", f"always ({a} |=> {b})", sup,
                f"{a} was followed next cycle by {b} in all {sup} cases", 0.9)

    # 5. bounded eventual response a |-> ##[0:window] b
    for a, b in itertools.permutations(sigs, 2):
        ok = True
        sup = 0
        for t in ts:
            for i in range(len(t)):
                if t[i].get(a):
                    sup += 1
                    if not any(t[j].get(b)
                               for j in range(i, min(len(t), i + window + 1))):
                        ok = False
                        break
            if not ok:
                break
        if ok and sup:
            add("eventual_response",
                f"always ({a} |-> ##[0:{window}] {b})", sup,
                f"{b} followed {a} within {window} cycles in all {sup} cases",
                0.7)
    return rank_properties(cands)


def rank_properties(cands: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order candidates by support × specificity (informative ones first)."""
    out = []
    for c in cands or []:
        score = float(c.get("support", 0)) * float(c.get("specificity", 0.5))
        out.append({**c, "score": round(score, 4)})
    out.sort(key=lambda c: (-c["score"], c["assertion"]))
    for i, c in enumerate(out, 1):
        c["rank"] = i
    return out


def to_expr(signals: Dict[str, Var], kind: str, a: str, b: str = "") -> Expr:
    """Convert a mined candidate back into an `Expr` for formal checking."""
    va = signals.get(a, Var(a))
    vb = signals.get(b, Var(b)) if b else Const(True)
    if kind == "implication":
        return Implies(va, vb)
    if kind == "mutual_exclusion":
        return Not(And(va, vb))
    return va


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
class FormalAnalysis:
    def __init__(self, system: TransitionSystem,
                 properties: Optional[Sequence[Dict[str, Any]]] = None,
                 covers: Optional[Dict[str, Expr]] = None,
                 traces: Optional[Sequence[Sequence[Dict[str, bool]]]] = None,
                 depth: int = DEFAULT_DEPTH):
        self.system = system
        self.properties = list(properties or [])
        self.covers = covers or {}
        self.traces = list(traces or [])
        self.depth = depth

    def run(self) -> Dict[str, Any]:
        started = _now()
        results, vacuity, cois = [], [], []
        for spec in self.properties:
            expr = spec.get("expr")
            if not isinstance(expr, Expr):
                continue
            r = bmc_safety(self.system, expr, self.depth)
            r["name"] = spec.get("name", repr(expr))
            results.append(r)
            v = detect_vacuity(self.system, expr, self.depth)
            v["name"] = r["name"]
            if v.get("vacuous"):
                vacuity.append(v)
            cois.append(cone_of_influence(self.system, expr))
            if r.get("verdict") == "violated" and r.get("counterexample"):
                r["minimized"] = minimize_counterexample(
                    self.system, expr, r["counterexample"])
                r["explanation"] = explain_counterexample(
                    expr, r["counterexample"])
        cover_res = {n: cover_property(self.system, e, self.depth)
                     for n, e in self.covers.items()}
        uncovered = [n for n, c in cover_res.items() if not c["covered"]]
        mined = mine_assertions(self.traces) if self.traces else []
        pcov = proof_coverage(self.system, results)
        violated = [r for r in results if r.get("verdict") == "violated"]
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "metrics": {
                "properties": len(results),
                "violated": len(violated),
                "vacuous": len(vacuity),
                "covers": len(cover_res),
                "uncovered": len(uncovered),
                "mined_assertions": len(mined),
                "proof_coverage": pcov["proof_coverage"],
                "mean_coi_reduction": round(
                    sum(c["reduction_ratio"] for c in cois) / len(cois), 4)
                if cois else 0.0,
            },
            "results": results,
            "vacuity": vacuity,
            "coverage": {"covers": cover_res, "uncovered": uncovered,
                         "proof": pcov},
            "cone_of_influence": cois,
            "mined_assertions": mined,
            "pass": not violated and not vacuity,
        }

"""Derive verification obligations directly from RTL.

Until now every property on the board was hand-written, which is the real
ceiling on scale: a human decides what to verify, so whatever the human forgets
is invisible. This module reads the design itself (via `AGENT_H.rtl_graph`,
hardened against a 9-repo corpus) and enumerates what *must* be verified.

**The central honesty rule: naming a property is not checking it.**

A generated obligation is only *dischargeable* if a checker is bound to it. The
rest are emitted as **declared but unverifiable** — they stay on the board, they
keep contributing residual risk, and they are reported explicitly. This converts
"unknown unknowns" (outputs nobody thought about) into "known unverified", which
is precisely what a verification plan is for. The alternative — quietly
generating only the properties we can already check — would make the board look
complete by construction, the exact failure mode this platform exists to prevent.

What is derived today:

  functional  one obligation per output port — that output must be correct
  structural  combinational-loop freedom (checkable now by `StaticChannel`)

`harness_map` binds an obligation to an existing checker (an sby task). Anything
unbound is declared-only.
"""
from __future__ import annotations
import os, sys

from board import Task

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def parse_rtl(rtl_path: str, module: str | None = None):
    """Parse one module from a real SystemVerilog file. Returns (module, source)."""
    from AGENT_H import rtl_graph as rg
    with open(rtl_path) as f:
        src = f.read()
    mods = rg.parse_module(src, rtl_path)
    if not mods:
        raise ValueError(f"no module parsed from {rtl_path}")
    if module:
        for m in mods:
            if m.name == module:
                return m, src
        raise ValueError(f"module '{module}' not found in {rtl_path}")
    return mods[0], src


# Outputs that are internal plumbing rather than architectural results. They are
# still emitted (they are real outputs and really are unverified) but weighted
# lower, so criticality reflects engineering judgement instead of pretending
# every port matters equally.
_LOW_WEIGHT_HINTS = ("imd_val", "unused", "_ext_")


def generate_obligations(rtl_path: str, module: str | None = None,
                         harness_map: dict | None = None,
                         base_weight: float = 5.0):
    """Enumerate obligations for a real RTL module.

    harness_map: {obligation_suffix: sby_task} binds a checker to an obligation.
                 Unbound obligations are declared-only (no evidence path).
    Returns (tasks, summary_dict).
    """
    from AGENT_H import rtl_graph as rg
    m, src = parse_rtl(rtl_path, module)
    harness_map = harness_map or {}
    tasks, covered = [], 0

    # ---- structural: checkable right now by StaticChannel -------------------
    tasks.append(Task(f"{m.name}.struct.comb_loops", base_weight / 2,
                      kind="structural",
                      note="derived: combinational-loop freedom over the parsed netlist"))

    # ---- functional: one obligation per output port -------------------------
    outputs = [p for p in m.ports if p.direction == "output"]
    for p in outputs:
        key = f"out.{p.name}"
        bind = harness_map.get(key)
        w = base_weight * (0.4 if any(h in p.name for h in _LOW_WEIGHT_HINTS) else 1.0)

        # A binding may be a plain task name (full coverage) or a dict declaring
        # PARTIAL coverage: {"task", "scope", "uncovered"}. Partial coverage
        # emits the covered obligation AND a declared-only remainder, so a
        # checker that handles part of an output can never be mistaken for one
        # that handles all of it.
        scope = uncovered = None
        harness = bind
        if isinstance(bind, dict):
            harness = bind.get("task")
            scope, uncovered = bind.get("scope"), bind.get("uncovered")
        if harness:
            covered += 1
        note = f"derived: output {p.name} {p.width} must be correct"
        if harness and scope:
            note += f" [checker scope: {scope}]"
        elif not harness:
            note += " — NO CHECKER BOUND"
        tasks.append(Task(f"{m.name}.{key}", w, formal_task=harness, note=note))

        if uncovered:
            tasks.append(Task(f"{m.name}.{key}[{uncovered}]", w * 0.6,
                              note=f"derived remainder: {p.name} outside the "
                                   f"checker's scope ({uncovered}) — NO CHECKER BOUND"))

    summary = {
        "module": m.name,
        "source": rtl_path,
        "ports": len(m.ports),
        "outputs": len(outputs),
        "outputs_with_checker": covered,
        "outputs_unverifiable": len(outputs) - covered,
        "fsms": len(rg.extract_fsms(src, m)),
        "assertions_in_rtl": len(m.assertions),
        "parse_warnings": len(m.parse_warnings),
        "obligations": len(tasks),
    }
    return tasks, summary


def describe(summary: dict) -> str:
    s = summary
    return (f"  module {s['module']}  ({s['ports']} ports, {s['outputs']} outputs, "
            f"{s['fsms']} FSMs, {s['assertions_in_rtl']} RTL assertions)\n"
            f"  generated {s['obligations']} obligations — "
            f"{s['outputs_with_checker']}/{s['outputs']} outputs have a checker, "
            f"{s['outputs_unverifiable']} have NONE")

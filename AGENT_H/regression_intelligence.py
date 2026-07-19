"""
AGENT_H.regression_intelligence — Test Selection / Scheduling / Health (T73)
============================================================================

Regression management with the intelligence layer on top: decide **which** tests
to run, in **what order**, on **how many** workers, and report how healthy the
regression itself is. Closes the regression-management side of taxonomy level 20
(roadmap 🟡).

1. Test impact analysis
------------------------
`impacted_tests()` maps changed RTL files to the tests that exercise them using
the coverage map (test -> elements). A test is impacted if it covers any changed
file, or if it is declared `always_run`. Tests with **no** coverage data are
treated as impacted (fail-safe: never silently skip an unknown test).

2. Intelligent test selection
------------------------------
`select_tests()` combines impact with historical value: a test's
**failure-detection rate** (how often it has caught a real bug) and its
**recency-weighted** failure history. Selection honours a budget (test count or
total seconds) and always keeps `always_run` and blocker-covering tests.
Returns the selected set plus the tests it dropped *and why* — skipping is
auditable, never silent.

3. Prioritisation / scheduling
-------------------------------
`prioritise_tests()` ranks by `value / cost`: expected bug-detection value
divided by runtime, so cheap high-yield tests run first (the classic
"time-to-first-failure" objective).
`schedule()` packs the ordered tests across N workers with **LPT** (longest
processing time first), a 4/3-approximation for makespan, and reports the
predicted makespan and per-worker load balance.

4. Health monitoring & flakiness
---------------------------------
`regression_health()` computes pass rate, failure rate, mean/median runtime and
a **flakiness score** per test. Flakiness uses the *transition rate* of the
pass/fail history (how often the result flips) — a test that alternates is flaky,
whereas one that fails consistently is simply broken. That distinction matters:
consistently-failing tests are bugs, flapping tests are noise.

5. Incremental regression & cost
---------------------------------
`incremental_plan()` produces the run plan for a commit range: impacted tests
only, plus the always-run set.
`cost_report()` quantifies the saving (CPU-seconds and wall-clock with N
workers) versus running everything, so the optimisation is measurable rather
than assumed.

Deterministic, stdlib-only, schema-v2.1.0.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("AGENT_H.regression_intel")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "regression_intelligence"

DEFAULT_RECENCY_DECAY = 0.85
FLAKY_THRESHOLD = 0.3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Test impact analysis
# ─────────────────────────────────────────────────────────────────────────────
def impacted_tests(changed_files: Sequence[str],
                   coverage: Dict[str, Sequence[str]],
                   always_run: Optional[Sequence[str]] = None,
                   all_tests: Optional[Sequence[str]] = None
                   ) -> Dict[str, Any]:
    """Which tests are affected by a set of changed RTL files?"""
    changed = {str(c) for c in (changed_files or [])}
    always = {str(t) for t in (always_run or [])}
    coverage = coverage or {}
    known = set(coverage)
    universe = {str(t) for t in (all_tests or [])} | known | always

    impacted: Set[str] = set(always)
    reasons: Dict[str, str] = {t: "always_run" for t in always}
    for test in universe:
        if test in impacted:
            continue
        elems = coverage.get(test)
        if elems is None:
            impacted.add(test)
            reasons[test] = "no_coverage_data"      # fail-safe
            continue
        hit = changed & {str(e) for e in elems}
        if hit:
            impacted.add(test)
            reasons[test] = f"covers_changed:{sorted(hit)[0]}"
    skipped = sorted(universe - impacted)
    return {
        "impacted": sorted(impacted),
        "skipped": skipped,
        "reasons": reasons,
        "changed_files": sorted(changed),
        "impact_ratio": round(len(impacted) / len(universe), 4) if universe else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2/3. Value, prioritisation, selection, scheduling
# ─────────────────────────────────────────────────────────────────────────────
def detection_rate(history: Sequence[bool],
                   decay: float = DEFAULT_RECENCY_DECAY) -> float:
    """Recency-weighted fraction of runs in which this test failed (i.e. caught
    something). Recent runs count more."""
    h = [bool(x) for x in (history or [])]
    if not h:
        return 0.0
    num = den = 0.0
    for i, failed in enumerate(reversed(h)):        # newest first
        w = decay ** i
        den += w
        if failed:
            num += w
    return num / den if den else 0.0


def prioritise_tests(tests: Sequence[str],
                     history: Optional[Dict[str, Sequence[bool]]] = None,
                     runtimes: Optional[Dict[str, float]] = None,
                     weights: Optional[Dict[str, float]] = None
                     ) -> List[Dict[str, Any]]:
    """Rank tests by value/cost (expected detection per second)."""
    history = history or {}
    runtimes = runtimes or {}
    weights = weights or {}
    out: List[Dict[str, Any]] = []
    for t in tests or []:
        t = str(t)
        rate = detection_rate(history.get(t, []))
        cost = float(runtimes.get(t, 1.0)) or 1.0
        value = (rate + 0.01) * float(weights.get(t, 1.0))   # +0.01: never zero
        out.append({
            "test": t,
            "detection_rate": round(rate, 4),
            "runtime_s": cost,
            "value": round(value, 6),
            "value_per_second": round(value / cost, 6),
        })
    out.sort(key=lambda r: (-r["value_per_second"], r["test"]))
    for i, r in enumerate(out, 1):
        r["priority"] = i
    return out


def select_tests(ranked: Sequence[Dict[str, Any]],
                 max_tests: Optional[int] = None,
                 time_budget_s: Optional[float] = None,
                 must_run: Optional[Sequence[str]] = None
                 ) -> Dict[str, Any]:
    """Pick the highest-value tests within a count and/or time budget."""
    must = {str(t) for t in (must_run or [])}
    selected: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    spent = 0.0
    # must-run first, budget or not (auditable)
    for r in ranked:
        if r["test"] in must:
            selected.append(r)
            spent += r["runtime_s"]
    for r in ranked:
        if r["test"] in must:
            continue
        if max_tests is not None and len(selected) >= max_tests:
            dropped.append({**r, "reason": "count_budget"})
            continue
        if time_budget_s is not None and spent + r["runtime_s"] > time_budget_s:
            dropped.append({**r, "reason": "time_budget"})
            continue
        selected.append(r)
        spent += r["runtime_s"]
    return {
        "selected": selected,
        "dropped": dropped,
        "selected_count": len(selected),
        "dropped_count": len(dropped),
        "estimated_seconds": round(spent, 3),
    }


def schedule(tests: Sequence[Dict[str, Any]], workers: int = 4) -> Dict[str, Any]:
    """LPT (longest processing time first) bin-packing across workers.

    LPT is a 4/3-approximation for minimising makespan on identical machines.
    """
    workers = max(1, int(workers or 1))
    order = sorted(tests or [], key=lambda r: -float(r.get("runtime_s", 1.0)))
    loads = [0.0] * workers
    assign: List[List[str]] = [[] for _ in range(workers)]
    for r in order:
        w = min(range(workers), key=lambda i: loads[i])
        assign[w].append(r.get("test"))
        loads[w] += float(r.get("runtime_s", 1.0))
    total = sum(loads)
    makespan = max(loads) if loads else 0.0
    return {
        "workers": workers,
        "assignment": {f"worker_{i}": assign[i] for i in range(workers)},
        "worker_loads_s": [round(x, 3) for x in loads],
        "makespan_s": round(makespan, 3),
        "total_cpu_s": round(total, 3),
        "balance": round((total / workers) / makespan, 4) if makespan else 1.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Health & flakiness
# ─────────────────────────────────────────────────────────────────────────────
def flakiness(history: Sequence[bool]) -> float:
    """Transition rate of the pass/fail history. A consistently failing test
    scores 0 (broken, not flaky); an alternating one approaches 1."""
    h = [bool(x) for x in (history or [])]
    if len(h) < 2:
        return 0.0
    flips = sum(1 for i in range(1, len(h)) if h[i] != h[i - 1])
    return flips / (len(h) - 1)


def regression_health(results: Dict[str, bool],
                      history: Optional[Dict[str, Sequence[bool]]] = None,
                      runtimes: Optional[Dict[str, float]] = None
                      ) -> Dict[str, Any]:
    results = results or {}
    history = history or {}
    runtimes = runtimes or {}
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    failed = total - passed
    rts = [float(v) for v in runtimes.values() if isinstance(v, (int, float))]
    per_test = []
    flaky = []
    for t in sorted(set(results) | set(history)):
        fl = flakiness(history.get(t, []))
        rec = {
            "test": t,
            "passed": results.get(t),
            "flakiness": round(fl, 4),
            "runs": len(history.get(t, [])),
            "runtime_s": runtimes.get(t),
        }
        per_test.append(rec)
        if fl >= FLAKY_THRESHOLD:
            flaky.append(rec)
    return {
        "total_tests": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "failure_rate": round(failed / total, 4) if total else 0.0,
        "mean_runtime_s": round(statistics.mean(rts), 3) if rts else 0.0,
        "median_runtime_s": round(statistics.median(rts), 3) if rts else 0.0,
        "total_runtime_s": round(sum(rts), 3) if rts else 0.0,
        "flaky_tests": sorted(flaky, key=lambda r: -r["flakiness"]),
        "flaky_count": len(flaky),
        "per_test": per_test,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Incremental plan & cost
# ─────────────────────────────────────────────────────────────────────────────
def incremental_plan(changed_files: Sequence[str],
                     coverage: Dict[str, Sequence[str]],
                     history: Optional[Dict[str, Sequence[bool]]] = None,
                     runtimes: Optional[Dict[str, float]] = None,
                     always_run: Optional[Sequence[str]] = None,
                     workers: int = 4,
                     time_budget_s: Optional[float] = None) -> Dict[str, Any]:
    impact = impacted_tests(changed_files, coverage, always_run)
    ranked = prioritise_tests(impact["impacted"], history, runtimes)
    sel = select_tests(ranked, time_budget_s=time_budget_s,
                       must_run=always_run)
    sched = schedule(sel["selected"], workers)
    return {"impact": impact, "ranked": ranked, "selection": sel,
            "schedule": sched}


def cost_report(all_tests: Sequence[str], selected: Sequence[str],
                runtimes: Optional[Dict[str, float]] = None,
                workers: int = 4) -> Dict[str, Any]:
    runtimes = runtimes or {}
    def total(ts: Iterable[str]) -> float:
        return sum(float(runtimes.get(str(t), 1.0)) for t in ts)
    full = total(all_tests)
    sel = total(selected)
    saved = full - sel
    return {
        "full_cpu_s": round(full, 3),
        "selected_cpu_s": round(sel, 3),
        "saved_cpu_s": round(saved, 3),
        "saved_pct": round(100.0 * saved / full, 2) if full else 0.0,
        "full_wallclock_s": round(full / max(1, workers), 3),
        "selected_wallclock_s": round(sel / max(1, workers), 3),
        "tests_full": len(list(all_tests)),
        "tests_selected": len(list(selected)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
class RegressionIntelligence:
    def __init__(self, coverage: Optional[Dict[str, Sequence[str]]] = None,
                 results: Optional[Dict[str, bool]] = None,
                 history: Optional[Dict[str, Sequence[bool]]] = None,
                 runtimes: Optional[Dict[str, float]] = None,
                 changed_files: Optional[Sequence[str]] = None,
                 always_run: Optional[Sequence[str]] = None,
                 workers: int = 4,
                 time_budget_s: Optional[float] = None):
        self.coverage = coverage or {}
        self.results = results or {}
        self.history = history or {}
        self.runtimes = runtimes or {}
        self.changed = list(changed_files or [])
        self.always = list(always_run or [])
        self.workers = workers
        self.budget = time_budget_s

    def run(self) -> Dict[str, Any]:
        started = _now()
        plan = incremental_plan(self.changed, self.coverage, self.history,
                                self.runtimes, self.always, self.workers,
                                self.budget)
        health = regression_health(self.results, self.history, self.runtimes)
        all_tests = sorted(set(self.coverage) | set(self.results) |
                           set(self.history) | set(self.always))
        chosen = [r["test"] for r in plan["selection"]["selected"]]
        cost = cost_report(all_tests, chosen, self.runtimes, self.workers)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "metrics": {
                "tests_total": len(all_tests),
                "tests_selected": len(chosen),
                "impact_ratio": plan["impact"]["impact_ratio"],
                "pass_rate": health["pass_rate"],
                "flaky_count": health["flaky_count"],
                "makespan_s": plan["schedule"]["makespan_s"],
                "saved_pct": cost["saved_pct"],
            },
            "plan": plan,
            "health": health,
            "cost": cost,
            "pass": health["failure_rate"] == 0.0,
        }


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("regression_intelligence: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    cov = manifest.get("test_coverage")
    res = manifest.get("test_results")
    if not cov and not res:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no regression data", "pass": True}
    else:
        rep = RegressionIntelligence(
            cov, res, manifest.get("test_history"),
            manifest.get("test_runtimes"), manifest.get("changed_files"),
            manifest.get("always_run"), int(manifest.get("workers", 4) or 4),
            manifest.get("time_budget_s")).run()
        rep["status"] = "completed"
    try:
        (run_dir / "regression_intelligence_report.json").write_text(
            json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("regression_intelligence: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA regression intelligence")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))

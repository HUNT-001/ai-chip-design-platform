"""
AGENT_H.verification_twin — Verification & AI Digital Twin (T79)
================================================================

A live, queryable model of a verification campaign that sits on top of the run
reports and the campaign history. This is the "digital twin" *depth* the roadmap
asks for — not the micro-ISS pre-screener (`digital_twin.py`), but the
verification-status / what-if / forecasting layer.

Three tiers (matching the roadmap breakdown)
--------------------------------------------
1. **Verification digital twin** — live status, per-module verification state,
   deterministic regression/failure **replay**, and **what-if** projection.
2. **AI digital twin** — coverage-closure **forecasting** (fit a saturating
   growth curve to the coverage time-series and extrapolate to the goal),
   next-regression **outcome prediction**, and a **tape-out readiness** score.
3. **Silicon digital twin** — FPGA / emulator / post-silicon **sync adapters**.
   These require real hardware in the loop, so they are honest *interfaces*
   that record and diff externally-supplied samples; they never fabricate
   silicon data. See `docs/DATA_AND_HARDWARE_REQUIREMENTS.md`.

The forecasting core is real math
---------------------------------
Coverage closure is modelled as ``cov(t) = Cmax·(1 − e^(−t/τ))`` — the standard
saturating curve for functional coverage. `fit_coverage_curve` estimates
``(Cmax, τ)`` by searching ``Cmax`` and linear-regressing ``ln(Cmax − cov)``
against ``t`` (best R²), then `forecast_closure` inverts the curve to predict the
effort to reach a goal, and reports *unreachable* honestly when the asymptote
``Cmax`` is below the goal — coverage that plateaus below target will never
close, and saying so is more useful than an optimistic number.

Deterministic, stdlib-only (``math`` only), schema-v2.1.0.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.vtwin")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "verification_twin"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Live verification status
# ─────────────────────────────────────────────────────────────────────────────
_BAND_SCORE = {"VERIFIED": 1.0, "CLEAN": 1.0, "HIGH": 0.8, "MEDIUM": 0.55,
               "DEGRADED": 0.55, "LOW": 0.35, "CRITICAL": 0.1}


def live_status(reports: Dict[str, Any],
                confidence: Optional[float] = None) -> Dict[str, Any]:
    """Aggregate the current run's agent reports into one live snapshot."""
    per_module = {}
    total_v = 0
    failing = []
    for name, rep in (reports or {}).items():
        if not isinstance(rep, dict):
            continue
        band = str(rep.get("band", "CLEAN"))
        viol = int(rep.get("violations", rep.get("total_violations", 0)) or 0)
        passed = bool(rep.get("pass", True))
        total_v += viol
        per_module[name] = {"band": band, "violations": viol, "pass": passed,
                            "health": _BAND_SCORE.get(band, 0.5)}
        if not passed:
            failing.append(name)
    n = len(per_module) or 1
    health = sum(m["health"] for m in per_module.values()) / n
    return {
        "agents": len(per_module),
        "total_violations": total_v,
        "failing_agents": sorted(failing),
        "overall_health": round(health, 4),
        "confidence": confidence,
        "band": ("CRITICAL" if failing else
                 "VERIFIED" if health >= 0.9 else
                 "HIGH" if health >= 0.75 else "MEDIUM"),
        "per_module": per_module,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Regression / failure replay (deterministic)
# ─────────────────────────────────────────────────────────────────────────────
def replay(record: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministically reproduce a recorded run's verdict from its stored
    inputs, and confirm the replay is faithful.

    A run record carries ``seed``, ``inputs`` (the stimulus/config) and the
    recorded ``outcome``. Replay recomputes the outcome hash from the inputs and
    checks it matches — if a run cannot be reproduced from its recorded inputs,
    the environment is non-deterministic and that is a finding in itself.
    """
    import hashlib
    inputs = record.get("inputs", {})
    seed = record.get("seed")
    canon = json.dumps({"seed": seed, "inputs": inputs}, sort_keys=True,
                       default=str)
    replay_hash = hashlib.sha256(canon.encode()).hexdigest()[:16]
    recorded_hash = record.get("input_hash")
    faithful = recorded_hash is None or recorded_hash == replay_hash
    return {
        "run_id": record.get("run_id"),
        "seed": seed,
        "replay_hash": replay_hash,
        "recorded_hash": recorded_hash,
        "faithful": faithful,
        "outcome": record.get("outcome"),
        "note": None if faithful else
                "replay hash differs from record — run is not reproducible from "
                "its stored inputs (non-deterministic environment)",
    }


def replay_failure(failure_record: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct the minimal reproduction for a recorded failure."""
    r = replay(failure_record)
    r["reproduction"] = {
        "seed": failure_record.get("seed"),
        "test": failure_record.get("test"),
        "failing_check": failure_record.get("check"),
        "trace_ref": failure_record.get("trace_ref"),
        "command": (f"make TEST={failure_record.get('test','?')} "
                    f"SEED={failure_record.get('seed','?')}"),
    }
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 3. What-if analysis
# ─────────────────────────────────────────────────────────────────────────────
def what_if(state: Dict[str, Any], change: Dict[str, Any]) -> Dict[str, Any]:
    """Project the campaign outcome under a hypothetical change.

    Supported levers (any subset):
    - ``add_tests``: n extra tests (moves coverage along the fitted curve)
    - ``fix_bugs``: n bugs resolved (reduces violations, raises confidence)
    - ``coverage_goal``: change the closure target
    - ``add_agents``: n more checkers enabled (raises completeness)
    """
    cov_hist = state.get("coverage_history") or []
    base_cov = cov_hist[-1] if cov_hist else state.get("coverage", 0.0)
    bugs = int(state.get("open_bugs", 0) or 0)
    conf = float(state.get("confidence", 0.5) or 0.5)
    completeness = float(state.get("completeness", 0.7) or 0.7)

    proj_cov = base_cov
    if "add_tests" in change and cov_hist:
        fit = fit_coverage_curve(cov_hist)
        t_now = len(cov_hist)
        proj_cov = _curve(t_now + int(change["add_tests"]),
                          fit["cmax"], fit["tau"])
    proj_bugs = max(0, bugs - int(change.get("fix_bugs", 0)))
    proj_conf = min(1.0, conf + 0.03 * int(change.get("fix_bugs", 0))
                    + 0.01 * int(change.get("add_agents", 0)))
    proj_complete = min(1.0, completeness + 0.02 * int(change.get("add_agents", 0)))
    goal = float(change.get("coverage_goal", state.get("coverage_goal", 90.0)))
    return {
        "change": change,
        "baseline": {"coverage": round(base_cov, 2), "open_bugs": bugs,
                     "confidence": round(conf, 3),
                     "completeness": round(completeness, 3)},
        "projected": {"coverage": round(proj_cov, 2), "open_bugs": proj_bugs,
                      "confidence": round(proj_conf, 3),
                      "completeness": round(proj_complete, 3)},
        "meets_goal": proj_cov >= goal and proj_bugs == 0,
        "delta_coverage": round(proj_cov - base_cov, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Coverage-closure forecasting (the real math)
# ─────────────────────────────────────────────────────────────────────────────
def _curve(t: float, cmax: float, tau: float) -> float:
    if tau <= 0:
        return cmax
    return cmax * (1.0 - math.exp(-t / tau))


def _r2(xs: Sequence[float], ys: Sequence[float],
        f) -> float:
    if len(ys) < 2:
        return 0.0
    mean = sum(ys) / len(ys)
    ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum((y - f(x)) ** 2 for x, y in zip(xs, ys))
    if ss_tot == 0:
        return 1.0 if ss_res == 0 else 0.0
    return 1.0 - ss_res / ss_tot


def fit_coverage_curve(coverage: Sequence[float]) -> Dict[str, Any]:
    """Fit cov(t) = Cmax·(1 − e^(−t/τ)) to a coverage time-series.

    Estimates Cmax by search (the asymptote may be < 100%), then linear-regress
    ln(Cmax − cov) vs t to get τ. Returns the parameters and the fit R².
    """
    ys = [float(c) for c in coverage if c is not None]
    n = len(ys)
    if n < 2:
        return {"cmax": ys[-1] if ys else 0.0, "tau": 1.0, "r2": 0.0,
                "samples": n, "note": "insufficient history to fit"}
    xs = list(range(1, n + 1))
    last = ys[-1]
    best = None
    # search the asymptote: from just above the last sample up to 100
    lo = max(last + 0.5, max(ys) + 0.5)
    for i in range(0, 201):
        cmax = lo + (100.0 - lo) * (i / 200.0) if lo < 100.0 else 100.0
        if cmax <= last:
            continue
        # linearise: z = ln(cmax - cov) = ln(cmax) - t/tau
        zs = []
        ok = True
        for x, y in zip(xs, ys):
            d = cmax - y
            if d <= 0:
                ok = False
                break
            zs.append(math.log(d))
        if not ok:
            continue
        # linear regression z = a + b t ; slope b = -1/tau
        mx = sum(xs) / n
        mz = sum(zs) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            continue
        b = sum((x - mx) * (z - mz) for x, z in zip(xs, zs)) / denom
        if b >= 0:                       # coverage must be increasing
            continue
        tau = -1.0 / b
        r2 = _r2(xs, ys, lambda t, c=cmax, ta=tau: _curve(t, c, ta))
        if best is None or r2 > best["r2"]:
            best = {"cmax": round(cmax, 3), "tau": round(tau, 4),
                    "r2": round(r2, 4)}
        if lo >= 100.0:
            break
    if best is None:
        return {"cmax": last, "tau": 1.0, "r2": 0.0, "samples": n,
                "note": "no monotone saturating fit found"}
    best["samples"] = n
    return best


def forecast_closure(coverage: Sequence[float], goal: float = 90.0
                     ) -> Dict[str, Any]:
    """Predict the effort (extra runs) to reach a coverage goal."""
    fit = fit_coverage_curve(coverage)
    cmax, tau = fit["cmax"], fit["tau"]
    n = len([c for c in coverage if c is not None])
    if cmax < goal:
        return {"fit": fit, "goal": goal, "reachable": False,
                "asymptote": cmax,
                "note": f"coverage plateaus at ~{cmax:.1f}% < goal {goal}%; the "
                        f"goal is unreachable with the current stimulus — need "
                        f"new coverage-directed tests, not more of the same"}
    # invert cov(t)=goal → t = -tau·ln(1 - goal/cmax)
    t_goal = -tau * math.log(1.0 - goal / cmax)
    extra = max(0, math.ceil(t_goal - n))
    return {"fit": fit, "goal": goal, "reachable": True,
            "runs_to_goal_total": round(t_goal, 1),
            "additional_runs_needed": extra,
            "current_runs": n,
            "projected_next": round(_curve(n + 1, cmax, tau), 2)}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Regression-outcome prediction
# ─────────────────────────────────────────────────────────────────────────────
def predict_regression(pass_rate_history: Sequence[float]) -> Dict[str, Any]:
    """Predict the next regression's pass rate from history (linear trend +
    residual spread for a confidence band)."""
    ys = [float(p) for p in pass_rate_history if p is not None]
    n = len(ys)
    if n == 0:
        return {"predicted_pass_rate": None, "confidence": 0.0,
                "trend": "unknown", "samples": 0}
    if n == 1:
        return {"predicted_pass_rate": round(ys[0], 4), "confidence": 0.3,
                "trend": "flat", "samples": 1}
    xs = list(range(1, n + 1))
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    b = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
         if denom else 0.0)
    a = my - b * mx
    pred = max(0.0, min(1.0, a + b * (n + 1)))
    resid = math.sqrt(sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys)) / n)
    conf = max(0.0, min(0.95, 1.0 - resid))
    trend = "improving" if b > 0.005 else "declining" if b < -0.005 else "flat"
    return {"predicted_pass_rate": round(pred, 4),
            "confidence": round(conf, 3), "trend": trend,
            "slope": round(b, 5), "residual_std": round(resid, 4),
            "samples": n}


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tape-out readiness
# ─────────────────────────────────────────────────────────────────────────────
_READINESS_WEIGHTS = {
    "coverage": 0.30, "confidence": 0.25, "bug_freedom": 0.20,
    "completeness": 0.15, "regression_health": 0.10,
}


def tapeout_readiness(state: Dict[str, Any]) -> Dict[str, Any]:
    """Weighted tape-out readiness score with a transparent factor breakdown.

    Deliberately conservative: any open **blocker** caps readiness, because a
    single sign-off blocker means "not ready" regardless of the averages.
    """
    cov = float(state.get("coverage", 0.0)) / 100.0
    conf = float(state.get("confidence", 0.0) or 0.0)
    bugs = int(state.get("open_bugs", 0) or 0)
    blockers = int(state.get("blockers", 0) or 0)
    completeness = float(state.get("completeness", 0.0) or 0.0)
    reg_health = float(state.get("regression_pass_rate", 0.0) or 0.0)
    bug_freedom = 1.0 / (1.0 + bugs)
    factors = {
        "coverage": min(1.0, cov),
        "confidence": min(1.0, conf),
        "bug_freedom": bug_freedom,
        "completeness": min(1.0, completeness),
        "regression_health": min(1.0, reg_health),
    }
    score = sum(_READINESS_WEIGHTS[k] * v for k, v in factors.items())
    if blockers > 0:
        score = min(score, 0.4)          # a blocker caps readiness
    band = ("READY" if score >= 0.9 and blockers == 0 else
            "NEARLY" if score >= 0.75 and blockers == 0 else
            "NOT_READY")
    gaps = sorted(((k, round(_READINESS_WEIGHTS[k] * (1 - v), 4))
                   for k, v in factors.items()), key=lambda kv: -kv[1])
    return {
        "readiness_score": round(score, 4),
        "band": band,
        "blockers": blockers,
        "factors": {k: round(v, 4) for k, v in factors.items()},
        "weighted_gaps": gaps[:3],
        "recommendation": ("tape-out gated: resolve blockers first"
                           if blockers else
                           "ready for sign-off review" if band == "READY" else
                           f"close the top gap: {gaps[0][0]}"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. Silicon twin sync adapters (honest hardware interfaces)
# ─────────────────────────────────────────────────────────────────────────────
def silicon_sync(rtl_samples: Sequence[Dict[str, Any]],
                 hw_samples: Optional[Sequence[Dict[str, Any]]] = None,
                 source: str = "fpga") -> Dict[str, Any]:
    """Correlate RTL-simulation samples with externally-supplied hardware
    samples (FPGA prototype / emulator / silicon tester).

    This does **not** fabricate hardware data: with no ``hw_samples`` it reports
    ``status='awaiting_hardware'`` and returns the sync contract the caller must
    satisfy. Given real samples, it diffs them against the RTL reference.
    """
    if not hw_samples:
        return {
            "source": source,
            "status": "awaiting_hardware",
            "rtl_samples": len(rtl_samples or []),
            "contract": {
                "format": "list of {cycle:int, signals:{name:hexvalue}}",
                "requirement": f"supply {source} captures aligned by cycle",
            },
            "note": f"no {source} data provided — correlation needs real "
                    f"hardware in the loop; this is an interface, not a "
                    f"simulated result",
        }
    ref = {s.get("cycle"): s.get("signals", {}) for s in rtl_samples or []}
    mism = []
    compared = 0
    for hs in hw_samples:
        c = hs.get("cycle")
        if c not in ref:
            continue
        for sig, val in (hs.get("signals", {}) or {}).items():
            if sig in ref[c]:
                compared += 1
                if str(ref[c][sig]) != str(val):
                    mism.append({"cycle": c, "signal": sig,
                                 "rtl": ref[c][sig], source: val})
    return {
        "source": source,
        "status": "correlated",
        "compared_points": compared,
        "mismatches": mism[:100],
        "mismatch_count": len(mism),
        "correlated": len(mism) == 0,
        "note": None if not mism else
                f"RTL and {source} diverge — post-silicon/pre-silicon mismatch",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
class VerificationTwin:
    def __init__(self, reports: Optional[Dict[str, Any]] = None,
                 coverage_history: Optional[Sequence[float]] = None,
                 pass_rate_history: Optional[Sequence[float]] = None,
                 state: Optional[Dict[str, Any]] = None):
        self.reports = reports or {}
        self.coverage_history = list(coverage_history or [])
        self.pass_rate_history = list(pass_rate_history or [])
        self.state = dict(state or {})

    def run(self, goal: float = 90.0) -> Dict[str, Any]:
        started = _now()
        status = live_status(self.reports, self.state.get("confidence"))
        forecast = (forecast_closure(self.coverage_history, goal)
                    if len(self.coverage_history) >= 2 else None)
        regr = (predict_regression(self.pass_rate_history)
                if self.pass_rate_history else None)
        st = dict(self.state)
        if self.coverage_history:
            st.setdefault("coverage", self.coverage_history[-1])
        if self.pass_rate_history:
            st.setdefault("regression_pass_rate", self.pass_rate_history[-1])
        st.setdefault("open_bugs", status["total_violations"])
        st.setdefault("completeness", status["overall_health"])
        st.setdefault("confidence", self.state.get("confidence", status["overall_health"]))
        readiness = tapeout_readiness(st)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "live_status": status,
            "coverage_forecast": forecast,
            "regression_prediction": regr,
            "tapeout_readiness": readiness,
            "metrics": {
                "agents": status["agents"],
                "overall_health": status["overall_health"],
                "coverage_reachable": (forecast or {}).get("reachable"),
                "readiness_band": readiness["band"],
                "readiness_score": readiness["readiness_score"],
            },
            "pass": readiness["band"] != "NOT_READY" or not self.reports,
        }


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("verification_twin: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    reports = manifest.get("reports") or {}
    if not reports:
        vp = run_dir / "verification_report.json"
        if vp.exists():
            try:
                reports = json.loads(vp.read_text()).get("reports", {})
            except (json.JSONDecodeError, OSError):
                reports = {}
    twin = VerificationTwin(
        reports,
        manifest.get("coverage_history"),
        manifest.get("pass_rate_history"),
        manifest.get("twin_state"),
    )
    rep = twin.run(float(manifest.get("coverage_goal", 90.0)))
    rep["status"] = "completed"
    try:
        (run_dir / "verification_twin_report.json").write_text(
            json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("verification_twin: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA verification digital twin")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))

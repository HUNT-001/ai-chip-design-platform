"""
AGENT_H.bug_intelligence — Bug Localization / Prediction / Classification (T72)
===============================================================================

Predictive bug analytics on top of the failure stream and the historical bug
ledger. Closes the **bug-prediction** half of taxonomy level 13 (roadmap 🟡).

1. Bug localization — spectrum-based (Ochiai)
----------------------------------------------
`localize()` implements **Ochiai suspiciousness**, the standard
spectrum-based fault-localization metric:

        suspiciousness(e) = failed(e) / sqrt(totalFailed × (failed(e) + passed(e)))

where `failed(e)` / `passed(e)` are the number of failing / passing tests that
executed element `e` (an RTL file or module). Ochiai is the well-established
choice in the SBFL literature and dominates Tarantula empirically; both are
provided (`tarantula()` for comparison). Elements are ranked, and the ranking is
returned with the raw spectra so a human can audit it.

2. Severity prediction
-----------------------
`predict_severity()` scores a bug from interpretable features — the checker's
own severity, whether the affected area is security/coherence/memory-model
critical, blast radius (tests hit), and whether it blocks a regression — and
maps the score to **CRITICAL / MAJOR / MINOR** with the contributing features
returned for transparency.

3. Lifetime prediction
-----------------------
`predict_lifetime()` estimates days-to-resolution from the **historical median**
resolution time of comparable bugs (same root-cause class, then same module,
then global), rather than inventing a number. Returns the estimate, the sample
size it was drawn from, and a confidence that degrades with small samples.

4. Reopen prediction
---------------------
`predict_reopen()` estimates P(reopen) using a **Laplace-smoothed** historical
reopen rate for the class/module, adjusted for whether the fix touched a file
with a high historical churn-defect correlation.

5. Duplicate bug detection
---------------------------
`find_duplicates()` matches a new bug against an existing corpus using the
canonical signature (exact) then token Jaccard (fuzzy), reusing
`failure_analytics` so dedup is consistent platform-wide.

6. Root-cause classification
-----------------------------
`classify_root_cause()` labels a failure as **rtl_bug / testbench_bug /
constraint_issue / environment_issue / simulator_issue / tool_issue** using a
weighted keyword-evidence model with per-class evidence returned. Unmatched
input yields `unknown` with confidence 0 rather than a false label.

Deterministic, stdlib-only, schema-v2.1.0.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:                                                # package or standalone
    from .failure_analytics import canonical_signature, _tokens, jaccard
except ImportError:                                 # pragma: no cover
    from failure_analytics import canonical_signature, _tokens, jaccard  # type: ignore

log = logging.getLogger("AGENT_H.bug_intel")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "bug_intelligence"

DUP_THRESHOLD = 0.7
_CRITICAL_AREAS = ("security", "privilege", "coherence", "memory_model",
                   "isolation", "mmu", "pmp", "crypto")

# Root-cause evidence: class -> {keyword: weight}
_ROOT_CAUSE_EVIDENCE: Dict[str, Dict[str, float]] = {
    "rtl_bug": {"mismatch": 2.0, "golden": 2.0, "rtl": 1.5, "dut": 1.5,
                "incorrect result": 2.0, "wrong value": 2.0, "commit log": 1.0,
                "register": 1.0, "signal": 1.0},
    "testbench_bug": {"testbench": 3.0, "tb ": 2.0, "driver": 1.5,
                      "scoreboard": 2.0, "monitor": 1.5, "stimulus": 1.5,
                      "harness": 2.0, "assertion in tb": 2.0},
    "constraint_issue": {"constraint": 3.0, "randomization": 2.0,
                         "randomize": 2.0, "solver failed": 2.5,
                         "inconsistent constraint": 3.0, "distribution": 1.0},
    "environment_issue": {"config": 1.5, "environment": 2.5, "path": 1.5,
                          "missing file": 2.5, "permission": 2.0,
                          "not found": 1.5, "no such file": 2.5,
                          "env var": 2.0, "setup": 1.0},
    "simulator_issue": {"simulator": 3.0, "verilator": 2.5, "vcs": 2.0,
                        "questa": 2.0, "xcelium": 2.0, "segmentation fault": 2.0,
                        "internal error": 2.0, "core dumped": 2.0},
    "tool_issue": {"synthesis": 2.0, "yosys": 2.5, "compiler": 2.0,
                   "license": 3.0, "toolchain": 2.5, "elaboration": 2.0,
                   "parse error": 2.0},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Spectrum-based localization
# ─────────────────────────────────────────────────────────────────────────────
def _spectra(coverage: Dict[str, Sequence[str]],
             results: Dict[str, bool]) -> Dict[str, Dict[str, int]]:
    """coverage: test -> elements executed. results: test -> passed(bool)."""
    spec: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"failed": 0, "passed": 0})
    for test, elements in (coverage or {}).items():
        passed = bool(results.get(test, True))
        for el in elements or ():
            key = "passed" if passed else "failed"
            spec[str(el)][key] += 1
    return spec


def ochiai(failed_e: int, passed_e: int, total_failed: int) -> float:
    denom = math.sqrt(total_failed * (failed_e + passed_e))
    return (failed_e / denom) if denom else 0.0


def tarantula(failed_e: int, passed_e: int,
              total_failed: int, total_passed: int) -> float:
    if total_failed == 0 or (failed_e + passed_e) == 0:
        return 0.0
    fr = failed_e / total_failed
    pr = (passed_e / total_passed) if total_passed else 0.0
    return fr / (fr + pr) if (fr + pr) else 0.0


def localize(coverage: Dict[str, Sequence[str]],
             results: Dict[str, bool],
             metric: str = "ochiai") -> List[Dict[str, Any]]:
    """Rank RTL files/modules by suspiciousness (spectrum-based FL)."""
    spec = _spectra(coverage, results)
    total_failed = sum(1 for t, p in (results or {}).items() if not p)
    total_passed = sum(1 for t, p in (results or {}).items() if p)
    out: List[Dict[str, Any]] = []
    for el, s in spec.items():
        if metric == "tarantula":
            score = tarantula(s["failed"], s["passed"], total_failed, total_passed)
        else:
            score = ochiai(s["failed"], s["passed"], total_failed)
        out.append({
            "element": el,
            "suspiciousness": round(score, 6),
            "failed_tests": s["failed"],
            "passed_tests": s["passed"],
            "metric": metric,
        })
    out.sort(key=lambda r: (-r["suspiciousness"], r["element"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 2. Severity prediction
# ─────────────────────────────────────────────────────────────────────────────
def predict_severity(bug: Dict[str, Any]) -> Dict[str, Any]:
    features: Dict[str, float] = {}
    reported = str(bug.get("severity", "")).upper()
    features["reported_severity"] = {"HIGH": 2.0, "CRITICAL": 3.0,
                                     "MEDIUM": 1.0, "LOW": 0.0}.get(reported, 1.0)
    blob = " ".join(str(bug.get(k, "")) for k in
                    ("check", "module", "agent", "message", "detail")).lower()
    features["critical_area"] = 2.0 if any(a in blob for a in _CRITICAL_AREAS) else 0.0
    tests = bug.get("tests") or []
    n = len(tests) if isinstance(tests, (list, tuple, set)) else int(tests or 0)
    features["blast_radius"] = min(2.0, math.log2(n + 1) / 2.0)
    features["regression_blocker"] = 1.5 if bug.get("regression_blocker") else 0.0
    features["silent_corruption"] = 1.5 if any(
        w in blob for w in ("corrupt", "undetected", "silent", "fabricat")) else 0.0
    score = sum(features.values())
    label = "CRITICAL" if score >= 5.0 else ("MAJOR" if score >= 2.5 else "MINOR")
    return {"severity": label, "score": round(score, 4), "features": features}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Lifetime prediction
# ─────────────────────────────────────────────────────────────────────────────
def predict_lifetime(bug: Dict[str, Any],
                     history: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Median days-to-resolution of comparable historical bugs."""
    hist = [h for h in (history or [])
            if isinstance(h, dict) and h.get("resolution_days") is not None]

    def med(sample: List[Dict[str, Any]]) -> Optional[float]:
        vals = [float(h["resolution_days"]) for h in sample]
        return statistics.median(vals) if vals else None

    cls = bug.get("root_cause")
    mod = bug.get("module")
    tiers: List[Tuple[str, List[Dict[str, Any]]]] = [
        ("root_cause", [h for h in hist if cls and h.get("root_cause") == cls]),
        ("module", [h for h in hist if mod and h.get("module") == mod]),
        ("global", hist),
    ]
    for basis, sample in tiers:
        m = med(sample)
        if m is not None and sample:
            conf = min(0.95, 0.3 + 0.1 * len(sample))
            return {"estimated_days": round(m, 2), "basis": basis,
                    "sample_size": len(sample), "confidence": round(conf, 3)}
    return {"estimated_days": None, "basis": "none", "sample_size": 0,
            "confidence": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# 4. Reopen prediction
# ─────────────────────────────────────────────────────────────────────────────
def predict_reopen(bug: Dict[str, Any],
                   history: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Laplace-smoothed historical reopen rate for the class/module."""
    hist = [h for h in (history or []) if isinstance(h, dict)]
    cls, mod = bug.get("root_cause"), bug.get("module")
    subset = [h for h in hist if (cls and h.get("root_cause") == cls)
              or (mod and h.get("module") == mod)]
    basis = "class_or_module"
    if not subset:
        subset, basis = hist, "global"
    reopened = sum(1 for h in subset if h.get("reopened"))
    # Laplace smoothing keeps small samples from producing 0.0 / 1.0
    p = (reopened + 1) / (len(subset) + 2) if subset else 0.5
    churn = float(bug.get("file_churn", 0) or 0)
    p_adj = min(0.99, p * (1.0 + min(0.5, churn / 100.0)))
    return {
        "reopen_probability": round(p_adj, 4),
        "basis": basis,
        "sample_size": len(subset),
        "historical_reopens": reopened,
        "risk": "high" if p_adj >= 0.5 else ("medium" if p_adj >= 0.25 else "low"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Duplicate bug detection
# ─────────────────────────────────────────────────────────────────────────────
def find_duplicates(bug: Dict[str, Any], corpus: Sequence[Dict[str, Any]],
                    threshold: float = DUP_THRESHOLD) -> List[Dict[str, Any]]:
    sig = canonical_signature(bug.get("message", bug.get("detail", "")))
    toks = _tokens(bug.get("message", bug.get("detail", "")))
    hits: List[Dict[str, Any]] = []
    for i, other in enumerate(corpus or []):
        if not isinstance(other, dict):
            continue
        osig = canonical_signature(other.get("message", other.get("detail", "")))
        if osig and osig == sig:
            hits.append({"index": i, "id": other.get("id"), "similarity": 1.0,
                         "match": "exact_signature"})
            continue
        sim = jaccard(toks, _tokens(other.get("message", other.get("detail", ""))))
        if sim >= threshold:
            hits.append({"index": i, "id": other.get("id"),
                         "similarity": round(sim, 4), "match": "fuzzy"})
    hits.sort(key=lambda h: -h["similarity"])
    return hits


# ─────────────────────────────────────────────────────────────────────────────
# 6. Root-cause classification
# ─────────────────────────────────────────────────────────────────────────────
def classify_root_cause(failure: Dict[str, Any]) -> Dict[str, Any]:
    blob = " ".join(str(failure.get(k, "")) for k in
                    ("message", "detail", "check", "module", "agent",
                     "log", "stage")).lower()
    scores: Dict[str, float] = {}
    evidence: Dict[str, List[str]] = defaultdict(list)
    for cls, kws in _ROOT_CAUSE_EVIDENCE.items():
        s = 0.0
        for kw, w in kws.items():
            if kw in blob:
                s += w
                evidence[cls].append(kw)
        if s:
            scores[cls] = s
    if not scores:
        return {"root_cause": "unknown", "confidence": 0.0, "scores": {},
                "evidence": {}}
    total = sum(scores.values())
    best = max(scores, key=lambda k: scores[k])
    return {
        "root_cause": best,
        "confidence": round(scores[best] / total, 4),
        "scores": {k: round(v, 3) for k, v in sorted(
            scores.items(), key=lambda kv: -kv[1])},
        "evidence": {k: sorted(v) for k, v in evidence.items()},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
class BugIntelligence:
    def __init__(self, failures: Sequence[Dict[str, Any]],
                 coverage: Optional[Dict[str, Sequence[str]]] = None,
                 results: Optional[Dict[str, bool]] = None,
                 history: Optional[Sequence[Dict[str, Any]]] = None):
        self.failures = [f for f in (failures or []) if isinstance(f, dict)]
        self.coverage = coverage or {}
        self.results = results or {}
        self.history = list(history or [])

    def run(self) -> Dict[str, Any]:
        started = _now()
        ranking = localize(self.coverage, self.results) if self.coverage else []
        enriched: List[Dict[str, Any]] = []
        for f in self.failures:
            rc = classify_root_cause(f)
            bug = {**f, "root_cause": rc["root_cause"]}
            enriched.append({
                "fingerprint": f.get("fingerprint"),
                "check": f.get("check"),
                "module": f.get("module", f.get("agent")),
                "root_cause": rc,
                "severity": predict_severity(bug),
                "lifetime": predict_lifetime(bug, self.history),
                "reopen": predict_reopen(bug, self.history),
                "duplicates": find_duplicates(f, self.history),
            })
        crit = sum(1 for e in enriched if e["severity"]["severity"] == "CRITICAL")
        rc_counts: Dict[str, int] = defaultdict(int)
        for e in enriched:
            rc_counts[e["root_cause"]["root_cause"]] += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "metrics": {
                "bugs_analysed": len(enriched),
                "critical_bugs": crit,
                "root_cause_breakdown": dict(rc_counts),
                "localized_elements": len(ranking),
                "top_suspect": ranking[0]["element"] if ranking else None,
            },
            "localization": ranking[:50],
            "bugs": enriched,
            "pass": crit == 0,
        }


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("bug_intelligence: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    name = (manifest.get("outputs", {}) or {}).get("failures", "failures.jsonl")
    p = run_dir / name
    failures: List[Dict[str, Any]] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    failures.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if not failures:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no failures", "pass": True}
    else:
        rep = BugIntelligence(failures,
                              manifest.get("test_coverage"),
                              manifest.get("test_results"),
                              manifest.get("bug_history")).run()
        rep["status"] = "completed"
    try:
        (run_dir / "bug_intelligence_report.json").write_text(
            json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("bug_intelligence: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA bug intelligence")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))

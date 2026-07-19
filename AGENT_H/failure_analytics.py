"""
AGENT_H.failure_analytics — Failure Clustering / Dedup / Triage (T71)
======================================================================

Large-scale regression intelligence: turn a pile of raw failures into a small
set of actionable, ranked, de-duplicated problems.

This closes the **failure-clustering** half of taxonomy level 13, which the
roadmap had at 🟡 ("ML clustering of failures ⬜").

1. Canonical signatures & fingerprinting
----------------------------------------
`canonical_signature()` normalises a failure message into a stable form:
hex addresses, decimal literals, cycle/seq numbers, timestamps, paths and UUIDs
are replaced with typed placeholders (`<HEX>`, `<NUM>`, `<PATH>`, …). Two
failures that differ only in run-specific values collapse to the same string.
`fingerprint()` hashes the canonical signature plus the failing check and module
into a short stable id — the primary dedup key.

2. Clustering (four signals)
-----------------------------
- **signature** — exact canonical-signature match (cheap, high precision).
- **log similarity** — token-set **Jaccard** similarity over the normalised
  message, agglomerated with a threshold (default 0.6).
- **stack-trace similarity** — top-N frame overlap, weighted so that agreement
  in the *innermost* frames counts more.
- **waveform similarity** — normalised cosine similarity over a numeric signal
  vector (e.g. sampled waveform digest), for failures with no useful text.
`cluster_failures()` runs single-link agglomeration over the chosen metric and
returns clusters with a representative, a size and a **root-cause group** —
failures grouped by (module, check) once similarity has merged them.

3. Prioritisation
-----------------
`prioritise()` scores each cluster:
`score = severity_weight × impact × recency × blocker_bonus`, where impact is the
number of distinct tests hit (blast radius), recency favours regressions that
appeared in the most recent run, and a **regression blocker** (a cluster whose
first occurrence is the current run *and* which hits a critical check) gets an
explicit flag. Also reports **first occurrence** per cluster.

4. Trend analysis
-----------------
`classify_trends()` labels each fingerprint across an ordered run history:
- **new** — first seen in the latest run.
- **persistent** — failed in every run since it appeared.
- **intermittent** — alternates pass/fail (flaky); reports a flip rate.
- **aging** — open for ≥ `aging_runs` runs without resolution.
- **recurring** — reappeared after at least one clean run (a regression of a fix).
- **resolved** — seen historically, absent from the latest run.

All algorithms are deterministic and stdlib-only (no numpy/sklearn), so results
are reproducible across machines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("AGENT_H.failure_analytics")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "failure_analytics"

DEFAULT_JACCARD = 0.6
DEFAULT_STACK_SIM = 0.5
DEFAULT_WAVE_SIM = 0.9
DEFAULT_AGING_RUNS = 5

_CRITICAL_HINTS = ("security", "privilege", "coherence", "memory_model",
                   "isolation", "corruption", "deadlock", "data_loss")

# Normalisation patterns, applied in order.
_NORMALISERS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<TIME>"),
    (re.compile(r"(?:[A-Za-z]:)?[\\/][\w.\-\\/]+\.(?:sv|v|py|log|jsonl)"), "<PATH>"),
    (re.compile(r"\b0[xX][0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b(?:cycle|seq|time|iter|run)\s*[:=#]?\s*\d+\b",
                re.IGNORECASE), "<POS>"),
    (re.compile(r"\b\d+\.\d+\b"), "<FLOAT>"),
    # indexed identifiers: x5/x9, t0, core3 -> x<N>, t<N>, core<N>. Only matches
    # when the digits END the token, so instruction mnemonics such as
    # `sha256sig0` or `aes64ks1i` are left intact.
    (re.compile(r"\b([A-Za-z_]+)\d+\b"), r"\1<N>"),
    (re.compile(r"\b\d+\b"), "<NUM>"),
    (re.compile(r"\s+"), " "),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Signatures & fingerprints
# ─────────────────────────────────────────────────────────────────────────────
def canonical_signature(message: Any) -> str:
    """Normalise a failure message to a run-independent canonical form."""
    s = str(message or "").strip()
    for pat, repl in _NORMALISERS:
        s = pat.sub(repl, s)
    return s.strip().lower()


def fingerprint(failure: Dict[str, Any]) -> str:
    """Stable short id for a failure: canonical message + check + module."""
    sig = canonical_signature(failure.get("message", failure.get("detail", "")))
    key = "|".join([
        str(failure.get("check", "")).lower(),
        str(failure.get("module", failure.get("agent", ""))).lower(),
        sig,
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _tokens(text: str) -> Set[str]:
    return {t for t in re.split(r"[^a-z0-9_<>]+", canonical_signature(text)) if t}


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def stack_similarity(a: Sequence[str], b: Sequence[str], top: int = 10) -> float:
    """Weighted top-frame overlap: innermost frames dominate (weight 1/(i+1))."""
    fa, fb = list(a)[:top], list(b)[:top]
    if not fa or not fb:
        return 0.0
    total = 0.0
    matched = 0.0
    for i in range(max(len(fa), len(fb))):
        w = 1.0 / (i + 1)
        total += w
        if i < len(fa) and i < len(fb) and fa[i] == fb[i]:
            matched += w
    return matched / total if total else 0.0


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    na = math.sqrt(sum(float(x) ** 2 for x in a[:n]))
    nb = math.sqrt(sum(float(x) ** 2 for x in b[:n]))
    if na == 0 or nb == 0:
        return 1.0 if na == nb else 0.0
    return dot / (na * nb)


# ─────────────────────────────────────────────────────────────────────────────
# Clustering
# ─────────────────────────────────────────────────────────────────────────────
def _similarity(f1: Dict[str, Any], f2: Dict[str, Any], method: str) -> float:
    if method == "signature":
        return 1.0 if canonical_signature(f1.get("message", "")) == \
            canonical_signature(f2.get("message", "")) else 0.0
    if method == "stack":
        return stack_similarity(f1.get("stack") or [], f2.get("stack") or [])
    if method == "waveform":
        return cosine(f1.get("waveform") or [], f2.get("waveform") or [])
    return jaccard(_tokens(f1.get("message", "")), _tokens(f2.get("message", "")))


def _threshold_for(method: str, override: Optional[float]) -> float:
    if override is not None:
        return override
    return {"signature": 1.0, "stack": DEFAULT_STACK_SIM,
            "waveform": DEFAULT_WAVE_SIM}.get(method, DEFAULT_JACCARD)


def cluster_failures(failures: Sequence[Dict[str, Any]],
                     method: str = "log",
                     threshold: Optional[float] = None
                     ) -> List[Dict[str, Any]]:
    """Single-link agglomerative clustering over the chosen similarity metric.

    Returns clusters sorted by size (descending), each with a representative,
    member indices, the shared fingerprint set and a root-cause group key.
    """
    items = [f for f in (failures or []) if isinstance(f, dict)]
    thr = _threshold_for(method, threshold)
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(n):
        for j in range(i + 1, n):
            if _similarity(items[i], items[j], method) >= thr:
                union(i, j)

    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    clusters: List[Dict[str, Any]] = []
    for root, members in groups.items():
        rep = items[members[0]]
        mods = {str(items[m].get("module", items[m].get("agent", "?")))
                for m in members}
        checks = {str(items[m].get("check", "?")) for m in members}
        tests = {str(items[m].get("test", "")) for m in members} - {""}
        clusters.append({
            "cluster_id": fingerprint(rep),
            "method": method,
            "size": len(members),
            "members": sorted(members),
            "representative": rep.get("message", rep.get("detail", "")),
            "fingerprints": sorted({fingerprint(items[m]) for m in members}),
            "modules": sorted(mods),
            "checks": sorted(checks),
            "tests": sorted(tests),
            "root_cause_group": f"{sorted(mods)[0]}::{sorted(checks)[0]}",
        })
    clusters.sort(key=lambda c: (-c["size"], c["cluster_id"]))
    return clusters


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────
def deduplicate(failures: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge equivalent failures by fingerprint. Returns unique failures with
    occurrence counts, plus the duplicate map."""
    unique: Dict[str, Dict[str, Any]] = {}
    dupes: Dict[str, List[int]] = defaultdict(list)
    for idx, f in enumerate(failures or []):
        if not isinstance(f, dict):
            continue
        fp = fingerprint(f)
        dupes[fp].append(idx)
        if fp not in unique:
            unique[fp] = {
                "fingerprint": fp,
                "check": f.get("check"),
                "module": f.get("module", f.get("agent")),
                "signature": canonical_signature(
                    f.get("message", f.get("detail", ""))),
                "message": f.get("message", f.get("detail", "")),
                "count": 0,
                "tests": set(),
            }
        unique[fp]["count"] += 1
        t = f.get("test")
        if t:
            unique[fp]["tests"].add(str(t))
    out = []
    for fp, rec in unique.items():
        rec = dict(rec)
        rec["tests"] = sorted(rec["tests"])
        rec["duplicate_indices"] = dupes[fp]
        out.append(rec)
    out.sort(key=lambda r: (-r["count"], r["fingerprint"]))
    total = sum(len(v) for v in dupes.values())
    return {
        "unique": out,
        "unique_count": len(out),
        "total_failures": total,
        "duplicates_removed": total - len(out),
        "dedup_ratio": round(1 - (len(out) / total), 4) if total else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Prioritisation
# ─────────────────────────────────────────────────────────────────────────────
_SEV_WEIGHT = {"CRITICAL": 4.0, "HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0}


def _is_critical(cluster: Dict[str, Any]) -> bool:
    blob = " ".join(cluster.get("checks", []) + cluster.get("modules", [])).lower()
    return any(h in blob for h in _CRITICAL_HINTS)


def prioritise(clusters: Sequence[Dict[str, Any]],
               history: Optional[Dict[str, List[bool]]] = None,
               severities: Optional[Dict[str, str]] = None
               ) -> List[Dict[str, Any]]:
    """Rank clusters by actionable priority. ``history`` maps fingerprint ->
    ordered per-run failure booleans (oldest first)."""
    history = history or {}
    severities = severities or {}
    out: List[Dict[str, Any]] = []
    for c in clusters or []:
        fps = c.get("fingerprints") or [c.get("cluster_id")]
        sev = max((severities.get(f, "HIGH") for f in fps),
                  key=lambda s: _SEV_WEIGHT.get(str(s).upper(), 1.0))
        sev_w = _SEV_WEIGHT.get(str(sev).upper(), 1.0)
        impact = max(1, len(c.get("tests", []) or []) or c.get("size", 1))
        # first occurrence = earliest run index in which any fingerprint failed
        first = None
        latest_new = False
        for f in fps:
            h = history.get(f) or []
            for i, failed in enumerate(h):
                if failed:
                    first = i if first is None else min(first, i)
                    break
            if h and h[-1] and not any(h[:-1]):
                latest_new = True
        critical = _is_critical(c)
        blocker = bool(latest_new and critical)
        recency = 1.5 if latest_new else 1.0
        score = sev_w * math.log2(impact + 1) * recency * (1.5 if blocker else 1.0)
        out.append({
            **c,
            "severity": str(sev).upper(),
            "impact_tests": impact,
            "first_occurrence_run": first,
            "is_critical": critical,
            "regression_blocker": blocker,
            "priority_score": round(score, 4),
        })
    out.sort(key=lambda r: (-r["priority_score"], r["cluster_id"]))
    for rank, r in enumerate(out, 1):
        r["rank"] = rank
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Trend analysis
# ─────────────────────────────────────────────────────────────────────────────
def classify_trends(history: Dict[str, List[bool]],
                    aging_runs: int = DEFAULT_AGING_RUNS) -> Dict[str, Any]:
    """Classify each fingerprint's behaviour across an ordered run history
    (oldest first, True = failed in that run)."""
    result: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, int] = defaultdict(int)
    for fp, runs in (history or {}).items():
        h = [bool(x) for x in (runs or [])]
        if not h:
            continue
        first_fail = next((i for i, v in enumerate(h) if v), None)
        flips = sum(1 for i in range(1, len(h)) if h[i] != h[i - 1])
        fail_count = sum(h)
        if first_fail is None:
            label = "resolved"
        elif not h[-1]:
            label = "resolved"
        elif fail_count == 1 and h[-1]:
            label = "new"
        elif all(h[first_fail:]):
            open_runs = len(h) - first_fail
            label = "aging" if open_runs >= aging_runs else "persistent"
        elif flips >= 3:
            label = "intermittent"
        else:
            # failed, cleared, failed again
            label = "recurring" if any(not x for x in h[first_fail:-1]) \
                else "persistent"
        counts[label] += 1
        result[fp] = {
            "label": label,
            "runs": len(h),
            "failures": fail_count,
            "first_failure_run": first_fail,
            "open_runs": (len(h) - first_fail) if first_fail is not None else 0,
            "flip_rate": round(flips / max(1, len(h) - 1), 4),
        }
    return {"per_fingerprint": result, "summary": dict(counts)}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
class FailureAnalytics:
    def __init__(self, failures: Sequence[Dict[str, Any]],
                 history: Optional[Dict[str, List[bool]]] = None,
                 method: str = "log",
                 threshold: Optional[float] = None):
        self.failures = [f for f in (failures or []) if isinstance(f, dict)]
        self.history = history or {}
        self.method = method
        self.threshold = threshold

    def run(self) -> Dict[str, Any]:
        started = _now()
        dedup = deduplicate(self.failures)
        clusters = cluster_failures(self.failures, self.method, self.threshold)
        sev = {fingerprint(f): f.get("severity", "HIGH") for f in self.failures}
        ranked = prioritise(clusters, self.history, sev)
        trends = classify_trends(self.history)
        blockers = [c for c in ranked if c["regression_blocker"]]
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "total_failures": len(self.failures),
            "metrics": {
                "clusters": len(clusters),
                "unique_failures": dedup["unique_count"],
                "duplicates_removed": dedup["duplicates_removed"],
                "dedup_ratio": dedup["dedup_ratio"],
                "regression_blockers": len(blockers),
                "trend_summary": trends["summary"],
                "method": self.method,
            },
            "deduplication": dedup,
            "clusters": ranked,
            "trends": trends,
            "blockers": blockers,
            "pass": len(blockers) == 0,
        }


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("failure_analytics: cannot read manifest: %s", exc)
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
    history = manifest.get("failure_history") or {}
    if not failures:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no failures", "pass": True}
    else:
        rep = FailureAnalytics(failures, history).run()
        rep["status"] = "completed"
    try:
        (run_dir / "failure_analytics_report.json").write_text(
            json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("failure_analytics: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA failure analytics")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))

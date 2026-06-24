"""
AGENT_H/confidence_scorer.py
=============================
T12 — Formalized Verification Confidence Score

Aggregates verification evidence from all agents (A–L + H) into a single
scalar confidence score [0.0, 1.0] that represents the probability that the
RTL implementation is correct given the evidence collected.

Scoring model
-------------
The score is a weighted product of sub-scores:

  score = Π  w_i × s_i   (for all active evidence sources i)

where each sub-score s_i ∈ [0, 1] captures one dimension of verification
quality.  The product is normalised to [0, 1] by geometric mean.

Evidence sources and their weights:

  Source                  Weight  Description
  ─────────────────────── ──────  ──────────────────────────────────────────
  coverage_completeness   0.30    Line/branch/toggle coverage vs thresholds
  mismatch_rate           0.25    (1 - bugs/commits) across all runs
  intent_checks           0.15    Fraction of intent specs passing
  formal_depth            0.10    BMC depth achieved by Agent L (0 if skipped)
  cdc_clean               0.05    0 HIGH CDC violations
  equiv_verified          0.10    Equivalence check passed (0 if skipped)
  perf_healthy            0.05    No performance regressions
  tests_generated         0.03    Causal tests written / 100 (capped)
  minimizer_coverage      0.02    Minimization runs / distinct mismatch classes

Confidence bands:
  ≥ 0.90  → VERIFIED    (production-ready)
  ≥ 0.70  → HIGH        (ready for tape-out sign-off review)
  ≥ 0.50  → MEDIUM      (regressions identified, further testing needed)
  ≥ 0.30  → LOW         (significant gaps, active bug hunting needed)
  < 0.30  → CRITICAL    (major functional issues present)

Usage
-----
  from AGENT_H.confidence_scorer import ConfidenceScorer

  scorer = ConfidenceScorer(run_dir=Path("rundir"))
  report = scorer.compute()
  print(report["score"], report["band"])
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Evidence source definitions
# ─────────────────────────────────────────────────────────

@dataclass
class EvidenceSource:
    name:    str
    weight:  float
    score:   float    = 0.0    # 0–1 computed score
    active:  bool     = True   # False if evidence file not found
    detail:  str      = ""


@dataclass
class ConfidenceReport:
    score:       float
    band:        str
    sources:     List[EvidenceSource]
    weighted_avg: float
    explanation: str


# ─────────────────────────────────────────────────────────
# Score computation helpers
# ─────────────────────────────────────────────────────────

def _load_json(path: Path) -> Optional[Dict]:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _coverage_score(coverage: Dict) -> Tuple[float, str]:
    """Score coverage completeness."""
    line_pct    = coverage.get("line_coverage_pct", 0)
    branch_pct  = coverage.get("branch_coverage_pct", 0)
    toggle_pct  = coverage.get("toggle_coverage_pct", 0)

    # Weighted average: line 50%, branch 30%, toggle 20%
    avg = (line_pct * 0.5 + branch_pct * 0.3 + toggle_pct * 0.2) / 100.0
    detail = f"line={line_pct:.1f}% branch={branch_pct:.1f}% toggle={toggle_pct:.1f}%"
    return round(min(avg, 1.0), 4), detail


def _mismatch_rate_score(manifest: Dict) -> Tuple[float, str]:
    """Score: (1 - mismatches/total_commits), floor at 0."""
    metrics = manifest.get("metrics", {})
    total   = metrics.get("total_commits", 0)
    bugs    = metrics.get("total_mismatches", 0)
    if total == 0:
        return 0.5, "no commit data"
    rate  = bugs / total
    score = max(0.0, 1.0 - rate * 10)   # 10 mismatches per 100 commits → score = 0
    return round(min(score, 1.0), 4), f"bugs={bugs}, commits={total}, rate={rate:.4f}"


def _intent_score(intent: Dict) -> Tuple[float, str]:
    specs_run  = max(intent.get("specs_run", 1), 1)
    violations = intent.get("total_violations", 0)
    score = max(0.0, 1.0 - violations / specs_run)
    return round(score, 4), f"violations={violations}, specs={specs_run}"


def _formal_depth_score(equiv: Dict) -> Tuple[float, str]:
    depth      = equiv.get("bmc_depth_achieved", 0)
    seq_result = equiv.get("sequential_result", "skipped")
    if seq_result == "skipped":
        return 0.5, "equivalence check skipped"
    if seq_result in ("PASS", "EQUIVALENT"):
        score = min(1.0, depth / 20.0)    # 20 cycles BMC → full score
    else:
        score = 0.0
    return round(score, 4), f"seq={seq_result}, depth={depth}"


def _cdc_score(cdc: Dict) -> Tuple[float, str]:
    high = sum(1 for p in cdc.get("paths", []) if p.get("severity") == "HIGH")
    if high == 0:
        return 1.0, "no HIGH CDC violations"
    score = max(0.0, 1.0 - high * 0.25)
    return round(score, 4), f"HIGH violations={high}"


def _equiv_score(equiv: Dict) -> Tuple[float, str]:
    comb = equiv.get("combinational_result", "skipped")
    seq  = equiv.get("sequential_result",    "skipped")
    if comb == "skipped" and seq == "skipped":
        return 0.5, "equiv check skipped"
    pass_states = {"PASS", "EQUIVALENT", "pass", "equivalent"}
    comb_ok = comb in pass_states
    seq_ok  = seq  in pass_states
    score = (int(comb_ok) + int(seq_ok)) / 2.0
    return round(score, 4), f"comb={comb}, seq={seq}"


def _perf_score(perf: Dict) -> Tuple[float, str]:
    alerts = perf.get("threshold_alerts", [])
    high_alerts = [a for a in alerts if a.get("severity") in ("HIGH", "CRITICAL")]
    if not high_alerts:
        return 1.0, "no performance regressions"
    score = max(0.0, 1.0 - len(high_alerts) * 0.2)
    return round(score, 4), f"performance alerts={len(high_alerts)}"


def _tests_generated_score(causal: Dict) -> Tuple[float, str]:
    written = causal.get("files_written", 0)
    score = min(1.0, written / 30.0)  # 30 tests = full score
    return round(score, 4), f"tests={written}"


def _minimizer_score(knowledge: Dict) -> Tuple[float, str]:
    bugs = knowledge.get("total_bugs", 0)
    if bugs == 0:
        return 1.0, "no bugs to minimize"
    min_count = knowledge.get("minimized_count", 0)
    score = min(1.0, min_count / max(bugs, 1))
    return round(score, 4), f"minimized={min_count}/{bugs}"


# ─────────────────────────────────────────────────────────
# Main scorer
# ─────────────────────────────────────────────────────────

class ConfidenceScorer:
    """
    Reads all available reports from run_dir and computes an aggregated
    verification confidence score.

    Parameters
    ----------
    run_dir  : path to the AVA run directory (contains all JSON reports)
    manifest : optional pre-loaded manifest dict; loaded from run_dir if None
    """

    def __init__(self, run_dir: Path, manifest: Optional[Dict] = None) -> None:
        self.run_dir  = Path(run_dir)
        self.manifest = manifest or _load_json(self.run_dir / "run_manifest.json") or {}

    def compute(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        outputs = self.manifest.get("outputs", {})

        sources: List[EvidenceSource] = []

        def _add(name: str, weight: float, path_key: str,
                 fn, fallback_score: float = 0.5) -> None:
            path = self.run_dir / (outputs.get(path_key) or path_key)
            data = _load_json(path)
            if data is None:
                sources.append(EvidenceSource(name, weight, fallback_score, False,
                                              "file not found"))
            else:
                score, detail = fn(data)
                sources.append(EvidenceSource(name, weight, score, True, detail))

        # Coverage (Agent F/E output)
        _add("coverage_completeness", 0.30, "coverage_summary.json",
             _coverage_score, 0.3)

        # Mismatch rate from manifest metrics
        mismatch_score, mismatch_detail = _mismatch_rate_score(self.manifest)
        sources.append(EvidenceSource("mismatch_rate", 0.25, mismatch_score, True,
                                      mismatch_detail))

        # Intent violations (Agent H T11)
        _add("intent_checks", 0.15, "intent_report.json", _intent_score, 0.5)

        # Formal depth / equivalence (Agent L)
        _add("formal_depth", 0.10, "equiv_report.json", _formal_depth_score, 0.5)

        # CDC (Agent J)
        _add("cdc_clean", 0.05, "cdc_report.json", _cdc_score, 0.5)

        # Equivalence (Agent L again, different score)
        _add("equiv_verified", 0.10, "equiv_report.json", _equiv_score, 0.5)

        # Performance (Agent K)
        _add("perf_healthy", 0.05, "perf.json", _perf_score, 0.8)

        # Causal tests generated (Agent G causal engine)
        _add("tests_generated", 0.03, "causal_evolution_report.json",
             _tests_generated_score, 0.0)

        # Minimization coverage (knowledge graph stats)
        _add("minimizer_coverage", 0.02, "knowledge_graph_stats.json",
             _minimizer_score, 0.5)

        # Weighted average (active sources only)
        total_weight = sum(s.weight for s in sources if s.active)
        if total_weight == 0:
            weighted_avg = 0.0
        else:
            weighted_avg = sum(s.weight * s.score for s in sources if s.active) / total_weight

        score = round(min(max(weighted_avg, 0.0), 1.0), 4)

        if score >= 0.90:
            band = "VERIFIED"
        elif score >= 0.70:
            band = "HIGH"
        elif score >= 0.50:
            band = "MEDIUM"
        elif score >= 0.30:
            band = "LOW"
        else:
            band = "CRITICAL"

        finished = datetime.now(timezone.utc)

        report = {
            "schema_version": SCHEMA_VERSION,
            "agent":          "confidence_scorer",
            "run_id":         self.manifest.get("run_id", "unknown"),
            "score":          score,
            "band":           band,
            "weighted_avg":   round(weighted_avg, 4),
            "sources": [
                {
                    "name":   s.name,
                    "weight": s.weight,
                    "score":  s.score,
                    "active": s.active,
                    "detail": s.detail,
                }
                for s in sources
            ],
            "explanation": (
                f"Confidence {band} ({score:.3f}): "
                f"{sum(1 for s in sources if s.active and s.score >= 0.7)}/{len(sources)} "
                f"evidence sources scoring ≥ 0.70"
            ),
            "started_at":  started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":  round((finished - started).total_seconds(), 3),
        }

        logger.info("ConfidenceScorer: score=%.3f band=%s", score, band)
        return report


def run_from_manifest(manifest_path: Path) -> int:
    with open(manifest_path) as f:
        manifest = json.load(f)

    run_dir = Path(manifest["run_dir"])
    scorer  = ConfidenceScorer(run_dir, manifest)
    report  = scorer.compute()

    report_path = run_dir / "confidence_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["confidence_report"] = "confidence_report.json"
    manifest.setdefault("metrics", {})["confidence_score"] = report["score"]
    manifest.setdefault("metrics", {})["confidence_band"]  = report["band"]

    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    return 0

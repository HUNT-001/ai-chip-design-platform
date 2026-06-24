"""
AGENT_H/economics_engine.py
============================
T21 — Verification Economics Engine

Tracks the return-on-investment of each verification activity:
  - bugs found per compute hour
  - coverage gained per test campaign
  - redundant test detection (dedup ratio)
  - agent efficiency scores

Produces a ``verification_ledger.json`` that accumulates across campaigns
and feeds the confidence scorer (T12) and the dashboard (T5).

Metrics tracked
---------------
  bugs_per_hour          : mismatches found / wall-clock hours spent
  coverage_gain_pct      : delta coverage gained by this campaign
  tests_run              : total ELF binaries simulated
  redundant_tests_pct    : fraction of tests that hit no new coverage
  agent_efficiency       : per-agent time breakdown (seconds)
  cost_per_bug           : estimated CPU-hours per bug found
  campaign_roi           : composite ROI score [0, 1]

Usage
-----
  from AGENT_H.economics_engine import EconomicsEngine

  engine = EconomicsEngine(run_dir=Path("rundir"))
  ledger = engine.compute()
  engine.save_ledger(ledger)
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


@dataclass
class AgentTiming:
    agent:       str
    duration_s:  float
    bugs_found:  int  = 0
    tests_run:   int  = 0


@dataclass
class CampaignLedger:
    run_id:              str
    started_at:          str
    duration_s:          float
    bugs_found:          int
    tests_run:           int
    coverage_before_pct: float
    coverage_after_pct:  float
    agent_timings:       List[AgentTiming] = field(default_factory=list)


class EconomicsEngine:
    """
    Computes verification economics metrics from AVA run artifacts.

    Parameters
    ----------
    run_dir  : path to the AVA run directory
    manifest : optional pre-loaded manifest dict
    """

    def __init__(self, run_dir: Path, manifest: Optional[Dict] = None) -> None:
        self.run_dir  = Path(run_dir)
        self.manifest = manifest or self._load_json(self.run_dir / "run_manifest.json") or {}

    @staticmethod
    def _load_json(path: Path) -> Optional[Dict]:
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def _duration_from_report(self, filename: str) -> float:
        data = self._load_json(self.run_dir / filename)
        if data:
            return float(data.get("duration_s") or 0)
        return 0.0

    def _bugs_from_manifest(self) -> int:
        metrics = self.manifest.get("metrics") or {}
        return int(metrics.get("total_mismatches") or 0)

    def _tests_from_manifest(self) -> int:
        metrics = self.manifest.get("metrics") or {}
        return int(metrics.get("tests_run") or metrics.get("total_commits") or 0)

    def _coverage_from_report(self, key: str, field_key: str) -> float:
        outputs = self.manifest.get("outputs") or {}
        filename = outputs.get("coverage_summary.json") or "coverage_summary.json"
        data = self._load_json(self.run_dir / filename)
        if data:
            return float(data.get(field_key) or 0)
        return 0.0

    def compute(self) -> Dict[str, Any]:
        started_at = self.manifest.get("started_at", "")
        finished_at = self.manifest.get("finished_at", "")

        # Duration calculation
        try:
            from datetime import datetime as _dt
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            s = _dt.strptime(started_at, fmt) if started_at else None
            e = _dt.strptime(finished_at, fmt) if finished_at else None
            duration_s = (e - s).total_seconds() if s and e else 0.0
        except Exception:
            duration_s = 0.0

        bugs_found = self._bugs_from_manifest()
        tests_run  = self._tests_from_manifest()

        # Agent timing breakdown from individual reports
        agent_report_files = {
            "agent_i": "litmus_report.json",
            "agent_j": "cdc_report.json",
            "agent_k": "perf.json",
            "agent_l": "equiv_report.json",
            "agent_h_intent": "intent_report.json",
            "confidence_scorer": "confidence_report.json",
            "temporal_checker": "temporal_report.json",
            "security_intel": "security_report.json",
            "causal_engine": "causal_evolution_report.json",
            "formal_fuzzer": "formal_fuzz_report.json",
            "contract_dsl": "contract_report.json",
        }
        agent_timings: List[Dict] = []
        total_agent_time = 0.0
        for agent_name, filename in agent_report_files.items():
            dur = self._duration_from_report(filename)
            total_agent_time += dur
            if dur > 0:
                agent_timings.append({"agent": agent_name, "duration_s": round(dur, 3)})

        # Economics metrics
        hours = duration_s / 3600.0 if duration_s > 0 else 1e-6
        bugs_per_hour = round(bugs_found / hours, 4)
        cost_per_bug  = round(hours / bugs_found, 4) if bugs_found > 0 else None

        # Coverage delta
        coverage_data = self._load_json(self.run_dir / "coverage_summary.json") or {}
        cov_pct       = float(coverage_data.get("line_coverage_pct") or 0)

        # Dedup ratio from digital twin (if available)
        twin_data = self._load_json(self.run_dir / "digital_twin_report.json") or {}
        dedup_ratio = float(twin_data.get("redundant_fraction") or 0)

        # ROI score: composite of bugs_per_hour (normalised), coverage, non-redundancy
        # Normalise bugs_per_hour against a baseline of 1 bug/hour
        bugs_score   = min(1.0, bugs_per_hour / 1.0)
        cov_score    = cov_pct / 100.0
        dedup_score  = 1.0 - dedup_ratio
        roi_score    = round((bugs_score * 0.5 + cov_score * 0.3 + dedup_score * 0.2), 4)

        report = {
            "schema_version":    SCHEMA_VERSION,
            "agent":             "economics_engine",
            "run_id":            self.manifest.get("run_id", "unknown"),
            "duration_s":        round(duration_s, 3),
            "duration_hours":    round(hours, 4),
            "bugs_found":        bugs_found,
            "tests_run":         tests_run,
            "bugs_per_hour":     bugs_per_hour,
            "cost_per_bug_hours": cost_per_bug,
            "coverage_pct":      round(cov_pct, 2),
            "redundant_tests_pct": round(dedup_ratio * 100, 2),
            "agent_timings":     agent_timings,
            "total_agent_time_s": round(total_agent_time, 3),
            "roi_score":         roi_score,
            "roi_band": (
                "EXCELLENT" if roi_score >= 0.8 else
                "GOOD"      if roi_score >= 0.6 else
                "FAIR"      if roi_score >= 0.4 else
                "POOR"
            ),
            "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return report

    def save_ledger(self, report: Dict[str, Any]) -> Path:
        """Append this campaign to the persistent ledger file."""
        ledger_path = self.run_dir.parent / "verification_ledger.json"
        campaigns: List[Dict] = []
        if ledger_path.exists():
            try:
                with open(ledger_path) as f:
                    data = json.load(f)
                    campaigns = data.get("campaigns", [])
            except Exception:
                pass
        campaigns.append(report)
        with open(ledger_path, "w") as f:
            json.dump({
                "schema_version": SCHEMA_VERSION,
                "campaigns": campaigns,
                "total_bugs_found":  sum(c.get("bugs_found", 0) for c in campaigns),
                "total_tests_run":   sum(c.get("tests_run", 0) for c in campaigns),
                "last_updated":      report["computed_at"],
            }, f, indent=2)
            f.write("\n")
        logger.info("EconomicsEngine: ledger updated at %s", ledger_path)
        return ledger_path


def run_from_manifest(manifest_path: Path) -> int:
    with open(manifest_path) as f:
        manifest = json.load(f)

    run_dir = Path(manifest["run_dir"])
    engine  = EconomicsEngine(run_dir, manifest)
    report  = engine.compute()

    report_path = run_dir / "economics_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    engine.save_ledger(report)

    manifest.setdefault("outputs", {})["economics_report"] = "economics_report.json"
    manifest.setdefault("metrics", {})["roi_score"] = report["roi_score"]
    manifest.setdefault("metrics", {})["roi_band"]  = report["roi_band"]

    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)
    return 0

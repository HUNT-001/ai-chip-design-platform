"""
AGENT_K/agent_k_perf.py
========================
Agent K — Microarchitectural Performance Collector

Aggregates per-instruction performance counter snapshots from the RTL commit
log (`perf_counters` field, schema v2.1.0) and produces a structured
per-run performance report (`perf.json`) with:

  - CPI / IPC summary
  - Stall cycle breakdown (load-use, multiply, divide, other)
  - Branch prediction statistics (taken rate, misprediction rate)
  - Cache miss rates (I-cache, D-cache)
  - KPI threshold enforcement with machine-readable alerts
  - Optional regression comparison against a baseline run

Output: `perf.json` in the run directory.
Manifest: updates `phases.perf_collect`, `outputs.perf_report`.

Schema: AGENT_A/commitlog.schema.json v2.1.0
        AGENT_A/run_manifest.schema.json v2.1.0
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
AGENT_ID       = "agent_k"

# ─────────────────────────────────────────────────────────
# Default KPI thresholds
# ─────────────────────────────────────────────────────────

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "cpi_max":                 10.0,   # alert if CPI > 10
    "branch_mispred_pct_max":  20.0,   # alert if misprediction rate > 20%
    "dcache_miss_rate_pct_max": 30.0,  # alert if D-cache miss rate > 30%
    "icache_miss_rate_pct_max": 10.0,  # alert if I-cache miss rate > 10%
    "load_use_stall_pct_max":  40.0,   # alert if load-use stalls > 40% of all cycles
}


# ─────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────

@dataclass
class PerfSnapshot:
    """Raw perf_counters snapshot from one commit-log record."""
    seq:             int
    cycles:          Optional[int] = None
    instret:         Optional[int] = None
    icache_miss:     Optional[int] = None
    dcache_miss:     Optional[int] = None
    branch_taken:    Optional[int] = None
    branch_mispred:  Optional[int] = None
    stall_cycles:    Optional[int] = None
    load_use_stalls: Optional[int] = None
    mul_stalls:      Optional[int] = None
    div_stalls:      Optional[int] = None
    # Extension counters (anything beyond the known set)
    extra:           Dict[str, int] = field(default_factory=dict)


_KNOWN_COUNTERS = {
    "cycles", "instret", "icache_miss", "dcache_miss",
    "branch_taken", "branch_mispred", "stall_cycles",
    "load_use_stalls", "mul_stalls", "div_stalls",
}


def _parse_snapshot(seq: int, perf_dict: dict) -> PerfSnapshot:
    snap = PerfSnapshot(seq=seq)
    for k, v in perf_dict.items():
        if k in _KNOWN_COUNTERS:
            setattr(snap, k, int(v))
        else:
            snap.extra[k] = int(v)
    return snap


@dataclass
class ThresholdAlert:
    """A KPI threshold violation."""
    metric:     str
    value:      float
    threshold:  float
    severity:   str       # CRITICAL / WARNING
    message:    str


@dataclass
class RegressionAlert:
    """A performance regression vs a baseline run."""
    metric:      str
    current:     float
    baseline:    float
    delta_pct:   float    # positive = worse
    threshold_pct: float  # regression threshold (e.g. 5%)
    message:     str


# ─────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────

class PerfCollector:
    """
    Reads an RTL commit-log JSONL file and extracts the perf_counters timeseries.

    The counters are cumulative (like hardware performance counters), so the
    collector computes per-interval deltas between consecutive snapshots.
    The final snapshot minus the first snapshot gives the totals.
    """

    def __init__(self, rtl_log_path: Path):
        self.rtl_log_path = rtl_log_path
        self._snapshots: List[PerfSnapshot] = []

    def load(self) -> int:
        """
        Parse the RTL commit log and extract perf_counters snapshots.

        Returns the number of snapshots found.
        """
        self._snapshots.clear()
        with open(self.rtl_log_path) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("JSON error at line %d; skipping", lineno)
                    continue
                perf = rec.get("perf_counters")
                if perf is None:
                    continue
                snap = _parse_snapshot(rec["seq"], perf)
                self._snapshots.append(snap)

        logger.info("Loaded %d perf snapshots from %s",
                    len(self._snapshots), self.rtl_log_path)
        return len(self._snapshots)

    def _delta(self, attr: str) -> Optional[int]:
        """
        Compute the delta of a counter between first and last snapshot.
        Returns None if neither snapshot has the counter.
        """
        vals = [getattr(s, attr) for s in self._snapshots
                if getattr(s, attr) is not None]
        if len(vals) < 2:
            return vals[0] if vals else None
        return vals[-1] - vals[0]

    def compute_summary(self) -> Dict[str, Any]:
        """Compute aggregate performance metrics from the loaded snapshots."""
        if not self._snapshots:
            return {
                "note": "No perf_counters data in RTL commit log. "
                        "Enable perf_counters in the RTL harness (Agent B).",
                "available": False,
            }

        total_cycles  = self._delta("cycles")
        total_instret = self._delta("instret")

        cpi: Optional[float] = None
        ipc: Optional[float] = None
        if total_cycles and total_instret and total_instret > 0:
            cpi = round(total_cycles / total_instret, 4)
            ipc = round(total_instret / total_cycles, 4)

        stall_cycles    = self._delta("stall_cycles")
        load_use_stalls = self._delta("load_use_stalls")
        mul_stalls      = self._delta("mul_stalls")
        div_stalls      = self._delta("div_stalls")

        def _pct(num, denom) -> Optional[float]:
            if num is None or denom is None or denom == 0:
                return None
            return round(100.0 * num / denom, 2)

        other_stalls: Optional[int] = None
        if stall_cycles is not None:
            accounted = sum(
                x for x in [load_use_stalls, mul_stalls, div_stalls]
                if x is not None
            )
            other_stalls = max(0, stall_cycles - accounted)

        branch_taken   = self._delta("branch_taken")
        branch_mispred = self._delta("branch_mispred")
        icache_miss    = self._delta("icache_miss")
        dcache_miss    = self._delta("dcache_miss")

        # Extension counters (aggregate last - first)
        extra_deltas: Dict[str, Optional[int]] = {}
        if self._snapshots:
            first_extra = self._snapshots[0].extra
            last_extra  = self._snapshots[-1].extra
            for k in set(first_extra) | set(last_extra):
                if k in first_extra and k in last_extra:
                    extra_deltas[k] = last_extra[k] - first_extra[k]
                elif k in last_extra:
                    extra_deltas[k] = last_extra[k]

        return {
            "available":    True,
            "snapshots":    len(self._snapshots),
            "total_cycles": total_cycles,
            "total_instret": total_instret,
            "cpi":          cpi,
            "ipc":          ipc,
            "stall_breakdown": {
                "total_stall_cycles":   stall_cycles,
                "load_use_stalls":      load_use_stalls,
                "mul_stalls":           mul_stalls,
                "div_stalls":           div_stalls,
                "other_stalls":         other_stalls,
                "load_use_stall_pct":   _pct(load_use_stalls, stall_cycles),
                "mul_stall_pct":        _pct(mul_stalls, stall_cycles),
                "div_stall_pct":        _pct(div_stalls, stall_cycles),
                "other_stall_pct":      _pct(other_stalls, stall_cycles),
                "stall_pct_of_cycles":  _pct(stall_cycles, total_cycles),
            },
            "branch_stats": {
                "branch_taken":      branch_taken,
                "branch_mispred":    branch_mispred,
                "branch_taken_pct":  _pct(branch_taken, total_instret),
                "mispred_pct":       _pct(branch_mispred, branch_taken),
            },
            "cache_stats": {
                "icache_miss":          icache_miss,
                "dcache_miss":          dcache_miss,
                "icache_miss_rate_pct": _pct(icache_miss, total_instret),
                "dcache_miss_rate_pct": _pct(dcache_miss, total_instret),
            },
            "extra_counters": extra_deltas,
        }


# ─────────────────────────────────────────────────────────
# Threshold enforcement
# ─────────────────────────────────────────────────────────

def check_thresholds(
    summary:    Dict[str, Any],
    thresholds: Dict[str, float],
) -> List[ThresholdAlert]:
    """
    Compare summary metrics against configured thresholds.
    Returns a list of alerts (empty = all within limits).
    """
    alerts: List[ThresholdAlert] = []

    def _alert(metric: str, value: Optional[float], threshold: float) -> None:
        if value is None:
            return
        if value > threshold:
            sev = "CRITICAL" if value > threshold * 1.5 else "WARNING"
            alerts.append(ThresholdAlert(
                metric=metric,
                value=round(value, 4),
                threshold=threshold,
                severity=sev,
                message=f"{metric} = {value:.4f} exceeds threshold {threshold:.4f}",
            ))

    _alert("cpi", summary.get("cpi"), thresholds.get("cpi_max", 10.0))

    bp = summary.get("branch_stats", {})
    _alert("branch_mispred_pct", bp.get("mispred_pct"),
           thresholds.get("branch_mispred_pct_max", 20.0))

    cs = summary.get("cache_stats", {})
    _alert("dcache_miss_rate_pct", cs.get("dcache_miss_rate_pct"),
           thresholds.get("dcache_miss_rate_pct_max", 30.0))
    _alert("icache_miss_rate_pct", cs.get("icache_miss_rate_pct"),
           thresholds.get("icache_miss_rate_pct_max", 10.0))

    sb = summary.get("stall_breakdown", {})
    _alert("load_use_stall_pct", sb.get("load_use_stall_pct"),
           thresholds.get("load_use_stall_pct_max", 40.0))

    return alerts


# ─────────────────────────────────────────────────────────
# Regression comparison
# ─────────────────────────────────────────────────────────

def compare_baseline(
    current: Dict[str, Any],
    baseline: Dict[str, Any],
    regression_pct: float = 5.0,
) -> List[RegressionAlert]:
    """
    Compare current run summary against a baseline run summary.
    Flag metrics that regressed by more than regression_pct percent.
    """
    alerts: List[RegressionAlert] = []

    METRICS = [
        ("cpi",             True),   # higher is worse
        ("ipc",             False),  # lower is worse
        ("branch_stats.mispred_pct",    True),
        ("cache_stats.dcache_miss_rate_pct", True),
        ("cache_stats.icache_miss_rate_pct", True),
        ("stall_breakdown.stall_pct_of_cycles", True),
    ]

    def _get(d: dict, key: str) -> Optional[float]:
        parts = key.split(".")
        for p in parts:
            if not isinstance(d, dict):
                return None
            d = d.get(p)
        return d

    for metric, higher_is_worse in METRICS:
        cur_val  = _get(current,  metric)
        base_val = _get(baseline, metric)
        if cur_val is None or base_val is None or base_val == 0:
            continue
        delta_pct = 100.0 * (cur_val - base_val) / abs(base_val)
        # Regression: higher_is_worse and delta > 0, or not higher_is_worse and delta < 0
        is_regression = (higher_is_worse and delta_pct > regression_pct) or \
                        (not higher_is_worse and delta_pct < -regression_pct)
        if is_regression:
            alerts.append(RegressionAlert(
                metric=metric,
                current=round(cur_val, 4),
                baseline=round(base_val, 4),
                delta_pct=round(delta_pct, 2),
                threshold_pct=regression_pct,
                message=(
                    f"{metric}: {cur_val:.4f} vs baseline {base_val:.4f} "
                    f"(delta {delta_pct:+.2f}%, threshold +-{regression_pct:.1f}%)"
                ),
            ))
    return alerts


# ─────────────────────────────────────────────────────────
# Manifest integration
# ─────────────────────────────────────────────────────────

def _load_manifest(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _save_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(path)


def run_from_manifest(
    manifest_path:  Path,
    thresholds:     Optional[Dict[str, float]] = None,
    baseline_report: Optional[Path] = None,
    regression_pct: float = 5.0,
) -> int:
    """
    Full Agent K pipeline driven by manifest.json.

    Returns: 0 = pass, 1 = threshold/regression alert, 2 = infra error.
    """
    manifest  = _load_manifest(manifest_path)
    run_dir   = Path(manifest["run_dir"])
    run_id    = manifest["run_id"]

    agent_cfg       = (manifest.get("agent_config") or {}).get("agent_k") or {}
    cpi_threshold   = agent_cfg.get("cpi_threshold")
    compare_run_id  = agent_cfg.get("regression_compare")

    if thresholds is None:
        thresholds = dict(DEFAULT_THRESHOLDS)
    if cpi_threshold:
        thresholds["cpi_max"] = cpi_threshold

    rtl_path = run_dir / (manifest["outputs"].get("rtl_commitlog") or "rtl_commit.jsonl")
    if not rtl_path.exists():
        logger.error("RTL commit log not found: %s", rtl_path)
        return 2

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest.setdefault("phases", {})["perf_collect"] = {
        "status":      "running_perf",
        "started_at":  started_at,
        "finished_at": None,
        "duration_s":  None,
        "exit_code":   None,
        "error_msg":   None,
        "retry_count": 0,
        "log_path":    "logs/agent_k.log",
    }
    manifest["status"] = "running_perf"
    _save_manifest(manifest_path, manifest)

    t0 = time.monotonic()
    try:
        collector = PerfCollector(rtl_path)
        collector.load()
        summary = collector.compute_summary()

        # Threshold check
        threshold_alerts = check_thresholds(summary, thresholds) \
            if summary.get("available") else []

        # Regression comparison
        regression_alerts: List[RegressionAlert] = []
        if baseline_report and baseline_report.exists():
            with open(baseline_report) as f:
                baseline_data = json.load(f)
            base_summary = baseline_data.get("summary", {})
            regression_alerts = compare_baseline(summary, base_summary, regression_pct)

        exit_code  = 1 if (threshold_alerts or regression_alerts) else 0
        duration_s = round(time.monotonic() - t0, 3)
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        report: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "agent":          AGENT_ID,
            "run_id":         run_id,
            "summary":        summary,
            "thresholds":     thresholds,
            "threshold_alerts": [
                {
                    "metric":    a.metric,
                    "value":     a.value,
                    "threshold": a.threshold,
                    "severity":  a.severity,
                    "message":   a.message,
                }
                for a in threshold_alerts
            ],
            "regression_alerts": [
                {
                    "metric":       a.metric,
                    "current":      a.current,
                    "baseline":     a.baseline,
                    "delta_pct":    a.delta_pct,
                    "threshold_pct": a.threshold_pct,
                    "message":      a.message,
                }
                for a in regression_alerts
            ],
            "overall_result": "failed" if exit_code else "passed",
            "started_at":    started_at,
            "finished_at":   finished_at,
            "duration_s":    duration_s,
            "generated_at":  finished_at,
        }

        # Write report atomically
        report_path = run_dir / "perf.json"
        tmp_path    = run_dir / "perf.json.tmp"
        with open(tmp_path, "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        tmp_path.rename(report_path)

        # Update manifest
        manifest["phases"]["perf_collect"].update({
            "status":      "failed" if exit_code else "passed",
            "finished_at": finished_at,
            "duration_s":  duration_s,
            "exit_code":   exit_code,
            "error_msg":   (
                f"PERF_REGRESSION: {len(threshold_alerts)} threshold alert(s), "
                f"{len(regression_alerts)} regression alert(s)"
                if exit_code else None
            ),
        })
        manifest["outputs"]["perf_report"] = "perf.json"

        # Update top-level metrics
        if summary.get("available"):
            manifest.setdefault("metrics", {}).update({
                k: v for k, v in {
                    "cpi":    summary.get("cpi"),
                    "ipc":    summary.get("ipc"),
                }.items() if v is not None
            })

        if exit_code:
            manifest["status"] = "failed"
            manifest["error"]  = {
                "code":        "PERF_REGRESSION",
                "message":     f"Agent K: {len(threshold_alerts)} threshold + {len(regression_alerts)} regression alert(s)",
                "phase":       "perf_collect",
                "recoverable": False,
                "repro_cmd":   f"python3 AGENT_K/agent_k_perf.py --manifest {manifest_path}",
            }
        else:
            manifest["status"] = "passed"

        _save_manifest(manifest_path, manifest)
        logger.info("Perf report written to %s", report_path)
        return exit_code

    except Exception as exc:
        duration_s  = round(time.monotonic() - t0, 3)
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.exception("Agent K infrastructure error: %s", exc)
        manifest["phases"]["perf_collect"].update({
            "status":      "infra_error",
            "finished_at": finished_at,
            "duration_s":  duration_s,
            "exit_code":   2,
            "error_msg":   str(exc)[:2048],
        })
        manifest["status"] = "infra_error"
        _save_manifest(manifest_path, manifest)
        return 2


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Agent K — Microarch performance collector for AVA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Full pipeline from manifest
  python3 AGENT_K/agent_k_perf.py --manifest /tmp/runs/run1/manifest.json

  # Standalone: parse RTL commit log directly
  python3 AGENT_K/agent_k_perf.py \\
      --rtl rtl_commit.jsonl \\
      --report perf.json

  # With baseline comparison and custom CPI threshold
  python3 AGENT_K/agent_k_perf.py --manifest manifest.json \\
      --baseline /tmp/runs/baseline_run/perf.json \\
      --cpi-max 6.0 --regression-pct 10.0
""",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest", type=Path, metavar="PATH")
    mode.add_argument("--rtl", type=Path, metavar="PATH",
                      help="RTL commit log JSONL (standalone mode)")
    p.add_argument("--report", type=Path, default=Path("perf.json"),
                   help="Output report path (standalone; default: perf.json)")
    p.add_argument("--baseline", type=Path, metavar="PATH",
                   help="Baseline perf.json for regression comparison")
    p.add_argument("--cpi-max", type=float, default=None,
                   help=f"CPI threshold (default: {DEFAULT_THRESHOLDS['cpi_max']})")
    p.add_argument("--regression-pct", type=float, default=5.0,
                   help="Regression threshold in percent (default: 5.0)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG","INFO","WARNING","ERROR"])
    return p


def main(argv: Optional[List[str]] = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    thresholds = dict(DEFAULT_THRESHOLDS)
    if args.cpi_max is not None:
        thresholds["cpi_max"] = args.cpi_max

    if args.manifest:
        return run_from_manifest(
            args.manifest,
            thresholds=thresholds,
            baseline_report=args.baseline,
            regression_pct=args.regression_pct,
        )

    # Standalone mode
    collector = PerfCollector(args.rtl)
    collector.load()
    summary = collector.compute_summary()

    threshold_alerts = check_thresholds(summary, thresholds) \
        if summary.get("available") else []

    regression_alerts: List[RegressionAlert] = []
    if args.baseline and args.baseline.exists():
        with open(args.baseline) as f:
            baseline_data = json.load(f)
        regression_alerts = compare_baseline(
            summary, baseline_data.get("summary", {}), args.regression_pct
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "schema_version":    SCHEMA_VERSION,
        "agent":             AGENT_ID,
        "summary":           summary,
        "thresholds":        thresholds,
        "threshold_alerts":  [
            {"metric":a.metric,"value":a.value,"threshold":a.threshold,
             "severity":a.severity,"message":a.message}
            for a in threshold_alerts
        ],
        "regression_alerts": [
            {"metric":a.metric,"current":a.current,"baseline":a.baseline,
             "delta_pct":a.delta_pct,"message":a.message}
            for a in regression_alerts
        ],
        "overall_result":    "failed" if (threshold_alerts or regression_alerts) else "passed",
        "generated_at":      now,
    }

    with open(args.report, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(f"Perf report: {args.report}")

    if threshold_alerts or regression_alerts:
        print(f"FAIL: {len(threshold_alerts)} threshold + {len(regression_alerts)} regression alert(s)")
        return 1

    avail = summary.get("available", False)
    if avail:
        print(f"PASS: CPI={summary.get('cpi','N/A')}, IPC={summary.get('ipc','N/A')}")
    else:
        print("PASS: No perf_counters data in log (RTL harness perf counters not enabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

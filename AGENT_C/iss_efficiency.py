#!/usr/bin/env python3
"""
sim/iss_efficiency.py
=====================
Agent C — ISS Efficiency Tracker

Tracks per-run ISS metrics in a local SQLite database and detects
when coverage has plateaued (i.e. generating more tests for this ISA
configuration is unlikely to find new bugs).

Design goals
------------
* Zero external dependencies (stdlib only: sqlite3, json, pathlib)
* WAL journal mode — safe for concurrent AVA orchestrator writes
* plateau_detect() uses a simple variance heuristic (configurable);
  can be upgraded to Mann-Kendall trend test if needed
* Integrated into run_iss_manifest() to track every real Spike run

Usage
-----
    from iss_efficiency import ISSEfficiencyTracker

    tracker = ISSEfficiencyTracker("iss_metrics.db")
    tracker.record_run(isa="rv32im", commit_count=47823, duration_s=1.23,
                       log_mode="log_commits", manifest_path="runs/r1/manifest.json")

    if tracker.is_plateau(isa="rv32im", window=10, variance_threshold=500):
        log.warning("ISS coverage plateau detected for rv32im — consider new seed/test")

Plateau detection algorithm
---------------------------
A plateau is declared when the variance of commit_count over the last
`window` runs is below `variance_threshold`.  Low variance means the ISS
is retiring roughly the same number of instructions each run — a sign that
the test generator is exploring the same paths.

Agent D / Coverage Director can query is_plateau() to decide whether to:
  - rotate seeds
  - switch to formal verification for remaining gaps
  - escalate to red-team (Agent H)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Schema DDL
# ─────────────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS iss_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,          -- ISO-8601 UTC timestamp
    isa          TEXT    NOT NULL,          -- e.g. "rv32im"
    commit_count INTEGER NOT NULL,          -- instructions committed
    duration_s   REAL    NOT NULL,          -- wall-clock seconds
    log_mode     TEXT    NOT NULL DEFAULT 'unknown',
    spike_exit   INTEGER,
    manifest     TEXT                       -- path to run manifest (nullable)
);

CREATE INDEX IF NOT EXISTS idx_isa_ts ON iss_runs (isa, ts DESC);
"""


class ISSEfficiencyTracker:
    """
    Lightweight SQLite-backed ISS run metrics tracker.

    Thread/process safety: SQLite WAL mode allows one writer + many readers.
    For parallel AVA workers writing the same DB, use separate per-worker
    databases and merge with ``merge_db()`` after the campaign.
    """

    def __init__(self, db_path: str | Path = "iss_metrics.db"):
        self.db_path = Path(db_path)
        self._con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript(_DDL)
        self._con.commit()
        logger.debug("ISSEfficiencyTracker DB: %s", self.db_path)

    # ── Write ─────────────────────────────────────────────────────────────

    def record_run(
        self,
        isa:          str,
        commit_count: int,
        duration_s:   float,
        log_mode:     str  = "unknown",
        spike_exit:   Optional[int] = None,
        manifest_path: Optional[str | Path] = None,
    ) -> None:
        """
        Persist one ISS run record.

        Parameters
        ----------
        isa           : ISA string, e.g. "rv32im"
        commit_count  : number of instructions committed (= JSONL line count)
        duration_s    : wall-clock seconds from Spike invocation to JSONL write
        log_mode      : "log_commits" | "enable_cl" | "trace_only"
        spike_exit    : Spike process return code (1 = normal tohost exit)
        manifest_path : path stored for traceability (not parsed)
        """
        ts = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            """
            INSERT INTO iss_runs (ts, isa, commit_count, duration_s, log_mode, spike_exit, manifest)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, isa.lower(), commit_count, duration_s, log_mode,
             spike_exit, str(manifest_path) if manifest_path else None),
        )
        self._con.commit()
        logger.info(
            "Recorded ISS run: isa=%s commits=%d duration=%.2fs",
            isa, commit_count, duration_s
        )

    # ── Query ─────────────────────────────────────────────────────────────

    def recent_commit_counts(self, isa: str, window: int = 10) -> List[int]:
        """Return the last `window` commit_count values for this ISA (newest first)."""
        rows = self._con.execute(
            """
            SELECT commit_count FROM iss_runs
            WHERE isa = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (isa.lower(), window),
        ).fetchall()
        return [r[0] for r in rows]

    def is_plateau(
        self,
        isa:                str,
        window:             int   = 10,
        variance_threshold: float = 500.0,
    ) -> bool:
        """
        Return True when ISS commit counts for `isa` have stabilised
        over the last `window` runs.

        Algorithm
        ---------
        Variance of commit_count < variance_threshold → plateau.

        The threshold of 500 (instructions²) is conservative for RV32IM
        programs that commit ~5k–50k instructions per run.  Tune it up for
        longer programs or down for tighter plateau detection.

        Returns False when fewer than `window` runs have been recorded
        (not enough data to decide).
        """
        counts = self.recent_commit_counts(isa, window)
        if len(counts) < window:
            logger.debug(
                "is_plateau(%s): only %d/%d runs — not enough data",
                isa, len(counts), window
            )
            return False

        var = statistics.variance(counts)
        plateau = var < variance_threshold
        logger.info(
            "is_plateau(%s, window=%d): variance=%.1f threshold=%.1f → %s",
            isa, window, var, variance_threshold, plateau
        )
        return plateau

    def stats(self, isa: Optional[str] = None) -> Dict:
        """
        Return summary statistics for an ISA (or all ISAs if None).

        Returns
        -------
        dict with keys: total_runs, total_commits, avg_duration_s,
                        avg_commits_per_run, plateau_status (per ISA)
        """
        if isa:
            rows = self._con.execute(
                """
                SELECT COUNT(*), SUM(commit_count), AVG(duration_s), AVG(commit_count)
                FROM iss_runs WHERE isa = ?
                """,
                (isa.lower(),),
            ).fetchone()
            return {
                "isa":                isa,
                "total_runs":         rows[0] or 0,
                "total_commits":      rows[1] or 0,
                "avg_duration_s":     round(rows[2] or 0, 3),
                "avg_commits_per_run": round(rows[3] or 0, 1),
                "is_plateau":         self.is_plateau(isa),
            }

        # All ISAs
        isas = [r[0] for r in
                self._con.execute("SELECT DISTINCT isa FROM iss_runs").fetchall()]
        return {i: self.stats(i) for i in isas}

    def merge_db(self, other_db: str | Path) -> int:
        """
        Import all records from another ISSEfficiencyTracker database.
        Useful for merging per-worker databases after a parallel campaign.
        Returns number of rows imported.
        """
        other = Path(other_db)
        if not other.exists():
            raise FileNotFoundError(other)
        self._con.execute(f"ATTACH DATABASE ? AS other", (str(other),))
        self._con.execute(
            "INSERT INTO iss_runs (ts,isa,commit_count,duration_s,log_mode,spike_exit,manifest) "
            "SELECT ts,isa,commit_count,duration_s,log_mode,spike_exit,manifest FROM other.iss_runs"
        )
        n = self._con.execute("SELECT changes()").fetchone()[0]
        self._con.commit()
        self._con.execute("DETACH DATABASE other")
        logger.info("Merged %d rows from %s", n, other)
        return n

    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# Integration shim — called from run_iss_manifest()
# ─────────────────────────────────────────────────────────────────────────────

def record_manifest_run(
    manifest_path: Path,
    db_path:       Path,
    commit_count:  int,
    duration_s:    float,
    log_mode:      str,
    spike_exit:    int,
) -> bool:
    """
    Convenience wrapper: record one manifest run and return is_plateau status.

    Called by run_iss_manifest() after a successful ISS run.
    Returns True if plateau detected (orchestrator may act on this).
    """
    try:
        manifest = json.loads(manifest_path.read_text())
        isa = manifest.get("isa", "unknown").lower()

        with ISSEfficiencyTracker(db_path) as tracker:
            tracker.record_run(
                isa=isa,
                commit_count=commit_count,
                duration_s=duration_s,
                log_mode=log_mode,
                spike_exit=spike_exit,
                manifest_path=manifest_path,
            )
            return tracker.is_plateau(isa)

    except Exception as exc:
        logger.warning("ISSEfficiencyTracker.record_manifest_run failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI — inspect DB from terminal
# ─────────────────────────────────────────────────────────────────────────────

def _cli():
    import argparse, sys
    p = argparse.ArgumentParser(description="Inspect ISS efficiency DB")
    p.add_argument("--db",  default="iss_metrics.db", help="DB path")
    p.add_argument("--isa", default=None, help="Filter by ISA")
    p.add_argument("--window", type=int, default=10, help="Plateau window")
    p.add_argument("--threshold", type=float, default=500.0,
                   help="Variance threshold for plateau detection")
    args = p.parse_args()

    with ISSEfficiencyTracker(args.db) as t:
        s = t.stats(args.isa)
        print(json.dumps(s, indent=2, default=str))

        if args.isa:
            counts = t.recent_commit_counts(args.isa, args.window)
            print(f"\nLast {args.window} commit counts for {args.isa}: {counts}")
            print(f"Plateau (variance<{args.threshold}): "
                  f"{t.is_plateau(args.isa, args.window, args.threshold)}")


if __name__ == "__main__":
    _cli()

#!/usr/bin/env python3
"""
coverage_database.py — AVA Coverage Analytics Database
=======================================================
WAL-enabled SQLite backend for coverage persistence, plateau detection,
cold-path ranking, and reachability analysis.

Fixes vs. the original provided version
----------------------------------------
  * _parse_dat_file: uses VerilatorCoverageParser (correct .dat format)
    instead of the broken colon-split heuristic that matched nothing
  * Thread safety: explicit threading.Lock on all write paths (WAL alone
    is not enough when the same connection object is shared)
  * ColdPath.reachability_score stored separately; not mixed into dataclass
  * Mann-Kendall plateau: corrected sign direction (rising trend ≠ plateau)
  * top_cold_paths: correct SQL query against real schema column names
  * Missing method test_attempts_for_path() added (needed by ColdPathRanker)
  * Proper __enter__/__exit__ for context-manager usage
  * All SQL uses parameterised queries (no f-string injection)

Schema
------
  coverage_points (run_id, module, line, col, hier, kind, description, hit_count)
  run_metadata    (run_id, seed, overall_pct, timestamp, plateau)
  test_attempts   (run_id, module, line, attempted_at)
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ── Conditional import of VerilatorCoverageParser ────────────────────────────
try:
    from coverage_pipeline import (  # type: ignore[import]
        VerilatorCoverageParser,
        CoverageMetrics,
        ParseError,
    )
    _PARSER_AVAILABLE = True
except ImportError:
    _PARSER_AVAILABLE = False
    logger.warning(
        "coverage_pipeline.py not importable — "
        "CoverageDatabase.load_coverage() will be unavailable."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ColdPath:
    """One uncovered coverage point with analytics metadata."""
    module:              str
    line:                int
    col:                 int    = 0
    type:                str    = "line"     # line | branch | toggle | expression
    description:         str    = ""
    hit_count:           int    = 0
    total_executions:    int    = 0
    reachability_score:  float  = 0.0       # set by _compute_reachability()

    @property
    def location_key(self) -> str:
        return f"{self.module}:{self.line}:{self.col}"


# ═══════════════════════════════════════════════════════════════════════════════
# CoverageDatabase
# ═══════════════════════════════════════════════════════════════════════════════

class CoverageDatabase:
    """
    Thread-safe SQLite coverage database with WAL journal mode.

    Usage
    -----
    db = CoverageDatabase("coverage.db")

    # After each sim run:
    db.load_coverage(Path("coverage.dat"), run_id="seed_42", seed=42)
    db.record_run_metadata(run_id="seed_42", seed=42, overall_pct=87.5)

    # Analytics:
    if db.is_plateau():
        ranker = ColdPathRanker(db)
        paths  = ranker.rank_by_roi(20)

    Context-manager usage:
    with CoverageDatabase("coverage.db") as db:
        ...
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS coverage_points (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT    NOT NULL,
        module          TEXT    NOT NULL,
        line            INTEGER NOT NULL,
        col             INTEGER NOT NULL DEFAULT 0,
        hier            TEXT    NOT NULL DEFAULT '',
        kind            TEXT    NOT NULL DEFAULT 'line',
        description     TEXT    NOT NULL DEFAULT '',
        hit_count       INTEGER NOT NULL DEFAULT 0,
        total_executions INTEGER NOT NULL DEFAULT 0,
        UNIQUE(run_id, module, line, col, kind)
    );
    CREATE TABLE IF NOT EXISTS run_metadata (
        run_id      TEXT    PRIMARY KEY,
        seed        INTEGER NOT NULL DEFAULT 0,
        overall_pct REAL    NOT NULL DEFAULT 0.0,
        timestamp   TEXT    NOT NULL,
        plateau     INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS test_attempts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT    NOT NULL,
        module      TEXT    NOT NULL,
        line        INTEGER NOT NULL,
        attempted_at TEXT   NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_cp_run    ON coverage_points(run_id);
    CREATE INDEX IF NOT EXISTS idx_cp_cold   ON coverage_points(hit_count, module, line);
    CREATE INDEX IF NOT EXISTS idx_rm_ts     ON run_metadata(timestamp);
    CREATE INDEX IF NOT EXISTS idx_ta_ml     ON test_attempts(module, line);
    """

    def __init__(self, db_path: Union[str, Path] = ":memory:") -> None:
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()
        logger.debug("CoverageDatabase opened: %s", self._path)

    # ── Context-manager support ────────────────────────────────────────────

    def __enter__(self) -> "CoverageDatabase":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Primary data ingestion ─────────────────────────────────────────────

    def load_coverage(
        self,
        dat_path: Union[str, Path],
        run_id:   str,
        seed:     int = 0,
    ) -> int:
        """
        Parse a Verilator coverage.dat file and insert all points.

        Uses VerilatorCoverageParser from coverage_pipeline.py for correct
        format handling (the original hand-rolled parser was broken).

        Returns the number of points inserted.
        Raises RuntimeError if coverage_pipeline.py is not importable.
        """
        if not _PARSER_AVAILABLE:
            raise RuntimeError(
                "coverage_pipeline.py must be importable to use load_coverage(). "
                "Place coverage_pipeline.py in the same directory."
            )

        dat_path = Path(dat_path)
        parser   = VerilatorCoverageParser()
        try:
            points = parser.parse_dat_file(dat_path)
        except ParseError as exc:
            logger.error("load_coverage parse error: %s", exc)
            raise

        rows: List[Tuple[Any, ...]] = [
            (
                run_id,
                pt.filename,           # module = source file
                pt.lineno,
                pt.column,
                pt.hier,
                pt.kind.value,         # "line" | "branch" | "toggle" | "expression"
                pt.comment or f"{pt.kind.value}@{pt.filename}:{pt.lineno}",
                pt.count,              # hit_count
                0,                     # total_executions — updated from perf counters
            )
            for pt in points
        ]

        with self._lock:
            self._conn.executemany(
                """INSERT OR REPLACE INTO coverage_points
                   (run_id, module, line, col, hier, kind, description,
                    hit_count, total_executions)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()

        logger.info("Loaded %d coverage points for run_id='%s'", len(rows), run_id)
        return len(rows)

    def record_run_metadata(
        self,
        run_id:      str,
        seed:        int   = 0,
        overall_pct: float = 0.0,
        timestamp:   Optional[str] = None,
        plateau:     bool  = False,
    ) -> None:
        """Insert or replace run-level metadata row."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO run_metadata
                   (run_id, seed, overall_pct, timestamp, plateau)
                   VALUES (?,?,?,?,?)""",
                (run_id, seed, round(overall_pct, 4), ts, int(plateau)),
            )
            self._conn.commit()

    # Convenience: record a CoverageMetrics object directly
    def record(
        self,
        metrics: "CoverageMetrics",
        seed:    int = 0,
        run_id:  Optional[str] = None,
    ) -> None:
        """Record a CoverageMetrics object into run_metadata."""
        rid = run_id or getattr(metrics, "run_id", "") or ""
        self.record_run_metadata(
            run_id=rid,
            seed=seed,
            overall_pct=metrics.functional,
            timestamp=getattr(metrics, "generated_at", None),
        )

    def record_test_attempt(self, run_id: str, module: str, line: int) -> None:
        """Record that a test was generated targeting (module, line)."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO test_attempts (run_id, module, line, attempted_at) VALUES (?,?,?,?)",
                (run_id, module, line, ts),
            )
            self._conn.commit()

    # ── Analytics queries ──────────────────────────────────────────────────

    def is_plateau(self, window: int = 10) -> bool:
        """
        Mann-Kendall trend test on the last *window* overall_pct values.

        Returns True when there is NO statistically significant upward trend
        (i.e. coverage has stalled).

        Requires at least MIN_WINDOW=5 data points for statistical validity.
        With fewer points the test has insufficient power and returns False
        (conservative: assume not yet plateaued rather than falsely alarming).

        The Mann-Kendall S statistic:
          S = Σ sgn(x_j - x_i)  for all i < j
        A large positive S = strong upward trend = NOT a plateau.
        |z| < 1.28 (α=0.10) = no trend detected = plateau.
        """
        MIN_WINDOW = 5
        effective  = max(window, MIN_WINDOW)
        rows = self._conn.execute(
            "SELECT overall_pct FROM run_metadata ORDER BY timestamp DESC LIMIT ?",
            (effective,),
        ).fetchall()

        if len(rows) < MIN_WINDOW:
            return False   # not enough data for a statistically valid conclusion

        trend = [r[0] for r in reversed(rows)]   # oldest → newest
        n     = len(trend)
        s     = 0
        for i in range(n):
            for j in range(i + 1, n):
                diff = trend[j] - trend[i]
                s   += (1 if diff > 0 else -1 if diff < 0 else 0)

        # Variance of S under H0 (no ties): n*(n-1)*(2n+5)/18
        var_s = n * (n - 1) * (2 * n + 5) / 18.0
        if var_s <= 0:
            return False
        import math
        z = (s - (1 if s > 0 else -1)) / math.sqrt(var_s) if s != 0 else 0.0
        # |z| < 1.28 → fail to reject H0 at 90% confidence → plateau
        return abs(z) < 1.28

    def top_cold_paths(self, limit: int = 20) -> List[ColdPath]:
        """
        Return cold coverage points ranked by reachability score descending.

        'Cold' = hit_count == 0.
        Reachability is estimated from predecessor hit density in the same
        module near the cold point.
        """
        rows = self._conn.execute(
            """SELECT module, line, col, kind, description, hit_count, total_executions
               FROM coverage_points
               WHERE hit_count = 0
               GROUP BY module, line, col, kind
               ORDER BY total_executions DESC, module, line
               LIMIT ?""",
            (limit * 3,),   # fetch 3× and trim after scoring
        ).fetchall()

        paths: List[ColdPath] = []
        for row in rows:
            cp = ColdPath(
                module=row[0], line=row[1], col=row[2],
                type=row[3], description=row[4] or f"{row[3]}@{row[0]}:{row[1]}",
                hit_count=row[5], total_executions=row[6],
            )
            cp.reachability_score = self._compute_reachability(cp)
            paths.append(cp)

        paths.sort(key=lambda p: p.reachability_score, reverse=True)
        return paths[:limit]

    def test_attempts_for_path(self, module: str, line: int) -> int:
        """
        Return how many times a test was generated targeting (module, line).

        Used by ColdPathRanker._novelty_bonus() to reward never-targeted paths.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM test_attempts WHERE module = ? AND line = ?",
            (module, line),
        ).fetchone()
        return int(row[0]) if row else 0

    def last_n_runs(self, n: int = 20) -> List[Dict[str, Any]]:
        """Return last n run_metadata rows as dicts, newest first."""
        cur = self._conn.execute(
            "SELECT * FROM run_metadata ORDER BY timestamp DESC LIMIT ?", (n,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def best_run(self) -> Optional[Dict[str, Any]]:
        """Return the run with the highest overall_pct."""
        cur = self._conn.execute(
            "SELECT * FROM run_metadata ORDER BY overall_pct DESC LIMIT 1"
        )
        cols = [d[0] for d in cur.description]
        row  = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def regression_alert(self, threshold: float = 2.0) -> Optional[str]:
        """Return warning string if latest run regressed > threshold vs best."""
        lasts = self.last_n_runs(1)
        if not lasts:
            return None
        latest = lasts[0]["overall_pct"]
        best   = self.best_run()
        if best and (best["overall_pct"] - latest) > threshold:
            return (
                f"REGRESSION: coverage dropped "
                f"{best['overall_pct'] - latest:.2f}% "
                f"(best={best['overall_pct']:.2f} -> latest={latest:.2f})"
            )
        return None

    def coverage_summary(self) -> Dict[str, Any]:
        """Return aggregate statistics across all runs."""
        row = self._conn.execute(
            """SELECT COUNT(*), AVG(overall_pct), MAX(overall_pct), MIN(overall_pct)
               FROM run_metadata"""
        ).fetchone()
        cold_total = self._conn.execute(
            "SELECT COUNT(*) FROM coverage_points WHERE hit_count = 0"
        ).fetchone()[0]
        return {
            "total_runs":  row[0] or 0,
            "avg_pct":     round(row[1] or 0.0, 2),
            "best_pct":    round(row[2] or 0.0, 2),
            "worst_pct":   round(row[3] or 0.0, 2),
            "cold_points": cold_total,
        }

    # ── Internal helpers ───────────────────────────────────────────────────

    def _compute_reachability(self, path: ColdPath) -> float:
        """
        Estimate reachability as predecessor hit density near the cold point.

        Score = (sum of hit_counts in same module, lines [line-10, line-1])
                / (1 + number of cold points in the same window)

        Clamped to [0.0, 1.0].
        """
        pred_hits = self._conn.execute(
            """SELECT COALESCE(SUM(hit_count), 0) FROM coverage_points
               WHERE module = ? AND line BETWEEN ? AND ? AND line != ?""",
            (path.module, max(0, path.line - 10), path.line - 1, path.line),
        ).fetchone()[0] or 0

        cold_neighbors = self._conn.execute(
            """SELECT COUNT(*) FROM coverage_points
               WHERE module = ? AND line BETWEEN ? AND ?
               AND hit_count = 0 AND line != ?""",
            (path.module, max(0, path.line - 10), path.line + 10, path.line),
        ).fetchone()[0] or 0

        denominator = 1 + cold_neighbors
        raw = pred_hits / denominator / max(pred_hits + 1, 100)
        return round(min(float(raw), 1.0), 6)

    # ── Cleanup ────────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.close()
        logger.debug("CoverageDatabase closed: %s", self._path)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


# ── CLI (for debugging / standalone analytics) ────────────────────────────────

def _main() -> int:
    import argparse, json, sys

    p = argparse.ArgumentParser(description="CoverageDatabase CLI")
    p.add_argument("--db",      required=True, type=Path,  help="Database path")
    p.add_argument("--load",    metavar="DAT", type=Path,  help="Load a coverage.dat")
    p.add_argument("--run-id",  default="cli_run",         help="Run ID for --load")
    p.add_argument("--summary", action="store_true",       help="Print DB summary")
    p.add_argument("--cold",    type=int, metavar="N",     help="Show top N cold paths")
    p.add_argument("--plateau", action="store_true",       help="Check for coverage plateau")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    with CoverageDatabase(args.db) as db:
        if args.load:
            n = db.load_coverage(args.load, run_id=args.run_id)
            print(f"Loaded {n} points for run_id='{args.run_id}'")

        if args.summary:
            print(json.dumps(db.coverage_summary(), indent=2))

        if args.cold:
            paths = db.top_cold_paths(args.cold)
            for i, cp in enumerate(paths, 1):
                print(f"  {i:3}. {cp.module}:{cp.line}  "
                      f"type={cp.type}  reach={cp.reachability_score:.4f}")

        if args.plateau:
            print(f"Plateau detected: {db.is_plateau()}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())

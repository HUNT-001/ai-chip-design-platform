"""
core/coverage_director.py

Tracks real coverage evolution across simulation runs,
identifies coverage gaps using LLM analysis, and
drives the next test-generation cycle.

Uses SQLite (no PostgreSQL dependency for local dev).
Drop-in upgrade path to PostgreSQL/SQLAlchemy when needed.

Dependencies:
    pip install requests  (for Ollama)
    sqlite3 is part of the Python standard library
"""

import re
import json
import time
import sqlite3
import logging
import asyncio
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import contextmanager

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

OLLAMA_URL         = "http://localhost:11434"
DEFAULT_MODEL      = "qwen2.5-coder:7b"
DEFAULT_DB_PATH    = "coverage.db"
LLM_TIMEOUT_SEC    = 90
MAX_HISTORY_ROWS   = 10     # Rows fed into LLM to avoid context overflow
SIGNOFF_THRESHOLD  = 95.0   # Coverage % required for tape-out sign-off


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class CoverageSnapshot:
    """One full coverage measurement at a point in time."""
    run_id:               int
    timestamp:            str
    line_coverage:        float
    toggle_coverage:      float
    branch_coverage:      float
    functional_coverage:  float
    overall:              float
    bugs_found:           int
    rtl_spec:             str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id":              self.run_id,
            "timestamp":           self.timestamp,
            "line_coverage":       self.line_coverage,
            "toggle_coverage":     self.toggle_coverage,
            "branch_coverage":     self.branch_coverage,
            "functional_coverage": self.functional_coverage,
            "overall":             self.overall,
            "bugs_found":          self.bugs_found,
            "rtl_spec":            self.rtl_spec,
        }


@dataclass
class CoverageGap:
    """A specific uncovered area with actionable suggestions."""
    block:       str
    gap_type:    str          # "line" | "branch" | "toggle" | "functional"
    current_pct: float
    target_pct:  float
    suggestion:  str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "block":       self.block,
            "gap_type":    self.gap_type,
            "current_pct": self.current_pct,
            "target_pct":  self.target_pct,
            "suggestion":  self.suggestion,
        }


@dataclass
class CoverageTrend:
    """Summary of coverage trajectory over recent runs."""
    direction:       str    # "improving" | "stagnant" | "regressing"
    delta_per_run:   float  # Average change in overall coverage per run
    runs_analysed:   int
    estimated_runs_to_signoff: Optional[int]   # None if stagnant or regressing

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction":                  self.direction,
            "delta_per_run":              round(self.delta_per_run, 3),
            "runs_analysed":              self.runs_analysed,
            "estimated_runs_to_signoff":  self.estimated_runs_to_signoff,
        }


# ─────────────────────────────────────────────
# Lightweight async Ollama client (shared pattern)
# ─────────────────────────────────────────────

class _OllamaClient:
    """Minimal requests-based Ollama client (mirrors api/main.py pattern)."""

    def __init__(self, base_url: str = OLLAMA_URL, model: str = DEFAULT_MODEL):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self._available: Optional[bool] = None

    def _check(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/api/tags", timeout=3).status_code == 200
        except Exception:
            return False

    async def generate(self, prompt: str, timeout: int = LLM_TIMEOUT_SEC) -> str:
        if self._available is None:
            self._available = self._check()
        if not self._available:
            return ""
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: requests.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model":   self.model,
                        "prompt":  prompt[:4000],
                        "stream":  False,
                        "options": {"temperature": 0.1, "num_predict": 1024},
                    },
                    timeout=timeout,
                    headers={"Content-Type": "application/json"},
                ),
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except requests.Timeout:
            logger.error("Ollama timeout in CoverageDirector.")
            self._available = False
            return ""
        except Exception as e:
            logger.error(f"Ollama error in CoverageDirector: {e}")
            return ""


# ─────────────────────────────────────────────
# SQLite Schema Manager
# ─────────────────────────────────────────────

class _SchemaManager:
    """Manages the SQLite schema — creates tables and indices if missing."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS coverage_runs (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp            TEXT    NOT NULL,
        rtl_spec             TEXT    NOT NULL DEFAULT '',
        line_coverage        REAL    NOT NULL DEFAULT 0.0,
        toggle_coverage      REAL    NOT NULL DEFAULT 0.0,
        branch_coverage      REAL    NOT NULL DEFAULT 0.0,
        functional_coverage  REAL    NOT NULL DEFAULT 0.0,
        overall_coverage     REAL    NOT NULL DEFAULT 0.0,
        bugs_found           INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_timestamp ON coverage_runs (timestamp);
    CREATE INDEX IF NOT EXISTS idx_overall   ON coverage_runs (overall_coverage);

    CREATE TABLE IF NOT EXISTS coverage_gaps (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       INTEGER NOT NULL REFERENCES coverage_runs(id),
        block        TEXT    NOT NULL,
        gap_type     TEXT    NOT NULL,
        current_pct  REAL    NOT NULL,
        target_pct   REAL    NOT NULL,
        suggestion   TEXT    NOT NULL DEFAULT ''
    );
    """

    def apply(self, conn: sqlite3.Connection) -> None:
        conn.executescript(self.SCHEMA)
        conn.commit()


# ─────────────────────────────────────────────
# Coverage Director
# ─────────────────────────────────────────────

class CoverageDirector:
    """
    Tracks coverage evolution, identifies gaps, and drives the next
    test-generation iteration toward sign-off.

    Thread-safe: uses a per-instance lock for all DB writes so it can
    be safely called from multiple asyncio tasks or threads.
    """

    def __init__(
        self,
        db_path:     str = DEFAULT_DB_PATH,
        llm_model:   str = DEFAULT_MODEL,
        ollama_url:  str = OLLAMA_URL,
        target_coverage: float = SIGNOFF_THRESHOLD,
    ):
        self.db_path          = Path(db_path)
        self.target_coverage  = target_coverage
        self._llm             = _OllamaClient(base_url=ollama_url, model=llm_model)
        self._lock            = threading.Lock()

        self._init_db()
        logger.info(
            f"CoverageDirector ready — db={self.db_path}, "
            f"target={target_coverage}%, model={llm_model}"
        )

    # ── Public API ────────────────────────────────────────────────────────

    def record_run(
        self,
        line_coverage:       float,
        toggle_coverage:     float,
        branch_coverage:     float,
        functional_coverage: float,
        bugs_found:          int  = 0,
        rtl_spec:            str  = "",
    ) -> int:
        """
        Persist a coverage measurement to the database.

        Returns:
            The new run_id (integer primary key).
        """
        overall = (
            line_coverage       * 0.35 +
            toggle_coverage     * 0.25 +
            branch_coverage     * 0.25 +
            functional_coverage * 0.15
        )
        timestamp = datetime.now(timezone.utc).isoformat()

        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO coverage_runs
                   (timestamp, rtl_spec, line_coverage, toggle_coverage,
                    branch_coverage, functional_coverage, overall_coverage, bugs_found)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (timestamp, rtl_spec, line_coverage, toggle_coverage,
                 branch_coverage, functional_coverage, overall, bugs_found),
            )
            run_id = cursor.lastrowid
            conn.commit()

        logger.info(
            f"Recorded run #{run_id}: overall={overall:.2f}%, bugs={bugs_found}"
        )
        return run_id

    def get_latest(self) -> Optional[CoverageSnapshot]:
        """Return the most recent coverage snapshot, or None if no runs exist."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM coverage_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def get_history(self, limit: int = MAX_HISTORY_ROWS) -> List[CoverageSnapshot]:
        """Return the N most recent snapshots, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM coverage_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_snapshot(r) for r in reversed(rows)]

    def compute_trend(self, lookback: int = 5) -> CoverageTrend:
        """
        Analyse the last `lookback` runs to determine coverage trajectory.
        Determines if we are improving, stagnant, or regressing, and
        estimates how many more runs are needed to reach sign-off.
        """
        history = self.get_history(lookback)

        if len(history) < 2:
            return CoverageTrend(
                direction="unknown",
                delta_per_run=0.0,
                runs_analysed=len(history),
                estimated_runs_to_signoff=None,
            )

        overalls = [s.overall for s in history]
        deltas   = [overalls[i+1] - overalls[i] for i in range(len(overalls) - 1)]
        avg_delta = sum(deltas) / len(deltas)

        if avg_delta > 0.2:
            direction = "improving"
        elif avg_delta < -0.2:
            direction = "regressing"
        else:
            direction = "stagnant"

        latest = overalls[-1]
        gap    = self.target_coverage - latest
        if avg_delta > 0 and gap > 0:
            est_runs = int(gap / avg_delta) + 1
        else:
            est_runs = None

        return CoverageTrend(
            direction                = direction,
            delta_per_run            = avg_delta,
            runs_analysed            = len(history),
            estimated_runs_to_signoff = est_runs,
        )

    async def analyse_gaps(
        self,
        current_coverage: Dict[str, float],
        rtl_blocks:       Optional[List[str]] = None,
    ) -> List[CoverageGap]:
        """
        Use the LLM to identify which RTL blocks are under-covered and
        what test scenarios would close the gap.

        Args:
            current_coverage: {"line": 82.1, "toggle": 78.0, ...}
            rtl_blocks:       Optional list of known RTL block names.

        Returns:
            List of CoverageGap objects with actionable suggestions.
        """
        history = self.get_history(MAX_HISTORY_ROWS)
        llm_raw = await self._llm.generate(
            self._build_gap_prompt(current_coverage, history, rtl_blocks)
        )

        if llm_raw:
            gaps = self._parse_gaps(llm_raw, current_coverage)
        else:
            logger.warning("LLM unavailable — using rule-based gap analysis.")
            gaps = self._rule_based_gaps(current_coverage)

        # Persist gaps for this run
        latest = self.get_latest()
        if latest and gaps:
            self._save_gaps(latest.run_id, gaps)

        return gaps

    def is_signoff_ready(self) -> bool:
        """Return True if the latest overall coverage meets or exceeds the target."""
        latest = self.get_latest()
        if not latest:
            return False
        return latest.overall >= self.target_coverage

    def summary(self) -> Dict[str, Any]:
        """Return a complete dashboard summary."""
        latest  = self.get_latest()
        trend   = self.compute_trend()
        history = self.get_history(5)

        return {
            "latest_coverage":    latest.to_dict() if latest else None,
            "trend":              trend.to_dict(),
            "recent_history":     [s.to_dict() for s in history],
            "signoff_ready":      self.is_signoff_ready(),
            "target_coverage":    self.target_coverage,
        }

    # ── Prompt builders ───────────────────────────────────────────────────

    def _build_gap_prompt(
        self,
        current: Dict[str, float],
        history: List[CoverageSnapshot],
        blocks:  Optional[List[str]],
    ) -> str:
        history_summary = "\n".join(
            f"  Run #{s.run_id} ({s.timestamp[:10]}): "
            f"overall={s.overall:.1f}%, "
            f"line={s.line_coverage:.1f}%, "
            f"toggle={s.toggle_coverage:.1f}%, "
            f"branch={s.branch_coverage:.1f}%"
            for s in history
        ) or "  (no history yet)"

        block_list = ", ".join(blocks) if blocks else "ALU, LSU, BranchPredictor, RegFile, CSR"

        return f"""You are a RISC-V verification coverage expert.

CURRENT COVERAGE:
  Line:        {current.get('line', 0):.1f}%
  Toggle:      {current.get('toggle', 0):.1f}%
  Branch:      {current.get('branch', 0):.1f}%
  Functional:  {current.get('functional', 0):.1f}%

COVERAGE HISTORY (last {len(history)} runs):
{history_summary}

KNOWN RTL BLOCKS: {block_list}

Identify the 3 most impactful coverage gaps and suggest test scenarios to close them.
Focus on blocks that are likely underexercised given the metrics above.

Reply ONLY with a JSON array — no prose, no markdown:
[
  {{
    "block":       "ALU",
    "gap_type":    "branch",
    "current_pct": 74.0,
    "target_pct":  95.0,
    "suggestion":  "Test all ALU opcodes including illegal/reserved encodings."
  }}
]
"""

    # ── Parsing helpers ───────────────────────────────────────────────────

    def _parse_gaps(
        self,
        raw: str,
        current: Dict[str, float],
    ) -> List[CoverageGap]:
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)   # Remove trailing commas

        # Extract JSON array even if surrounded by stray text
        arr_match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not arr_match:
            logger.warning("No JSON array found in LLM gap response.")
            return self._rule_based_gaps(current)

        try:
            data = json.loads(arr_match.group(0))
        except json.JSONDecodeError as e:
            logger.warning(f"Gap JSON parse failed: {e}")
            return self._rule_based_gaps(current)

        gaps = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                gaps.append(CoverageGap(
                    block       = str(item.get("block", "unknown")),
                    gap_type    = str(item.get("gap_type", "line")),
                    current_pct = float(item.get("current_pct", 0.0)),
                    target_pct  = float(item.get("target_pct", self.target_coverage)),
                    suggestion  = str(item.get("suggestion", ""))[:500],
                ))
            except (TypeError, ValueError) as e:
                logger.debug(f"Skipping malformed gap entry: {e}")

        return gaps

    def _rule_based_gaps(self, current: Dict[str, float]) -> List[CoverageGap]:
        """
        Deterministic gap analysis when the LLM is unavailable.
        Flags any coverage dimension below target with a generic suggestion.
        """
        gaps = []
        rules = {
            "line":       ("all", "Add directed tests for uncovered statement paths."),
            "branch":     ("BranchPredictor", "Test all branch outcomes: taken/not-taken + misprediction recovery."),
            "toggle":     ("RegFile", "Toggle all register bits with alternating 0xAAAAAAAA / 0x55555555 patterns."),
            "functional": ("CSR", "Exercise all CSR read/write/set/clear operations."),
        }
        for gap_type, (block, suggestion) in rules.items():
            pct = current.get(gap_type, 0.0)
            if pct < self.target_coverage:
                gaps.append(CoverageGap(
                    block       = block,
                    gap_type    = gap_type,
                    current_pct = pct,
                    target_pct  = self.target_coverage,
                    suggestion  = suggestion,
                ))
        return gaps

    # ── DB helpers ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _SchemaManager().apply(conn)

    @contextmanager
    def _connect(self):
        """Context manager that opens a SQLite connection and always closes it."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")   # Better concurrent read performance
        try:
            yield conn
        finally:
            conn.close()

    def _save_gaps(self, run_id: int, gaps: List[CoverageGap]) -> None:
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO coverage_gaps
                   (run_id, block, gap_type, current_pct, target_pct, suggestion)
                   VALUES (?,?,?,?,?,?)""",
                [(run_id, g.block, g.gap_type, g.current_pct, g.target_pct, g.suggestion)
                 for g in gaps],
            )
            conn.commit()

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> CoverageSnapshot:
        return CoverageSnapshot(
            run_id               = row["id"],
            timestamp            = row["timestamp"],
            rtl_spec             = row["rtl_spec"],
            line_coverage        = row["line_coverage"],
            toggle_coverage      = row["toggle_coverage"],
            branch_coverage      = row["branch_coverage"],
            functional_coverage  = row["functional_coverage"],
            overall              = row["overall_coverage"],
            bugs_found           = row["bugs_found"],
        )
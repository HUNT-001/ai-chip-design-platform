"""
core/verification_state.py

Persistent verification state across API restarts.
Primary backend: SQLite (zero-config, works everywhere).
Optional upgrade: PostgreSQL via SQLAlchemy (set DATABASE_URL env var).

Supports:
- Save / resume interrupted verification runs
- Full run history with agent decisions
- Thread-safe writes
- Automatic schema migrations

Dependencies (core):    sqlite3  (stdlib)
Dependencies (upgrade): pip install sqlalchemy psycopg2-binary
"""

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Optional SQLAlchemy / PostgreSQL
# ─────────────────────────────────────────────
try:
    from sqlalchemy import (
        Column, Integer, String, Float, Text, create_engine, text
    )
    from sqlalchemy.orm import declarative_base, sessionmaker, Session
    SQLALCHEMY_AVAILABLE = True
    _Base = declarative_base()
except ImportError:
    SQLALCHEMY_AVAILABLE = False
    logger.info(
        "SQLAlchemy not installed — using SQLite backend. "
        "For PostgreSQL: pip install sqlalchemy psycopg2-binary"
    )


# ─────────────────────────────────────────────
# Enumerations & Data classes
# ─────────────────────────────────────────────

class RunStatus(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    TIMEOUT    = "timeout"
    CANCELLED  = "cancelled"


@dataclass
class AgentDecision:
    agent_id:   str
    action:     str
    reasoning:  str
    timestamp:  str = field(default_factory=lambda: _now())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationRun:
    id:               str
    rtl_spec:         str
    status:           RunStatus
    coverage:         Dict[str, float]
    bugs_found:       List[Dict[str, Any]]
    agent_decisions:  List[AgentDecision]
    created_at:       str
    updated_at:       str
    completed_at:     Optional[str]         = None
    error_message:    Optional[str]         = None
    microarch:        str                   = "in_order"
    target_coverage:  float                 = 95.0
    elapsed_sec:      float                 = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":               self.id,
            "rtl_spec":         self.rtl_spec,
            "status":           self.status.value,
            "coverage":         self.coverage,
            "bugs_found":       self.bugs_found,
            "agent_decisions":  [d.to_dict() for d in self.agent_decisions],
            "created_at":       self.created_at,
            "updated_at":       self.updated_at,
            "completed_at":     self.completed_at,
            "error_message":    self.error_message,
            "microarch":        self.microarch,
            "target_coverage":  self.target_coverage,
            "elapsed_sec":      self.elapsed_sec,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# SQLite backend
# ─────────────────────────────────────────────

class _SQLiteBackend:
    """
    Thread-safe SQLite state backend.
    Uses WAL mode for safe concurrent reads from multiple asyncio tasks.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS verification_runs (
        id               TEXT    PRIMARY KEY,
        rtl_spec         TEXT    NOT NULL DEFAULT '',
        microarch        TEXT    NOT NULL DEFAULT 'in_order',
        status           TEXT    NOT NULL DEFAULT 'pending',
        coverage         TEXT    NOT NULL DEFAULT '{}',
        bugs_found       TEXT    NOT NULL DEFAULT '[]',
        agent_decisions  TEXT    NOT NULL DEFAULT '[]',
        error_message    TEXT,
        target_coverage  REAL    NOT NULL DEFAULT 95.0,
        elapsed_sec      REAL    NOT NULL DEFAULT 0.0,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL,
        completed_at     TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_status     ON verification_runs (status);
    CREATE INDEX IF NOT EXISTS idx_created_at ON verification_runs (created_at);
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._apply_schema()

    # ── Internal connection management ────────────────────────────────────

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _apply_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    # ── CRUD ──────────────────────────────────────────────────────────────

    def save(self, run: VerificationRun) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO verification_runs
                   (id, rtl_spec, microarch, status, coverage, bugs_found,
                    agent_decisions, error_message, target_coverage,
                    elapsed_sec, created_at, updated_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.id,
                    run.rtl_spec,
                    run.microarch,
                    run.status.value,
                    json.dumps(run.coverage),
                    json.dumps(run.bugs_found),
                    json.dumps([d.to_dict() for d in run.agent_decisions]),
                    run.error_message,
                    run.target_coverage,
                    run.elapsed_sec,
                    run.created_at,
                    run.updated_at,
                    run.completed_at,
                ),
            )
            conn.commit()

    def get(self, run_id: str) -> Optional[VerificationRun]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM verification_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._row_to_run(row) if row else None

    def update_status(
        self,
        run_id:        str,
        status:        RunStatus,
        error_message: Optional[str] = None,
        elapsed_sec:   float = 0.0,
    ) -> bool:
        now = _now()
        completed_at = now if status in (
            RunStatus.COMPLETED, RunStatus.FAILED,
            RunStatus.TIMEOUT,   RunStatus.CANCELLED,
        ) else None

        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                """UPDATE verification_runs
                   SET status=?, updated_at=?, completed_at=?,
                       error_message=?, elapsed_sec=?
                   WHERE id=?""",
                (status.value, now, completed_at, error_message, elapsed_sec, run_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_coverage(self, run_id: str, coverage: Dict[str, float]) -> bool:
        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                "UPDATE verification_runs SET coverage=?, updated_at=? WHERE id=?",
                (json.dumps(coverage), _now(), run_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def append_decision(self, run_id: str, decision: AgentDecision) -> bool:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT agent_decisions FROM verification_runs WHERE id=?",
                (run_id,)
            ).fetchone()
            if not row:
                return False
            decisions = json.loads(row["agent_decisions"])
            decisions.append(decision.to_dict())
            conn.execute(
                "UPDATE verification_runs SET agent_decisions=?, updated_at=? WHERE id=?",
                (json.dumps(decisions), _now(), run_id),
            )
            conn.commit()
            return True

    def append_bug(self, run_id: str, bug: Dict[str, Any]) -> bool:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT bugs_found FROM verification_runs WHERE id=?",
                (run_id,)
            ).fetchone()
            if not row:
                return False
            bugs = json.loads(row["bugs_found"])
            bugs.append(bug)
            conn.execute(
                "UPDATE verification_runs SET bugs_found=?, updated_at=? WHERE id=?",
                (json.dumps(bugs), _now(), run_id),
            )
            conn.commit()
            return True

    def list_runs(
        self,
        status:  Optional[RunStatus] = None,
        limit:   int = 50,
        offset:  int = 0,
    ) -> List[VerificationRun]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM verification_runs WHERE status=? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status.value, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM verification_runs "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def delete(self, run_id: str) -> bool:
        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM verification_runs WHERE id=?", (run_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def count(self, status: Optional[RunStatus] = None) -> int:
        with self._conn() as conn:
            if status:
                return conn.execute(
                    "SELECT COUNT(*) FROM verification_runs WHERE status=?",
                    (status.value,)
                ).fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM verification_runs"
            ).fetchone()[0]

    # ── Row deserializer ──────────────────────────────────────────────────

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> VerificationRun:
        raw_decisions = json.loads(row["agent_decisions"] or "[]")
        decisions = [
            AgentDecision(
                agent_id  = d.get("agent_id", "unknown"),
                action    = d.get("action", ""),
                reasoning = d.get("reasoning", ""),
                timestamp = d.get("timestamp", _now()),
            )
            for d in raw_decisions
        ]
        return VerificationRun(
            id               = row["id"],
            rtl_spec         = row["rtl_spec"],
            microarch        = row["microarch"],
            status           = RunStatus(row["status"]),
            coverage         = json.loads(row["coverage"] or "{}"),
            bugs_found       = json.loads(row["bugs_found"] or "[]"),
            agent_decisions  = decisions,
            error_message    = row["error_message"],
            target_coverage  = row["target_coverage"],
            elapsed_sec      = row["elapsed_sec"],
            created_at       = row["created_at"],
            updated_at       = row["updated_at"],
            completed_at     = row["completed_at"],
        )


# ─────────────────────────────────────────────
# PostgreSQL backend (optional upgrade path)
# ─────────────────────────────────────────────

class _PostgresBackend:
    """
    SQLAlchemy-based PostgreSQL backend.
    Activated automatically when DATABASE_URL is set and SQLAlchemy is installed.
    Has the same public interface as _SQLiteBackend.
    """

    def __init__(self, database_url: str):
        if not SQLALCHEMY_AVAILABLE:
            raise RuntimeError(
                "SQLAlchemy not installed. Run: pip install sqlalchemy psycopg2-binary"
            )
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,         # Detect stale connections
            pool_size=5,
            max_overflow=10,
        )
        self._SessionLocal = sessionmaker(bind=self.engine)
        self._apply_schema()

    def _apply_schema(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS verification_runs (
                    id               VARCHAR PRIMARY KEY,
                    rtl_spec         TEXT    NOT NULL DEFAULT '',
                    microarch        VARCHAR NOT NULL DEFAULT 'in_order',
                    status           VARCHAR NOT NULL DEFAULT 'pending',
                    coverage         JSONB   NOT NULL DEFAULT '{}',
                    bugs_found       JSONB   NOT NULL DEFAULT '[]',
                    agent_decisions  JSONB   NOT NULL DEFAULT '[]',
                    error_message    TEXT,
                    target_coverage  FLOAT   NOT NULL DEFAULT 95.0,
                    elapsed_sec      FLOAT   NOT NULL DEFAULT 0.0,
                    created_at       TEXT    NOT NULL,
                    updated_at       TEXT    NOT NULL,
                    completed_at     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pg_status     ON verification_runs (status);
                CREATE INDEX IF NOT EXISTS idx_pg_created_at ON verification_runs (created_at);
            """))
            conn.commit()

    @contextmanager
    def _session(self):
        session = self._SessionLocal()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- All methods delegate to raw SQL for simplicity --

    def save(self, run: VerificationRun) -> None:
        with self._session() as s:
            s.execute(text("""
                INSERT INTO verification_runs
                    (id,rtl_spec,microarch,status,coverage,bugs_found,
                     agent_decisions,error_message,target_coverage,
                     elapsed_sec,created_at,updated_at,completed_at)
                VALUES
                    (:id,:rtl_spec,:microarch,:status,:coverage,:bugs_found,
                     :agent_decisions,:error_message,:target_coverage,
                     :elapsed_sec,:created_at,:updated_at,:completed_at)
                ON CONFLICT (id) DO UPDATE SET
                    status=EXCLUDED.status, coverage=EXCLUDED.coverage,
                    bugs_found=EXCLUDED.bugs_found,
                    agent_decisions=EXCLUDED.agent_decisions,
                    error_message=EXCLUDED.error_message,
                    elapsed_sec=EXCLUDED.elapsed_sec,
                    updated_at=EXCLUDED.updated_at,
                    completed_at=EXCLUDED.completed_at
            """), {
                "id":               run.id,
                "rtl_spec":         run.rtl_spec,
                "microarch":        run.microarch,
                "status":           run.status.value,
                "coverage":         json.dumps(run.coverage),
                "bugs_found":       json.dumps(run.bugs_found),
                "agent_decisions":  json.dumps([d.to_dict() for d in run.agent_decisions]),
                "error_message":    run.error_message,
                "target_coverage":  run.target_coverage,
                "elapsed_sec":      run.elapsed_sec,
                "created_at":       run.created_at,
                "updated_at":       run.updated_at,
                "completed_at":     run.completed_at,
            })
            s.commit()

    def get(self, run_id: str) -> Optional[VerificationRun]:
        with self._session() as s:
            row = s.execute(
                text("SELECT * FROM verification_runs WHERE id=:id"),
                {"id": run_id}
            ).mappings().fetchone()
        return self._map_to_run(row) if row else None

    def update_status(self, run_id, status, error_message=None, elapsed_sec=0.0):
        now = _now()
        completed_at = now if status in (
            RunStatus.COMPLETED, RunStatus.FAILED,
            RunStatus.TIMEOUT,   RunStatus.CANCELLED
        ) else None
        with self._session() as s:
            s.execute(text("""
                UPDATE verification_runs
                SET status=:s, updated_at=:u, completed_at=:c,
                    error_message=:e, elapsed_sec=:el
                WHERE id=:id
            """), dict(s=status.value, u=now, c=completed_at,
                       e=error_message, el=elapsed_sec, id=run_id))
            s.commit()

    def list_runs(self, status=None, limit=50, offset=0):
        with self._session() as s:
            if status:
                rows = s.execute(text(
                    "SELECT * FROM verification_runs WHERE status=:st "
                    "ORDER BY created_at DESC LIMIT :l OFFSET :o"
                ), {"st": status.value, "l": limit, "o": offset}).mappings().fetchall()
            else:
                rows = s.execute(text(
                    "SELECT * FROM verification_runs "
                    "ORDER BY created_at DESC LIMIT :l OFFSET :o"
                ), {"l": limit, "o": offset}).mappings().fetchall()
        return [self._map_to_run(r) for r in rows]

    def count(self, status=None):
        with self._session() as s:
            if status:
                return s.execute(
                    text("SELECT COUNT(*) FROM verification_runs WHERE status=:s"),
                    {"s": status.value}
                ).scalar()
            return s.execute(
                text("SELECT COUNT(*) FROM verification_runs")
            ).scalar()

    @staticmethod
    def _map_to_run(row) -> VerificationRun:
        raw_d = json.loads(row["agent_decisions"]) if isinstance(row["agent_decisions"], str) else row["agent_decisions"]
        decisions = [
            AgentDecision(
                agent_id  = d.get("agent_id", "unknown"),
                action    = d.get("action", ""),
                reasoning = d.get("reasoning", ""),
                timestamp = d.get("timestamp", _now()),
            )
            for d in (raw_d or [])
        ]
        coverage = row["coverage"]
        if isinstance(coverage, str):
            coverage = json.loads(coverage)
        bugs = row["bugs_found"]
        if isinstance(bugs, str):
            bugs = json.loads(bugs)
        return VerificationRun(
            id              = row["id"],
            rtl_spec        = row["rtl_spec"],
            microarch       = row["microarch"],
            status          = RunStatus(row["status"]),
            coverage        = coverage or {},
            bugs_found      = bugs or [],
            agent_decisions = decisions,
            error_message   = row["error_message"],
            target_coverage = row["target_coverage"],
            elapsed_sec     = row["elapsed_sec"],
            created_at      = row["created_at"],
            updated_at      = row["updated_at"],
            completed_at    = row["completed_at"],
        )


# ─────────────────────────────────────────────
# Public Manager (backend-agnostic)
# ─────────────────────────────────────────────

class VerificationStateManager:
    """
    Persistent verification state manager.

    Auto-selects backend:
    - PostgreSQL if DATABASE_URL env var is set and SQLAlchemy is installed.
    - SQLite otherwise (default path: verification_state.db).

    All methods are thread-safe.
    """

    def __init__(self, db_path: str = "verification_state.db"):
        database_url = os.environ.get("DATABASE_URL", "")

        if database_url and SQLALCHEMY_AVAILABLE:
            try:
                self._backend = _PostgresBackend(database_url)
                logger.info(f"VerificationStateManager: PostgreSQL backend active.")
            except Exception as e:
                logger.warning(
                    f"PostgreSQL backend failed ({e}). Falling back to SQLite."
                )
                self._backend = _SQLiteBackend(db_path)
        else:
            self._backend = _SQLiteBackend(db_path)
            logger.info(f"VerificationStateManager: SQLite backend active ({db_path}).")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def create_run(
        self,
        rtl_spec:        str,
        microarch:       str   = "in_order",
        target_coverage: float = 95.0,
    ) -> VerificationRun:
        """Create and persist a new verification run. Returns the run."""
        now = _now()
        run = VerificationRun(
            id               = str(uuid.uuid4()),
            rtl_spec         = rtl_spec,
            microarch        = microarch,
            status           = RunStatus.PENDING,
            coverage         = {},
            bugs_found       = [],
            agent_decisions  = [],
            target_coverage  = target_coverage,
            created_at       = now,
            updated_at       = now,
        )
        self._backend.save(run)
        logger.info(f"Created verification run {run.id} ({microarch})")
        return run

    def save_run(self, run: VerificationRun) -> None:
        """Persist (insert or update) a full run object."""
        run.updated_at = _now()
        self._backend.save(run)

    def get_run(self, run_id: str) -> Optional[VerificationRun]:
        """Fetch a run by ID. Returns None if not found."""
        return self._backend.get(run_id)

    def resume_run(self, run_id: str) -> Optional[VerificationRun]:
        """
        Resume an interrupted run.
        Marks the run as RUNNING and returns it, or None if not found.
        """
        run = self._backend.get(run_id)
        if not run:
            logger.warning(f"Cannot resume: run {run_id} not found.")
            return None

        if run.status in (RunStatus.COMPLETED, RunStatus.CANCELLED):
            logger.warning(f"Cannot resume run {run_id}: status is {run.status.value}.")
            return None

        self._backend.update_status(run_id, RunStatus.RUNNING)
        run.status = RunStatus.RUNNING
        logger.info(f"Resumed run {run_id} (was: {run.status.value})")
        return run

    # ── Updates ───────────────────────────────────────────────────────────

    def mark_running(self, run_id: str) -> bool:
        return self._backend.update_status(run_id, RunStatus.RUNNING)

    def mark_completed(self, run_id: str, elapsed_sec: float = 0.0) -> bool:
        return self._backend.update_status(
            run_id, RunStatus.COMPLETED, elapsed_sec=elapsed_sec
        )

    def mark_failed(self, run_id: str, error: str, elapsed_sec: float = 0.0) -> bool:
        return self._backend.update_status(
            run_id, RunStatus.FAILED, error_message=error, elapsed_sec=elapsed_sec
        )

    def mark_timeout(self, run_id: str) -> bool:
        return self._backend.update_status(
            run_id, RunStatus.TIMEOUT,
            error_message="Simulation timed out — suspected livelock."
        )

    def update_coverage(self, run_id: str, coverage: Dict[str, float]) -> bool:
        return self._backend.update_coverage(run_id, coverage)

    def record_decision(self, run_id: str, decision: AgentDecision) -> bool:
        return self._backend.append_decision(run_id, decision)

    def record_bug(self, run_id: str, bug: Dict[str, Any]) -> bool:
        return self._backend.append_bug(run_id, bug)

    # ── Queries ───────────────────────────────────────────────────────────

    def list_runs(
        self,
        status: Optional[RunStatus] = None,
        limit:  int = 50,
        offset: int = 0,
    ) -> List[VerificationRun]:
        return self._backend.list_runs(status=status, limit=limit, offset=offset)

    def list_incomplete(self) -> List[VerificationRun]:
        """Return all runs that were interrupted (RUNNING or PENDING)."""
        running = self._backend.list_runs(status=RunStatus.RUNNING,  limit=100)
        pending = self._backend.list_runs(status=RunStatus.PENDING,  limit=100)
        return running + pending

    def stats(self) -> Dict[str, Any]:
        return {
            "total":     self._backend.count(),
            "pending":   self._backend.count(RunStatus.PENDING),
            "running":   self._backend.count(RunStatus.RUNNING),
            "completed": self._backend.count(RunStatus.COMPLETED),
            "failed":    self._backend.count(RunStatus.FAILED),
            "timeout":   self._backend.count(RunStatus.TIMEOUT),
            "backend":   "postgresql" if isinstance(self._backend, _PostgresBackend) else "sqlite",
        }
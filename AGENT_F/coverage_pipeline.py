"""
coverage_pipeline.py — Agent F: Verilator Coverage Pipeline
============================================================
SOTA Verilator coverage extraction, aggregation, and AVA integration.

Architecture
------------
  VerilatorCoverageParser   — parse .dat files (all Verilator versions)
  CoverageMetrics           — aggregated type-safe metrics + cold paths
  FunctionalCoverageModel   — RV32IM instruction-category functional coverage
  CoverageDatabase          — cross-run trend tracking (SQLite-backed)
  CoverageReporter          — JSON / CSV / HTML export
  VerilatorCoverageBackend  — drop-in for SpikeISS._calculate_coverage()
  run_verilator_coverage    — subprocess wrapper for verilator_coverage

Design principles
-----------------
  * No placeholders — every metric is real or explicitly flagged as absent.
  * Deterministic / seedable (no hidden randomness).
  * Fully typed (PEP 526 style, Python 3.9+ compatible).
  * Thread-safe accumulation for parallel sim runs.
  * Graceful degradation — missing tools raise informative errors.
  * Testable — all public classes have no hidden I/O side-effects by default.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

__version__ = "3.0.0"
__all__ = [
    "CoverageKind", "CoveragePoint", "CoverageMetrics",
    "FunctionalCoverageModel", "CoverageDatabase", "CoverageReporter",
    "VerilatorCoverageParser", "VerilatorCoverageBackend",
    "run_verilator_coverage", "extract_coverage_from_run", "save_coverage_report",
    "CoverageError", "ParseError", "BackendError",
    "parse_spike_commit_log", "parse_dut_commit_log", "count_cycles_instrets",
    "_parse_commit_log", "_count_cycles_instrs",   # backward-compat aliases
    # v3.0: manifest / cross-agent contract utilities
    "atomic_write", "format_ava_schema", "load_manifest", "update_manifest",
    "ManifestError", "EXIT_SUCCESS", "EXIT_PARSE_ERROR", "EXIT_CONFIG_ERROR",
]

# ── AVA contract exit codes ────────────────────────────────────────────────────
EXIT_SUCCESS      = 0    # pipeline ran cleanly
EXIT_PARSE_ERROR  = 1    # .dat parse / coverage calculation failed
EXIT_CONFIG_ERROR = 3    # manifest / config problem (missing file, bad schema)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════

class CoverageError(Exception):
    """Base class for all coverage pipeline errors."""


class ParseError(CoverageError):
    """Raised when a .dat file cannot be parsed."""
    def __init__(self, message: str, path: Optional[Path] = None, line: int = 0):
        self.path = path
        self.line_number = line
        loc = f" [{path}:{line}]" if path else ""
        super().__init__(f"{message}{loc}")


class BackendError(CoverageError):
    """Raised when the Verilator backend fails."""


class DatabaseError(CoverageError):
    """Raised when the coverage SQLite database is inaccessible."""


class ManifestError(CoverageError):
    """Raised when the AVA manifest.json is missing, malformed, or violates contract."""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Enumerations
# ═══════════════════════════════════════════════════════════════════════════════

class CoverageKind(str, Enum):
    """Type of a Verilator coverage point."""
    LINE       = "line"
    BRANCH     = "branch"
    TOGGLE     = "toggle"
    EXPRESSION = "expression"
    ASSERT     = "assert"
    UNKNOWN    = "unknown"

    @classmethod
    def from_comment(cls, comment: str) -> "CoverageKind":
        """
        Classify kind from Verilator comment label:
          ''        -> LINE      (empty = statement hit)
          'b<n>'    -> BRANCH
          's<n>'    -> TOGGLE    (signal transition)
          'e<n>'    -> EXPRESSION
          'a<n>'    -> ASSERT    (SVA property)
        """
        c = comment.strip().lower()
        if not c:
            return cls.LINE
        mapping = {"b": cls.BRANCH, "s": cls.TOGGLE, "e": cls.EXPRESSION, "a": cls.ASSERT}
        return mapping.get(c[0], cls.UNKNOWN)


class DatVersion(Enum):
    """Detected Verilator .dat file format version."""
    V4_FULL  = auto()   # full 7-field (Verilator >= 4.0)
    V4_SHORT = auto()   # 4-field (older 4.x)
    MERGED   = auto()   # output of verilator_coverage --write
    UNKNOWN  = auto()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CoveragePoint:
    """Immutable representation of one Verilator coverage database entry."""
    kind:     CoverageKind
    count:    int
    filename: str
    lineno:   int
    column:   int
    hier:     str
    comment:  str

    @property
    def is_cold(self) -> bool:
        return self.count == 0

    @property
    def location_key(self) -> str:
        return f"{self.filename}:{self.lineno}:{self.column}:{self.comment}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value, "count": self.count,
            "filename": self.filename, "lineno": self.lineno,
            "column": self.column, "hier": self.hier, "comment": self.comment,
        }


@dataclass
class CoverageMetrics:
    """
    Aggregated, typed coverage metrics with cold-path detail.
    All percentage fields are in [0.0, 100.0].
    """
    # Percentage metrics
    line:       float = 0.0
    branch:     float = 0.0
    toggle:     float = 0.0
    expression: float = 0.0
    functional: float = 0.0   # weighted composite

    # Raw counts
    lines_hit:         int = 0
    lines_total:       int = 0
    branches_hit:      int = 0
    branches_total:    int = 0
    toggles_hit:       int = 0
    toggles_total:     int = 0
    expressions_hit:   int = 0
    expressions_total: int = 0
    asserts_hit:       int = 0
    asserts_total:     int = 0

    # Cold-path detail (capped at MAX_COLD_PATHS per kind)
    cold_lines:    List[Dict[str, Any]] = field(default_factory=list)
    cold_branches: List[Dict[str, Any]] = field(default_factory=list)
    cold_toggles:  List[Dict[str, Any]] = field(default_factory=list)

    # Provenance
    source_file:       str = ""
    generated_at:      str = ""
    verilator_version: str = ""
    run_id:            str = ""

    MAX_COLD_PATHS: int = field(default=500, init=False, repr=False, compare=False)
    WEIGHTS: Dict[str, float] = field(
        default_factory=lambda: {"line": 0.35, "branch": 0.35,
                                 "toggle": 0.20, "expression": 0.10},
        init=False, repr=False, compare=False,
    )

    def to_ava_dict(self) -> Dict[str, float]:
        """Dict consumed by AVA's VerificationResult.coverage and CoverageDirector."""
        return {
            "line":       round(self.line, 2),
            "branch":     round(self.branch, 2),
            "toggle":     round(self.toggle, 2),
            "expression": round(self.expression, 2),
            "functional": round(self.functional, 2),
        }

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("MAX_COLD_PATHS", "WEIGHTS"):
            d.pop(k, None)
        return d

    def is_industrial_grade(
        self,
        line_threshold: float = 95.0,
        branch_threshold: float = 90.0,
        toggle_threshold: float = 85.0,
    ) -> bool:
        return (self.line >= line_threshold
                and self.branch >= branch_threshold
                and self.toggle >= toggle_threshold)

    def delta(self, previous: "CoverageMetrics") -> Dict[str, float]:
        return {
            "line":       round(self.line - previous.line, 2),
            "branch":     round(self.branch - previous.branch, 2),
            "toggle":     round(self.toggle - previous.toggle, 2),
            "expression": round(self.expression - previous.expression, 2),
            "functional": round(self.functional - previous.functional, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Verilator .dat parser
# ═══════════════════════════════════════════════════════════════════════════════

class VerilatorCoverageParser:
    """
    Parse Verilator coverage database files (.dat).

    Supports:
      * Verilator >= 4.x full 7-field format
      * Older 4-field short format
      * Merged .dat produced by verilator_coverage --write
      * UTF-8 and Latin-1 encoded files (auto-detected)
      * Gzip-compressed .dat.gz files

    Thread-safe: each instance maintains its own state.
    """

    # Full 7-field:  [type] 'count' 'filename' 'lineno' 'col' 'hier' 'comment'
    _RE_FULL = re.compile(
        r"^[A-Z]?\s*'(?P<count>\d+)'\s+'(?P<filename>[^']*)'\s+"
        r"'(?P<lineno>\d+)'\s+'(?P<column>\d+)'\s+'(?P<hier>[^']*)'\s+'(?P<comment>[^']*)'",
        re.ASCII,
    )
    # Short 4-field:  'count' 'filename' 'lineno' 'comment'
    _RE_SHORT = re.compile(
        r"^'(?P<count>\d+)'\s+'(?P<filename>[^']*)'\s+'(?P<lineno>\d+)'\s+'(?P<comment>[^']*)'",
        re.ASCII,
    )
    # Merged numeric:  count filename:lineno[.col] [hier] [comment]
    _RE_MERGED = re.compile(
        r"^(?P<count>\d+)\s+(?P<filename>[^:]+):(?P<lineno>\d+)"
        r"(?:\.(?P<column>\d+))?(?:\s+(?P<hier>\S+))?(?:\s+'?(?P<comment>[^']*)'?)?",
        re.ASCII,
    )
    _RE_VERSION = re.compile(r"verilator\s+([\d.]+)", re.IGNORECASE)

    def __init__(self, max_cold_paths: int = 500):
        self._max_cold = max_cold_paths
        self._version: str = ""
        self._format: DatVersion = DatVersion.UNKNOWN
        self._parse_errors: List[str] = []

    @property
    def version(self) -> str:
        return self._version

    @property
    def detected_format(self) -> DatVersion:
        return self._format

    @property
    def parse_errors(self) -> List[str]:
        return list(self._parse_errors)

    def parse_dat_file(self, path: Union[str, Path]) -> List[CoveragePoint]:
        """
        Parse one .dat file; return a deduplicated list of CoveragePoints.

        Raises ParseError if the file is missing or completely unparseable.
        """
        path = Path(path)
        if not path.exists():
            raise ParseError("Coverage .dat not found", path=path)
        if not path.is_file():
            raise ParseError("Path is not a regular file", path=path)

        self._parse_errors = []
        raw_lines = self._read_file(path)
        points, self._version, self._format = self._parse_lines(raw_lines, path)

        has_data_lines = any(
            l.strip() and not l.startswith("#") for l in raw_lines
        )
        if not points and has_data_lines:
            raise ParseError(
                "File contains data lines but no points could be parsed. "
                "Check Verilator version compatibility.",
                path=path,
            )

        deduped = self._deduplicate(points)
        logger.info(
            "Parsed %d points (%d unique) from %s [format=%s ver=%s]",
            len(points), len(deduped), path.name,
            self._format.name, self._version or "unknown",
        )
        return deduped

    def parse_dat_directory(
        self,
        directory: Union[str, Path],
        pattern: str = "*.dat",
        max_workers: int = 4,
        run_id: str = "",
    ) -> List[CoveragePoint]:
        """
        Parse all .dat files in *directory* concurrently and merge.
        Returns merged, deduplicated list of CoveragePoints.
        """
        directory = Path(directory)
        dat_files = sorted(directory.glob(pattern))
        if not dat_files:
            logger.warning("No files matching '%s' in %s", pattern, directory)
            return []

        all_points: List[CoveragePoint] = []
        lock = threading.Lock()

        def _worker(dat: Path) -> List[CoveragePoint]:
            p = VerilatorCoverageParser(max_cold_paths=self._max_cold)
            try:
                return p.parse_dat_file(dat)
            except ParseError as exc:
                logger.warning("Skipping %s: %s", dat.name, exc)
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_worker, f): f for f in dat_files}
            for fut in concurrent.futures.as_completed(futures):
                with lock:
                    all_points.extend(fut.result())

        merged = self._deduplicate(all_points)
        logger.info("Merged %d files -> %d unique points", len(dat_files), len(merged))
        return merged

    def aggregate(
        self,
        points: List[CoveragePoint],
        run_id: str = "",
    ) -> CoverageMetrics:
        """Aggregate a list of CoveragePoints into CoverageMetrics."""
        metrics = CoverageMetrics(
            generated_at=datetime.now(timezone.utc).isoformat(),
            verilator_version=self._version,
            run_id=run_id,
        )

        by_kind: Dict[CoverageKind, List[CoveragePoint]] = {k: [] for k in CoverageKind}
        for pt in points:
            by_kind[pt.kind].append(pt)

        def _pct(hit: int, total: int) -> float:
            return round(100.0 * hit / total, 4) if total else 0.0

        def _cold_dicts(pts: List[CoveragePoint]) -> List[Dict[str, Any]]:
            cold = sorted(
                (p for p in pts if p.is_cold),
                key=lambda p: (p.filename, p.lineno, p.column),
            )
            return [
                {"file": p.filename, "line": p.lineno, "column": p.column,
                 "hier": p.hier, "comment": p.comment, "kind": p.kind.value}
                for p in cold[:metrics.MAX_COLD_PATHS]
            ]

        # Line
        lines = by_kind[CoverageKind.LINE]
        metrics.lines_total = len(lines)
        metrics.lines_hit   = sum(1 for p in lines if not p.is_cold)
        metrics.line        = _pct(metrics.lines_hit, metrics.lines_total)
        metrics.cold_lines  = _cold_dicts(lines)

        # Branch
        branches = by_kind[CoverageKind.BRANCH]
        metrics.branches_total = len(branches)
        metrics.branches_hit   = sum(1 for p in branches if not p.is_cold)
        metrics.branch         = _pct(metrics.branches_hit, metrics.branches_total)
        metrics.cold_branches  = _cold_dicts(branches)

        # Toggle
        toggles = by_kind[CoverageKind.TOGGLE]
        metrics.toggles_total = len(toggles)
        metrics.toggles_hit   = sum(1 for p in toggles if not p.is_cold)
        metrics.toggle        = _pct(metrics.toggles_hit, metrics.toggles_total)
        metrics.cold_toggles  = _cold_dicts(toggles)

        # Expression
        exprs = by_kind[CoverageKind.EXPRESSION]
        metrics.expressions_total = len(exprs)
        metrics.expressions_hit   = sum(1 for p in exprs if not p.is_cold)
        metrics.expression        = _pct(metrics.expressions_hit, metrics.expressions_total)

        # Assert
        asserts = by_kind[CoverageKind.ASSERT]
        metrics.asserts_total = len(asserts)
        metrics.asserts_hit   = sum(1 for p in asserts if not p.is_cold)

        # Weighted functional composite
        w = metrics.WEIGHTS
        metrics.functional = round(
            w["line"]       * metrics.line
            + w["branch"]   * metrics.branch
            + w["toggle"]   * metrics.toggle
            + w["expression"] * metrics.expression,
            4,
        )
        return metrics

    # ── Internal helpers ───────────────────────────────────────────────────

    def _read_file(self, path: Path) -> List[str]:
        """Read with automatic encoding detection and optional gzip support."""
        if path.suffix == ".gz":
            import gzip
            with gzip.open(path, "rt", errors="replace") as fh:
                return fh.readlines()
        for encoding in ("utf-8", "latin-1"):
            try:
                with open(path, encoding=encoding, errors="strict") as fh:
                    return fh.readlines()
            except UnicodeDecodeError:
                continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readlines()

    def _parse_lines(
        self, raw_lines: List[str], path: Path
    ) -> Tuple[List[CoveragePoint], str, DatVersion]:
        version = ""
        fmt = DatVersion.UNKNOWN
        points: List[CoveragePoint] = []
        first_data = True

        for lineno, raw in enumerate(raw_lines, start=1):
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith("#"):
                m = self._RE_VERSION.search(line)
                if m:
                    version = m.group(1)
                continue
            if first_data:
                fmt = self._detect_format(line)
                first_data = False
            pt = self._parse_single_line(line)
            if pt is not None:
                points.append(pt)
            else:
                self._parse_errors.append(f"{path}:{lineno}: unparseable: {line[:80]}")
                logger.debug("Unparseable line %d: %.80s", lineno, line)

        return points, version, fmt

    def _detect_format(self, line: str) -> DatVersion:
        if self._RE_FULL.match(line):
            return DatVersion.V4_FULL
        if self._RE_SHORT.match(line):
            return DatVersion.V4_SHORT
        if self._RE_MERGED.match(line):
            return DatVersion.MERGED
        return DatVersion.UNKNOWN

    def _parse_single_line(self, line: str) -> Optional[CoveragePoint]:
        m = self._RE_FULL.match(line)
        if m:
            return self._make_point(m, has_hier=True, has_col=True)
        m = self._RE_SHORT.match(line)
        if m:
            return self._make_point(m, has_hier=False, has_col=False)
        m = self._RE_MERGED.match(line)
        if m:
            return self._make_point(m, has_hier=True, has_col=True)
        return None

    @staticmethod
    def _make_point(m: re.Match, *, has_hier: bool, has_col: bool) -> CoveragePoint:
        comment  = (m.group("comment") or "").strip()
        gd = m.groupdict()
        column   = int(gd.get("column") or 0) if has_col else 0
        hier     = (gd.get("hier") or "").strip() if has_hier else ""
        filename = (gd.get("filename") or "").strip()
        return CoveragePoint(
            kind=CoverageKind.from_comment(comment),
            count=int(m.group("count")),
            filename=filename,
            lineno=int(m.group("lineno")),
            column=column,
            hier=hier,
            comment=comment,
        )

    @staticmethod
    def _deduplicate(points: List[CoveragePoint]) -> List[CoveragePoint]:
        """Keep the point with the highest count per location key."""
        best: Dict[str, CoveragePoint] = {}
        for pt in points:
            key = pt.location_key
            existing = best.get(key)
            if existing is None or pt.count > existing.count:
                best[key] = pt
        return list(best.values())


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Functional coverage model (RV32IM)
# ═══════════════════════════════════════════════════════════════════════════════

class FunctionalCoverageModel:
    """
    RV32IM instruction-level functional coverage.

    Parses a commit log (list of instruction hex words) and computes
    per-instruction-category hit rates. Supplements structural coverage.
    Thread-safe via internal lock.
    """

    _OPCODE_TABLE: Dict[int, Tuple[str, str]] = {
        0b0110111: ("RV32I", "LUI"),
        0b0010111: ("RV32I", "AUIPC"),
        0b1101111: ("RV32I", "JAL"),
        0b1100111: ("RV32I", "JALR"),
        0b1100011: ("RV32I", "BRANCH"),
        0b0000011: ("RV32I", "LOAD"),
        0b0100011: ("RV32I", "STORE"),
        0b0010011: ("RV32I", "ALU_I"),
        0b0110011: ("RV32I_M", "ALU_R_OR_M"),
        0b0001111: ("SYSTEM", "FENCE"),
        0b1110011: ("SYSTEM", "SYSTEM_CSR"),
    }
    _M_FUNCT3: Dict[int, str] = {
        0: "MUL", 1: "MULH", 2: "MULHSU", 3: "MULHU",
        4: "DIV", 5: "DIVU", 6: "REM",  7: "REMU",
    }
    _ALU_R_FUNCT3: Dict[int, Tuple[str, str]] = {
        0: ("ADD", "SUB"), 1: ("SLL", "SLL"), 2: ("SLT", "SLT"),
        3: ("SLTU", "SLTU"), 4: ("XOR", "XOR"), 5: ("SRL", "SRA"),
        6: ("OR", "OR"), 7: ("AND", "AND"),
    }
    _UNIVERSE: Dict[str, Set[str]] = {
        "RV32I":  {"LUI","AUIPC","JAL","JALR","BRANCH","LOAD","STORE","ALU_I",
                   "ADD","SUB","SLL","SLT","SLTU","XOR","SRL","SRA","OR","AND"},
        "RV32M":  {"MUL","MULH","MULHSU","MULHU","DIV","DIVU","REM","REMU"},
        "SYSTEM": {"ECALL","EBREAK","MRET","WFI","FENCE","CSR"},
    }

    def __init__(self) -> None:
        self._seen: Dict[str, Set[str]] = {}
        self._total: int = 0
        self._lock = threading.Lock()

    def ingest_commit_log(self, commit_log: List[Dict[str, Any]]) -> None:
        """Ingest a list of {pc, instr, rd, rd_val} commit entries."""
        with self._lock:
            for entry in commit_log:
                try:
                    word = int(entry.get("instr", "0x0"), 16)
                except (ValueError, TypeError):
                    continue
                self._decode(word)
                self._total += 1

    def _decode(self, word: int) -> None:
        opcode = word & 0x7F
        info = self._OPCODE_TABLE.get(opcode)
        if info is None:
            return
        group, mnemonic = info

        if group == "RV32I_M":
            funct7 = (word >> 25) & 0x7F
            funct3 = (word >> 12) & 0x07
            if funct7 == 0b0000001:
                group, mnemonic = "RV32M", self._M_FUNCT3.get(funct3, "MUL")
            else:
                group = "RV32I"
                pair = self._ALU_R_FUNCT3.get(funct3, ("ALU_R", "ALU_R"))
                mnemonic = pair[1] if funct7 & 0x20 else pair[0]

        self._seen.setdefault(group, set()).add(mnemonic)

    def compute(self) -> Dict[str, Dict[str, Any]]:
        """Return per-category functional coverage dict."""
        with self._lock:
            result: Dict[str, Dict[str, Any]] = {}
            for cat, universe in self._UNIVERSE.items():
                seen    = self._seen.get(cat, set())
                hit     = len(universe & seen)
                total   = len(universe)
                missing = sorted(universe - seen)
                result[cat] = {
                    "hit": hit, "total": total,
                    "pct": round(100.0 * hit / total, 2) if total else 0.0,
                    "missing": missing,
                }
            return result

    def overall_pct(self) -> float:
        fc = self.compute()
        total_u = sum(v["total"] for v in fc.values())
        total_h = sum(v["hit"]   for v in fc.values())
        return round(100.0 * total_h / total_u, 2) if total_u else 0.0

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._total = 0


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Coverage database (SQLite, thread-safe)
# ═══════════════════════════════════════════════════════════════════════════════

class CoverageDatabase:
    """
    Persistent cross-run coverage trend database backed by SQLite.
    Thread-safe via per-write lock + WAL journal mode.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT    NOT NULL DEFAULT '',
        seed        INTEGER NOT NULL DEFAULT 0,
        ts          TEXT    NOT NULL,
        line_pct    REAL    NOT NULL,
        branch_pct  REAL    NOT NULL,
        toggle_pct  REAL    NOT NULL,
        expr_pct    REAL    NOT NULL,
        functional  REAL    NOT NULL,
        bug_count   INTEGER NOT NULL DEFAULT 0,
        source_dat  TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_runs_id   ON runs(id);
    CREATE INDEX IF NOT EXISTS idx_runs_func ON runs(functional);
    """

    def __init__(self, db_path: Union[str, Path] = ":memory:") -> None:
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def record(self, metrics: CoverageMetrics, seed: int = 0, bug_count: int = 0) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO runs
                       (run_id,seed,ts,line_pct,branch_pct,toggle_pct,
                        expr_pct,functional,bug_count,source_dat)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (metrics.run_id, seed,
                     metrics.generated_at or datetime.now(timezone.utc).isoformat(),
                     metrics.line, metrics.branch, metrics.toggle,
                     metrics.expression, metrics.functional,
                     bug_count, metrics.source_file),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                raise DatabaseError(f"Failed to insert run: {exc}") from exc

    def last_n(self, n: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def best(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM runs ORDER BY functional DESC LIMIT 1")
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None

    def regression_alert(self, threshold: float = 2.0) -> Optional[str]:
        last = self.last_n(1)
        if not last:
            return None
        latest = last[0]["functional"]
        best   = self.best()
        if best and (best["functional"] - latest) > threshold:
            return (f"REGRESSION: functional dropped "
                    f"{best['functional']-latest:.2f}% "
                    f"(best={best['functional']:.2f} -> latest={latest:.2f})")
        return None

    def plateau_detected(self, window: int = 5, threshold: float = 0.5) -> bool:
        rows = self.last_n(window)
        if len(rows) < window:
            return False
        vals = [r["functional"] for r in rows]
        return (max(vals) - min(vals)) < threshold

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Coverage reporter (JSON / CSV / HTML)
# ═══════════════════════════════════════════════════════════════════════════════

class CoverageReporter:
    """Multi-format coverage report writer."""

    SCHEMA_VERSION = "2.0"

    def __init__(self, metrics: CoverageMetrics) -> None:
        self._m = metrics

    def write_json(self, out_dir: Path, extra: Optional[Dict[str, Any]] = None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        m = self._m
        report: Dict[str, Any] = {
            "schema_version":    self.SCHEMA_VERSION,
            "generated_at":      m.generated_at,
            "verilator_version": m.verilator_version,
            "run_id":            m.run_id,
            "source_file":       m.source_file,
            "summary":           m.to_ava_dict(),
            "raw_counts": {
                "line":       {"hit": m.lines_hit,       "total": m.lines_total},
                "branch":     {"hit": m.branches_hit,    "total": m.branches_total},
                "toggle":     {"hit": m.toggles_hit,     "total": m.toggles_total},
                "expression": {"hit": m.expressions_hit, "total": m.expressions_total},
                "assert":     {"hit": m.asserts_hit,     "total": m.asserts_total},
            },
            "weights":          m.WEIGHTS,
            "industrial_grade": m.is_industrial_grade(),
            "cold_paths": {
                "lines":    m.cold_lines,
                "branches": m.cold_branches,
                "toggles":  m.cold_toggles,
            },
        }
        if extra:
            report["extra"] = extra
        path = out_dir / "coverage_report.json"
        atomic_write(path, json.dumps(report, indent=2, ensure_ascii=False))
        logger.info("JSON report -> %s", path)
        return path

    def write_ava_summary(
        self,
        out_dir: Path,
        extra: Optional[Dict[str, Any]] = None,
        plateau: bool = False,
        top_cold: Optional[List[Dict[str, Any]]] = None,
    ) -> Path:
        """
        Write ``coveragesummary.json`` — the AVA inter-agent contract output.

        This is the canonical output consumed by other agents (Agent A schema,
        Agent D comparator, Agent H red team).  File name and schema are
        contractually fixed by ``schemas/run_manifest.schema.json``.

        Parameters
        ----------
        out_dir  : destination directory (typically ``<rundir>/``)
        extra    : arbitrary extra fields merged under ``"extra"`` key
        plateau  : whether the CoverageDatabase detected a plateau
        top_cold : ranked cold paths from ColdPathRanker (optional)
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        m = self._m
        summary = format_ava_schema(
            m.to_ava_dict(),
            raw_counts={
                "line":       {"hit": m.lines_hit,       "total": m.lines_total},
                "branch":     {"hit": m.branches_hit,    "total": m.branches_total},
                "toggle":     {"hit": m.toggles_hit,     "total": m.toggles_total},
                "expression": {"hit": m.expressions_hit, "total": m.expressions_total},
            },
            industrial_grade=m.is_industrial_grade(),
            run_id=m.run_id,
            generated_at=m.generated_at,
            source_file=m.source_file,
        )
        summary["plateau_detected"] = plateau
        summary["top_cold_paths"]   = top_cold or []
        if extra:
            summary["extra"] = extra

        path = out_dir / "coveragesummary.json"
        atomic_write(path, json.dumps(summary, indent=2, ensure_ascii=False))
        logger.info("AVA summary -> %s", path)
        return path

    def write_csv(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        m = self._m
        path = out_dir / "coverage_report.csv"
        rows = [
            ("metric", "hit", "total", "pct"),
            ("line",       m.lines_hit,       m.lines_total,       m.line),
            ("branch",     m.branches_hit,    m.branches_total,    m.branch),
            ("toggle",     m.toggles_hit,     m.toggles_total,     m.toggle),
            ("expression", m.expressions_hit, m.expressions_total, m.expression),
        ]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        logger.info("CSV report  -> %s", path)
        return path

    def write_html(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        m = self._m

        def _bar(pct: float, label: str) -> str:
            c = "#22c55e" if pct >= 90 else "#f59e0b" if pct >= 70 else "#ef4444"
            return (
                f'<div style="margin:6px 0"><span style="display:inline-block;width:110px;'
                f'font-weight:600">{label}</span>'
                f'<div style="display:inline-block;width:300px;background:#e5e7eb;border-radius:4px">'
                f'<div style="width:{pct:.1f}%;background:{c};border-radius:4px;padding:2px 6px;'
                f'color:#fff;font-size:12px">{pct:.2f}%</div></div></div>'
            )

        bars = (
            _bar(m.line, "Line")
            + _bar(m.branch, "Branch")
            + _bar(m.toggle, "Toggle")
            + _bar(m.expression, "Expression")
            + _bar(m.functional, "Functional ★")
        )
        grade_ok = m.is_industrial_grade()
        grade_txt = "✔ INDUSTRIAL GRADE" if grade_ok else "✘ Below Threshold"
        grade_col = "#16a34a" if grade_ok else "#dc2626"

        def _cold_rows(items: List[Dict[str, Any]], limit: int = 20) -> str:
            return "".join(
                f"{p['file']}:{p['line']}  {p['hier']}  [{p['comment']}]\n"
                for p in items[:limit]
            )

        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>AVA Coverage Report</title>
<style>body{{font-family:system-ui,sans-serif;max-width:860px;margin:40px auto;padding:20px}}
pre{{background:#f1f5f9;padding:10px;overflow:auto;font-size:12px}}</style>
</head><body>
<h1>AVA Coverage Report</h1>
<p style="color:grey">Generated: {m.generated_at} | Run: {m.run_id or 'N/A'} | {m.source_file}</p>
<p style="color:{grade_col};font-weight:bold;font-size:1.2em">{grade_txt}</p>
<h2>Metrics</h2>{bars}
<h2>Raw Counts</h2>
<table border="1" cellpadding="6" style="border-collapse:collapse">
<tr><th>Metric</th><th>Hit</th><th>Total</th><th>%</th></tr>
<tr><td>Line</td><td>{m.lines_hit}</td><td>{m.lines_total}</td><td>{m.line:.2f}</td></tr>
<tr><td>Branch</td><td>{m.branches_hit}</td><td>{m.branches_total}</td><td>{m.branch:.2f}</td></tr>
<tr><td>Toggle</td><td>{m.toggles_hit}</td><td>{m.toggles_total}</td><td>{m.toggle:.2f}</td></tr>
<tr><td>Expression</td><td>{m.expressions_hit}</td><td>{m.expressions_total}</td><td>{m.expression:.2f}</td></tr>
</table>
<h2>Cold Lines (top {min(20,len(m.cold_lines))} of {len(m.cold_lines)})</h2>
<pre>{_cold_rows(m.cold_lines)}</pre>
<h2>Cold Branches (top {min(20,len(m.cold_branches))} of {len(m.cold_branches)})</h2>
<pre>{_cold_rows(m.cold_branches)}</pre>
<h2>Cold Toggles (top {min(20,len(m.cold_toggles))} of {len(m.cold_toggles)})</h2>
<pre>{_cold_rows(m.cold_toggles)}</pre>
</body></html>"""

        path = out_dir / "coverage_report.html"
        path.write_text(html, encoding="utf-8")
        logger.info("HTML report -> %s", path)
        return path


# ═══════════════════════════════════════════════════════════════════════════════
# 8. verilator_coverage subprocess wrapper
# ═══════════════════════════════════════════════════════════════════════════════

def run_verilator_coverage(
    dat_path: Union[str, Path],
    annotate_dir: Optional[Union[str, Path]] = None,
    write_merged: Optional[Union[str, Path]] = None,
    additional_dats: Optional[List[Union[str, Path]]] = None,
    verilator_coverage_bin: str = "verilator_coverage",
    timeout: int = 120,
) -> Tuple[int, str, str]:
    """
    Run the verilator_coverage tool.

    Returns (returncode, stdout, stderr).
    Raises BackendError if the binary is not on PATH.
    """
    bin_path = shutil.which(verilator_coverage_bin)
    if bin_path is None:
        raise BackendError(
            f"'{verilator_coverage_bin}' not found on PATH. "
            "Install Verilator and ensure it is on PATH."
        )

    cmd: List[str] = [bin_path, str(dat_path)]
    if additional_dats:
        cmd.extend(str(d) for d in additional_dats)
    if annotate_dir:
        Path(annotate_dir).mkdir(parents=True, exist_ok=True)
        cmd += ["--annotate", str(annotate_dir), "--annotate-all"]
    if write_merged:
        cmd += ["--write", str(write_merged)]

    logger.info("Running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            logger.warning("verilator_coverage rc=%d: %s",
                           proc.returncode, proc.stderr[:800])
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        logger.error("verilator_coverage timed out after %ds", timeout)
        return -1, "", "timeout"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Top-level extraction function
# ═══════════════════════════════════════════════════════════════════════════════

def extract_coverage_from_run(
    run_dir: Union[str, Path],
    dat_filename: str = "coverage.dat",
    run_id: str = "",
) -> CoverageMetrics:
    """
    Locate, parse, and aggregate coverage for one completed simulation run.

    Search order:
      1. <run_dir>/<dat_filename>
      2. Any *.dat in <run_dir>
      3. Any *.dat in <run_dir>/obj_dir/

    Raises ParseError if no usable .dat is found.
    """
    run_dir = Path(run_dir)

    candidates: List[Path] = []
    canonical = run_dir / dat_filename
    if canonical.exists():
        candidates.append(canonical)

    if not candidates:
        candidates = sorted(run_dir.glob("*.dat"))
    if not candidates:
        obj_dir = run_dir / "obj_dir"
        if obj_dir.is_dir():
            candidates = sorted(obj_dir.glob("*.dat"))

    if not candidates:
        raise ParseError(
            f"No Verilator coverage .dat found in {run_dir}. "
            "Ensure Verilator was compiled with --coverage and the simulation completed."
        )

    parser = VerilatorCoverageParser()

    if len(candidates) == 1:
        points = parser.parse_dat_file(candidates[0])
        metrics = parser.aggregate(points, run_id=run_id)
        metrics.source_file = str(candidates[0])
    else:
        logger.info("Found %d .dat files; merging", len(candidates))
        all_pts: List[CoveragePoint] = []
        for c in candidates:
            try:
                all_pts.extend(parser.parse_dat_file(c))
            except ParseError as exc:
                logger.warning("Skipping %s: %s", c.name, exc)
        deduped = VerilatorCoverageParser._deduplicate(all_pts)
        metrics = parser.aggregate(deduped, run_id=run_id)
        metrics.source_file = str(run_dir)

    return metrics


def save_coverage_report(
    metrics: CoverageMetrics,
    run_dir: Union[str, Path],
    extra: Optional[Dict[str, Any]] = None,
    formats: Sequence[str] = ("json",),
    ava_summary: bool = False,
    plateau: bool = False,
    top_cold: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Path]:
    """
    Write coverage reports to *run_dir* in the requested formats.

    Parameters
    ----------
    formats     : iterable of ``'json'``, ``'csv'``, ``'html'``
    ava_summary : if True, also write ``coveragesummary.json`` (AVA contract)
    plateau     : plateau flag forwarded to ``write_ava_summary``
    top_cold    : ranked cold paths from ColdPathRanker

    Returns dict mapping format name -> output path.
    """
    run_dir  = Path(run_dir)
    reporter = CoverageReporter(metrics)
    out: Dict[str, Path] = {}
    if "json" in formats:
        out["json"] = reporter.write_json(run_dir, extra=extra)
    if "csv"  in formats:
        out["csv"]  = reporter.write_csv(run_dir)
    if "html" in formats:
        out["html"] = reporter.write_html(run_dir)
    if ava_summary:
        out["ava_summary"] = reporter.write_ava_summary(
            run_dir, extra=extra, plateau=plateau, top_cold=top_cold
        )
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 10. AVA integration backend
# ═══════════════════════════════════════════════════════════════════════════════

class VerilatorCoverageBackend:
    """
    Drop-in replacement for SpikeISS._calculate_coverage().

    Priority chain:
      1. Real Verilator .dat in run_dir     <- always preferred
      2. coverage_data dict in rtl_results  <- legacy/manual stub
      3. Zeros + loud WARNING               <- last resort

    Thread-safe via internal lock.
    """

    def __init__(
        self,
        run_dir: Union[str, Path] = ".",
        dat_filename: str = "coverage.dat",
        fallback_on_missing: bool = True,
        report_formats: Sequence[str] = ("json",),
        database: Optional[CoverageDatabase] = None,
    ) -> None:
        self._run_dir       = Path(run_dir)
        self._dat_filename  = dat_filename
        self._fallback      = fallback_on_missing
        self._formats       = list(report_formats)
        self._database      = database
        self._metrics: Optional[CoverageMetrics] = None
        self._lock          = threading.Lock()
        self._func_model    = FunctionalCoverageModel()

    def update_run_dir(self, run_dir: Union[str, Path]) -> None:
        """Call before each new simulation run."""
        with self._lock:
            self._run_dir = Path(run_dir)
            self._func_model.reset()

    def get_coverage(self, rtl_results: Dict[str, Any]) -> Dict[str, float]:
        """
        Compute and return coverage dict for AVA's VerificationResult.
        Writes reports to run_dir and optionally records in database.
        """
        with self._lock:
            commit_log = rtl_results.get("commit_log", [])
            if commit_log:
                self._func_model.ingest_commit_log(commit_log)

            metrics = self._resolve_metrics(rtl_results)
            self._metrics = metrics

            # Blend in instruction-level functional coverage
            instr_fc = self._func_model.overall_pct()
            if instr_fc > 0.0:
                metrics.functional = round(
                    0.7 * metrics.functional + 0.3 * instr_fc, 4
                )

            # Save reports
            extra = {
                "instr_functional_pct": instr_fc,
                "functional_detail": self._func_model.compute(),
            }
            save_coverage_report(metrics, self._run_dir, extra=extra, formats=self._formats)

            # Database record
            if self._database is not None:
                bug_count = len(rtl_results.get("mismatches", []))
                try:
                    self._database.record(metrics, bug_count=bug_count)
                    alert = self._database.regression_alert()
                    if alert:
                        logger.warning(alert)
                    if self._database.plateau_detected():
                        logger.warning("Coverage plateau detected — consider switching strategy")
                except DatabaseError as exc:
                    logger.warning("DB record failed: %s", exc)

            return metrics.to_ava_dict()

    @property
    def cold_paths(self) -> Dict[str, List[Dict[str, Any]]]:
        """Thread-safe snapshot of cold-path detail for CoverageDirector."""
        with self._lock:
            if self._metrics is None:
                return {"lines": [], "branches": [], "toggles": []}
            return {
                "lines":    list(self._metrics.cold_lines),
                "branches": list(self._metrics.cold_branches),
                "toggles":  list(self._metrics.cold_toggles),
            }

    @property
    def metrics(self) -> Optional[CoverageMetrics]:
        with self._lock:
            return self._metrics

    @property
    def functional_coverage_detail(self) -> Dict[str, Dict[str, Any]]:
        return self._func_model.compute()

    # ── Internal ───────────────────────────────────────────────────────────

    def _resolve_metrics(self, rtl_results: Dict[str, Any]) -> CoverageMetrics:
        # 1. Real .dat
        try:
            m = extract_coverage_from_run(
                self._run_dir,
                dat_filename=self._dat_filename,
                run_id=str(rtl_results.get("seed", "")),
            )
            logger.info(
                "Coverage from .dat: line=%.2f%% branch=%.2f%% "
                "toggle=%.2f%% functional=%.2f%%",
                m.line, m.branch, m.toggle, m.functional,
            )
            return m
        except ParseError as exc:
            logger.warning("No .dat available: %s", exc)

        # 2. Legacy dict
        cov_data = rtl_results.get("coverage_data")
        if cov_data and isinstance(cov_data, dict) and any(cov_data.values()):
            logger.info("Falling back to rtl_results.coverage_data")
            return self._metrics_from_dict(cov_data)

        # 3. Zeros
        if not self._fallback:
            raise BackendError(
                "No coverage data and fallback_on_missing=False. "
                "Wire Verilator --coverage into _simulate_rtl."
            )
        logger.error(
            "No coverage data found — all metrics are 0.0. "
            "Wire Verilator --coverage into _simulate_rtl."
        )
        return CoverageMetrics(
            generated_at=datetime.now(timezone.utc).isoformat(),
            source_file="NO_DATA",
        )

    @staticmethod
    def _metrics_from_dict(cov_data: Dict[str, Any]) -> CoverageMetrics:
        lines_hit      = int(cov_data.get("lines_hit", 0))
        total_lines    = max(int(cov_data.get("total_lines", 1)), 1)
        branches_hit   = int(cov_data.get("branches_hit", 0))
        total_branches = max(int(cov_data.get("total_branches", 1)), 1)
        toggle_pct     = float(cov_data.get("toggle_pct", 0.0))
        line_pct       = round(100.0 * lines_hit / total_lines, 4)
        branch_pct     = round(100.0 * branches_hit / total_branches, 4)
        functional     = round(0.35*line_pct + 0.35*branch_pct + 0.20*toggle_pct, 4)
        return CoverageMetrics(
            line=min(line_pct, 100.0), branch=min(branch_pct, 100.0),
            toggle=min(toggle_pct, 100.0), functional=min(functional, 100.0),
            lines_hit=lines_hit, lines_total=total_lines,
            branches_hit=branches_hit, branches_total=total_branches,
            generated_at=datetime.now(timezone.utc).isoformat(),
            source_file="legacy_dict",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Commit-log helpers (imported by ava_patched.py)
# ═══════════════════════════════════════════════════════════════════════════════

_SPIKE_COMMIT_RE = re.compile(
    r"core\s+\d+:\s+(?P<pc>0x[0-9a-fA-F]+)\s+\((?P<instr>0x[0-9a-fA-F]+)\)"
    r"(?:\s+(?P<rd>[xf]\d+|pc)\s+(?P<rd_val>0x[0-9a-fA-F]+))?",
    re.ASCII,
)
_DUT_COMMIT_RE = re.compile(
    r"COMMIT\s+pc=(?P<pc>0x[0-9a-fA-F]+)\s+instr=(?P<instr>0x[0-9a-fA-F]+)"
    r"(?:\s+rd=(?P<rd>[xf]\d+)\s+val=(?P<rd_val>0x[0-9a-fA-F]+))?",
    re.ASCII,
)
_CYCLE_RE   = re.compile(r"(\d+)\s*cycles?",         re.IGNORECASE)
_INSTRET_RE = re.compile(r"(\d+)\s*inst(?:ret|ructions?)?", re.IGNORECASE)


def parse_spike_commit_log(stdout: str) -> List[Dict[str, Any]]:
    """Parse Spike --log-commits output into list of {pc, instr, rd, rd_val} dicts."""
    entries: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        m = _SPIKE_COMMIT_RE.search(line)
        if m:
            entries.append({
                "pc":     m.group("pc"),
                "instr":  m.group("instr"),
                "rd":     m.group("rd")     or "",
                "rd_val": m.group("rd_val") or "",
            })
    return entries


def parse_dut_commit_log(stdout: str) -> List[Dict[str, Any]]:
    """Parse DUT COMMIT log lines (adjust _DUT_COMMIT_RE for your DUT)."""
    entries: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        m = _DUT_COMMIT_RE.search(line)
        if m:
            entries.append({
                "pc":     m.group("pc"),
                "instr":  m.group("instr"),
                "rd":     m.group("rd")     or "",
                "rd_val": m.group("rd_val") or "",
            })
    return entries


def count_cycles_instrets(stdout: str) -> Tuple[int, int]:
    """Extract (cycles, instrets) from simulation stdout."""
    cycles = instrets = 0
    for line in stdout.splitlines():
        if not cycles:
            m = _CYCLE_RE.search(line)
            if m:
                cycles = int(m.group(1))
        if not instrets:
            m = _INSTRET_RE.search(line)
            if m:
                instrets = int(m.group(1))
    return cycles, instrets


# Backward-compatibility aliases
_parse_commit_log    = parse_spike_commit_log
_count_cycles_instrs = count_cycles_instrets


# ═══════════════════════════════════════════════════════════════════════════════
# 12. AVA manifest / cross-agent contract utilities
# ═══════════════════════════════════════════════════════════════════════════════

def atomic_write(path: Union[str, Path], content: str, encoding: str = "utf-8") -> None:
    """
    Write *content* to *path* atomically.

    Uses a unique per-call temporary file (``<path>.<pid>.<tid>.tmp``) so that
    concurrent writers never overwrite each other's in-progress temp file.
    On POSIX, ``Path.replace()`` is atomic.  On Windows it is atomic since
    Python 3.3 (uses MoveFileExW with MOVEFILE_REPLACE_EXISTING).

    Parameters
    ----------
    path     : final destination path (parent directory created if absent)
    content  : text to write
    encoding : text encoding (default utf-8)
    """
    import os
    import threading as _threading

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Unique suffix prevents races between concurrent writers for the same path
    unique = f"{os.getpid()}.{_threading.get_ident()}"
    tmp = path.with_name(f"{path.name}.{unique}.tmp")

    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(path)          # atomic on POSIX; atomic-ish on Win32
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def format_ava_schema(
    ava_dict: Dict[str, float],
    *,
    raw_counts: Optional[Dict[str, Dict[str, int]]] = None,
    industrial_grade: bool = False,
    run_id: str = "",
    generated_at: str = "",
    source_file: str = "",
) -> Dict[str, Any]:
    """
    Format coverage data into the canonical AVA inter-agent schema v3.0.

    This schema is the single source of truth consumed by:
      * Agent D comparator  (reads ``overall.pct``)
      * Agent H red team    (reads ``cold_paths``, ``plateau_detected``)
      * Agent G test gen    (reads ``top_cold_paths``)
      * manifest_lock.py    (validates required fields)

    The schema matches ``schemas/run_manifest.schema.json``.
    """
    total_hit   = sum(v.get("hit",   0) for v in (raw_counts or {}).values())
    total_pts   = sum(v.get("total", 0) for v in (raw_counts or {}).values())
    overall_pct = round(ava_dict.get("functional", 0.0), 2)

    return {
        "schemaversion":   "3.0.0",
        "generated_at":    generated_at or datetime.now(timezone.utc).isoformat(),
        "run_id":          run_id,
        "source_file":     source_file,
        "overall": {
            "hit":   total_hit,
            "total": total_pts,
            "pct":   overall_pct,
        },
        "metrics": {
            "line":       round(ava_dict.get("line",       0.0), 2),
            "branch":     round(ava_dict.get("branch",     0.0), 2),
            "toggle":     round(ava_dict.get("toggle",     0.0), 2),
            "expression": round(ava_dict.get("expression", 0.0), 2),
            "functional": overall_pct,
        },
        "raw_counts":      raw_counts or {},
        "industrial_grade":industrial_grade,
        "plateau_detected":False,      # filled by caller after DB query
        "top_cold_paths":  [],         # filled by caller after ColdPathRanker
        "cold_paths": {
            "lines":    [],
            "branches": [],
            "toggles":  [],
        },
    }


def load_manifest(manifest_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load and minimally validate an AVA manifest.json.

    Raises ManifestError with a descriptive message on any problem so the
    caller can exit with EXIT_CONFIG_ERROR.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise ManifestError(f"Manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Manifest JSON parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("Manifest root must be a JSON object")
    if "rundir" not in data:
        raise ManifestError("Manifest missing required field: 'rundir'")
    return data


def update_manifest(
    manifest_path: Union[str, Path],
    updates: Dict[str, Any],
) -> None:
    """
    Apply dot-notation key updates to a manifest and write atomically.

    Examples
    --------
    update_manifest(p, {"phases.coverage.status": "completed",
                        "metrics.coveragepct": 87.5})
    """
    manifest_path = Path(manifest_path)
    data = load_manifest(manifest_path)

    for dotkey, value in updates.items():
        parts = dotkey.split(".")
        node  = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    atomic_write(manifest_path, json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("Manifest updated: %s (%d fields)", manifest_path.name, len(updates))


def _run_manifest_mode(args: "argparse.Namespace") -> int:
    """
    AVA contract execution mode.

    Reads coverage.dat location from manifest, runs the pipeline, and
    writes back to the manifest so downstream agents can consume results.

    Contract
    --------
    Input  : manifest["rundir"] / "outputs" / "coverageraw" / "coverage.dat"
    Outputs:
      <rundir>/coveragesummary.json           (AVA inter-agent schema)
      <rundir>/coverage_report.json           (full legacy report)
      <manifest> fields updated:
        phases.coverage.status     = "completed"
        phases.coverage.duration   = <float seconds>
        outputs.coveragesummary    = "coveragesummary.json"
        metrics.coveragepct        = <float>
        metrics.coverage_plateau   = <bool>

    Exit codes
    ----------
    EXIT_SUCCESS (0)      pipeline ran cleanly
    EXIT_PARSE_ERROR (1)  .dat parse / coverage calculation failed
    EXIT_CONFIG_ERROR (3) manifest / config problem
    """
    import time as _time
    t_start = _time.monotonic()

    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        logger.error("Manifest error: %s", exc)
        return EXIT_CONFIG_ERROR

    rundir = Path(manifest["rundir"])
    dat_path = rundir / "outputs" / "coverageraw" / "coverage.dat"

    if not dat_path.exists():
        logger.error(
            "coverage.dat not found at contract location: %s\n"
            "Ensure the simulation phase wrote to outputs.coverageraw/coverage.dat",
            dat_path,
        )
        return EXIT_CONFIG_ERROR

    run_id = args.run_id or manifest.get("run_id", "")

    # Parse
    cov_parser = VerilatorCoverageParser()
    try:
        points  = cov_parser.parse_dat_file(dat_path)
        metrics = cov_parser.aggregate(points, run_id=run_id)
        metrics.source_file = str(dat_path)
    except ParseError as exc:
        logger.error("Coverage parse failed: %s", exc)
        return EXIT_PARSE_ERROR

    # Database (plateau + trend)
    plateau = False
    top_cold: List[Dict[str, Any]] = []
    db_path  = args.db or rundir / "coverage_trend.sqlite"
    db: Optional[CoverageDatabase] = None
    try:
        db = CoverageDatabase(str(db_path))
        db.record(metrics, run_id=run_id)
        plateau = db.plateau_detected()
        alert   = db.regression_alert()
        if alert:
            logger.warning(alert)
    except (DatabaseError, Exception) as exc:
        logger.warning("DB unavailable (%s) — plateau detection skipped", exc)

    # Try to get ranked cold paths from coverage_database if available
    try:
        from coverage_database import CoverageDatabase as ExtDB  # type: ignore[import]
        ext_db = ExtDB(db_path)
        raw_cold = ext_db.top_cold_paths(20)
        top_cold = [
            {
                "module":             cp.module,
                "line":               cp.line,
                "type":               cp.type,
                "description":        cp.description,
                "reachability_score": cp.reachability_score,
            }
            for cp in raw_cold
        ]
    except Exception:
        # Fall back to inline cold paths from metrics
        top_cold = metrics.cold_lines[:20] + metrics.cold_branches[:10]

    # Write reports
    reporter = CoverageReporter(metrics)
    reporter.write_json(rundir)                        # coverage_report.json (legacy)
    summary_path = reporter.write_ava_summary(         # coveragesummary.json (contract)
        rundir,
        plateau=plateau,
        top_cold=top_cold,
    )

    duration = round(_time.monotonic() - t_start, 3)

    # Update manifest (atomic)
    try:
        update_manifest(args.manifest, {
            "phases.coverage.status":   "completed",
            "phases.coverage.duration": duration,
            "outputs.coveragesummary":  "coveragesummary.json",
            "metrics.coveragepct":      round(metrics.functional, 2),
            "metrics.coverage_plateau": plateau,
        })
    except (ManifestError, OSError) as exc:
        logger.error("Manifest update failed: %s", exc)
        return EXIT_CONFIG_ERROR
    finally:
        if db:
            db.close()

    _print_summary(metrics)
    logger.info(
        "Manifest mode complete in %.3fs | summary=%s | plateau=%s",
        duration, summary_path, plateau,
    )
    return EXIT_SUCCESS

# ═══════════════════════════════════════════════════════════════════════════════
# 13. CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coverage_pipeline",
        description="Agent F — Verilator coverage pipeline for AVA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes
-----
  Direct mode  : --dat / --dat-dir  (standalone; no manifest needed)
  Manifest mode: --manifest          (AVA contract; reads/writes manifest.json)

Examples
--------
  # Direct: parse a single .dat
  python coverage_pipeline.py --dat obj_dir/coverage.dat

  # Direct: multi-format output
  python coverage_pipeline.py --dat coverage.dat --out run/ --formats json html csv

  # Direct: merge a directory of .dat files
  python coverage_pipeline.py --dat-dir obj_dir/ --out run/

  # Direct: trend database
  python coverage_pipeline.py --dat coverage.dat --db trend.sqlite --run-id seed_42

  # Manifest mode (AVA contract)
  python coverage_pipeline.py --manifest run/manifest.json

Exit codes
----------
  0  success (direct) / pipeline completed cleanly (manifest)
  1  parse / calculation error
  3  manifest / config error (missing file, bad schema)
""",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dat",      metavar="FILE", help="Verilator coverage.dat (direct mode)")
    src.add_argument("--dat-dir",  metavar="DIR",  help="Directory of *.dat files (direct mode)")
    src.add_argument("--manifest", metavar="FILE", type=Path,
                     help="AVA manifest.json (contract mode)")
    p.add_argument("--annotate",      metavar="DIR",  help="verilator_coverage --annotate dir")
    p.add_argument("--write-merged",  metavar="FILE", help="Write merged .dat here")
    p.add_argument("--out",           metavar="DIR",  default=".", help="Output dir (default: .)")
    p.add_argument("--formats",  nargs="+", choices=["json","csv","html"],
                   default=["json"], metavar="FMT")
    p.add_argument("--db",            metavar="FILE", help="SQLite trend DB path")
    p.add_argument("--verilator-coverage-bin", default="verilator_coverage", metavar="BIN")
    p.add_argument("--run-id",        default="", metavar="ID")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def _print_summary(metrics: CoverageMetrics) -> None:
    W = 50
    print("\n" + "─" * 70)
    print("  Coverage Summary")
    print("─" * 70)
    for k, v in metrics.to_ava_dict().items():
        bar_w  = int(v / 2)
        bar    = "█" * bar_w + "░" * (W - bar_w)
        colour = "\033[92m" if v >= 90 else "\033[93m" if v >= 70 else "\033[91m"
        reset  = "\033[0m"
        star   = " ★" if k == "functional" else ""
        print(f"  {k:<12}{colour}{v:>7.2f}%{reset}  [{bar}]{star}")
    print()
    print(f"  Line       : {metrics.lines_hit:>6} / {metrics.lines_total:<6} points hit")
    print(f"  Branch     : {metrics.branches_hit:>6} / {metrics.branches_total:<6} arms hit")
    print(f"  Toggle     : {metrics.toggles_hit:>6} / {metrics.toggles_total:<6} transitions")
    print(f"  Expression : {metrics.expressions_hit:>6} / {metrics.expressions_total:<6} terms")
    print()
    print(f"  Cold lines   : {len(metrics.cold_lines)}")
    print(f"  Cold branches: {len(metrics.cold_branches)}")
    print(f"  Cold toggles : {len(metrics.cold_toggles)}")
    grade = "✔ INDUSTRIAL GRADE" if metrics.is_industrial_grade() else "✘ Below threshold"
    print(f"\n  Grade: {grade}")
    print("─" * 70 + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    p = _build_cli()
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    # ── Manifest mode (AVA contract) ──────────────────────────────────────
    if args.manifest:
        return _run_manifest_mode(args)

    # ── Direct mode ───────────────────────────────────────────────────────
    if args.annotate and args.dat:
        try:
            rc, _, stderr = run_verilator_coverage(
                args.dat,
                annotate_dir=args.annotate,
                write_merged=args.write_merged,
                verilator_coverage_bin=args.verilator_coverage_bin,
            )
            if rc != 0:
                logger.warning("verilator_coverage rc=%d: %s", rc, stderr[:500])
        except BackendError as exc:
            logger.warning("verilator_coverage unavailable: %s", exc)

    parser = VerilatorCoverageParser()
    try:
        if args.dat_dir:
            points = parser.parse_dat_directory(args.dat_dir)
        else:
            points = parser.parse_dat_file(args.dat)
    except ParseError as exc:
        logger.error("Parse failed: %s", exc)
        return EXIT_PARSE_ERROR

    metrics = parser.aggregate(points, run_id=args.run_id)
    metrics.source_file = args.dat or args.dat_dir or ""

    plateau = False
    db: Optional[CoverageDatabase] = None
    if args.db:
        try:
            db = CoverageDatabase(args.db)
            db.record(metrics)
            alert = db.regression_alert()
            if alert:
                print(f"\n\033[91m{alert}\033[0m")
            plateau = db.plateau_detected()
            if plateau:
                print("\n\033[93mWARN: Coverage plateau — consider switching strategy\033[0m")
        except DatabaseError as exc:
            logger.warning("DB error: %s", exc)

    paths = save_coverage_report(
        metrics,
        Path(args.out),
        formats=args.formats,
        ava_summary=True,    # always emit coveragesummary.json in direct mode too
        plateau=plateau,
    )
    _print_summary(metrics)
    for fmt, path in paths.items():
        print(f"  [{fmt.upper():12}] {path}")
    print()

    if db:
        db.close()

    return EXIT_SUCCESS if metrics.is_industrial_grade() else EXIT_PARSE_ERROR


if __name__ == "__main__":
    sys.exit(main())

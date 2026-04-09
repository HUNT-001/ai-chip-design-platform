#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_commitlogs.py — Agent D: Comparator & Triage  (SOTA v2)
================================================================
Stream-compares two RISC-V commit logs (RTL vs ISS/golden), classifies
every divergence, and emits structured artefacts consumed by the AVA
verification pipeline.

Design goals
------------
* Zero RAM explosion: pure iterator pipeline; only a bounded context
  window and collected mismatches ever live in memory.
* Transparent compression: auto-detects .gz / .bz2 / .lzma / .xz.
* Robust ingestion: UTF-8 BOM stripping, blank/comment-line skip,
  configurable tolerance for malformed JSON lines.
* Step continuity checks: duplicate steps, gaps, and out-of-order
  steps are reported separately before field comparison.
* RISC-V semantic checks: x0 hardwired-zero invariant enforced on
  every commit for both RTL and ISS independently.
* XLEN-aware hex comparison: values masked to 32 or 64 bits before
  numerical comparison so 0xffffffff == -1 for RV32.
* CSR write order normalisation: sorted by address before compare.
* Configurable skip rules and bit-masks per CSR address.
* Multiple output formats: JSON bug-report, JUnit XML, Markdown, SARIF.
* Self-test suite: --self-test runs all built-in regression cases.
* Batch mode: YAML/JSON manifest for multiple log pairs.
* AVA VerificationResult.bugs drop-in: CompareResult.bugs returns a
  list of strings in the expected format.

Commit-log schema (JSONL, one object per line)
----------------------------------------------
Required:
  step      int          monotone commit index
  pc        hex-string   "0x00001000"
  instr     hex-string   "0x00a50533"
  trap      bool

Optional:
  disasm    string
  rd        int          destination GPR index (0-31)
  rd_val    hex-string
  rs1/rs2   int
  rs1_val/rs2_val  hex-string
  mem_addr  hex-string
  mem_val   hex-string
  mem_op    "load"|"store"|"amo"
  mem_size  int          bytes: 1|2|4|8
  csr_writes  [{csr: hex-string, val: hex-string}, ...]
  trap_cause  hex-string
  trap_tval   hex-string
  trap_pc     hex-string
  privilege   "M"|"S"|"U"

Exit codes: 0=PASS  1=MISMATCH  2=ERROR
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import io
import json
import logging
import lzma
import os
import re
import shlex
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import (
    Any, Deque, Dict, FrozenSet, Iterator,
    List, Optional, Set, Tuple,
)

import queue
import threading

__version__ = "3.0.0"
__all__ = [
    "compare_logs",
    "compare_logs_batch",
    "CommitEntry",
    "Mismatch",
    "CompareResult",
    "CompareConfig",
    "MismatchType",
    "Severity",
    "ComparatorError",
    "LogFormatError",
    "ParseError",
    "ConfigError",
    # AVA exit-code constants
    "EXIT_PASS",
    "EXIT_MISMATCH",
    "EXIT_INFRA",
    "EXIT_CONFIG",
]

_log = logging.getLogger(__name__)

# ── AVA-standard exit codes ────────────────────────────────────────────────────
EXIT_PASS     = 0   # logs identical within comparison rules
EXIT_MISMATCH = 1   # at least one logical divergence found
EXIT_INFRA    = 2   # infrastructure error (I/O, parse, thread)
EXIT_CONFIG   = 3   # configuration / manifest error (bad paths, missing fields)


# ═══════════════════════════════════════════════════════════════════════════════
# Enumerations
# ═══════════════════════════════════════════════════════════════════════════════

class MismatchType(str, Enum):
    """Complete AVA error-code taxonomy (11 canonical codes + extras).

    The *value* of each member is the exact string the AVA orchestrator and
    downstream agents expect in bug_report.json.  Python attribute names use
    underscores for readability; canonical wire-format strings are camelCase-
    free per the AVA contract.

    AVA-required canonical codes (11)
    ----------------------------------
    PCMISMATCH, REGMISMATCH, CSRMISMATCH, MEMMISMATCH, TRAPMISMATCH,
    LENGTHMISMATCH, SEQGAP, X0WRITTEN, ALIGNMENTERROR, SCHEMAINVALID,
    BINARYHASHMISMATCH

    Additional codes (comparator-internal)
    ----------------------------------------
    INSTRMISMATCH, TRAPCAUSEMISMATCH, PRIVILEGEMISMATCH, PARSEERROR
    """
    # ── AVA canonical ─────────────────────────────────────────────────────────
    PC_MISMATCH           = "PCMISMATCH"
    REG_MISMATCH          = "REGMISMATCH"
    CSR_MISMATCH          = "CSRMISMATCH"
    MEM_MISMATCH          = "MEMMISMATCH"
    TRAP_MISMATCH         = "TRAPMISMATCH"
    LENGTH_MISMATCH       = "LENGTHMISMATCH"    # log has fewer commits than peer
    SEQ_GAP               = "SEQGAP"            # step-number discontinuity
    X0_WRITTEN            = "X0WRITTEN"         # x0 hardwired-zero violated
    ALIGN_ERROR           = "ALIGNMENTERROR"    # unaligned memory access
    SCHEMA_INVALID        = "SCHEMAINVALID"     # commit-log record fails schema
    BINARY_HASH_MISMATCH  = "BINARYHASHMISMATCH"# binary SHA-256 mismatch vs manifest

    # ── Comparator-internal (not in AVA-11 but useful) ────────────────────────
    INSTR_MISMATCH        = "INSTRMISMATCH"     # instruction word divergence
    TRAP_CAUSE_MISMATCH   = "TRAPCAUSEMISMATCH" # mcause/mepc/mtval differ
    PRIVILEGE_MISMATCH    = "PRIVILEGEMISMATCH" # M/S/U mode differs
    PARSE_WARNING         = "PARSEERROR"        # malformed / skipped line

    # ── Backward-compatible aliases (same value → Python alias, not new member)
    # These exist so callers using the old underscore API still resolve correctly.
    LOG_LENGTH         = "LENGTHMISMATCH"   # alias for LENGTH_MISMATCH
    STEP_DISCONTINUITY = "SEQGAP"           # alias for SEQ_GAP
    X0_INVARIANT       = "X0WRITTEN"        # alias for X0_WRITTEN

    @property
    def human(self) -> str:
        return _MISMATCH_HUMAN.get(self.value, self.value)


_MISMATCH_HUMAN: Dict[str, str] = {
    "PCMISMATCH":          "Program counter divergence",
    "REGMISMATCH":         "General-purpose register write-back mismatch",
    "CSRMISMATCH":         "CSR write value mismatch",
    "MEMMISMATCH":         "Memory address or value mismatch",
    "TRAPMISMATCH":        "Trap/exception signalling mismatch",
    "LENGTHMISMATCH":      "Logs have different commit counts",
    "SEQGAP":              "Step-number sequence discontinuity (gap/duplicate/reorder)",
    "X0WRITTEN":           "x0 hardwired-zero invariant violated",
    "ALIGNMENTERROR":      "Unaligned memory access detected",
    "SCHEMAINVALID":       "Commit-log record fails required schema",
    "BINARYHASHMISMATCH":  "Binary SHA-256 mismatch vs manifest expected hash",
    "INSTRMISMATCH":       "Instruction word divergence (fetch/decode error)",
    "TRAPCAUSEMISMATCH":   "Trap taken but mcause/mepc/mtval differ",
    "PRIVILEGEMISMATCH":   "Privilege mode (M/S/U) mismatch at commit",
    "PARSEERROR":          "Malformed or skipped commit-log line",
}


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    ERROR    = "ERROR"
    WARNING  = "WARNING"
    INFO     = "INFO"


_SEVERITY_MAP: Dict[str, Severity] = {
    "PCMISMATCH":         Severity.CRITICAL,
    "LENGTHMISMATCH":     Severity.CRITICAL,
    "SEQGAP":             Severity.CRITICAL,
    "SCHEMAINVALID":      Severity.CRITICAL,
    "BINARYHASHMISMATCH": Severity.CRITICAL,
    "INSTRMISMATCH":      Severity.ERROR,
    "TRAPMISMATCH":       Severity.ERROR,
    "TRAPCAUSEMISMATCH":  Severity.ERROR,
    "X0WRITTEN":          Severity.ERROR,
    "ALIGNMENTERROR":     Severity.ERROR,
    "REGMISMATCH":        Severity.WARNING,
    "CSRMISMATCH":        Severity.WARNING,
    "MEMMISMATCH":        Severity.WARNING,
    "PRIVILEGEMISMATCH":  Severity.WARNING,
    "PARSEERROR":         Severity.INFO,
}

def _severity_for(mt: MismatchType) -> Severity:
    """Look up severity by canonical wire-value (alias-safe)."""
    return _SEVERITY_MAP.get(mt.value, Severity.WARNING)

_SARIF_LEVEL: Dict[Severity, str] = {
    Severity.CRITICAL: "error",
    Severity.ERROR:    "error",
    Severity.WARNING:  "warning",
    Severity.INFO:     "note",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════════

class ComparatorError(Exception):
    """Base class for all comparator errors."""


class LogFormatError(ComparatorError):
    """Log file cannot be opened or has an unsupported format."""


class ParseError(ComparatorError):
    """Hard JSON parse failure when max_parse_errors=0."""
    def __init__(self, path: str, lineno: int, line: str, cause: Exception) -> None:
        self.path   = path
        self.lineno = lineno
        self.line   = line
        self.cause  = cause
        super().__init__(f"{path}:{lineno}: {cause}  — {line[:80]!r}")


class StepError(ComparatorError):
    """Step-number discontinuity in strict mode."""


class ConfigError(ComparatorError):
    """Bad manifest, missing required field, or incompatible configuration.

    Catching this exception should cause the caller to exit with EXIT_CONFIG (3).
    """
    def __init__(self, message: str, field: Optional[str] = None) -> None:
        self.field = field
        super().__init__(message)


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CompareConfig:
    """All comparison-control knobs.

    Can be built programmatically or deserialised from a YAML/JSON config::

        cfg = CompareConfig.from_dict(yaml.safe_load(open("cfg.yaml")))
    """

    # ── RISC-V parameters ────────────────────────────────────────────────────
    xlen: int = 32
    """Word width: 32 or 64.  Values are masked to this width."""

    extensions: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"I", "M"})
    )

    # ── Comparison rules ─────────────────────────────────────────────────────
    skip_fields: FrozenSet[str] = field(default_factory=frozenset)
    """Field names to exclude.  Valid names: pc instr rd rd_val csr_writes
    mem_addr mem_val mem_op trap trap_cause trap_tval trap_pc privilege."""

    csr_masks: Dict[int, int] = field(default_factory=dict)
    """Per-CSR address bit-masks.  Only masked bits are compared.
    Example: {0x300: 0x88} compares only MIE/MPIE bits of mstatus."""

    ignored_csrs: FrozenSet[int] = field(default_factory=frozenset)
    """CSR addresses never compared (e.g. cycle/time/instret: 0xC00)."""

    enforce_x0_invariant: bool = True
    """Flag any commit where rd==0 but rd_val != 0 on either side."""

    csr_write_order_sensitive: bool = False
    """If False, CSR writes are sorted by address before comparison."""

    # ── Tolerance ────────────────────────────────────────────────────────────
    max_parse_errors: int = 10
    """Malformed JSON lines tolerated before aborting.  0 = zero tolerance."""

    max_mismatches: int = 1
    """Stop after N mismatches.  0 = unlimited.  Overridden by stop_on_first."""

    stop_on_first: bool = True
    """Shorthand for max_mismatches=1."""

    strict_steps: bool = False
    """Raise StepError on any step-number discontinuity."""

    check_alignment: bool = True
    """Emit ALIGNMENTERROR when a memory access address is not naturally aligned
    to its access size (e.g. a 4-byte load at an odd address)."""

    expected_rtl_sha256: Optional[str] = None
    """If set, compare against _sha256(rtl_path) and emit BINARYHASHMISMATCH
    on mismatch.  Allows the manifest to pin a known-good log checksum."""

    expected_iss_sha256: Optional[str] = None
    """Same as expected_rtl_sha256 but for the ISS log."""

    # ── Context ───────────────────────────────────────────────────────────────
    window: int = 32
    """Prior commits to attach as context in each mismatch record."""

    # ── Progress ─────────────────────────────────────────────────────────────
    progress_every: int = 100_000
    """Log a progress message every N steps (0 = disabled)."""

    def __post_init__(self) -> None:
        if self.xlen not in (32, 64):
            raise ValueError(f"xlen must be 32 or 64, got {self.xlen!r}")
        if self.window < 0:
            raise ValueError(f"window must be >= 0, got {self.window!r}")
        if self.stop_on_first:
            self.max_mismatches = 1

    @property
    def value_mask(self) -> int:
        return (1 << self.xlen) - 1

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CompareConfig":
        d = dict(raw)
        if "extensions" in d:
            d["extensions"] = frozenset(d["extensions"])
        if "skip_fields" in d:
            d["skip_fields"] = frozenset(d["skip_fields"])
        if "ignored_csrs" in d:
            d["ignored_csrs"] = frozenset(
                int(x, 16) if isinstance(x, str) else int(x)
                for x in d["ignored_csrs"]
            )
        if "csr_masks" in d:
            d["csr_masks"] = {
                (int(k, 16) if isinstance(k, str) else int(k)):
                (int(v, 16) if isinstance(v, str) else int(v))
                for k, v in d["csr_masks"].items()
            }
        valid_keys = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        d = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**d)


# ═══════════════════════════════════════════════════════════════════════════════
# Hex utilities
# ═══════════════════════════════════════════════════════════════════════════════

_HEX_RE = re.compile(r"^(0[xX])?[0-9A-Fa-f]+$")


def _parse_hex(value: Any, field_name: str = "?") -> int:
    """Parse a hex string or integer.  Raises ValueError on failure."""
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        raise ValueError(f"Field {field_name!r} is empty")
    if not _HEX_RE.match(s):
        raise ValueError(
            f"Field {field_name!r} has non-hex value {s!r}; "
            "expected e.g. '0x1000' or '1000'"
        )
    return int(s, 16)


def _parse_hex_opt(value: Any, field_name: str = "?") -> Optional[int]:
    return None if value is None else _parse_hex(value, field_name)


def _fmt_hex(value: Optional[int], width: int = 8) -> Optional[str]:
    return None if value is None else f"0x{value:0{width}x}"


# ═══════════════════════════════════════════════════════════════════════════════
# Delta-based shadow state for context window
# ═══════════════════════════════════════════════════════════════════════════════
# Instead of storing the full to_dict() of every commit in the sliding window
# (which copies all optional fields even when unchanged), we store only the
# fields that *changed* from the previous commit.  On mismatch we reconstruct
# the full context by forward-replaying deltas from a base snapshot.
#
# Memory saving: a typical RV32IM commit writes 1 register + maybe 1 CSR.
# A full to_dict() is ~25 keys; a delta is 4-5 keys.  For a window of 32 the
# saving is ~5×: ~40 KB → ~8 KB per mismatch event on a 1M-instruction trace.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _DeltaEntry:
    """One commit's delta vs the previous commit in the window."""
    step:   int
    pc:     int
    instr:  int
    # changed fields only (None means "same as previous commit")
    rd:      Optional[int]  = None
    rd_val:  Optional[int]  = None
    mem_op:  Optional[str]  = None
    mem_addr:Optional[int]  = None
    mem_val: Optional[int]  = None
    trap:    Optional[bool] = None
    disasm:  Optional[str]  = None
    # Always stored (cheap, needed for context display)
    raw_csr_writes: Optional[str] = None   # JSON string, only when CSRs changed


class _DeltaWindow:
    """Fixed-depth sliding window that stores deltas, not full dicts.

    ``push(entry)`` is O(1).  ``snapshot()`` reconstructs the full list of
    dicts (for attaching to a Mismatch) in O(window) time.
    """

    def __init__(self, maxlen: int) -> None:
        self._maxlen  = max(maxlen, 0)
        self._ring:   Deque[_DeltaEntry] = deque(maxlen=self._maxlen)
        self._prev:   Optional[CommitEntry] = None   # type: ignore[name-defined]

    def push(self, entry: "CommitEntry") -> None:  # type: ignore[name-defined]
        if self._maxlen == 0:
            return
        p = self._prev
        delta = _DeltaEntry(
            step    = entry.step,
            pc      = entry.pc,
            instr   = entry.instr,
            rd      = entry.rd      if (p is None or entry.rd      != p.rd)      else None,
            rd_val  = entry.rd_val  if (p is None or entry.rd_val  != p.rd_val)  else None,
            mem_op  = entry.mem_op  if (p is None or entry.mem_op  != p.mem_op)  else None,
            mem_addr= entry.mem_addr if (p is None or entry.mem_addr!= p.mem_addr) else None,
            mem_val = entry.mem_val  if (p is None or entry.mem_val != p.mem_val)  else None,
            trap    = entry.trap    if (p is None or entry.trap    != p.trap)    else None,
            disasm  = entry.disasm  if (p is None or entry.disasm  != p.disasm)  else None,
            raw_csr_writes = (
                json.dumps([c.to_dict() for c in entry.csr_writes])
                if (p is None or entry.csr_writes != p.csr_writes)
                else None
            ),
        )
        self._ring.append(delta)
        self._prev = entry

    def snapshot(self) -> List[Dict[str, Any]]:
        """Reconstruct full commit dicts from stored deltas (forward replay)."""
        out: List[Dict[str, Any]] = []
        # Start with zero state
        state: Dict[str, Any] = {
            "rd": None, "rd_val": None, "mem_op": None,
            "mem_addr": None, "mem_val": None, "trap": False,
            "disasm": None, "csr_writes": [],
        }
        for d in self._ring:
            if d.rd       is not None: state["rd"]       = d.rd
            if d.rd_val   is not None: state["rd_val"]   = _fmt_hex(d.rd_val)
            if d.mem_op   is not None: state["mem_op"]   = d.mem_op
            if d.mem_addr is not None: state["mem_addr"] = _fmt_hex(d.mem_addr)
            if d.mem_val  is not None: state["mem_val"]  = _fmt_hex(d.mem_val)
            if d.trap     is not None: state["trap"]     = d.trap
            if d.disasm   is not None: state["disasm"]   = d.disasm
            if d.raw_csr_writes is not None:
                state["csr_writes"] = json.loads(d.raw_csr_writes)
            out.append({
                "step":       d.step,
                "pc":         _fmt_hex(d.pc),
                "instr":      _fmt_hex(d.instr),
                "rd":         state["rd"],
                "rd_val":     state["rd_val"],
                "mem_op":     state["mem_op"],
                "mem_addr":   state["mem_addr"],
                "mem_val":    state["mem_val"],
                "trap":       state["trap"],
                "disasm":     state["disasm"],
                "csr_writes": state["csr_writes"],
            })
        return out


# ═══════════════════════════════════════════════════════════════════════════════
# Core data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CsrWrite:
    csr: int   # CSR address as int
    val: int   # written value (XLEN-masked)

    def to_dict(self) -> Dict[str, str]:
        return {"csr": f"0x{self.csr:03x}", "val": f"0x{self.val:08x}"}

    @staticmethod
    def from_dict(d: Dict[str, Any], xlen: int) -> "CsrWrite":
        mask = (1 << xlen) - 1
        return CsrWrite(
            csr=_parse_hex(d["csr"], "csr.csr"),
            val=_parse_hex(d["val"], "csr.val") & mask,
        )


@dataclass
class CommitEntry:
    """One decoded, XLEN-masked commit-log record.

    All numeric fields are stored as Python ints after normalisation,
    so comparison is pure integer equality — no string juggling.
    """
    # Required
    step:  int
    pc:    int
    instr: int
    trap:  bool

    # Write-back
    rd:      Optional[int] = None
    rd_val:  Optional[int] = None

    # Source registers (context only, never compared by default)
    rs1:     Optional[int] = None
    rs1_val: Optional[int] = None
    rs2:     Optional[int] = None
    rs2_val: Optional[int] = None

    # Memory
    mem_addr: Optional[int] = None
    mem_val:  Optional[int] = None
    mem_op:   Optional[str] = None   # "load" | "store" | "amo"
    mem_size: Optional[int] = None   # bytes

    # CSR side-effects
    csr_writes: List[CsrWrite] = field(default_factory=list)

    # Trap / interrupt
    trap_cause: Optional[int] = None
    trap_tval:  Optional[int] = None
    trap_pc:    Optional[int] = None

    # Privilege
    privilege: Optional[str] = None   # "M" | "S" | "U"

    # Metadata (never compared)
    disasm: Optional[str] = None
    lineno: int           = 0
    raw_pc: str           = ""

    # Set by reader
    x0_violation:    bool = False  # rd==0 but rd_val != 0
    schema_violation: bool = False  # record failed optional-field type checks
    schema_errors:   List[str] = field(default_factory=list)  # human-readable reasons

    @staticmethod
    def from_dict(
        d:      Dict[str, Any],
        *,
        xlen:   int = 32,
        lineno: int = 0,
        source: str = "",
    ) -> "CommitEntry":
        mask = (1 << xlen) - 1

        def _h(k: str) -> Optional[int]:
            v = d.get(k)
            return None if v is None else _parse_hex(v, k) & mask

        # Validate required fields
        for req in ("step", "pc", "instr"):
            if req not in d:
                raise KeyError(
                    f"Required field {req!r} missing at line {lineno}"
                    + (f" in {source!r}" if source else "")
                )

        raw_csrs = d.get("csr_writes") or []
        if not isinstance(raw_csrs, list):
            raise ValueError(f"'csr_writes' must be a list at line {lineno}")
        csr_list = [CsrWrite.from_dict(c, xlen) for c in raw_csrs]

        raw_pc  = str(d["pc"])
        pc_int  = _parse_hex(raw_pc, "pc") & mask
        rd      = d.get("rd")
        rd_val  = _h("rd_val")

        e = CommitEntry(
            step        = int(d["step"]),
            pc          = pc_int,
            instr       = _parse_hex(d["instr"], "instr") & mask,
            trap        = bool(d.get("trap", False)),
            rd          = int(rd) if rd is not None else None,
            rd_val      = rd_val,
            rs1         = (lambda v: int(v) if v is not None else None)(d.get("rs1")),
            rs1_val     = _h("rs1_val"),
            rs2         = (lambda v: int(v) if v is not None else None)(d.get("rs2")),
            rs2_val     = _h("rs2_val"),
            mem_addr    = _h("mem_addr"),
            mem_val     = _h("mem_val"),
            mem_op      = d.get("mem_op"),
            mem_size    = (lambda v: int(v) if v is not None else None)(d.get("mem_size")),
            csr_writes  = csr_list,
            trap_cause  = _h("trap_cause"),
            trap_tval   = _h("trap_tval"),
            trap_pc     = _h("trap_pc"),
            privilege   = d.get("privilege"),
            disasm      = d.get("disasm"),
            lineno      = lineno,
            raw_pc      = raw_pc,
        )
        if e.rd == 0 and e.rd_val is not None and e.rd_val != 0:
            e.x0_violation = True

        # ── Schema validation: optional-field type/range checks ───────────────
        schema_errs: List[str] = []
        if e.rd is not None and not (0 <= e.rd <= 31):
            schema_errs.append(f"rd={e.rd} out of range 0-31")
        if e.rs1 is not None and not (0 <= e.rs1 <= 31):
            schema_errs.append(f"rs1={e.rs1} out of range 0-31")
        if e.rs2 is not None and not (0 <= e.rs2 <= 31):
            schema_errs.append(f"rs2={e.rs2} out of range 0-31")
        if e.mem_op is not None and e.mem_op not in ("load", "store", "amo"):
            schema_errs.append(f"mem_op={e.mem_op!r} not in {{load,store,amo}}")
        if e.mem_size is not None and e.mem_size not in (1, 2, 4, 8):
            schema_errs.append(f"mem_size={e.mem_size} not in {{1,2,4,8}}")
        if e.privilege is not None and e.privilege not in ("M", "S", "U"):
            schema_errs.append(f"privilege={e.privilege!r} not in {{M,S,U}}")
        if e.step < 0:
            schema_errs.append(f"step={e.step} is negative")
        if schema_errs:
            e.schema_violation = True
            e.schema_errors    = schema_errs
        return e

    def to_dict(self) -> Dict[str, Any]:
        def _h(v: Optional[int], w: int = 8) -> Optional[str]:
            return None if v is None else f"0x{v:0{w}x}"
        return {
            "step":       self.step,
            "pc":         _h(self.pc),
            "instr":      _h(self.instr),
            "disasm":     self.disasm,
            "trap":       self.trap,
            "rd":         self.rd,
            "rd_val":     _h(self.rd_val),
            "rs1":        self.rs1,
            "rs1_val":    _h(self.rs1_val),
            "rs2":        self.rs2,
            "rs2_val":    _h(self.rs2_val),
            "mem_addr":   _h(self.mem_addr),
            "mem_val":    _h(self.mem_val),
            "mem_op":     self.mem_op,
            "mem_size":   self.mem_size,
            "csr_writes": [c.to_dict() for c in self.csr_writes],
            "trap_cause": _h(self.trap_cause),
            "trap_tval":  _h(self.trap_tval),
            "trap_pc":    _h(self.trap_pc),
            "privilege":  self.privilege,
            "lineno":     self.lineno,
            "schema_violation": self.schema_violation if self.schema_violation else None,
            "schema_errors":    self.schema_errors    if self.schema_errors    else None,
        }

    def fmt_pc(self)    -> str: return f"0x{self.pc:08x}"
    def fmt_instr(self) -> str: return f"0x{self.instr:08x}"


@dataclass
class Mismatch:
    """One classified divergence event, compatible with AVA VerificationResult.bugs."""
    mismatch_type:   MismatchType
    severity:        Severity
    description:     str
    step:            int
    rtl_entry:       Optional[Dict[str, Any]]
    iss_entry:       Optional[Dict[str, Any]]
    differing_field: Optional[str] = None
    rtl_value:       Optional[str] = None
    iss_value:       Optional[str] = None
    repro_cmd:       Optional[str] = None
    context_window:  List[Dict[str, Any]] = field(default_factory=list)
    elapsed_s:       float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mismatch_type"] = self.mismatch_type.value
        d["severity"]      = self.severity.value
        return d

    def to_ava_bug(self) -> str:
        """AVA VerificationResult.bugs compatible string."""
        return (
            f"[{self.severity.value}] step={self.step} "
            f"{self.mismatch_type.value}: {self.description} "
            f"(rtl={self.rtl_value!r} iss={self.iss_value!r})"
        )

    def to_github_annotation(self, filename: str = "commitlog") -> str:
        level = "error" if self.severity in (Severity.CRITICAL, Severity.ERROR) else "warning"
        return (
            f"::{level} file={filename},line={self.step},"
            f"title={self.mismatch_type.value}::"
            f"{self.description} (rtl={self.rtl_value} iss={self.iss_value})"
        )


@dataclass
class CompareStats:
    total_steps:           int   = 0
    total_mismatches:      int   = 0
    rtl_parse_warnings:    int   = 0
    iss_parse_warnings:    int   = 0
    rtl_x0_violations:     int   = 0
    iss_x0_violations:     int   = 0
    rtl_step_anomalies:    int   = 0
    iss_step_anomalies:    int   = 0
    first_divergence_step: Optional[int] = None
    elapsed_s:             float = 0.0
    mismatch_by_type:      Dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )

    def record(self, m: Mismatch) -> None:
        self.total_mismatches += 1
        self.mismatch_by_type[m.mismatch_type.value] += 1
        if self.first_divergence_step is None:
            self.first_divergence_step = m.step

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mismatch_by_type"] = dict(self.mismatch_by_type)
        return d


@dataclass
class CompareResult:
    """
    Top-level result object.  Import-and-use example for AVA::

        from compare_commitlogs import compare_logs
        result = compare_logs("rtl.commitlog.jsonl", "iss.commitlog.jsonl", seed=42)
        verification_result.bugs.extend(result.bugs)
        if not result.passed:
            Path("bug_report.json").write_text(
                json.dumps(result.to_bug_report(), indent=2))
    """
    passed:     bool
    stats:      CompareStats
    mismatches: List[Mismatch]    = field(default_factory=list)
    rtl_log:    str               = ""
    iss_log:    str               = ""
    rtl_sha256: Optional[str]     = None
    iss_sha256: Optional[str]     = None
    seed:       Optional[int]     = None
    rtl_bin:    Optional[str]     = None
    iss_bin:    Optional[str]     = None
    config:     Optional[Dict]    = None

    @property
    def bugs(self) -> List[str]:
        """Drop-in for AVA VerificationResult.bugs."""
        return [m.to_ava_bug() for m in self.mismatches]

    @property
    def total_steps(self) -> int:
        return self.stats.total_steps

    # ── JSON bug report ───────────────────────────────────────────────────────
    def to_bug_report(self) -> Dict[str, Any]:
        # AVA-contract reprocmd: a single line that re-runs the failing seed
        # through the RTL+ISS pipeline. Format: --seed N --binary PATH --iss PATH
        reprocmd: Optional[str] = None
        if self.mismatches:
            _parts: List[str] = []
            if self.seed is not None:
                _parts.append(f"--seed {self.seed}")
            if self.rtl_bin:
                _parts.append(f"--binary {shlex.quote(self.rtl_bin)}")
            if self.iss_bin:
                _parts.append(f"--iss {shlex.quote(self.iss_bin)}")
            # If no binary info, fall back to comparator repro
            reprocmd = (
                " ".join(_parts) if _parts else self.mismatches[0].repro_cmd
            )

        return {
            "schema_version": "3.0",
            "tool":           f"compare_commitlogs.py v{__version__}",
            "passed":         self.passed,
            "stats":          self.stats.to_dict(),
            "rtl_log":        self.rtl_log,
            "iss_log":        self.iss_log,
            "rtl_sha256":     self.rtl_sha256,
            "iss_sha256":     self.iss_sha256,
            "seed":           self.seed,
            "rtl_bin":        self.rtl_bin,
            "iss_bin":        self.iss_bin,
            # AVA contract: reprocmd = single-line simulator re-run command
            "reprocmd":       reprocmd,
            # Full comparator repro (re-runs just the comparison, not the sim)
            "comparator_repro_cmd": self.mismatches[0].repro_cmd if self.mismatches else None,
            "config":         self.config,
            "mismatches":     [m.to_dict() for m in self.mismatches],
            "ava_bugs":       self.bugs,
        }

    # ── SARIF v2.1 ────────────────────────────────────────────────────────────
    def to_sarif(self) -> Dict[str, Any]:
        rules = [
            {
                "id":   mt.value,
                "name": mt.value.replace("_", " ").title(),
                "shortDescription": {"text": mt.human},
                "defaultConfiguration": {"level": _SARIF_LEVEL[_SEVERITY_MAP[mt]]},
            }
            for mt in MismatchType
        ]
        results_list = []
        for m in self.mismatches:
            results_list.append({
                "ruleId":  m.mismatch_type.value,
                "level":   _SARIF_LEVEL[m.severity],
                "message": {"text": m.description},
                "locations": [{
                    "logicalLocations": [{
                        "name":          (m.rtl_entry or {}).get("pc", "?"),
                        "decoratedName": f"step={m.step}",
                        "kind":          "commitStep",
                    }]
                }],
                "properties": {"rtl_value": m.rtl_value, "iss_value": m.iss_value},
            })
        return {
            "$schema": "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{
                "tool":    {"driver": {
                    "name":    "compare_commitlogs",
                    "version": __version__,
                    "rules":   rules,
                }},
                "results": results_list,
            }],
        }

    # ── JUnit XML ─────────────────────────────────────────────────────────────
    def to_junit_xml(self) -> str:
        suite = ET.Element(
            "testsuite",
            name="commit_log_comparison",
            tests=str(self.stats.total_steps),
            failures=str(self.stats.total_mismatches),
            errors="0",
            time=f"{self.stats.elapsed_s:.3f}",
        )
        if self.passed:
            tc = ET.SubElement(suite, "testcase",
                               name="full_log_comparison",
                               classname="CommitLogComparator",
                               time=f"{self.stats.elapsed_s:.3f}")
            ET.SubElement(tc, "system-out").text = (
                f"PASS — {self.stats.total_steps:,} steps compared"
            )
        else:
            for m in self.mismatches:
                tc = ET.SubElement(suite, "testcase",
                                   name=f"step_{m.step}_{m.mismatch_type.value}",
                                   classname="CommitLogComparator",
                                   time="0")
                fail = ET.SubElement(tc, "failure",
                                     message=m.description,
                                     attrib={"type": m.mismatch_type.value})
                fail.text = (
                    f"step={m.step}\n"
                    f"type={m.mismatch_type.value}\n"
                    f"severity={m.severity.value}\n"
                    f"rtl={m.rtl_value}\n"
                    f"iss={m.iss_value}\n"
                    f"repro: {m.repro_cmd}"
                )
        try:
            ET.indent(suite, space="  ")
        except AttributeError:
            pass   # Python < 3.9 — skip indentation
        return ET.tostring(suite, encoding="unicode", xml_declaration=False)

    # ── Markdown ──────────────────────────────────────────────────────────────
    def to_markdown(self) -> str:
        lines = [
            "# Commit Log Comparison Report",
            "",
            f"**Tool:** compare_commitlogs.py v{__version__}  ",
            f"**RTL log:** `{self.rtl_log}`  ",
            f"**ISS log:** `{self.iss_log}`  ",
            f"**Seed:** `{self.seed}`  ",
            f"**Steps:** {self.stats.total_steps:,}  ",
            f"**Status:** {'✅ PASS' if self.passed else '❌ MISMATCH'}",
            "",
        ]
        if not self.passed:
            lines += [
                "## Mismatches",
                "",
                "| # | Step | Type | Severity | Field | RTL | ISS |",
                "|---|------|------|----------|-------|-----|-----|",
            ]
            for i, m in enumerate(self.mismatches, 1):
                lines.append(
                    f"| {i} | {m.step} | `{m.mismatch_type.value}` "
                    f"| {m.severity.value} "
                    f"| `{m.differing_field or '—'}` "
                    f"| `{m.rtl_value or '—'}` "
                    f"| `{m.iss_value or '—'}` |"
                )
            lines += ["", "## Statistics", "", "| Type | Count |", "|------|-------|"]
            for k, v in sorted(self.stats.mismatch_by_type.items(), key=lambda x: -x[1]):
                lines.append(f"| `{k}` | {v} |")
            if self.mismatches[0].repro_cmd:
                lines += ["", "## Reproduction", "", "```bash",
                           self.mismatches[0].repro_cmd, "```"]
            if self.mismatches and self.mismatches[0].context_window:
                lines += ["", "## Context (last commits before first divergence)", "", "```"]
                for c in self.mismatches[0].context_window[-8:]:
                    lines.append(
                        f"  step={c.get('step','?'):>7}  "
                        f"pc={c.get('pc','?')}  "
                        f"instr={c.get('instr','?')}  "
                        f"{c.get('disasm','')}"
                    )
                lines.append("```")
        return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════════
# Log reader — transparent decompression, BOM, tolerance
# ═══════════════════════════════════════════════════════════════════════════════

def _open_log_file(path: str) -> io.TextIOBase:
    """Open a JSONL commit log with transparent decompression and BOM stripping.

    Supports: .gz  .bz2  .lzma  .xz  (plain text otherwise)
    Also accepts ``"-"`` for stdin.
    Raises LogFormatError on missing file or open failure.
    """
    if path == "-":
        return io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig")
    p = Path(path)
    if not p.exists():
        raise LogFormatError(f"Commit log not found: {path!r}")
    suffix = p.suffix.lower()
    try:
        if suffix == ".gz":
            return gzip.open(str(p), "rt", encoding="utf-8-sig")   # type: ignore[return-value]
        if suffix == ".bz2":
            return bz2.open(str(p), "rt", encoding="utf-8-sig")    # type: ignore[return-value]
        if suffix in (".lzma", ".xz"):
            return lzma.open(str(p), "rt", encoding="utf-8-sig")   # type: ignore[return-value]
        return open(str(p), "r", encoding="utf-8-sig")             # type: ignore[return-value]
    except (OSError, EOFError, Exception) as exc:
        raise LogFormatError(f"Cannot open {path!r}: {exc}") from exc


def _sha256(path: str) -> Optional[str]:
    """Return SHA-256 hex digest of *path*, or None if unavailable."""
    if path == "-":
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _iter_commits(
    fh:               io.TextIOBase,
    *,
    path:             str,
    xlen:             int          = 32,
    max_parse_errors: int          = 10,
    stats:            Optional[CompareStats] = None,
    side:             str          = "?",
) -> Iterator[CommitEntry]:
    """Yield CommitEntry objects from an open JSONL handle.

    Handles:
    * Blank lines and lines starting with # or //
    * Malformed JSON: warn-and-skip up to max_parse_errors, then stop
    * CommitEntry validation failures: same tolerance
    * x0 invariant violations: sets entry.x0_violation, increments stats
    """
    parse_errors = 0
    for lineno, raw in enumerate(fh, 1):
        stripped = raw.strip()
        if not stripped or stripped[0] in ("#", "/"):
            continue

        try:
            d = json.loads(stripped)
        except json.JSONDecodeError as exc:
            parse_errors += 1
            _log.warning("[%s] %s:%d: JSON error: %s", side, path, lineno, exc)
            if stats is not None:
                if side == "RTL":
                    stats.rtl_parse_warnings += 1
                else:
                    stats.iss_parse_warnings += 1
            if max_parse_errors == 0:
                raise ParseError(path, lineno, stripped, exc)
            if parse_errors >= max_parse_errors:
                _log.error("[%s] %s: max_parse_errors=%d reached — stopping stream",
                           side, path, max_parse_errors)
                return
            continue

        try:
            entry = CommitEntry.from_dict(d, xlen=xlen, lineno=lineno, source=path)
        except (KeyError, ValueError, TypeError) as exc:
            parse_errors += 1
            _log.warning("[%s] %s:%d: Entry error: %s", side, path, lineno, exc)
            if stats is not None:
                if side == "RTL":
                    stats.rtl_parse_warnings += 1
                else:
                    stats.iss_parse_warnings += 1
            if max_parse_errors == 0:
                raise LogFormatError(f"[{side}] {path}:{lineno}: {exc}") from exc
            if parse_errors >= max_parse_errors:
                _log.error("[%s] max_parse_errors reached — stopping", side)
                return
            continue

        if entry.x0_violation:
            _log.warning("[%s] x0 invariant violated: line=%d step=%d pc=%s",
                         side, lineno, entry.step, entry.fmt_pc())
            if stats is not None:
                if side == "RTL":
                    stats.rtl_x0_violations += 1
                else:
                    stats.iss_x0_violations += 1

        if entry.schema_violation:
            _log.warning("[%s] schema violation: line=%d step=%d: %s",
                         side, lineno, entry.step, "; ".join(entry.schema_errors))

        yield entry


# ═══════════════════════════════════════════════════════════════════════════════
# Step continuity validator
# ═══════════════════════════════════════════════════════════════════════════════

class _StepValidator:
    """Track step-number sequence for one side; report anomalies."""

    def __init__(self, side: str) -> None:
        self._side = side
        self._last: Optional[int] = None
        self._seen: Set[int]      = set()

    def check(self, entry: CommitEntry, stats: CompareStats) -> Optional[Mismatch]:
        step = entry.step

        if self._last is None:
            self._last = step - 1   # first entry: init without check

        m: Optional[Mismatch] = None

        if step in self._seen:
            m = Mismatch(
                mismatch_type   = MismatchType.SEQ_GAP,
                severity        = Severity.CRITICAL,
                description     = (
                    f"[{self._side}] Duplicate step={step} at pc={entry.fmt_pc()}"
                ),
                step            = step,
                rtl_entry       = entry.to_dict(),
                iss_entry       = None,
                differing_field = "step",
                rtl_value       = str(step),
                iss_value       = f"dup (last={self._last})",
            )
            if self._side == "RTL":
                stats.rtl_step_anomalies += 1
            else:
                stats.iss_step_anomalies += 1

        elif step != self._last + 1:
            direction = "gap" if step > self._last + 1 else "reorder"
            m = Mismatch(
                mismatch_type   = MismatchType.SEQ_GAP,
                severity        = Severity.CRITICAL,
                description     = (
                    f"[{self._side}] Step {direction}: "
                    f"expected {self._last+1} got {step} "
                    f"at pc={entry.fmt_pc()}"
                ),
                step            = step,
                rtl_entry       = entry.to_dict(),
                iss_entry       = None,
                differing_field = "step",
                rtl_value       = str(step),
                iss_value       = str(self._last + 1),
            )
            if self._side == "RTL":
                stats.rtl_step_anomalies += 1
            else:
                stats.iss_step_anomalies += 1

        self._seen.add(step)
        self._last = step
        return m


# ═══════════════════════════════════════════════════════════════════════════════
# Field-level comparators
# ═══════════════════════════════════════════════════════════════════════════════

# (type, description, field_name, rtl_val_str, iss_val_str)
_Issue = Tuple[MismatchType, str, str, Optional[str], Optional[str]]


class _FieldComparator:
    """Stateless, config-driven field comparison engine."""

    def __init__(self, cfg: CompareConfig) -> None:
        self._cfg  = cfg
        self._skip = cfg.skip_fields

    def compare(self, rtl: CommitEntry, iss: CommitEntry) -> List[_Issue]:
        """Compare one matched commit pair.  Returns all issues found.

        Check order:
          1. Schema violations (both sides)
          2. x0 invariant (both sides, independent)
          3. PC — abort further checks on mismatch
          4. Instruction word — abort on mismatch
          5. Trap signalling + cause fields
          6. Register write-back
          7. CSR writes
          8. Memory (addr → val → alignment)
          9. Privilege mode
        """
        issues: List[_Issue] = []

        # ── 1. Schema validation (per-side, independent) ─────────────────────
        for side_name, entry in (("RTL", rtl), ("ISS", iss)):
            if entry.schema_violation:
                for err in entry.schema_errors:
                    issues.append((
                        MismatchType.SCHEMA_INVALID,
                        f"[{side_name}] Schema violation at {entry.fmt_pc()}: {err}",
                        "schema",
                        err if side_name == "RTL" else None,
                        err if side_name == "ISS" else None,
                    ))

        # ── 2. x0 invariant (per-side, independent) ──────────────────────────
        if self._cfg.enforce_x0_invariant:
            for side_name, entry in (("RTL", rtl), ("ISS", iss)):
                if entry.x0_violation:
                    issues.append((
                        MismatchType.X0_WRITTEN,
                        f"[{side_name}] x0 written with "
                        f"{_fmt_hex(entry.rd_val)} at {entry.fmt_pc()}",
                        "rd_val",
                        _fmt_hex(entry.rd_val) if side_name == "RTL" else "0x00000000",
                        "0x00000000" if side_name == "RTL" else _fmt_hex(entry.rd_val),
                    ))

        # ── 3. PC ─────────────────────────────────────────────────────────────
        if "pc" not in self._skip and rtl.pc != iss.pc:
            issues.append((
                MismatchType.PC_MISMATCH,
                f"PC diverged: rtl={rtl.fmt_pc()} iss={iss.fmt_pc()}",
                "pc", rtl.fmt_pc(), iss.fmt_pc(),
            ))
            return issues   # all further comparisons meaningless

        # ── 4. Instruction word ───────────────────────────────────────────────
        if "instr" not in self._skip and rtl.instr != iss.instr:
            issues.append((
                MismatchType.INSTR_MISMATCH,
                f"Instruction word mismatch at {rtl.fmt_pc()}: "
                f"rtl={rtl.fmt_instr()} iss={iss.fmt_instr()}",
                "instr", rtl.fmt_instr(), iss.fmt_instr(),
            ))
            return issues   # fetch divergence → rest not comparable

        # ── 5. Trap signalling ────────────────────────────────────────────────
        if "trap" not in self._skip:
            if rtl.trap != iss.trap:
                issues.append((
                    MismatchType.TRAP_MISMATCH,
                    f"Trap mismatch at {rtl.fmt_pc()}: "
                    f"rtl.trap={rtl.trap} iss.trap={iss.trap}",
                    "trap", str(rtl.trap), str(iss.trap),
                ))
            elif rtl.trap:
                for attr, label in (
                    ("trap_cause", "trap_cause"),
                    ("trap_tval",  "trap_tval"),
                    ("trap_pc",    "trap_pc"),
                ):
                    if attr in self._skip:
                        continue
                    rv = getattr(rtl, attr)
                    iv = getattr(iss, attr)
                    if rv is not None and iv is not None and rv != iv:
                        issues.append((
                            MismatchType.TRAP_CAUSE_MISMATCH,
                            f"{label} mismatch at {rtl.fmt_pc()}: "
                            f"rtl={_fmt_hex(rv)} iss={_fmt_hex(iv)}",
                            label, _fmt_hex(rv), _fmt_hex(iv),
                        ))

        # ── 6. Register write-back ────────────────────────────────────────────
        if "rd_val" not in self._skip:
            if rtl.rd is not None and rtl.rd != 0:
                if iss.rd is not None and rtl.rd != iss.rd:
                    issues.append((
                        MismatchType.REG_MISMATCH,
                        f"Destination register index mismatch at {rtl.fmt_pc()}: "
                        f"rtl=x{rtl.rd} iss=x{iss.rd}",
                        "rd", f"x{rtl.rd}", f"x{iss.rd}",
                    ))
                elif rtl.rd_val is not None and iss.rd_val is not None:
                    if rtl.rd_val != iss.rd_val:
                        issues.append((
                            MismatchType.REG_MISMATCH,
                            f"x{rtl.rd} write-back mismatch at {rtl.fmt_pc()}: "
                            f"rtl={_fmt_hex(rtl.rd_val)} iss={_fmt_hex(iss.rd_val)}",
                            "rd_val",
                            _fmt_hex(rtl.rd_val), _fmt_hex(iss.rd_val),
                        ))

        # ── 7. CSR writes ─────────────────────────────────────────────────────
        if "csr_writes" not in self._skip:
            issues.extend(self._cmp_csrs(rtl, iss))

        # ── 8. Memory (address, value, then alignment) ────────────────────────
        if not {"mem_addr", "mem_val", "mem_op"}.issubset(self._skip):
            issues.extend(self._cmp_mem(rtl, iss))

        # ── 9. Privilege mode ─────────────────────────────────────────────────
        if (
            "privilege" not in self._skip
            and rtl.privilege is not None
            and iss.privilege is not None
            and rtl.privilege != iss.privilege
        ):
            issues.append((
                MismatchType.PRIVILEGE_MISMATCH,
                f"Privilege mismatch at {rtl.fmt_pc()}: "
                f"rtl={rtl.privilege} iss={iss.privilege}",
                "privilege", rtl.privilege, iss.privilege,
            ))

        return issues

    # ── CSR helper ────────────────────────────────────────────────────────────

    def _cmp_csrs(self, rtl: CommitEntry, iss: CommitEntry) -> List[_Issue]:
        issues: List[_Issue] = []
        cfg = self._cfg

        def _norm(writes: List[CsrWrite]) -> List[CsrWrite]:
            # Drop ignored CSRs
            out = [w for w in writes if w.csr not in cfg.ignored_csrs]
            # Apply per-CSR masks
            if cfg.csr_masks:
                out = [
                    CsrWrite(w.csr, w.val & cfg.csr_masks.get(w.csr, cfg.value_mask))
                    for w in out
                ]
            # Normalise order
            if not cfg.csr_write_order_sensitive:
                out = sorted(out, key=lambda w: w.csr)
            return out

        rl = _norm(rtl.csr_writes)
        il = _norm(iss.csr_writes)

        if len(rl) != len(il):
            issues.append((
                MismatchType.CSR_MISMATCH,
                f"CSR write count at {rtl.fmt_pc()}: rtl={len(rl)} iss={len(il)}",
                "csr_writes",
                str([w.to_dict() for w in rl]),
                str([w.to_dict() for w in il]),
            ))
            return issues

        for rw, iw in zip(rl, il):
            if rw.csr != iw.csr:
                issues.append((
                    MismatchType.CSR_MISMATCH,
                    f"CSR address mismatch at {rtl.fmt_pc()}: "
                    f"rtl=0x{rw.csr:03x} iss=0x{iw.csr:03x}",
                    "csr_writes",
                    f"0x{rw.csr:03x}", f"0x{iw.csr:03x}",
                ))
            elif rw.val != iw.val:
                issues.append((
                    MismatchType.CSR_MISMATCH,
                    f"CSR[0x{rw.csr:03x}] value mismatch at {rtl.fmt_pc()}: "
                    f"rtl={_fmt_hex(rw.val)} iss={_fmt_hex(iw.val)}",
                    f"csr[0x{rw.csr:03x}]",
                    _fmt_hex(rw.val), _fmt_hex(iw.val),
                ))
        return issues

    # ── Memory helper ─────────────────────────────────────────────────────────

    def _cmp_mem(self, rtl: CommitEntry, iss: CommitEntry) -> List[_Issue]:
        issues: List[_Issue] = []

        if rtl.mem_op is None and iss.mem_op is None:
            return issues

        if rtl.mem_op != iss.mem_op:
            issues.append((
                MismatchType.MEM_MISMATCH,
                f"Memory op-type mismatch at {rtl.fmt_pc()}: "
                f"rtl={rtl.mem_op!r} iss={iss.mem_op!r}",
                "mem_op", str(rtl.mem_op), str(iss.mem_op),
            ))
            return issues

        if rtl.mem_addr is not None and iss.mem_addr is not None:
            if rtl.mem_addr != iss.mem_addr:
                issues.append((
                    MismatchType.MEM_MISMATCH,
                    f"Memory address mismatch at {rtl.fmt_pc()} op={rtl.mem_op}: "
                    f"rtl={_fmt_hex(rtl.mem_addr)} iss={_fmt_hex(iss.mem_addr)}",
                    "mem_addr",
                    _fmt_hex(rtl.mem_addr), _fmt_hex(iss.mem_addr),
                ))
                return issues   # addr wrong → val comparison meaningless

        if rtl.mem_val is not None and iss.mem_val is not None:
            size_mask = self._cfg.value_mask
            for sz in (rtl.mem_size, iss.mem_size):
                if sz is not None:
                    size_mask = min(size_mask, (1 << (sz * 8)) - 1)
            if (rtl.mem_val & size_mask) != (iss.mem_val & size_mask):
                issues.append((
                    MismatchType.MEM_MISMATCH,
                    f"Memory value mismatch at {rtl.fmt_pc()} "
                    f"op={rtl.mem_op} addr={_fmt_hex(rtl.mem_addr)}: "
                    f"rtl={_fmt_hex(rtl.mem_val)} iss={_fmt_hex(iss.mem_val)}",
                    "mem_val",
                    _fmt_hex(rtl.mem_val), _fmt_hex(iss.mem_val),
                ))

        # ── Alignment check (ALIGNMENTERROR) — per-side, independent ─────────
        if self._cfg.check_alignment:
            for side_name, entry in (("RTL", rtl), ("ISS", iss)):
                if (
                    entry.mem_addr is not None
                    and entry.mem_size is not None
                    and entry.mem_size > 1
                    and (entry.mem_addr % entry.mem_size) != 0
                ):
                    issues.append((
                        MismatchType.ALIGN_ERROR,
                        f"[{side_name}] Unaligned {entry.mem_op} at {entry.fmt_pc()}: "
                        f"addr={_fmt_hex(entry.mem_addr)} size={entry.mem_size}B "
                        f"(misalignment={(entry.mem_addr % entry.mem_size)}B)",
                        "mem_addr",
                        _fmt_hex(entry.mem_addr) if side_name == "RTL" else None,
                        _fmt_hex(entry.mem_addr) if side_name == "ISS" else None,
                    ))

        return issues


# ═══════════════════════════════════════════════════════════════════════════════
# Threaded parallel log reader
# ═══════════════════════════════════════════════════════════════════════════════
# Two producer threads push CommitEntry objects into bounded queues.  The main
# thread consumes from both queues synchronously.  This keeps I/O and JSON
# parsing off the main thread and bounds working memory to ~2×queue_size records
# regardless of trace length.  A 1M-instruction run at ~300 B/record is ~300 MB
# as a list; with queue_size=512 the working set stays under 1 MB.

_QUEUE_SENTINEL = object()  # signals end-of-stream to consumer


def _reader_thread(
    path:             str,
    xlen:             int,
    max_parse_errors: int,
    stats:            CompareStats,
    side:             str,
    q:                "queue.Queue[Any]",
    error_box:        List[Optional[Exception]],
) -> None:
    """Producer: open log → parse → enqueue CommitEntry objects."""
    try:
        fh = _open_log_file(path)
        try:
            for entry in _iter_commits(
                fh, path=path, xlen=xlen,
                max_parse_errors=max_parse_errors,
                stats=stats, side=side,
            ):
                q.put(entry)
        finally:
            fh.close()
    except Exception as exc:
        error_box[0] = exc
    finally:
        q.put(_QUEUE_SENTINEL)


# ═══════════════════════════════════════════════════════════════════════════════
# Core streaming comparator
# ═══════════════════════════════════════════════════════════════════════════════

def compare(
    rtl_path: str,
    iss_path: str,
    *,
    cfg:              Optional[CompareConfig] = None,
    seed:             Optional[int]           = None,
    rtl_bin:          Optional[str]           = None,
    iss_bin:          Optional[str]           = None,
    repro_extra_args: str                     = "",
) -> CompareResult:
    """
    Stream-compare two commit logs using parallel reader threads.

    Architecture
    ------------
    Two daemon threads parse and enqueue CommitEntry objects from each log.
    The main thread dequeues one entry from each side per iteration, runs the
    field comparator, and records mismatches.  Only a bounded queue (512 entries
    per side) and the sliding delta-window live in memory at any time, keeping
    RAM usage under ~1 MB even for multi-million-instruction traces.

    Parameters
    ----------
    rtl_path         : path to RTL JSONL commit log (or ``"-"`` for stdin)
    iss_path         : path to ISS/golden JSONL commit log
    cfg              : comparison config; defaults applied if None
    seed             : RNG seed embedded in the repro command string
    rtl_bin          : RTL simulator binary path (for repro string)
    iss_bin          : ISS binary path (for repro string)
    repro_extra_args : appended verbatim to repro command string

    Returns
    -------
    CompareResult — raises LogFormatError / ConfigError for unrecoverable errors.
    """
    if cfg is None:
        cfg = CompareConfig()

    stats   = CompareStats()
    t0      = time.perf_counter()
    results: List[Mismatch] = []

    # ── delta-based sliding window (replaces full-dict deque) ────────────────
    dwindow = _DeltaWindow(maxlen=max(cfg.window, 0))

    # ── build repro command ───────────────────────────────────────────────────
    parts = [sys.executable, os.path.abspath(__file__), rtl_path, iss_path]
    if seed is not None:
        parts += ["--seed", str(seed)]
    if rtl_bin:
        parts += ["--rtl-bin", shlex.quote(rtl_bin)]
    if iss_bin:
        parts += ["--iss-bin", shlex.quote(iss_bin)]
    parts += ["--xlen", str(cfg.xlen), "--window", str(cfg.window)]
    if repro_extra_args:
        parts.append(repro_extra_args)
    repro_cmd = " ".join(parts)

    # ── SHA-256 checksums (computed before opening file handles) ─────────────
    rtl_sha = _sha256(rtl_path)
    iss_sha = _sha256(iss_path)

    max_m = cfg.max_mismatches if not cfg.stop_on_first else 1

    def _add(m: Mismatch) -> None:
        m.repro_cmd      = repro_cmd
        m.context_window = dwindow.snapshot()
        m.elapsed_s      = time.perf_counter() - t0
        results.append(m)
        stats.record(m)
        _log.debug("Mismatch [%s]: %s", m.mismatch_type.value, m.description)

    def _limit() -> bool:
        return max_m > 0 and len(results) >= max_m

    # ── binary hash pre-flight checks ─────────────────────────────────────────
    for expected, actual, log_label in (
        (cfg.expected_rtl_sha256, rtl_sha, "RTL"),
        (cfg.expected_iss_sha256, iss_sha, "ISS"),
    ):
        if expected is not None and actual is not None:
            if expected.lower() != actual.lower():
                _add(Mismatch(
                    mismatch_type   = MismatchType.BINARY_HASH_MISMATCH,
                    severity        = Severity.CRITICAL,
                    description     = (
                        f"{log_label} log SHA-256 mismatch: "
                        f"expected {expected[:16]}… got {actual[:16]}…"
                    ),
                    step            = 0,
                    rtl_entry       = None,
                    iss_entry       = None,
                    differing_field = f"{log_label.lower()}_sha256",
                    rtl_value       = actual   if log_label == "RTL" else None,
                    iss_value       = actual   if log_label == "ISS" else None,
                    repro_cmd       = repro_cmd,
                ))
                if _limit():
                    return CompareResult(
                        passed     = False,
                        stats      = stats,
                        mismatches = results,
                        rtl_log    = rtl_path,
                        iss_log    = iss_path,
                        rtl_sha256 = rtl_sha,
                        iss_sha256 = iss_sha,
                        seed       = seed,
                        rtl_bin    = rtl_bin,
                        iss_bin    = iss_bin,
                        config     = _serialise_cfg(cfg),
                    )

    # ── launch reader threads ─────────────────────────────────────────────────
    _QUEUE_DEPTH = 512
    rtl_q:     "queue.Queue[Any]" = queue.Queue(maxsize=_QUEUE_DEPTH)
    iss_q:     "queue.Queue[Any]" = queue.Queue(maxsize=_QUEUE_DEPTH)
    rtl_err:   List[Optional[Exception]] = [None]
    iss_err:   List[Optional[Exception]] = [None]

    rtl_thread = threading.Thread(
        target=_reader_thread,
        args=(rtl_path, cfg.xlen, cfg.max_parse_errors, stats, "RTL", rtl_q, rtl_err),
        daemon=True, name="rtl-reader",
    )
    iss_thread = threading.Thread(
        target=_reader_thread,
        args=(iss_path, cfg.xlen, cfg.max_parse_errors, stats, "ISS", iss_q, iss_err),
        daemon=True, name="iss-reader",
    )
    rtl_thread.start()
    iss_thread.start()

    comparator = _FieldComparator(cfg)
    rtl_sv     = _StepValidator("RTL")
    iss_sv     = _StepValidator("ISS")

    try:
        rtl_done = iss_done = False

        while not _limit():
            # ── dequeue one entry from each side ─────────────────────────────
            r: Optional[CommitEntry] = None
            i: Optional[CommitEntry] = None

            if not rtl_done:
                item = rtl_q.get()
                if item is _QUEUE_SENTINEL:
                    rtl_done = True
                else:
                    r = item

            if not iss_done:
                item = iss_q.get()
                if item is _QUEUE_SENTINEL:
                    iss_done = True
                else:
                    i = item

            if rtl_done and iss_done:
                # Final thread-error check before exit
                for err_box, side_label in ((rtl_err, "RTL"), (iss_err, "ISS")):
                    if err_box[0] is not None:
                        raise LogFormatError(
                            f"{side_label} reader thread failed: {err_box[0]}"
                        ) from err_box[0]
                break

            stats.total_steps += 1

            # ── thread error check ────────────────────────────────────────────
            for err_box, side_label in ((rtl_err, "RTL"), (iss_err, "ISS")):
                if err_box[0] is not None:
                    raise LogFormatError(
                        f"{side_label} reader thread failed: {err_box[0]}"
                    ) from err_box[0]

            # ── one side ended early ──────────────────────────────────────────
            if rtl_done or iss_done:
                which   = "RTL" if rtl_done else "ISS"
                present = i if rtl_done else r
                snum    = present.step if present else stats.total_steps
                _add(Mismatch(
                    mismatch_type   = MismatchType.LENGTH_MISMATCH,
                    severity        = Severity.CRITICAL,
                    description     = (
                        f"{which} log ended at step {snum}; "
                        "the other log has more commits."
                    ),
                    step            = snum,
                    rtl_entry       = r.to_dict() if r else None,
                    iss_entry       = i.to_dict() if i else None,
                    differing_field = "eof",
                    rtl_value       = "EOF" if rtl_done else f"step={snum}",
                    iss_value       = f"step={snum}" if rtl_done else "EOF",
                ))
                break

            # ── step continuity ───────────────────────────────────────────────
            for sv, entry in ((rtl_sv, r), (iss_sv, i)):
                sm = sv.check(entry, stats)  # type: ignore[arg-type]
                if sm is not None:
                    if cfg.strict_steps:
                        raise StepError(sm.description)
                    _add(sm)
                    if _limit():
                        break
            if _limit():
                break

            # ── progress ──────────────────────────────────────────────────────
            if (
                cfg.progress_every > 0
                and stats.total_steps % cfg.progress_every == 0
            ):
                _log.info(
                    "Progress: %d steps, %d mismatches",
                    stats.total_steps, stats.total_mismatches,
                )

            # ── push to delta window BEFORE field comparison ──────────────────
            dwindow.push(r)   # type: ignore[arg-type]

            # ── field comparison ──────────────────────────────────────────────
            for (mtype, desc, fname, rval, ival) in comparator.compare(r, i):  # type: ignore[arg-type]
                _add(Mismatch(
                    mismatch_type   = mtype,
                    severity        = _severity_for(mtype),
                    description     = desc,
                    step            = r.step,   # type: ignore[union-attr]
                    rtl_entry       = r.to_dict(),  # type: ignore[union-attr]
                    iss_entry       = i.to_dict(),  # type: ignore[union-attr]
                    differing_field = fname,
                    rtl_value       = rval,
                    iss_value       = ival,
                ))
                if _limit():
                    break

    finally:
        # Drain queues so producer threads can unblock and exit cleanly
        for q_obj in (rtl_q, iss_q):
            while True:
                try:
                    q_obj.get_nowait()
                except queue.Empty:
                    break
        rtl_thread.join(timeout=5)
        iss_thread.join(timeout=5)

    stats.elapsed_s = time.perf_counter() - t0
    _log.info(
        "Done: %d steps, %d mismatches, %.3fs",
        stats.total_steps, stats.total_mismatches, stats.elapsed_s,
    )

    return CompareResult(
        passed     = len(results) == 0,
        stats      = stats,
        mismatches = results,
        rtl_log    = rtl_path,
        iss_log    = iss_path,
        rtl_sha256 = rtl_sha,
        iss_sha256 = iss_sha,
        seed       = seed,
        rtl_bin    = rtl_bin,
        iss_bin    = iss_bin,
        config     = _serialise_cfg(cfg),
    )


def _serialise_cfg(cfg: CompareConfig) -> Optional[Dict]:
    """Return a JSON-serialisable dict from a CompareConfig."""
    try:
        d = asdict(cfg)
        for k in ("extensions", "skip_fields", "ignored_csrs"):
            if k in d:
                d[k] = list(d[k])
        return d
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Batch runner
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BatchEntry:
    rtl_log: str
    iss_log: str
    seed:    Optional[int]  = None
    rtl_bin: Optional[str]  = None
    iss_bin: Optional[str]  = None
    label:   str            = ""
    config:  Optional[Dict] = None


def compare_logs_batch(
    entries:  List[BatchEntry],
    base_cfg: Optional[CompareConfig] = None,
) -> List[CompareResult]:
    """Compare multiple log pairs sequentially."""
    out = []
    for idx, entry in enumerate(entries, 1):
        _log.info("Batch [%d/%d] label=%r", idx, len(entries), entry.label)
        cfg = base_cfg
        if entry.config:
            merged = asdict(base_cfg) if base_cfg else {}
            merged.update(entry.config)
            cfg = CompareConfig.from_dict(merged)
        out.append(compare(
            entry.rtl_log, entry.iss_log,
            cfg=cfg, seed=entry.seed,
            rtl_bin=entry.rtl_bin, iss_bin=entry.iss_bin,
        ))
    return out


def _load_manifest(path: str) -> List[BatchEntry]:
    p = Path(path)
    if not p.exists():
        raise LogFormatError(f"Manifest not found: {path!r}")
    text = p.read_text(encoding="utf-8")
    if path.endswith(".json"):
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore[import]
            data = yaml.safe_load(text)
        except ImportError:
            data = json.loads(text)   # YAML superset of JSON for basic cases
    raw_list = data.get("entries", data)
    if not isinstance(raw_list, list):
        raise LogFormatError("Manifest must contain a list of entries")
    return [
        BatchEntry(
            rtl_log = row["rtl_log"],
            iss_log = row["iss_log"],
            seed    = row.get("seed"),
            rtl_bin = row.get("rtl_bin"),
            iss_bin = row.get("iss_bin"),
            label   = row.get("label", ""),
            config  = row.get("config"),
        )
        for row in raw_list
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# AVA Manifest mode — atomic I/O helpers
# ═══════════════════════════════════════════════════════════════════════════════

def atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* crash-safely via a unique sibling temp file.

    Uses ``tempfile.mkstemp`` in the same directory so the rename is guaranteed
    to be on the same filesystem.  On POSIX, ``rename()`` is atomic.  On
    Windows, we unlink the target first (NTFS requirement).
    """
    import tempfile as _tf
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = _tf.mkstemp(dir=path.parent, prefix=".atomic_", suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if sys.platform == "win32" and path.exists():
            path.unlink()
        tmp.rename(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_update_manifest(manifest_path: Path, updates: Dict[str, Any]) -> None:
    """Merge *updates* into the JSON manifest at *manifest_path* atomically.

    Keys may use dot-notation to address nested fields::

        atomic_update_manifest(p, {"phases.compare.status": "passed"})
        # sets manifest["phases"]["compare"]["status"] = "passed"
    """
    manifest_path = Path(manifest_path)
    try:
        data: Dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read manifest {manifest_path}: {exc}") from exc

    for key, value in updates.items():
        if "." in key:
            *parents, child = key.split(".")
            d = data
            for parent in parents:
                if parent not in d or not isinstance(d[parent], dict):
                    d[parent] = {}
                d = d[parent]
            d[child] = value
        else:
            data[key] = value

    atomic_write(manifest_path, json.dumps(data, indent=2, default=str))


def main_manifest(manifest_path: Path, base_cfg: Optional[CompareConfig] = None) -> int:
    """AVA contract: ``--manifest manifest.json`` mode.

    Reads the AVA run manifest, resolves the standard log paths
    (``rundir/outputs/rtlcommit.jsonl`` and ``rundir/outputs/isscommitlog.jsonl``),
    runs the comparison, writes ``bugreport.json``, and updates the manifest
    with the comparison outcome.

    Exit codes follow the AVA standard:
      0 — PASS
      1 — logical mismatch
      2 — infrastructure error (I/O, thread failure)
      3 — configuration error (missing fields, bad paths)
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        _log.error("Manifest not found: %s", manifest_path)
        return EXIT_CONFIG

    try:
        manifest: Dict[str, Any] = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError) as exc:
        _log.error("Cannot read manifest: %s", exc)
        return EXIT_CONFIG

    # ── resolve required fields ───────────────────────────────────────────────
    rundir_raw = manifest.get("rundir")
    if not rundir_raw:
        _log.error("Manifest missing required field 'rundir'")
        try:
            atomic_update_manifest(manifest_path, {
                "phases.compare.status": "error",
                "phases.compare.error":  "Manifest missing 'rundir'",
            })
        except ConfigError:
            pass
        return EXIT_CONFIG

    rundir  = Path(rundir_raw)
    rtl_log = rundir / "outputs" / "rtlcommit.jsonl"
    iss_log = rundir / "outputs" / "isscommitlog.jsonl"

    # Also accept gzip variants automatically
    for candidate in (rtl_log, rtl_log.with_suffix(".jsonl.gz")):
        if candidate.exists():
            rtl_log = candidate
            break
    for candidate in (iss_log, iss_log.with_suffix(".jsonl.gz")):
        if candidate.exists():
            iss_log = candidate
            break

    if not rtl_log.exists() or not iss_log.exists():
        missing = [str(p) for p in (rtl_log, iss_log) if not p.exists()]
        _log.error("Missing commit logs: %s", missing)
        try:
            atomic_update_manifest(manifest_path, {
                "phases.compare.status": "error",
                "phases.compare.error":  f"Missing commit logs: {missing}",
                "status":                "error",
            })
        except ConfigError:
            pass
        return EXIT_CONFIG

    # ── build config from manifest fields ─────────────────────────────────────
    cfg = base_cfg or CompareConfig()
    manifest_cfg = manifest.get("compare_config", {})
    if manifest_cfg:
        try:
            cfg = CompareConfig.from_dict({**asdict(cfg), **manifest_cfg})
        except (ValueError, TypeError) as exc:
            _log.error("Invalid compare_config in manifest: %s", exc)
            try:
                atomic_update_manifest(manifest_path, {
                    "phases.compare.status": "error",
                    "phases.compare.error":  f"Invalid compare_config: {exc}",
                })
            except ConfigError:
                pass
            return EXIT_CONFIG

    seed    = manifest.get("seed")
    rtl_bin = manifest.get("rtl_bin")
    iss_bin = manifest.get("iss_bin")

    # ── pin expected SHA-256 hashes if manifest provides them ─────────────────
    expected_hashes = manifest.get("expected_sha256", {})
    if expected_hashes.get("rtl_log"):
        cfg.expected_rtl_sha256 = expected_hashes["rtl_log"]
    if expected_hashes.get("iss_log"):
        cfg.expected_iss_sha256 = expected_hashes["iss_log"]

    # ── run comparison ────────────────────────────────────────────────────────
    try:
        result = compare(
            str(rtl_log), str(iss_log),
            cfg=cfg, seed=seed,
            rtl_bin=rtl_bin, iss_bin=iss_bin,
        )
    except (LogFormatError, ParseError) as exc:
        _log.error("Infrastructure error: %s", exc)
        try:
            atomic_update_manifest(manifest_path, {
                "phases.compare.status": "error",
                "phases.compare.error":  str(exc),
                "status":                "error",
            })
        except ConfigError:
            pass
        return EXIT_INFRA
    except Exception as exc:
        _log.exception("Unexpected comparator error: %s", exc)
        return EXIT_INFRA

    # ── write bugreport.json ──────────────────────────────────────────────────
    bugreport_path: Optional[Path] = None
    if not result.passed:
        bugreport_path = rundir / "bugreport.json"
        try:
            atomic_write(
                bugreport_path,
                json.dumps(result.to_bug_report(), indent=2, default=str),
            )
        except OSError as exc:
            _log.error("Cannot write bugreport.json: %s", exc)
            return EXIT_INFRA

    # ── update manifest atomically ────────────────────────────────────────────
    first_type = (
        result.mismatches[0].mismatch_type.value if result.mismatches else None
    )
    try:
        atomic_update_manifest(manifest_path, {
            "phases.compare.status":         "passed" if result.passed else "failed",
            "phases.compare.total_steps":    result.stats.total_steps,
            "phases.compare.total_mismatches": result.stats.total_mismatches,
            "phases.compare.elapsed_s":      round(result.stats.elapsed_s, 4),
            "phases.compare.first_mismatch": first_type,
            "outputs.bugreport":             str(bugreport_path) if bugreport_path else None,
            "status":                        "passed" if result.passed else "failed",
        })
    except ConfigError as exc:
        _log.error("Cannot update manifest: %s", exc)
        return EXIT_INFRA

    return EXIT_PASS if result.passed else EXIT_MISMATCH


# ═══════════════════════════════════════════════════════════════════════════════
# Terminal output
# ═══════════════════════════════════════════════════════════════════════════════

_R = "\033[0m"
_RED   = "\033[31m"
_GREEN = "\033[32m"
_YEL   = "\033[33m"
_CYAN  = "\033[36m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"


def _c(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_R}" if enabled else text


def print_summary(
    result:    CompareResult,
    use_color: bool = True,
    file:      Any  = None,
    verbose:   bool = False,
) -> None:
    out = file or sys.stdout
    sep = "─" * 76

    def _p(s: str = "") -> None:
        print(s, file=out)

    st = result.stats

    if result.passed:
        _p(_c(
            f"\n✓  PASS  — {st.total_steps:,} steps compared in "
            f"{st.elapsed_s:.3f}s — no mismatches\n",
            _GREEN + _BOLD, use_color,
        ))
        if st.rtl_parse_warnings or st.iss_parse_warnings:
            _p(_c(
                f"  ⚠ Parse warnings: RTL={st.rtl_parse_warnings} "
                f"ISS={st.iss_parse_warnings}",
                _YEL, use_color,
            ))
        return

    _p()
    _p(_c(sep, _RED, use_color))
    _p(_c(
        f"✗  MISMATCH  — first divergence at step "
        f"{st.first_divergence_step:,} "
        f"(of {st.total_steps:,} compared)  [{st.elapsed_s:.3f}s]",
        _RED + _BOLD, use_color,
    ))
    _p(_c(sep, _RED, use_color))

    for idx, m in enumerate(result.mismatches):
        _p()
        _p(_c(
            f"  [{idx+1}/{len(result.mismatches)}] "
            f"{m.mismatch_type.value} [{m.severity.value}] — "
            f"{m.mismatch_type.human}",
            _YEL + _BOLD, use_color,
        ))
        _p(f"      step     : {m.step:,}")
        _p(f"      field    : {m.differing_field or '—'}")
        _p(_c(f"      RTL      : {m.rtl_value}", _RED,   use_color))
        _p(_c(f"      ISS/gold : {m.iss_value}", _GREEN, use_color))
        _p(f"      detail   : {m.description}")

        if verbose:
            for side_name, entry_dict in (("RTL", m.rtl_entry), ("ISS", m.iss_entry)):
                if entry_dict:
                    _p(f"\n      {side_name} commit:")
                    for k, v in entry_dict.items():
                        if v is not None:
                            _p(f"        {k:<14}: {v}")

        if m.context_window:
            n = min(len(m.context_window), 8)
            _p()
            _p(_c(
                f"      Context window — "
                f"last {len(m.context_window)} RTL commits (showing {n})",
                _DIM, use_color,
            ))
            for c in m.context_window[-n:]:
                _p(
                    f"        step={c.get('step','?'):>7}  "
                    f"pc={c.get('pc','?')}  "
                    f"instr={c.get('instr','?')}  "
                    f"{c.get('disasm','')}"
                )

        _p()
        if m.repro_cmd:
            _p(_c(f"  ▶ Repro: {m.repro_cmd}", _CYAN, use_color))

    _p()
    _p(_c("  Mismatch summary:", _BOLD, use_color))
    for mtype, cnt in sorted(st.mismatch_by_type.items(), key=lambda x: -x[1]):
        _p(f"    {mtype:<28} {cnt}")

    if st.rtl_parse_warnings or st.iss_parse_warnings:
        _p(_c(
            f"\n  ⚠ Parse warnings: RTL={st.rtl_parse_warnings} "
            f"ISS={st.iss_parse_warnings}",
            _YEL, use_color,
        ))
    if st.rtl_x0_violations or st.iss_x0_violations:
        _p(_c(
            f"  ⚠ x0 violations: RTL={st.rtl_x0_violations} "
            f"ISS={st.iss_x0_violations}",
            _YEL, use_color,
        ))
    _p(_c(sep, _RED, use_color))
    _p()


# ═══════════════════════════════════════════════════════════════════════════════
# Sample log generator
# ═══════════════════════════════════════════════════════════════════════════════

def _mk(
    step: int, pc: int, instr: int = 0x00000013, *,
    trap:       bool            = False,
    rd:         Optional[int]   = None,
    rd_val:     Optional[int]   = None,
    csr_writes: Optional[list]  = None,
    mem_op:     Optional[str]   = None,
    mem_addr:   Optional[int]   = None,
    mem_val:    Optional[int]   = None,
    mem_size:   Optional[int]   = None,
    trap_cause: Optional[int]   = None,
    trap_pc:    Optional[int]   = None,
    privilege:  Optional[str]   = None,
    disasm:     Optional[str]   = None,
) -> Dict[str, Any]:
    e: Dict[str, Any] = {
        "step": step,
        "pc":   f"0x{pc:08x}",
        "instr": f"0x{instr:08x}",
        "trap": trap,
        "csr_writes": csr_writes or [],
    }
    if rd is not None:      e["rd"]      = rd
    if rd_val is not None:  e["rd_val"]  = f"0x{rd_val:08x}"
    if mem_op:
        e["mem_op"]   = mem_op
        e["mem_addr"] = f"0x{mem_addr:08x}" if mem_addr is not None else None
        e["mem_val"]  = f"0x{mem_val:08x}"  if mem_val  is not None else None
    if mem_size is not None: e["mem_size"] = mem_size
    if trap_cause is not None: e["trap_cause"] = f"0x{trap_cause:08x}"
    if trap_pc    is not None: e["trap_pc"]    = f"0x{trap_pc:08x}"
    if privilege:  e["privilege"] = privilege
    if disasm:     e["disasm"]    = disasm
    return e


def generate_sample_logs(output_dir: str = ".") -> None:
    """Write four sample log pairs to *output_dir*."""
    od = Path(output_dir)
    od.mkdir(parents=True, exist_ok=True)

    base: List[Dict] = [
        _mk(1, 0x1000, 0x00100093, rd=1,  rd_val=1,  disasm="addi x1,x0,1"),
        _mk(2, 0x1004, 0x00200113, rd=2,  rd_val=2,  disasm="addi x2,x0,2"),
        _mk(3, 0x1008, 0x002081B3, rd=3,  rd_val=3,  disasm="add x3,x1,x2"),
        _mk(4, 0x100C, 0x02208533, rd=10, rd_val=2,  disasm="mul x10,x1,x2"),
        _mk(5, 0x1010, disasm="nop"),
        _mk(6, 0x1014, csr_writes=[{"csr":"0x300","val":"0x00000008"}],
            disasm="csrw mstatus,t0"),
        _mk(7, 0x1018, mem_op="store", mem_addr=0x80000000, mem_val=0xDEAD,
            disasm="sw x3,0(x5)"),
        _mk(8, 0x101C, privilege="M", disasm="nop"),
    ]

    def _w(name: str, entries: List[Dict]) -> None:
        p = od / name
        with open(p, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        print(f"  Wrote {p}")

    _w("rtl_pass.commitlog.jsonl", base)
    _w("iss_pass.commitlog.jsonl", base)

    fail_reg    = [dict(e) for e in base]
    fail_reg[3] = dict(fail_reg[3]); fail_reg[3]["rd_val"] = "0x00000000"
    _w("rtl_fail_reg.commitlog.jsonl", fail_reg)
    _w("iss_fail_reg.commitlog.jsonl", base)

    fail_trap = [dict(e) for e in base]
    fail_trap[4] = _mk(5, 0x1010, trap=True, trap_cause=0x2, trap_pc=0x1014,
                        disasm="(illegal instruction)")
    _w("rtl_fail_trap.commitlog.jsonl", base)
    _w("iss_fail_trap.commitlog.jsonl", fail_trap)

    fail_mem    = [dict(e) for e in base]
    fail_mem[6] = _mk(7, 0x1018, mem_op="store",
                       mem_addr=0x80000004, mem_val=0xDEAD,
                       disasm="sw x3,0(x5)")
    _w("rtl_fail_mem.commitlog.jsonl", fail_mem)
    _w("iss_fail_mem.commitlog.jsonl", base)

    print()
    print("Try:")
    print(f"  python compare_commitlogs.py "
          f"{od/'rtl_pass.commitlog.jsonl'} "
          f"{od/'iss_pass.commitlog.jsonl'}")
    print(f"  python compare_commitlogs.py "
          f"{od/'rtl_fail_reg.commitlog.jsonl'} "
          f"{od/'iss_fail_reg.commitlog.jsonl'} -o bug_report.json")


# ═══════════════════════════════════════════════════════════════════════════════
# Built-in self-test suite
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _TC:
    name:            str
    rtl:             List[Dict]
    iss:             List[Dict]
    expect_pass:     bool
    expect_type:     Optional[MismatchType] = None
    expect_field:    Optional[str]          = None
    cfg:             Optional[CompareConfig] = None


def _build_tests() -> List[_TC]:
    nop = lambda s, p: _mk(s, p)
    return [
        _TC("identical_pass",
            rtl=[nop(1,0x1000), nop(2,0x1004), nop(3,0x1008)],
            iss=[nop(1,0x1000), nop(2,0x1004), nop(3,0x1008)],
            expect_pass=True),

        _TC("pc_mismatch",
            rtl=[nop(1,0x1000), _mk(2,0x1008)],
            iss=[nop(1,0x1000), _mk(2,0x1004)],
            expect_pass=False,
            expect_type=MismatchType.PC_MISMATCH,
            expect_field="pc"),

        _TC("reg_mismatch",
            rtl=[nop(1,0x1000), _mk(2,0x1004,0x02208533,rd=10,rd_val=0,disasm="mul")],
            iss=[nop(1,0x1000), _mk(2,0x1004,0x02208533,rd=10,rd_val=2,disasm="mul")],
            expect_pass=False,
            expect_type=MismatchType.REG_MISMATCH,
            expect_field="rd_val"),

        _TC("x0_written",
            rtl=[_mk(1,0x1000,rd=0,rd_val=0xDEAD)],
            iss=[_mk(1,0x1000,rd=0,rd_val=0x0000)],
            expect_pass=False,
            expect_type=MismatchType.X0_WRITTEN),

        _TC("x0_both_zero_pass",
            rtl=[_mk(1,0x1000,rd=0,rd_val=0)],
            iss=[_mk(1,0x1000,rd=0,rd_val=0)],
            expect_pass=True),

        _TC("csr_mismatch",
            rtl=[_mk(1,0x1000,csr_writes=[{"csr":"0x300","val":"0x00000001"}])],
            iss=[_mk(1,0x1000,csr_writes=[{"csr":"0x300","val":"0x00000008"}])],
            expect_pass=False,
            expect_type=MismatchType.CSR_MISMATCH),

        _TC("csr_order_insensitive_pass",
            rtl=[_mk(1,0x1000,csr_writes=[
                {"csr":"0x341","val":"0x00001000"},
                {"csr":"0x300","val":"0x00000008"},
            ])],
            iss=[_mk(1,0x1000,csr_writes=[
                {"csr":"0x300","val":"0x00000008"},
                {"csr":"0x341","val":"0x00001000"},
            ])],
            expect_pass=True,
            cfg=CompareConfig(csr_write_order_sensitive=False)),

        _TC("mem_addr_mismatch",
            rtl=[_mk(1,0x1000,mem_op="load",mem_addr=0x2000,mem_val=0xCAFE)],
            iss=[_mk(1,0x1000,mem_op="load",mem_addr=0x2004,mem_val=0xCAFE)],
            expect_pass=False,
            expect_type=MismatchType.MEM_MISMATCH,
            expect_field="mem_addr"),

        _TC("mem_val_mismatch",
            rtl=[_mk(1,0x1000,mem_op="store",mem_addr=0x2000,mem_val=0x1)],
            iss=[_mk(1,0x1000,mem_op="store",mem_addr=0x2000,mem_val=0x2)],
            expect_pass=False,
            expect_type=MismatchType.MEM_MISMATCH,
            expect_field="mem_val"),

        _TC("align_error_halfword",
            # 2-byte load at odd address → ALIGNMENTERROR
            rtl=[_mk(1,0x1000,mem_op="load",mem_addr=0x2001,mem_val=0xAB,
                     mem_size=2)],
            iss=[_mk(1,0x1000,mem_op="load",mem_addr=0x2001,mem_val=0xAB,
                     mem_size=2)],
            expect_pass=False,
            expect_type=MismatchType.ALIGN_ERROR,
            cfg=CompareConfig(check_alignment=True)),

        _TC("align_error_word",
            # 4-byte load at 2-byte-aligned address → ALIGNMENTERROR
            rtl=[_mk(1,0x1000,mem_op="load",mem_addr=0x2002,mem_val=0xDEAD,
                     mem_size=4)],
            iss=[_mk(1,0x1000,mem_op="load",mem_addr=0x2002,mem_val=0xDEAD,
                     mem_size=4)],
            expect_pass=False,
            expect_type=MismatchType.ALIGN_ERROR),

        _TC("align_ok_byte_pass",
            # Byte accesses are always aligned
            rtl=[_mk(1,0x1000,mem_op="load",mem_addr=0x2001,mem_val=0xAB,
                     mem_size=1)],
            iss=[_mk(1,0x1000,mem_op="load",mem_addr=0x2001,mem_val=0xAB,
                     mem_size=1)],
            expect_pass=True),

        _TC("align_check_disabled_pass",
            # Misaligned but check_alignment=False
            rtl=[_mk(1,0x1000,mem_op="load",mem_addr=0x2001,mem_val=0xAB,
                     mem_size=4)],
            iss=[_mk(1,0x1000,mem_op="load",mem_addr=0x2001,mem_val=0xAB,
                     mem_size=4)],
            expect_pass=True,
            cfg=CompareConfig(check_alignment=False)),

        _TC("length_mismatch",
            rtl=[nop(1,0x1000), nop(2,0x1004)],
            iss=[nop(1,0x1000), nop(2,0x1004), nop(3,0x1008)],
            expect_pass=False,
            expect_type=MismatchType.LENGTH_MISMATCH),

        _TC("trap_mismatch",
            rtl=[_mk(1,0x1000,trap=True,trap_cause=0xB,trap_pc=0x1004)],
            iss=[_mk(1,0x1000,trap=False)],
            expect_pass=False,
            expect_type=MismatchType.TRAP_MISMATCH),

        _TC("trap_cause_mismatch",
            rtl=[_mk(1,0x1000,trap=True,trap_cause=0xB,trap_pc=0x1004)],
            iss=[_mk(1,0x1000,trap=True,trap_cause=0x2,trap_pc=0x1004)],
            expect_pass=False,
            expect_type=MismatchType.TRAP_CAUSE_MISMATCH),

        _TC("privilege_mismatch",
            rtl=[_mk(1,0x1000,privilege="M")],
            iss=[_mk(1,0x1000,privilege="U")],
            expect_pass=False,
            expect_type=MismatchType.PRIVILEGE_MISMATCH),

        _TC("xlen32_mask_pass",
            rtl=[_mk(1,0x1000,rd=1,rd_val=0xFFFFFFFF)],
            iss=[_mk(1,0x1000,rd=1,rd_val=0x00000000FFFFFFFF)],
            expect_pass=True,
            cfg=CompareConfig(xlen=32)),

        _TC("instr_mismatch",
            rtl=[_mk(1,0x1000,instr=0xDEADBEEF)],
            iss=[_mk(1,0x1000,instr=0x00000013)],
            expect_pass=False,
            expect_type=MismatchType.INSTR_MISMATCH,
            expect_field="instr"),

        _TC("ignored_csr_pass",
            rtl=[_mk(1,0x1000,csr_writes=[{"csr":"0xC00","val":"0x00000001"}])],
            iss=[_mk(1,0x1000,csr_writes=[{"csr":"0xC00","val":"0x00000002"}])],
            expect_pass=True,
            cfg=CompareConfig(ignored_csrs=frozenset({0xC00}))),

        _TC("empty_logs_pass",
            rtl=[], iss=[], expect_pass=True),

        _TC("single_commit_pass",
            rtl=[_mk(1,0x1000,rd=1,rd_val=1)],
            iss=[_mk(1,0x1000,rd=1,rd_val=1)],
            expect_pass=True),

        _TC("skip_privilege_field",
            rtl=[_mk(1,0x1000,privilege="M")],
            iss=[_mk(1,0x1000,privilege="U")],
            expect_pass=True,
            cfg=CompareConfig(skip_fields=frozenset({"privilege"}))),

        _TC("csr_mask_pass",
            rtl=[_mk(1,0x1000,csr_writes=[{"csr":"0x300","val":"0x00000009"}])],
            iss=[_mk(1,0x1000,csr_writes=[{"csr":"0x300","val":"0x00000001"}])],
            expect_pass=True,
            cfg=CompareConfig(csr_masks={0x300: 0x00000001})),

        _TC("ava_all_11_codes_present",
            # Meta-test: verify all 11 AVA canonical codes are in the enum
            rtl=[], iss=[], expect_pass=True),

        _TC("seq_gap_detected",
            # step jumps from 1 to 3 (gap of 1)
            rtl=[nop(1,0x1000), _mk(3,0x1008)],
            iss=[nop(1,0x1000), _mk(3,0x1008)],
            expect_pass=False,
            expect_type=MismatchType.SEQ_GAP),
    ]


def _tmp_jsonl(entries: List[Dict]) -> str:
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def run_self_tests(verbose: bool = False) -> bool:
    # ── AVA contract meta-check (no file I/O needed) ─────────────────────────
    _AVA_REQUIRED = {
        "PCMISMATCH", "REGMISMATCH", "CSRMISMATCH", "MEMMISMATCH",
        "TRAPMISMATCH", "LENGTHMISMATCH", "SEQGAP", "X0WRITTEN",
        "ALIGNMENTERROR", "SCHEMAINVALID", "BINARYHASHMISMATCH",
    }
    _implemented = {m.value for m in MismatchType}
    _missing     = _AVA_REQUIRED - _implemented

    cases  = _build_tests()
    passed = failed = errors = 0
    W = 60

    print(f"\n{'═'*W}")
    print(f"  compare_commitlogs.py v{__version__} — self-test ({len(cases)} cases)")
    print(f"{'═'*W}")

    # Print AVA contract check first
    if _missing:
        print(f"  ✗  AVA contract: missing codes: {sorted(_missing)}")
        failed += 1
    else:
        print(f"  ✓  AVA contract: all 11 canonical codes implemented")
        passed += 1

    for tc in cases:
        # The meta-test is handled above; skip the placeholder entry
        if tc.name == "ava_all_11_codes_present":
            continue

        rp = ip = ""
        try:
            rp = _tmp_jsonl(tc.rtl)
            ip = _tmp_jsonl(tc.iss)
            result = compare(rp, ip, cfg=tc.cfg or CompareConfig())

            ok  = True
            why = ""
            if result.passed != tc.expect_pass:
                ok  = False
                why = (f"expected passed={tc.expect_pass} "
                       f"got passed={result.passed}")
            elif not tc.expect_pass:
                if not result.mismatches:
                    ok  = False
                    why = "no mismatches returned"
                elif tc.expect_type and result.mismatches[0].mismatch_type != tc.expect_type:
                    ok  = False
                    why = (f"expected {tc.expect_type.value} "
                           f"got {result.mismatches[0].mismatch_type.value}")
                elif (tc.expect_field is not None
                      and result.mismatches[0].differing_field != tc.expect_field):
                    ok  = False
                    why = (f"expected field={tc.expect_field!r} "
                           f"got {result.mismatches[0].differing_field!r}")

            if ok:
                print(f"  ✓  {tc.name}")
                if verbose and result.mismatches:
                    for m in result.mismatches:
                        print(f"       {m.to_ava_bug()}")
                passed += 1
            else:
                print(f"  ✗  {tc.name}  — {why}")
                failed += 1

        except Exception as exc:
            print(f"  ✗  {tc.name}  — EXCEPTION: {exc}")
            if verbose:
                import traceback; traceback.print_exc()
            errors += 1
        finally:
            for p in (rp, ip):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    print(f"{'─'*W}")
    print(f"  Results: {passed} passed, {failed} failed, {errors} errors")
    print(f"{'═'*W}\n")
    return failed == 0 and errors == 0


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="compare_commitlogs.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(f"""\
            compare_commitlogs.py v{__version__} — Agent D: Comparator & Triage
            =====================================================================
            Stream-compare RTL vs ISS commit logs (JSONL), classify divergences,
            and emit structured artefacts for the AVA verification pipeline.
            Exit: 0=PASS  1=MISMATCH  2=ERROR
        """),
        epilog=textwrap.dedent("""\
            Examples
            --------
              python compare_commitlogs.py rtl.jsonl iss.jsonl
              python compare_commitlogs.py rtl.jsonl iss.jsonl \\
                  --seed 42 --xlen 32 --window 32 \\
                  --bug-report bug.json --junit ci.xml --markdown report.md
              python compare_commitlogs.py --all --max-mismatches 50 rtl.jsonl iss.jsonl
              python compare_commitlogs.py --batch manifest.yaml
              python compare_commitlogs.py --self-test
        """),
    )

    p.add_argument("rtl_log", nargs="?", default=None,
                   help="RTL commit log (JSONL, or - for stdin)")
    p.add_argument("iss_log", nargs="?", default=None,
                   help="ISS/golden commit log (JSONL)")

    rv = p.add_argument_group("RISC-V")
    rv.add_argument("--xlen", type=int, default=32, choices=[32, 64])

    cmp = p.add_argument_group("Comparison rules")
    cmp.add_argument("--skip-fields", nargs="+", default=[], metavar="FIELD")
    cmp.add_argument("--ignore-csrs", nargs="+", default=[], metavar="ADDR")
    cmp.add_argument("--no-x0-check", action="store_true")
    cmp.add_argument("--no-align-check", action="store_true",
                     help="Disable ALIGNMENTERROR checks for unaligned memory accesses")
    cmp.add_argument("--csr-order-sensitive", action="store_true")
    cmp.add_argument("--strict", action="store_true",
                     help="Abort on step-number discontinuities")

    mode = p.add_argument_group("Mode")
    mode.add_argument("--all", action="store_true",
                      help="Collect all mismatches (not just the first)")
    mode.add_argument("--max-mismatches", type=int, default=1, metavar="N",
                      help="Stop after N mismatches (0=unlimited, default 1)")
    mode.add_argument("--max-parse-errors", type=int, default=10, metavar="N")
    mode.add_argument("--window", "-w", type=int, default=32, metavar="N")

    rep = p.add_argument_group("Reproduction metadata")
    rep.add_argument("--seed", type=int, default=None)
    rep.add_argument("--rtl-bin", default=None, metavar="PATH")
    rep.add_argument("--iss-bin", default=None, metavar="PATH")

    out = p.add_argument_group("Output")
    out.add_argument("--bug-report", "-o", default=None, metavar="FILE")
    out.add_argument("--junit", default=None, metavar="FILE")
    out.add_argument("--markdown", default=None, metavar="FILE")
    out.add_argument("--sarif", default=None, metavar="FILE")
    out.add_argument("--github-annotations", action="store_true")
    out.add_argument("--quiet", "-q", action="store_true")
    out.add_argument("--verbose", "-v", action="store_true")
    out.add_argument("--no-color", action="store_true")
    out.add_argument("--log-level", default="WARNING",
                     choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sp = p.add_argument_group("Special modes")
    sp.add_argument("--self-test", action="store_true")
    sp.add_argument("--generate-sample-logs", action="store_true")
    sp.add_argument("--sample-dir", default=".", metavar="DIR")
    sp.add_argument("--batch", default=None, metavar="MANIFEST",
                    help="Run batch comparison from YAML/JSON manifest file")
    sp.add_argument("--manifest", default=None, metavar="MANIFEST.json",
                    type=Path,
                    help=(
                        "AVA contract mode: read rundir/seed/bins from a manifest JSON, "
                        "run comparison, write bugreport.json, update manifest. "
                        "Exit 0=pass 1=mismatch 2=infra-error 3=config-error."
                    ))

    return p


def _build_cfg(args: argparse.Namespace) -> CompareConfig:
    ignored: FrozenSet[int] = frozenset(
        int(a, 16) if a.lower().startswith("0x") else int(a)
        for a in (args.ignore_csrs or [])
    )
    if args.all:
        max_m = args.max_mismatches if args.max_mismatches > 0 else 0
    else:
        max_m = 1
    return CompareConfig(
        xlen                      = args.xlen,
        skip_fields               = frozenset(args.skip_fields or []),
        ignored_csrs              = ignored,
        enforce_x0_invariant      = not args.no_x0_check,
        csr_write_order_sensitive = args.csr_order_sensitive,
        strict_steps              = args.strict,
        check_alignment           = not args.no_align_check,
        max_parse_errors          = args.max_parse_errors,
        max_mismatches            = max_m,
        stop_on_first             = not args.all,
        window                    = args.window,
    )


def _write(path: str, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _emit_outputs(result: CompareResult, args: argparse.Namespace) -> None:
    if args.bug_report:
        _write(args.bug_report,
               json.dumps(result.to_bug_report(), indent=2, default=str))
        if not args.quiet:
            print(f"Bug report  → {args.bug_report}")
    if args.junit:
        _write(args.junit, result.to_junit_xml())
        if not args.quiet:
            print(f"JUnit XML   → {args.junit}")
    if args.markdown:
        _write(args.markdown, result.to_markdown())
        if not args.quiet:
            print(f"Markdown    → {args.markdown}")
    if args.sarif:
        _write(args.sarif, json.dumps(result.to_sarif(), indent=2))
        if not args.quiet:
            print(f"SARIF       → {args.sarif}")
    if args.github_annotations:
        for m in result.mismatches:
            print(m.to_github_annotation(filename=args.rtl_log or "commitlog"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args   = parser.parse_args(argv)

    logging.basicConfig(
        level  = getattr(logging, args.log_level),
        format = "%(levelname)s: %(message)s",
        stream = sys.stderr,
    )

    # ── special modes ─────────────────────────────────────────────────────────
    if args.self_test:
        return EXIT_PASS if run_self_tests(verbose=args.verbose) else EXIT_MISMATCH

    if args.generate_sample_logs:
        print("Generating sample logs …")
        generate_sample_logs(output_dir=args.sample_dir)
        return EXIT_PASS

    # ── AVA manifest mode ────────────────────────────────────────────────────
    if args.manifest:
        cfg = None
        # Pass through any explicitly-set comparison flags to manifest mode
        if any([args.skip_fields, args.ignore_csrs, args.no_x0_check,
                args.no_align_check, args.csr_order_sensitive, args.strict]):
            try:
                cfg = _build_cfg(args)
            except (ValueError, TypeError) as exc:
                _log.error("Config error: %s", exc)
                return EXIT_CONFIG
        return main_manifest(args.manifest, base_cfg=cfg)

    # ── batch mode ────────────────────────────────────────────────────────────
    if args.batch:
        return _run_batch(args)

    # ── single-pair comparison ────────────────────────────────────────────────
    if not args.rtl_log or not args.iss_log:
        parser.error(
            "rtl_log and iss_log are required "
            "(or use --self-test / --generate-sample-logs / --batch / --manifest)"
        )

    try:
        cfg = _build_cfg(args)
    except (ValueError, TypeError) as exc:
        _log.error("Configuration error: %s", exc)
        return EXIT_CONFIG

    try:
        result = compare(
            args.rtl_log, args.iss_log,
            cfg=cfg, seed=args.seed,
            rtl_bin=args.rtl_bin, iss_bin=args.iss_bin,
        )
    except ConfigError as exc:
        _log.error("Configuration error: %s", exc)
        return EXIT_CONFIG
    except (LogFormatError, ParseError) as exc:
        _log.error("Infrastructure error: %s", exc)
        return EXIT_INFRA
    except StepError as exc:
        _log.error("Step discontinuity (strict mode): %s", exc)
        return EXIT_INFRA
    except Exception as exc:
        _log.exception("Unexpected error: %s", exc)
        return EXIT_INFRA

    use_color = not args.no_color and sys.stdout.isatty()
    if not args.quiet:
        print_summary(result, use_color=use_color, verbose=args.verbose)

    _emit_outputs(result, args)
    return EXIT_PASS if result.passed else EXIT_MISMATCH


def _run_batch(args: argparse.Namespace) -> int:
    try:
        entries = _load_manifest(args.batch)
    except Exception as exc:
        _log.error("Manifest error: %s", exc)
        return EXIT_INFRA

    base_cfg = _build_cfg(args)
    results  = compare_logs_batch(entries, base_cfg=base_cfg)
    n_pass   = sum(1 for r in results if r.passed)
    n_fail   = len(results) - n_pass
    use_color = not args.no_color and sys.stdout.isatty()
    out_dir  = Path(args.bug_report or ".")

    print(f"\nBatch: {n_pass}/{len(results)} PASS, {n_fail} FAIL\n")
    for entry, result in zip(entries, results):
        label  = entry.label or "run"
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {label}")
        if not result.passed and not args.quiet:
            print_summary(result, use_color=use_color)
        if args.bug_report and not result.passed:
            _write(str(out_dir / f"{label}_bug_report.json"),
                   json.dumps(result.to_bug_report(), indent=2, default=str))

    return EXIT_PASS if n_fail == 0 else EXIT_MISMATCH


# ═══════════════════════════════════════════════════════════════════════════════
# Public importable API
# ═══════════════════════════════════════════════════════════════════════════════

def compare_logs(
    rtl_path:      str,
    iss_path:      str,
    *,
    cfg:           Optional[CompareConfig] = None,
    seed:          Optional[int]           = None,
    rtl_bin:       Optional[str]           = None,
    iss_bin:       Optional[str]           = None,
    xlen:          int                     = 32,
    window:        int                     = 32,
    stop_on_first: bool                    = True,
) -> CompareResult:
    """Primary importable API for the AVA verification pipeline.

    Minimal usage::

        from compare_commitlogs import compare_logs
        result = compare_logs("rtl.commitlog.jsonl", "iss.commitlog.jsonl", seed=42)
        verification_result.bugs.extend(result.bugs)
        if not result.passed:
            Path("bug_report.json").write_text(
                json.dumps(result.to_bug_report(), indent=2))

    Parameters
    ----------
    rtl_path      : path to RTL JSONL commit log
    iss_path      : path to ISS/golden JSONL commit log
    cfg           : full CompareConfig (takes precedence over keyword shortcuts)
    seed          : RNG seed used to generate the test
    rtl_bin       : RTL simulator binary path
    iss_bin       : ISS binary path (e.g. spike)
    xlen          : RISC-V word width 32|64 — ignored if cfg is given
    window        : context window depth — ignored if cfg is given
    stop_on_first : stop after first mismatch — ignored if cfg is given
    """
    if cfg is None:
        cfg = CompareConfig(xlen=xlen, window=window, stop_on_first=stop_on_first)
    return compare(rtl_path, iss_path, cfg=cfg, seed=seed,
                   rtl_bin=rtl_bin, iss_bin=iss_bin)


if __name__ == "__main__":
    sys.exit(main())

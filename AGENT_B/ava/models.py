"""
ava/models.py — Shared dataclasses used across the entire AVA platform.
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Commit log entry (mirrors commitlog.schema.json)
# ---------------------------------------------------------------------------

@dataclass
class MemOp:
    op:   str   # "load" | "store"
    addr: int
    data: int
    size: int   # bytes: 1/2/4/8

    def to_dict(self) -> dict:
        return {
            "op":   self.op,
            "addr": hex(self.addr),
            "data": hex(self.data),
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemOp":
        return cls(
            op=d["op"],
            addr=int(d["addr"], 16),
            data=int(d["data"], 16),
            size=d["size"],
        )


@dataclass
class TrapInfo:
    cause:  str
    tval:   int = 0
    is_ret: bool = False

    def to_dict(self) -> dict:
        return {"cause": self.cause, "tval": hex(self.tval), "is_ret": self.is_ret}

    @classmethod
    def from_dict(cls, d: dict) -> "TrapInfo":
        return cls(cause=d["cause"], tval=int(d.get("tval", "0x0"), 16),
                   is_ret=d.get("is_ret", False))


@dataclass
class CommitEntry:
    seq:   int
    pc:    int
    instr: int
    mode:  str = "M"                         # M / S / U
    rd:    Dict[str, int] = field(default_factory=dict)   # {reg: value}
    csr:   Dict[str, int] = field(default_factory=dict)   # {name: value}
    mem:   List[MemOp]    = field(default_factory=list)
    trap:  Optional[TrapInfo] = None

    def to_dict(self) -> dict:
        d: dict = {
            "seq":   self.seq,
            "pc":    hex(self.pc),
            "instr": hex(self.instr),
            "mode":  self.mode,
        }
        if self.rd:
            d["rd"]  = {k: hex(v) for k, v in self.rd.items()}
        if self.csr:
            d["csr"] = {k: hex(v) for k, v in self.csr.items()}
        if self.mem:
            d["mem"] = [m.to_dict() for m in self.mem]
        if self.trap:
            d["trap"] = self.trap.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CommitEntry":
        return cls(
            seq=d["seq"],
            pc=int(d["pc"], 16),
            instr=int(d["instr"], 16),
            mode=d.get("mode", "M"),
            rd={k: int(v, 16) for k, v in d.get("rd", {}).items()},
            csr={k: int(v, 16) for k, v in d.get("csr", {}).items()},
            mem=[MemOp.from_dict(m) for m in d.get("mem", [])],
            trap=TrapInfo.from_dict(d["trap"]) if "trap" in d else None,
        )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

@dataclass
class CoverageReport:
    line:       float = 0.0   # 0.0 – 1.0
    branch:     float = 0.0
    toggle:     float = 0.0
    functional: float = 0.0
    raw:        Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "branch": self.branch,
            "toggle": self.toggle,
            "functional": self.functional,
        }


# ---------------------------------------------------------------------------
# Bug / divergence
# ---------------------------------------------------------------------------

@dataclass
class BugReport:
    bug_id:      str
    seq:         int
    mismatch_class: str   # pc | reg | csr | mem | trap | unknown
    rtl_entry:   Optional[CommitEntry]
    iss_entry:   Optional[CommitEntry]
    context_before: List[CommitEntry] = field(default_factory=list)  # up to 8 prior
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "bug_id":    self.bug_id,
            "seq":       self.seq,
            "mismatch_class": self.mismatch_class,
            "description": self.description,
            "rtl_entry": self.rtl_entry.to_dict() if self.rtl_entry else None,
            "iss_entry": self.iss_entry.to_dict() if self.iss_entry else None,
            "context_before": [e.to_dict() for e in self.context_before],
        }


# ---------------------------------------------------------------------------
# Backend results
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    commitlog_path:       Path
    coverage_path:        Optional[Path] = None
    waveform_path:        Optional[Path] = None
    exit_code:            int = 0
    instructions_retired: int = 0
    error_message:        str = ""


# ---------------------------------------------------------------------------
# Comparator result
# ---------------------------------------------------------------------------

@dataclass
class CompareResult:
    match:                  bool
    first_divergence_seq:   Optional[int]
    mismatch_class:         Optional[str]
    bugs:                   List[BugReport] = field(default_factory=list)
    rtl_insns:              int = 0
    iss_insns:              int = 0


# ---------------------------------------------------------------------------
# Top-level verification result
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    run_id:      str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status:      str = "pending"   # pass | fail | error | timeout
    bugs:        List[BugReport]   = field(default_factory=list)
    coverage:    Optional[CoverageReport] = None
    compare:     Optional[CompareResult]  = None
    run_dir:     Optional[Path]           = None
    notes:       str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"

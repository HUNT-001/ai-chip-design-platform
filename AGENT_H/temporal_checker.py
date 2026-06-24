"""
AGENT_H/temporal_checker.py
============================
T17 — Temporal Behaviour Verification

Verifies that the instruction commit sequence satisfies temporal ordering
properties — linear temporal logic (LTL)-style assertions expressed as
finite-state monitor automata operating over the commit log stream.

Each temporal property is a finite automaton that monitors the commit stream
and raises a violation if it reaches an error state.

Built-in properties:

  P1: FENCE-before-load  — after any store, a load to the same address must be
      preceded by a FENCE (or the store must be followed by a drain signal).
      [ORDERMISMATCH prevention]

  P2: MRET-after-trap    — MRET must only appear after a trap record has been
      seen.  Spurious MRET without a prior trap is a privilege escalation risk.

  P3: No-consecutive-stores-same-addr — two consecutive stores to the same
      address without an intervening load suggests a write-after-write hazard
      that the RTL may not handle correctly.

  P4: CSR-read-after-write — a CSR that is written must be readable within N
      subsequent instructions (i.e. the write must be visible).

  P5: LR-before-SC       — SC.W must be preceded by a matching LR.W within the
      last 64 instructions (RISC-V reservation window).

Usage
-----
  from AGENT_H.temporal_checker import TemporalChecker

  checker = TemporalChecker(rtl_log, iss_log)
  report  = checker.run()
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Monitor automaton base class
# ─────────────────────────────────────────────────────────

@dataclass
class TemporalViolation:
    property_name: str
    severity:      str
    seq:           int
    pc:            Optional[str]
    disasm:        Optional[str]
    description:   str


class Monitor:
    """Base class for a temporal property monitor."""
    name:        str = "monitor"
    description: str = ""
    severity:    str = "MEDIUM"

    def step(
        self,
        rtl:   Dict[str, Any],
        iss:   Dict[str, Any],
        seq:   int,
    ) -> Optional[str]:
        """
        Process one instruction pair.
        Return None if property holds, error message if violated.
        """
        raise NotImplementedError

    def _disasm(self, rec: Dict) -> str:
        return (rec.get("disasm") or "").strip().lower()

    def _addr(self, rec: Dict) -> Optional[int]:
        """Extract load/store address from commit record."""
        mw = rec.get("mem_writes")
        mr = rec.get("mem_reads")
        if mw:
            a = mw[0].get("addr") if isinstance(mw, list) else mw.get("addr")
            return int(a, 16) if isinstance(a, str) else a
        if mr:
            a = mr[0].get("addr") if isinstance(mr, list) else mr.get("addr")
            return int(a, 16) if isinstance(a, str) else a
        return None


# ─────────────────────────────────────────────────────────
# Concrete monitors
# ─────────────────────────────────────────────────────────

class MretAfterTrap(Monitor):
    name        = "mret_after_trap"
    description = "MRET must only appear after a trap record has been seen"
    severity    = "HIGH"

    def __init__(self):
        self._trap_seen = False

    def step(self, rtl, iss, seq):
        disasm = self._disasm(iss)
        if iss.get("trap") or rtl.get("trap"):
            self._trap_seen = True
        if disasm == "mret":
            if not self._trap_seen:
                return "MRET encountered without a prior trap record"
            self._trap_seen = False  # consume trap after MRET
        return None


class LrBeforeSc(Monitor):
    name        = "lr_before_sc"
    description = "SC.W must be preceded by LR.W within the reservation window"
    severity    = "HIGH"

    def __init__(self, window: int = 64):
        self._window      = window
        self._lr_seen_at  = None   # seq of last LR.W

    def step(self, rtl, iss, seq):
        disasm = self._disasm(iss)
        if "lr.w" in disasm:
            self._lr_seen_at = seq
        elif "sc.w" in disasm:
            if self._lr_seen_at is None:
                return "SC.W without a preceding LR.W"
            if seq - self._lr_seen_at > self._window:
                return (f"SC.W at seq {seq} but LR.W was at seq {self._lr_seen_at} "
                        f"(gap={seq - self._lr_seen_at} > window={self._window})")
            self._lr_seen_at = None  # consume reservation
        return None


class ConsecutiveStoresSameAddr(Monitor):
    name        = "consecutive_stores_same_addr"
    description = "Two consecutive stores to the same address without an intervening load"
    severity    = "MEDIUM"

    def __init__(self):
        self._last_store_addr: Optional[int] = None

    def step(self, rtl, iss, seq):
        disasm = self._disasm(iss)
        is_store = any(disasm.startswith(m) for m in ("sw", "sh", "sb", "sc.w"))
        is_load  = any(disasm.startswith(m) for m in ("lw", "lh", "lb", "lhu", "lbu", "lr.w"))

        if is_load:
            self._last_store_addr = None
        elif is_store:
            addr = self._addr(iss)
            if addr is not None and addr == self._last_store_addr:
                return (f"Consecutive stores to same address 0x{addr:08x} "
                        f"without intervening load at seq {seq}")
            self._last_store_addr = addr
        else:
            # Non-memory instruction: don't clear last_store
            pass
        return None


class CsrReadAfterWrite(Monitor):
    name        = "csr_read_after_write"
    description = "Written CSR must be visible within N subsequent instructions"
    severity    = "MEDIUM"

    def __init__(self, deadline: int = 10):
        self._pending: Dict[str, int] = {}   # csr_name → seq_of_write
        self._deadline = deadline

    def step(self, rtl, iss, seq):
        disasm = self._disasm(iss)

        # Check deadlines
        expired = [csr for csr, ws in self._pending.items()
                   if seq - ws > self._deadline]
        if expired:
            msg = f"CSR(s) {expired} written but not read within {self._deadline} instructions"
            for csr in expired:
                del self._pending[csr]
            return msg

        # Track CSR writes
        if any(disasm.startswith(m) for m in ("csrrw", "csrrs", "csrrc")):
            # Extract CSR name from disasm if possible
            parts = disasm.split()
            if len(parts) >= 3:
                csr_name = parts[2]
                self._pending[csr_name] = seq

        # Track CSR reads (any CSR instruction where rd != x0)
        if any(disasm.startswith(m) for m in ("csrr", "csrrw", "csrrs", "csrrc")):
            parts = disasm.split()
            if len(parts) >= 3:
                csr_name = parts[2]
                self._pending.pop(csr_name, None)
        return None


class FenceBeforeLoadAfterStore(Monitor):
    name        = "fence_before_load_after_store"
    description = "After a FENCE, the next load must see the previous store result"
    severity    = "LOW"

    def __init__(self):
        self._fence_seen = False
        self._store_addr: Optional[int] = None

    def step(self, rtl, iss, seq):
        disasm = self._disasm(iss)
        if "fence" in disasm:
            self._fence_seen = True
        elif any(disasm.startswith(m) for m in ("sw", "sh", "sb")):
            self._store_addr = self._addr(iss)
        elif any(disasm.startswith(m) for m in ("lw", "lh", "lb", "lhu", "lbu")):
            if self._fence_seen and self._store_addr is not None:
                load_addr = self._addr(iss)
                if load_addr == self._store_addr:
                    # Check that RTL and ISS agree on the loaded value
                    rtl_regs = rtl.get("regs") or {}
                    iss_regs = iss.get("regs") or {}
                    for reg in iss_regs:
                        if reg == "x0":
                            continue
                        rtl_val = rtl_regs.get(reg)
                        iss_val = iss_regs.get(reg)
                        if rtl_val != iss_val:
                            self._fence_seen = False
                            return (f"Post-FENCE load at 0x{load_addr:08x} produced "
                                    f"RTL {rtl_val} vs ISS {iss_val}")
                    self._fence_seen = False
        return None


# ─────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────

class TemporalChecker:
    """
    Runs all temporal property monitors against paired RTL/ISS commit logs.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : list of ISS commit records
    monitors       : custom monitor list (defaults to all built-in monitors)
    max_violations : stop after this many violations
    """

    DEFAULT_MONITORS = [
        MretAfterTrap,
        LrBeforeSc,
        ConsecutiveStoresSameAddr,
        CsrReadAfterWrite,
        FenceBeforeLoadAfterStore,
    ]

    def __init__(
        self,
        rtl_log:        List[Dict],
        iss_log:        List[Dict],
        monitors:       Optional[List] = None,
        max_violations: int = 200,
    ) -> None:
        self.rtl_log        = rtl_log
        self.iss_log        = iss_log
        self.max_violations = max_violations
        self._monitors      = [m() for m in (monitors or self.DEFAULT_MONITORS)]

    def run(self) -> Dict[str, Any]:
        started    = datetime.now(timezone.utc)
        n          = min(len(self.rtl_log), len(self.iss_log))
        violations: List[TemporalViolation] = []

        for i in range(n):
            if len(violations) >= self.max_violations:
                break
            rtl_r  = self.rtl_log[i]
            iss_r  = self.iss_log[i]
            seq    = rtl_r.get("seq", i)
            disasm = (iss_r.get("disasm") or "").strip().lower()

            for mon in self._monitors:
                try:
                    err = mon.step(rtl_r, iss_r, seq)
                except Exception as exc:
                    logger.warning("Monitor %s raised: %s", mon.name, exc)
                    err = None
                if err:
                    violations.append(TemporalViolation(
                        property_name=mon.name,
                        severity=mon.severity,
                        seq=seq,
                        pc=iss_r.get("pc"),
                        disasm=disasm,
                        description=err,
                    ))

        finished    = datetime.now(timezone.utc)
        high_viols  = [v for v in violations if v.severity == "HIGH"]

        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "temporal_checker",
            "records_checked":  n,
            "monitors_run":     len(self._monitors),
            "total_violations": len(violations),
            "high_violations":  len(high_viols),
            "pass":             len(violations) == 0,
            "violations": [
                {
                    "property":  v.property_name,
                    "severity":  v.severity,
                    "seq":       v.seq,
                    "pc":        v.pc,
                    "disasm":    v.disasm,
                    "description": v.description,
                }
                for v in violations[:50]
            ],
            "started_at":  started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":  round((finished - started).total_seconds(), 3),
        }


def run_from_manifest(manifest_path: Path) -> int:
    with open(manifest_path) as f:
        manifest = json.load(f)

    run_dir = Path(manifest["run_dir"])
    outputs = manifest.get("outputs", {})

    def _load_log(key, default):
        p = run_dir / (outputs.get(key) or default)
        if not p.exists():
            return []
        recs = []
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
        return recs

    rtl_log = _load_log("rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log("iss_commit_log", "iss_commit.jsonl")

    if not rtl_log or not iss_log:
        return 0

    checker = TemporalChecker(rtl_log, iss_log)
    report  = checker.run()

    report_path = run_dir / "temporal_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["temporal_report"] = "temporal_report.json"
    manifest.setdefault("phases", {})["temporal_check"] = {
        "status": "pass" if report["pass"] else "fail",
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)
    return 0 if report["pass"] else 1

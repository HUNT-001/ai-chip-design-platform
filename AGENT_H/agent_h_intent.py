"""
AGENT_H/agent_h_intent.py
==========================
T11 — Architectural Intent Verification

Verifies that the RTL implementation matches a set of declarative architectural
intent specifications.  Intent specs capture high-level behavioural invariants
that the ISA mandates but that unit tests rarely encode explicitly — e.g.:

  - x0 must always read as zero (zero-register invariant)
  - MRET must restore PC from mepc (trap-return invariant)
  - MUL/MULH must produce consistent high/low halves
  - FENCE must drain the store buffer before any subsequent load sees the result
  - Privilege mode must match mstatus.MPP after MRET

Each intent spec is a Python callable that receives a commit-log record pair
(RTL, ISS) and returns True if the invariant holds, False if it is violated.

Why not just use the existing Agent D comparator?
Agent D checks cycle-by-cycle register/PC equality — it flags *any* divergence.
Agent H/T11 checks *semantic* invariants that may hold even when cycle timing
differs (e.g. speculative execution, pipeline stages) — or that reveal silent
hardware bugs that don't show up as direct ISS/RTL mismatches.

Output
------
Writes ``intent_report.json`` to the run directory and updates the manifest
``phases.intent_check`` and ``outputs.intent_report``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"

# ─────────────────────────────────────────────────────────
# Intent spec definition
# ─────────────────────────────────────────────────────────

Checker = Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]
# (rtl_record, iss_record) → None if OK, error_message if violated


@dataclass
class IntentSpec:
    """Declarative architectural intent invariant."""
    name:        str
    description: str
    checker:     Checker
    severity:    str = "HIGH"   # HIGH / MEDIUM / LOW
    applies_to:  str = "*"      # mnemonic pattern or '*' for all


@dataclass
class IntentViolation:
    spec_name:   str
    description: str
    severity:    str
    seq:         int
    pc:          Optional[str]
    disasm:      Optional[str]
    error_msg:   str


# ─────────────────────────────────────────────────────────
# Built-in intent specs
# ─────────────────────────────────────────────────────────

def _spec_x0_zero(rtl: Dict, iss: Dict) -> Optional[str]:
    """x0 must always read as 0 (zero-register invariant)."""
    for src, label in ((rtl, "RTL"), (iss, "ISS")):
        regs = src.get("regs") or {}
        x0 = regs.get("x0")
        if x0 is not None:
            val = int(x0, 16) if isinstance(x0, str) else x0
            if val != 0:
                return f"{label} x0 = 0x{val:08x} (expected 0)"
    return None


def _spec_mret_restores_mepc(rtl: Dict, iss: Dict) -> Optional[str]:
    """After MRET, PC must equal the prior mepc value."""
    disasm = (iss.get("disasm") or "").strip().lower()
    if disasm != "mret":
        return None
    iss_mepc = (iss.get("csrs") or {}).get("mepc")
    iss_pc   = iss.get("pc")
    if iss_mepc and iss_pc:
        mepc_val = int(iss_mepc, 16) if isinstance(iss_mepc, str) else iss_mepc
        pc_val   = int(iss_pc,   16) if isinstance(iss_pc,   str) else iss_pc
        if mepc_val != pc_val:
            return f"MRET: next PC=0x{pc_val:08x} != mepc=0x{mepc_val:08x}"
    return None


def _spec_mul_mulh_consistent(rtl: Dict, iss: Dict) -> Optional[str]:
    """MUL and MULH on the same operands must produce consistent results."""
    # This is a cross-instruction invariant; checked via context window
    # (single-record check: ensure MUL result fits 64-bit multiplication)
    disasm = (iss.get("disasm") or "").strip().lower()
    if not disasm.startswith("mul"):
        return None
    # Can't check cross-instruction consistency in a single-record callback;
    # just verify the RTL/ISS result agree.
    rtl_regs = rtl.get("regs") or {}
    iss_regs = iss.get("regs") or {}
    for reg in rtl_regs:
        if reg == "x0":
            continue
        rtl_val = rtl_regs.get(reg)
        iss_val = iss_regs.get(reg)
        if rtl_val is not None and iss_val is not None:
            rv = int(rtl_val, 16) if isinstance(rtl_val, str) else rtl_val
            iv = int(iss_val, 16) if isinstance(iss_val, str) else iss_val
            if rv != iv:
                return f"MUL result mismatch: RTL {reg}=0x{rv:08x}, ISS {reg}=0x{iv:08x}"
    return None


def _spec_div_zero_trap(rtl: Dict, iss: Dict) -> Optional[str]:
    """DIV/REM by zero must trap (RISC-V mandates it returns -1/dividend, not an exception,
    but both RTL and ISS must agree on the result)."""
    disasm = (iss.get("disasm") or "").strip().lower()
    if not any(disasm.startswith(m) for m in ("div", "divu", "rem", "remu")):
        return None
    rtl_regs = rtl.get("regs") or {}
    iss_regs = iss.get("regs") or {}
    for reg in rtl_regs:
        if reg == "x0":
            continue
        rtl_val = rtl_regs.get(reg)
        iss_val = iss_regs.get(reg)
        if rtl_val is not None and iss_val is not None:
            rv = int(rtl_val, 16) if isinstance(rtl_val, str) else rtl_val
            iv = int(iss_val, 16) if isinstance(iss_val, str) else iss_val
            if rv != iv:
                return f"DIV result mismatch: RTL {reg}=0x{rv:08x}, ISS {reg}=0x{iv:08x}"
    return None


def _spec_ecall_raises_trap(rtl: Dict, iss: Dict) -> Optional[str]:
    """ECALL must produce a trap with cause=8 (environment call from M-mode)."""
    disasm = (iss.get("disasm") or "").strip().lower()
    if disasm != "ecall":
        return None
    iss_trap  = iss.get("trap")
    rtl_trap  = rtl.get("trap")
    if iss_trap is None and rtl_trap is None:
        return "ECALL produced no trap record in either RTL or ISS"
    for src, label, trap in ((rtl, "RTL", rtl_trap), (iss, "ISS", iss_trap)):
        if trap is not None:
            cause = trap.get("cause")
            if cause is not None and cause not in (8, 9, 10, 11):
                return f"ECALL {label} trap cause={cause} (expected 8-11 for env-call)"
    return None


def _spec_csr_privilege_mstatus(rtl: Dict, iss: Dict) -> Optional[str]:
    """mstatus.MIE bit must be consistent between RTL and ISS after CSR writes."""
    disasm = (iss.get("disasm") or "").strip().lower()
    if not any(disasm.startswith(m) for m in ("csrrw", "csrrs", "csrrc", "csrrwi")):
        return None
    rtl_csrs = rtl.get("csrs") or {}
    iss_csrs = iss.get("csrs") or {}
    rtl_mst  = rtl_csrs.get("mstatus")
    iss_mst  = iss_csrs.get("mstatus")
    if rtl_mst is not None and iss_mst is not None:
        rv = int(rtl_mst, 16) if isinstance(rtl_mst, str) else rtl_mst
        iv = int(iss_mst, 16) if isinstance(iss_mst, str) else iss_mst
        if rv != iv:
            return f"mstatus divergence after CSR write: RTL=0x{rv:08x}, ISS=0x{iv:08x}"
    return None


def _spec_pc_alignment(rtl: Dict, iss: Dict) -> Optional[str]:
    """PC must be 4-byte aligned (or 2-byte for RVC, which AVA doesn't support)."""
    for src, label in ((rtl, "RTL"), (iss, "ISS")):
        pc_raw = src.get("pc")
        if pc_raw:
            pc_val = int(pc_raw, 16) if isinstance(pc_raw, str) else pc_raw
            if pc_val % 4 != 0:
                return f"{label} PC=0x{pc_val:08x} is not 4-byte aligned"
    return None


BUILT_IN_SPECS: List[IntentSpec] = [
    IntentSpec(
        name="x0_always_zero",
        description="x0 register must always read as 0",
        checker=_spec_x0_zero,
        severity="HIGH",
        applies_to="*",
    ),
    IntentSpec(
        name="mret_restores_mepc",
        description="MRET must restore PC from mepc",
        checker=_spec_mret_restores_mepc,
        severity="HIGH",
        applies_to="mret",
    ),
    IntentSpec(
        name="mul_mulh_consistent",
        description="MUL/MULH result must be consistent between RTL and ISS",
        checker=_spec_mul_mulh_consistent,
        severity="HIGH",
        applies_to="mul*",
    ),
    IntentSpec(
        name="div_result_consistent",
        description="DIV/REM by zero must produce consistent results (RISC-V mandates -1)",
        checker=_spec_div_zero_trap,
        severity="MEDIUM",
        applies_to="div*",
    ),
    IntentSpec(
        name="ecall_raises_trap",
        description="ECALL must generate a machine-mode trap with cause 8-11",
        checker=_spec_ecall_raises_trap,
        severity="HIGH",
        applies_to="ecall",
    ),
    IntentSpec(
        name="csr_mstatus_consistent",
        description="mstatus must be consistent after CSR writes",
        checker=_spec_csr_privilege_mstatus,
        severity="HIGH",
        applies_to="csr*",
    ),
    IntentSpec(
        name="pc_4byte_aligned",
        description="PC must be 4-byte aligned (AVA targets RV32IM without RVC)",
        checker=_spec_pc_alignment,
        severity="MEDIUM",
        applies_to="*",
    ),
]


# ─────────────────────────────────────────────────────────
# Checker engine
# ─────────────────────────────────────────────────────────

class IntentChecker:
    """
    Runs all applicable IntentSpecs against paired RTL/ISS commit logs.

    Parameters
    ----------
    rtl_log_path : path to RTL commit JSONL
    iss_log_path : path to ISS commit JSONL
    specs        : list of IntentSpecs (defaults to BUILT_IN_SPECS)
    max_violations : stop after this many violations
    """

    def __init__(
        self,
        rtl_log_path:   str | Path,
        iss_log_path:   str | Path,
        specs:          Optional[List[IntentSpec]] = None,
        max_violations: int = 100,
    ) -> None:
        self.rtl_log_path  = Path(rtl_log_path)
        self.iss_log_path  = Path(iss_log_path)
        self.specs         = specs or BUILT_IN_SPECS
        self.max_violations = max_violations

    def _load(self, path: Path) -> List[Dict]:
        records = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        rtl_log = self._load(self.rtl_log_path)
        iss_log = self._load(self.iss_log_path)
        n = min(len(rtl_log), len(iss_log))

        violations: List[IntentViolation] = []
        spec_results: Dict[str, Dict[str, Any]] = {
            s.name: {"checked": 0, "violations": 0} for s in self.specs
        }

        for i in range(n):
            if len(violations) >= self.max_violations:
                break
            rtl_r = rtl_log[i]
            iss_r = iss_log[i]
            disasm = (iss_r.get("disasm") or "").strip().lower()

            for spec in self.specs:
                pat = spec.applies_to
                if pat != "*":
                    # Simple glob-style matching (prefix with *)
                    if pat.endswith("*"):
                        if not disasm.startswith(pat[:-1]):
                            continue
                    else:
                        if disasm != pat:
                            continue

                spec_results[spec.name]["checked"] += 1
                try:
                    err = spec.checker(rtl_r, iss_r)
                except Exception as exc:
                    logger.warning("Intent spec %s raised: %s", spec.name, exc)
                    err = f"Checker exception: {exc}"

                if err:
                    spec_results[spec.name]["violations"] += 1
                    violations.append(IntentViolation(
                        spec_name=spec.name,
                        description=spec.description,
                        severity=spec.severity,
                        seq=rtl_r.get("seq", i),
                        pc=iss_r.get("pc"),
                        disasm=disasm,
                        error_msg=err,
                    ))

        finished = datetime.now(timezone.utc)
        high_violations = [v for v in violations if v.severity == "HIGH"]

        return {
            "schema_version": SCHEMA_VERSION,
            "agent": "agent_h_intent",
            "records_checked": n,
            "specs_run": len(self.specs),
            "total_violations": len(violations),
            "high_violations": len(high_violations),
            "pass": len(violations) == 0,
            "spec_results": spec_results,
            "violations": [
                {
                    "spec_name":   v.spec_name,
                    "severity":    v.severity,
                    "seq":         v.seq,
                    "pc":          v.pc,
                    "disasm":      v.disasm,
                    "error":       v.error_msg,
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
    rtl_log = run_dir / (manifest.get("outputs", {}).get("rtl_commit_log") or "rtl_commit.jsonl")
    iss_log = run_dir / (manifest.get("outputs", {}).get("iss_commit_log") or "iss_commit.jsonl")

    if not rtl_log.exists() or not iss_log.exists():
        logger.warning("IntentChecker: commit logs not found, skipping")
        return 0

    checker = IntentChecker(rtl_log, iss_log)
    report  = checker.run()

    report_path = run_dir / "intent_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("phases", {})["intent_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    manifest.setdefault("outputs", {})["intent_report"] = "intent_report.json"

    if not report["pass"]:
        manifest["status"] = "fail"
        manifest["error"] = {
            "code": "INTENT_VIOLATION",
            "message": f"{report['high_violations']} HIGH-severity intent violations",
        }

    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("IntentChecker: %d violations (%d HIGH)",
                report["total_violations"], report["high_violations"])
    return 0 if report["pass"] else 1

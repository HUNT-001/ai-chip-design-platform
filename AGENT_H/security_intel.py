"""
AGENT_H/security_intel.py
==========================
T19 — Hardware Security Intelligence Layer

Analyses the RTL/ISS commit log for microarchitectural security properties:
  - Speculative execution side channels (Spectre-style gadget patterns)
  - Cache timing covert channels (dcache/icache miss rate anomalies)
  - Privilege escalation paths (illegal mode transitions)
  - TLB / address-translation attacks (mismatched privilege levels)
  - Information leakage via undefined-result instructions

Each pattern is scored with a leak_score [0.0, 1.0]:
  0.0 → no evidence of leakage
  1.0 → confirmed leakage path

Note: This is a static-analysis / heuristic engine based on commit-log
patterns — it does not replace a proper microarchitectural leakage model.
For production use, combine with formal property checking via Agent L.

Output: security_report.json with:
  - gadgets: list of detected gadget instances
  - privilege_violations: mode transition anomalies
  - leak_score: aggregate security risk score [0,1]
  - band: CLEAN / LOW / MEDIUM / HIGH / CRITICAL
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Gadget patterns
# ─────────────────────────────────────────────────────────

@dataclass
class SecurityFinding:
    """One detected security-relevant pattern."""
    category:    str    # "spectre_gadget", "priv_escalation", "info_leak", "cache_side_channel"
    name:        str    # short identifier
    description: str
    severity:    str    # LOW / MEDIUM / HIGH / CRITICAL
    seq:         int
    pc:          Optional[str]
    disasm:      Optional[str]
    leak_score:  float  # 0.0–1.0


class _GadgetDetector:
    """Base class for security gadget detectors."""
    name:        str = "base"
    category:    str = "unknown"
    severity:    str = "MEDIUM"
    leak_score:  float = 0.5

    def detect(
        self,
        rtl:    Dict[str, Any],
        iss:    Dict[str, Any],
        window: List[Tuple[Dict, Dict]],
        seq:    int,
    ) -> Optional[SecurityFinding]:
        raise NotImplementedError

    def _disasm(self, rec: Dict) -> str:
        return (rec.get("disasm") or "").strip().lower()


# ── Spectre-style pattern: branch → load → dependent-load ──

class SpectreV1Detector(_GadgetDetector):
    """
    Detect Spectre v1 gadget pattern:
      (1) conditional branch on array length check
      (2) array load using tainted index
      (3) cache-timing load based on loaded value

    Heuristic: branch → load within 3 instructions → another load
    """
    name       = "spectre_v1_pattern"
    category   = "spectre_gadget"
    severity   = "HIGH"
    leak_score = 0.7

    def __init__(self):
        self._last_branch_seq: Optional[int] = None
        self._last_load_reg:   Optional[str] = None

    def detect(self, rtl, iss, window, seq):
        disasm = self._disasm(iss)
        is_branch = any(disasm.startswith(m) for m in ("beq","bne","blt","bge","bltu","bgeu"))
        is_load   = any(disasm.startswith(m) for m in ("lw","lh","lb","lhu","lbu"))

        if is_branch:
            self._last_branch_seq = seq
            self._last_load_reg   = None
        elif is_load and self._last_branch_seq is not None:
            if seq - self._last_branch_seq <= 3:
                # First load after branch — capture destination reg
                parts = disasm.split()
                if len(parts) >= 2:
                    self._last_load_reg = parts[1].rstrip(",")
            elif is_load and self._last_load_reg is not None:
                # Second load — check if it uses the first load's result (taint propagation)
                if self._last_load_reg in disasm:
                    return SecurityFinding(
                        category   = self.category,
                        name       = self.name,
                        description= (
                            f"Spectre v1 gadget pattern: branch at seq {self._last_branch_seq}, "
                            f"array load, dependent cache-timing load at seq {seq}"
                        ),
                        severity   = self.severity,
                        seq        = seq,
                        pc         = iss.get("pc"),
                        disasm     = disasm,
                        leak_score = self.leak_score,
                    )
        return None


# ── Privilege escalation: mode transitions ──

class PrivilegeEscalationDetector(_GadgetDetector):
    """
    Detect illegal privilege mode transitions in commit log.
    Looks for CSR writes that lower privilege level unexpectedly.
    """
    name       = "privilege_escalation"
    category   = "priv_escalation"
    severity   = "CRITICAL"
    leak_score = 0.9

    def detect(self, rtl, iss, window, seq):
        disasm = self._disasm(iss)
        if not any(disasm.startswith(m) for m in ("csrrw", "csrrs", "mret")):
            return None

        # Check mstatus.MPP field (bits 12:11)
        rtl_csrs = rtl.get("csrs") or {}
        iss_csrs = iss.get("csrs") or {}
        for src, label in ((rtl_csrs, "RTL"), (iss_csrs, "ISS")):
            mst = src.get("mstatus")
            if mst is None:
                continue
            mst_val = int(mst, 16) if isinstance(mst, str) else mst
            mpp = (mst_val >> 11) & 0x3
            # MPP=0b00 means M-mode returning to U-mode — only valid after deliberate mret
            if mpp == 0 and disasm.startswith("csrrw"):
                return SecurityFinding(
                    category   = self.category,
                    name       = self.name,
                    description= (
                        f"{label} mstatus.MPP=0 (U-mode) set via CSR write at seq {seq} — "
                        f"potential privilege downgrade without mret"
                    ),
                    severity   = self.severity,
                    seq        = seq,
                    pc         = iss.get("pc"),
                    disasm     = disasm,
                    leak_score = self.leak_score,
                )
        return None


# ── Information leak via undefined-result instructions ──

class UndefinedResultLeakDetector(_GadgetDetector):
    """
    Detect DIV-by-zero and other undefined-result sequences that may leak
    microarchitectural state through the defined-but-implementation-specific
    result (RISC-V defines the result but not timing/pipeline behaviour).
    """
    name       = "undefined_result_leak"
    category   = "info_leak"
    severity   = "MEDIUM"
    leak_score = 0.4

    def detect(self, rtl, iss, window, seq):
        disasm = self._disasm(iss)
        if not any(disasm.startswith(m) for m in ("div", "divu", "rem", "remu")):
            return None
        # Heuristic: if prior instruction loaded from an attacker-controlled address
        if window:
            prev_disasm = (window[-1][1].get("disasm") or "").lower()
            if any(prev_disasm.startswith(m) for m in ("lw", "lh", "lb")):
                return SecurityFinding(
                    category   = self.category,
                    name       = self.name,
                    description= (
                        f"DIV/REM at seq {seq} following a memory load — "
                        f"timing of undefined-result path may leak loaded value"
                    ),
                    severity   = self.severity,
                    seq        = seq,
                    pc         = iss.get("pc"),
                    disasm     = disasm,
                    leak_score = self.leak_score,
                )
        return None


# ── Cache side-channel via miss-rate anomaly ──

class CacheSideChannelDetector(_GadgetDetector):
    """
    Detect unusual dcache/icache miss rate patterns that may indicate a
    cache-based covert channel (e.g. Flush+Reload, Prime+Probe setup).
    """
    name       = "cache_side_channel"
    category   = "cache_side_channel"
    severity   = "MEDIUM"
    leak_score = 0.5

    def __init__(self, miss_rate_threshold: float = 0.40):
        self._threshold  = miss_rate_threshold
        self._load_count = 0
        self._miss_count = 0
        self._window_seq = 0

    def detect(self, rtl, iss, window, seq):
        # Use perf_counters if present
        pc_data = iss.get("perf_counters") or {}
        dcache_miss = pc_data.get("dcache_miss")
        instret     = pc_data.get("instret")
        if dcache_miss is not None and instret and instret > 0:
            miss_rate = dcache_miss / instret
            if miss_rate > self._threshold and seq - self._window_seq > 50:
                self._window_seq = seq
                return SecurityFinding(
                    category   = self.category,
                    name       = self.name,
                    description= (
                        f"Elevated dcache miss rate {miss_rate:.1%} at seq {seq} "
                        f"(threshold {self._threshold:.1%}) — possible cache covert channel"
                    ),
                    severity   = self.severity,
                    seq        = seq,
                    pc         = iss.get("pc"),
                    disasm     = self._disasm(iss),
                    leak_score = min(1.0, miss_rate * self.leak_score / self._threshold),
                )
        return None


# ─────────────────────────────────────────────────────────
# Security Intelligence Engine
# ─────────────────────────────────────────────────────────

class SecurityIntelligence:
    """
    Analyses paired RTL/ISS commit logs for hardware security properties.

    Parameters
    ----------
    rtl_log        : list of RTL commit records
    iss_log        : list of ISS commit records
    window_size    : sliding window size for temporal pattern detection
    max_findings   : stop after this many findings
    """

    def __init__(
        self,
        rtl_log:      List[Dict],
        iss_log:      List[Dict],
        window_size:  int = 8,
        max_findings: int = 100,
    ) -> None:
        self.rtl_log     = rtl_log
        self.iss_log     = iss_log
        self.window_size = window_size
        self.max_findings = max_findings
        self._detectors  = [
            SpectreV1Detector(),
            PrivilegeEscalationDetector(),
            UndefinedResultLeakDetector(),
            CacheSideChannelDetector(),
        ]

    def run(self) -> Dict[str, Any]:
        started   = datetime.now(timezone.utc)
        n         = min(len(self.rtl_log), len(self.iss_log))
        findings: List[SecurityFinding] = []
        window:   List[Tuple[Dict, Dict]] = []

        for i in range(n):
            if len(findings) >= self.max_findings:
                break
            rtl_r = self.rtl_log[i]
            iss_r = self.iss_log[i]
            seq   = rtl_r.get("seq", i)

            for det in self._detectors:
                try:
                    f = det.detect(rtl_r, iss_r, window[-self.window_size:], seq)
                    if f:
                        findings.append(f)
                except Exception as exc:
                    logger.warning("SecurityDetector %s raised: %s", det.name, exc)
            window.append((rtl_r, iss_r))

        finished = datetime.now(timezone.utc)

        # Aggregate leak score (max of individual scores, weighted by severity)
        sev_weights = {"CRITICAL": 1.0, "HIGH": 0.8, "MEDIUM": 0.5, "LOW": 0.2}
        if findings:
            agg_score = max(f.leak_score * sev_weights.get(f.severity, 0.5)
                            for f in findings)
        else:
            agg_score = 0.0

        if agg_score >= 0.8:   band = "CRITICAL"
        elif agg_score >= 0.6: band = "HIGH"
        elif agg_score >= 0.3: band = "MEDIUM"
        elif agg_score > 0:    band = "LOW"
        else:                  band = "CLEAN"

        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "security_intel",
            "records_analysed": n,
            "total_findings":   len(findings),
            "leak_score":       round(agg_score, 4),
            "band":             band,
            "categories":       list({f.category for f in findings}),
            "findings": [
                {
                    "category":    f.category,
                    "name":        f.name,
                    "severity":    f.severity,
                    "seq":         f.seq,
                    "pc":          f.pc,
                    "disasm":      f.disasm,
                    "description": f.description,
                    "leak_score":  f.leak_score,
                }
                for f in findings[:50]
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

    engine = SecurityIntelligence(rtl_log, iss_log)
    report = engine.run()

    report_path = run_dir / "security_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["security_report"] = "security_report.json"
    manifest.setdefault("metrics", {})["security_leak_score"] = report["leak_score"]
    manifest.setdefault("metrics", {})["security_band"]       = report["band"]

    if report["band"] in ("HIGH", "CRITICAL"):
        manifest["status"] = "fail"
        manifest["error"] = {
            "code":    "SECURITY_FINDING",
            "message": f"Security band {report['band']}: {report['total_findings']} findings",
        }

    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)
    return 0 if report["band"] in ("CLEAN", "LOW") else 1

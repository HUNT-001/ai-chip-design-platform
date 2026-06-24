"""
AGENT_H/root_cause_localizer.py
================================
T8 — Root-Cause Localization Engine

Given a bug report (bug_report.json) and the RTL source tree, ranks candidate
root-cause modules and RTL lines by computing a "suspicion score" derived from:

  1. Signal-cone analysis — which RTL signals are in the write-cone of the
     diverging register/PC, counted backward from the mismatch point.
  2. Instruction-class heuristics — known error-code families map to known
     suspect module classes (e.g. REG_MISMATCH on DIV → ALU / M-extension).
  3. Coverage-cold-path correlation — modules with uncovered lines near the
     mismatch PC are ranked higher.

Output
------
Returns a ranked list of {"module", "confidence", "suspect_lines"} entries
and optionally writes a `root_cause.json` file.

Limitations
-----------
Without a full RTL elaboration database, cone analysis is approximated by
text-pattern matching on signal names in the RTL sources. A production
deployment would integrate with Verilator's XML coverage database or a
proper elaboration tool (e.g. Yosys netlist) for exact cone computation.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Heuristic: error code → suspect module patterns
# ─────────────────────────────────────────────────────────

_ERROR_MODULE_HINTS: Dict[str, List[str]] = {
    "REG_MISMATCH":   ["alu", "execute", "writeback", "mul", "div", "reg_file"],
    "PC_MISMATCH":    ["branch", "pc_gen", "fetch", "decode", "jump"],
    "CSR_MISMATCH":   ["csr", "priv", "trap", "mstatus", "control"],
    "MEM_MISMATCH":   ["lsu", "mem", "store", "load", "dcache", "store_buf"],
    "TRAP_MISMATCH":  ["trap", "exception", "interrupt", "priv", "control"],
    "ORDERMISMATCH":  ["lsu", "store_buf", "mem_order", "amo", "cache"],
    "LENGTH_MISMATCH": ["decode", "fetch", "pc_gen", "stall"],
    "ALIGNMENT_ERROR": ["pc_gen", "fetch", "branch"],
    "X0_WRITTEN":     ["decode", "reg_file", "writeback"],
}

_INSTR_MODULE_HINTS: Dict[str, List[str]] = {
    "MUL":    ["mul", "mext", "alu"],
    "DIV":    ["div", "mext", "alu"],
    "REM":    ["div", "mext", "alu"],
    "LOAD":   ["lsu", "dcache", "mem"],
    "STORE":  ["lsu", "dcache", "mem", "store_buf"],
    "BRANCH": ["branch", "pc_gen", "bpu"],
    "JUMP":   ["pc_gen", "fetch"],
    "CSR":    ["csr", "priv"],
    "TRAP":   ["trap", "priv", "csr"],
    "FENCE":  ["lsu", "mem_order"],
    "AMO":    ["amo", "lsu", "dcache"],
    "SHIFT":  ["alu"],
    "ALU":    ["alu"],
}


# ─────────────────────────────────────────────────────────
# RTL text analysis helpers
# ─────────────────────────────────────────────────────────

def _find_signal_in_rtl(
    signal_name: str,
    rtl_sources: List[Path],
) -> List[Tuple[str, int]]:
    """
    Find all occurrences of a signal name in RTL source files.

    Returns list of (file_path, line_number).
    """
    hits: List[Tuple[str, int]] = []
    # Match exact word boundary (avoid false positives like 'mstatus_we' when looking for 'mstatus')
    pattern = re.compile(r"\b" + re.escape(signal_name) + r"\b")
    for src in rtl_sources:
        try:
            lines = src.read_text(errors="replace").splitlines()
            for i, line in enumerate(lines, 1):
                if pattern.search(line):
                    hits.append((str(src), i))
        except OSError:
            pass
    return hits


def _module_name_from_path(path: str) -> str:
    """Derive a module nickname from a file path."""
    p = Path(path)
    return p.stem.lower()


def _score_rtl_module(
    module_path:   str,
    hint_patterns: List[str],
    pc_hex:        Optional[str],
) -> float:
    """
    Heuristic score [0, 1] for a given RTL module path.
    Higher = more likely to be the root cause.
    """
    name = _module_name_from_path(module_path).lower()
    score = 0.0
    for hint in hint_patterns:
        if hint in name:
            score += 0.3
    # Cap at 1.0
    return min(score, 1.0)


# ─────────────────────────────────────────────────────────
# Root-cause candidate
# ─────────────────────────────────────────────────────────

@dataclass
class RootCauseCandidate:
    module:        str             # RTL file path or module name
    confidence:    float           # 0.0 to 1.0
    suspect_lines: List[int] = field(default_factory=list)
    evidence:      List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────
# Localizer
# ─────────────────────────────────────────────────────────

class RootCauseLocalizer:
    """
    Localizes the root cause of an AVA bug report to RTL modules / lines.

    Parameters
    ----------
    bug_report   : parsed bug_report.json dict
    rtl_sources  : list of RTL file paths to search
    coverage_summary : parsed coverage_summary.json dict (optional)
    """

    def __init__(
        self,
        bug_report:       Dict[str, Any],
        rtl_sources:      List[str | Path],
        coverage_summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.bug_report       = bug_report
        self.rtl_sources      = [Path(s) for s in rtl_sources]
        self.coverage_summary = coverage_summary

    def localize(self) -> List[RootCauseCandidate]:
        """
        Run localization and return candidates sorted by confidence (descending).
        """
        mismatch_class = self.bug_report.get("mismatch_class", "UNKNOWN")
        details        = self.bug_report.get("details", {})
        rtl_context    = self.bug_report.get("rtl_context", [])

        # Gather hint patterns
        error_hints = _ERROR_MODULE_HINTS.get(mismatch_class, [])
        instr_hints: List[str] = []
        for rec in (rtl_context or []):
            disasm = (rec.get("disasm") or "").lower().split()[0] if rec.get("disasm") else ""
            for itype, hints in _INSTR_MODULE_HINTS.items():
                if disasm.startswith(itype.lower()):
                    instr_hints.extend(hints)

        all_hints = list(set(error_hints + instr_hints))

        # Score each RTL source file
        candidates: Dict[str, RootCauseCandidate] = {}
        for src in self.rtl_sources:
            module = str(src)
            score  = _score_rtl_module(module, all_hints, None)

            # Boost score if the diverging register/signal appears in this file
            diverging_signal = details.get("register") or details.get("signal")
            suspect_lines: List[int] = []
            evidence: List[str] = []
            if diverging_signal:
                hits = _find_signal_in_rtl(diverging_signal, [src])
                if hits:
                    score += 0.2 * min(len(hits), 3)
                    suspect_lines = [h[1] for h in hits[:10]]
                    evidence.append(
                        f"Signal '{diverging_signal}' found at {len(hits)} location(s)"
                    )

            # Boost from coverage cold paths
            if self.coverage_summary:
                cold_paths = self.coverage_summary.get("cold_paths", [])
                for cp in cold_paths:
                    cp_module = _module_name_from_path(cp.get("module", ""))
                    src_module = _module_name_from_path(str(src))
                    if cp_module == src_module:
                        score += 0.1
                        suspect_lines.append(cp.get("line", 0))
                        evidence.append(
                            f"Cold path at line {cp.get('line')}: {cp.get('description','')}"
                        )

            if score > 0:
                candidates[module] = RootCauseCandidate(
                    module=module,
                    confidence=min(round(score, 4), 1.0),
                    suspect_lines=sorted(set(suspect_lines)),
                    evidence=evidence,
                )

        sorted_candidates = sorted(
            candidates.values(), key=lambda c: c.confidence, reverse=True
        )
        return sorted_candidates[:10]   # return top-10

    def to_report(self) -> Dict[str, Any]:
        """Return a JSON-serialisable localization report."""
        candidates = self.localize()
        return {
            "schema_version":  SCHEMA_VERSION,
            "mismatch_class":  self.bug_report.get("mismatch_class"),
            "run_id":          self.bug_report.get("run_id"),
            "first_divergence_seq": self.bug_report.get("first_divergence_seq"),
            "candidates": [
                {
                    "module":        c.module,
                    "confidence":    c.confidence,
                    "suspect_lines": c.suspect_lines,
                    "evidence":      c.evidence,
                }
                for c in candidates
            ],
            "top_candidate": candidates[0].module if candidates else None,
        }

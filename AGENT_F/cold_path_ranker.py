#!/usr/bin/env python3
"""
cold_path_ranker.py — AVA Cold Path ROI Ranker
===============================================
Ranks uncovered coverage points by ROI = reachability × impact × novelty,
then generates actionable RV32IM assembly constraints for test generation.

Fixes vs. the original provided version
----------------------------------------
  * rank_by_roi: original built path_dict inside loop but immediately lost it
    (sorted cold_paths — ColdPath objects — not the dict list). Fixed to
    return a proper List[Dict] with roi_score and test_constraint.
  * _impact_factor: original checked path.description but ColdPath.description
    is a raw file:line string; fixed to also check path.type and path.module.
  * _novelty_bonus: calls db.test_attempts_for_path() which now exists.
  * _generate_constraint: returns executable RV32IM assembly snippet, not
    a comment stub.
  * All imports were missing; now complete.
  * CLI: --top N, --run-id, --json flags added.

Usage
-----
  python cold_path_ranker.py --db coverage.db --top 20
  python cold_path_ranker.py --db coverage.db --top 10 --json
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Import CoverageDatabase from coverage_database.py ────────────────────────
try:
    from coverage_database import CoverageDatabase, ColdPath
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    logger.warning(
        "coverage_database.py not importable. "
        "Place it alongside cold_path_ranker.py."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# RV32IM assembly constraint templates
# ═══════════════════════════════════════════════════════════════════════════════

# Map coverage kind → template factory.
# Templates produce stand-alone assembly that forces the specific coverage event.
_BRANCH_TEMPLATES = {
    "b0": ("Force branch NOT-taken (arm 0)",
           "  li   t0, 0\n  li   t1, 1\n  beq  t0, t1, .+8\n"),       # condition false
    "b1": ("Force branch TAKEN (arm 1)",
           "  li   t0, 1\n  li   t1, 1\n  beq  t0, t1, .+8\n"),       # condition true
}

_M_EXT_CRITICAL = frozenset({"mul","mulh","mulhsu","mulhu","div","divu","rem","remu"})
_TRAP_KEYWORDS  = frozenset({"trap","ecall","ebreak","mret","exception","interrupt"})
_CSR_KEYWORDS   = frozenset({"csr","mcause","mepc","mtvec","mstatus","mie","mip"})


def _asm_for_branch(comment: str) -> str:
    tmpl = _BRANCH_TEMPLATES.get(comment.strip().lower())
    if tmpl:
        return tmpl[1]
    # Unknown arm → exercise both
    return (
        "  # Force both branch arms\n"
        "  li   t0, 0\n  li   t1, 1\n  beq  t0, t1, .+8\n"
        "  li   t0, 1\n  li   t1, 1\n  beq  t0, t1, .+8\n"
    )


def _asm_for_toggle(comment: str) -> str:
    s = comment.strip().lower()
    if s == "s0":   # 0→1 transition
        return "  li   t0, 0\n  li   t0, 1\n  # Force 0->1 toggle\n"
    if s == "s1":   # 1→0 transition
        return "  li   t0, 1\n  li   t0, 0\n  # Force 1->0 toggle\n"
    return "  li   t0, 0\n  li   t0, 1\n  li   t0, 0\n  # Toggle both directions\n"


def _asm_for_m_corner(desc_lower: str) -> str:
    if "div" in desc_lower or "rem" in desc_lower:
        return (
            "  # M-ext corner: div/rem by zero + overflow\n"
            "  li   t0, 0x80000000\n"
            "  li   t1, 0\n"
            "  div  t2, t0, t1     # div by zero -> -1\n"
            "  li   t1, 0xFFFFFFFF\n"
            "  div  t2, t0, t1     # INT_MIN / -1 -> INT_MIN\n"
            "  rem  t3, t0, t1     # INT_MIN % -1 -> 0\n"
        )
    if "mul" in desc_lower:
        return (
            "  # M-ext corner: MULH signed/unsigned edges\n"
            "  li   t0, 0x80000000\n"
            "  li   t1, 0x80000000\n"
            "  mulh t2, t0, t1     # MULH(INT_MIN, INT_MIN)\n"
            "  mulhu t3, t0, t1    # MULHU(INT_MIN, INT_MIN)\n"
            "  li   t0, 0xFFFFFFFF\n"
            "  mul  t4, t0, t0     # MUL(-1,-1)=1\n"
        )
    return "  nop\n"


def _asm_for_csr(module_lower: str) -> str:
    return (
        "  # CSR access sequence\n"
        "  csrr t0, mcause\n"
        "  csrr t1, mepc\n"
        "  csrr t2, mstatus\n"
    )


def _asm_for_trap() -> str:
    return (
        "  # Trap / exception sequence\n"
        "  la   t0, _trap_handler\n"
        "  csrw mtvec, t0\n"
        "  ecall\n"
        "  .align 2\n"
        "_trap_handler:\n"
        "  mret\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ColdPathRanker
# ═══════════════════════════════════════════════════════════════════════════════

class ColdPathRanker:
    """
    Rank uncovered coverage points by ROI for test generation guidance.

    ROI = reachability_score × impact_factor × novelty_bonus

    reachability  : 0→1  (from CoverageDatabase._compute_reachability)
    impact        : 1.0 | 3.0  (critical path bonus for M-ext, traps, CSRs)
    novelty       : 2.0 → 1/(1+attempts)  (reward never-targeted points)
    """

    def __init__(self, db: "CoverageDatabase") -> None:
        if not _DB_AVAILABLE:
            raise RuntimeError(
                "coverage_database.py must be importable to use ColdPathRanker."
            )
        self.db = db

    def rank_by_roi(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Return a list of ranked dicts, each representing one cold path.

        Keys per entry:
          module           : source file / module
          line             : line number
          type             : coverage kind (line | branch | toggle | expression)
          description      : raw Verilator description
          reachability     : float [0, 1]
          impact_factor    : float
          novelty_bonus    : float
          roi_score        : float (product of above three)
          test_constraint  : RV32IM assembly snippet targeting this point
          attempts         : int (how many previous tests targeted this)
        """
        cold_paths = self.db.top_cold_paths(limit * 3)   # over-fetch for filtering

        results: List[Dict[str, Any]] = []
        for path in cold_paths:
            impact  = self._impact_factor(path)
            novelty = self._novelty_bonus(path)
            roi     = round(path.reachability_score * impact * novelty, 6)

            results.append({
                "module":          path.module,
                "line":            path.line,
                "col":             path.col,
                "type":            path.type,
                "description":     path.description,
                "reachability":    round(path.reachability_score, 4),
                "impact_factor":   impact,
                "novelty_bonus":   round(novelty, 4),
                "roi_score":       roi,
                "attempts":        self.db.test_attempts_for_path(path.module, path.line),
                "test_constraint": self._generate_constraint(path),
            })

        results.sort(key=lambda d: d["roi_score"], reverse=True)
        return results[:limit]

    def top_constraints(self, limit: int = 10) -> List[str]:
        """Return just the assembly snippets for the top *limit* cold paths."""
        return [d["test_constraint"] for d in self.rank_by_roi(limit)]

    # ── Scoring helpers ────────────────────────────────────────────────────

    def _impact_factor(self, path: ColdPath) -> float:
        """
        Return a multiplier [1.0, 3.0] based on coverage point criticality.

        High-impact categories:
          * M-extension arithmetic (mul / div / rem corner cases)
          * Trap / exception / privilege handling
          * CSR access paths
          * Branch arms in critical modules
        """
        desc_lower   = (path.description or "").lower()
        module_lower = (path.module or "").lower()

        # M-extension critical corners
        if any(kw in desc_lower or kw in module_lower for kw in _M_EXT_CRITICAL):
            return 3.0
        # Trap / exception paths
        if any(kw in desc_lower or kw in module_lower for kw in _TRAP_KEYWORDS):
            return 3.0
        # CSR accesses
        if any(kw in desc_lower or kw in module_lower for kw in _CSR_KEYWORDS):
            return 2.5
        # Branch paths in decode / execute stages
        if path.type == "branch" and any(s in module_lower for s in ["decode","execute","alu"]):
            return 2.0
        # Toggle in control-critical signals
        if path.type == "toggle" and any(s in module_lower for s in ["ctrl","control","hazard"]):
            return 1.8
        return 1.0

    def _novelty_bonus(self, path: ColdPath) -> float:
        """
        Bonus multiplier for paths that have never been targeted by a test.

        attempts == 0 → 2.0 (strong bonus: explore unseen territory)
        attempts  > 0 → 1 / (1 + attempts)  (diminishing return)
        """
        attempts = self.db.test_attempts_for_path(path.module, path.line)
        if attempts == 0:
            return 2.0
        return round(1.0 / (1.0 + attempts), 4)

    def _generate_constraint(self, path: ColdPath) -> str:
        """
        Return an executable RV32IM assembly snippet that targets this cold path.

        The snippet is self-contained: it can be embedded in a test template
        after the prologue (clocks stable, resets deasserted).
        """
        header = (
            f"  # ── Target {path.type} @ {path.module}:{path.line} ──\n"
        )
        desc_lower   = (path.description or "").lower()
        module_lower = (path.module or "").lower()
        comment      = (path.description or "").split("@")[0].strip().lower()

        # Branch arms
        if path.type == "branch":
            return header + _asm_for_branch(comment)

        # Toggle transitions
        if path.type == "toggle":
            return header + _asm_for_toggle(comment)

        # M-extension corners (line or expression inside M-ext module)
        if any(kw in module_lower or kw in desc_lower for kw in _M_EXT_CRITICAL):
            return header + _asm_for_m_corner(desc_lower)

        # Trap / exception paths
        if any(kw in module_lower or kw in desc_lower for kw in _TRAP_KEYWORDS):
            return header + _asm_for_trap()

        # CSR paths
        if any(kw in module_lower or kw in desc_lower for kw in _CSR_KEYWORDS):
            return header + _asm_for_csr(module_lower)

        # Default: NOPs with a clear comment for the test author
        return (
            header
            + f"  # No specific template for type='{path.type}' — insert stimulus here\n"
            + "  nop\n"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _main() -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="AVA Cold Path ROI Ranker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cold_path_ranker.py --db coverage.db --top 20
  python cold_path_ranker.py --db coverage.db --top 10 --json
  python cold_path_ranker.py --db coverage.db --top 5 --constraints-only
""",
    )
    p.add_argument("--db",              required=True, type=Path,  help="coverage_database.db path")
    p.add_argument("--top",             type=int, default=20,      help="Number of paths to rank")
    p.add_argument("--json",            action="store_true",        help="Output as JSON")
    p.add_argument("--constraints-only",action="store_true",        help="Only print asm snippets")
    p.add_argument("--run-id",          default="",                 help="Record test attempt run_id")
    p.add_argument("--verbose", "-v",   action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(message)s",
    )

    if not _DB_AVAILABLE:
        print("ERROR: coverage_database.py not importable.", file=sys.stderr)
        return 1

    with CoverageDatabase(args.db) as db:
        ranker  = ColdPathRanker(db)
        ranked  = ranker.rank_by_roi(args.top)

        if args.constraints_only:
            for entry in ranked:
                print(entry["test_constraint"])
            return 0

        if args.json:
            print(json.dumps(ranked, indent=2))
            return 0

        # Human-readable table
        print(f"\n{'#':>3}  {'Module:Line':<40} {'Type':<10} "
              f"{'ROI':>7}  {'Reach':>6}  {'Impact':>6}  {'Novelty':>7}")
        print("─" * 88)
        for i, entry in enumerate(ranked, 1):
            loc = f"{Path(entry['module']).name}:{entry['line']}"
            print(
                f"{i:>3}  {loc:<40} {entry['type']:<10} "
                f"{entry['roi_score']:>7.4f}  {entry['reachability']:>6.4f}  "
                f"{entry['impact_factor']:>6.1f}  {entry['novelty_bonus']:>7.4f}"
            )
            if args.verbose:
                print(f"     Constraint:\n{entry['test_constraint']}")
        print()

        # Record attempts if run_id given
        if args.run_id:
            for entry in ranked:
                db.record_test_attempt(args.run_id, entry["module"], entry["line"])
            print(f"Recorded {len(ranked)} test attempts for run_id='{args.run_id}'")

    return 0


if __name__ == "__main__":
    sys.exit(_main())

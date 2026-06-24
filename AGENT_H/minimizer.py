"""
AGENT_H/minimizer.py
====================
T10 — Automated Counterexample Minimization

Implements a delta-debugging loop that reduces a failing commit-log sequence
to the minimal subset of instructions that still triggers the same AVA
mismatch classification.

Algorithm
---------
Based on Andreas Zeller's delta-debugging (ddmin) algorithm:

  1. Start with the full instruction sequence [I_0, ..., I_N].
  2. Split into two halves. Try each half alone — does it still fail?
  3. If yes, recurse on the failing half.
  4. If no, try removing each half (complement). Does the remaining half fail?
  5. Repeat until the granularity reaches 1 (individual instruction).

The result is a 1-minimal failing subsequence: removing any single instruction
would cause the mismatch to disappear.

Usage
-----
  from AGENT_H.minimizer import CommitLogMinimizer

  minimizer = CommitLogMinimizer(
      rtl_log    = list_of_rtl_records,
      iss_log    = list_of_iss_records,
      oracle     = my_comparator_function,   # (rtl_sub, iss_sub) -> bool (True = still fails)
      max_rounds = 50,
  )
  minimal_rtl, minimal_iss, stats = minimizer.minimize()

In AVA, the oracle is Agent D's comparator invoked as a subprocess or
function call. The minimizer works on in-memory record lists for speed.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Oracle type: (rtl_records, iss_records) -> bool
# True means the mismatch still occurs, False means it does not.
Oracle = Callable[[List[dict], List[dict]], bool]


@dataclass
class MinimizationStats:
    """Statistics for one minimization run."""
    initial_length:  int
    final_length:    int
    oracle_calls:    int
    rounds:          int
    elapsed_s:       float
    reduction_pct:   float   # = (1 - final/initial) * 100


# ─────────────────────────────────────────────────────────
# Built-in oracle: in-process comparator
# ─────────────────────────────────────────────────────────

def _make_in_process_oracle(mismatch_class: str) -> Oracle:
    """
    Build an oracle that checks whether a (rtl_sub, iss_sub) pair still
    triggers a mismatch of the specified class using a simplified inline
    comparator (no Agent D subprocess needed).

    Checks only the specific condition the mismatch_class requires:
      REG_MISMATCH — shadow register files diverge
      PC_MISMATCH  — PCs don't match
      CSR_MISMATCH — CSR maps diverge
      MEM_MISMATCH — mem_writes differ
      TRAP_MISMATCH — trap fields differ
    """
    def oracle(rtl_sub: List[dict], iss_sub: List[dict]) -> bool:
        if len(rtl_sub) != len(iss_sub):
            return mismatch_class == "LENGTH_MISMATCH"

        rtl_regs = {f"x{i}": 0 for i in range(32)}
        iss_regs = {f"x{i}": 0 for i in range(32)}
        rtl_csrs: Dict[str, int] = {}
        iss_csrs: Dict[str, int] = {}

        def _apply(regs: dict, csrs: dict, rec: dict) -> None:
            for k, v in (rec.get("regs") or {}).items():
                if k != "x0":
                    regs[k] = int(v, 16) if isinstance(v, str) else v
            for k, v in (rec.get("csrs") or {}).items():
                csrs[k] = int(v, 16) if isinstance(v, str) else v

        for rtl_r, iss_r in zip(rtl_sub, iss_sub):
            if mismatch_class == "PC_MISMATCH" and rtl_r.get("pc") != iss_r.get("pc"):
                return True
            _apply(rtl_regs, rtl_csrs, rtl_r)
            _apply(iss_regs, iss_csrs, iss_r)
            if mismatch_class == "REG_MISMATCH" and rtl_regs != iss_regs:
                return True
            if mismatch_class == "CSR_MISMATCH" and rtl_csrs != iss_csrs:
                return True
            if mismatch_class == "MEM_MISMATCH":
                if rtl_r.get("mem_writes") != iss_r.get("mem_writes"):
                    return True
            if mismatch_class == "TRAP_MISMATCH":
                rtl_trap = rtl_r.get("trap")
                iss_trap = iss_r.get("trap")
                if (rtl_trap is None) != (iss_trap is None):
                    return True
                if rtl_trap and iss_trap:
                    if rtl_trap.get("cause") != iss_trap.get("cause"):
                        return True
        return False

    return oracle


# ─────────────────────────────────────────────────────────
# Delta-debugging minimizer
# ─────────────────────────────────────────────────────────

class CommitLogMinimizer:
    """
    Reduces a failing (rtl_log, iss_log) pair to a minimal failing subsequence.

    Parameters
    ----------
    rtl_log    : list of RTL commit-log records (dicts)
    iss_log    : list of ISS commit-log records (dicts)
    oracle     : callable (rtl_sub, iss_sub) -> bool; True = mismatch still present
    max_rounds : stop after this many oracle calls (safety cap)
    verbose    : if True, log progress every round
    """

    def __init__(
        self,
        rtl_log:    List[dict],
        iss_log:    List[dict],
        oracle:     Optional[Oracle] = None,
        mismatch_class: str = "REG_MISMATCH",
        max_rounds: int = 500,
        verbose:    bool = False,
    ) -> None:
        self.rtl_log        = rtl_log
        self.iss_log        = iss_log
        self.mismatch_class = mismatch_class
        self.oracle         = oracle or _make_in_process_oracle(mismatch_class)
        self.max_rounds     = max_rounds
        self.verbose        = verbose

        self._oracle_calls  = 0
        self._rounds        = 0

    def _call_oracle(self, rtl_sub: List[dict], iss_sub: List[dict]) -> bool:
        """Invoke oracle and track call count."""
        self._oracle_calls += 1
        if self._oracle_calls > self.max_rounds:
            logger.warning("Minimizer: max oracle calls (%d) reached", self.max_rounds)
            return False
        return self.oracle(rtl_sub, iss_sub)

    def _subset(self, indices: List[int]) -> Tuple[List[dict], List[dict]]:
        """Extract records at given indices from both logs."""
        rtl_sub = [self.rtl_log[i] for i in indices if i < len(self.rtl_log)]
        iss_sub = [self.iss_log[i] for i in indices if i < len(self.iss_log)]
        return rtl_sub, iss_sub

    def minimize(self) -> Tuple[List[dict], List[dict], MinimizationStats]:
        """
        Run delta-debugging minimization.

        Returns
        -------
        (minimal_rtl, minimal_iss, stats)
          minimal_rtl : minimal failing RTL subsequence
          minimal_iss : corresponding ISS subsequence
          stats       : MinimizationStats dataclass
        """
        t0 = time.monotonic()
        n  = min(len(self.rtl_log), len(self.iss_log))
        indices = list(range(n))

        # Verify the full log fails before minimizing
        rtl_full, iss_full = self._subset(indices)
        if not self._call_oracle(rtl_full, iss_full):
            logger.warning("Minimizer: full log does NOT trigger mismatch — nothing to minimise")
            stats = MinimizationStats(
                initial_length=n, final_length=n,
                oracle_calls=self._oracle_calls, rounds=0,
                elapsed_s=round(time.monotonic() - t0, 3),
                reduction_pct=0.0,
            )
            return self.rtl_log[:n], self.iss_log[:n], stats

        # Delta-debugging (ddmin)
        minimal_indices = self._ddmin(indices, 2)

        final_n   = len(minimal_indices)
        elapsed_s = round(time.monotonic() - t0, 3)
        reduction = round(100.0 * (1 - final_n / n), 2) if n > 0 else 0.0

        logger.info(
            "Minimizer: %d → %d instructions (%.1f%% reduction) in %d calls, %.2fs",
            n, final_n, reduction, self._oracle_calls, elapsed_s
        )

        rtl_min, iss_min = self._subset(minimal_indices)
        stats = MinimizationStats(
            initial_length=n,
            final_length=final_n,
            oracle_calls=self._oracle_calls,
            rounds=self._rounds,
            elapsed_s=elapsed_s,
            reduction_pct=reduction,
        )
        return rtl_min, iss_min, stats

    def _ddmin(self, indices: List[int], granularity: int) -> List[int]:
        """
        Recursive ddmin algorithm.
        Returns the minimal failing index list.
        """
        n = len(indices)
        if n == 1 or self._oracle_calls >= self.max_rounds:
            return indices

        chunk_size = max(1, math.ceil(n / granularity))
        chunks = [
            indices[i * chunk_size:(i + 1) * chunk_size]
            for i in range(granularity)
        ]
        self._rounds += 1

        if self.verbose:
            logger.debug("ddmin: n=%d, gran=%d, oracle_calls=%d",
                         n, granularity, self._oracle_calls)

        # Try each chunk alone
        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            rtl_sub, iss_sub = self._subset(chunk)
            if self._call_oracle(rtl_sub, iss_sub):
                return self._ddmin(chunk, 2)

        # Try removing each chunk (complement)
        for i, chunk in enumerate(chunks):
            complement = [idx for idx in indices if idx not in chunk]
            if not complement:
                continue
            rtl_sub, iss_sub = self._subset(complement)
            if self._call_oracle(rtl_sub, iss_sub):
                new_gran = max(2, granularity - 1)
                return self._ddmin(complement, new_gran)

        # Increase granularity if we haven't minimised yet
        if granularity < n:
            return self._ddmin(indices, min(granularity * 2, n))

        return indices   # 1-minimal: cannot reduce further


# ─────────────────────────────────────────────────────────
# Convenience function
# ─────────────────────────────────────────────────────────

def minimize_bug_report(
    rtl_log_path:   "str | Path",
    iss_log_path:   "str | Path",
    mismatch_class: str,
    output_rtl:     Optional["str | Path"] = None,
    output_iss:     Optional["str | Path"] = None,
    max_rounds:     int = 200,
) -> MinimizationStats:
    """
    Convenience wrapper: load JSONL files, minimise, write output JSONL files.

    Parameters
    ----------
    rtl_log_path   : path to full RTL commit JSONL
    iss_log_path   : path to full ISS commit JSONL
    mismatch_class : AVA error code to preserve during minimization
    output_rtl     : write minimal RTL log here (default: <rtl>_minimal.jsonl)
    output_iss     : write minimal ISS log here (default: <iss>_minimal.jsonl)

    Returns MinimizationStats.
    """
    import json
    from pathlib import Path as _Path

    rtl_path = _Path(rtl_log_path)
    iss_path = _Path(iss_log_path)

    def _load(p: _Path) -> List[dict]:
        recs = []
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
        return recs

    rtl_log = _load(rtl_path)
    iss_log = _load(iss_path)

    minimizer = CommitLogMinimizer(
        rtl_log=rtl_log,
        iss_log=iss_log,
        mismatch_class=mismatch_class,
        max_rounds=max_rounds,
    )
    rtl_min, iss_min, stats = minimizer.minimize()

    def _write(recs: List[dict], path: _Path) -> None:
        with open(path, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    out_rtl = _Path(output_rtl) if output_rtl else rtl_path.with_suffix("_minimal.jsonl")
    out_iss = _Path(output_iss) if output_iss else iss_path.with_suffix("_minimal.jsonl")
    _write(rtl_min, out_rtl)
    _write(iss_min, out_iss)

    logger.info("Minimal RTL log: %s (%d records)", out_rtl, len(rtl_min))
    logger.info("Minimal ISS log: %s (%d records)", out_iss, len(iss_min))
    return stats

"""
redteam/agents/conflict_agent.py

Adversarial agent that finds cache coherency bugs in multi-core RISC-V chips
by systematically provoking MESI state transitions and race windows.

Attack patterns:
  1. Read-Write Conflict   — stale data reads after concurrent writes
  2. Write-Write Conflict  — lost updates from simultaneous stores
  3. Invalidation Race     — torn reads during cache line invalidation
  4. False Sharing         — excessive coherency traffic on unrelated data
  5. Atomic Violation      — non-atomicity of compare-and-swap sequences

Coverage metric: number of unique MESI transitions exercised (real, not random).
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .dut_interface import (
    AccessType, CacheState, DUTInterface, MockDUT
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────

class BugSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"


@dataclass
class CoherencyBug:
    bug_type:    str
    address:     int
    severity:    BugSeverity
    cycle:       int
    core_ids:    List[int]
    expected:    Optional[str]  = None
    actual:      Optional[str]  = None
    description: str            = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bug_type":    self.bug_type,
            "address":     hex(self.address),
            "severity":    self.severity.value,
            "cycle":       self.cycle,
            "core_ids":    self.core_ids,
            "expected":    self.expected,
            "actual":      self.actual,
            "description": self.description,
        }


@dataclass
class ConflictReport:
    agent:              str = "ConflictAgent"
    bugs_found:         int = 0
    patterns_tested:    int = 0
    critical_bugs:      List[CoherencyBug] = field(default_factory=list)
    mesi_transitions:   Dict[str, int] = field(default_factory=dict)
    unique_transitions: int = 0
    coverage_pct:       float = 0.0    # Based on MESI transition coverage
    elapsed_sec:        float = 0.0
    cycles_run:         int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent":              self.agent,
            "bugs_found":         self.bugs_found,
            "patterns_tested":    self.patterns_tested,
            "critical_bugs":      [b.to_dict() for b in self.critical_bugs],
            "mesi_transitions":   self.mesi_transitions,
            "unique_transitions": self.unique_transitions,
            "coverage_pct":       round(self.coverage_pct, 2),
            "elapsed_sec":        round(self.elapsed_sec, 2),
            "cycles_run":         self.cycles_run,
        }


# ─────────────────────────────────────────────
# All possible MESI transitions (ground truth)
# ─────────────────────────────────────────────
ALL_MESI_TRANSITIONS = {
    "I→S", "I→E", "I→M",
    "S→M", "S→I",
    "E→M", "E→S", "E→I",
    "M→S", "M→I",
}


# ─────────────────────────────────────────────
# Conflict Agent
# ─────────────────────────────────────────────

class ConflictAgent:
    """
    Adversarial cache coherency tester.

    All attack coroutines share one DUT interface and one bug accumulator.
    Campaigns are time-bounded: each attack pattern gets duration_cycles / 5
    cycles (equally divided).

    Args:
        dut:          DUT interface (CocotbDUT or MockDUT).
        campaign_seed: RNG seed for reproducible attack addresses.
        per_pattern_timeout: Max seconds any single attack pattern may run.
    """

    CACHE_LINE_MASK = ~63   # 64-byte aligned

    def __init__(
        self,
        dut:                    DUTInterface,
        campaign_seed:          int   = 0,
        per_pattern_timeout:    float = 120.0,
    ):
        self._dut              = dut
        self._rng              = random.Random(campaign_seed)
        self._timeout          = per_pattern_timeout
        self._bugs:            List[CoherencyBug] = []
        self._patterns = [
            ("read_write_conflict",   self._read_write_conflict),
            ("write_write_conflict",  self._write_write_conflict),
            ("invalidation_race",     self._invalidation_race),
            ("false_sharing",         self._false_sharing),
            ("atomic_violation",      self._atomic_violation),
        ]

        logger.info(
            f"ConflictAgent initialised — {dut.num_cores} cores, "
            f"seed={campaign_seed}"
        )

    # ── Public API ────────────────────────────────────────────────────────

    async def run_campaign(
        self,
        duration_cycles: int = 10_000,
    ) -> ConflictReport:
        """
        Run all attack patterns in parallel, each for duration_cycles/5 cycles.
        Returns a ConflictReport with real MESI transition coverage.
        """
        logger.info(
            f"[ConflictAgent] Campaign start — "
            f"{duration_cycles} total cycles across {len(self._patterns)} patterns"
        )
        start = time.monotonic()
        self._bugs.clear()

        cycles_per_pattern = max(100, duration_cycles // len(self._patterns))

        tasks = [
            asyncio.create_task(
                self._run_with_timeout(name, coro, cycles_per_pattern),
                name=name,
            )
            for name, coro in self._patterns
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for name, result in zip([n for n, _ in self._patterns], results):
            if isinstance(result, Exception):
                logger.error(f"[ConflictAgent] Pattern '{name}' raised: {result}")

        elapsed    = time.monotonic() - start
        transitions = self._get_transitions()
        coverage   = self._compute_coverage(transitions)

        report = ConflictReport(
            bugs_found       = len(self._bugs),
            patterns_tested  = len(self._patterns),
            critical_bugs    = [b for b in self._bugs
                                if b.severity == BugSeverity.CRITICAL],
            mesi_transitions = transitions,
            unique_transitions = len([t for t in transitions if transitions[t] > 0]),
            coverage_pct     = coverage,
            elapsed_sec      = elapsed,
            cycles_run       = await self._dut.get_cycle_count(),
        )

        logger.info(
            f"[ConflictAgent] Campaign done — "
            f"bugs={report.bugs_found}, coverage={coverage:.1f}%, "
            f"elapsed={elapsed:.1f}s"
        )
        return report

    # ── Attack patterns ───────────────────────────────────────────────────

    async def _read_write_conflict(self, cycles: int) -> None:
        """
        ATTACK: Core 0 reads, Core 1 writes immediately — detects stale reads.
        BUG: Core 0 sees old value after Core 1's write (cache not invalidated).
        """
        for _ in range(cycles):
            addr  = self._random_aligned_addr()
            write_val = self._rng.randint(1, 0xFFFF_FFFF)

            # Prime: Core 1 writes a known value
            await self._dut.core_write(1 % self._dut.num_cores, addr, write_val)

            # Core 0 reads — should see write_val (MESI: I→S)
            read_val = await self._dut.core_read(0, addr)

            # Core 1 writes a new value (triggers invalidation)
            new_val = write_val ^ 0xFFFF_FFFF
            await self._dut.core_write(1 % self._dut.num_cores, addr, new_val)

            # Core 0 reads again — MUST see new_val, not stale write_val
            stale_check = await self._dut.core_read(0, addr)
            cycle = await self._dut.get_cycle_count()

            if stale_check == write_val and stale_check != new_val:
                self._record_bug(CoherencyBug(
                    bug_type    = "READ_WRITE_STALE",
                    address     = addr,
                    severity    = BugSeverity.CRITICAL,
                    cycle       = cycle,
                    core_ids    = [0, 1],
                    expected    = hex(new_val),
                    actual      = hex(stale_check),
                    description = "Core 0 read stale data after Core 1 write + invalidation.",
                ))
            await self._dut.wait_cycles(1)

    async def _write_write_conflict(self, cycles: int) -> None:
        """
        ATTACK: All cores write to the same address simultaneously.
        BUG: Corrupted final value (not any core's written value).
        """
        valid_cores = list(range(self._dut.num_cores))
        for i in range(0, cycles, self._dut.num_cores):
            addr = self._random_aligned_addr()
            expected_values = {
                c: 0xA000_0000 + c for c in valid_cores
            }

            # All cores write concurrently
            await asyncio.gather(*[
                self._dut.core_write(c, addr, expected_values[c])
                for c in valid_cores
            ])

            # Final value must be one of the written values (one core wins)
            final = await self._dut.core_read(0, addr)
            cycle = await self._dut.get_cycle_count()

            if final not in expected_values.values():
                self._record_bug(CoherencyBug(
                    bug_type    = "WRITE_CORRUPTION",
                    address     = addr,
                    severity    = BugSeverity.CRITICAL,
                    cycle       = cycle,
                    core_ids    = valid_cores,
                    expected    = f"one of {[hex(v) for v in expected_values.values()]}",
                    actual      = hex(final),
                    description = "Memory value corrupted by concurrent writes (torn write).",
                ))
            await self._dut.wait_cycles(2)

    async def _invalidation_race(self, cycles: int) -> None:
        """
        ATTACK: Invalidate a cache line while another core is reading it.
        BUG: Torn read — value read is neither old nor new (partial word).
        """
        for _ in range(0, cycles, 2):
            addr = self._random_aligned_addr()
            sentinel = 0xDEAD_BEEF

            # Prime: write sentinel, both cores get SHARED copies
            await self._dut.core_write(0, addr, sentinel)
            await self._dut.core_read(1 % self._dut.num_cores, addr)

            # Launch read and invalidation concurrently
            read_result, _ = await asyncio.gather(
                self._dut.core_read(0, addr),
                self._dut.core_write(
                    1 % self._dut.num_cores,
                    addr,
                    sentinel ^ 0xFFFF_FFFF
                ),
            )

            # A torn read is one that is neither the old nor the new value
            new_value = sentinel ^ 0xFFFF_FFFF
            cycle = await self._dut.get_cycle_count()

            if read_result not in (sentinel, new_value):
                self._record_bug(CoherencyBug(
                    bug_type    = "TORN_READ",
                    address     = addr,
                    severity    = BugSeverity.CRITICAL,
                    cycle       = cycle,
                    core_ids    = [0, 1],
                    expected    = f"{hex(sentinel)} or {hex(new_value)}",
                    actual      = hex(read_result),
                    description = "Torn read: value is neither old nor new (partial invalidation).",
                ))
            await self._dut.wait_cycles(1)

    async def _false_sharing(self, cycles: int) -> None:
        """
        ATTACK: Cores write to different bytes in the same cache line.
        Detects excessive invalidation traffic (performance correctness bug).
        BUG: Cache line repeatedly bounces even though cores touch different words.
        """
        cache_line_base = 0x1000_0000
        invalidation_count = 0
        word_size = 4    # 32-bit words

        for i in range(0, cycles, self._dut.num_cores):
            # Each core writes to a different word in the same 64-byte line
            await asyncio.gather(*[
                self._dut.core_write(
                    c,
                    cache_line_base + (c * word_size),
                    0x1000 + c,
                )
                for c in range(self._dut.num_cores)
            ])

            # Count how many cores lost their SHARED state (invalidations)
            states = await asyncio.gather(*[
                self._dut.get_cache_state(c, cache_line_base)
                for c in range(self._dut.num_cores)
            ])
            invalidations_this_round = sum(
                1 for s in states if s == CacheState.INVALID
            )
            invalidation_count += invalidations_this_round
            await self._dut.wait_cycles(1)

        # Excessive invalidations: more than 50% of rounds had full evictions
        cycle = await self._dut.get_cycle_count()
        expected_max = (cycles // self._dut.num_cores) * 1
        if invalidation_count > expected_max * self._dut.num_cores * 0.5:
            self._record_bug(CoherencyBug(
                bug_type    = "FALSE_SHARING",
                address     = cache_line_base,
                severity    = BugSeverity.MEDIUM,
                cycle       = cycle,
                core_ids    = list(range(self._dut.num_cores)),
                description = (
                    f"Excessive cache invalidations ({invalidation_count}) "
                    f"from false sharing on cache line {hex(cache_line_base)}."
                ),
            ))

    async def _atomic_violation(self, cycles: int) -> None:
        """
        ATTACK: Interleave CAS operations from multiple cores on same address.
        BUG: CAS appears to succeed on both cores — atomicity violation.
        """
        for _ in range(0, cycles, 4):
            addr    = self._random_aligned_addr()
            initial = 0x0000_1111

            await self._dut.core_write(0, addr, initial)

            # Both cores try to CAS from initial to their own value simultaneously
            results = await asyncio.gather(*[
                self._dut.atomic_compare_swap(c, addr, initial, 0xC000_0000 + c)
                for c in range(min(2, self._dut.num_cores))
            ])
            cycle = await self._dut.get_cycle_count()

            # At most ONE CAS can succeed — if both return True, that's a bug
            successes = sum(1 for r in results if r is True)
            if successes > 1:
                self._record_bug(CoherencyBug(
                    bug_type    = "ATOMIC_VIOLATION",
                    address     = addr,
                    severity    = BugSeverity.CRITICAL,
                    cycle       = cycle,
                    core_ids    = [0, 1],
                    description = (
                        f"CAS atomicity violation: {successes} cores "
                        f"simultaneously succeeded on address {hex(addr)}."
                    ),
                ))
            await self._dut.wait_cycles(2)

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _run_with_timeout(
        self, name: str, coro_fn, cycles: int
    ) -> None:
        try:
            await asyncio.wait_for(coro_fn(cycles), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[ConflictAgent] Pattern '{name}' timed out after {self._timeout}s")
        except Exception as e:
            logger.error(f"[ConflictAgent] Pattern '{name}' error: {e}", exc_info=True)
            raise

    def _record_bug(self, bug: CoherencyBug) -> None:
        self._bugs.append(bug)
        logger.warning(
            f"[ConflictAgent] {bug.severity.value} bug: {bug.bug_type} "
            f"@ {hex(bug.address)} cycle={bug.cycle}"
        )

    def _random_aligned_addr(self) -> int:
        raw = self._rng.randint(0, (1 << self._dut.address_space_bits) - 64)
        return raw & self.CACHE_LINE_MASK

    def _get_transitions(self) -> Dict[str, int]:
        """Get real MESI transition counts from the DUT's access log."""
        if isinstance(self._dut, MockDUT):
            return self._dut.get_mesi_transition_count()
        return {}

    def _compute_coverage(self, transitions: Dict[str, int]) -> float:
        """Coverage = fraction of known MESI transitions that were exercised."""
        if not transitions:
            return 0.0
        seen = {k for k, v in transitions.items() if v > 0}
        return (len(seen & ALL_MESI_TRANSITIONS) / len(ALL_MESI_TRANSITIONS)) * 100.0
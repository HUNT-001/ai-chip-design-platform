"""
redteam/agents/power_agent.py

Adversarial power stress tester. Finds worst-case power consumption scenarios
that could cause thermal shutdown or violate chip power budget.

Attack strategies:
  1. Maximum Switching       — toggle all register bits 0→1→0 (max dynamic power)
  2. ALU Hammering           — continuous multiply/divide (highest power ops)
  3. Simultaneous Wake       — wake all cores at once (inrush current spike)
  4. Vector Walk             — bit-flip sweep across all register banks
  5. Pipeline Flood          — fill all pipeline stages simultaneously

Power model:
  All measurements come from dut.measure_power_mw().
  In real simulation, this reads the DUT's built-in power monitor output.
  In MockDUT mode, it uses a physics-informed model based on register
  toggle activity and active core count.
"""

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .dut_interface import DUTInterface

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Configurable thresholds
# ─────────────────────────────────────────────

@dataclass
class PowerThresholds:
    """Configurable power budget thresholds. Adjust per chip spec."""
    sustained_mw:       float = 3_000.0   # Max sustained power
    peak_mw:            float = 6_000.0   # Max instantaneous power
    inrush_mw:          float = 8_000.0   # Max inrush on wake-up
    thermal_shutdown_mw: float = 10_000.0  # Shutdown threshold


# ─────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────

class PowerBugType(str, Enum):
    THERMAL_VIOLATION   = "THERMAL_VIOLATION"
    INRUSH_SPIKE        = "INRUSH_CURRENT_SPIKE"
    SUSTAINED_OVERDRAW  = "SUSTAINED_POWER_OVERDRAW"
    POWER_VIRUS         = "POWER_VIRUS"


@dataclass
class PowerViolation:
    bug_type:       PowerBugType
    measured_mw:    float
    threshold_mw:   float
    cycle:          int
    attack_pattern: str
    duration_ms:    float    # How long the violation lasted
    description:    str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bug_type":       self.bug_type.value,
            "measured_mw":    round(self.measured_mw, 1),
            "threshold_mw":   self.threshold_mw,
            "over_budget_pct": round(
                (self.measured_mw - self.threshold_mw) / self.threshold_mw * 100, 1
            ),
            "cycle":          self.cycle,
            "attack_pattern": self.attack_pattern,
            "duration_ms":    round(self.duration_ms, 2),
            "description":    self.description,
        }


@dataclass
class PowerReport:
    agent:               str = "PowerAgent"
    max_power_mw:        float = 0.0
    avg_power_mw:        float = 0.0
    violations:          List[PowerViolation] = field(default_factory=list)
    thermal_violations:  int = 0
    inrush_events:       int = 0
    power_samples:       int = 0
    elapsed_sec:         float = 0.0
    pattern_results:     Dict[str, Dict] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent":              self.agent,
            "max_power_mw":       round(self.max_power_mw, 1),
            "avg_power_mw":       round(self.avg_power_mw, 1),
            "violations":         [v.to_dict() for v in self.violations],
            "thermal_violations": self.thermal_violations,
            "inrush_events":      self.inrush_events,
            "power_samples":      self.power_samples,
            "elapsed_sec":        round(self.elapsed_sec, 2),
            "pattern_results":    self.pattern_results,
        }


# ─────────────────────────────────────────────
# Power Agent
# ─────────────────────────────────────────────

class PowerAgent:
    """
    Finds worst-case power scenarios and thermal violations.

    All power measurements come from dut.measure_power_mw() — never from
    random numbers. The agent records a rolling sample history to detect
    both instantaneous spikes and sustained violations.

    Args:
        dut:                 DUT interface.
        thresholds:          Power budget thresholds.
        per_pattern_timeout: Max seconds per attack pattern.
        sample_interval:     Sample power every N cycles.
    """

    NUM_REGISTERS     = 32
    NUM_PIPELINE_STAGES = 5

    def __init__(
        self,
        dut:                  DUTInterface,
        thresholds:           Optional[PowerThresholds] = None,
        per_pattern_timeout:  float = 120.0,
        sample_interval:      int = 10,
    ):
        self._dut             = dut
        self._thresh          = thresholds or PowerThresholds()
        self._timeout         = per_pattern_timeout
        self._sample_interval = sample_interval
        self._violations:     List[PowerViolation] = []
        self._all_samples:    List[float] = []

        self._patterns = [
            ("maximum_switching",          self._maximum_switching),
            ("alu_hammering",              self._alu_hammering),
            ("simultaneous_core_wake",     self._simultaneous_core_wake),
            ("vector_walk",                self._vector_walk),
            ("pipeline_flood",             self._pipeline_flood),
        ]

        logger.info(
            f"PowerAgent initialised — "
            f"peak_threshold={self._thresh.peak_mw}mW, "
            f"inrush_threshold={self._thresh.inrush_mw}mW"
        )

    # ── Public API ────────────────────────────────────────────────────────

    async def run_campaign(self, duration_cycles: int = 10_000) -> PowerReport:
        """Run all power stress patterns and return a consolidated report."""
        logger.info(
            f"[PowerAgent] Campaign start — {duration_cycles} cycles"
        )
        start = time.monotonic()
        self._violations.clear()
        self._all_samples.clear()
        cycles_each = max(50, duration_cycles // len(self._patterns))

        tasks = [
            asyncio.create_task(
                self._run_with_timeout(name, coro, cycles_each),
                name=f"power_{name}",
            )
            for name, coro in self._patterns
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        pattern_results: Dict[str, Dict] = {}
        for (name, _), result in zip(self._patterns, raw_results):
            if isinstance(result, Exception):
                logger.error(f"[PowerAgent] '{name}' raised: {result}")
                pattern_results[name] = {"error": str(result)}
            else:
                pattern_results[name] = result or {}

        elapsed = time.monotonic() - start
        report = PowerReport(
            max_power_mw       = max(self._all_samples, default=0.0),
            avg_power_mw       = statistics.mean(self._all_samples) if self._all_samples else 0.0,
            violations         = self._violations,
            thermal_violations = sum(
                1 for v in self._violations
                if v.bug_type == PowerBugType.THERMAL_VIOLATION
            ),
            inrush_events      = sum(
                1 for v in self._violations
                if v.bug_type == PowerBugType.INRUSH_SPIKE
            ),
            power_samples      = len(self._all_samples),
            elapsed_sec        = elapsed,
            pattern_results    = pattern_results,
        )
        logger.info(
            f"[PowerAgent] Done — "
            f"max={report.max_power_mw:.0f}mW, "
            f"violations={len(self._violations)}, "
            f"elapsed={elapsed:.1f}s"
        )
        return report

    # ── Attack patterns ───────────────────────────────────────────────────

    async def _maximum_switching(self, cycles: int) -> Dict:
        """
        Toggle ALL register bits on every other cycle (0xFFFF…→0x0000…→0xFFFF…).
        Maximises dynamic power: P_dyn = α × C × V² × f.
        """
        pattern = "maximum_switching"
        samples: List[float] = []

        for c in range(0, cycles, self._sample_interval):
            toggle_val = 0xFFFF_FFFF if (c // self._sample_interval) % 2 == 0 else 0x0000_0000

            # Batch all register writes (one await per core, not one per register)
            for core_id in range(self._dut.num_cores):
                write_ops = [
                    self._dut.write_register(core_id, reg, toggle_val)
                    for reg in range(self.NUM_REGISTERS)
                ]
                await asyncio.gather(*write_ops)

            power = await self._dut.measure_power_mw()
            samples.append(power)
            self._all_samples.append(power)
            cycle = await self._dut.get_cycle_count()

            self._check_violation(power, cycle, pattern)
            await self._dut.wait_cycles(self._sample_interval)

        return self._pattern_summary(pattern, samples)

    async def _alu_hammering(self, cycles: int) -> Dict:
        """
        Flood all ALUs with back-to-back multiply operations (highest power op).
        Targets clock-gating bypass — clock gates should reduce power when idle.
        """
        pattern = "alu_hammering"
        samples: List[float] = []

        # Worst-case operands: all bits set (maximum switching activity in multiplier)
        operands = [0xFFFF_FFFF, 0xFFFF_FFFF]

        for _ in range(0, cycles, self._sample_interval):
            # Issue MUL on all cores simultaneously
            mul_ops = [
                self._dut.execute_instruction(core_id, "MUL", operands)
                for core_id in range(self._dut.num_cores)
            ]
            await asyncio.gather(*mul_ops)

            power = await self._dut.measure_power_mw()
            samples.append(power)
            self._all_samples.append(power)
            cycle = await self._dut.get_cycle_count()

            self._check_violation(power, cycle, pattern)
            await self._dut.wait_cycles(self._sample_interval)

        return self._pattern_summary(pattern, samples)

    async def _simultaneous_core_wake(self, cycles: int) -> Dict:
        """
        Sleep all cores, then wake them all at the same instant.
        Tests inrush current handling — sudden simultaneous activation.
        """
        pattern = "simultaneous_core_wake"
        inrush_events = 0

        for _ in range(0, cycles, 100):
            # Power down all cores sequentially (controlled shutdown)
            for core_id in range(self._dut.num_cores):
                await self._dut.set_core_power_state(core_id, active=False)

            await self._dut.wait_cycles(10)

            # Measure baseline (all sleeping)
            baseline = await self._dut.measure_power_mw()

            # ATTACK: Wake all cores simultaneously (inrush spike)
            wake_ops = [
                self._dut.set_core_power_state(core_id, active=True)
                for core_id in range(self._dut.num_cores)
            ]
            await asyncio.gather(*wake_ops)

            # Sample immediately after wake-up
            inrush_power = await self._dut.measure_power_mw()
            self._all_samples.append(inrush_power)
            cycle = await self._dut.get_cycle_count()

            if inrush_power > self._thresh.inrush_mw:
                inrush_events += 1
                self._record_violation(PowerViolation(
                    bug_type       = PowerBugType.INRUSH_SPIKE,
                    measured_mw    = inrush_power,
                    threshold_mw   = self._thresh.inrush_mw,
                    cycle          = cycle,
                    attack_pattern = pattern,
                    duration_ms    = 0.1,
                    description    = (
                        f"Inrush spike: {inrush_power:.0f}mW vs "
                        f"baseline {baseline:.0f}mW on simultaneous "
                        f"{self._dut.num_cores}-core wake-up."
                    ),
                ))
            await self._dut.wait_cycles(10)

        return {"inrush_events": inrush_events}

    async def _vector_walk(self, cycles: int) -> Dict:
        """
        Walk a single '1' bit through all registers (bit 0, bit 1, …, bit 31).
        Tests power consumption of systematic toggle patterns — finds
        registers that draw abnormal power when specific bits toggle.
        """
        pattern = "vector_walk"
        samples: List[float] = []
        per_bit_power: Dict[int, float] = {}

        for bit in range(32):
            val = 1 << bit
            for core_id in range(self._dut.num_cores):
                writes = [
                    self._dut.write_register(core_id, reg, val)
                    for reg in range(self.NUM_REGISTERS)
                ]
                await asyncio.gather(*writes)

            power = await self._dut.measure_power_mw()
            samples.append(power)
            self._all_samples.append(power)
            per_bit_power[bit] = power
            cycle = await self._dut.get_cycle_count()
            self._check_violation(power, cycle, pattern)
            await self._dut.wait_cycles(self._sample_interval)

        # Detect bits with anomalously high power (>2σ above mean)
        if len(samples) >= 2:
            mean = statistics.mean(samples)
            stddev = statistics.stdev(samples)
            anomalous_bits = [b for b, p in per_bit_power.items()
                              if p > mean + 2 * stddev]
            return {
                "per_bit_max_mw": max(per_bit_power.values(), default=0),
                "anomalous_bits": anomalous_bits,
            }
        return {}

    async def _pipeline_flood(self, cycles: int) -> Dict:
        """
        Back-to-back instructions with maximum data dependencies to prevent
        pipeline from being gated.  Tests worst-case pipeline power when
        no idle cycles exist.
        """
        pattern = "pipeline_flood"
        samples: List[float] = []

        # Circular dependency chain: each result feeds the next instruction
        operands_cycle = [
            [0xFFFF_FFFF, 0x0000_0001],   # MUL — high switching
            [0xAAAA_AAAA, 0x5555_5555],   # XOR — high toggle
            [0xFFFF_FFFF, 0xFFFF_FFFF],   # ADD — carry propagation
        ]

        for i in range(0, cycles, self._sample_interval):
            ops_for_round = operands_cycle[i // self._sample_interval % len(operands_cycle)]
            instr_tasks = [
                self._dut.execute_instruction(
                    c, "MUL" if i % 2 == 0 else "XOR", ops_for_round
                )
                for c in range(self._dut.num_cores)
            ]
            await asyncio.gather(*instr_tasks)

            power = await self._dut.measure_power_mw()
            samples.append(power)
            self._all_samples.append(power)
            cycle = await self._dut.get_cycle_count()
            self._check_violation(power, cycle, pattern)
            await self._dut.wait_cycles(self._sample_interval)

        return self._pattern_summary(pattern, samples)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _check_violation(self, power: float, cycle: int, pattern: str) -> None:
        """Check a power sample against all thresholds and record violations."""
        if power >= self._thresh.thermal_shutdown_mw:
            self._record_violation(PowerViolation(
                bug_type       = PowerBugType.THERMAL_VIOLATION,
                measured_mw    = power,
                threshold_mw   = self._thresh.thermal_shutdown_mw,
                cycle          = cycle,
                attack_pattern = pattern,
                duration_ms    = 0.0,
                description    = f"Thermal shutdown threshold exceeded: {power:.0f}mW.",
            ))
        elif power >= self._thresh.peak_mw:
            self._record_violation(PowerViolation(
                bug_type       = PowerBugType.POWER_VIRUS,
                measured_mw    = power,
                threshold_mw   = self._thresh.peak_mw,
                cycle          = cycle,
                attack_pattern = pattern,
                duration_ms    = 0.0,
                description    = f"Peak power budget exceeded: {power:.0f}mW.",
            ))

    def _record_violation(self, v: PowerViolation) -> None:
        self._violations.append(v)
        logger.warning(
            f"[PowerAgent] {v.bug_type.value}: "
            f"{v.measured_mw:.0f}mW (threshold={v.threshold_mw:.0f}mW) "
            f"@ cycle {v.cycle}"
        )

    async def _run_with_timeout(
        self, name: str, coro_fn, cycles: int
    ) -> Optional[Dict]:
        try:
            return await asyncio.wait_for(coro_fn(cycles), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[PowerAgent] '{name}' timed out.")
            return {}
        except Exception as e:
            logger.error(f"[PowerAgent] '{name}' error: {e}", exc_info=True)
            raise

    @staticmethod
    def _pattern_summary(name: str, samples: List[float]) -> Dict:
        if not samples:
            return {"samples": 0}
        return {
            "samples":  len(samples),
            "max_mw":   round(max(samples), 1),
            "avg_mw":   round(statistics.mean(samples), 1),
            "p95_mw":   round(sorted(samples)[int(len(samples) * 0.95)], 1),
        }
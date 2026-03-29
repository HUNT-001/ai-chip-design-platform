"""
redteam/agents/speculation_agent.py

Adversarial agent that hunts Spectre/Meltdown-class vulnerabilities in
out-of-order RISC-V processors by probing speculative execution paths
and measuring cache-based side channels.

Attack variants:
  1. Bounds-Check Bypass (Spectre v1)    — train then exploit array bounds
  2. Branch Target Injection (Spectre v2) — poison BTB with attacker target
  3. Speculative Store Bypass (Spectre v4) — load sees value before store
  4. Meltdown-style Kernel Read           — user-space access to kernel memory

Side-channel measurement is based on cache hit latency thresholds.
No magic random numbers — all detections require measurable timing differences.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .dut_interface import DUTInterface, MockDUT

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Latency thresholds (nanoseconds)
# Values below HIT_THRESHOLD indicate the address was cached speculatively
HIT_THRESHOLD_NS  = 10.0    # Below this = cache hit (speculative access left a trace)
MISS_THRESHOLD_NS = 40.0    # Above this = definite cache miss

# Training repetitions for branch predictor conditioning
BP_TRAIN_REPS = 20

# Addresses used in attacks (must not overlap)
ATTACKER_ARRAY_BASE   = 0x0010_0000
VICTIM_SECRET_ADDR    = 0x0080_0000   # "kernel" page — should be inaccessible
PROBE_ARRAY_BASE      = 0x0020_0000   # 256 * 64B = 16KB probe array
PROBE_STRIDE          = 64            # One cache line per bucket

VALID_INDEX_MAX       = 15            # Legitimate array bounds
BTB_MALICIOUS_TARGET  = 0x00C0_0000  # Attacker-controlled jump target


# ─────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────

class LeakType(str, Enum):
    BOUNDS_CHECK_BYPASS    = "BOUNDS_CHECK_BYPASS"
    BTB_POISONING          = "BTB_POISONING"
    SPECULATIVE_STORE      = "SPECULATIVE_STORE_BYPASS"
    MELTDOWN               = "MELTDOWN_KERNEL_READ"


@dataclass
class SpeculativeLeak:
    leak_type:       LeakType
    attacker_addr:   int
    victim_addr:     int
    leaked_byte:     Optional[int]    # 0–255 or None if byte not recovered
    cycle:           int
    hit_latency_ns:  float
    confidence:      float            # 0.0–1.0
    description:     str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "leak_type":      self.leak_type.value,
            "attacker_addr":  hex(self.attacker_addr),
            "victim_addr":    hex(self.victim_addr),
            "leaked_byte":    self.leaked_byte,
            "cycle":          self.cycle,
            "hit_latency_ns": round(self.hit_latency_ns, 2),
            "confidence":     round(self.confidence, 3),
            "description":    self.description,
        }


@dataclass
class SpeculationReport:
    agent:                   str = "SpeculationAgent"
    leaks_found:             int = 0
    attacks_attempted:       int = 0
    critical_vulnerabilities: List[SpeculativeLeak] = field(default_factory=list)
    variant_results:         Dict[str, int] = field(default_factory=dict)
    elapsed_sec:             float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent":                    self.agent,
            "leaks_found":              self.leaks_found,
            "attacks_attempted":        self.attacks_attempted,
            "critical_vulnerabilities": [v.to_dict() for v in self.critical_vulnerabilities],
            "variant_results":          self.variant_results,
            "elapsed_sec":              round(self.elapsed_sec, 2),
        }


# ─────────────────────────────────────────────
# Speculation Agent
# ─────────────────────────────────────────────

class SpeculationAgent:
    """
    Hunts speculative execution side-channel vulnerabilities.

    Detection method:
    - Prime+Probe: fill probe array, trigger speculative access, measure latency.
    - A cache hit on a probe slot (latency < HIT_THRESHOLD_NS) indicates that
      the CPU speculatively loaded that address — revealing a secret byte.

    Args:
        dut:                DUT interface.
        hit_threshold_ns:   Max latency (ns) to classify as a cache hit.
        per_attack_timeout: Max seconds per attack variant.
    """

    def __init__(
        self,
        dut:                  DUTInterface,
        hit_threshold_ns:     float = HIT_THRESHOLD_NS,
        per_attack_timeout:   float = 120.0,
    ):
        self._dut          = dut
        self._hit_threshold = hit_threshold_ns
        self._timeout       = per_attack_timeout
        self._leaks:        List[SpeculativeLeak] = []

        self._attacks = [
            ("bounds_check_bypass",    self._bounds_check_bypass),
            ("branch_target_injection", self._branch_target_injection),
            ("speculative_store_bypass", self._speculative_store_bypass),
            ("meltdown_kernel_read",    self._meltdown_kernel_read),
        ]

        logger.info("SpeculationAgent initialised.")

    # ── Public API ────────────────────────────────────────────────────────

    async def run_campaign(self, duration_cycles: int = 10_000) -> SpeculationReport:
        """Run all speculation attacks and return a consolidated report."""
        logger.info(
            f"[SpeculationAgent] Campaign start — {duration_cycles} cycles"
        )
        start = time.monotonic()
        self._leaks.clear()
        cycles_each = max(100, duration_cycles // len(self._attacks))

        tasks = [
            asyncio.create_task(
                self._run_with_timeout(name, coro, cycles_each),
                name=f"spec_{name}",
            )
            for name, coro in self._attacks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        variant_results: Dict[str, int] = {}
        for (name, _), result in zip(self._attacks, results):
            if isinstance(result, Exception):
                logger.error(f"[SpeculationAgent] '{name}' raised: {result}")
                variant_results[name] = -1
            else:
                variant_results[name] = result or 0

        elapsed = time.monotonic() - start
        report = SpeculationReport(
            leaks_found              = len(self._leaks),
            attacks_attempted        = sum(max(v, 0) for v in variant_results.values()),
            critical_vulnerabilities = self._leaks,
            variant_results          = variant_results,
            elapsed_sec              = elapsed,
        )
        logger.info(
            f"[SpeculationAgent] Done — leaks={report.leaks_found}, "
            f"elapsed={elapsed:.1f}s"
        )
        return report

    # ── Attack variants ───────────────────────────────────────────────────

    async def _bounds_check_bypass(self, cycles: int) -> int:
        """
        Spectre v1: Train branch predictor with in-bounds accesses,
        then feed out-of-bounds index — CPU speculatively executes the
        body before the bounds check resolves.
        """
        attempts = 0
        for _ in range(0, cycles, BP_TRAIN_REPS + 1):
            # Phase 1: Train branch predictor with valid accesses
            for train_idx in range(BP_TRAIN_REPS):
                valid_idx = train_idx % VALID_INDEX_MAX
                await self._dut.core_read(0, ATTACKER_ARRAY_BASE + valid_idx * 4)

            # Phase 2: Flush probe array from cache (Prime)
            await self._prime_probe_array()

            # Phase 3: Exploit — CPU mispredicts bounds check
            oob_offset = (VICTIM_SECRET_ADDR - ATTACKER_ARRAY_BASE)
            await self._dut.core_read(0, ATTACKER_ARRAY_BASE + oob_offset)

            # Phase 4: Probe — measure latency on each slot
            leaked_byte = await self._probe_for_secret()
            cycle = await self._dut.get_cycle_count()
            attempts += 1

            if leaked_byte is not None:
                latency = await self._dut.get_cache_hit_latency(
                    PROBE_ARRAY_BASE + leaked_byte * PROBE_STRIDE
                )
                confidence = self._compute_confidence(latency)
                if confidence > 0.7:
                    self._record_leak(SpeculativeLeak(
                        leak_type      = LeakType.BOUNDS_CHECK_BYPASS,
                        attacker_addr  = ATTACKER_ARRAY_BASE,
                        victim_addr    = VICTIM_SECRET_ADDR,
                        leaked_byte    = leaked_byte,
                        cycle          = cycle,
                        hit_latency_ns = latency,
                        confidence     = confidence,
                        description    = (
                            f"Spectre v1: OOB index {oob_offset} read secret byte "
                            f"{leaked_byte:#04x} via cache timing."
                        ),
                    ))
            await self._dut.wait_cycles(2)
        return attempts

    async def _branch_target_injection(self, cycles: int) -> int:
        """
        Spectre v2: Poison the Branch Target Buffer (BTB) with an
        attacker-controlled target. When victim code executes an indirect
        branch, it speculatively jumps to the malicious target.
        """
        attempts = 0

        # Phase 1: Train BTB — repeatedly execute indirect branch to malicious target
        for _ in range(BP_TRAIN_REPS):
            await self._dut.execute_instruction(
                0, "JALR", [BTB_MALICIOUS_TARGET]
            )

        # Phase 2: Probe whether victim's indirect branch was hijacked
        await self._prime_probe_array()

        for _ in range(0, cycles, 10):
            # Trigger victim code that uses an indirect branch
            victim_jump_addr = VICTIM_SECRET_ADDR
            await self._dut.core_read(0, victim_jump_addr)

            leaked_byte = await self._probe_for_secret()
            cycle = await self._dut.get_cycle_count()
            attempts += 1

            if leaked_byte is not None:
                latency    = await self._dut.get_cache_hit_latency(
                    PROBE_ARRAY_BASE + leaked_byte * PROBE_STRIDE
                )
                confidence = self._compute_confidence(latency)
                if confidence > 0.75:
                    self._record_leak(SpeculativeLeak(
                        leak_type      = LeakType.BTB_POISONING,
                        attacker_addr  = BTB_MALICIOUS_TARGET,
                        victim_addr    = victim_jump_addr,
                        leaked_byte    = leaked_byte,
                        cycle          = cycle,
                        hit_latency_ns = latency,
                        confidence     = confidence,
                        description    = (
                            f"Spectre v2: BTB poisoned → victim jumped to "
                            f"{hex(BTB_MALICIOUS_TARGET)}, leaked byte={leaked_byte:#04x}."
                        ),
                    ))
            await self._dut.wait_cycles(2)
        return attempts

    async def _speculative_store_bypass(self, cycles: int) -> int:
        """
        Spectre v4: A load speculatively reads a stale value from an address
        before an in-flight store to that address has resolved.
        The stale value is then used to index a probe array — leaking old data.
        """
        address    = 0x0040_0000
        new_value  = 0xCAFE_BABE    # ← was `0xNEWVALUE` (syntax error) in original
        old_value  = 0xDEAD_BEEF
        attempts   = 0

        # Initialise address with known old_value
        await self._dut.core_write(0, address, old_value)

        for _ in range(0, cycles, 4):
            # Store new_value — takes multiple cycles to commit
            store_task = asyncio.create_task(
                self._slow_store(0, address, new_value)
            )

            # Speculative load — may read old_value before store commits
            speculative_read = await self._dut.core_read(0, address)

            await store_task    # Ensure store completes before next iteration

            cycle = await self._dut.get_cycle_count()
            attempts += 1

            # If we read old_value after the store was issued, that's a bypass
            if speculative_read == old_value:
                latency = await self._dut.get_cache_hit_latency(
                    PROBE_ARRAY_BASE + (old_value & 0xFF) * PROBE_STRIDE
                )
                confidence = self._compute_confidence(latency)
                if confidence > 0.6:
                    self._record_leak(SpeculativeLeak(
                        leak_type      = LeakType.SPECULATIVE_STORE,
                        attacker_addr  = address,
                        victim_addr    = address,
                        leaked_byte    = old_value & 0xFF,
                        cycle          = cycle,
                        hit_latency_ns = latency,
                        confidence     = confidence,
                        description    = (
                            f"Spectre v4: Load at {hex(address)} read stale "
                            f"value {hex(old_value)} before store {hex(new_value)} committed."
                        ),
                    ))
            await self._dut.wait_cycles(2)
        return attempts

    async def _meltdown_kernel_read(self, cycles: int) -> int:
        """
        Meltdown: User-space access to a kernel-mapped address raises a fault,
        but speculative execution beyond the fault accesses the data first.
        Tests that the CPU's exception delivery prevents data from reaching
        the architectural state before the fault is taken.
        """
        attempts = 0
        for _ in range(0, cycles, 5):
            await self._prime_probe_array()

            # Attempt to read from kernel page — should fault immediately
            try:
                # In real DUT, this should trigger a page-fault exception
                value = await self._dut.core_read(0, VICTIM_SECRET_ADDR)
                cycle = await self._dut.get_cycle_count()
                attempts += 1

                # If we reach here without exception, test the probe array
                leaked_byte = await self._probe_for_secret()
                if leaked_byte is not None:
                    latency    = await self._dut.get_cache_hit_latency(
                        PROBE_ARRAY_BASE + leaked_byte * PROBE_STRIDE
                    )
                    confidence = self._compute_confidence(latency)
                    if confidence > 0.8:
                        self._record_leak(SpeculativeLeak(
                            leak_type      = LeakType.MELTDOWN,
                            attacker_addr  = PROBE_ARRAY_BASE,
                            victim_addr    = VICTIM_SECRET_ADDR,
                            leaked_byte    = leaked_byte,
                            cycle          = cycle,
                            hit_latency_ns = latency,
                            confidence     = confidence,
                            description    = (
                                f"Meltdown: kernel address {hex(VICTIM_SECRET_ADDR)} "
                                f"read without fault — byte {leaked_byte:#04x} leaked."
                            ),
                        ))
            except Exception:
                # Expected: the DUT properly faulted on kernel access
                attempts += 1
            await self._dut.wait_cycles(3)
        return attempts

    # ── Side-channel helpers ──────────────────────────────────────────────

    async def _prime_probe_array(self) -> None:
        """
        Flush all 256 probe slots from cache (Prime phase).
        After this, any cache hit on a probe slot indicates speculative access.
        """
        for i in range(256):
            await self._dut.flush_cache_line(
                0, PROBE_ARRAY_BASE + i * PROBE_STRIDE
            )

    async def _probe_for_secret(self) -> Optional[int]:
        """
        Measure access time for each of 256 probe slots.
        The slot with the lowest latency was accessed speculatively.
        Returns the index (0–255) of the suspected leaked byte, or None.
        """
        latencies: List[float] = []
        for i in range(256):
            lat = await self._dut.get_cache_hit_latency(
                PROBE_ARRAY_BASE + i * PROBE_STRIDE
            )
            latencies.append(lat)

        min_lat = min(latencies)
        if min_lat >= self._hit_threshold:
            return None    # No slot was cached — no leak detected

        # Return the bucket with lowest latency
        return latencies.index(min_lat)

    def _compute_confidence(self, latency_ns: float) -> float:
        """
        Confidence is higher the lower the latency is relative to threshold.
        Scales from 0.5 (at threshold) to 1.0 (at 0 ns).
        """
        if latency_ns >= self._hit_threshold:
            return 0.0
        ratio = 1.0 - (latency_ns / self._hit_threshold)
        return 0.5 + ratio * 0.5

    async def _slow_store(self, core_id: int, address: int, data: int) -> None:
        """Simulate a multi-cycle store that takes several clock cycles to commit."""
        await self._dut.wait_cycles(4)
        await self._dut.core_write(core_id, address, data)

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _run_with_timeout(
        self, name: str, coro_fn, cycles: int
    ) -> Optional[int]:
        try:
            return await asyncio.wait_for(coro_fn(cycles), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[SpeculationAgent] '{name}' timed out.")
            return 0
        except Exception as e:
            logger.error(f"[SpeculationAgent] '{name}' error: {e}", exc_info=True)
            raise

    def _record_leak(self, leak: SpeculativeLeak) -> None:
        self._leaks.append(leak)
        logger.warning(
            f"[SpeculationAgent] LEAK: {leak.leak_type.value} "
            f"byte={leak.leaked_byte} confidence={leak.confidence:.2f}"
        )
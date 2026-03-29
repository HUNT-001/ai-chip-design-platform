"""
redteam/agents/dut_interface.py

Abstract DUT (Device Under Test) interface used by all red-team agents.

Decouples agent logic from cocotb signal manipulation so that:
- Agents can be unit-tested with the MockDUT without a simulator.
- Switching to real cocotb requires only swapping the DUT implementation.
- All signal access is logged for post-analysis.

Usage:
    # Real cocotb (inside a @cocotb.test() coroutine):
    dut_if = CocotbDUT(dut, clk=dut.clk)

    # Mock (for API / unit tests):
    dut_if = MockDUT(num_cores=4)

    # Both expose the same interface:
    value = await dut_if.core_read(core_id=0, address=0x1000)
    await dut_if.core_write(core_id=1, address=0x1000, data=0xDEADBEEF)
"""

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────

class CacheState(Enum):
    MODIFIED  = "M"
    EXCLUSIVE = "E"
    SHARED    = "S"
    INVALID   = "I"


class AccessType(str, Enum):
    READ         = "read"
    WRITE        = "write"
    ATOMIC_READ  = "atomic_read"
    ATOMIC_WRITE = "atomic_write"
    EVICT        = "evict"
    FLUSH        = "flush"


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class CacheAccess:
    core_id:     int
    address:     int
    access_type: AccessType
    cycle:       int
    state_before: CacheState
    state_after:  CacheState
    data:         Optional[int] = None
    latency_ns:   float = 0.0


@dataclass
class PowerSample:
    cycle:       int
    power_mw:    float
    timestamp_ns: float
    core_id:     Optional[int] = None


@dataclass
class BranchEvent:
    pc:           int
    target:       int
    taken:        bool
    speculative:  bool
    cycle:        int
    mispredicted: bool = False


# ─────────────────────────────────────────────
# Abstract DUT Interface
# ─────────────────────────────────────────────

class DUTInterface(ABC):
    """
    Abstract interface for all DUT operations used by red-team agents.

    Subclass this for:
    - Real cocotb simulation (CocotbDUT)
    - Mock testing without a simulator (MockDUT)
    - Future: remote DUT over gRPC
    """

    @abstractmethod
    async def core_read(self, core_id: int, address: int) -> int:
        """Issue a read from the given core. Returns data value."""

    @abstractmethod
    async def core_write(self, core_id: int, address: int, data: int) -> None:
        """Issue a write from the given core."""

    @abstractmethod
    async def atomic_compare_swap(
        self, core_id: int, address: int, expected: int, new_value: int
    ) -> bool:
        """Atomic CAS. Returns True if swap occurred."""

    @abstractmethod
    async def flush_cache_line(self, core_id: int, address: int) -> None:
        """Flush a cache line from the given core's cache."""

    @abstractmethod
    async def get_cache_state(self, core_id: int, address: int) -> CacheState:
        """Return the current MESI state of a cache line."""

    @abstractmethod
    async def get_cache_hit_latency(self, address: int) -> float:
        """Return measured cache hit latency in ns (for side-channel analysis)."""

    @abstractmethod
    async def set_core_power_state(self, core_id: int, active: bool) -> None:
        """Wake or sleep a core."""

    @abstractmethod
    async def measure_power_mw(self) -> float:
        """Return current chip power draw in milliwatts."""

    @abstractmethod
    async def write_register(self, core_id: int, reg_id: int, data: int) -> None:
        """Write a value into a general-purpose register."""

    @abstractmethod
    async def execute_instruction(
        self, core_id: int, opcode: str, operands: List[int]
    ) -> Optional[int]:
        """Execute one instruction. Returns result value if applicable."""

    @abstractmethod
    async def get_cycle_count(self) -> int:
        """Return the current simulation cycle count."""

    @abstractmethod
    async def wait_cycles(self, n: int) -> None:
        """Advance simulation by n clock cycles."""

    @property
    @abstractmethod
    def num_cores(self) -> int:
        """Number of cores in the DUT."""

    @property
    @abstractmethod
    def address_space_bits(self) -> int:
        """Address bus width in bits."""


# ─────────────────────────────────────────────
# Cocotb DUT (real simulation)
# ─────────────────────────────────────────────

class CocotbDUT(DUTInterface):
    """
    Real cocotb DUT implementation.
    Wraps actual signal assignments and RisingEdge waits.

    This class must be instantiated inside a @cocotb.test() coroutine.
    """

    def __init__(self, dut: Any, clk: Any, num_cores_: int = 4):
        self._dut       = dut
        self._clk       = clk
        self._num_cores = num_cores_
        self._cycle     = 0

        # Lazy import — cocotb only available in simulation context
        try:
            from cocotb.triggers import RisingEdge, Timer
            self._RisingEdge = RisingEdge
            self._Timer      = Timer
        except ImportError:
            raise RuntimeError(
                "CocotbDUT requires cocotb. "
                "Use MockDUT outside of a simulation context."
            )

    async def core_read(self, core_id: int, address: int) -> int:
        self._dut.cores[core_id].read_addr.value  = address
        self._dut.cores[core_id].read_req.value   = 1
        await self._RisingEdge(self._clk)
        self._dut.cores[core_id].read_req.value   = 0

        # Wait for acknowledgement (up to 64 cycles)
        for _ in range(64):
            await self._RisingEdge(self._clk)
            if self._dut.cores[core_id].read_ack.value == 1:
                return int(self._dut.cores[core_id].read_data.value)
        raise TimeoutError(f"Core {core_id} read from {hex(address)} timed out")

    async def core_write(self, core_id: int, address: int, data: int) -> None:
        self._dut.cores[core_id].write_addr.value = address
        self._dut.cores[core_id].write_data.value = data
        self._dut.cores[core_id].write_req.value  = 1
        await self._RisingEdge(self._clk)
        self._dut.cores[core_id].write_req.value  = 0
        for _ in range(64):
            await self._RisingEdge(self._clk)
            if self._dut.cores[core_id].write_ack.value == 1:
                return
        raise TimeoutError(f"Core {core_id} write to {hex(address)} timed out")

    async def atomic_compare_swap(self, core_id, address, expected, new_value):
        self._dut.cores[core_id].cas_addr.value     = address
        self._dut.cores[core_id].cas_expected.value = expected
        self._dut.cores[core_id].cas_new.value      = new_value
        self._dut.cores[core_id].cas_req.value      = 1
        await self._RisingEdge(self._clk)
        self._dut.cores[core_id].cas_req.value      = 0
        for _ in range(64):
            await self._RisingEdge(self._clk)
            if self._dut.cores[core_id].cas_ack.value == 1:
                return bool(self._dut.cores[core_id].cas_success.value)
        raise TimeoutError("CAS timed out")

    async def flush_cache_line(self, core_id, address):
        self._dut.cores[core_id].flush_addr.value = address
        self._dut.cores[core_id].flush_req.value  = 1
        await self._RisingEdge(self._clk)
        self._dut.cores[core_id].flush_req.value  = 0
        await self._RisingEdge(self._clk)

    async def get_cache_state(self, core_id, address) -> CacheState:
        self._dut.cores[core_id].state_query_addr.value = address
        await self._RisingEdge(self._clk)
        raw = int(self._dut.cores[core_id].cache_state.value)
        mapping = {0: CacheState.INVALID, 1: CacheState.SHARED,
                   2: CacheState.EXCLUSIVE, 3: CacheState.MODIFIED}
        return mapping.get(raw, CacheState.INVALID)

    async def get_cache_hit_latency(self, address: int) -> float:
        t0 = time.perf_counter()
        self._dut.latency_probe_addr.value = address
        self._dut.latency_probe_req.value  = 1
        await self._RisingEdge(self._clk)
        self._dut.latency_probe_req.value  = 0
        for _ in range(256):
            await self._RisingEdge(self._clk)
            if self._dut.latency_probe_ack.value == 1:
                return (time.perf_counter() - t0) * 1e9   # nanoseconds
        return -1.0   # Probe timed out

    async def set_core_power_state(self, core_id, active):
        self._dut.cores[core_id].power_en.value = 1 if active else 0
        await self._RisingEdge(self._clk)

    async def measure_power_mw(self) -> float:
        # Assumes DUT exposes power monitor output in fixed-point mW
        return float(self._dut.power_monitor_mw.value)

    async def write_register(self, core_id, reg_id, data):
        self._dut.cores[core_id].reg_wr_addr.value = reg_id
        self._dut.cores[core_id].reg_wr_data.value = data
        self._dut.cores[core_id].reg_wr_en.value   = 1
        await self._RisingEdge(self._clk)
        self._dut.cores[core_id].reg_wr_en.value   = 0

    async def execute_instruction(self, core_id, opcode, operands):
        opcode_map = {"MUL": 0x1, "DIV": 0x2, "ADD": 0x3, "XOR": 0x4}
        self._dut.cores[core_id].instr_op.value  = opcode_map.get(opcode, 0)
        self._dut.cores[core_id].instr_a.value   = operands[0] if operands else 0
        self._dut.cores[core_id].instr_b.value   = operands[1] if len(operands) > 1 else 0
        self._dut.cores[core_id].instr_req.value = 1
        await self._RisingEdge(self._clk)
        self._dut.cores[core_id].instr_req.value = 0
        for _ in range(32):
            await self._RisingEdge(self._clk)
            if self._dut.cores[core_id].instr_done.value == 1:
                return int(self._dut.cores[core_id].instr_result.value)
        return None

    async def get_cycle_count(self) -> int:
        return int(self._dut.cycle_counter.value)

    async def wait_cycles(self, n: int) -> None:
        for _ in range(n):
            await self._RisingEdge(self._clk)

    @property
    def num_cores(self) -> int:
        return self._num_cores

    @property
    def address_space_bits(self) -> int:
        return 32


# ─────────────────────────────────────────────
# Mock DUT (deterministic, for testing/API mode)
# ─────────────────────────────────────────────

class MockDUT(DUTInterface):
    """
    Deterministic mock DUT for testing agents without a real simulator.

    Uses a simple in-memory address space with MESI state tracking.
    Deliberately injects faults based on configurable fault_rate so
    agents can exercise their bug-detection logic.

    Args:
        num_cores_:   Number of simulated cores.
        fault_rate:   Probability [0, 1] of injecting a coherency fault.
        seed:         Random seed for reproducibility.
    """

    CACHE_LINE_SIZE = 64   # bytes

    def __init__(
        self,
        num_cores_: int  = 4,
        fault_rate: float = 0.02,
        seed:       int   = 42,
    ):
        self._num_cores     = num_cores_
        self._fault_rate    = fault_rate
        self._rng           = random.Random(seed)
        self._memory:        Dict[int, int]  = defaultdict(int)
        self._cache_states:  Dict[tuple, CacheState] = {}
        self._power_state:   List[bool]      = [True] * num_cores_
        self._registers:     List[Dict[int, int]] = [defaultdict(int) for _ in range(num_cores_)]
        self._cycle:         int  = 0
        self._power_base_mw: float = 1500.0
        self._accesses:      List[CacheAccess] = []

    # ── DUT operations ────────────────────────────────────────────────────

    async def core_read(self, core_id: int, address: int) -> int:
        self._cycle += 1
        aligned = address & ~(self.CACHE_LINE_SIZE - 1)
        state_before = self._cache_states.get((core_id, aligned), CacheState.INVALID)

        # Fault injection: return stale data occasionally
        if self._rng.random() < self._fault_rate:
            stale = self._rng.randint(0, 0xFFFFFFFF)
            self._record_access(core_id, address, AccessType.READ,
                                state_before, state_before, stale)
            return stale

        value = self._memory[aligned]
        self._cache_states[(core_id, aligned)] = CacheState.SHARED
        self._record_access(core_id, address, AccessType.READ,
                            state_before, CacheState.SHARED, value)
        return value

    async def core_write(self, core_id: int, address: int, data: int) -> None:
        self._cycle += 1
        aligned = address & ~(self.CACHE_LINE_SIZE - 1)
        state_before = self._cache_states.get((core_id, aligned), CacheState.INVALID)

        self._memory[aligned] = data
        # Invalidate other cores (MESI protocol)
        for other in range(self._num_cores):
            if other != core_id:
                self._cache_states[(other, aligned)] = CacheState.INVALID
        self._cache_states[(core_id, aligned)] = CacheState.MODIFIED
        self._record_access(core_id, address, AccessType.WRITE,
                            state_before, CacheState.MODIFIED, data)

    async def atomic_compare_swap(self, core_id, address, expected, new_value):
        aligned = address & ~(self.CACHE_LINE_SIZE - 1)
        current = self._memory[aligned]
        if current == expected:
            await self.core_write(core_id, address, new_value)
            return True
        return False

    async def flush_cache_line(self, core_id, address):
        aligned = address & ~(self.CACHE_LINE_SIZE - 1)
        self._cache_states[(core_id, aligned)] = CacheState.INVALID

    async def get_cache_state(self, core_id, address) -> CacheState:
        aligned = address & ~(self.CACHE_LINE_SIZE - 1)
        return self._cache_states.get((core_id, aligned), CacheState.INVALID)

    async def get_cache_hit_latency(self, address: int) -> float:
        aligned = address & ~(self.CACHE_LINE_SIZE - 1)
        # Any core having this line in M/E/S = cache hit = low latency
        for core_id in range(self._num_cores):
            state = self._cache_states.get((core_id, aligned), CacheState.INVALID)
            if state != CacheState.INVALID:
                return self._rng.uniform(0.5, 2.0)    # Cache hit: ~1ns
        return self._rng.uniform(50.0, 150.0)          # Cache miss: ~100ns

    async def set_core_power_state(self, core_id, active):
        self._power_state[core_id] = active

    async def measure_power_mw(self) -> float:
        active_cores = sum(1 for s in self._power_state if s)
        base = self._power_base_mw * active_cores / self._num_cores
        # Reflect register toggle activity in power estimate
        toggle_factor = self._estimate_toggle_factor()
        return base * toggle_factor

    async def write_register(self, core_id, reg_id, data):
        self._registers[core_id][reg_id] = data

    async def execute_instruction(self, core_id, opcode, operands):
        a = operands[0] if operands else 0
        b = operands[1] if len(operands) > 1 else 1
        mapping = {
            "MUL": lambda: (a * b) & 0xFFFFFFFF,
            "DIV": lambda: (a // max(b, 1)) & 0xFFFFFFFF,
            "ADD": lambda: (a + b) & 0xFFFFFFFF,
            "XOR": lambda: (a ^ b) & 0xFFFFFFFF,
        }
        return mapping.get(opcode, lambda: 0)()

    async def get_cycle_count(self) -> int:
        return self._cycle

    async def wait_cycles(self, n: int) -> None:
        self._cycle += n
        await asyncio.sleep(0)   # Yield to event loop without wall-clock delay

    @property
    def num_cores(self) -> int:
        return self._num_cores

    @property
    def address_space_bits(self) -> int:
        return 32

    # ── Internal helpers ──────────────────────────────────────────────────

    def _record_access(
        self, core_id, address, access_type,
        state_before, state_after, data=None
    ):
        self._accesses.append(CacheAccess(
            core_id      = core_id,
            address      = address,
            access_type  = access_type,
            cycle        = self._cycle,
            state_before = state_before,
            state_after  = state_after,
            data         = data,
        ))
        # Keep bounded
        if len(self._accesses) > 50_000:
            self._accesses = self._accesses[-50_000:]

    def _estimate_toggle_factor(self) -> float:
        """Estimate power multiplier based on recent register activity."""
        if not self._accesses:
            return 1.0
        recent = self._accesses[-100:]
        writes = sum(1 for a in recent if a.access_type == AccessType.WRITE)
        return 1.0 + (writes / max(len(recent), 1)) * 3.0

    def get_access_log(self) -> List[CacheAccess]:
        return list(self._accesses)

    def get_mesi_transition_count(self) -> Dict[str, int]:
        """Count MESI state transitions — used for real coverage metrics."""
        counts: Dict[str, int] = defaultdict(int)
        for acc in self._accesses:
            key = f"{acc.state_before.value}→{acc.state_after.value}"
            counts[key] += 1
        return dict(counts)
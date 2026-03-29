"""
core/resilient_executor.py

Fault-tolerant wrapper for simulation tasks.
Provides: retry with exponential backoff, hard timeout, zombie-process
cleanup, and a simple circuit-breaker to stop hammering a broken tool.

Dependencies:
    pip install tenacity   (optional but recommended)
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Optional: tenacity for decorator-style retries
# ─────────────────────────────────────────────
try:
    from tenacity import (
        AsyncRetrying,
        RetryError,
        stop_after_attempt,
        wait_exponential,
        retry_if_exception_type,
    )
    TENACITY_AVAILABLE = True
except ImportError:
    TENACITY_AVAILABLE = False
    logger.warning(
        "tenacity not installed — using built-in retry logic. "
        "Run: pip install tenacity"
    )


# ─────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────

@dataclass
class ExecutionResult:
    success:      bool
    value:        Any                    = None
    error:        Optional[str]         = None
    timed_out:    bool                   = False
    attempts:     int                    = 1
    elapsed_sec:  float                  = 0.0
    circuit_open: bool                   = False   # True when circuit breaker fired

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success":       self.success,
            "error":         self.error,
            "timed_out":     self.timed_out,
            "attempts":      self.attempts,
            "elapsed_sec":   round(self.elapsed_sec, 3),
            "circuit_open":  self.circuit_open,
        }


# ─────────────────────────────────────────────
# Circuit Breaker
# ─────────────────────────────────────────────

class CircuitBreaker:
    """
    Classic three-state circuit breaker (CLOSED → OPEN → HALF-OPEN).

    - CLOSED:     normal operation, failures are counted.
    - OPEN:       calls are rejected immediately (system is unhealthy).
    - HALF-OPEN:  one probe allowed; resets to CLOSED on success, re-opens on failure.
    """

    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int   = 5,
        recovery_timeout:  float = 60.0,    # seconds before moving to HALF-OPEN
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._state            = self.CLOSED
        self._failure_count    = 0
        self._opened_at:  Optional[float] = None

    # ── Public ────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        self._maybe_recover()
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == self.OPEN

    def record_success(self) -> None:
        self._failure_count = 0
        self._state         = self.CLOSED
        self._opened_at     = None

    def record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            if self._state != self.OPEN:
                logger.warning(
                    f"Circuit breaker OPENED after {self._failure_count} failures."
                )
            self._state     = self.OPEN
            self._opened_at = time.monotonic()

    def reset(self) -> None:
        self._state         = self.CLOSED
        self._failure_count = 0
        self._opened_at     = None

    # ── Internal ──────────────────────────────────────────────────────────

    def _maybe_recover(self) -> None:
        if (
            self._state == self.OPEN
            and self._opened_at is not None
            and (time.monotonic() - self._opened_at) >= self.recovery_timeout
        ):
            logger.info("Circuit breaker moving to HALF-OPEN — probing.")
            self._state = self.HALF_OPEN


# ─────────────────────────────────────────────
# Resilient Executor
# ─────────────────────────────────────────────

class ResilientExecutor:
    """
    Fault-tolerant async executor for simulation and LLM tasks.

    Features:
    - Retry with exponential backoff (tenacity if available, built-in otherwise).
    - Hard timeout that kills hung subprocesses.
    - Zombie-process and temp-artifact cleanup.
    - Per-executor circuit breaker to stop flooding a broken service.
    - Full execution metrics on every result.

    Usage:
        executor = ResilientExecutor(timeout_sec=300, max_attempts=3)
        result   = await executor.run(some_async_task, arg1, arg2)
    """

    def __init__(
        self,
        timeout_sec:        int   = 300,
        max_attempts:       int   = 3,
        backoff_min_sec:    float = 4.0,
        backoff_max_sec:    float = 60.0,
        backoff_multiplier: float = 2.0,
        circuit_breaker:    Optional[CircuitBreaker] = None,
        work_dir:           Optional[Path] = None,
    ):
        self.timeout_sec        = timeout_sec
        self.max_attempts       = max_attempts
        self.backoff_min_sec    = backoff_min_sec
        self.backoff_max_sec    = backoff_max_sec
        self.backoff_multiplier = backoff_multiplier
        self.circuit_breaker    = circuit_breaker or CircuitBreaker()
        self.work_dir           = work_dir or Path(
            os.environ.get("SIM_WORK_DIR", "/tmp/sim_artifacts")
        )
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # Internal stats
        self._total_runs:    int = 0
        self._total_success: int = 0
        self._total_timeout: int = 0

    # ── Public API ────────────────────────────────────────────────────────

    async def run(
        self,
        coro_fn: Callable[..., Coroutine],
        *args: Any,
        **kwargs: Any,
    ) -> ExecutionResult:
        """
        Execute ``coro_fn(*args, **kwargs)`` with retry, timeout, and circuit breaker.

        Args:
            coro_fn: An async callable (coroutine function).
            *args:   Positional arguments passed to ``coro_fn``.
            **kwargs: Keyword arguments passed to ``coro_fn``.

        Returns:
            ExecutionResult with full metrics.
        """
        self._total_runs += 1

        # ── Circuit breaker guard ─────────────────────────────────────────
        if self.circuit_breaker.is_open:
            logger.warning("Circuit breaker is OPEN — rejecting execution.")
            return ExecutionResult(
                success      = False,
                error        = "Circuit breaker open — too many recent failures.",
                circuit_open = True,
            )

        start    = time.monotonic()
        attempt  = 0
        last_err = ""

        while attempt < self.max_attempts:
            attempt += 1
            logger.info(f"[Executor] Attempt {attempt}/{self.max_attempts}…")

            try:
                value = await asyncio.wait_for(
                    coro_fn(*args, **kwargs),
                    timeout=self.timeout_sec,
                )
                self._total_success += 1
                self.circuit_breaker.record_success()
                return ExecutionResult(
                    success     = True,
                    value       = value,
                    attempts    = attempt,
                    elapsed_sec = time.monotonic() - start,
                )

            except asyncio.TimeoutError:
                self._total_timeout += 1
                self.circuit_breaker.record_failure()
                logger.warning(
                    f"[Executor] Timeout after {self.timeout_sec}s "
                    f"(attempt {attempt}) — suspected livelock."
                )
                await self._cleanup()
                return ExecutionResult(
                    success     = False,
                    timed_out   = True,
                    error       = f"Timed out after {self.timeout_sec}s.",
                    attempts    = attempt,
                    elapsed_sec = time.monotonic() - start,
                )

            except Exception as e:
                last_err = str(e)
                self.circuit_breaker.record_failure()
                logger.error(
                    f"[Executor] Attempt {attempt} failed: {e}", exc_info=True
                )
                await self._cleanup()

                if attempt < self.max_attempts:
                    delay = min(
                        self.backoff_min_sec * (self.backoff_multiplier ** (attempt - 1)),
                        self.backoff_max_sec,
                    )
                    logger.info(f"[Executor] Retrying in {delay:.1f}s…")
                    await asyncio.sleep(delay)

        return ExecutionResult(
            success     = False,
            error       = f"All {self.max_attempts} attempts failed. Last: {last_err}",
            attempts    = attempt,
            elapsed_sec = time.monotonic() - start,
        )

    async def run_subprocess(
        self,
        cmd: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """
        Convenience wrapper: run a shell command with the same retry / timeout
        / circuit-breaker guarantees as ``run()``.

        Returns ExecutionResult where ``value`` is
        ``{"stdout": str, "stderr": str, "returncode": int}``.
        """
        async def _run_cmd() -> Dict[str, Any]:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env or dict(os.environ),
            )
            self._track_process(proc)
            stdout_b, stderr_b = await proc.communicate()
            self._untrack_process(proc)
            return {
                "stdout":     stdout_b.decode(errors="replace"),
                "stderr":     stderr_b.decode(errors="replace"),
                "returncode": proc.returncode,
            }

        return await self.run(_run_cmd)

    def stats(self) -> Dict[str, Any]:
        return {
            "total_runs":      self._total_runs,
            "total_success":   self._total_success,
            "total_timeouts":  self._total_timeout,
            "total_failures":  self._total_runs - self._total_success,
            "circuit_state":   self.circuit_breaker.state,
        }

    # ── Internal: process tracking ────────────────────────────────────────

    _tracked_pids: List[int] = []          # class-level, shared across instances

    def _track_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.pid:
            ResilientExecutor._tracked_pids.append(proc.pid)

    def _untrack_process(self, proc: asyncio.subprocess.Process) -> None:
        try:
            ResilientExecutor._tracked_pids.remove(proc.pid)
        except ValueError:
            pass

    # ── Internal: cleanup ─────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        """Kill zombie Verilator processes and clean up temp artifacts."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sync_cleanup)

    def _sync_cleanup(self) -> None:
        # Kill known tracked PIDs
        for pid in list(ResilientExecutor._tracked_pids):
            try:
                os.kill(pid, signal.SIGTERM)
                logger.debug(f"Sent SIGTERM to PID {pid}")
            except (ProcessLookupError, PermissionError):
                pass    # Already dead or not ours
            try:
                ResilientExecutor._tracked_pids.remove(pid)
            except ValueError:
                pass

        # Kill any stray verilator/make processes (best-effort)
        for name in ("verilator", "make"):
            try:
                result = subprocess.run(
                    ["pkill", "-f", name],
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    logger.debug(f"Killed stray '{name}' process(es).")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        # Remove temp simulation artifacts
        self._cleanup_sim_artifacts()

    def _cleanup_sim_artifacts(self) -> None:
        """Remove temp files left by Verilator/cocotb in the work directory."""
        artifact_patterns = ["*.vcd", "*.dat", "*.log", "obj_dir"]
        cleaned = 0
        for pattern in artifact_patterns:
            for artifact in self.work_dir.glob(pattern):
                try:
                    if artifact.is_dir():
                        shutil.rmtree(artifact, ignore_errors=True)
                    else:
                        artifact.unlink(missing_ok=True)
                    cleaned += 1
                except Exception as e:
                    logger.debug(f"Could not remove {artifact}: {e}")
        if cleaned:
            logger.debug(f"Cleaned {cleaned} simulation artifact(s).")
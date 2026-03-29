"""
core/telemetry.py

Observability layer for the AI RISC-V verification platform.

Provides:
- Prometheus metrics (simulations, coverage, bugs, latency, agent actions)
- In-memory fallback when prometheus_client is not installed
- FastAPI /metrics endpoint integration
- Structured event logging for Grafana Loki compatibility
- Context-manager helper for timing code blocks

Dependencies (optional):
    pip install prometheus_client

Usage in FastAPI:
    from core.telemetry import telemetry, instrument_app

    instrument_app(app)           # adds /metrics endpoint
    telemetry.sim_started()
    telemetry.sim_completed(coverage=87.3, duration_sec=42.1)
    telemetry.bug_found("circular_dependency")
"""

import json
import logging
import time
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Optional: prometheus_client
# ─────────────────────────────────────────────
try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        Summary,
        CollectorRegistry,
        generate_latest,
        CONTENT_TYPE_LATEST,
        multiprocess,
        start_http_server,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.info(
        "prometheus_client not installed — using in-memory fallback. "
        "Run: pip install prometheus_client"
    )


# ─────────────────────────────────────────────
# In-memory fallback metric primitives
# ─────────────────────────────────────────────

class _MemCounter:
    def __init__(self, name: str, doc: str, labels: List[str] = []):
        self._name   = name
        self._labels = labels
        self._values: Dict[tuple, float] = defaultdict(float)

    def labels(self, **kw) -> "_MemCounter":
        self._current_labels = tuple(kw.get(l, "") for l in self._labels)
        return self

    def inc(self, amount: float = 1.0) -> None:
        key = getattr(self, "_current_labels", ())
        self._values[key] += amount
        self._current_labels = ()

    def value(self, **kw) -> float:
        key = tuple(kw.get(l, "") for l in self._labels)
        return self._values.get(key, 0.0)

    def total(self) -> float:
        return sum(self._values.values())


class _MemGauge:
    def __init__(self, name: str, doc: str, labels: List[str] = []):
        self._name   = name
        self._labels = labels
        self._values: Dict[tuple, float] = defaultdict(float)

    def labels(self, **kw) -> "_MemGauge":
        self._current_labels = tuple(kw.get(l, "") for l in self._labels)
        return self

    def set(self, value: float) -> None:
        key = getattr(self, "_current_labels", ())
        self._values[key] = value
        self._current_labels = ()

    def inc(self, amount: float = 1.0) -> None:
        key = getattr(self, "_current_labels", ())
        self._values[key] += amount
        self._current_labels = ()

    def dec(self, amount: float = 1.0) -> None:
        self.inc(-amount)

    def value(self, **kw) -> float:
        key = tuple(kw.get(l, "") for l in self._labels)
        return self._values.get(key, 0.0)


class _MemHistogram:
    def __init__(self, name: str, doc: str, labels: List[str] = [], buckets: List[float] = []):
        self._name   = name
        self._labels = labels
        self._obs: List[float] = []

    def labels(self, **kw) -> "_MemHistogram":
        return self

    def observe(self, value: float) -> None:
        self._obs.append(value)

    def mean(self) -> float:
        return sum(self._obs) / len(self._obs) if self._obs else 0.0

    def percentile(self, p: float) -> float:
        if not self._obs:
            return 0.0
        sorted_obs = sorted(self._obs)
        idx = int(len(sorted_obs) * p / 100)
        return sorted_obs[min(idx, len(sorted_obs) - 1)]


# ─────────────────────────────────────────────
# Metric factory (Prometheus or in-memory)
# ─────────────────────────────────────────────

def _counter(name: str, doc: str, labels: List[str] = []):
    if PROMETHEUS_AVAILABLE:
        try:
            return Counter(name, doc, labels)
        except Exception:
            pass    # Already registered — return in-memory for safety
    return _MemCounter(name, doc, labels)


def _gauge(name: str, doc: str, labels: List[str] = []):
    if PROMETHEUS_AVAILABLE:
        try:
            return Gauge(name, doc, labels)
        except Exception:
            pass
    return _MemGauge(name, doc, labels)


def _histogram(name: str, doc: str, labels: List[str] = [], buckets: List[float] = []):
    if PROMETHEUS_AVAILABLE:
        try:
            kw = {"buckets": buckets} if buckets else {}
            return Histogram(name, doc, labels, **kw)
        except Exception:
            pass
    return _MemHistogram(name, doc, labels, buckets)


# ─────────────────────────────────────────────
# Platform metrics registry
# ─────────────────────────────────────────────

# ── Simulation metrics ────────────────────────
simulations_started   = _counter("simulations_started_total", "Total simulations launched")
simulations_completed = _counter("simulations_completed_total", "Total simulations completed", ["status"])
simulation_duration   = _histogram(
    "simulation_duration_seconds",
    "Simulation wall-clock time",
    buckets=[10, 30, 60, 120, 300, 600, 1800],
)
active_simulations    = _gauge("active_simulations", "Currently running simulations")

# ── Coverage metrics ──────────────────────────
coverage_gauge        = _gauge("coverage_percent", "Current coverage %", ["type"])
coverage_delta        = _histogram(
    "coverage_delta_percent",
    "Per-run coverage improvement",
    buckets=[-5, -1, 0, 0.5, 1, 2, 5, 10, 20],
)

# ── Bug metrics ───────────────────────────────
bugs_found_total      = _counter("bugs_found_total", "Total bugs found", ["bug_type"])
assertion_failures    = _counter("assertion_failures_total", "Total assertion failures")

# ── Agent metrics ─────────────────────────────
agent_decisions_total = _counter("agent_decisions_total", "Total agent decisions", ["agent_id", "action"])
agent_attacks_success = _counter("agent_attacks_success_total", "Successful red-team attacks", ["agent_id"])
rl_training_steps     = _counter("rl_training_steps_total", "RL agent training steps")
rl_epsilon            = _gauge("rl_epsilon", "RL exploration rate")

# ── API metrics ───────────────────────────────
api_requests_total    = _counter("api_requests_total", "Total API requests", ["endpoint", "status"])
api_request_duration  = _histogram(
    "api_request_duration_seconds",
    "API request latency",
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0],
)

# ── System health ─────────────────────────────
ollama_available      = _gauge("ollama_available", "1 if Ollama is reachable, 0 otherwise")
rag_modules_loaded    = _gauge("rag_modules_loaded", "Number of RAG modules in memory")


# ─────────────────────────────────────────────
# Structured event log (Loki-compatible)
# ─────────────────────────────────────────────

@dataclass
class TelemetryEvent:
    timestamp: str
    event:     str
    labels:    Dict[str, str]
    data:      Dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({
            "timestamp": self.timestamp,
            "event":     self.event,
            "labels":    self.labels,
            **self.data,
        })


class _EventLog:
    """Thread-safe bounded event log for structured telemetry."""

    def __init__(self, maxlen: int = 1000):
        self._lock   = threading.Lock()
        self._events: List[TelemetryEvent] = []
        self._maxlen = maxlen

    def emit(
        self,
        event:  str,
        labels: Dict[str, str] = {},
        **data: Any,
    ) -> None:
        e = TelemetryEvent(
            timestamp = datetime.now(timezone.utc).isoformat(),
            event     = event,
            labels    = labels,
            data      = data,
        )
        with self._lock:
            self._events.append(e)
            if len(self._events) > self._maxlen:
                self._events = self._events[-self._maxlen:]

        logger.debug(f"[telemetry] {event} {data}")

    def recent(self, n: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return [json.loads(e.to_json()) for e in self._events[-n:]]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


event_log = _EventLog()


# ─────────────────────────────────────────────
# High-level Telemetry helper
# ─────────────────────────────────────────────

class Telemetry:
    """
    High-level telemetry API.
    All methods are safe to call from any thread or asyncio task.

    Usage:
        from core.telemetry import telemetry
        telemetry.sim_started()
        telemetry.sim_completed(coverage=87.3, duration_sec=42.1)
    """

    # ── Simulation ────────────────────────────────────────────────────────

    def sim_started(self, rtl_spec: str = "") -> None:
        simulations_started.inc()
        active_simulations.inc()
        event_log.emit("sim_started", rtl_spec=rtl_spec[:80])

    def sim_completed(
        self,
        coverage:      float,
        duration_sec:  float,
        status:        str = "success",
        bugs_found:    int = 0,
    ) -> None:
        simulations_completed.labels(status=status).inc()
        active_simulations.dec()
        simulation_duration.observe(duration_sec)
        event_log.emit(
            "sim_completed",
            coverage     = round(coverage, 2),
            duration_sec = round(duration_sec, 2),
            status       = status,
            bugs_found   = bugs_found,
        )

    def sim_timeout(self) -> None:
        simulations_completed.labels(status="timeout").inc()
        active_simulations.dec()
        event_log.emit("sim_timeout")

    # ── Coverage ──────────────────────────────────────────────────────────

    def record_coverage(
        self,
        line:       float,
        toggle:     float,
        branch:     float,
        functional: float,
        overall:    float,
        delta:      float = 0.0,
    ) -> None:
        coverage_gauge.labels(type="line").set(line)
        coverage_gauge.labels(type="toggle").set(toggle)
        coverage_gauge.labels(type="branch").set(branch)
        coverage_gauge.labels(type="functional").set(functional)
        coverage_gauge.labels(type="overall").set(overall)
        coverage_delta.observe(delta)
        event_log.emit(
            "coverage_recorded",
            line=line, toggle=toggle, branch=branch,
            functional=functional, overall=overall, delta=delta,
        )

    # ── Bugs ──────────────────────────────────────────────────────────────

    def bug_found(self, bug_type: str = "unknown") -> None:
        bugs_found_total.labels(bug_type=bug_type).inc()
        event_log.emit("bug_found", bug_type=bug_type)

    def assertion_failed(self, signal: str = "", file: str = "") -> None:
        assertion_failures.inc()
        event_log.emit("assertion_failed", signal=signal, file=file)

    # ── Agent ─────────────────────────────────────────────────────────────

    def agent_decision(self, agent_id: str, action: str) -> None:
        agent_decisions_total.labels(agent_id=agent_id, action=action).inc()

    def agent_attack_success(self, agent_id: str) -> None:
        agent_attacks_success.labels(agent_id=agent_id).inc()
        event_log.emit("agent_attack_success", agent_id=agent_id)

    def rl_step(self, epsilon: float, loss: Optional[float] = None) -> None:
        rl_training_steps.inc()
        rl_epsilon.set(epsilon)
        event_log.emit("rl_step", epsilon=round(epsilon, 4), loss=loss)

    # ── API ───────────────────────────────────────────────────────────────

    def api_request(self, endpoint: str, status: str, duration_sec: float) -> None:
        api_requests_total.labels(endpoint=endpoint, status=status).inc()
        api_request_duration.observe(duration_sec)

    # ── System ────────────────────────────────────────────────────────────

    def set_ollama_available(self, available: bool) -> None:
        ollama_available.set(1.0 if available else 0.0)

    def set_rag_modules(self, count: int) -> None:
        rag_modules_loaded.set(float(count))

    # ── Dashboard snapshot ────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot of all key metrics."""
        def _safe_total(m):
            try:
                return m.total() if hasattr(m, "total") else float(m._value.get())
            except Exception:
                return 0.0

        return {
            "simulations_started":   _safe_total(simulations_started),
            "active_simulations":    active_simulations.value() if hasattr(active_simulations, "value") else 0,
            "bugs_found_total":      _safe_total(bugs_found_total),
            "recent_events":         event_log.recent(10),
            "prometheus_available":  PROMETHEUS_AVAILABLE,
        }


# ─────────────────────────────────────────────
# Timing context manager
# ─────────────────────────────────────────────

@contextmanager
def timed_operation(
    name:     str,
    callback: Optional[Callable[[float], None]] = None,
) -> Generator[None, None, None]:
    """
    Context manager that measures wall-clock time of a block
    and calls ``callback(elapsed_sec)`` on exit.

    Usage:
        with timed_operation("verilator_lint", lambda t: telemetry.api_request("/lint", "ok", t)):
            await lint_rtl(code)
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        if callback:
            callback(elapsed)
        logger.debug(f"[timing] {name}: {elapsed:.3f}s")


# ─────────────────────────────────────────────
# FastAPI integration
# ─────────────────────────────────────────────

def instrument_app(app: Any) -> None:
    """
    Add a /metrics (Prometheus) and /telemetry/events endpoint to a FastAPI app.
    Safe to call even if prometheus_client is not installed.

    Args:
        app: A FastAPI application instance.
    """
    try:
        from fastapi import Request, Response
        from fastapi.responses import JSONResponse

        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics():
            if PROMETHEUS_AVAILABLE:
                return Response(
                    content     = generate_latest(),
                    media_type  = CONTENT_TYPE_LATEST,
                )
            return JSONResponse(
                content = {"error": "prometheus_client not installed"},
                status_code = 503,
            )

        @app.get("/telemetry/events", tags=["Telemetry"])
        async def recent_events(n: int = 50):
            """Return the last N structured telemetry events."""
            return {"events": event_log.recent(n)}

        @app.get("/telemetry/snapshot", tags=["Telemetry"])
        async def telemetry_snapshot():
            """Return a snapshot of all key metrics."""
            return telemetry.snapshot()

        @app.middleware("http")
        async def track_requests(request: Request, call_next: Callable):
            start = time.monotonic()
            response = await call_next(request)
            elapsed  = time.monotonic() - start
            telemetry.api_request(
                endpoint     = request.url.path,
                status       = str(response.status_code),
                duration_sec = elapsed,
            )
            return response

        logger.info("Telemetry endpoints registered: /metrics, /telemetry/events, /telemetry/snapshot")

    except ImportError:
        logger.warning("FastAPI not available — skipping app instrumentation.")


# ─────────────────────────────────────────────
# Singleton instance
# ─────────────────────────────────────────────

telemetry = Telemetry()
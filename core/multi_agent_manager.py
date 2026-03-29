"""
core/multi_agent_manager.py

Orchestrates all red-team agents in a parallel adversarial swarm.

Agents run concurrently using asyncio. Each agent is isolated:
- Runs inside asyncio.wait_for() with its own timeout
- Its exception cannot crash the swarm
- Its results are independently typed and validated

SwarmManager (alias: MultiAgentManager for backwards compatibility)
also integrates with:
- DependencyGraph: real-time deadlock detection across all agents
- VerificationStateManager: persists results across API restarts
- Telemetry: Prometheus metrics for each agent's run

Usage (standalone):
    asyncio.run(main())

Usage (from FastAPI /agent_swarm endpoint):
    manager = SwarmManager()
    result  = await manager.verify_tapeout("RV32IMC with MESI cache")
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from redteam.agents.conflict_agent    import ConflictAgent,    ConflictReport
from redteam.agents.speculation_agent import SpeculationAgent, SpeculationReport
from redteam.agents.power_agent       import PowerAgent,       PowerReport
from redteam.agents.dut_interface     import DUTInterface,     MockDUT
from redteam.graph.dependency_graph   import DependencyGraph,  Transaction, RequestType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Optional integrations (graceful degradation)
# ─────────────────────────────────────────────
try:
    from core.verification_state import VerificationStateManager, RunStatus
    STATE_MANAGER_AVAILABLE = True
except ImportError:
    STATE_MANAGER_AVAILABLE = False
    logger.info("VerificationStateManager not available — results not persisted.")

try:
    from core.telemetry import telemetry
    TELEMETRY_AVAILABLE = True
except ImportError:
    TELEMETRY_AVAILABLE = False


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class SwarmConfig:
    """
    All tunable parameters for a swarm run.
    Pass an instance to SwarmManager() to customise behavior.
    """
    num_cores:           int   = 4
    duration_cycles:     int   = 10_000
    per_agent_timeout:   float = 300.0    # seconds
    swarm_timeout:       float = 600.0    # total swarm timeout
    fault_rate:          float = 0.02     # MockDUT fault injection rate
    enable_conflict:     bool  = True
    enable_speculation:  bool  = True
    enable_power:        bool  = True
    enable_dependency:   bool  = True
    report_dir:          str   = "reports"
    save_report:         bool  = True


# ─────────────────────────────────────────────
# Per-agent result wrapper
# ─────────────────────────────────────────────

@dataclass
class AgentResult:
    agent_name:      str
    success:         bool
    bugs_found:      int
    coverage_boost:  float
    elapsed_sec:     float
    raw_report:      Any                           # Typed report from the agent
    error:           Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        raw = self.raw_report
        raw_dict = raw.to_dict() if hasattr(raw, "to_dict") else str(raw)
        return {
            "agent_name":     self.agent_name,
            "success":        self.success,
            "bugs_found":     self.bugs_found,
            "coverage_boost": round(self.coverage_boost, 2),
            "elapsed_sec":    round(self.elapsed_sec, 2),
            "error":          self.error,
            "report":         raw_dict,
        }


# ─────────────────────────────────────────────
# Final swarm report
# ─────────────────────────────────────────────

@dataclass
class SwarmReport:
    status:               str
    total_bugs:           int
    critical_bugs:        int
    coverage_boost:       float
    elapsed_sec:          float
    agent_results:        List[AgentResult]
    dependency_stats:     Dict[str, Any]
    deadlocks_detected:   int
    verification_passed:  bool
    rtl_spec:             str = ""
    run_id:               Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status":              self.status,
            "total_bugs":          self.total_bugs,
            "critical_bugs":       self.critical_bugs,
            "coverage_boost":      round(self.coverage_boost, 2),
            "elapsed_sec":         round(self.elapsed_sec, 2),
            "verification_passed": self.verification_passed,
            "deadlocks_detected":  self.deadlocks_detected,
            "dependency_stats":    self.dependency_stats,
            "agent_results":       [r.to_dict() for r in self.agent_results],
            "run_id":              self.run_id,
        }


# ─────────────────────────────────────────────
# Swarm Manager
# ─────────────────────────────────────────────

class SwarmManager:
    """
    Orchestrates all red-team agents in a parallel adversarial swarm.

    Each agent runs in its own asyncio task with its own timeout.
    One agent failing or timing out does NOT abort the others.

    The DependencyGraph runs as a background monitor task throughout
    the entire swarm, continuously detecting deadlocks.

    Args:
        config:  SwarmConfig with all tunable parameters.
        dut:     DUT interface. If None, creates a MockDUT with config settings.
        state_manager: Optional VerificationStateManager for persistence.
    """

    def __init__(
        self,
        config:        Optional[SwarmConfig] = None,
        dut:           Optional[DUTInterface] = None,
        state_manager  = None,
    ):
        self.config  = config or SwarmConfig()
        self._dut    = dut or MockDUT(
            num_cores_ = self.config.num_cores,
            fault_rate = self.config.fault_rate,
        )
        self._dep_graph = DependencyGraph(max_wait_depth=4)
        self._state_mgr = state_manager

        # Lazy-load state manager if available and not provided
        if self._state_mgr is None and STATE_MANAGER_AVAILABLE:
            try:
                self._state_mgr = VerificationStateManager()
            except Exception as e:
                logger.warning(f"Could not init VerificationStateManager: {e}")

        logger.info(
            f"SwarmManager initialised — "
            f"cores={self.config.num_cores}, "
            f"cycles={self.config.duration_cycles}, "
            f"agents=[conflict={self.config.enable_conflict}, "
            f"speculation={self.config.enable_speculation}, "
            f"power={self.config.enable_power}]"
        )

    # ── Public API ────────────────────────────────────────────────────────

    async def verify_tapeout(self, rtl_spec: str = "RV32IM baseline") -> Dict[str, Any]:
        """
        Run the full adversarial swarm on the given RTL spec.
        Returns a complete SwarmReport dict.
        """
        run_id = None
        if self._state_mgr:
            try:
                run = self._state_mgr.create_run(rtl_spec=rtl_spec)
                run_id = run.id
                self._state_mgr.mark_running(run_id)
            except Exception as e:
                logger.warning(f"State manager error: {e}")

        if TELEMETRY_AVAILABLE:
            telemetry.sim_started(rtl_spec=rtl_spec)

        report = await self._run_swarm(rtl_spec)
        report.run_id = run_id

        # Persist outcome
        if self._state_mgr and run_id:
            try:
                if report.verification_passed:
                    self._state_mgr.mark_completed(run_id, report.elapsed_sec)
                else:
                    self._state_mgr.mark_failed(
                        run_id,
                        f"Found {report.total_bugs} bugs",
                        report.elapsed_sec,
                    )
                self._state_mgr.update_coverage(
                    run_id,
                    {"overall": report.coverage_boost},
                )
            except Exception as e:
                logger.warning(f"State manager persist error: {e}")

        if TELEMETRY_AVAILABLE:
            telemetry.sim_completed(
                coverage     = report.coverage_boost,
                duration_sec = report.elapsed_sec,
                status       = "pass" if report.verification_passed else "fail",
                bugs_found   = report.total_bugs,
            )

        # Optionally save JSON report
        if self.config.save_report:
            self._save_report(report, rtl_spec)

        return report.to_dict()

    # ── Orchestration ─────────────────────────────────────────────────────

    async def _run_swarm(self, rtl_spec: str) -> SwarmReport:
        start = time.monotonic()

        logger.info(f"\n{'='*70}")
        logger.info(f"🚀 SWARM VERIFICATION: {rtl_spec}")
        logger.info(f"{'='*70}\n")

        # Build agent registry — only enabled agents
        agents: List[tuple] = []

        if self.config.enable_conflict:
            agents.append((
                "ConflictAgent",
                ConflictAgent(self._dut, per_pattern_timeout=self.config.per_agent_timeout / 5),
                "run_campaign",
            ))
        if self.config.enable_speculation:
            agents.append((
                "SpeculationAgent",
                SpeculationAgent(self._dut, per_attack_timeout=self.config.per_agent_timeout / 4),
                "run_campaign",
            ))
        if self.config.enable_power:
            agents.append((
                "PowerAgent",
                PowerAgent(self._dut, per_pattern_timeout=self.config.per_agent_timeout / 5),
                "run_campaign",
            ))

        if not agents:
            logger.warning("No agents enabled — returning empty report.")
            return self._build_report([], 0.0, rtl_spec)

        # Background deadlock monitor (runs for entire swarm duration)
        monitor_task = None
        if self.config.enable_dependency:
            monitor_task = asyncio.create_task(
                self._dep_graph.monitor(
                    interval_sec  = 2.0,
                    on_deadlock   = self._on_deadlock,
                    on_starvation = self._on_starvation,
                ),
                name="dep_graph_monitor",
            )

        # Launch all agents under the total swarm timeout
        try:
            agent_tasks = [
                asyncio.create_task(
                    self._run_agent(name, agent, method),
                    name=f"agent_{name}",
                )
                for name, agent, method in agents
            ]
            agent_results = await asyncio.wait_for(
                asyncio.gather(*agent_tasks, return_exceptions=True),
                timeout=self.config.swarm_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"Swarm timed out after {self.config.swarm_timeout}s — "
                f"partial results collected."
            )
            agent_results = [
                t.result() if not t.done() else AgentResult(
                    agent_name="timeout", success=False, bugs_found=0,
                    coverage_boost=0.0, elapsed_sec=self.config.swarm_timeout,
                    raw_report=None, error="Swarm timeout",
                )
                for t in agent_tasks
            ]
        finally:
            if monitor_task:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

        # Validate results: exceptions from gather() are included as-is
        validated: List[AgentResult] = []
        for name_tuple, result in zip([(n, a, m) for n, a, m in agents], agent_results):
            name = name_tuple[0]
            if isinstance(result, Exception):
                logger.error(f"Agent {name} raised unhandled exception: {result}")
                validated.append(AgentResult(
                    agent_name    = name,
                    success       = False,
                    bugs_found    = 0,
                    coverage_boost = 0.0,
                    elapsed_sec   = 0.0,
                    raw_report    = None,
                    error         = str(result),
                ))
            elif isinstance(result, AgentResult):
                validated.append(result)
            else:
                logger.warning(f"Unexpected result type from {name}: {type(result)}")

        elapsed = time.monotonic() - start
        return self._build_report(validated, elapsed, rtl_spec)

    async def _run_agent(
        self,
        name:   str,
        agent:  Any,
        method: str,
    ) -> AgentResult:
        """Run a single agent with timeout, error isolation, and telemetry."""
        logger.info(f"[{name}] Starting…")
        start = time.monotonic()

        try:
            coro = getattr(agent, method)(
                duration_cycles=self.config.duration_cycles
            )
            raw_report = await asyncio.wait_for(
                coro, timeout=self.config.per_agent_timeout
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            logger.warning(f"[{name}] Timed out after {elapsed:.1f}s.")
            return AgentResult(
                agent_name    = name,
                success       = False,
                bugs_found    = 0,
                coverage_boost = 0.0,
                elapsed_sec   = elapsed,
                raw_report    = None,
                error         = f"Agent timeout after {self.config.per_agent_timeout}s",
            )
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error(f"[{name}] Unhandled error: {e}", exc_info=True)
            return AgentResult(
                agent_name    = name,
                success       = False,
                bugs_found    = 0,
                coverage_boost = 0.0,
                elapsed_sec   = elapsed,
                raw_report    = None,
                error         = str(e),
            )

        elapsed = time.monotonic() - start
        bugs, coverage = self._extract_metrics(name, raw_report)

        if TELEMETRY_AVAILABLE:
            telemetry.agent_decision(name, "campaign_complete")
            for _ in range(bugs):
                telemetry.bug_found(name.lower())

        result = AgentResult(
            agent_name    = name,
            success       = True,
            bugs_found    = bugs,
            coverage_boost = coverage,
            elapsed_sec   = elapsed,
            raw_report    = raw_report,
        )
        logger.info(
            f"[{name}] ✅ Done — bugs={bugs}, coverage_boost={coverage:.1f}%, "
            f"time={elapsed:.1f}s"
        )
        return result

    # ── Report building ───────────────────────────────────────────────────

    def _build_report(
        self,
        results:    List[AgentResult],
        elapsed:    float,
        rtl_spec:   str,
    ) -> SwarmReport:
        total_bugs     = sum(r.bugs_found for r in results)
        critical_bugs  = self._count_critical(results)
        coverage_boost = sum(r.coverage_boost for r in results)
        dep_stats      = self._dep_graph.get_stats()
        deadlocks      = dep_stats.get("deadlocks_detected", 0)

        # Tape-out criteria:
        # - No critical bugs
        # - At least one agent ran successfully
        # - No unresolved deadlocks
        passed = (
            critical_bugs == 0 and
            any(r.success for r in results) and
            deadlocks == 0
        )

        if total_bugs > 10:
            status = "FAILED — critical bug count exceeded"
        elif deadlocks > 0:
            status = "FAILED — unresolved deadlocks detected"
        elif not any(r.success for r in results):
            status = "ERROR — all agents failed"
        elif passed:
            status = "PASSED — tape-out ready"
        else:
            status = "INCOMPLETE — review required"

        # Print summary table
        self._print_summary(results, total_bugs, coverage_boost, elapsed, status)

        return SwarmReport(
            status               = status,
            total_bugs           = total_bugs,
            critical_bugs        = critical_bugs,
            coverage_boost       = coverage_boost,
            elapsed_sec          = elapsed,
            agent_results        = results,
            dependency_stats     = dep_stats,
            deadlocks_detected   = deadlocks,
            verification_passed  = passed,
            rtl_spec             = rtl_spec,
        )

    def _count_critical(self, results: List[AgentResult]) -> int:
        count = 0
        for r in results:
            report = r.raw_report
            if hasattr(report, "critical_bugs"):
                count += len(report.critical_bugs)
            if hasattr(report, "critical_vulnerabilities"):
                count += len(report.critical_vulnerabilities)
        return count

    @staticmethod
    def _extract_metrics(name: str, raw_report: Any) -> tuple:
        """Extract (bugs_found, coverage_boost) from any agent report type."""
        if isinstance(raw_report, ConflictReport):
            return raw_report.bugs_found, raw_report.coverage_pct
        if isinstance(raw_report, SpeculationReport):
            return raw_report.leaks_found, 0.0
        if isinstance(raw_report, PowerReport):
            return len(raw_report.violations), 0.0
        # Generic fallback via attribute lookup
        bugs = getattr(raw_report, "bugs_found", 0) or 0
        cov  = getattr(raw_report, "coverage_pct", 0.0) or 0.0
        return int(bugs), float(cov)

    # ── Dependency graph callbacks ────────────────────────────────────────

    async def _on_deadlock(self, event) -> None:
        logger.critical(f"[Swarm] DEADLOCK ALERT: {event.visualize()}")
        if TELEMETRY_AVAILABLE:
            telemetry.bug_found("deadlock")

    async def _on_starvation(self, event) -> None:
        logger.warning(f"[Swarm] STARVATION: {event.node} (depth={event.wait_depth})")
        if TELEMETRY_AVAILABLE:
            telemetry.bug_found("starvation")

    # ── Report persistence ────────────────────────────────────────────────

    def _save_report(self, report: SwarmReport, rtl_spec: str) -> None:
        report_dir = Path(self.config.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        filepath  = report_dir / f"swarm_report_{timestamp}.json"
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, indent=2)
            logger.info(f"[Swarm] Report saved: {filepath}")
        except Exception as e:
            logger.warning(f"[Swarm] Could not save report: {e}")

    # ── Console summary ───────────────────────────────────────────────────

    @staticmethod
    def _print_summary(
        results:       List[AgentResult],
        total_bugs:    int,
        cov_boost:     float,
        elapsed:       float,
        status:        str,
    ) -> None:
        sep = "=" * 70
        logger.info(f"\n{sep}")
        logger.info("📊 SWARM VERIFICATION COMPLETE")
        logger.info(f"{sep}")
        logger.info(f"Total Bugs:       {total_bugs}")
        logger.info(f"Coverage Boost:   +{cov_boost:.2f}%")
        logger.info(f"Elapsed:          {elapsed:.1f}s")
        logger.info(f"\n{'Agent':<24} {'Bugs':<8} {'Coverage':<14} {'Time':<10} Status")
        logger.info("-" * 70)
        for r in results:
            status_str = "✅" if r.success else f"❌ {r.error or ''}"[:30]
            logger.info(
                f"{r.agent_name:<24} {r.bugs_found:<8} "
                f"+{r.coverage_boost:<13.2f}% {r.elapsed_sec:<10.1f}s "
                f"{status_str}"
            )
        logger.info(f"\n🎯 STATUS: {status}\n{sep}\n")


# ─────────────────────────────────────────────
# Backwards-compatibility alias
# ─────────────────────────────────────────────
MultiAgentManager = SwarmManager


# ─────────────────────────────────────────────
# Standalone entry point
# ─────────────────────────────────────────────

async def main():
    config = SwarmConfig(
        num_cores        = 4,
        duration_cycles  = 5_000,
        per_agent_timeout = 120.0,
        swarm_timeout    = 360.0,
        save_report      = True,
    )

    manager = SwarmManager(config=config)
    result  = await manager.verify_tapeout("RV32IMC with MESI cache coherency")

    print(json.dumps({
        "status":       result["status"],
        "total_bugs":   result["total_bugs"],
        "passed":       result["verification_passed"],
        "elapsed_sec":  result["elapsed_sec"],
    }, indent=2))


if __name__ == "__main__":
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )
    asyncio.run(main())
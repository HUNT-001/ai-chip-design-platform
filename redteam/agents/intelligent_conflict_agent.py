"""
redteam/agents/intelligent_conflict_agent.py

LLM-driven Red-Team agent with learning and memory.
Uses the same requests-based OllamaClient pattern as the rest of the codebase
(NOT the ollama library directly, for consistency and fallback support).

Dependencies:
    pip install requests
"""

import re
import json
import time
import logging
import asyncio
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime
from enum import Enum

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

OLLAMA_URL       = "http://localhost:11434"
DEFAULT_MODEL    = "qwen2.5-coder:7b"       # 7b is the local standard; 32b only if GPU available
MAX_MEMORY_SIZE  = 100                       # Cap stored plans to avoid unbounded growth
MAX_PROMPT_CHARS = 3000                      # Safety cap to avoid context overflow
LLM_TIMEOUT_SEC  = 90


# ─────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────

class OperationType(str, Enum):
    READ          = "READ"
    WRITE         = "WRITE"
    ATOMIC_WRITE  = "ATOMIC_WRITE"
    ATOMIC_CAS    = "ATOMIC_CAS"       # Compare-And-Swap
    EVICT         = "EVICT"
    FLUSH         = "FLUSH"


class FailureType(str, Enum):
    DEADLOCK    = "deadlock"
    LIVELOCK    = "livelock"
    DATA_RACE   = "data_race"
    STARVATION  = "starvation"
    COHERENCY   = "coherency_violation"
    TIMEOUT     = "timeout"
    UNKNOWN     = "unknown"


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class AttackPlan:
    target_address:  str
    cores:           List[int]
    sequence:        List[OperationType]
    timing:          str                   # "immediate" | "delayed_N_cycles"
    reasoning:       str
    timestamp:       str = field(default_factory=lambda: datetime.utcnow().isoformat())
    success:         Optional[bool] = None
    triggered_bug:   Optional[str]  = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_address": self.target_address,
            "cores":          self.cores,
            "sequence":       [op.value for op in self.sequence],
            "timing":         self.timing,
            "reasoning":      self.reasoning,
            "timestamp":      self.timestamp,
            "success":        self.success,
            "triggered_bug":  self.triggered_bug,
        }


@dataclass
class SuccessPattern:
    pattern_type:  str
    conditions:    Dict[str, Any]
    confirmed_at:  str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class SystemState:
    cores:      int
    coverage:   float
    failures:   List[str]
    noc_util:   float   # 0.0–1.0

    def to_prompt_str(self) -> str:
        return (
            f"Cores: {self.cores}\n"
            f"Coverage: {self.coverage:.1f}%\n"
            f"Recent failures: {', '.join(self.failures) or 'none'}\n"
            f"NoC utilisation: {self.noc_util * 100:.1f}%"
        )


# ─────────────────────────────────────────────
# Lightweight async Ollama client
# ─────────────────────────────────────────────

class _AsyncOllamaClient:
    """
    Thin async wrapper around Ollama's /api/generate endpoint.
    Mirrors the pattern used in api/main.py for consistency.
    """

    def __init__(self, base_url: str = OLLAMA_URL, model: str = DEFAULT_MODEL):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self._available: Optional[bool] = None   # Lazy-checked on first call

    def _check_availability(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    async def generate(self, prompt: str, timeout: int = LLM_TIMEOUT_SEC) -> str:
        if self._available is None:
            self._available = self._check_availability()

        if not self._available:
            logger.warning("Ollama unavailable — skipping LLM call.")
            return ""

        payload = {
            "model":   self.model,
            "prompt":  prompt[:MAX_PROMPT_CHARS],
            "stream":  False,
            "options": {"temperature": 0.2, "top_p": 0.9, "num_predict": 1024},
        }
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                    timeout=timeout,
                    headers={"Content-Type": "application/json"},
                ),
            )
            response.raise_for_status()
            return response.json().get("response", "")
        except requests.Timeout:
            logger.error(f"Ollama request timed out after {timeout}s.")
            self._available = False   # Mark as unavailable to avoid repeated timeouts
            return ""
        except Exception as e:
            logger.error(f"Ollama generate error: {e}")
            return ""


# ─────────────────────────────────────────────
# Intelligent Conflict Agent
# ─────────────────────────────────────────────

class IntelligentConflictAgent:
    """
    LLM-driven Red-Team agent that plans cache coherency attacks,
    learns from their outcomes, and adapts its strategy over time.

    Attack lifecycle:
        1. plan_attack()      — LLM proposes an attack based on system state.
        2. execute_attack()   — Caller runs the attack; provides SimulationResult.
        3. learn_from_result()— Agent updates its success patterns accordingly.
    """

    def __init__(
        self,
        llm_model: str    = DEFAULT_MODEL,
        ollama_url: str   = OLLAMA_URL,
        agent_id: str     = "conflict_agent_0",
    ):
        self.agent_id        = agent_id
        self._llm            = _AsyncOllamaClient(base_url=ollama_url, model=llm_model)
        self.memory: deque   = deque(maxlen=MAX_MEMORY_SIZE)   # Recent attack plans
        self.success_patterns: List[SuccessPattern] = []

        logger.info(f"IntelligentConflictAgent '{agent_id}' initialised with model '{llm_model}'.")

    # ── Public interface ──────────────────────────────────────────────────

    async def plan_attack(self, system_state: SystemState) -> AttackPlan:
        """
        Use the LLM to plan the next attack vector given current system state.
        Falls back to a heuristic plan if the LLM is unavailable.
        """
        llm_raw = await self._llm.generate(
            self._build_attack_prompt(system_state)
        )

        if llm_raw:
            plan = self._parse_attack_plan(llm_raw)
        else:
            logger.warning("LLM unavailable — using heuristic fallback attack plan.")
            plan = self._heuristic_attack_plan(system_state)

        self.memory.append(plan)
        logger.info(
            f"[{self.agent_id}] Attack plan → address={plan.target_address}, "
            f"cores={plan.cores}, sequence={plan.sequence}"
        )
        return plan

    async def learn_from_result(
        self,
        plan:           AttackPlan,
        failure_type:   FailureType,
        conditions:     Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Feedback loop: update success patterns based on attack outcome.
        Call this after every simulation run that used a generated attack plan.

        Args:
            plan:         The AttackPlan that was executed.
            failure_type: The type of bug triggered (or FailureType.UNKNOWN).
            conditions:   Optional dict of conditions observed during the run.
        """
        conditions = conditions or {}

        if failure_type == FailureType.UNKNOWN:
            plan.success = False
            logger.debug(f"[{self.agent_id}] Attack plan had no effect.")
            return

        plan.success      = True
        plan.triggered_bug = failure_type.value

        self.success_patterns.append(SuccessPattern(
            pattern_type = failure_type.value,
            conditions   = {
                "address":  plan.target_address,
                "cores":    plan.cores,
                "sequence": [op.value for op in plan.sequence],
                **conditions,
            },
        ))

        # Keep pattern list bounded
        if len(self.success_patterns) > 50:
            self.success_patterns = self.success_patterns[-50:]

        logger.info(
            f"[{self.agent_id}] Learned new pattern: "
            f"{failure_type.value} at {plan.target_address}"
        )

        # Ask LLM to analyse the success (fire-and-forget)
        asyncio.ensure_future(self._analyse_success(plan, failure_type, conditions))

    def get_stats(self) -> Dict[str, Any]:
        """Return agent statistics for the dashboard."""
        successful = [p for p in self.memory if p.success is True]
        return {
            "agent_id":            self.agent_id,
            "plans_generated":     len(self.memory),
            "successful_attacks":  len(successful),
            "patterns_learned":    len(self.success_patterns),
            "bug_types_found":     list({p.triggered_bug for p in successful if p.triggered_bug}),
        }

    # ── Prompt builders ───────────────────────────────────────────────────

    def _build_attack_prompt(self, state: SystemState) -> str:
        pattern_summary = (
            json.dumps([
                {"type": p.pattern_type, "conditions": p.conditions}
                for p in self.success_patterns[-5:]
            ], indent=2)
            if self.success_patterns else "none yet"
        )

        return f"""You are a hardware Red-Team verification agent hunting cache coherency bugs.

SYSTEM STATE:
{state.to_prompt_str()}

PREVIOUSLY SUCCESSFUL ATTACK PATTERNS:
{pattern_summary}

Your task: propose the single most promising cache-coherency attack.
Choose a cache-line address with high contention potential (e.g. shared data structures).
Interleave operations across cores to maximise coherency pressure.

Respond ONLY with a valid JSON object. No prose, no markdown:
{{
  "target_address": "0xCAFEBEEF",
  "cores": [0, 1],
  "sequence": ["READ", "ATOMIC_WRITE", "READ"],
  "timing": "immediate",
  "reasoning": "brief explanation"
}}

Valid sequence operations: {[op.value for op in OperationType]}
Valid timing values: "immediate", "delayed_10_cycles", "delayed_100_cycles"
"""

    def _build_analysis_prompt(
        self, plan: AttackPlan, failure_type: FailureType, conditions: Dict
    ) -> str:
        return f"""A cache-coherency attack succeeded. Analyse the root cause.

ATTACK:
{json.dumps(plan.to_dict(), indent=2)}

FAILURE TYPE: {failure_type.value}
CONDITIONS: {json.dumps(conditions, indent=2)}

What microarchitectural condition caused this? Suggest 2 additional variant attacks.
Reply in JSON only:
{{
  "root_cause": "...",
  "variant_attacks": [
    {{"description": "...", "sequence": [...], "target_address": "..."}}
  ]
}}
"""

    # ── Parsing helpers ───────────────────────────────────────────────────

    def _parse_attack_plan(self, raw: str) -> AttackPlan:
        """
        Parse LLM JSON response into an AttackPlan.
        Gracefully handles markdown fences, trailing commas, and type errors.
        """
        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()

        # Remove trailing commas before } or ] (common LLM mistake)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed ({e}). Falling back to heuristic.")
            return self._heuristic_attack_plan(None)

        # Coerce sequence strings → OperationType (unknown values default to READ)
        raw_sequence = data.get("sequence", ["READ", "WRITE"])
        sequence = []
        for op in raw_sequence:
            try:
                sequence.append(OperationType(op.upper()))
            except ValueError:
                logger.debug(f"Unknown operation '{op}' — defaulting to READ.")
                sequence.append(OperationType.READ)

        # Coerce cores → list[int]
        raw_cores = data.get("cores", [0, 1])
        try:
            cores = [int(c) for c in raw_cores]
        except (TypeError, ValueError):
            cores = [0, 1]

        return AttackPlan(
            target_address = str(data.get("target_address", "0xDEADBEEF")),
            cores          = cores,
            sequence       = sequence,
            timing         = str(data.get("timing", "immediate")),
            reasoning      = str(data.get("reasoning", "LLM-generated"))[:500],
        )

    def _heuristic_attack_plan(self, state: Optional[SystemState]) -> AttackPlan:
        """
        Rule-based fallback when the LLM is unavailable.
        Rotates through known high-value attack patterns.
        """
        # Pick pattern based on number of plans generated so far
        idx = len(self.memory) % 4
        patterns = [
            AttackPlan(
                target_address = "0xDEADBEEF",
                cores          = [0, 1],
                sequence       = [OperationType.READ, OperationType.ATOMIC_WRITE, OperationType.READ],
                timing         = "immediate",
                reasoning      = "Heuristic: Classic MESI race — read-then-atomic-write from two cores.",
            ),
            AttackPlan(
                target_address = "0xCAFEBABE",
                cores          = [0, 1, 2],
                sequence       = [OperationType.WRITE, OperationType.EVICT, OperationType.READ],
                timing         = "delayed_10_cycles",
                reasoning      = "Heuristic: Evict-then-read stresses memory controller ordering.",
            ),
            AttackPlan(
                target_address = "0xBAADF00D",
                cores          = [0, 3],
                sequence       = [OperationType.ATOMIC_CAS, OperationType.ATOMIC_CAS],
                timing         = "immediate",
                reasoning      = "Heuristic: Dual CAS on same line tests LL/SC reservation logic.",
            ),
            AttackPlan(
                target_address = "0xFEEDC0DE",
                cores          = list(range(min(state.cores, 4) if state else 4)),
                sequence       = [OperationType.FLUSH, OperationType.READ, OperationType.WRITE],
                timing         = "immediate",
                reasoning      = "Heuristic: Flush-all + concurrent access tests MSHR exhaustion.",
            ),
        ]
        return patterns[idx]

    # ── Background analysis ───────────────────────────────────────────────

    async def _analyse_success(
        self, plan: AttackPlan, failure_type: FailureType, conditions: Dict
    ) -> None:
        """Fire-and-forget LLM analysis of a successful attack."""
        raw = await self._llm.generate(
            self._build_analysis_prompt(plan, failure_type, conditions),
            timeout=60,
        )
        if not raw:
            return
        try:
            cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
            result  = json.loads(cleaned)
            logger.info(
                f"[{self.agent_id}] Root cause analysis: "
                f"{result.get('root_cause', 'N/A')}"
            )
        except json.JSONDecodeError:
            logger.debug(f"Root cause analysis JSON malformed: {raw[:200]}")
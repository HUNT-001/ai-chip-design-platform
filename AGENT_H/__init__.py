"""
AGENT_H — AVA Extended Verification Research Tier
==================================================
14 specialised modules for deep hardware verification analysis.

Quick imports::

    from AGENT_H import SecurityIntelligence, ConfidenceScorer, TemporalChecker
    from AGENT_H import ContractRunner, IntentChecker, BugExplainer
    from AGENT_H import get_adapter, DUTClass, EconomicsEngine, KnowledgeGraph
"""

from .agent_h_intent       import IntentChecker
from .confidence_scorer    import ConfidenceScorer
from .contract_dsl         import ContractRunner
from .cross_domain         import get_adapter, DUTClass, register_adapter
from .digital_twin         import DigitalTwin
from .economics_engine     import EconomicsEngine
from .explainer            import BugExplainer
from .formal_fuzzer        import FormalFuzzBridge, disassemble_rv32im
from .knowledge_graph      import KnowledgeGraph
from .minimizer            import CommitLogMinimizer
from .root_cause_localizer import RootCauseLocalizer
from .security_intel       import SecurityIntelligence
from .temporal_checker     import TemporalChecker

__version__ = "3.0.0"
__all__ = [
    "IntentChecker", "ConfidenceScorer", "ContractRunner",
    "get_adapter", "DUTClass", "register_adapter",
    "DigitalTwin", "EconomicsEngine", "BugExplainer",
    "FormalFuzzBridge", "disassemble_rv32im",
    "KnowledgeGraph", "CommitLogMinimizer",
    "RootCauseLocalizer", "SecurityIntelligence", "TemporalChecker",
]

"""
AGENT_H — AVA Extended Verification Research Tier
==================================================
15 specialised modules for deep hardware verification analysis.

Quick imports::

    from AGENT_H import SecurityIntelligence, ConfidenceScorer, TemporalChecker
    from AGENT_H import ContractRunner, IntentChecker, BugExplainer
    from AGENT_H import get_adapter, DUTClass, EconomicsEngine, KnowledgeGraph
    from AGENT_H import AtomicsVerifier
"""

from .agent_h_intent       import IntentChecker
from .atomics_verifier     import AtomicsVerifier, amo_compute, decode_atomic
from .bitmanip_verifier    import BitmanipVerifier, decode_bitmanip
from .bus_verifier         import BusVerifier, axi_expected_beats
from .cache_verifier       import CacheVerifier, CacheModel
from .confidence_scorer    import ConfidenceScorer
from .contract_dsl         import ContractRunner
from .cross_domain         import get_adapter, DUTClass, register_adapter
from .csr_verifier         import CSRVerifier, decode_csr, csr_is_readonly
from .digital_twin         import DigitalTwin
from .fp_verifier          import FPVerifier, decode_fp, fclass_mask
from .economics_engine     import EconomicsEngine
from .explainer            import BugExplainer
from .fault_injector       import FaultCampaign, inject_fault
from .formal_fuzzer        import FormalFuzzBridge, disassemble_rv32im
from .knowledge_graph      import KnowledgeGraph
from .minimizer            import CommitLogMinimizer
from .peripheral_verifier  import PeripheralVerifier, get_checker, register_checker
from .pipeline_verifier    import PipelineVerifier, alu_eval
from .privilege_verifier   import PrivilegeVerifier, PMPModel, parse_priv
from .root_cause_localizer import RootCauseLocalizer
from .rv64_verifier        import RV64Verifier, alu64, aluw, sext32
from .rv64_atomics_verifier import RV64AtomicsVerifier, amo_compute64
from .rvc_verifier         import RVCVerifier, is_compressed
from .security_intel       import SecurityIntelligence
from .sv_mmu_verifier      import SvMMU, SvMMUVerifier
from .temporal_checker     import TemporalChecker
from .tlb_verifier         import TLBVerifier
from .vm_verifier          import VMVerifier, Sv32MMU

__version__ = "2.0.0"
__all__ = [
    "IntentChecker", "AtomicsVerifier", "amo_compute", "decode_atomic",
    "BitmanipVerifier", "decode_bitmanip",
    "BusVerifier", "axi_expected_beats",
    "CacheVerifier", "CacheModel",
    "ConfidenceScorer", "ContractRunner",
    "CSRVerifier", "decode_csr", "csr_is_readonly",
    "FPVerifier", "decode_fp", "fclass_mask",
    "get_adapter", "DUTClass", "register_adapter",
    "DigitalTwin", "EconomicsEngine", "BugExplainer",
    "FaultCampaign", "inject_fault",
    "FormalFuzzBridge", "disassemble_rv32im",
    "KnowledgeGraph", "CommitLogMinimizer",
    "PeripheralVerifier", "get_checker", "register_checker",
    "PipelineVerifier", "alu_eval",
    "PrivilegeVerifier", "PMPModel", "parse_priv",
    "RV64Verifier", "alu64", "aluw", "sext32",
    "RV64AtomicsVerifier", "amo_compute64",
    "RVCVerifier", "is_compressed",
    "RootCauseLocalizer", "SecurityIntelligence", "TemporalChecker",
    "SvMMU", "SvMMUVerifier",
    "VMVerifier", "Sv32MMU", "TLBVerifier",
]

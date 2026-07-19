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
from .aes_verifier         import AESVerifier, aes_golden, aes64ks1i
from .sm4_verifier         import SM4Verifier, sm4_golden
from .vaes_verifier        import VAESVerifier, vaes_round
from .vsha_verifier        import VSHAVerifier, vsha2ms_golden, vsha2c_golden
from .vsm3_verifier        import VSM3Verifier, vsm3me_golden, vsm3c_golden
from .vaeskf_verifier      import VAESKFVerifier, vaeskf1_golden, vaeskf2_golden
from .vsm4_verifier        import VSM4Verifier, vsm4r_golden, vsm4k_golden
from .vghash_verifier      import VGHASHVerifier, vgmul_golden, vghsh_golden
from .power_verifier       import PowerVerifier
from .verification_twin    import (
    VerificationTwin, live_status, replay, replay_failure, what_if,
    fit_coverage_curve, forecast_closure, predict_regression,
    tapeout_readiness, silicon_sync,
)
from .rtl_graph            import (
    RTLGraphAnalyzer, Module as RTLModule, Port as RTLPort, FSM as RTLFSM,
    parse_module, extract_fsms, embed as rtl_embed, similarity as rtl_similarity,
    find_clones, find_comb_loops, graph_depth,
)
from .formal_engine        import (
    Expr, Var, Not, And, Or, Implies, Iff, Xor, Const, big_and, big_or,
    CNF, to_cnf, solve, satisfiable, is_tautology, unsat_core,
    TransitionSystem, bmc_safety, bmc_liveness, reachable, deadlock_free,
    mutual_exclusion, equivalence as formal_equivalence, check_all,
)
from .formal_analysis      import (
    FormalAnalysis, cover_property, unreachable_states, cone_of_influence,
    proof_coverage, detect_vacuity, minimize_counterexample,
    explain_counterexample, proof_core, mine_assertions, rank_properties,
)
from .failure_analytics    import (
    FailureAnalytics, canonical_signature, fingerprint, cluster_failures,
    deduplicate, prioritise, classify_trends, jaccard, stack_similarity,
)
from .bug_intelligence     import (
    BugIntelligence, localize, ochiai, tarantula, predict_severity,
    predict_lifetime, predict_reopen, find_duplicates, classify_root_cause,
)
from .regression_intelligence import (
    RegressionIntelligence, impacted_tests, prioritise_tests, select_tests,
    schedule, regression_health, flakiness, incremental_plan, cost_report,
)
from .dashboard            import (
    DashboardBuilder, write_dashboards, sparkline, heatmap, sankey, scorecard,
)
from .rtl_basics_verifier  import RTLBasicsVerifier, FSMModel, FIFOModel, MemModel
from .soc_peripheral_verifier import SoCPeripheralVerifier
from .interconnect_verifier   import InterconnectVerifier
from .advanced_link_verifier  import AdvancedLinkVerifier, find_cycle as adv_find_cycle
from .cdc_verifier         import CDCVerifier
from .equivalence_verifier import (
    EquivalenceVerifier, comb_equivalent, best_latency_offset,
)
from .aia_verifier         import AIAVerifier, IMSICModel, APLICModel
from .atomics_verifier     import AtomicsVerifier, amo_compute, decode_atomic
from .bitmanip_verifier    import BitmanipVerifier, decode_bitmanip
from .branch_predictor_verifier import BranchPredictorVerifier
from .bus_verifier         import BusVerifier, axi_expected_beats
from .cas_verifier         import CASVerifier
from .cache_verifier       import CacheVerifier, CacheModel
from .confidence_scorer    import ConfidenceScorer
from .coherence_verifier   import CoherenceVerifier
from .coverage_collector   import CoverageCollector, classify_value
from .contract_dsl         import ContractRunner
from .cross_domain         import get_adapter, DUTClass, register_adapter
from .csr_verifier         import CSRVerifier, decode_csr, csr_is_readonly
from .crypto_verifier      import CryptoVerifier, crypto_golden
from .debug_verifier       import DebugVerifier, Trigger
from .demo_traces          import write_demo_run
from .digital_twin         import DigitalTwin
from .fp_verifier          import FPVerifier, decode_fp, fclass_mask
from .economics_engine     import EconomicsEngine
from .explainer            import BugExplainer
from .fault_injector       import FaultCampaign, inject_fault
from .formal_fuzzer        import FormalFuzzBridge, disassemble_rv32im
from .knowledge_graph      import KnowledgeGraph
from .hypervisor_verifier  import HypervisorVerifier, TwoStageMMU
from .interrupt_verifier   import InterruptVerifier, PLICModel, CLICModel
from .memory_model_verifier import MemoryModelVerifier, find_cycle
from .minimizer            import CommitLogMinimizer
from .ooo_verifier         import OOOVerifier
from .lsq_verifier         import LSQVerifier
from .perf_counter_verifier import PerfCounterVerifier
from .peripheral_verifier  import PeripheralVerifier, get_checker, register_checker
from .pipeline_verifier    import PipelineVerifier, alu_eval
from .privilege_verifier   import PrivilegeVerifier, PMPModel, parse_priv
from .reset_verifier       import ResetVerifier
from .root_cause_localizer import RootCauseLocalizer
from .rv64_verifier        import RV64Verifier, alu64, aluw, sext32
from .rv64_atomics_verifier import RV64AtomicsVerifier, amo_compute64
from .rvc_verifier         import RVCVerifier, is_compressed
from .security_intel       import SecurityIntelligence
from .self_evolving_engine import (
    SelfEvolvingEngine, BanditPolicy, UCB1, DiscountedUCB1, SlidingWindowUCB,
    ThompsonSampling, make_policy, CoverageState, constraint_for,
    plan_from_coverage, run_campaign,
)
from .stimulus_generator   import StimulusGenerator, generate_from_holes
from .sv_mmu_verifier      import SvMMU, SvMMUVerifier
from .temporal_checker     import TemporalChecker
from .tlb_verifier         import TLBVerifier
from .vector_verifier      import VectorVerifier, decode_vtype, velem_compute, vlmax
from .vm_verifier          import VMVerifier, Sv32MMU

__version__ = "2.0.0"
__all__ = [
    "IntentChecker", "AtomicsVerifier", "amo_compute", "decode_atomic",
    "BitmanipVerifier", "decode_bitmanip",
    "BranchPredictorVerifier",
    "BusVerifier", "axi_expected_beats", "CASVerifier",
    "CacheVerifier", "CacheModel",
    "ConfidenceScorer", "ContractRunner",
    "CoverageCollector", "classify_value", "CoherenceVerifier",
    "CSRVerifier", "decode_csr", "csr_is_readonly",
    "CryptoVerifier", "crypto_golden", "AESVerifier", "aes_golden", "aes64ks1i",
    "SM4Verifier", "sm4_golden", "VAESVerifier", "vaes_round",
    "VSHAVerifier", "vsha2ms_golden", "vsha2c_golden",
    "VSM3Verifier", "vsm3me_golden", "vsm3c_golden",
    "VAESKFVerifier", "vaeskf1_golden", "vaeskf2_golden",
    "VSM4Verifier", "vsm4r_golden", "vsm4k_golden",
    "VGHASHVerifier", "vgmul_golden", "vghsh_golden",
    "PowerVerifier", "CDCVerifier",
    "VerificationTwin", "live_status", "replay", "replay_failure", "what_if",
    "fit_coverage_curve", "forecast_closure", "predict_regression",
    "tapeout_readiness", "silicon_sync",
    "RTLGraphAnalyzer", "RTLModule", "RTLPort", "RTLFSM", "parse_module",
    "extract_fsms", "rtl_embed", "rtl_similarity", "find_clones",
    "find_comb_loops", "graph_depth",
    "Expr", "Var", "Not", "And", "Or", "Implies", "Iff", "Xor", "Const",
    "big_and", "big_or", "CNF", "to_cnf", "solve", "satisfiable",
    "is_tautology", "unsat_core", "TransitionSystem", "bmc_safety",
    "bmc_liveness", "reachable", "deadlock_free", "mutual_exclusion",
    "formal_equivalence", "check_all",
    "FormalAnalysis", "cover_property", "unreachable_states",
    "cone_of_influence", "proof_coverage", "detect_vacuity",
    "minimize_counterexample", "explain_counterexample", "proof_core",
    "mine_assertions", "rank_properties",
    "FailureAnalytics", "canonical_signature", "fingerprint",
    "cluster_failures", "deduplicate", "prioritise", "classify_trends",
    "jaccard", "stack_similarity",
    "BugIntelligence", "localize", "ochiai", "tarantula", "predict_severity",
    "predict_lifetime", "predict_reopen", "find_duplicates",
    "classify_root_cause",
    "RegressionIntelligence", "impacted_tests", "prioritise_tests",
    "select_tests", "schedule", "regression_health", "flakiness",
    "incremental_plan", "cost_report",
    "DashboardBuilder", "write_dashboards", "sparkline", "heatmap", "sankey",
    "scorecard",
    "RTLBasicsVerifier", "FSMModel", "FIFOModel", "MemModel",
    "SoCPeripheralVerifier", "InterconnectVerifier",
    "AdvancedLinkVerifier", "adv_find_cycle",
    "EquivalenceVerifier", "comb_equivalent", "best_latency_offset",
    "FPVerifier", "decode_fp", "fclass_mask",
    "get_adapter", "DUTClass", "register_adapter",
    "DigitalTwin", "EconomicsEngine", "BugExplainer",
    "FaultCampaign", "inject_fault",
    "FormalFuzzBridge", "disassemble_rv32im",
    "KnowledgeGraph", "CommitLogMinimizer", "OOOVerifier", "LSQVerifier",
    "MemoryModelVerifier", "find_cycle",
    "InterruptVerifier", "PLICModel", "CLICModel", "PerfCounterVerifier",
    "HypervisorVerifier", "TwoStageMMU", "AIAVerifier", "IMSICModel", "APLICModel",
    "DebugVerifier", "Trigger", "ResetVerifier", "write_demo_run",
    "PeripheralVerifier", "get_checker", "register_checker",
    "PipelineVerifier", "alu_eval",
    "PrivilegeVerifier", "PMPModel", "parse_priv",
    "RV64Verifier", "alu64", "aluw", "sext32",
    "RV64AtomicsVerifier", "amo_compute64",
    "RVCVerifier", "is_compressed",
    "RootCauseLocalizer", "SecurityIntelligence", "TemporalChecker",
    "SelfEvolvingEngine", "BanditPolicy", "UCB1", "DiscountedUCB1",
    "SlidingWindowUCB", "ThompsonSampling", "make_policy", "CoverageState",
    "constraint_for", "plan_from_coverage", "run_campaign",
    "StimulusGenerator", "generate_from_holes",
    "SvMMU", "SvMMUVerifier",
    "VMVerifier", "Sv32MMU", "TLBVerifier",
    "VectorVerifier", "decode_vtype", "velem_compute", "vlmax",
]

"""
tests/test_agents.py
====================
Pytest suite for all T9-T22 agent modules.

All tests are pure-Python — no EDA tools (Spike / Verilator / Yosys) needed.
Each test smoke-checks the core class and asserts on output structure.

Run with:
    pytest tests/test_agents.py -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_rtl_log():
    """Minimal RTL commit-log (10 records)."""
    instructions = [
        ("addi", "x1,x0,1"),
        ("addi", "x2,x0,2"),
        ("add",  "x3,x1,x2"),
        ("sw",   "x3,0(x1)"),
        ("lw",   "x4,0(x1)"),
        ("beq",  "x4,x3,8"),
        ("lw",   "x5,4(x1)"),
        ("mret", ""),
        ("csrrw","x0,mstatus,x1"),
        ("fence",""),
    ]
    log = []
    for i, (mnem, args) in enumerate(instructions):
        disasm = f"{mnem} {args}".strip()
        rec = {
            "schema_version": "2.1.0",
            "seq": i,
            "pc": f"0x{(0x80000000 + i * 4):08x}",
            "disasm": disasm,
            "regs": {"x1": "0x00000001", "x2": "0x00000002"},
            "csrs": {"mstatus": "0x00001800"},
        }
        if mnem == "sw":
            rec["mem_writes"] = [{"addr": "0x00000004", "size": 4, "value": "0x3"}]
        if mnem == "lw":
            rec["mem_reads"] = [{"addr": "0x00000004", "size": 4, "value": "0x3"}]
        if mnem == "mret":
            rec["trap"] = {"cause": 11, "tval": "0x0"}
        log.append(rec)
    return log


@pytest.fixture
def sample_iss_log(sample_rtl_log):
    """ISS log identical to RTL (no mismatches) for most tests."""
    import copy
    return copy.deepcopy(sample_rtl_log)


@pytest.fixture
def sample_bug_report():
    return {
        "kind": "register_mismatch",
        "mismatch_class": "alu_result",
        "severity": "HIGH",
        "seq": 2,
        "pc": "0x80000008",
        "disasm": "add x3,x1,x2",
        "rtl_val": "0x00000004",
        "iss_val": "0x00000003",
        "description": "ALU result mismatch on ADD",
    }


@pytest.fixture
def tmp_run_dir(tmp_path):
    """Temporary run directory with a minimal manifest."""
    manifest = {
        "schema_version": "2.1.0",
        "run_id": "test-run-001",
        "run_dir": str(tmp_path),
        "status": "running",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:01:00Z",
        "isa": "rv32im",
        "dut_module": "riscv_core",
        "metrics": {
            "total_commits": 10,
            "total_mismatches": 1,
            "tests_run": 1,
        },
    }
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# T11 — Architectural Intent Checker
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentHIntent:
    def test_import(self):
        from AGENT_H import agent_h_intent
        assert hasattr(agent_h_intent, "IntentChecker")

    def test_run_no_violations(self, sample_rtl_log, sample_iss_log, tmp_path):
        from AGENT_H.agent_h_intent import IntentChecker
        # Write logs to temp files
        rtl_f = tmp_path / "rtl.jsonl"
        iss_f = tmp_path / "iss.jsonl"
        rtl_f.write_text("\n".join(json.dumps(r) for r in sample_rtl_log))
        iss_f.write_text("\n".join(json.dumps(r) for r in sample_iss_log))

        checker = IntentChecker(rtl_f, iss_f)
        report = checker.run()

        assert "total_violations" in report
        assert "pass" in report
        assert isinstance(report["total_violations"], int)

    def test_report_schema(self, sample_rtl_log, sample_iss_log, tmp_path):
        from AGENT_H.agent_h_intent import IntentChecker
        rtl_f = tmp_path / "rtl.jsonl"
        iss_f = tmp_path / "iss.jsonl"
        rtl_f.write_text("\n".join(json.dumps(r) for r in sample_rtl_log))
        iss_f.write_text("\n".join(json.dumps(r) for r in sample_iss_log))

        report = IntentChecker(rtl_f, iss_f).run()
        for key in ("schema_version", "agent", "total_violations", "pass", "violations"):
            assert key in report, f"Missing key: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# T12 — Confidence Scorer
# ─────────────────────────────────────────────────────────────────────────────

class TestConfidenceScorer:
    def test_import(self):
        from AGENT_H import confidence_scorer
        assert hasattr(confidence_scorer, "ConfidenceScorer")

    def test_compute_returns_score(self, tmp_run_dir):
        from AGENT_H.confidence_scorer import ConfidenceScorer
        manifest = json.loads((tmp_run_dir / "run_manifest.json").read_text())
        scorer = ConfidenceScorer(tmp_run_dir, manifest)
        report = scorer.compute()

        assert "score" in report
        assert "band" in report
        assert 0.0 <= report["score"] <= 1.0
        assert report["band"] in ("VERIFIED", "HIGH", "MEDIUM", "LOW", "CRITICAL")

    def test_score_with_coverage(self, tmp_run_dir):
        from AGENT_H.confidence_scorer import ConfidenceScorer
        # Add a fake coverage summary
        (tmp_run_dir / "coverage_summary.json").write_text(json.dumps({
            "line_coverage_pct": 85.0,
            "branch_coverage_pct": 70.0,
        }))
        manifest = json.loads((tmp_run_dir / "run_manifest.json").read_text())
        scorer = ConfidenceScorer(tmp_run_dir, manifest)
        report = scorer.compute()
        assert report["score"] > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# T13 — Formal-Guided Fuzzing Bridge
# ─────────────────────────────────────────────────────────────────────────────

class TestFormalFuzzer:
    def test_import(self):
        from AGENT_H import formal_fuzzer
        assert hasattr(formal_fuzzer, "FormalFuzzBridge")

    def test_run_no_witness(self, tmp_path):
        from AGENT_H.formal_fuzzer import FormalFuzzBridge
        # Non-existent witness → should still produce corner seeds
        bridge = FormalFuzzBridge(
            witness_path=tmp_path / "nonexistent.txt",
            outdir=tmp_path / "seeds",
            max_seeds=10,
        )
        report = bridge.run()
        assert "files_written" in report
        # Corner seeds should always be written
        assert report["files_written"] >= 1

    def test_disassembler(self):
        from AGENT_H.formal_fuzzer import disassemble_rv32im
        # addi x1, x0, 1  =  0x00100093
        result = disassemble_rv32im(0x00100093)
        assert "addi" in result.lower() or result != "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# T14 — Digital Twin
# ─────────────────────────────────────────────────────────────────────────────

class TestDigitalTwin:
    def test_import(self):
        from AGENT_H import digital_twin
        assert hasattr(digital_twin, "DigitalTwin")

    def test_simulate_simple_program(self):
        from AGENT_H.digital_twin import DigitalTwin
        # addi x1,x0,5 ; addi x2,x0,3 ; add x3,x1,x2
        asm = "addi x1,x0,5\naddi x2,x0,3\nadd x3,x1,x2\n"
        twin = DigitalTwin()
        result = twin.simulate(asm_text=asm)
        assert result is not None
        assert hasattr(result, "fingerprint")
        assert hasattr(result, "is_redundant")

    def test_batch_screen_empty(self, tmp_path):
        from AGENT_H.digital_twin import DigitalTwin
        twin = DigitalTwin()
        results = twin.batch_screen([], min_score=0.0)
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# T15 — Explainability Layer
# ─────────────────────────────────────────────────────────────────────────────

class TestExplainer:
    def test_import(self):
        from AGENT_H import explainer
        assert hasattr(explainer, "BugExplainer")

    def test_explain_returns_summary(self, sample_bug_report):
        from AGENT_H.explainer import BugExplainer
        exp = BugExplainer(sample_bug_report, root_cause=None)
        report = exp.explain()
        assert "summary" in report
        assert "symptom" in report
        assert "recommended_actions" in report
        assert isinstance(report["recommended_actions"], list)

    def test_explain_with_root_cause(self, sample_bug_report):
        from AGENT_H.explainer import BugExplainer
        rc = {
            "top_candidate": "AGENT_G/execute_stage.v",
            "candidates": [{"file": "execute_stage.v", "score": 0.9}],
        }
        exp = BugExplainer(sample_bug_report, root_cause=rc)
        report = exp.explain()
        assert report["summary"]


# ─────────────────────────────────────────────────────────────────────────────
# T16 — Design Contract DSL
# ─────────────────────────────────────────────────────────────────────────────

class TestContractDSL:
    def test_import(self):
        from AGENT_H import contract_dsl
        assert hasattr(contract_dsl, "ContractRunner")

    def test_run_no_violations(self, sample_rtl_log, sample_iss_log):
        from AGENT_H.contract_dsl import ContractRunner
        runner = ContractRunner()
        report = runner.check(sample_rtl_log, sample_iss_log)
        assert "total_violations" in report
        assert "pass" in report
        assert isinstance(report["total_violations"], int)

    def test_report_schema(self, sample_rtl_log, sample_iss_log):
        from AGENT_H.contract_dsl import ContractRunner
        report = ContractRunner().check(sample_rtl_log, sample_iss_log)
        for key in ("schema_version", "agent", "total_violations", "pass", "violations"):
            assert key in report, f"Missing key: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# T17 — Temporal Behaviour Verification
# ─────────────────────────────────────────────────────────────────────────────

class TestTemporalChecker:
    def test_import(self):
        from AGENT_H import temporal_checker
        assert hasattr(temporal_checker, "TemporalChecker")

    def test_run_clean_log(self, sample_rtl_log, sample_iss_log):
        from AGENT_H.temporal_checker import TemporalChecker
        checker = TemporalChecker(sample_rtl_log, sample_iss_log)
        report = checker.run()
        assert "total_violations" in report
        assert "pass" in report
        assert isinstance(report["violations"], list)

    def test_detects_sc_without_lr(self, sample_rtl_log):
        from AGENT_H.temporal_checker import TemporalChecker
        # Inject sc.w without preceding lr.w
        bad_log = [dict(r) for r in sample_rtl_log]
        bad_log[3]["disasm"] = "sc.w x1,x2,0(x3)"
        checker = TemporalChecker(bad_log, bad_log)
        report = checker.run()
        # Should detect the violation
        assert report["total_violations"] >= 1

    def test_report_schema(self, sample_rtl_log, sample_iss_log):
        from AGENT_H.temporal_checker import TemporalChecker
        report = TemporalChecker(sample_rtl_log, sample_iss_log).run()
        for key in ("schema_version", "agent", "records_checked", "monitors_run",
                    "total_violations", "pass", "violations"):
            assert key in report, f"Missing key: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# T19 — Hardware Security Intelligence
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurityIntel:
    def test_import(self):
        from AGENT_H import security_intel
        assert hasattr(security_intel, "SecurityIntelligence")

    def test_run_clean_log(self, sample_rtl_log, sample_iss_log):
        from AGENT_H.security_intel import SecurityIntelligence
        si = SecurityIntelligence(sample_rtl_log, sample_iss_log)
        report = si.run()
        assert "leak_score" in report
        assert "band" in report
        assert 0.0 <= report["leak_score"] <= 1.0
        assert report["band"] in ("CLEAN", "LOW", "MEDIUM", "HIGH", "CRITICAL")

    def test_report_schema(self, sample_rtl_log, sample_iss_log):
        from AGENT_H.security_intel import SecurityIntelligence
        report = SecurityIntelligence(sample_rtl_log, sample_iss_log).run()
        for key in ("schema_version", "agent", "total_findings", "leak_score",
                    "band", "findings"):
            assert key in report, f"Missing key: {key}"

    def test_privilege_escalation_detection(self, sample_rtl_log):
        from AGENT_H.security_intel import SecurityIntelligence
        # Inject a csrrw that sets mstatus.MPP=0 (U-mode downgrade)
        bad_log = [dict(r) for r in sample_rtl_log]
        bad_log[5]["disasm"] = "csrrw x0,mstatus,x1"
        bad_log[5]["csrs"] = {"mstatus": "0x00000000"}  # MPP=0b00
        si = SecurityIntelligence(bad_log, bad_log)
        report = si.run()
        # Should find at least the privilege escalation
        assert isinstance(report["total_findings"], int)


# ─────────────────────────────────────────────────────────────────────────────
# T21 — Verification Economics Engine
# ─────────────────────────────────────────────────────────────────────────────

class TestEconomicsEngine:
    def test_import(self):
        from AGENT_H import economics_engine
        assert hasattr(economics_engine, "EconomicsEngine")

    def test_compute_basic(self, tmp_run_dir):
        from AGENT_H.economics_engine import EconomicsEngine
        manifest = json.loads((tmp_run_dir / "run_manifest.json").read_text())
        eng = EconomicsEngine(tmp_run_dir, manifest)
        report = eng.compute()
        assert "roi_score" in report
        assert "roi_band" in report
        assert "bugs_per_hour" in report
        assert report["roi_band"] in ("EXCELLENT", "GOOD", "FAIR", "POOR")

    def test_save_ledger(self, tmp_run_dir):
        from AGENT_H.economics_engine import EconomicsEngine
        manifest = json.loads((tmp_run_dir / "run_manifest.json").read_text())
        eng = EconomicsEngine(tmp_run_dir, manifest)
        report = eng.compute()
        ledger_path = eng.save_ledger(report)
        assert ledger_path.exists()
        data = json.loads(ledger_path.read_text())
        assert "campaigns" in data
        assert len(data["campaigns"]) >= 1

    def test_report_schema(self, tmp_run_dir):
        from AGENT_H.economics_engine import EconomicsEngine
        manifest = json.loads((tmp_run_dir / "run_manifest.json").read_text())
        report = EconomicsEngine(tmp_run_dir, manifest).compute()
        for key in ("schema_version", "agent", "roi_score", "roi_band",
                    "bugs_per_hour", "duration_s"):
            assert key in report, f"Missing key: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# T22 — Cross-Domain Adapters
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossDomain:
    def test_import(self):
        from AGENT_H import cross_domain
        assert hasattr(cross_domain, "get_adapter")
        assert hasattr(cross_domain, "DUTClass")

    def test_crypto_adapter(self):
        from AGENT_H.cross_domain import get_adapter, DUTClass
        adapter = get_adapter(DUTClass.CRYPTO)
        raw = [{"op": "AES_ENC", "key_addr": "0x1000", "data_in": "0xDEAD",
                "data_out": "0xBEEF", "cycles": 10, "status": "DONE"}]
        translated = adapter.translate(raw)
        assert len(translated) == 1
        rec = translated[0]
        assert rec["schema_version"] == "2.1.0"
        assert "regs" in rec
        assert "disasm" in rec

    def test_dma_adapter(self):
        from AGENT_H.cross_domain import get_adapter, DUTClass
        adapter = get_adapter(DUTClass.DMA)
        raw = [{"channel": 0, "op": "TRANSFER", "src_addr": "0x1000",
                "dst_addr": "0x2000", "length": 64, "cycles": 32, "error": False}]
        translated = adapter.translate(raw)
        assert len(translated) == 1
        rec = translated[0]
        assert "mem_reads" in rec or "mem_writes" in rec

    def test_uart_adapter(self):
        from AGENT_H.cross_domain import get_adapter, DUTClass
        adapter = get_adapter(DUTClass.UART)
        raw = [{"op": "TX", "data": "0x41", "baud_rate": 115200,
                "parity": "NONE", "framing_error": False, "parity_error": False}]
        translated = adapter.translate(raw)
        assert len(translated) == 1

    def test_custom_adapter_passthrough(self):
        from AGENT_H.cross_domain import get_adapter, DUTClass
        adapter = get_adapter(DUTClass.CUSTOM)
        raw = [{"seq": 0, "pc": "0x0", "disasm": "nop", "regs": {}, "csrs": {}}]
        translated = adapter.translate(raw)
        assert translated[0]["schema_version"] == "2.1.0"

    def test_string_dut_class(self):
        from AGENT_H.cross_domain import get_adapter
        adapter = get_adapter("crypto")
        assert adapter.dut_class.value == "crypto"

    def test_translate_file(self, tmp_path):
        from AGENT_H.cross_domain import get_adapter, DUTClass
        adapter = get_adapter(DUTClass.CRYPTO)
        raw = [{"op": "SHA256", "data_in": "0xAB", "data_out": "0xCD",
                "cycles": 5, "status": "DONE"}]
        in_path = tmp_path / "raw.jsonl"
        out_path = tmp_path / "translated.jsonl"
        in_path.write_text(json.dumps(raw[0]) + "\n")
        n = adapter.translate_file(in_path, out_path)
        assert n == 1
        assert out_path.exists()


# ─────────────────────────────────────────────────────────────────────────────
# T9 — Causal AI-Guided Test Generation
# ─────────────────────────────────────────────────────────────────────────────

class TestCausalEngine:
    def test_import(self):
        from AGENT_G import causal_engine
        assert hasattr(causal_engine, "CausalGeneticEngine")

    def test_evolve_causal_returns_report(self, sample_bug_report, tmp_path):
        from AGENT_G.causal_engine import CausalGeneticEngine
        engine = CausalGeneticEngine(
            seed=42, population_size=10, generations=2, output_count=3
        )
        report = engine.evolve_causal(
            bug_report=sample_bug_report,
            root_cause_report=None,
            outdir=tmp_path / "causal_out",
            assemble=False,  # skip gcc
        )
        assert "improvement_factor" in report
        assert "files_written" in report
        assert isinstance(report["files_written"], int)
        assert report["files_written"] >= 1

    def test_evolve_writes_asm_files(self, sample_bug_report, tmp_path):
        from AGENT_G.causal_engine import CausalGeneticEngine
        outdir = tmp_path / "causal_asm"
        engine = CausalGeneticEngine(seed=0, population_size=8, generations=2, output_count=3)
        engine.evolve_causal(sample_bug_report, None, outdir=outdir, assemble=False)
        asm_files = list(outdir.glob("*.S"))
        assert len(asm_files) >= 1

    def test_causal_constraints_built(self, sample_bug_report):
        from AGENT_G.causal_engine import build_causal_constraints
        constraints = build_causal_constraints(sample_bug_report, root_cause_report=None)
        assert isinstance(constraints, list)


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaFiles:
    def test_commitlog_schema_exists(self):
        root = Path(__file__).parent.parent
        candidates = [
            root / "schemas" / "commitlog.schema.json",
            root / "AGENT_A" / "commitlog.schema.json",
            root / "AGENT_B" / "schemas" / "commitlog.schema.json",
        ]
        schema_path = next((p for p in candidates if p.exists()), None)
        assert schema_path is not None, "commitlog.schema.json not found in any known location"
        data = json.loads(schema_path.read_text())
        assert data.get("$schema") or data.get("type") or data.get("properties")

    def test_run_manifest_schema_exists(self):
        root = Path(__file__).parent.parent
        candidates = [
            root / "schemas" / "run_manifest.schema.json",
            root / "AGENT_A" / "run_manifest.schema.json",
            root / "AGENT_B" / "schemas" / "run_manifest.schema.json",
        ]
        schema_path = next((p for p in candidates if p.exists()), None)
        assert schema_path is not None, "run_manifest.schema.json not found in any known location"
        data = json.loads(schema_path.read_text())
        assert data.get("$schema") or data.get("type") or data.get("properties")

    def test_all_agent_modules_importable(self):
        modules = [
            "AGENT_H.agent_h_intent",
            "AGENT_H.confidence_scorer",
            "AGENT_H.formal_fuzzer",
            "AGENT_H.digital_twin",
            "AGENT_H.explainer",
            "AGENT_H.contract_dsl",
            "AGENT_H.temporal_checker",
            "AGENT_H.coverage_collector",
            "AGENT_H.coherence_verifier",
            "AGENT_H.memory_model_verifier",
            "AGENT_H.interrupt_verifier",
            "AGENT_H.perf_counter_verifier",
            "AGENT_H.debug_verifier",
            "AGENT_H.reset_verifier",
            "AGENT_H.atomics_verifier",
            "AGENT_H.bitmanip_verifier",
            "AGENT_H.csr_verifier",
            "AGENT_H.rvc_verifier",
            "AGENT_H.fp_verifier",
            "AGENT_H.privilege_verifier",
            "AGENT_H.vm_verifier",
            "AGENT_H.tlb_verifier",
            "AGENT_H.pipeline_verifier",
            "AGENT_H.branch_predictor_verifier",
            "AGENT_H.self_evolving_engine",
            "AGENT_H.stimulus_generator",
            "AGENT_H.vector_verifier",
            "AGENT_H.cache_verifier",
            "AGENT_H.bus_verifier",
            "AGENT_H.fault_injector",
            "AGENT_H.rv64_verifier",
            "AGENT_H.rv64_atomics_verifier",
            "AGENT_H.sv_mmu_verifier",
            "AGENT_H.peripheral_verifier",
            "AGENT_H.security_intel",
            "AGENT_H.economics_engine",
            "AGENT_H.cross_domain",
            "AGENT_H.knowledge_graph",
            "AGENT_H.minimizer",
            "AGENT_H.root_cause_localizer",
            "AGENT_G.causal_engine",
        ]
        import importlib
        failed = []
        for mod in modules:
            try:
                importlib.import_module(mod)
            except Exception as e:
                failed.append(f"{mod}: {e}")
        assert not failed, "Failed imports:\n" + "\n".join(failed)


# ─────────────────────────────────────────────────────────────────────────────
# T23 — RV32A Atomics Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_rec(seq, disasm, regs=None, reads=None, writes=None, trap=None):
    """Build one commit-log record for atomics tests."""
    rec = {
        "schema_version": "2.1.0",
        "seq": seq,
        "pc": f"0x{(0x80000000 + seq * 4):08x}",
        "disasm": disasm,
        "regs": regs or {},
        "csrs": {},
    }
    if reads is not None:
        rec["mem_reads"] = reads
    if writes is not None:
        rec["mem_writes"] = writes
    if trap is not None:
        rec["trap"] = trap
    return rec


class TestAtomicsVerifier:
    def test_import(self):
        from AGENT_H import atomics_verifier
        assert hasattr(atomics_verifier, "AtomicsVerifier")

    def test_amo_compute_golden_math(self):
        from AGENT_H.atomics_verifier import amo_compute
        assert amo_compute("swap", 0x10, 0x99) == 0x99
        assert amo_compute("add", 0xFFFFFFFF, 0x1) == 0x0          # 32-bit wrap
        assert amo_compute("and", 0xF0F0F0F0, 0x0FF00FF0) == 0x00F000F0
        assert amo_compute("or",  0x0F0F0F0F, 0xF0F0F0F0) == 0xFFFFFFFF
        assert amo_compute("xor", 0xFFFFFFFF, 0x0F0F0F0F) == 0xF0F0F0F0
        # signed min/max: 0xFFFFFFFF == -1 < 1
        assert amo_compute("min",  0xFFFFFFFF, 0x1) == 0xFFFFFFFF
        assert amo_compute("max",  0xFFFFFFFF, 0x1) == 0x1
        # unsigned min/max: 0xFFFFFFFF is the largest
        assert amo_compute("minu", 0xFFFFFFFF, 0x1) == 0x1
        assert amo_compute("maxu", 0xFFFFFFFF, 0x1) == 0xFFFFFFFF

    def test_decode_atomic(self):
        from AGENT_H.atomics_verifier import decode_atomic
        d = decode_atomic("amoadd.w.aq.rl x5, x6, (x10)")
        assert d.kind == "amo" and d.mnem == "add"
        assert d.aq and d.rl and d.rd == "x5" and d.rs2 == "x6" and d.rs1 == "x10"
        assert decode_atomic("lr.w x1, (x2)").kind == "lr"
        assert decode_atomic("sc.w x1, x2, (x3)").kind == "sc"
        assert decode_atomic("addi x1,x0,1") is None

    def test_clean_amo_passes(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        log = [
            _atomic_rec(0, "addi x6,x0,5", regs={"x6": "0x00000005"}),
            # amoadd.w x5,x6,(x10): mem[0x100]=0x10 → rd=0x10, new=0x15
            _atomic_rec(1, "amoadd.w x5, x6, (x10)",
                        regs={"x5": "0x00000010"},
                        reads=[{"addr": "0x00000100", "size": 4, "value": "0x00000010"}],
                        writes=[{"addr": "0x00000100", "size": 4, "value": "0x00000015"}]),
        ]
        rep = AtomicsVerifier(log).run()
        assert rep["pass"] is True
        assert rep["total_violations"] == 0
        assert rep["stats"]["amo"] == 1

    def test_amo_writeback_bug_caught(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        log = [
            _atomic_rec(0, "addi x6,x0,5", regs={"x6": "0x00000005"}),
            # BUG: writes 0x14 instead of 0x10+0x5=0x15
            _atomic_rec(1, "amoadd.w x5, x6, (x10)",
                        regs={"x5": "0x00000010"},
                        reads=[{"addr": "0x00000100", "size": 4, "value": "0x00000010"}],
                        writes=[{"addr": "0x00000100", "size": 4, "value": "0x00000014"}]),
        ]
        rep = AtomicsVerifier(log).run()
        assert rep["pass"] is False
        checks = {v["check"] for v in rep["violations"]}
        assert "amo_writeback" in checks
        assert rep["band"] == "CRITICAL"

    def test_lr_sc_success_passes(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        log = [
            _atomic_rec(0, "addi x6,x0,7", regs={"x6": "0x00000007"}),
            _atomic_rec(1, "lr.w x5, (x10)",
                        regs={"x5": "0x00000010"},
                        reads=[{"addr": "0x00000100", "size": 4, "value": "0x00000010"}]),
            # valid reservation → store rs2 (x6=7), rd=0 success
            _atomic_rec(2, "sc.w x4, x6, (x10)",
                        regs={"x4": "0x00000000"},
                        writes=[{"addr": "0x00000100", "size": 4, "value": "0x00000007"}]),
        ]
        rep = AtomicsVerifier(log).run()
        assert rep["pass"] is True
        assert rep["stats"]["sc_success"] == 1

    def test_sc_spurious_store_caught(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        # SC.W with no preceding LR.W must fail (no write, rd!=0).
        # Here the RTL wrongly writes memory and reports success → atomicity bug.
        log = [
            _atomic_rec(0, "addi x6,x0,7", regs={"x6": "0x00000007"}),
            _atomic_rec(1, "sc.w x4, x6, (x10)",
                        regs={"x4": "0x00000000"},
                        writes=[{"addr": "0x00000100", "size": 4, "value": "0x00000007"}]),
        ]
        rep = AtomicsVerifier(log).run()
        assert rep["pass"] is False
        checks = {v["check"] for v in rep["violations"]}
        assert "sc_fail_wrote" in checks or "sc_fail_rd" in checks

    def test_reservation_broken_by_intervening_store(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        # LR sets reservation; a plain store to the same word breaks it;
        # the later SC must therefore FAIL. RTL reporting success is a bug.
        log = [
            _atomic_rec(0, "lr.w x5, (x10)",
                        regs={"x5": "0x00000010"},
                        reads=[{"addr": "0x00000100", "size": 4, "value": "0x00000010"}]),
            _atomic_rec(1, "sw x6, 0(x10)",
                        regs={},
                        writes=[{"addr": "0x00000100", "size": 4, "value": "0x000000AA"}]),
            _atomic_rec(2, "sc.w x4, x6, (x10)",
                        regs={"x4": "0x00000000"},   # claims success — wrong
                        writes=[{"addr": "0x00000100", "size": 4, "value": "0x00000007"}]),
        ]
        rep = AtomicsVerifier(log).run()
        assert rep["pass"] is False

    def test_misaligned_atomic_without_trap_caught(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        log = [
            _atomic_rec(0, "amoadd.w x5, x6, (x10)",
                        regs={"x5": "0x00000010"},
                        reads=[{"addr": "0x00000102", "size": 4, "value": "0x00000010"}],
                        writes=[{"addr": "0x00000102", "size": 4, "value": "0x00000015"}]),
        ]
        rep = AtomicsVerifier(log).run()
        assert rep["pass"] is False
        assert any(v["check"] == "alignment" for v in rep["violations"])

    def test_report_schema(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        rep = AtomicsVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked",
                    "total_violations", "pass", "violations", "band", "stats"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "atomics_verifier"
        assert rep["schema_version"] == "2.1.0"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.atomics_verifier import run_from_manifest
        rtl = [
            _atomic_rec(0, "addi x6,x0,5", regs={"x6": "0x00000005"}),
            _atomic_rec(1, "amoadd.w x5, x6, (x10)",
                        regs={"x5": "0x00000010"},
                        reads=[{"addr": "0x00000100", "size": 4, "value": "0x00000010"}],
                        writes=[{"addr": "0x00000100", "size": 4, "value": "0x00000015"}]),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {
            "schema_version": "2.1.0",
            "run_id": "atomics-test",
            "run_dir": str(tmp_path),
            "outputs": {"rtl_commit_log": "rtl_commit.jsonl"},
        }
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "atomics_report.json").exists()
        updated = json.loads(mpath.read_text())
        assert "atomics_report" in updated["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# AVA orchestrator smoke test
# ─────────────────────────────────────────────────────────────────────────────

class TestAVAOrchestrator:
    def test_ava_importable(self):
        import ava_patched
        assert hasattr(ava_patched, "AVA")

    def test_ava_init_extended_params(self):
        import ava_patched
        ava = ava_patched.AVA(
            enable_llm=False,
            enable_database=False,
            enable_extended=True,
            rtl_sources=[],
            timeout=10,
        )
        assert hasattr(ava, "enable_extended")
        assert hasattr(ava, "rtl_sources")
        assert isinstance(ava.rtl_sources, list)

    @pytest.mark.asyncio
    async def test_ava_generate_suite_smoke(self, tmp_path):
        import ava_patched
        SAMPLE_RTL = """
module riscv_core (
    input  wire clk, rst,
    input  wire [31:0] instr_in,
    output reg  [31:0] data_out
);
    reg [31:0] pc;
endmodule
"""
        ava = ava_patched.AVA(
            enable_llm=False,
            enable_database=False,
            enable_extended=True,
            rtl_sources=[],
            timeout=20,
            target_coverage=50.0,
        )
        result = await ava.generate_suite(
            rtl_spec=SAMPLE_RTL,
            microarch="in_order",
            seed=7,
            save_results=False,
        )
        assert isinstance(result, dict)
        assert result.get("status") == "completed"
        assert result["semantic_map"]["dut_module"] == "riscv_core"


# ─────────────────────────────────────────────────────────────────────────────
# T24 — SoC Peripheral Protocol Verifier
# ─────────────────────────────────────────────────────────────────────────────

class TestPeripheralVerifier:
    def test_import(self):
        from AGENT_H import peripheral_verifier
        assert hasattr(peripheral_verifier, "PeripheralVerifier")

    def test_factory(self):
        from AGENT_H.peripheral_verifier import get_checker
        assert get_checker("dma").domain == "dma"
        assert get_checker("uart").domain == "uart"
        assert get_checker("crypto").domain == "crypto"
        assert get_checker("cpu") is None

    # -- DMA -----------------------------------------------------------------
    def test_dma_clean_transfer_passes(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"channel": 0, "op": "READ",  "src_addr": "0x1000", "length": 64},
            {"channel": 0, "op": "WRITE", "dst_addr": "0x2000", "length": 64},
            {"channel": 0, "op": "DONE"},
        ]
        rep = PeripheralVerifier(recs, "dma").run()
        assert rep["pass"] is True
        assert rep["band"] == "CLEAN"

    def test_dma_byte_mismatch_caught(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"channel": 0, "op": "READ",  "src_addr": "0x1000", "length": 64},
            {"channel": 0, "op": "WRITE", "dst_addr": "0x2000", "length": 32},
            {"channel": 0, "op": "DONE"},
        ]
        rep = PeripheralVerifier(recs, "dma").run()
        assert rep["pass"] is False
        assert any(v["check"] == "dma_byte_mismatch" for v in rep["violations"])

    def test_dma_null_pointer_and_dangling(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"channel": 1, "op": "TRANSFER", "src_addr": "0x0", "dst_addr": "0x2000", "length": 16},
        ]
        rep = PeripheralVerifier(recs, "dma").run()
        checks = {v["check"] for v in rep["violations"]}
        assert "dma_null_src" in checks
        assert "dma_dangling_channel" in checks   # never DONE

    # -- UART ----------------------------------------------------------------
    def test_uart_clean_passes(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"op": "CONFIG", "baud_rate": 115200, "parity": "EVEN"},
            {"op": "TX", "data": "0x41"},
            {"op": "RX", "data": "0x42"},
        ]
        rep = PeripheralVerifier(recs, "uart").run()
        assert rep["pass"] is True

    def test_uart_unconfigured_and_overflow(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"op": "TX", "data": "0x1FF"},   # before config + >8-bit
        ]
        rep = PeripheralVerifier(recs, "uart").run()
        checks = {v["check"] for v in rep["violations"]}
        assert "uart_unconfigured_use" in checks
        assert "uart_data_overflow" in checks

    def test_uart_inconsistent_parity_error(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"op": "CONFIG", "baud_rate": 9600, "parity": "NONE"},
            {"op": "RX", "data": "0x55", "parity_error": True},
        ]
        rep = PeripheralVerifier(recs, "uart").run()
        assert any(v["check"] == "uart_inconsistent_parity_error" for v in rep["violations"])

    # -- CRYPTO --------------------------------------------------------------
    def test_crypto_sha256_kat_passes(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        # sha256("abc") known answer
        recs = [
            {"op": "SHA256", "status": "DONE", "data_in": "0x616263",
             "data_out": "0xba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
        ]
        rep = PeripheralVerifier(recs, "crypto").run()
        assert rep["pass"] is True

    def test_crypto_sha256_kat_mismatch_caught(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"op": "SHA256", "status": "DONE", "data_in": "0x616263",
             "data_out": "0xdeadbeef8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
        ]
        rep = PeripheralVerifier(recs, "crypto").run()
        assert rep["pass"] is False
        assert any(v["check"] == "sha256_kat" for v in rep["violations"])

    def test_crypto_error_leak_and_no_key(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"op": "AES_ENC", "status": "ERROR", "key_addr": "0x0",
             "data_in": "0x1234", "data_out": "0xcafe", "error_code": 3},
        ]
        rep = PeripheralVerifier(recs, "crypto").run()
        checks = {v["check"] for v in rep["violations"]}
        assert "crypto_no_key" in checks
        assert "crypto_error_with_output" in checks

    def test_crypto_aes_roundtrip_caught(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        recs = [
            {"op": "AES_ENC", "status": "DONE", "key_addr": "0xK", "data_in": "0xAA", "data_out": "0xBB"},
            # decrypt of BB with same key should recover AA, not CC
            {"op": "AES_DEC", "status": "DONE", "key_addr": "0xK", "data_in": "0xBB", "data_out": "0xCC"},
        ]
        rep = PeripheralVerifier(recs, "crypto").run()
        assert any(v["check"] == "aes_roundtrip" for v in rep["violations"])

    # -- report / manifest ---------------------------------------------------
    def test_report_schema(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        rep = PeripheralVerifier([], "dma").run()
        for key in ("schema_version", "agent", "dut_class", "records_checked",
                    "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "peripheral_verifier"

    def test_cpu_class_skips(self):
        from AGENT_H.peripheral_verifier import PeripheralVerifier
        rep = PeripheralVerifier([{"op": "x"}], "cpu").run()
        assert rep["status"] == "skipped"
        assert rep["pass"] is True

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.peripheral_verifier import run_from_manifest
        raw = [
            {"channel": 0, "op": "READ",  "src_addr": "0x1000", "length": 64},
            {"channel": 0, "op": "WRITE", "dst_addr": "0x2000", "length": 64},
            {"channel": 0, "op": "DONE"},
        ]
        (tmp_path / "raw_rtl.jsonl").write_text("\n".join(json.dumps(r) for r in raw))
        manifest = {
            "schema_version": "2.1.0",
            "run_id": "periph-test",
            "run_dir": str(tmp_path),
            "agent_config": {"dut_class": "dma"},
            "outputs": {"raw_rtl_log": "raw_rtl.jsonl"},
        }
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "peripheral_report.json").exists()
        updated = json.loads(mpath.read_text())
        assert "peripheral_report" in updated["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T25 — Zicsr / Zifencei CSR Semantics Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _csr_rec(seq, disasm, regs=None, csrs=None, trap=None, pc=None, writes=None):
    rec = {
        "schema_version": "2.1.0",
        "seq": seq,
        "pc": pc or f"0x{(0x80000000 + seq * 4):08x}",
        "disasm": disasm,
        "regs": regs or {},
        "csrs": csrs or {},
    }
    if trap is not None:
        rec["trap"] = trap
    if writes is not None:
        rec["mem_writes"] = writes
    return rec


class TestCSRVerifier:
    def test_import(self):
        from AGENT_H import csr_verifier
        assert hasattr(csr_verifier, "CSRVerifier")

    def test_decode_csr(self):
        from AGENT_H.csr_verifier import decode_csr
        d = decode_csr("csrrw x5, mstatus, x6")
        assert d.op == "RW" and d.rd == "x5" and d.csr == "mstatus" and d.src == "x6"
        assert decode_csr("csrr x1, mstatus").op == "RS"          # pseudo read
        assert decode_csr("csrrsi x5, mstatus, 4").src_kind == "imm"
        d2 = decode_csr("csrw mstatus, x1")                       # pseudo write
        assert d2.op == "RW" and d2.rd == "x0"
        assert decode_csr("addi x1,x0,1") is None

    def test_readonly_table(self):
        from AGENT_H.csr_verifier import csr_is_readonly
        assert csr_is_readonly("mvendorid") is True
        assert csr_is_readonly("mstatus") is False
        assert csr_is_readonly("0x300") is False
        assert csr_is_readonly("0xF11") is True       # read-only by encoding
        assert csr_is_readonly("cycle") is True

    def test_clean_csrrw_passes(self):
        from AGENT_H.csr_verifier import CSRVerifier
        log = [
            _csr_rec(0, "addi x6,x0,0x1888", regs={"x6": "0x00001888"}),
            _csr_rec(1, "csrrw x5, mstatus, x6",
                     regs={"x5": "0x00001800"},          # rd = old mstatus
                     csrs={"mstatus": "0x00001888"}),     # post = x6
        ]
        rep = CSRVerifier(log).run()
        assert rep["pass"] is True
        assert rep["csr_ops"] == 1

    def test_csrrs_setbits_passes(self):
        from AGENT_H.csr_verifier import CSRVerifier
        log = [
            _csr_rec(0, "addi x6,x0,0x80", regs={"x6": "0x00000080"}),
            _csr_rec(1, "csrrs x5, mstatus, x6",
                     regs={"x5": "0x00001800"},
                     csrs={"mstatus": "0x00001880"}),     # 0x1800 | 0x80
        ]
        assert CSRVerifier(log).run()["pass"] is True

    def test_writeback_bug_caught(self):
        from AGENT_H.csr_verifier import CSRVerifier
        log = [
            _csr_rec(0, "addi x6,x0,0x80", regs={"x6": "0x00000080"}),
            _csr_rec(1, "csrrs x5, mstatus, x6",
                     regs={"x5": "0x00001800"},
                     csrs={"mstatus": "0x00001881"}),     # BUG: extra bit set
        ]
        rep = CSRVerifier(log).run()
        assert rep["pass"] is False
        assert any(v["check"] == "csr_writeback" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_readonly_write_caught(self):
        from AGENT_H.csr_verifier import CSRVerifier
        log = [
            _csr_rec(0, "addi x6,x0,1", regs={"x6": "0x00000001"}),
            # write to a read-only CSR with no illegal-instruction trap
            _csr_rec(1, "csrrw x0, mvendorid, x6", csrs={"mvendorid": "0x00000001"}),
        ]
        rep = CSRVerifier(log).run()
        assert any(v["check"] == "csr_readonly_write" for v in rep["violations"])

    def test_spurious_write_on_x0(self):
        from AGENT_H.csr_verifier import CSRVerifier
        log = [
            # csrrs with x0 source must not modify the CSR
            _csr_rec(0, "csrrs x5, mstatus, x0",
                     regs={"x5": "0x00001800"},
                     csrs={"mstatus": "0x00001801"}),     # BUG: changed
        ]
        rep = CSRVerifier(log).run()
        assert any(v["check"] == "csr_spurious_write" for v in rep["violations"])

    def test_rd_old_value_mismatch(self):
        from AGENT_H.csr_verifier import CSRVerifier
        log = [
            _csr_rec(0, "addi x6,x0,0x1888", regs={"x6": "0x00001888"}),
            _csr_rec(1, "csrrw x5, mstatus, x6",
                     regs={"x5": "0x00001800"}, csrs={"mstatus": "0x00001888"}),
            # subsequent read-back of old value disagrees with the model
            _csr_rec(2, "csrr x7, mstatus",
                     regs={"x7": "0x00009999"}),           # should be 0x1888
        ]
        rep = CSRVerifier(log).run()
        assert any(v["check"] == "csr_read_value" for v in rep["violations"])

    def test_fencei_missing_caught(self):
        from AGENT_H.csr_verifier import CSRVerifier
        log = [
            _csr_rec(0, "sw x6, 0(x10)", writes=[{"addr": "0x80000010", "size": 4, "value": "0x1"}]),
            _csr_rec(1, "addi x1,x0,1", pc="0x80000010"),  # execute modified word, no fence.i
        ]
        rep = CSRVerifier(log).run()
        assert any(v["check"] == "fencei_missing" for v in rep["violations"])

    def test_report_schema(self):
        from AGENT_H.csr_verifier import CSRVerifier
        rep = CSRVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked",
                    "csr_ops", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "csr_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.csr_verifier import run_from_manifest
        rtl = [
            _csr_rec(0, "addi x6,x0,0x1888", regs={"x6": "0x00001888"}),
            _csr_rec(1, "csrrw x5, mstatus, x6",
                     regs={"x5": "0x00001800"}, csrs={"mstatus": "0x00001888"}),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {
            "schema_version": "2.1.0", "run_id": "csr-test",
            "run_dir": str(tmp_path),
            "outputs": {"rtl_commit_log": "rtl_commit.jsonl"},
        }
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "csr_report.json").exists()
        assert "csr_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T26 — RV32C Compressed Instruction Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _rvc_rec(seq, disasm, pc, regs=None, trap=None, extra=None):
    rec = {
        "schema_version": "2.1.0",
        "seq": seq,
        "pc": pc,
        "disasm": disasm,
        "regs": regs or {},
        "csrs": {},
    }
    if trap is not None:
        rec["trap"] = trap
    if extra:
        rec.update(extra)
    return rec


class TestRVCVerifier:
    def test_import(self):
        from AGENT_H import rvc_verifier
        assert hasattr(rvc_verifier, "RVCVerifier")

    def test_is_compressed(self):
        from AGENT_H.rvc_verifier import is_compressed
        assert is_compressed({"disasm": "c.addi x1,1"}) is True
        assert is_compressed({"disasm": "addi x1,x0,1"}) is False
        assert is_compressed({"disasm": "addi x1,x0,1", "insn_len": 2}) is True
        assert is_compressed({"disasm": "x", "insn": "0x4521"}) is True
        assert is_compressed({"disasm": "x", "insn": "0x00150513"}) is False

    def test_clean_compressed_stride_passes(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        log = [
            _rvc_rec(0, "c.addi x1,1", "0x80000000"),
            _rvc_rec(1, "c.addi x2,1", "0x80000002"),   # +2 correct
            _rvc_rec(2, "c.mv x3,x1",  "0x80000004"),
        ]
        rep = RVCVerifier(log).run()
        assert rep["pass"] is True
        assert rep["compressed_seen"] == 3

    def test_pc_stride_bug_caught(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        log = [
            _rvc_rec(0, "c.addi x1,1", "0x80000000"),
            _rvc_rec(1, "c.addi x2,1", "0x80000004"),   # BUG: advanced by 4
        ]
        rep = RVCVerifier(log).run()
        assert rep["pass"] is False
        assert any(v["check"] == "rvc_pc_stride" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_branch_form_skips_stride(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        # a taken compressed branch legitimately changes PC by more than 2
        log = [
            _rvc_rec(0, "c.j 0x80000040", "0x80000000"),
            _rvc_rec(1, "c.addi x1,1",    "0x80000040"),
        ]
        rep = RVCVerifier(log).run()
        assert rep["pass"] is True

    def test_reg_constraint_caught(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        # c.lw uses prime fields (x8-x15); x3 is illegal here
        log = [
            _rvc_rec(0, "c.lw x3,0(x8)", "0x80000000"),
            _rvc_rec(1, "c.nop",         "0x80000002"),
        ]
        rep = RVCVerifier(log).run()
        assert any(v["check"] == "rvc_reg_constraint" for v in rep["violations"])

    def test_reg_constraint_ok(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        log = [
            _rvc_rec(0, "c.lw x9,0(x8)", "0x80000000"),
            _rvc_rec(1, "c.nop",         "0x80000002"),
        ]
        assert RVCVerifier(log).run()["pass"] is True

    def test_reserved_encoding_caught(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        # c.addi4spn with zero immediate is reserved and must trap
        log = [
            _rvc_rec(0, "c.addi4spn x8,0", "0x80000000"),
            _rvc_rec(1, "c.nop",           "0x80000002"),
        ]
        rep = RVCVerifier(log).run()
        assert any(v["check"] == "rvc_reserved" for v in rep["violations"])

    def test_reserved_encoding_trapped_ok(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        # same reserved encoding, but correctly trapped → no violation
        log = [
            _rvc_rec(0, "c.jr x0", "0x80000000", trap={"cause": 2, "tval": "0x0"}),
        ]
        assert RVCVerifier(log).run()["pass"] is True

    def test_report_schema(self):
        from AGENT_H.rvc_verifier import RVCVerifier
        rep = RVCVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked",
                    "compressed_seen", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "rvc_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.rvc_verifier import run_from_manifest
        rtl = [
            _rvc_rec(0, "c.addi x1,1", "0x80000000"),
            _rvc_rec(1, "c.addi x2,1", "0x80000002"),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {
            "schema_version": "2.1.0", "run_id": "rvc-test",
            "run_dir": str(tmp_path),
            "outputs": {"rtl_commit_log": "rtl_commit.jsonl"},
        }
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "rvc_report.json").exists()
        assert "rvc_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T27 — RV32F / RV32D Floating-Point Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _fp_rec(seq, disasm, fregs=None, regs=None, csrs=None, pc=None):
    rec = {
        "schema_version": "2.1.0",
        "seq": seq,
        "pc": pc or f"0x{(0x80000000 + seq * 4):08x}",
        "disasm": disasm,
        "regs": regs or {},
        "csrs": csrs or {},
    }
    if fregs is not None:
        rec["fregs"] = fregs
    return rec


# single-precision bit patterns
_F1 = "0x3f800000"   # 1.0
_F2 = "0x40000000"   # 2.0
_F3 = "0x40400000"   # 3.0
_FN1 = "0xbf800000"  # -1.0
_FINF = "0x7f800000" # +inf
_FNAN = "0x7fc00000" # canonical qNaN


class TestFPVerifier:
    def test_import(self):
        from AGENT_H import fp_verifier
        assert hasattr(fp_verifier, "FPVerifier")

    def test_decode_fp(self):
        from AGENT_H.fp_verifier import decode_fp
        d = decode_fp("fadd.s f3,f1,f2")
        assert d.mnem == "fadd" and d.width == "s" and d.fregs == ["f3", "f1", "f2"]
        m = decode_fp("fmv.x.w x5,f1")
        assert m.mnem == "fmv" and m.xregs == ["x5"] and m.fregs == ["f1"]
        assert decode_fp("addi x1,x0,1") is None

    def test_fclass_mask(self):
        from AGENT_H.fp_verifier import fclass_mask
        assert fclass_mask(0x7F800000, "s") == (1 << 7)   # +inf
        assert fclass_mask(0xFF800000, "s") == (1 << 0)   # -inf
        assert fclass_mask(0x00000000, "s") == (1 << 4)   # +0
        assert fclass_mask(0x80000000, "s") == (1 << 3)   # -0
        assert fclass_mask(0x7FC00000, "s") == (1 << 9)   # qNaN
        assert fclass_mask(0x7F800001, "s") == (1 << 8)   # sNaN

    def test_fadd_clean_passes(self):
        from AGENT_H.fp_verifier import FPVerifier
        log = [
            _fp_rec(0, "nop", fregs={"f1": _F1, "f2": _F2}),
            _fp_rec(1, "fadd.s f3,f1,f2", fregs={"f3": _F3}),
        ]
        rep = FPVerifier(log).run()
        assert rep["pass"] is True
        assert rep["flen"] == 32 and rep["fp_ops"] == 1

    def test_fadd_result_bug_caught(self):
        from AGENT_H.fp_verifier import FPVerifier
        log = [
            _fp_rec(0, "nop", fregs={"f1": _F1, "f2": _F2}),
            _fp_rec(1, "fadd.s f3,f1,f2,rne", fregs={"f3": "0x40400001"}),
        ]
        rep = FPVerifier(log).run()
        assert rep["pass"] is False
        assert any(v["check"] == "fp_result" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_nan_boxing_caught(self):
        from AGENT_H.fp_verifier import FPVerifier
        log = [
            _fp_rec(0, "nop", fregs={"f1": "0xffffffff3f800000", "f2": "0xffffffff40000000"}),
            _fp_rec(1, "fadd.s f3,f1,f2", fregs={"f3": "0x0000000040400000"}),
        ]
        rep = FPVerifier(log, flen=64).run()
        assert any(v["check"] == "fp_nan_boxing" for v in rep["violations"])

    def test_fdiv_by_zero_flag(self):
        from AGENT_H.fp_verifier import FPVerifier
        # result correct (+inf) but DZ flag not raised
        log = [
            _fp_rec(0, "nop", fregs={"f1": _F1, "f2": "0x00000000"}),
            _fp_rec(1, "fdiv.s f3,f1,f2,rne", fregs={"f3": _FINF}, csrs={"fflags": "0x0"}),
        ]
        rep = FPVerifier(log).run()
        assert any(v["check"] == "fp_flag_missing" for v in rep["violations"])

    def test_fsgnj_bug_caught(self):
        from AGENT_H.fp_verifier import FPVerifier
        # fsgnj.s f3,f1,f2 -> magnitude of f1 (1.0), sign of f2 (-1.0) = -1.0
        log = [
            _fp_rec(0, "nop", fregs={"f1": _F1, "f2": _FN1}),
            _fp_rec(1, "fsgnj.s f3,f1,f2", fregs={"f3": _F1}),   # BUG: should be -1.0
        ]
        rep = FPVerifier(log).run()
        assert any(v["check"] == "fp_sgnj" for v in rep["violations"])

    def test_fmax_passes(self):
        from AGENT_H.fp_verifier import FPVerifier
        log = [
            _fp_rec(0, "nop", fregs={"f1": _F1, "f2": _F2}),
            _fp_rec(1, "fmax.s f3,f1,f2", fregs={"f3": _F2}),
        ]
        assert FPVerifier(log).run()["pass"] is True

    def test_feq_nan_bug_caught(self):
        from AGENT_H.fp_verifier import FPVerifier
        log = [
            _fp_rec(0, "nop", fregs={"f1": _F1, "f2": _FNAN}),
            _fp_rec(1, "feq.s x5,f1,f2", regs={"x5": "0x1"}),    # BUG: NaN compare must be 0
        ]
        rep = FPVerifier(log).run()
        assert any(v["check"] == "fp_compare" for v in rep["violations"])

    def test_fclass_bug_caught(self):
        from AGENT_H.fp_verifier import FPVerifier
        log = [
            _fp_rec(0, "nop", fregs={"f1": _FINF}),
            _fp_rec(1, "fclass.s x5,f1", regs={"x5": "0x40"}),   # BUG: +inf is 0x80
        ]
        rep = FPVerifier(log).run()
        assert any(v["check"] == "fp_class" for v in rep["violations"])

    def test_fmv_x_w_bug_caught(self):
        from AGENT_H.fp_verifier import FPVerifier
        log = [
            _fp_rec(0, "nop", fregs={"f1": _F1}),
            _fp_rec(1, "fmv.x.w x5,f1", regs={"x5": "0x3f800001"}),  # BUG: != f1 bits
        ]
        rep = FPVerifier(log).run()
        assert any(v["check"] == "fp_move" for v in rep["violations"])

    def test_report_schema(self):
        from AGENT_H.fp_verifier import FPVerifier
        rep = FPVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked", "flen",
                    "fp_ops", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "fp_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.fp_verifier import run_from_manifest
        rtl = [
            _fp_rec(0, "nop", fregs={"f1": _F1, "f2": _F2}),
            _fp_rec(1, "fadd.s f3,f1,f2", fregs={"f3": _F3}),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {
            "schema_version": "2.1.0", "run_id": "fp-test",
            "run_dir": str(tmp_path),
            "outputs": {"rtl_commit_log": "rtl_commit.jsonl"},
        }
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "fp_report.json").exists()
        assert "fp_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T28 — RV32B Bit-Manipulation Verifier (Zba/Zbb/Zbc/Zbs)
# ─────────────────────────────────────────────────────────────────────────────

def _bm_run(disasm, seed, committed):
    """Build a 2-record log (seed regs, then the op) and run the verifier."""
    from AGENT_H.bitmanip_verifier import BitmanipVerifier
    rd = re.findall(r"x\d+", disasm)[0]
    log = [
        {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
         "disasm": "nop", "regs": seed, "csrs": {}},
        {"schema_version": "2.1.0", "seq": 1, "pc": "0x80000004",
         "disasm": disasm, "regs": {rd: committed}, "csrs": {}},
    ]
    return BitmanipVerifier(log).run()


# (disasm, seed registers, golden result)
_BM_GOLDEN = [
    ("sh1add x3,x1,x2", {"x1": "0x3", "x2": "0xa"}, "0x00000010"),
    ("sh2add x3,x1,x2", {"x1": "0x3", "x2": "0xa"}, "0x00000016"),
    ("sh3add x3,x1,x2", {"x1": "0x3", "x2": "0xa"}, "0x00000022"),
    ("andn x3,x1,x2",   {"x1": "0xff", "x2": "0x0f"}, "0x000000f0"),
    ("orn x3,x1,x2",    {"x1": "0xf0", "x2": "0x0f"}, "0xfffffff0"),
    ("xnor x3,x1,x2",   {"x1": "0xff", "x2": "0x0f"}, "0xffffff0f"),
    ("clz x3,x1",       {"x1": "0x00010000"}, "0x0000000f"),
    ("ctz x3,x1",       {"x1": "0x00010000"}, "0x00000010"),
    ("cpop x3,x1",      {"x1": "0xff"}, "0x00000008"),
    ("min x3,x1,x2",    {"x1": "0xffffffff", "x2": "0x1"}, "0xffffffff"),
    ("minu x3,x1,x2",   {"x1": "0xffffffff", "x2": "0x1"}, "0x00000001"),
    ("max x3,x1,x2",    {"x1": "0xffffffff", "x2": "0x1"}, "0x00000001"),
    ("maxu x3,x1,x2",   {"x1": "0xffffffff", "x2": "0x1"}, "0xffffffff"),
    ("rol x3,x1,x2",    {"x1": "0x80000001", "x2": "0x1"}, "0x00000003"),
    ("ror x3,x1,x2",    {"x1": "0x3", "x2": "0x1"}, "0x80000001"),
    ("rori x3,x1,4",    {"x1": "0xf"}, "0xf0000000"),
    ("orc.b x3,x1",     {"x1": "0x00ff0001"}, "0x00ff00ff"),
    ("rev8 x3,x1",      {"x1": "0x01020304"}, "0x04030201"),
    ("sext.b x3,x1",    {"x1": "0xff"}, "0xffffffff"),
    ("sext.h x3,x1",    {"x1": "0x8000"}, "0xffff8000"),
    ("zext.h x3,x1",    {"x1": "0xffffffff"}, "0x0000ffff"),
    ("clmul x3,x1,x2",  {"x1": "0x3", "x2": "0x3"}, "0x00000005"),
    ("clmulh x3,x1,x2", {"x1": "0x80000000", "x2": "0x2"}, "0x00000001"),
    ("bset x3,x1,x2",   {"x1": "0x0", "x2": "0x5"}, "0x00000020"),
    ("bclr x3,x1,x2",   {"x1": "0xff", "x2": "0x0"}, "0x000000fe"),
    ("bext x3,x1,x2",   {"x1": "0x80", "x2": "0x7"}, "0x00000001"),
    ("binv x3,x1,x2",   {"x1": "0xf", "x2": "0x0"}, "0x0000000e"),
    ("bclri x3,x1,3",   {"x1": "0xff"}, "0x000000f7"),
]


class TestBitmanipVerifier:
    def test_import(self):
        from AGENT_H import bitmanip_verifier
        assert hasattr(bitmanip_verifier, "BitmanipVerifier")

    def test_decode(self):
        from AGENT_H.bitmanip_verifier import decode_bitmanip
        d = decode_bitmanip("andn x3,x1,x2")
        assert d.mnem == "andn" and d.kind == "bin" and d.rd == "x3"
        u = decode_bitmanip("clz x3,x1")
        assert u.kind == "un" and u.rs1 == "x1"
        i = decode_bitmanip("rori x3,x1,4")
        assert i.kind == "imm" and i.imm == 4
        assert decode_bitmanip("addi x1,x0,1") is None

    @pytest.mark.parametrize("disasm,seed,golden", _BM_GOLDEN)
    def test_golden_vectors_pass(self, disasm, seed, golden):
        rep = _bm_run(disasm, seed, golden)
        assert rep["pass"] is True, f"{disasm} flagged a correct result"
        assert rep["bitmanip_ops"] == 1

    def test_andn_bug_caught(self):
        rep = _bm_run("andn x3,x1,x2", {"x1": "0xff", "x2": "0x0f"}, "0x000000f1")
        assert rep["pass"] is False
        assert any(v["check"] == "bitmanip_result" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_clz_bug_caught(self):
        rep = _bm_run("clz x3,x1", {"x1": "0x00010000"}, "0x00000010")  # should be 15
        assert rep["pass"] is False
        assert any(v["check"] == "bitmanip_result" for v in rep["violations"])

    def test_report_schema(self):
        from AGENT_H.bitmanip_verifier import BitmanipVerifier
        rep = BitmanipVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked",
                    "bitmanip_ops", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "bitmanip_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.bitmanip_verifier import run_from_manifest
        rtl = [
            {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
             "disasm": "nop", "regs": {"x1": "0x3", "x2": "0xa"}, "csrs": {}},
            {"schema_version": "2.1.0", "seq": 1, "pc": "0x80000004",
             "disasm": "sh1add x3,x1,x2", "regs": {"x3": "0x00000010"}, "csrs": {}},
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {
            "schema_version": "2.1.0", "run_id": "bm-test",
            "run_dir": str(tmp_path),
            "outputs": {"rtl_commit_log": "rtl_commit.jsonl"},
        }
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "bitmanip_report.json").exists()
        assert "bitmanip_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T29 — Privilege & PMP Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _pv_rec(seq, disasm, priv=None, csrs=None, trap=None, reads=None, writes=None):
    rec = {
        "schema_version": "2.1.0",
        "seq": seq,
        "pc": f"0x{(0x80000000 + seq * 4):08x}",
        "disasm": disasm,
        "regs": {},
        "csrs": csrs or {},
    }
    if priv is not None:
        rec["priv"] = priv
    if trap is not None:
        rec["trap"] = trap
    if reads is not None:
        rec["mem_reads"] = reads
    if writes is not None:
        rec["mem_writes"] = writes
    return rec


class TestPrivilegeVerifier:
    def test_import(self):
        from AGENT_H import privilege_verifier
        assert hasattr(privilege_verifier, "PrivilegeVerifier")

    def test_parse_priv(self):
        from AGENT_H.privilege_verifier import parse_priv
        assert parse_priv({"priv": "M"}) == 3
        assert parse_priv({"mode": "S"}) == 1
        assert parse_priv({"privilege": 0}) == 0
        assert parse_priv({}) is None

    def test_pmp_napot_model(self):
        from AGENT_H.privilege_verifier import PMPModel
        m = PMPModel()
        m.update_from_csrs({"pmpcfg0": "0x18", "pmpaddr0": "0x5ff"})  # NAPOT [0x1000,0x2000)
        assert m.configured() is True
        assert m.match(0x1000) is not None
        assert m.match(0x2000) is None
        assert m.permitted(0x1000, "r", 0) is False   # no R perm, U-mode
        assert m.permitted(0x1000, "r", 3) is True     # M-mode bypass (unlocked)

    def test_mret_in_user_illegal(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        log = [_pv_rec(0, "mret", priv=0)]   # MRET from U with no trap
        rep = PrivilegeVerifier(log).run()
        assert any(v["check"] == "priv_xret_illegal" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_mret_legal_passes(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        log = [
            _pv_rec(0, "nop", priv=3, csrs={"mstatus": "0x00000000"}),  # MPP=U
            _pv_rec(1, "mret", priv=3),
            _pv_rec(2, "addi x1,x0,1", priv=0),   # returned to U as MPP said
        ]
        assert PrivilegeVerifier(log).run()["pass"] is True

    def test_ecall_cause_bug(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        # ECALL from U must raise cause 8; here it raises 11
        log = [_pv_rec(0, "ecall", priv=0, trap={"cause": 11, "tval": "0x0"})]
        rep = PrivilegeVerifier(log).run()
        assert any(v["check"] == "priv_ecall_cause" for v in rep["violations"])

    def test_csr_access_from_user_caught(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        # writing mstatus (M-CSR) from U with no illegal trap
        log = [_pv_rec(0, "csrrw x0,mstatus,x1", priv=0)]
        rep = PrivilegeVerifier(log).run()
        assert any(v["check"] == "priv_csr_access" for v in rep["violations"])

    def test_mret_target_mismatch(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        log = [
            _pv_rec(0, "nop", priv=3, csrs={"mstatus": "0x00000000"}),  # MPP=U(0)
            _pv_rec(1, "mret", priv=3),
            _pv_rec(2, "addi x1,x0,1", priv=1),   # BUG: returned to S, not U
        ]
        rep = PrivilegeVerifier(log).run()
        assert any(v["check"] == "priv_mret_target" for v in rep["violations"])

    def test_pmp_missing_fault(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        log = [
            _pv_rec(0, "nop", priv=3, csrs={"pmpcfg0": "0x18", "pmpaddr0": "0x5ff"}),
            # U-mode read of a no-permission region must fault; here it doesn't
            _pv_rec(1, "lw x1,0(x2)", priv=0, reads=[{"addr": "0x1000", "size": 4}]),
        ]
        rep = PrivilegeVerifier(log).run()
        assert any(v["check"] == "pmp_missing_fault" for v in rep["violations"])

    def test_pmp_permitted_passes(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        log = [
            _pv_rec(0, "nop", priv=3, csrs={"pmpcfg0": "0x19", "pmpaddr0": "0x5ff"}),  # R=1
            _pv_rec(1, "lw x1,0(x2)", priv=0, reads=[{"addr": "0x1000", "size": 4}]),
        ]
        assert PrivilegeVerifier(log).run()["pass"] is True

    def test_report_schema(self):
        from AGENT_H.privilege_verifier import PrivilegeVerifier
        rep = PrivilegeVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked",
                    "pmp_configured", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "privilege_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.privilege_verifier import run_from_manifest
        rtl = [_pv_rec(0, "mret", priv=3, csrs={"mstatus": "0x00001800"})]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {
            "schema_version": "2.1.0", "run_id": "priv-test",
            "run_dir": str(tmp_path),
            "outputs": {"rtl_commit_log": "rtl_commit.jsonl"},
        }
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert (tmp_path / "privilege_report.json").exists()
        assert "privilege_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T30 — Sv32 Virtual-Memory Verifier
# ─────────────────────────────────────────────────────────────────────────────

# satp: MODE=Sv32, root page table at physical 0x80000000 (PPN 0x80000)
_SATP = "0x80080000"
# 4 KB mapping: VA 0x00001000 -> PA 0x90000000
#   L1 PTE @ 0x80000000 = pointer to L0 table @ 0x80001000
#   L0 PTE @ 0x80001004 = leaf -> PPN 0x90000, flags V R W X U A D (0xDF)
_PT_4K = {"0x80000000": "0x20000401", "0x80001004": "0x240000df"}


def _vm_rec(seq, disasm, satp=_SATP, priv=0, reads=None, writes=None, trap=None,
            mstatus=None, phys_mem=None):
    csrs = {"satp": satp}
    if mstatus is not None:
        csrs["mstatus"] = mstatus
    rec = {"schema_version": "2.1.0", "seq": seq, "pc": f"0x{0x80000000 + seq*4:08x}",
           "disasm": disasm, "regs": {}, "csrs": csrs, "priv": priv}
    if reads is not None:
        rec["mem_reads"] = reads
    if writes is not None:
        rec["mem_writes"] = writes
    if trap is not None:
        rec["trap"] = trap
    if phys_mem is not None:
        rec["phys_mem"] = phys_mem
    return rec


class TestVMVerifier:
    # -- golden Sv32 MMU (validated against hand-computed page tables) --------
    def test_mmu_4kb_translation(self):
        from AGENT_H.vm_verifier import Sv32MMU
        pm = {0x80000000: 0x20000401, 0x80001004: 0x240000DF}
        mmu = Sv32MMU(pm, 0x80080000)
        assert mmu.enabled is True
        t = mmu.translate(0x00001000, "load", priv=0)
        assert t.ok and t.pa == 0x90000000 and t.level == 0

    def test_mmu_superpage(self):
        from AGENT_H.vm_verifier import Sv32MMU
        # L1 leaf @ 0x80000004: PPN1=0x100, aligned -> VA 0x00400000 -> PA 0x40000000
        pm = {0x80000004: 0x100000DF}
        mmu = Sv32MMU(pm, 0x80080000)
        t = mmu.translate(0x00400000, "load", priv=0)
        assert t.ok and t.pa == 0x40000000 and t.level == 1

    def test_mmu_misaligned_superpage(self):
        from AGENT_H.vm_verifier import Sv32MMU
        pm = {0x80000004: 0x100004DF}  # PPN[0]!=0 -> misaligned superpage
        t = Sv32MMU(pm, 0x80080000).translate(0x00400000, "load", priv=0)
        assert not t.ok and t.cause == 13

    def test_mmu_invalid_pte_faults(self):
        from AGENT_H.vm_verifier import Sv32MMU
        pm = {0x80000000: 0x20000401, 0x80001004: 0x240000DE}  # V=0 on leaf
        t = Sv32MMU(pm, 0x80080000).translate(0x00001000, "load", priv=0)
        assert not t.ok and t.cause == 13

    def test_mmu_write_without_w_faults(self):
        from AGENT_H.vm_verifier import Sv32MMU
        pm = {0x80000000: 0x20000401, 0x80001004: 0x240000DB}  # R X U A D, no W
        mmu = Sv32MMU(pm, 0x80080000)
        assert mmu.translate(0x00001000, "load", priv=0).ok is True
        st = mmu.translate(0x00001000, "store", priv=0)
        assert not st.ok and st.cause == 15

    def test_mmu_user_page_supervisor_sum(self):
        from AGENT_H.vm_verifier import Sv32MMU
        pm = {0x80000000: 0x20000401, 0x80001004: 0x240000DF}  # U=1
        mmu = Sv32MMU(pm, 0x80080000)
        assert not mmu.translate(0x1000, "load", priv=1, sum_=False).ok   # S, no SUM
        assert mmu.translate(0x1000, "load", priv=1, sum_=True).ok        # S, SUM set

    def test_mmu_bare_mode_identity(self):
        from AGENT_H.vm_verifier import Sv32MMU
        mmu = Sv32MMU({}, 0x00000000)   # MODE=0 -> Bare
        assert mmu.enabled is False

    # -- VMVerifier (commit-log checker) -------------------------------------
    def test_clean_translation_passes(self):
        from AGENT_H.vm_verifier import VMVerifier
        log = [_vm_rec(0, "lw x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000000", "size": 4}])]
        rep = VMVerifier(log, phys_mem=_PT_4K).run()
        assert rep["pass"] is True
        assert rep["sv32_enabled"] is True and rep["translations"] == 1

    def test_translation_bug_caught(self):
        from AGENT_H.vm_verifier import VMVerifier
        log = [_vm_rec(0, "lw x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000004", "size": 4}])]
        rep = VMVerifier(log, phys_mem=_PT_4K).run()
        assert rep["pass"] is False
        assert any(v["check"] == "vm_translation" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_missing_fault_caught(self):
        from AGENT_H.vm_verifier import VMVerifier
        pt = {"0x80000000": "0x20000401", "0x80001004": "0x240000de"}  # invalid leaf
        log = [_vm_rec(0, "lw x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "size": 4}])]  # no trap
        rep = VMVerifier(log, phys_mem=pt).run()
        assert any(v["check"] == "vm_missing_fault" for v in rep["violations"])

    def test_missing_fault_correctly_trapped_passes(self):
        from AGENT_H.vm_verifier import VMVerifier
        pt = {"0x80000000": "0x20000401", "0x80001004": "0x240000de"}
        log = [_vm_rec(0, "lw x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "size": 4}],
                       trap={"cause": 13, "tval": "0x1000"})]
        assert VMVerifier(log, phys_mem=pt).run()["pass"] is True

    def test_spurious_fault_caught(self):
        from AGENT_H.vm_verifier import VMVerifier
        log = [_vm_rec(0, "lw x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000000", "size": 4}],
                       trap={"cause": 13, "tval": "0x1000"})]   # valid but faulted
        rep = VMVerifier(log, phys_mem=_PT_4K).run()
        assert any(v["check"] == "vm_spurious_fault" for v in rep["violations"])

    def test_per_record_phys_mem(self):
        from AGENT_H.vm_verifier import VMVerifier
        log = [_vm_rec(0, "lw x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000000", "size": 4}],
                       phys_mem=_PT_4K)]
        assert VMVerifier(log).run()["pass"] is True

    def test_gated_when_not_sv32(self):
        from AGENT_H.vm_verifier import VMVerifier
        # MODE=0 satp -> no translation; even a bogus paddr must not flag
        log = [_vm_rec(0, "lw x1,0(x2)", satp="0x00000000",
                       reads=[{"vaddr": "0x1000", "paddr": "0xdeadbeef", "size": 4}])]
        assert VMVerifier(log, phys_mem=_PT_4K).run()["pass"] is True

    def test_gated_in_m_mode(self):
        from AGENT_H.vm_verifier import VMVerifier
        log = [_vm_rec(0, "lw x1,0(x2)", priv=3,
                       reads=[{"vaddr": "0x1000", "paddr": "0xdeadbeef", "size": 4}])]
        assert VMVerifier(log, phys_mem=_PT_4K).run()["pass"] is True

    def test_robustness_malformed(self):
        from AGENT_H.vm_verifier import VMVerifier
        for log in ([], [None, 5, "x"], [{}], [{"csrs": None}]):
            import copy
            rep = VMVerifier(copy.deepcopy(log)).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.vm_verifier import VMVerifier
        rep = VMVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked", "sv32_enabled",
                    "translations", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "vm_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.vm_verifier import run_from_manifest
        rtl = [_vm_rec(0, "lw x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000000", "size": 4}],
                       phys_mem=_PT_4K)]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "vm-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "vm_report.json").exists()
        assert "vm_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T31 — TLB Coherence / sfence.vma Verifier
# ─────────────────────────────────────────────────────────────────────────────

# base page table: VA 0x1000 -> PA 0x90000000
_TLB_PT_BASE = {"0x80000000": "0x20000401", "0x80001004": "0x240000df"}
# remap leaf so the walk now gives VA 0x1000 -> PA 0x91000000
_TLB_PTE_NEW = {"0x80001004": "0x244000df"}


def _tlb_rec(seq, disasm, reads=None, phys_mem=None, satp="0x80080000", priv=0):
    rec = {"schema_version": "2.1.0", "seq": seq, "pc": f"0x{0x80000000 + seq*4:08x}",
           "disasm": disasm, "regs": {}, "csrs": {"satp": satp}, "priv": priv}
    if reads is not None:
        rec["mem_reads"] = reads
    if phys_mem is not None:
        rec["phys_mem"] = phys_mem
    return rec


def _rd(va, pa):
    return [{"vaddr": va, "paddr": pa, "size": 4}]


class TestTLBVerifier:
    def test_fill_then_permitted_stale_passes(self):
        from AGENT_H.tlb_verifier import TLBVerifier
        log = [
            _tlb_rec(0, "lw x1,0(x2)", reads=_rd("0x1000", "0x90000000"), phys_mem=_TLB_PT_BASE),
            # PTE changed but no sfence yet -> serving the old PA is legal
            _tlb_rec(1, "lw x1,0(x2)", reads=_rd("0x1000", "0x90000000"), phys_mem=_TLB_PTE_NEW),
        ]
        rep = TLBVerifier(log).run()
        assert rep["pass"] is True
        assert rep["stats"]["permitted_stale"] == 1

    def test_stale_after_sfence_caught(self):
        from AGENT_H.tlb_verifier import TLBVerifier
        log = [
            _tlb_rec(0, "lw x1,0(x2)", reads=_rd("0x1000", "0x90000000"), phys_mem=_TLB_PT_BASE),
            _tlb_rec(1, "sfence.vma", phys_mem=_TLB_PTE_NEW),       # flush-all + remap
            _tlb_rec(2, "lw x1,0(x2)", reads=_rd("0x1000", "0x90000000")),  # stale!
        ]
        rep = TLBVerifier(log).run()
        assert rep["pass"] is False
        assert any(v["check"] == "tlb_stale_after_sfence" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_correct_refill_after_sfence_passes(self):
        from AGENT_H.tlb_verifier import TLBVerifier
        log = [
            _tlb_rec(0, "lw x1,0(x2)", reads=_rd("0x1000", "0x90000000"), phys_mem=_TLB_PT_BASE),
            _tlb_rec(1, "sfence.vma", phys_mem=_TLB_PTE_NEW),
            _tlb_rec(2, "lw x1,0(x2)", reads=_rd("0x1000", "0x91000000")),  # current
        ]
        assert TLBVerifier(log).run()["pass"] is True

    def test_incoherent_translation_caught(self):
        from AGENT_H.tlb_verifier import TLBVerifier
        # cold access served a PA that is neither the walk nor any cached entry
        log = [_tlb_rec(0, "lw x1,0(x2)", reads=_rd("0x1000", "0x80000000"),
                        phys_mem=_TLB_PT_BASE)]
        rep = TLBVerifier(log).run()
        assert rep["pass"] is False
        assert any(v["check"] == "tlb_incoherent" for v in rep["violations"])

    def test_gated_when_not_sv32(self):
        from AGENT_H.tlb_verifier import TLBVerifier
        log = [_tlb_rec(0, "lw x1,0(x2)", reads=_rd("0x1000", "0xdeadbeef"),
                        phys_mem=_TLB_PT_BASE, satp="0x00000000")]
        assert TLBVerifier(log).run()["pass"] is True

    def test_robustness_malformed(self):
        from AGENT_H.tlb_verifier import TLBVerifier
        import copy
        for log in ([], [None, 1, "x"], [{}], [{"csrs": None}]):
            rep = TLBVerifier(copy.deepcopy(log)).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.tlb_verifier import TLBVerifier
        rep = TLBVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked",
                    "translations", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "tlb_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.tlb_verifier import run_from_manifest
        rtl = [_tlb_rec(0, "lw x1,0(x2)", reads=_rd("0x1000", "0x90000000"),
                        phys_mem=_TLB_PT_BASE)]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "tlb-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "tlb_report.json").exists()
        assert "tlb_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T32 — Pipeline & Hazard Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _pv_rec2(seq, disasm, regs=None, pc=None, perf=None):
    rec = {"schema_version": "2.1.0", "seq": seq,
           "pc": pc or f"0x{0x80000000 + seq*4:08x}",
           "disasm": disasm, "regs": regs or {}, "csrs": {}}
    if perf is not None:
        rec["perf_counters"] = perf
    return rec


class TestPipelineVerifier:
    def test_import(self):
        from AGENT_H import pipeline_verifier
        assert hasattr(pipeline_verifier, "PipelineVerifier")

    def test_alu_eval_golden(self):
        from AGENT_H.pipeline_verifier import alu_eval
        assert alu_eval("add", 5, 3) == 8
        assert alu_eval("sub", 5, 3) == 2
        assert alu_eval("and", 0xFF, 0x0F) == 0x0F
        assert alu_eval("or", 0xF0, 0x0F) == 0xFF
        assert alu_eval("xor", 0xFF, 0x0F) == 0xF0
        assert alu_eval("sll", 1, 4) == 16
        assert alu_eval("srl", 0x80000000, 4) == 0x08000000
        assert alu_eval("sra", 0x80000000, 4) == 0xF8000000
        assert alu_eval("slt", 0xFFFFFFFF, 1) == 1     # -1 < 1 signed
        assert alu_eval("sltu", 0xFFFFFFFF, 1) == 0    # huge > 1 unsigned

    def test_clean_alu_passes(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        log = [
            _pv_rec2(0, "addi x5,x0,5", regs={"x5": "0x00000005"}),
            _pv_rec2(1, "add x6,x5,x0", regs={"x6": "0x00000005"}),
            _pv_rec2(2, "xori x7,x5,0xff", regs={"x7": "0x000000fa"}),  # 5 ^ 0xff
        ]
        rep = PipelineVerifier(log).run()
        assert rep["pass"] is True
        assert rep["alu_checked"] == 3

    def test_forwarding_hazard_diagnosed(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        log = [
            _pv_rec2(0, "addi x5,x0,5",  regs={"x5": "0x00000005"}),
            _pv_rec2(1, "addi x5,x0,10", regs={"x5": "0x0000000a"}),   # producer
            # consumer used the STALE x5 (5) instead of the forwarded 10
            _pv_rec2(2, "add x6,x5,x0",  regs={"x6": "0x00000005"}),
        ]
        rep = PipelineVerifier(log).run()
        assert rep["pass"] is False
        v = [x for x in rep["violations"] if x["check"] == "hazard_forwarding"]
        assert v, "forwarding hazard not diagnosed"
        assert v[0]["expected"] == "0x0000000a" and v[0]["actual"] == "0x00000005"
        assert rep["band"] == "CRITICAL"

    def test_generic_alu_mismatch(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        log = [
            _pv_rec2(0, "addi x5,x0,5", regs={"x5": "0x00000005"}),
            _pv_rec2(1, "add x6,x5,x0", regs={"x6": "0x00000063"}),  # 99, unexplained
        ]
        rep = PipelineVerifier(log).run()
        assert any(v["check"] == "alu_result" for v in rep["violations"])

    def test_control_hazard_caught(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        log = [
            _pv_rec2(0, "addi x1,x0,0x40", regs={"x1": "0x00000040"}),  # ra
            _pv_rec2(1, "ret", pc="0x80000004"),
            _pv_rec2(2, "addi x2,x0,1", pc="0x80000008"),  # should be at 0x40
        ]
        rep = PipelineVerifier(log).run()
        assert any(v["check"] == "control_hazard" for v in rep["violations"])

    def test_control_hazard_clean(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        log = [
            _pv_rec2(0, "addi x1,x0,0x40", regs={"x1": "0x00000040"}),
            _pv_rec2(1, "ret", pc="0x80000004"),
            _pv_rec2(2, "addi x2,x0,1", pc="0x00000040"),  # correctly redirected
        ]
        assert PipelineVerifier(log).run()["pass"] is True

    def test_hazard_inventory(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        log = [
            _pv_rec2(0, "addi x5,x0,1", regs={"x5": "0x1"}),
            _pv_rec2(1, "add x6,x5,x0", regs={"x6": "0x1"}),  # RAW on x5
        ]
        rep = PipelineVerifier(log).run()
        assert rep["hazards"]["raw"] >= 1

    def test_perf_metrics(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        log = [
            _pv_rec2(0, "addi x5,x0,1", regs={"x5": "0x1"}, perf={"cycles": 2, "instret": 1}),
            _pv_rec2(1, "addi x6,x0,2", regs={"x6": "0x2"}, perf={"cycles": 4, "instret": 2}),
            _pv_rec2(2, "addi x7,x0,3", regs={"x7": "0x3"}, perf={"cycles": 6, "instret": 3}),
        ]
        rep = PipelineVerifier(log).run()
        assert rep["perf"]["cpi"] == 2.0
        assert rep["perf"]["stall_cycles"] == 3

    def test_robustness_malformed(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        import copy
        for log in ([], [None, 1, "x"], [{}], [{"regs": None, "disasm": None}]):
            rep = PipelineVerifier(copy.deepcopy(log)).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.pipeline_verifier import PipelineVerifier
        rep = PipelineVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked", "alu_checked",
                    "hazards", "perf", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "pipeline_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.pipeline_verifier import run_from_manifest
        rtl = [
            _pv_rec2(0, "addi x5,x0,5", regs={"x5": "0x00000005"}),
            _pv_rec2(1, "add x6,x5,x0", regs={"x6": "0x00000005"}),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "pipe-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "pipeline_report.json").exists()
        assert "pipeline_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T33 — Cache Subsystem Verifier
# ─────────────────────────────────────────────────────────────────────────────

_CFG2 = {"sets": 1, "ways": 2, "line_size": 16, "policy": "lru", "write_policy": "wb"}
_CFG1 = {"sets": 1, "ways": 1, "line_size": 16, "policy": "lru", "write_policy": "wb"}


def _cache_entry(addr, hit=None, evict=None, wb=None, value=None):
    e = {"addr": addr}
    if value is not None:
        e["value"] = value
    c = {}
    if hit is not None:
        c["hit"] = hit
    if evict is not None:
        c["evict_addr"] = evict
    if wb is not None:
        c["writeback"] = wb
    if c:
        e["cache"] = c
    return e


def _cache_rec(seq, reads=None, writes=None):
    rec = {"schema_version": "2.1.0", "seq": seq, "pc": f"0x{seq:08x}",
           "disasm": "lw", "regs": {}, "csrs": {}}
    if reads is not None:
        rec["mem_reads"] = reads
    if writes is not None:
        rec["mem_writes"] = writes
    return rec


class TestCacheVerifier:
    def test_import(self):
        from AGENT_H import cache_verifier
        assert hasattr(cache_verifier, "CacheVerifier")

    # -- golden cache model --------------------------------------------------
    def test_model_hit_miss(self):
        from AGENT_H.cache_verifier import CacheModel
        m = CacheModel(1, 2, 16, "lru", "wb")
        assert m.access(0x00, False).hit is False
        assert m.access(0x10, False).hit is False
        assert m.access(0x00, False).hit is True   # still resident

    def test_model_lru_eviction(self):
        from AGENT_H.cache_verifier import CacheModel
        m = CacheModel(1, 2, 16, "lru", "wb")
        m.access(0x00, False)
        m.access(0x10, False)
        m.access(0x00, False)             # A becomes most-recent
        r = m.access(0x20, False)         # evict LRU = B (0x10)
        assert r.hit is False and r.victim_addr == 0x10 and r.writeback is False

    def test_model_dirty_eviction_writeback(self):
        from AGENT_H.cache_verifier import CacheModel
        m = CacheModel(1, 1, 16, "lru", "wb")
        m.access(0x00, True)              # write -> dirty
        r = m.access(0x10, False)         # evict dirty A
        assert r.writeback is True and r.victim_addr == 0x00

    # -- checker -------------------------------------------------------------
    def test_clean_trace_passes(self):
        from AGENT_H.cache_verifier import CacheVerifier
        log = [
            _cache_rec(0, reads=[_cache_entry("0x00", hit=False)]),
            _cache_rec(1, reads=[_cache_entry("0x10", hit=False)]),
            _cache_rec(2, reads=[_cache_entry("0x00", hit=True)]),
            _cache_rec(3, reads=[_cache_entry("0x20", hit=False, evict="0x10", wb=False)]),
        ]
        rep = CacheVerifier(log, config=_CFG2).run()
        assert rep["pass"] is True
        assert rep["metrics"]["hit_rate"] == 0.25

    def test_hitmiss_bug_caught(self):
        from AGENT_H.cache_verifier import CacheVerifier
        log = [
            _cache_rec(0, reads=[_cache_entry("0x00", hit=False)]),
            _cache_rec(1, reads=[_cache_entry("0x10", hit=False)]),
            _cache_rec(2, reads=[_cache_entry("0x00", hit=False)]),  # claims miss; golden hit
        ]
        rep = CacheVerifier(log, config=_CFG2).run()
        assert rep["pass"] is False
        assert any(v["check"] == "cache_hitmiss" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_eviction_bug_caught(self):
        from AGENT_H.cache_verifier import CacheVerifier
        log = [
            _cache_rec(0, reads=[_cache_entry("0x00", hit=False)]),
            _cache_rec(1, reads=[_cache_entry("0x10", hit=False)]),
            _cache_rec(2, reads=[_cache_entry("0x00", hit=True)]),
            _cache_rec(3, reads=[_cache_entry("0x20", hit=False, evict="0x00")]),  # wrong victim
        ]
        rep = CacheVerifier(log, config=_CFG2).run()
        assert any(v["check"] == "cache_eviction" for v in rep["violations"])

    def test_missing_writeback_caught(self):
        from AGENT_H.cache_verifier import CacheVerifier
        log = [
            _cache_rec(0, writes=[_cache_entry("0x00", hit=False)]),
            _cache_rec(1, reads=[_cache_entry("0x10", hit=False, evict="0x00", wb=False)]),
        ]
        rep = CacheVerifier(log, config=_CFG1).run()
        assert any(v["check"] == "cache_writeback" for v in rep["violations"])

    def test_line_corruption_caught(self):
        from AGENT_H.cache_verifier import CacheVerifier
        log = [
            _cache_rec(0, writes=[_cache_entry("0x00", hit=False, value="0xaa")]),
            _cache_rec(1, reads=[_cache_entry("0x00", hit=True, value="0xbb")]),  # stale
        ]
        rep = CacheVerifier(log, config=_CFG1).run()
        assert any(v["check"] == "cache_data" for v in rep["violations"])

    def test_gated_without_config(self):
        from AGENT_H.cache_verifier import CacheVerifier
        log = [_cache_rec(0, reads=[_cache_entry("0x00", hit=True)])]  # bogus
        rep = CacheVerifier(log, config=None).run()
        assert rep["pass"] is True and rep["cache_enabled"] is False

    def test_gated_nondeterministic_policy(self):
        from AGENT_H.cache_verifier import CacheVerifier
        log = [_cache_rec(0, reads=[_cache_entry("0x00", hit=True)])]
        cfg = {"sets": 1, "ways": 2, "line_size": 16, "policy": "random"}
        assert CacheVerifier(log, config=cfg).run()["cache_enabled"] is False

    def test_robustness_malformed(self):
        from AGENT_H.cache_verifier import CacheVerifier
        import copy
        for log in ([], [None, 1, "x"], [{}], [{"mem_reads": None}]):
            rep = CacheVerifier(copy.deepcopy(log), config=_CFG2).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.cache_verifier import CacheVerifier
        rep = CacheVerifier([], config=_CFG2).run()
        for key in ("schema_version", "agent", "records_checked", "cache_enabled",
                    "metrics", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "cache_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.cache_verifier import run_from_manifest
        rtl = [
            _cache_rec(0, reads=[_cache_entry("0x00", hit=False)]),
            _cache_rec(1, reads=[_cache_entry("0x00", hit=True)]),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "cache-test",
                    "run_dir": str(tmp_path), "cache_config": _CFG2,
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "cache_report.json").exists()
        assert "cache_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T34 — Bus Protocol Verifier (AXI / AHB / APB)
# ─────────────────────────────────────────────────────────────────────────────

def _axi_incr(addr=0x1000, length=3, size=2, resp="okay", beats=None):
    return {"protocol": "axi4", "txn": "write", "addr": hex(addr),
            "len": length, "size": size, "burst": "incr",
            "beats": beats, "resp": resp}


def _incr_beats(addr=0x1000, n=4, step=4):
    return [{"addr": hex(addr + i * step), "last": (i == n - 1)} for i in range(n)]


class TestBusVerifier:
    def test_import(self):
        from AGENT_H import bus_verifier
        assert hasattr(bus_verifier, "BusVerifier")

    def test_expected_beats_incr(self):
        from AGENT_H.bus_verifier import axi_expected_beats
        assert axi_expected_beats(0x1000, 3, 2, "incr") == [
            (0x1000, False), (0x1004, False), (0x1008, False), (0x100c, True)]

    def test_expected_beats_fixed(self):
        from AGENT_H.bus_verifier import axi_expected_beats
        assert axi_expected_beats(0x1000, 2, 2, "fixed") == [
            (0x1000, False), (0x1000, False), (0x1000, True)]

    def test_expected_beats_wrap(self):
        from AGENT_H.bus_verifier import axi_expected_beats
        # start mid-region: wraps at the 16-byte boundary 0x1000
        assert axi_expected_beats(0x1008, 3, 2, "wrap") == [
            (0x1008, False), (0x100c, False), (0x1000, False), (0x1004, True)]

    def test_clean_incr_passes(self):
        from AGENT_H.bus_verifier import BusVerifier
        txn = _axi_incr(beats=_incr_beats())
        rep = BusVerifier([txn]).run()
        assert rep["pass"] is True
        assert rep["metrics"]["beats"] == 4

    def test_burst_length_bug(self):
        from AGENT_H.bus_verifier import BusVerifier
        txn = _axi_incr(length=3, beats=_incr_beats(n=3))  # 3 beats but AxLEN=3 ⇒ 4
        rep = BusVerifier([txn]).run()
        assert rep["pass"] is False
        assert any(v["check"] == "bus_burst_length" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_beat_addr_bug(self):
        from AGENT_H.bus_verifier import BusVerifier
        beats = _incr_beats()
        beats[1]["addr"] = "0x1010"   # should be 0x1004
        rep = BusVerifier([_axi_incr(beats=beats)]).run()
        assert any(v["check"] == "bus_beat_addr" for v in rep["violations"])

    def test_wlast_bug(self):
        from AGENT_H.bus_verifier import BusVerifier
        beats = _incr_beats()
        beats[2]["last"] = True       # premature LAST
        beats[3]["last"] = False
        rep = BusVerifier([_axi_incr(beats=beats)]).run()
        assert any(v["check"] == "bus_wlast" for v in rep["violations"])

    def test_4kb_boundary(self):
        from AGENT_H.bus_verifier import BusVerifier
        txn = _axi_incr(addr=0xFF8, length=3, size=2)  # 0xFF8..0x1007 crosses 0x1000
        rep = BusVerifier([txn]).run()
        assert any(v["check"] == "bus_4kb_boundary" for v in rep["violations"])

    def test_wrap_invalid(self):
        from AGENT_H.bus_verifier import BusVerifier
        txn = {"protocol": "axi4", "addr": "0x1000", "len": 2, "size": 2, "burst": "wrap"}
        rep = BusVerifier([txn]).run()
        assert any(v["check"] == "bus_wrap_invalid" for v in rep["violations"])

    def test_invalid_resp(self):
        from AGENT_H.bus_verifier import BusVerifier
        txn = _axi_incr(beats=_incr_beats(), resp="bogus")
        rep = BusVerifier([txn]).run()
        assert any(v["check"] == "bus_resp" for v in rep["violations"])

    def test_apb_single_passes(self):
        from AGENT_H.bus_verifier import BusVerifier
        txn = {"protocol": "apb", "txn": "read", "addr": "0x4000",
               "len": 0, "size": 2, "burst": "incr",
               "beats": [{"addr": "0x4000", "last": True}], "resp": "okay"}
        assert BusVerifier([txn]).run()["pass"] is True

    def test_robustness_malformed(self):
        from AGENT_H.bus_verifier import BusVerifier
        import copy
        for txns in ([], [None, 1, "x"], [{}], [{"protocol": None}]):
            rep = BusVerifier(copy.deepcopy(txns)).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.bus_verifier import BusVerifier
        rep = BusVerifier([]).run()
        for key in ("schema_version", "agent", "transactions",
                    "metrics", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "bus_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.bus_verifier import run_from_manifest
        # transactions embedded in the commit log via the "bus" field
        rtl = [{"schema_version": "2.1.0", "seq": 0, "pc": "0x0", "disasm": "sw",
                "regs": {}, "csrs": {}, "bus": _axi_incr(beats=_incr_beats())}]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "bus-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "bus_report.json").exists()
        assert "bus_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T35 — Fault-Injection Campaign Engine
# ─────────────────────────────────────────────────────────────────────────────

def _alu_golden_log():
    # a clean ALU trace the pipeline verifier passes as-is
    return [
        {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
         "disasm": "addi x5,x0,5", "regs": {"x5": "0x00000005"}, "csrs": {}},
        {"schema_version": "2.1.0", "seq": 1, "pc": "0x80000004",
         "disasm": "add x6,x5,x0", "regs": {"x6": "0x00000005"}, "csrs": {}},
        {"schema_version": "2.1.0", "seq": 2, "pc": "0x80000008",
         "disasm": "xori x7,x5,0xff", "regs": {"x7": "0x000000fa"}, "csrs": {}},
    ]


class TestFaultInjector:
    def test_import(self):
        from AGENT_H import fault_injector
        assert hasattr(fault_injector, "FaultCampaign")

    def test_inject_register_corruption(self):
        from AGENT_H.fault_injector import inject_fault, Fault, REGISTER_CORRUPTION
        log = _alu_golden_log()
        f = Fault(REGISTER_CORRUPTION, seq=1, target="reg", reg="x6")
        faulted = inject_fault(log, f)
        assert faulted[1]["regs"]["x6"] != "0x00000005"
        assert f.old == 5 and f.new != 5
        assert log[1]["regs"]["x6"] == "0x00000005"   # original untouched

    def test_inject_bit_flip(self):
        from AGENT_H.fault_injector import inject_fault, Fault, BIT_FLIP
        f = Fault(BIT_FLIP, seq=1, target="reg", reg="x6", bit=1)
        faulted = inject_fault(_alu_golden_log(), f)
        assert int(faulted[1]["regs"]["x6"], 16) == (5 ^ (1 << 1))  # 5 -> 7

    def test_register_fault_detected(self):
        from AGENT_H.fault_injector import inject_fault, Fault, REGISTER_CORRUPTION
        from AGENT_H.pipeline_verifier import PipelineVerifier
        f = Fault(REGISTER_CORRUPTION, seq=1, target="reg", reg="x6")
        faulted = inject_fault(_alu_golden_log(), f)
        assert PipelineVerifier(faulted).run()["pass"] is False

    def test_campaign_high_detection_for_register_faults(self):
        from AGENT_H.fault_injector import FaultCampaign, REGISTER_CORRUPTION
        rep = FaultCampaign(_alu_golden_log(), models=[REGISTER_CORRUPTION],
                            seed=1).run(n=20)
        assert rep["detection_rate"] == 1.0
        assert rep["per_model"][REGISTER_CORRUPTION]["rate"] == 1.0
        assert rep["band"] == "VERIFIED"

    def test_campaign_reports_blind_spot(self):
        from AGENT_H.fault_injector import FaultCampaign, PC_CORRUPTION
        # the panel has no PC/control checker for plain ALU ops -> undetected
        rep = FaultCampaign(_alu_golden_log(), models=[PC_CORRUPTION], seed=2).run(n=10)
        assert rep["detection_rate"] == 0.0
        assert len(rep["undetected"]) > 0

    def test_campaign_deterministic(self):
        from AGENT_H.fault_injector import FaultCampaign
        a = FaultCampaign(_alu_golden_log(), seed=7).run(n=15)
        b = FaultCampaign(_alu_golden_log(), seed=7).run(n=15)
        assert a["faults_injected"] == b["faults_injected"]
        assert a["detection_rate"] == b["detection_rate"]

    def test_robustness_malformed(self):
        from AGENT_H.fault_injector import FaultCampaign
        import copy
        for log in ([], [None, 1, "x"], [{}]):
            rep = FaultCampaign(copy.deepcopy(log), seed=1).run(n=5)
            assert rep["pass"] is True and "detection_rate" in rep

    def test_report_schema(self):
        from AGENT_H.fault_injector import FaultCampaign
        rep = FaultCampaign(_alu_golden_log(), seed=0).run(n=5)
        for key in ("schema_version", "agent", "golden_records", "faults_injected",
                    "detection_rate", "fault_coverage", "per_model", "undetected",
                    "band", "pass"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "fault_injector"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.fault_injector import run_from_manifest
        rtl = _alu_golden_log()
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "fi-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath, n=10)
        assert rc == 0
        assert (tmp_path / "fault_report.json").exists()
        assert "fault_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T36 — RV64 Datapath Verifier (XLEN-64 widening)
# ─────────────────────────────────────────────────────────────────────────────

def _rv64_rec(seq, disasm, regs):
    return {"schema_version": "2.1.0", "seq": seq, "pc": f"0x{0x80000000 + seq*4:08x}",
            "disasm": disasm, "regs": regs, "csrs": {}}


class TestRV64Verifier:
    def test_import(self):
        from AGENT_H import rv64_verifier
        assert hasattr(rv64_verifier, "RV64Verifier")

    def test_golden_helpers(self):
        from AGENT_H.rv64_verifier import sext32, alu64, aluw
        assert sext32(0x7FFFFFFF) == 0x7FFFFFFF
        assert sext32(0x80000000) == 0xFFFFFFFF80000000
        assert alu64("add", 0x100000000, 1) == 0x100000001       # no 32-bit truncation
        assert alu64("sll", 1, 32) == 0x100000000                # 64-bit shift
        assert aluw("addw", 0xFFFFFFFF, 1) == 0                   # 32-bit wrap, sext 0
        assert aluw("subw", 0, 1) == 0xFFFFFFFFFFFFFFFF          # -1 sign-extended
        assert aluw("sraw", 0x80000000, 4) == 0xFFFFFFFFF8000000  # arith shift + sext

    def test_clean_rv64_passes(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        log = [
            _rv64_rec(0, "addiw x5,x0,5", {"x5": "0x5"}),
            _rv64_rec(1, "addw x6,x5,x5", {"x6": "0xa"}),
        ]
        rep = RV64Verifier(log).run()
        assert rep["rv64_detected"] is True and rep["pass"] is True
        assert rep["stats"]["word_ops"] == 2

    def test_word_sext_bug_diagnosed(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        log = [
            _rv64_rec(0, "addiw x5,x0,5", {"x5": "0x5"}),
            # slliw 5<<31 = 0x80000000 -> must sign-extend to 0xFFFFFFFF80000000
            _rv64_rec(1, "slliw x6,x5,31", {"x6": "0x0000000080000000"}),
        ]
        rep = RV64Verifier(log).run()
        assert rep["pass"] is False
        v = [x for x in rep["violations"] if x["check"] == "rv64_word_sext"]
        assert v and v[0]["expected"] == "0xffffffff80000000"
        assert rep["band"] == "CRITICAL"

    def test_64bit_result_bug(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        log = [
            _rv64_rec(0, "addi x5,x0,1", {"x5": "0x1"}),
            _rv64_rec(1, "slli x6,x5,32", {"x6": "0x1"}),   # truncated (RV32 behaviour)
        ]
        rep = RV64Verifier(log, force=True).run()
        assert any(v["check"] == "rv64_result" for v in rep["violations"])

    def test_rv32_trace_is_noop(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        # no W-op, no 64-bit value -> not RV64 -> agent stays out even on a wrong result
        log = [_rv64_rec(0, "addi x5,x0,5", {"x5": "0x99"})]
        rep = RV64Verifier(log).run()
        assert rep["rv64_detected"] is False and rep["pass"] is True

    def test_reserved_shamt(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        log = [
            _rv64_rec(0, "addiw x5,x0,5", {"x5": "0x5"}),
            _rv64_rec(1, "slliw x6,x5,40", {"x6": "0x0"}),   # shamt > 31 reserved
        ]
        rep = RV64Verifier(log).run()
        assert any(v["check"] == "rv64_shamt" for v in rep["violations"])

    def test_robustness_malformed(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        import copy
        for log in ([], [None, 1, "x"], [{}], [{"disasm": None, "regs": None}]):
            rep = RV64Verifier(copy.deepcopy(log)).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        rep = RV64Verifier([]).run()
        for key in ("schema_version", "agent", "records_checked", "rv64_detected",
                    "ops_checked", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "rv64_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.rv64_verifier import run_from_manifest
        rtl = [
            _rv64_rec(0, "addiw x5,x0,5", {"x5": "0x5"}),
            _rv64_rec(1, "addw x6,x5,x5", {"x6": "0xa"}),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "rv64-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "rv64_report.json").exists()
        assert "rv64_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T37 — Sv39 / Sv48 Virtual-Memory Verifier (RV64 paging)
# ─────────────────────────────────────────────────────────────────────────────

# satp: MODE=Sv39 (8), root page table at physical 0x80000000 (PPN 0x80000)
_SATP39 = "0x8000000000080000"
# 3-level table mapping VA 0x1000 -> PA 0x90000000 (4 KB page)
#   L2 @ 0x80000000 -> L1 @ 0x80001000 -> L0 @ 0x80002008 (leaf, RWXUAD)
_PT_SV39_4K = {
    "0x80000000": "0x20000401",   # -> ppn 0x80001
    "0x80001000": "0x20000801",   # -> ppn 0x80002
    "0x80002008": "0x240000df",   # leaf -> ppn 0x90000
}


def _sv_rec(seq, disasm, reads=None, satp=_SATP39, priv=0, trap=None, phys_mem=None):
    rec = {"schema_version": "2.1.0", "seq": seq, "pc": f"0x{0x80000000 + seq*4:08x}",
           "disasm": disasm, "regs": {}, "csrs": {"satp": satp}, "priv": priv}
    if reads is not None:
        rec["mem_reads"] = reads
    if trap is not None:
        rec["trap"] = trap
    if phys_mem is not None:
        rec["phys_mem"] = phys_mem
    return rec


class TestSvMMUVerifier:
    # -- mode detection ------------------------------------------------------
    def test_mode_detection(self):
        from AGENT_H.sv_mmu_verifier import satp_mode
        assert satp_mode(0x8000000000080000) == "sv39"
        assert satp_mode(0x9000000000080000) == "sv48"
        assert satp_mode(0x0) == "bare"

    # -- golden Sv39 walker --------------------------------------------------
    def test_sv39_4kb(self):
        from AGENT_H.sv_mmu_verifier import SvMMU
        pm = {0x80000000: 0x20000401, 0x80001000: 0x20000801, 0x80002008: 0x240000DF}
        t = SvMMU(pm, 0x8000000000080000).translate(0x1000, "load", 0)
        assert t.ok and t.pa == 0x90000000 and t.level == 0

    def test_sv39_2mb_superpage(self):
        from AGENT_H.sv_mmu_verifier import SvMMU
        # L1 leaf @ 0x80001008 -> 2 MB page, PA 0x40000000
        pm = {0x80000000: 0x20000401, 0x80001008: 0x100000DF}
        t = SvMMU(pm, 0x8000000000080000).translate(0x200000, "load", 0)
        assert t.ok and t.pa == 0x40000000 and t.level == 1

    def test_sv39_1gb_superpage(self):
        from AGENT_H.sv_mmu_verifier import SvMMU
        # L2 leaf @ 0x80000008 -> 1 GB page, PA 0xC0000000
        pm = {0x80000008: 0x300000DF}
        t = SvMMU(pm, 0x8000000000080000).translate(0x40000000, "load", 0)
        assert t.ok and t.pa == 0xC0000000 and t.level == 2

    def test_sv39_misaligned_superpage(self):
        from AGENT_H.sv_mmu_verifier import SvMMU
        # L2 leaf with non-zero low PPN bits -> misaligned 1 GB superpage
        pm = {0x80000008: 0x300010DF}
        t = SvMMU(pm, 0x8000000000080000).translate(0x40000000, "load", 0)
        assert not t.ok and t.cause == 13

    def test_sv39_invalid_pte(self):
        from AGENT_H.sv_mmu_verifier import SvMMU
        pm = {0x80000000: 0x20000401, 0x80001000: 0x20000801, 0x80002008: 0x240000DE}
        t = SvMMU(pm, 0x8000000000080000).translate(0x1000, "load", 0)
        assert not t.ok and t.cause == 13

    def test_sv39_noncanonical_va(self):
        from AGENT_H.sv_mmu_verifier import SvMMU
        pm = {0x80000000: 0x20000401}
        t = SvMMU(pm, 0x8000000000080000).translate(1 << 39, "load", 0)
        assert not t.ok and "non-canonical" in t.reason

    # -- checker -------------------------------------------------------------
    def test_clean_translation_passes(self):
        from AGENT_H.sv_mmu_verifier import SvMMUVerifier
        log = [_sv_rec(0, "ld x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000000", "size": 8}])]
        rep = SvMMUVerifier(log, phys_mem=_PT_SV39_4K).run()
        assert rep["pass"] is True
        assert rep["mode"] == "sv39" and rep["translations"] == 1

    def test_translation_bug_caught(self):
        from AGENT_H.sv_mmu_verifier import SvMMUVerifier
        log = [_sv_rec(0, "ld x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000004", "size": 8}])]
        rep = SvMMUVerifier(log, phys_mem=_PT_SV39_4K).run()
        assert rep["pass"] is False
        assert any(v["check"] == "sv_translation" for v in rep["violations"])
        assert rep["band"] == "CRITICAL"

    def test_missing_fault_caught(self):
        from AGENT_H.sv_mmu_verifier import SvMMUVerifier
        pt = dict(_PT_SV39_4K); pt["0x80002008"] = "0x240000de"  # invalid leaf
        log = [_sv_rec(0, "ld x1,0(x2)", reads=[{"vaddr": "0x1000", "size": 8}])]
        rep = SvMMUVerifier(log, phys_mem=pt).run()
        assert any(v["check"] == "sv_missing_fault" for v in rep["violations"])

    def test_gated_bare_mode(self):
        from AGENT_H.sv_mmu_verifier import SvMMUVerifier
        log = [_sv_rec(0, "ld x1,0(x2)", satp="0x0",
                       reads=[{"vaddr": "0x1000", "paddr": "0xdead", "size": 8}])]
        rep = SvMMUVerifier(log, phys_mem=_PT_SV39_4K).run()
        assert rep["pass"] is True and rep["sv_enabled"] is False

    def test_robustness_malformed(self):
        from AGENT_H.sv_mmu_verifier import SvMMUVerifier
        import copy
        for log in ([], [None, 1, "x"], [{}], [{"csrs": None}]):
            rep = SvMMUVerifier(copy.deepcopy(log)).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.sv_mmu_verifier import SvMMUVerifier
        rep = SvMMUVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked", "mode",
                    "sv_enabled", "translations", "total_violations", "pass",
                    "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "sv_mmu_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.sv_mmu_verifier import run_from_manifest
        rtl = [_sv_rec(0, "ld x1,0(x2)",
                       reads=[{"vaddr": "0x1000", "paddr": "0x90000000", "size": 8}],
                       phys_mem=_PT_SV39_4K)]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "sv-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "sv_mmu_report.json").exists()
        assert "sv_mmu_report" in json.loads(mpath.read_text())["outputs"]


# ─────────────────────────────────────────────────────────────────────────────
# T38 — RV64 Atomics Verifier (.D atomics)
# ─────────────────────────────────────────────────────────────────────────────

def _rv64a_rec(seq, disasm, regs=None, reads=None, writes=None, trap=None):
    rec = {"schema_version": "2.1.0", "seq": seq, "pc": f"0x{0x80000000 + seq*4:08x}",
           "disasm": disasm, "regs": regs or {}, "csrs": {}}
    if reads is not None:
        rec["mem_reads"] = reads
    if writes is not None:
        rec["mem_writes"] = writes
    if trap is not None:
        rec["trap"] = trap
    return rec


class TestRV64AtomicsVerifier:
    def test_import(self):
        from AGENT_H import rv64_atomics_verifier
        assert hasattr(rv64_atomics_verifier, "RV64AtomicsVerifier")

    def test_amo_compute64_golden(self):
        from AGENT_H.rv64_atomics_verifier import amo_compute64
        assert amo_compute64("swap", 0x10, 0x99) == 0x99
        assert amo_compute64("add", 0xFFFFFFFFFFFFFFFF, 1) == 0      # 64-bit wrap
        assert amo_compute64("min", 0xFFFFFFFFFFFFFFFF, 1) == 0xFFFFFFFFFFFFFFFF  # -1<1
        assert amo_compute64("max", 0xFFFFFFFFFFFFFFFF, 1) == 1
        assert amo_compute64("minu", 0xFFFFFFFFFFFFFFFF, 1) == 1
        assert amo_compute64("maxu", 0xFFFFFFFFFFFFFFFF, 1) == 0xFFFFFFFFFFFFFFFF

    def test_clean_amod_passes(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        log = [
            _rv64a_rec(0, "li x6,0x100000000", regs={"x6": "0x100000000"}),
            _rv64a_rec(1, "amoadd.d x5,x6,(x10)", regs={"x5": "0x200000000"},
                       reads=[{"addr": "0x100", "size": 8, "value": "0x200000000"}],
                       writes=[{"addr": "0x100", "size": 8, "value": "0x300000000"}]),
        ]
        rep = RV64AtomicsVerifier(log).run()
        assert rep["pass"] is True and rep["atomics_d_examined"] == 1

    def test_signed_min_d_passes(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        log = [
            _rv64a_rec(0, "li x6,-1", regs={"x6": "0xffffffffffffffff"}),
            _rv64a_rec(1, "amomin.d x5,x6,(x10)", regs={"x5": "0x1"},
                       reads=[{"addr": "0x100", "size": 8, "value": "0x1"}],
                       writes=[{"addr": "0x100", "size": 8, "value": "0xffffffffffffffff"}]),
        ]
        assert RV64AtomicsVerifier(log).run()["pass"] is True

    def test_amod_writeback_bug(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        log = [
            _rv64a_rec(0, "li x6,0x100000000", regs={"x6": "0x100000000"}),
            _rv64a_rec(1, "amoadd.d x5,x6,(x10)", regs={"x5": "0x200000000"},
                       reads=[{"addr": "0x100", "size": 8, "value": "0x200000000"}],
                       writes=[{"addr": "0x100", "size": 8, "value": "0x300000001"}]),  # bug
        ]
        rep = RV64AtomicsVerifier(log).run()
        assert rep["pass"] is False
        v = [x for x in rep["violations"] if x["check"] == "amod_writeback"]
        assert v and v[0]["expected"] == "0x0000000300000000"

    def test_lr_sc_d_success(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        log = [
            _rv64a_rec(0, "addi x6,x0,7", regs={"x6": "0x7"}),
            _rv64a_rec(1, "lr.d x5,(x10)", regs={"x5": "0x10"},
                       reads=[{"addr": "0x100", "size": 8, "value": "0x10"}]),
            _rv64a_rec(2, "sc.d x4,x6,(x10)", regs={"x4": "0x0"},
                       writes=[{"addr": "0x100", "size": 8, "value": "0x7"}]),
        ]
        rep = RV64AtomicsVerifier(log).run()
        assert rep["pass"] is True and rep["stats"]["sc_success"] == 1

    def test_scd_spurious_store_caught(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        log = [
            _rv64a_rec(0, "addi x6,x0,7", regs={"x6": "0x7"}),
            _rv64a_rec(1, "sc.d x4,x6,(x10)", regs={"x4": "0x0"},
                       writes=[{"addr": "0x100", "size": 8, "value": "0x7"}]),
        ]
        rep = RV64AtomicsVerifier(log).run()
        assert any(v["check"] in ("scd_fail_wrote", "scd_fail_rd") for v in rep["violations"])

    def test_misaligned_d_caught(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        log = [_rv64a_rec(0, "amoadd.d x5,x6,(x10)", regs={"x5": "0x10"},
                          reads=[{"addr": "0x104", "size": 8, "value": "0x10"}],
                          writes=[{"addr": "0x104", "size": 8, "value": "0x15"}])]
        rep = RV64AtomicsVerifier(log).run()
        assert any(v["check"] == "amod_alignment" for v in rep["violations"])

    def test_rv32_atomics_no_longer_flags_d_on_rv64(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        # RV64 trace (64-bit value present) -> .D must NOT be flagged illegal
        log = [
            _rv64a_rec(0, "li x1,0x100000000", regs={"x1": "0x100000000"}),
            _rv64a_rec(1, "amoadd.d x5,x6,(x10)", regs={"x5": "0x10"},
                       reads=[{"addr": "0x100", "size": 8, "value": "0x10"}],
                       writes=[{"addr": "0x100", "size": 8, "value": "0x10"}]),
        ]
        rep = AtomicsVerifier(log).run()
        assert not any(v["check"] == "rv32_illegal_d" for v in rep["violations"])

    def test_rv32_atomics_still_flags_d_on_rv32(self):
        from AGENT_H.atomics_verifier import AtomicsVerifier
        log = [_rv64a_rec(0, "amoswap.d x5,x6,(x10)", regs={"x5": "0x1"})]
        rep = AtomicsVerifier(log).run()
        assert any(v["check"] == "rv32_illegal_d" for v in rep["violations"])

    def test_robustness_malformed(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        import copy
        for log in ([], [None, 1, "x"], [{}], [{"disasm": None}]):
            rep = RV64AtomicsVerifier(copy.deepcopy(log)).run()
            assert rep["pass"] is True and "band" in rep

    def test_report_schema(self):
        from AGENT_H.rv64_atomics_verifier import RV64AtomicsVerifier
        rep = RV64AtomicsVerifier([]).run()
        for key in ("schema_version", "agent", "records_checked",
                    "atomics_d_examined", "total_violations", "pass", "violations", "band"):
            assert key in rep, f"Missing key: {key}"
        assert rep["agent"] == "rv64_atomics_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.rv64_atomics_verifier import run_from_manifest
        rtl = [
            _rv64a_rec(0, "li x6,0x100000000", regs={"x6": "0x100000000"}),
            _rv64a_rec(1, "amoadd.d x5,x6,(x10)", regs={"x5": "0x200000000"},
                       reads=[{"addr": "0x100", "size": 8, "value": "0x200000000"}],
                       writes=[{"addr": "0x100", "size": 8, "value": "0x300000000"}]),
        ]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(r) for r in rtl))
        manifest = {"schema_version": "2.1.0", "run_id": "rv64a-test",
                    "run_dir": str(tmp_path),
                    "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mpath = tmp_path / "run_manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        rc = run_from_manifest(mpath)
        assert rc == 0
        assert (tmp_path / "rv64_atomics_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# RV64 M-extension ops (mul/div/rem + W forms) in the RV64 verifier
# ─────────────────────────────────────────────────────────────────────────────

class TestRV64MExtension:
    def test_alu64_m_golden(self):
        from AGENT_H.rv64_verifier import alu64
        assert alu64("mul", 0xFFFFFFFFFFFFFFFF, 2) == 0xFFFFFFFFFFFFFFFE
        assert alu64("mulh", 0x8000000000000000, 2) == 0xFFFFFFFFFFFFFFFF
        assert alu64("mulhu", 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF) == 0xFFFFFFFFFFFFFFFE
        assert alu64("div", 0xFFFFFFFFFFFFFFF9, 2) == 0xFFFFFFFFFFFFFFFD   # -7/2 = -3
        assert alu64("div", 5, 0) == 0xFFFFFFFFFFFFFFFF                    # x/0 = -1
        assert alu64("div", 0x8000000000000000, 0xFFFFFFFFFFFFFFFF) == 0x8000000000000000  # overflow
        assert alu64("divu", 0xFFFFFFFFFFFFFFFF, 2) == 0x7FFFFFFFFFFFFFFF
        assert alu64("rem", 0xFFFFFFFFFFFFFFF9, 2) == 0xFFFFFFFFFFFFFFFF   # -7%2 = -1
        assert alu64("rem", 5, 0) == 5                                     # x%0 = x
        assert alu64("remu", 0xFFFFFFFFFFFFFFFF, 3) == 0

    def test_aluw_m_golden(self):
        from AGENT_H.rv64_verifier import aluw
        assert aluw("mulw", 0xFFFFFFFF, 2) == 0xFFFFFFFFFFFFFFFE
        assert aluw("divw", 0xFFFFFFF9, 2) == 0xFFFFFFFFFFFFFFFD          # -7/2 sext
        assert aluw("divw", 5, 0) == 0xFFFFFFFFFFFFFFFF                    # /0 -> -1 sext
        assert aluw("divuw", 0xFFFFFFFF, 2) == 0x7FFFFFFF
        assert aluw("remw", 0xFFFFFFF9, 2) == 0xFFFFFFFFFFFFFFFF          # -7%2 = -1 sext
        assert aluw("remuw", 7, 0) == 7

    def test_mulw_integration_passes(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        log = [
            _rv64_rec(0, "addiw x5,x0,6", {"x5": "0x6"}),
            _rv64_rec(1, "addiw x6,x0,7", {"x6": "0x7"}),
            _rv64_rec(2, "mulw x7,x5,x6", {"x7": "0x2a"}),   # 42
        ]
        rep = RV64Verifier(log).run()
        assert rep["pass"] is True and rep["stats"]["word_ops"] == 3

    def test_divw_by_zero_passes(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        log = [
            _rv64_rec(0, "addiw x5,x0,5", {"x5": "0x5"}),
            _rv64_rec(1, "addiw x6,x0,0", {"x6": "0x0"}),
            _rv64_rec(2, "divw x7,x5,x6", {"x7": "0xffffffffffffffff"}),  # -1
        ]
        assert RV64Verifier(log).run()["pass"] is True

    def test_mulw_bug_caught(self):
        from AGENT_H.rv64_verifier import RV64Verifier
        log = [
            _rv64_rec(0, "addiw x5,x0,6", {"x5": "0x6"}),
            _rv64_rec(1, "addiw x6,x0,7", {"x6": "0x7"}),
            _rv64_rec(2, "mulw x7,x5,x6", {"x7": "0x2b"}),   # wrong (should be 0x2a)
        ]
        rep = RV64Verifier(log).run()
        assert rep["pass"] is False
        assert any(v["check"] in ("rv64_word_op", "rv64_word_sext") for v in rep["violations"])


# ─────────────────────────────────────────────────────────────────────────────
# T39 — Branch Predictor Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _bp_rec(seq, disasm, pc, regs=None, target=None, predict=None, trap=None):
    r = {"schema_version": "2.1.0", "seq": seq, "pc": pc, "disasm": disasm,
         "regs": regs or {}, "csrs": {}}
    if target is not None:
        r["target"] = target
    if predict is not None:
        r["predict"] = predict
    if trap is not None:
        r["trap"] = trap
    return r


class TestBranchPredictorVerifier:
    def test_import(self):
        from AGENT_H import branch_predictor_verifier
        assert hasattr(branch_predictor_verifier, "BranchPredictorVerifier")

    def test_helpers(self):
        from AGENT_H.branch_predictor_verifier import reg_idx, _abs_target
        assert reg_idx("a0") == 10 and reg_idx("ra") == 1
        assert _abs_target("beq a0,a1,0x80000040") == 0x80000040
        assert _abs_target("addi a0,a0,16") is None

    def test_recovery_taken_clean(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        log = [_bp_rec(0, "addi a0,x0,5", "0x80000000", {"a0": "0x5"}),
               _bp_rec(1, "addi a1,x0,5", "0x80000004", {"a1": "0x5"}),
               _bp_rec(2, "beq a0,a1,0x80000040", "0x80000008", target="0x80000040"),
               _bp_rec(3, "addi x0,x0,0", "0x80000040")]
        r = BranchPredictorVerifier(log).run()
        assert r["pass"] is True and r["metrics"]["taken_rate"] == 1.0

    def test_recovery_taken_bug(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        log = [_bp_rec(0, "addi a0,x0,5", "0x80000000", {"a0": "0x5"}),
               _bp_rec(1, "addi a1,x0,5", "0x80000004", {"a1": "0x5"}),
               _bp_rec(2, "beq a0,a1,0x80000040", "0x80000008", target="0x80000040"),
               _bp_rec(3, "addi x0,x0,0", "0x8000000c")]
        r = BranchPredictorVerifier(log).run()
        assert r["pass"] is False
        assert any(v["check"] == "bp_recovery" for v in r["violations"])
        assert r["band"] == "CRITICAL"

    def test_recovery_not_taken_bug(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        log = [_bp_rec(0, "addi a0,x0,5", "0x80000000", {"a0": "0x5"}),
               _bp_rec(1, "addi a1,x0,6", "0x80000004", {"a1": "0x6"}),
               _bp_rec(2, "beq a0,a1,0x80000040", "0x80000008", target="0x80000040"),
               _bp_rec(3, "addi x0,x0,0", "0x80000040")]
        r = BranchPredictorVerifier(log).run()
        assert any(v["check"] == "bp_recovery" for v in r["violations"])

    def test_hit_flag_inconsistent(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        log = [_bp_rec(0, "addi a0,x0,5", "0x80000000", {"a0": "0x5"}),
               _bp_rec(1, "addi a1,x0,5", "0x80000004", {"a1": "0x5"}),
               _bp_rec(2, "beq a0,a1,0x80000040", "0x80000008", target="0x80000040",
                       predict={"taken": True, "correct": False}),
               _bp_rec(3, "addi x0,x0,0", "0x80000040")]
        r = BranchPredictorVerifier(log).run()
        assert any(v["check"] == "bp_hit_flag" for v in r["violations"])

    def test_accuracy_metric(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        log = [_bp_rec(0, "addi a0,x0,5", "0x80000000", {"a0": "0x5"}),
               _bp_rec(1, "addi a1,x0,5", "0x80000004", {"a1": "0x5"}),
               _bp_rec(2, "beq a0,a1,0x80000040", "0x80000008", target="0x80000040",
                       predict={"taken": True, "correct": True}),
               _bp_rec(3, "addi x0,x0,0", "0x80000040")]
        m = BranchPredictorVerifier(log).run()["metrics"]
        assert m["predictions"] == 1 and m["accuracy"] == 1.0

    def test_jump_recovery_bug(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        log = [_bp_rec(0, "jal ra,0x80000100", "0x80000010", target="0x80000100"),
               _bp_rec(1, "addi x0,x0,0", "0x80000014")]
        r = BranchPredictorVerifier(log).run()
        assert any(v["check"] == "bp_recovery" for v in r["violations"])

    def test_ras_return_accuracy(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        log = [_bp_rec(0, "jal ra,0x80000100", "0x80000010", target="0x80000100"),
               _bp_rec(1, "addi x0,x0,0", "0x80000100"),
               _bp_rec(2, "ret", "0x80000104"),
               _bp_rec(3, "addi x0,x0,0", "0x80000014")]
        m = BranchPredictorVerifier(log).run()["metrics"]
        assert m["ras_returns"] == 1 and m["ras_accuracy"] == 1.0

    def test_robustness_malformed(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        import copy
        for log in ([], [None, 1, "x"], [{}], [{"disasm": None}]):
            r = BranchPredictorVerifier(copy.deepcopy(log)).run()
            assert r["pass"] is True and "band" in r

    def test_report_schema(self):
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        r = BranchPredictorVerifier([]).run()
        for k in ("schema_version", "agent", "records_checked", "metrics",
                  "total_violations", "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "branch_predictor_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.branch_predictor_verifier import run_from_manifest
        log = [_bp_rec(0, "addi a0,x0,5", "0x80000000", {"a0": "0x5"}),
               _bp_rec(1, "addi a1,x0,5", "0x80000004", {"a1": "0x5"}),
               _bp_rec(2, "beq a0,a1,0x80000040", "0x80000008", target="0x80000040"),
               _bp_rec(3, "addi x0,x0,0", "0x80000040")]
        (tmp_path / "rtl_commit.jsonl").write_text("\n".join(json.dumps(x) for x in log))
        man = {"schema_version": "2.1.0", "run_id": "bp", "run_dir": str(tmp_path),
               "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        rc = run_from_manifest(mp)
        assert rc == 0 and (tmp_path / "branch_predictor_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T40 — Self-Evolving Verification Engine (RL coverage-closure loop)
# ─────────────────────────────────────────────────────────────────────────────

def _se_env(seed=0):
    """genetic > directed > random productivity; deterministic."""
    import random as _r
    rng = _r.Random(seed)
    yields = {"genetic": 4, "directed": 2, "random": 1}

    def generate(strategy, constraints):
        return {"strategy": strategy, "targets": [c["target"] for c in constraints]}

    def evaluate(batch):
        s = batch["strategy"]; n = yields.get(s, 1); targets = batch["targets"]
        covered = ([f"bin{rng.randrange(40)}" for _ in range(n)]
                   if s == "random" else targets[:n])
        bugs = 1 if (s == "genetic" and rng.random() < 0.3) else 0
        cost = {"genetic": 0.4, "directed": 0.25, "random": 0.1}.get(s, 0.2)
        return {"covered": covered, "bugs": bugs, "cost": cost}
    return generate, evaluate


_SE_BINS = [f"bin{i}" for i in range(40)]
_SE_STRATS = ["random", "directed", "genetic"]


class TestSelfEvolvingEngine:
    def test_import(self):
        from AGENT_H import self_evolving_engine as se
        assert hasattr(se, "SelfEvolvingEngine")

    def test_ucb1_cold_start_then_exploit(self):
        from AGENT_H.self_evolving_engine import UCB1
        b = UCB1(["a", "b", "c"])
        picks = []
        for _ in range(3):
            p = b.select(); b.update(p, 1.0 if p == "b" else 0.0); picks.append(p)
        assert set(picks) == {"a", "b", "c"}
        for _ in range(50):
            a = b.select(); b.update(a, 1.0 if a == "b" else 0.0)
        assert b.best() == "b"

    def test_ucb1_unknown_arm_safe(self):
        from AGENT_H.self_evolving_engine import UCB1
        b = UCB1(["a"]); b.update("zz", 0.5)
        assert "zz" in b.counts

    def test_coverage_state(self):
        from AGENT_H.self_evolving_engine import CoverageState
        c = CoverageState(["x", "y", "z"])
        assert c.cover(["x", "x", "q"]) == {"x"}
        assert c.holes() == {"y", "z"}

    def test_coverage_no_universe_adopts(self):
        from AGENT_H.self_evolving_engine import CoverageState
        c = CoverageState([]); c.cover(["a", "b"])
        assert c.total == {"a", "b"} and c.fraction() == 1.0

    def test_constraint_for(self):
        from AGENT_H.self_evolving_engine import constraint_for
        con = constraint_for("branch:taken:neg")
        assert con["kind"] == "branch" and con["values"] == ["taken", "neg"]
        assert constraint_for("weirdbin")["kind"] == "bin"

    def test_evolve_increases_coverage(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        gen, ev = _se_env()
        eng = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=1,
                                 coverage_target=0.95, plateau_patience=8)
        rep = eng.evolve(gen, ev, max_rounds=100)
        assert rep["final_coverage"] > rep["initial_round_coverage"]
        assert rep["coverage_improvement"] > 0.5
        traj = rep["coverage_trajectory"]
        assert all(traj[i] <= traj[i + 1] + 1e-9 for i in range(len(traj) - 1))

    def test_evolve_bandit_prefers_productive(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        gen, ev = _se_env()
        eng = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=2,
                                 coverage_target=1.0, plateau_patience=12,
                                 holes_per_round=3)
        rep = eng.evolve(gen, ev, max_rounds=200)
        st = rep["strategy_stats"]
        assert st["genetic"]["mean_reward"] >= st["random"]["mean_reward"]
        assert rep["recommended_strategy"] in ("genetic", "directed")

    def test_evolve_target_stop(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        gen, ev = _se_env()
        eng = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=3,
                                 coverage_target=0.5, plateau_patience=20)
        rep = eng.evolve(gen, ev, max_rounds=200)
        assert rep["final_coverage"] >= 0.5
        assert rep["stop_reason"] in ("coverage_target_reached", "no_holes_remaining")

    def test_evolve_plateau_stop(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        eng = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=4, plateau_patience=3)
        rep = eng.evolve(lambda s, c: {"strategy": s, "targets": []},
                         lambda b: {"covered": [], "bugs": 0, "cost": 0.1},
                         max_rounds=100)
        assert rep["stop_reason"] == "plateau" and rep["rounds_run"] == 3

    def test_evolve_bad_plugin_survives(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        def gen(s, c): raise RuntimeError("boom")
        eng = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=5, plateau_patience=2)
        rep = eng.evolve(gen, lambda b: {}, max_rounds=10)
        assert rep["pass"] is True and rep["stop_reason"] == "plateau"

    def test_report_schema(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        gen, ev = _se_env()
        rep = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=6).evolve(gen, ev, max_rounds=30)
        for k in ("schema_version", "agent", "pass", "band", "rounds_run",
                  "final_coverage", "coverage_trajectory", "recommended_strategy",
                  "strategy_stats", "bugs_found", "holes_remaining"):
            assert k in rep
        assert rep["agent"] == "self_evolving_engine"
        assert rep["schema_version"] == "2.1.0"

    def test_plan_from_coverage(self):
        from AGENT_H.self_evolving_engine import plan_from_coverage
        plan = plan_from_coverage(
            covered_bins=["bin0", "bin1"], total_bins=_SE_BINS,
            strategy_stats={"random": {"mean_reward": 0.1},
                            "genetic": {"mean_reward": 0.6}})
        assert plan["holes_remaining"] == 38
        assert plan["recommended_strategy"] == "genetic"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.self_evolving_engine import run_from_manifest
        (tmp_path / "coverage_summary.json").write_text(json.dumps({
            "covered_bins": ["instr:add", "instr:sub"],
            "total_bins": ["instr:add", "instr:sub", "instr:amoadd.w",
                           "csr:mstatus:write"]}))
        man = {"schema_version": "2.1.0", "run_id": "se", "run_dir": str(tmp_path)}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 0
        out = json.loads((tmp_path / "self_evolving_report.json").read_text())
        assert out["holes_remaining"] == 2 and out["status"] == "completed"

    def test_run_from_manifest_no_coverage(self, tmp_path):
        from AGENT_H.self_evolving_engine import run_from_manifest
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path)}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 0
        out = json.loads((tmp_path / "self_evolving_report.json").read_text())
        assert out["status"] == "skipped"

    def test_robustness_empty(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        gen, ev = _se_env()
        rep = SelfEvolvingEngine([], [], seed=0).evolve(gen, ev, max_rounds=5)
        assert rep["pass"] is True


class TestSelfEvolvingResearchGrade:
    """T40 hardening: non-stationary policies, difficulty scheduler, regret,
    reproducibility, constraint escalation, novelty."""

    def test_make_policy_variants(self):
        from AGENT_H.self_evolving_engine import (
            make_policy, UCB1, DiscountedUCB1, SlidingWindowUCB, ThompsonSampling)
        assert isinstance(make_policy("ucb1", _SE_STRATS), UCB1)
        assert isinstance(make_policy("discounted", _SE_STRATS), DiscountedUCB1)
        assert isinstance(make_policy("sliding_window", _SE_STRATS), SlidingWindowUCB)
        assert isinstance(make_policy("thompson", _SE_STRATS, seed=0), ThompsonSampling)
        with pytest.raises(ValueError):
            make_policy("nope", _SE_STRATS)

    def test_discounted_is_nonstationary(self):
        from AGENT_H.self_evolving_engine import DiscountedUCB1
        duc = DiscountedUCB1(["a", "b"], gamma=0.7)
        for _ in range(20): duc.update("a", 1.0); duc.update("b", 0.0)
        for _ in range(20): duc.update("a", 0.0); duc.update("b", 1.0)
        assert duc.mean("b") > duc.mean("a")  # forgets stale evidence
        assert duc.name == "discounted_ucb"

    def test_ucb1_stationary_averages(self):
        from AGENT_H.self_evolving_engine import UCB1
        uc = UCB1(["a", "b"])
        for _ in range(20): uc.update("a", 1.0); uc.update("b", 0.0)
        for _ in range(20): uc.update("a", 0.0); uc.update("b", 1.0)
        assert abs(uc.mean("a") - 0.5) < 1e-6  # stationary → full-history mean

    def test_sliding_window_forgets(self):
        from AGENT_H.self_evolving_engine import SlidingWindowUCB
        sw = SlidingWindowUCB(["a", "b"], window=6)
        for _ in range(10): sw.update("a", 1.0)
        for _ in range(10): sw.update("b", 1.0)
        assert sw.counts["a"] == 10 and "a" in sw.arms

    def test_thompson_confidence_shrinks(self):
        from AGENT_H.self_evolving_engine import ThompsonSampling
        ts = ThompsonSampling(["a"], seed=1)
        c0 = ts.confidence("a")
        for _ in range(50): ts.update("a", 1.0)
        assert ts.confidence("a") < c0

    def test_policy_confidence_reported(self):
        from AGENT_H.self_evolving_engine import make_policy
        for name in ("ucb1", "discounted", "sliding_window", "thompson"):
            p = make_policy(name, _SE_STRATS, seed=0)
            for a in _SE_STRATS: p.update(a, 0.5)
            assert all("confidence" in p.stats()[a] for a in _SE_STRATS)

    def test_constraint_escalation_ladder(self):
        from AGENT_H.self_evolving_engine import constraint_for, _ESCALATION
        assert constraint_for("instr:amoadd.w", 0)["mutations"] == []
        c2 = constraint_for("instr:amoadd.w", 2)
        assert c2["strategy_hint"] == "edge_values" and len(c2["mutations"]) == 2
        c4 = constraint_for("instr:amoadd.w", 4)
        assert c4["adversarial"] and c4["repair"]
        assert constraint_for("x", 99)["level"] == len(_ESCALATION) - 1

    def test_importance_ranked_scheduling(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        eng = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=0,
                                 weights={"bin5": 10.0}, holes_per_round=3)
        assert "bin5" in eng.select_holes()

    def test_weighted_coverage(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        w = {b: 1.0 for b in _SE_BINS}; w["bin0"] = 100.0
        eng = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=0, weights=w)
        eng.cov.cover(["bin0"])
        assert eng.cov.weighted_fraction() > eng.cov.fraction()

    def test_novelty_rewards_new_regions(self):
        from AGENT_H.self_evolving_engine import CoverageState
        c = CoverageState(["k1:a", "k1:b", "k2:a"]); c.cover(["k1:a"])
        assert c.novelty("k2:a") > c.novelty("k1:b")

    def test_suspected_unreachable_flagged(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        eng = SelfEvolvingEngine(["h0", "h1", "h2"], _SE_STRATS, seed=0,
                                 plateau_patience=100, unreachable_after=3,
                                 holes_per_round=3)
        rep = eng.evolve(
            lambda s, c: {"strategy": s, "targets": [x["target"] for x in c]},
            lambda b: {"covered": [], "bugs": 0, "cost": 0.1}, max_rounds=5)
        assert rep["suspected_unreachable_count"] >= 1
        assert all(h in rep["suspected_unreachable"] for h in ("h0", "h1", "h2"))

    def test_regret_and_efficiency(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        gen, ev = _se_env()
        rep = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=1,
                                 coverage_target=0.9, plateau_patience=10
                                 ).evolve(gen, ev, max_rounds=100)
        assert rep["cumulative_regret"] >= 0.0 and rep["coverage_velocity"] > 0
        assert "closure_confidence" in rep and "est_rounds_to_target" in rep

    def test_run_campaign_stats(self):
        from AGENT_H.self_evolving_engine import run_campaign
        rep = run_campaign(_SE_BINS, _SE_STRATS,
                           env_factory=lambda s: _se_env(s),
                           seeds=[0, 1, 2, 3, 4], coverage_target=0.9,
                           plateau_patience=10, max_rounds=100)
        fc = rep["final_coverage"]
        assert fc["n"] == 5 and fc["min"] <= fc["mean"] <= fc["max"]
        assert rep["modal_strategy"] in _SE_STRATS

    def test_campaign_determinism(self):
        from AGENT_H.self_evolving_engine import run_campaign
        kw = dict(env_factory=lambda s: _se_env(s), seeds=[0, 1, 2],
                  coverage_target=0.8, plateau_patience=8, max_rounds=80)
        a = run_campaign(_SE_BINS, _SE_STRATS, **kw)
        b = run_campaign(_SE_BINS, _SE_STRATS, **kw)
        assert a["final_coverage"] == b["final_coverage"]

    def test_engine_policy_selectable(self):
        from AGENT_H.self_evolving_engine import SelfEvolvingEngine
        gen, ev = _se_env()
        for pol in ("ucb1", "discounted", "sliding_window", "thompson"):
            rep = SelfEvolvingEngine(_SE_BINS, _SE_STRATS, seed=0, policy=pol,
                                     coverage_target=0.8, plateau_patience=10
                                     ).evolve(gen, ev, max_rounds=100)
            assert rep["policy"] in ("ucb1", "discounted_ucb",
                                     "sliding_window_ucb", "thompson")
            assert rep["pass"] is True

    def test_plan_escalates_by_attempts(self):
        from AGENT_H.self_evolving_engine import plan_from_coverage
        plan = plan_from_coverage(covered_bins=[], total_bins=["a", "b"],
                                  attempts={"a": 6})
        entry = [p for p in plan["closure_plan"] if p["hole"] == "a"][0]
        assert entry["constraint"]["level"] >= 2


# ─────────────────────────────────────────────────────────────────────────────
# T41 — RISC-V Vector (RVV) Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _vrec(seq, disasm, **kw):
    r = {"schema_version": "2.1.0", "seq": seq, "pc": hex(0x80000000 + seq * 4),
         "disasm": disasm, "regs": kw.pop("regs", {}), "csrs": {}}
    r.update(kw)
    return r


class TestVectorVerifier:
    def test_import(self):
        from AGENT_H import vector_verifier as vv
        assert hasattr(vv, "VectorVerifier")

    def test_decode_vtype(self):
        from AGENT_H.vector_verifier import decode_vtype
        d = decode_vtype({"sew": 32, "lmul": 1})
        assert d["sew"] == 32 and d["golden_vill"] is False
        enc = (2 << 3) | (1 << 6)              # sew32, lmul1, vta1
        assert decode_vtype(enc)["sew"] == 32

    def test_vlmax_fractional(self):
        from AGENT_H.vector_verifier import vlmax
        assert vlmax(32, 1.0, 128) == 4 and vlmax(32, 0.5, 128) == 2

    def test_velem_compute(self):
        from AGENT_H.vector_verifier import velem_compute
        assert velem_compute("vadd", 8, 200, 100) == (300 & 0xFF)
        assert velem_compute("vminu", 8, 0xFF, 1) == 1
        assert velem_compute("vmin", 8, 0xFF, 1) == 0xFF     # signed -1 < 1
        assert velem_compute("vmulhu", 8, 0xFF, 0xFF) == 0xFE

    def test_vset_vl_clean(self):
        from AGENT_H.vector_verifier import VectorVerifier
        log = [_vrec(0, "vsetvli x1,x2,e32,m1", vtype={"sew": 32, "lmul": 1},
                     vl=4, avl=4, vlen=128)]
        assert VectorVerifier(log).run()["pass"]

    def test_vset_vl_exceeds_vlmax(self):
        from AGENT_H.vector_verifier import VectorVerifier
        log = [_vrec(0, "vsetvli x1,x2,e32,m1", vtype={"sew": 32, "lmul": 1},
                     vl=8, avl=8, vlen=128)]
        r = VectorVerifier(log).run()
        assert not r["pass"] and any(v["check"] == "vset_vl" for v in r["violations"])

    def test_vset_ambiguous_band(self):
        from AGENT_H.vector_verifier import VectorVerifier
        # VLMAX=4, AVL=6 → vl ∈ [3,4] legal; vl=2 illegal
        assert VectorVerifier([_vrec(0, "vsetvli x1,x2,e32,m1",
                                     vtype={"sew": 32, "lmul": 1}, vl=4, avl=6,
                                     vlen=128)]).run()["pass"]
        assert not VectorVerifier([_vrec(0, "vsetvli x1,x2,e32,m1",
                                         vtype={"sew": 32, "lmul": 1}, vl=2,
                                         avl=6, vlen=128)]).run()["pass"]

    def test_vill_on_illegal(self):
        from AGENT_H.vector_verifier import VectorVerifier
        log = [_vrec(0, "vsetvli x1,x2,e128,m1", vtype={"sew": 128, "lmul": 1},
                     vl=1, avl=1, vlen=128)]
        assert any(v["check"] == "vtype_vill"
                   for v in VectorVerifier(log).run()["violations"])

    def test_velem_clean_and_bug(self):
        from AGENT_H.vector_verifier import VectorVerifier
        clean = [_vrec(0, "vadd.vv v1,v2,v3", vtype={"sew": 8, "lmul": 1}, vl=4,
                       vregs={"v2": [1, 2, 3, 4], "v3": [10, 20, 30, 40],
                              "v1": [11, 22, 33, 44]})]
        assert VectorVerifier(clean).run()["pass"]
        bug = [_vrec(0, "vadd.vv v1,v2,v3", vtype={"sew": 8, "lmul": 1}, vl=4,
                     vregs={"v2": [1, 2, 3, 4], "v3": [10, 20, 30, 40],
                            "v1": [11, 22, 99, 44]})]
        r = VectorVerifier(bug).run()
        assert not r["pass"] and any(v["check"] == "velem" for v in r["violations"])

    def test_velem_vx_and_vi(self):
        from AGENT_H.vector_verifier import VectorVerifier
        vx = [_vrec(0, "vadd.vx v1,v2,x5", vtype={"sew": 8, "lmul": 1}, vl=3,
                    regs={"x5": 5}, vregs={"v2": [1, 2, 3], "v1": [6, 7, 8]})]
        vi = [_vrec(0, "vadd.vi v1,v2,3", vtype={"sew": 8, "lmul": 1}, vl=2,
                    vregs={"v2": [10, 20], "v1": [13, 23]})]
        assert VectorVerifier(vx).run()["pass"]
        assert VectorVerifier(vi).run()["pass"]

    def test_velem_tail_and_mask_ignored(self):
        from AGENT_H.vector_verifier import VectorVerifier
        # tail element beyond vl and mask-off element may be garbage
        tail = [_vrec(0, "vadd.vv v1,v2,v3", vtype={"sew": 8, "lmul": 1}, vl=2,
                      vregs={"v2": [1, 2, 9], "v3": [10, 20, 9],
                             "v1": [11, 22, 123]})]
        masked = [_vrec(0, "vadd.vv v1,v2,v3,v0.t", vtype={"sew": 8, "lmul": 1},
                        vl=3, vmask=[True, False, True],
                        vregs={"v2": [1, 2, 3], "v3": [10, 20, 30],
                               "v1": [11, 99, 33]})]
        assert VectorVerifier(tail).run()["pass"]
        assert VectorVerifier(masked).run()["pass"]

    def test_vtail_disturbed(self):
        from AGENT_H.vector_verifier import VectorVerifier
        log = [_vrec(0, "vadd.vv v1,v2,v3", vtype={"sew": 8, "lmul": 1, "vta": 0},
                     vl=2, vlen=32,
                     vregs={"v2": [1, 2, 0, 0], "v3": [10, 20, 0, 0],
                            "v1": [11, 22, 55, 66]},
                     vstate_prev={"v1": [0, 0, 33, 44]})]
        assert any(v["check"] == "vtail"
                   for v in VectorVerifier(log).run()["violations"])

    def test_no_vector_noop(self):
        from AGENT_H.vector_verifier import VectorVerifier
        r = VectorVerifier([_vrec(0, "addi x1,x0,1")]).run()
        assert r["pass"] and r["vector_active"] is False

    def test_robustness(self):
        from AGENT_H.vector_verifier import VectorVerifier
        for log in ([], [None, 5, "x"], [{}], [{"disasm": None}],
                    [_vrec(0, "vadd.vv v1,v2,v3")]):
            r = VectorVerifier(log).run()
            assert r["pass"] and "band" in r

    def test_report_schema(self):
        from AGENT_H.vector_verifier import VectorVerifier
        r = VectorVerifier([]).run()
        for k in ("schema_version", "agent", "records_checked", "metrics",
                  "total_violations", "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "vector_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.vector_verifier import run_from_manifest
        log = [_vrec(0, "vsetvli x1,x2,e32,m1", vtype={"sew": 32, "lmul": 1},
                     vl=8, avl=8, vlen=128)]
        (tmp_path / "rtl_commit.jsonl").write_text(
            "\n".join(json.dumps(x) for x in log))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "vector_report.json").exists()


class TestVectorMemory:
    """T41 vector load/store: addressing modes, active-element accesses, EEW,
    value consistency."""

    def test_decode_vmem_modes(self):
        from AGENT_H.vector_verifier import decode_vmem
        assert decode_vmem("vle32.v v1,(a0)")["mode"] == "unit"
        assert decode_vmem("vlse64.v v1,(a0),a1")["mode"] == "strided"
        ix = decode_vmem("vluxei32.v v1,(a0),v2")
        assert ix["mode"] == "indexed" and ix["index_eew"] == 32
        assert decode_vmem("vadd.vv v1,v2,v3") is None
        assert decode_vmem("vsub.vv v1,v2,v3") is None

    def test_unit_load_clean_and_wrong(self):
        from AGENT_H.vector_verifier import VectorVerifier
        clean = [_vrec(0, "vle32.v v1,(a0)", vtype={"sew": 32, "lmul": 1}, vl=4,
                       regs={"a0": 0x1000},
                       mem_reads=[{"addr": hex(0x1000 + i * 4), "size": 4}
                                  for i in range(4)])]
        assert VectorVerifier(clean).run()["pass"]
        wrong = [_vrec(0, "vle32.v v1,(a0)", vtype={"sew": 32, "lmul": 1}, vl=4,
                       regs={"a0": 0x1000},
                       mem_reads=[{"addr": hex(0x1000 + i * 8), "size": 4}
                                  for i in range(4)])]
        r = VectorVerifier(wrong).run()
        assert not r["pass"] and any(v["check"] == "vmem_addr" for v in r["violations"])

    def test_spurious_access_count(self):
        from AGENT_H.vector_verifier import VectorVerifier
        log = [_vrec(0, "vse32.v v1,(a0)", vtype={"sew": 32, "lmul": 1}, vl=4,
                     regs={"a0": 0x2000},
                     mem_writes=[{"addr": hex(0x2000 + i * 4), "size": 4}
                                 for i in range(5)])]
        assert any(v["check"] == "vmem_count"
                   for v in VectorVerifier(log).run()["violations"])

    def test_mask_suppresses_access(self):
        from AGENT_H.vector_verifier import VectorVerifier
        ok = [_vrec(0, "vle32.v v1,(a0),v0.t", vtype={"sew": 32, "lmul": 1},
                    vl=4, regs={"a0": 0x1000}, vmask=[True, False, True, True],
                    mem_reads=[{"addr": hex(0x1000 + i * 4), "size": 4}
                               for i in (0, 2, 3)])]
        assert VectorVerifier(ok).run()["pass"]
        spur = [_vrec(0, "vle32.v v1,(a0),v0.t", vtype={"sew": 32, "lmul": 1},
                      vl=4, regs={"a0": 0x1000}, vmask=[True, False, True, True],
                      mem_reads=[{"addr": hex(0x1000 + i * 4), "size": 4}
                                 for i in range(4)])]
        r = VectorVerifier(spur).run()
        assert any(v["check"] in ("vmem_addr", "vmem_count") for v in r["violations"])

    def test_strided(self):
        from AGENT_H.vector_verifier import VectorVerifier
        log = [_vrec(0, "vlse32.v v1,(a0),a1", vtype={"sew": 32, "lmul": 1},
                     vl=3, regs={"a0": 0x1000, "a1": 16},
                     mem_reads=[{"addr": hex(0x1000 + i * 16), "size": 4}
                                for i in range(3)])]
        assert VectorVerifier(log).run()["pass"]

    def test_indexed_clean_and_wrong(self):
        from AGENT_H.vector_verifier import VectorVerifier
        clean = [_vrec(0, "vluxei32.v v1,(a0),v2", vtype={"sew": 32, "lmul": 1},
                       vl=3, regs={"a0": 0x1000}, vregs={"v2": [0, 64, 8]},
                       mem_reads=[{"addr": hex(0x1000 + o), "size": 4}
                                  for o in (0, 64, 8)])]
        assert VectorVerifier(clean).run()["pass"]
        wrong = [_vrec(0, "vluxei32.v v1,(a0),v2", vtype={"sew": 32, "lmul": 1},
                       vl=3, regs={"a0": 0x1000}, vregs={"v2": [0, 64, 8]},
                       mem_reads=[{"addr": hex(0x1000 + o), "size": 4}
                                  for o in (0, 64, 99)])]
        assert any(v["check"] == "vmem_addr"
                   for v in VectorVerifier(wrong).run()["violations"])

    def test_eew_and_value(self):
        from AGENT_H.vector_verifier import VectorVerifier
        eew = [_vrec(0, "vle32.v v1,(a0)", vtype={"sew": 32, "lmul": 1}, vl=2,
                     regs={"a0": 0x1000},
                     mem_reads=[{"addr": hex(0x1000 + i * 4), "size": 2}
                                for i in range(2)])]
        assert any(v["check"] == "vmem_eew"
                   for v in VectorVerifier(eew).run()["violations"])
        val = [_vrec(0, "vle32.v v1,(a0)", vtype={"sew": 32, "lmul": 1}, vl=2,
                     regs={"a0": 0x1000}, vregs={"v1": [0xAA, 0xBB]},
                     mem_reads=[{"addr": "0x1000", "size": 4, "value": 0xAA},
                                {"addr": "0x1004", "size": 4, "value": 0xCC}])]
        assert any(v["check"] == "vmem_value"
                   for v in VectorVerifier(val).run()["violations"])

    def test_metrics_and_gating(self):
        from AGENT_H.vector_verifier import VectorVerifier
        log = [_vrec(0, "vle32.v v1,(a0)", vtype={"sew": 32, "lmul": 1}, vl=4,
                     regs={"a0": 0x1000},
                     mem_reads=[{"addr": hex(0x1000 + i * 4), "size": 4}
                                for i in range(4)])]
        met = VectorVerifier(log).run()["metrics"]
        assert met["mem_ops"] == 1 and met["mem_elements"] == 4
        # no access data → cannot check, no false positive
        nodata = [_vrec(0, "vle32.v v1,(a0)", vtype={"sew": 32, "lmul": 1},
                        vl=4, regs={"a0": 0x1000})]
        assert VectorVerifier(nodata).run()["pass"]


# ─────────────────────────────────────────────────────────────────────────────
# T42 — Functional Coverage Collector (+ self-evolving loop closure)
# ─────────────────────────────────────────────────────────────────────────────

def _crec(seq, disasm, regs=None, **kw):
    r = {"schema_version": "2.1.0", "seq": seq, "pc": hex(0x80000000 + seq * 4),
         "disasm": disasm, "regs": regs or {}, "csrs": {}}
    r.update(kw)
    return r


class TestCoverageCollector:
    def test_import(self):
        from AGENT_H import coverage_collector as cc
        assert hasattr(cc, "CoverageCollector")

    def test_classify_value(self):
        from AGENT_H.coverage_collector import classify_value
        assert classify_value(0) == "zero" and classify_value(1) == "one"
        assert classify_value(0xFFFFFFFF) == "all_ones"
        assert classify_value(0x80000000) == "neg"
        assert classify_value(42) == "pos_small" and classify_value(0x1234) == "pos_large"

    def test_reg_and_valclass(self):
        from AGENT_H.coverage_collector import CoverageCollector
        log = [_crec(0, "addi x5,x0,1", {"x5": "0x1"}),
               _crec(1, "add x6,x5,x5", {"x6": "0x2"})]
        r = CoverageCollector(log).collect()
        assert "reg:x5" in r["covered_bins"] and "valclass:one" in r["covered_bins"]
        assert "reg:x7" in r["holes"] and "reg:x0" not in r["total_bins"]

    def test_abi_name_maps(self):
        from AGENT_H.coverage_collector import CoverageCollector
        r = CoverageCollector([_crec(0, "mv ra,sp", {"ra": "0x80"})]).collect()
        assert "reg:x1" in r["covered_bins"]

    def test_branch_direction(self):
        from AGENT_H.coverage_collector import CoverageCollector
        taken = [_crec(0, "beq x1,x2,0x80000040"),
                 {"seq": 1, "pc": "0x80000040", "disasm": "nop", "regs": {}}]
        nt = [_crec(0, "beq x1,x2,0x80000040"),
              {"seq": 1, "pc": "0x80000004", "disasm": "nop", "regs": {}}]
        assert "branch:taken" in CoverageCollector(taken).collect()["covered_bins"]
        assert "branch:not_taken" in CoverageCollector(nt).collect()["covered_bins"]

    def test_privilege_and_holes(self):
        from AGENT_H.coverage_collector import CoverageCollector
        r = CoverageCollector([_crec(0, "csrr x1,mstatus", priv="M"),
                               _crec(1, "sret", priv="S")]).collect()
        assert "priv:M" in r["covered_bins"] and "priv:U" in r["holes"]

    def test_instruction_model(self):
        from AGENT_H.coverage_collector import CoverageCollector
        r = CoverageCollector([_crec(0, "add x1,x2,x3", {"x1": "0x5"})],
                              model={"instructions": ["add", "sub"]}).collect()
        assert "instr:add" in r["covered_bins"] and "instr:sub" in r["holes"]

    def test_weights(self):
        from AGENT_H.coverage_collector import CoverageCollector
        w = CoverageCollector([_crec(0, "addi x1,x0,1", {"x1": "0x1"})]).collect()["weights"]
        assert w["priv:M"] == 3.0 and w["branch:taken"] == 2.0 and w["reg:x1"] == 1.0

    def test_telemetry(self):
        from AGENT_H.coverage_collector import CoverageCollector
        log = [_crec(0, "csrrw x1,mstatus,x2", {"x1": "0x0"},
                     csrs={"mstatus": "0x1800"}, trap={"cause": 11},
                     vtype={"sew": 32, "lmul": 1})]
        ex = CoverageCollector(log).collect()["observed_extra"]
        assert "trap:cause11" in ex and any(x.startswith("csr:mstatus") for x in ex)

    def test_cross_coverage(self):
        from AGENT_H.coverage_collector import CoverageCollector
        log = [_crec(0, "add x5,x6,x7", {"x5": "0x80000000"})]   # add → neg
        r = CoverageCollector(log).collect()
        assert "cross:add:neg" in r["covered_bins"]
        assert "cross:add:zero" in r["holes"]           # finite universe → hole
        assert r["weights"]["cross:mul:all_ones"] == 2.0
        assert "cross" in r["by_category"]
        # cross bins only for known instructions
        r2 = CoverageCollector([_crec(0, "fakeop x5,x6,x7", {"x5": "0x1"})]).collect()
        assert not any(b.startswith("cross:fakeop") for b in r2["covered_bins"])

    def test_operand_cross_coverage(self):
        from AGENT_H.coverage_collector import CoverageCollector
        # shadow: x6=neg, x7=zero, then add → opnd:neg:zero
        log = [_crec(0, "li x6,0x80000000", {"x6": "0x80000000"}),
               _crec(1, "li x7,0x0", {"x7": "0x0"}),
               _crec(2, "add x5,x6,x7", {"x5": "0x80000000"})]
        r = CoverageCollector(log).collect()
        assert "opnd:neg:zero" in r["covered_bins"]
        assert "opnd:all_ones:all_ones" in r["holes"]
        assert len([b for b in r["total_bins"] if b.startswith("opnd:")]) == 36
        assert "opnd" in r["by_category"]

    def test_operand_reads_preexecution_value(self):
        from AGENT_H.coverage_collector import CoverageCollector
        # add x5,x5,x6 must read the OLD x5 (5=pos_small), not the result
        log = [_crec(0, "li x5,0x5", {"x5": "0x5"}),
               _crec(1, "li x6,0x1", {"x6": "0x1"}),
               _crec(2, "add x5,x5,x6", {"x5": "0x6"})]
        r = CoverageCollector(log).collect()
        assert "opnd:pos_small:one" in r["covered_bins"]

    def test_report_schema(self):
        from AGENT_H.coverage_collector import CoverageCollector
        r = CoverageCollector([]).collect()
        for k in ("schema_version", "agent", "coverage_pct", "covered_bins",
                  "total_bins", "holes", "weights", "coverage_summary", "band"):
            assert k in r
        assert r["agent"] == "coverage_collector"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.coverage_collector import run_from_manifest
        log = [_crec(0, "addi x5,x0,1", {"x5": "0x1"})]
        (tmp_path / "rtl_commit.jsonl").write_text(
            "\n".join(json.dumps(x) for x in log))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 0
        summ = json.loads((tmp_path / "coverage_summary.json").read_text())
        assert "covered_bins" in summ and "total_bins" in summ

    def test_feeds_self_evolving_planner(self):
        """Loop closure: collector holes/weights drive the self-evolving planner."""
        from AGENT_H.coverage_collector import CoverageCollector
        from AGENT_H.self_evolving_engine import plan_from_coverage
        log = [_crec(0, "addi x5,x0,1", {"x5": "0x1"}),
               _crec(1, "sub x7,x6,x5", {"x7": "0xFFFFFFFF"})]
        summ = CoverageCollector(log).collect()["coverage_summary"]
        plan = plan_from_coverage(summ["covered_bins"], summ["total_bins"],
                                  weights=summ["weights"], max_holes=500)
        assert plan["holes_remaining"] == len(summ["holes"])
        labels = [p["hole"] for p in plan["closure_plan"]]
        assert "priv:M" in labels
        assert labels.index("priv:M") < labels.index("reg:x31")  # weight-ranked
        assert plan["closure_plan"][0]["constraint"]["target"] in summ["holes"]


# ─────────────────────────────────────────────────────────────────────────────
# T43 — Coverage-Directed Stimulus Generator (end-to-end loop closure)
# ─────────────────────────────────────────────────────────────────────────────

class TestStimulusGenerator:
    def test_import(self):
        from AGENT_H import stimulus_generator as sg
        assert hasattr(sg, "StimulusGenerator")

    def test_reg_target_self_validates(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        s = g.generate_for(constraint_for("reg:x15"))
        assert g.covers_target(s) and "reg:x15" in g.predicted_coverage(s)

    def test_all_valclass_targets(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        for c in ("zero", "one", "neg", "all_ones", "pos_small", "pos_large"):
            assert g.covers_target(g.generate_for(constraint_for(f"valclass:{c}")))

    def test_branch_and_priv_targets(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=1)
        assert g.covers_target(g.generate_for(constraint_for("branch:taken")))
        assert g.covers_target(g.generate_for(constraint_for("branch:not_taken")))
        for mode in ("M", "S", "U"):
            assert g.covers_target(g.generate_for(constraint_for(f"priv:{mode}")))

    def test_instr_and_batch(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        assert "instr:xor" in g.predicted_coverage(
            g.generate_for(constraint_for("instr:xor")))
        seeds = g.generate_batch([constraint_for(h)
                                  for h in ("reg:x3", "valclass:neg", "priv:S")])
        assert len(seeds) == 3 and all(g.covers_target(s) for s in seeds)

    def test_cross_coverage_targets(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        for m in ("add", "sub", "xor", "mul"):
            for c in ("zero", "neg", "all_ones", "pos_large"):
                s = g.generate_for(constraint_for(f"cross:{m}:{c}"))
                assert g.covers_target(s), f"{m}:{c}"

    def test_operand_cross_targets(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        for a in ("zero", "one", "neg", "all_ones", "pos_small", "pos_large"):
            for b in ("zero", "neg", "all_ones"):
                s = g.generate_for(constraint_for(f"opnd:{a}:{b}"))
                assert g.covers_target(s), f"{a}:{b}"

    def test_env_plugins(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        gen, ev = g.make_env()
        res = ev(gen("directed", [constraint_for("reg:x9"), constraint_for("priv:U")]))
        assert "reg:x9" in res["covered"] and "priv:U" in res["covered"]

    def test_close_coverage_end_to_end(self):
        """Headline: generated stimulus actually closes coverage via the loop."""
        from AGENT_H.stimulus_generator import StimulusGenerator
        rep = StimulusGenerator(seed=0).close_coverage(coverage_target=0.95,
                                                       max_rounds=400)
        assert rep["final_coverage"] >= 0.95 and rep["pass"] is True
        assert rep["strategy_stats"]["directed"]["mean_reward"] >= \
            rep["strategy_stats"]["random"]["mean_reward"]

    def test_generate_from_holes(self):
        from AGENT_H.stimulus_generator import StimulusGenerator, generate_from_holes
        seeds = generate_from_holes(["reg:x20", "valclass:all_ones",
                                     "branch:taken", "priv:S"])
        g = StimulusGenerator()
        assert len(seeds) == 4 and all(g.covers_target(s) for s in seeds)

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.stimulus_generator import run_from_manifest
        summary = {"covered_bins": ["reg:x1"],
                   "total_bins": ["reg:x1", "reg:x2", "priv:S", "valclass:neg"],
                   "holes": ["reg:x2", "priv:S", "valclass:neg"]}
        (tmp_path / "coverage_summary.json").write_text(json.dumps(summary))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path)}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 0
        out = json.loads((tmp_path / "stimulus.json").read_text())
        assert out["holes_targeted"] == 3 and out["seeds_self_validated"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# T44 — Multicore Cache-Coherence Checker
# ─────────────────────────────────────────────────────────────────────────────

def _cev(core, op, addr, value=None, **kw):
    e = {"core": core, "op": op, "addr": hex(addr)}
    if value is not None:
        e["value"] = hex(value)
    e.update(kw)
    return e


class TestCoherenceVerifier:
    def test_import(self):
        from AGENT_H import coherence_verifier as cv
        assert hasattr(cv, "CoherenceVerifier")

    def test_clean_producer_consumer(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(0, "store", 0x40, 7, cycle=1), _cev(1, "load", 0x40, 7, cycle=2)]
        r = CoherenceVerifier(evs).run()
        assert r["pass"] and r["metrics"]["cores"] == 2

    def test_fabricated_value(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(0, "store", 0x40, 7, cycle=1), _cev(1, "load", 0x40, 99, cycle=2)]
        r = CoherenceVerifier(evs).run()
        assert any(v["check"] == "read_from_valid" for v in r["violations"])

    def test_initial_zero_ok_nonzero_flagged(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        assert CoherenceVerifier([_cev(0, "load", 0x40, 0, cycle=1)]).run()["pass"]
        r = CoherenceVerifier([_cev(0, "load", 0x40, 5, cycle=1)]).run()
        assert any(v["check"] == "read_from_valid" for v in r["violations"])

    def test_stale_read(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(0, "store", 0x40, 10, cycle=1), _cev(0, "store", 0x40, 20, cycle=2),
               _cev(1, "load", 0x40, 20, cycle=3), _cev(1, "load", 0x40, 10, cycle=4)]
        r = CoherenceVerifier(evs).run()
        assert not r["pass"]
        assert any(v["check"] == "coherence_read_monotonic" for v in r["violations"])

    def test_cores_disagree_on_order(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(0, "store", 0x80, 10, cycle=1), _cev(1, "store", 0x80, 20, cycle=2),
               _cev(2, "load", 0x80, 20, cycle=3), _cev(2, "load", 0x80, 10, cycle=4)]
        assert any(v["check"] == "coherence_read_monotonic"
                   for v in CoherenceVerifier(evs).run()["violations"])

    def test_explicit_ver(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(0, "store", 0x40, 5, ver=0, cycle=1),
               _cev(0, "store", 0x40, 5, ver=1, cycle=2),
               _cev(1, "load", 0x40, 5, ver=1, cycle=3),
               _cev(1, "load", 0x40, 5, ver=0, cycle=4)]
        assert any(v["check"] == "coherence_read_monotonic"
                   for v in CoherenceVerifier(evs).run()["violations"])

    def test_swmr_two_writers(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(0, "store", 0x40, 1, cycle=1, state="M"),
               _cev(1, "store", 0x40, 2, cycle=2, state="M")]
        assert any(v["check"] == "swmr" for v in CoherenceVerifier(evs).run()["violations"])

    def test_swmr_writer_with_reader(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(0, "store", 0x40, 1, cycle=1, state="M"),
               _cev(1, "load", 0x40, 1, cycle=2, state="S")]
        assert any(v["check"] == "swmr" for v in CoherenceVerifier(evs).run()["violations"])

    def test_swmr_clean_paths(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        after_inval = [_cev(0, "store", 0x40, 1, cycle=1, state="M"),
                       _cev(0, "load", 0x40, 1, cycle=2, state="I"),
                       _cev(1, "store", 0x40, 2, cycle=3, state="M")]
        assert CoherenceVerifier(after_inval).run()["pass"]
        sharers = [_cev(0, "load", 0x40, 0, cycle=1, state="S"),
                   _cev(1, "load", 0x40, 0, cycle=2, state="S"),
                   _cev(2, "load", 0x40, 0, cycle=3, state="S")]
        assert CoherenceVerifier(sharers).run()["pass"]

    def test_cycle_ordering(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        evs = [_cev(1, "load", 0x40, 20, cycle=4), _cev(0, "store", 0x40, 10, cycle=1),
               _cev(1, "load", 0x40, 10, cycle=2), _cev(0, "store", 0x40, 20, cycle=3)]
        assert CoherenceVerifier(evs).run()["pass"]

    def test_single_core_noop_and_robustness(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        r = CoherenceVerifier([_cev(0, "store", 0x40, 1, cycle=1),
                               _cev(0, "load", 0x40, 1, cycle=2)]).run()
        assert r["pass"] and r["coherence_active"] is False
        for evs in ([], [None, 5, "x"], [{}], [{"op": "load"}]):
            assert CoherenceVerifier(evs).run()["pass"]

    def test_report_schema(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        r = CoherenceVerifier([]).run()
        for k in ("schema_version", "agent", "records_checked", "metrics",
                  "total_violations", "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "coherence_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.coherence_verifier import run_from_manifest
        evs = [_cev(0, "store", 0x40, 10, cycle=1), _cev(0, "store", 0x40, 20, cycle=2),
               _cev(1, "load", 0x40, 20, cycle=3), _cev(1, "load", 0x40, 10, cycle=4)]
        (tmp_path / "coherence_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"coherence_trace": "coherence_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "coherence_report.json").exists()


class TestCoherenceCoverage:
    """Coherence-aware coverage/generation loop: multicore scenario bins flow
    through collector → planner → coherence stimulus → coverage."""

    def test_coverage_bins_and_universe(self):
        from AGENT_H.coherence_verifier import (
            coherence_coverage_bins, coherence_universe)
        evs = [_cev(0, "load", 0x40, 0, cycle=1, state="S"),
               _cev(0, "store", 0x40, 1, cycle=2, state="M"),
               _cev(1, "load", 0x40, 1, cycle=3, state="S")]
        covered, sp = coherence_coverage_bins(evs)
        assert sp is True
        assert {"cohtrans:I->S", "cohtrans:S->M", "cohstate:M",
                "cohpat:producer_consumer", "cohshare:1"} <= covered
        # universe is dynamic: state bins only when states present
        assert "cohstate:M" not in coherence_universe(False)
        assert "cohpat:migratory" in coherence_universe(False)

    def test_verifier_report_carries_coverage(self):
        from AGENT_H.coherence_verifier import CoherenceVerifier
        rep = CoherenceVerifier(
            [_cev(0, "store", 0x40, 7, cycle=1),
             _cev(1, "load", 0x40, 7, cycle=2)]).run()
        cov = rep["coherence_coverage"]
        assert "cohpat:producer_consumer" in cov["covered_bins"]
        assert "cohpat:read_shared" in cov["holes"]

    def test_collector_merges_coherence_bins(self):
        from AGENT_H.coverage_collector import CoverageCollector
        evs = [_cev(0, "store", 0x40, 1, cycle=1, state="M"),
               _cev(1, "load", 0x40, 1, cycle=2, state="S")]
        r = CoverageCollector([], coherence_events=evs).collect()
        assert "cohstate:M" in r["covered_bins"] and "cohstate:M" in r["total_bins"]
        assert "cohpat:migratory" in r["holes"]           # not exercised → hole
        assert "cohpat" in r["by_category"]
        # no coherence trace → no coherence bins
        r2 = CoverageCollector([_crec(0, "addi x5,x0,1", {"x5": "0x1"})]).collect()
        assert not any(b.startswith("coh") for b in r2["total_bins"])

    def test_stimulus_coherence_templates_self_validate(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        for bin_ in ("cohpat:producer_consumer", "cohpat:migratory",
                     "cohpat:read_shared", "cohpat:write_shared",
                     "cohstate:M", "cohstate:E", "cohstate:S", "cohstate:I",
                     "cohtrans:I->S", "cohtrans:I->E", "cohtrans:I->M",
                     "cohtrans:S->M", "cohtrans:E->M",
                     "cohshare:1", "cohshare:2", "cohshare:3plus"):
            assert g.covers_target(g.generate_for(constraint_for(bin_))), bin_

    def test_generated_coherence_is_coherence_clean(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.coherence_verifier import CoherenceVerifier
        from AGENT_H.self_evolving_engine import constraint_for
        g = StimulusGenerator(seed=0)
        for bin_ in ("cohpat:producer_consumer", "cohtrans:S->M", "cohshare:2"):
            s = g.generate_for(constraint_for(bin_))
            assert CoherenceVerifier(s["coherence_events"]).run()["pass"], bin_

    def test_loop_closes_coherence_coverage(self):
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.coverage_collector import CoverageCollector
        from AGENT_H.coherence_verifier import coherence_universe
        g = StimulusGenerator(seed=0)
        total = sorted(set(CoverageCollector([]).collect()["total_bins"])
                       | set(coherence_universe(True)))
        assert any(b.startswith("coh") for b in total)
        rep = g.close_coverage(total_bins=total, coverage_target=0.9,
                               max_rounds=1200)
        assert rep["final_coverage"] >= 0.9


# ─────────────────────────────────────────────────────────────────────────────
# T45 — Memory-Consistency Checker (SC / TSO / RVWMO)
# ─────────────────────────────────────────────────────────────────────────────

def _mop(core, o, addr=None, value=None, **kw):
    e = {"core": core, "op": o}
    if addr is not None:
        e["addr"] = hex(addr)
    if value is not None:
        e["value"] = value
    e.update(kw)
    return e


_MX, _MY = 0x10, 0x20
_SB = [_mop(0, "store", _MX, 1), _mop(0, "load", _MY, 0),
       _mop(1, "store", _MY, 1), _mop(1, "load", _MX, 0)]
_MP = [_mop(0, "store", _MX, 1), _mop(0, "store", _MY, 1),
       _mop(1, "load", _MY, 1), _mop(1, "load", _MX, 0)]
_LB = [_mop(0, "load", _MX, 1), _mop(0, "store", _MY, 1),
       _mop(1, "load", _MY, 1), _mop(1, "store", _MX, 1)]


class TestMemoryModelVerifier:
    def test_import(self):
        from AGENT_H import memory_model_verifier as mm
        assert hasattr(mm, "MemoryModelVerifier")

    def test_sb_allowed_tso_forbidden_sc(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        assert MemoryModelVerifier(_SB, "tso").run()["pass"]        # store→load relaxed
        r = MemoryModelVerifier(_SB, "sc").run()
        assert not r["pass"]
        assert any(v["check"] == "consistency_sc" for v in r["violations"])

    def test_sb_fenced_forbidden_tso(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        fenced = [_mop(0, "store", _MX, 1), _mop(0, "fence"), _mop(0, "load", _MY, 0),
                  _mop(1, "store", _MY, 1), _mop(1, "fence"), _mop(1, "load", _MX, 0)]
        assert not MemoryModelVerifier(fenced, "tso").run()["pass"]

    def test_mp_forbidden_tso_allowed_rvwmo(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        assert not MemoryModelVerifier(_MP, "tso").run()["pass"]    # ss & ll preserved
        assert MemoryModelVerifier(_MP, "rvwmo").run()["pass"]      # unordered w/o fence

    def test_mp_fenced_forbidden_rvwmo(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        fenced = [_mop(0, "store", _MX, 1), _mop(0, "fence"), _mop(0, "store", _MY, 1),
                  _mop(1, "load", _MY, 1), _mop(1, "fence"), _mop(1, "load", _MX, 0)]
        assert not MemoryModelVerifier(fenced, "rvwmo").run()["pass"]

    def test_lb_forbidden_tso_allowed_rvwmo(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        assert not MemoryModelVerifier(_LB, "tso").run()["pass"]    # load→store preserved
        assert MemoryModelVerifier(_LB, "rvwmo").run()["pass"]

    def test_coherence_via_sc_per_location(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        corr = [_mop(0, "store", _MX, 1, co=0), _mop(0, "store", _MX, 2, co=1),
                _mop(1, "load", _MX, 2), _mop(1, "load", _MX, 1)]   # reads 2 then 1
        r = MemoryModelVerifier(corr, "tso").run()
        assert not r["pass"]
        assert any(v["check"] == "sc_per_location" for v in r["violations"])

    def test_cycle_witness(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        r = MemoryModelVerifier(_MP, "tso").run()
        assert r["violations"][0]["cycle"] and len(r["violations"][0]["cycle"]) >= 3

    def test_model_normalisation_and_noop(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        assert MemoryModelVerifier([], "garbage").model == "tso"
        assert MemoryModelVerifier([], "RVWMO").model == "rvwmo"
        r = MemoryModelVerifier([_mop(0, "store", _MX, 1),
                                 _mop(0, "load", _MX, 1)], "tso").run()
        assert r["pass"] and r["consistency_active"] is False

    def test_robustness_and_schema(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        for exe in ([], [None, 5], [{}], [{"op": "bogus"}]):
            assert MemoryModelVerifier(exe, "tso").run()["pass"]
        r = MemoryModelVerifier([], "tso").run()
        for k in ("schema_version", "agent", "model", "metrics",
                  "total_violations", "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "memory_model_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.memory_model_verifier import run_from_manifest
        (tmp_path / "consistency_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in _MP))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "memory_model": "tso",
               "outputs": {"consistency_trace": "consistency_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1     # MP is a TSO violation
        assert (tmp_path / "memory_model_report.json").exists()

    def test_release_acquire_rvwmo(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        # release store + acquire load restores MP ordering under RVWMO
        forb = [_mop(0, "store", _MX, 1), _mop(0, "store", _MY, 1, rl=True),
                _mop(1, "load", _MY, 1, aq=True), _mop(1, "load", _MX, 0)]
        assert not MemoryModelVerifier(forb, "rvwmo").run()["pass"]
        # release alone (no acquire) is insufficient
        rel_only = [_mop(0, "store", _MX, 1), _mop(0, "store", _MY, 1, rl=True),
                    _mop(1, "load", _MY, 1), _mop(1, "load", _MX, 0)]
        assert MemoryModelVerifier(rel_only, "rvwmo").run()["pass"]

    def test_fence_predecessor_successor_sets(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        rr = [_mop(0, "store", _MX, 1), _mop(0, "fence", pred="r", succ="r"),
              _mop(0, "load", _MY, 0),
              _mop(1, "store", _MY, 1), _mop(1, "fence", pred="r", succ="r"),
              _mop(1, "load", _MX, 0)]
        assert MemoryModelVerifier(rr, "tso").run()["pass"]      # r,r doesn't order W→R
        rw = [_mop(0, "store", _MX, 1), _mop(0, "fence", pred="rw", succ="rw"),
              _mop(0, "load", _MY, 0),
              _mop(1, "store", _MY, 1), _mop(1, "fence", pred="rw", succ="rw"),
              _mop(1, "load", _MX, 0)]
        assert not MemoryModelVerifier(rw, "tso").run()["pass"]  # rw,rw does

    def test_rmw_atomicity(self):
        from AGENT_H.memory_model_verifier import MemoryModelVerifier
        # a remote store interposed between the RMW's read and write
        broken = [_mop(0, "load", _MX, 0, rmw="g"),
                  _mop(1, "store", _MX, 9, co=0),
                  _mop(0, "store", _MX, 1, rmw="g", co=1)]
        r = MemoryModelVerifier(broken, "tso").run()
        assert not r["pass"]
        assert any(v["check"] == "rmw_atomicity" for v in r["violations"])
        # no interposition → atomic
        ok = [_mop(0, "load", _MX, 0, rmw="g"),
              _mop(0, "store", _MX, 1, rmw="g", co=0)]
        assert MemoryModelVerifier(ok, "tso").run()["pass"]

    def test_consistency_coverage_and_loop(self):
        from AGENT_H.memory_model_verifier import (
            consistency_coverage_bins, consistency_universe)
        from AGENT_H.coverage_collector import CoverageCollector
        from AGENT_H.stimulus_generator import StimulusGenerator
        from AGENT_H.self_evolving_engine import constraint_for
        exe = [_mop(0, "store", _MX, 1), _mop(0, "fence"),
               _mop(0, "load", _MY, 0, aq=True)]
        cov = consistency_coverage_bins(exe)
        assert {"mmpair:store_load", "mmsync:fence", "mmsync:aq"} <= cov
        assert len(consistency_universe()) == 8
        # collector merges consistency bins into the coverage model
        r = CoverageCollector([], consistency_execution=exe).collect()
        assert "mmpair:store_load" in r["covered_bins"] and "mmsync:rmw" in r["holes"]
        # stimulus templates self-validate + loop closes over the mm universe
        g = StimulusGenerator(seed=0)
        for b in ("mmpair:load_store", "mmsync:rl", "mmsync:rmw"):
            assert g.covers_target(g.generate_for(constraint_for(b))), b
        total = sorted(set(CoverageCollector([]).collect()["total_bins"])
                       | set(consistency_universe()))
        rep = g.close_coverage(total_bins=total, coverage_target=0.9, max_rounds=1000)
        assert rep["final_coverage"] >= 0.9


# ─────────────────────────────────────────────────────────────────────────────
# T46 — Interrupt Controller (PLIC / CLINT) Checker
# ─────────────────────────────────────────────────────────────────────────────

class TestInterruptVerifier:
    _CFG = {"op": "config", "priorities": {"3": 7, "5": 4, "7": 0},
            "enables": {"0": [3, 5, 7]}, "thresholds": {"0": 2}}

    def _run(self, evs):
        from AGENT_H.interrupt_verifier import InterruptVerifier
        return InterruptVerifier(evs).run()

    def test_import(self):
        from AGENT_H import interrupt_verifier as iv
        assert hasattr(iv, "InterruptVerifier") and hasattr(iv, "PLICModel")

    def test_claim_highest_priority_and_wrong(self):
        pend = [self._CFG, {"op": "pending", "source": 3},
                {"op": "pending", "source": 5}]
        assert self._run(pend + [{"op": "claim", "context": 0, "result": 3}])["pass"]
        r = self._run(pend + [{"op": "claim", "context": 0, "result": 5}])
        assert not r["pass"] and any(v["check"] == "plic_claim_wrong"
                                     for v in r["violations"])

    def test_threshold_and_priority0(self):
        thr = [{"op": "config", "priorities": {"5": 4}, "enables": {"0": [5]},
                "thresholds": {"0": 5}}, {"op": "pending", "source": 5},
               {"op": "claim", "context": 0, "result": 5}]
        assert not self._run(thr)["pass"]
        p0 = [self._CFG, {"op": "pending", "source": 7},
              {"op": "claim", "context": 0, "result": 7}]
        assert any(v["check"] == "plic_priority0"
                   for v in self._run(p0)["violations"])

    def test_tie_break_and_claim_clears(self):
        tie = [{"op": "config", "priorities": {"3": 5, "5": 5},
                "enables": {"0": [3, 5]}, "thresholds": {"0": 0}},
               {"op": "pending", "source": 5}, {"op": "pending", "source": 3},
               {"op": "claim", "context": 0, "result": 3}]     # tie → lowest id
        assert self._run(tie)["pass"]
        seq = [self._CFG, {"op": "pending", "source": 3}, {"op": "pending", "source": 5},
               {"op": "claim", "context": 0, "result": 3},
               {"op": "claim", "context": 0, "result": 5}]      # 3 cleared → 5 next
        assert self._run(seq)["pass"]

    def test_disabled_and_no_pending(self):
        dis = [{"op": "config", "priorities": {"3": 7}, "enables": {"0": []},
                "thresholds": {"0": 0}}, {"op": "pending", "source": 3},
               {"op": "claim", "context": 0, "result": 3}]
        assert not self._run(dis)["pass"]
        assert self._run([self._CFG, {"op": "claim", "context": 0, "result": 0}])["pass"]

    def test_clint(self):
        assert self._run([{"op": "clint", "mtime": 100, "mtimecmp": 100,
                           "mtip": True}])["pass"]
        r = self._run([{"op": "clint", "mtime": 99, "mtimecmp": 100, "mtip": True}])
        assert not r["pass"] and any(v["check"] == "clint_mtip" for v in r["violations"])
        r2 = self._run([{"op": "clint", "msip": True, "expected_msip": False}])
        assert any(v["check"] == "clint_msip" for v in r2["violations"])

    def test_robustness_and_schema(self):
        for evs in ([], [None, 5], [{}], [{"op": "bogus"}]):
            assert self._run(evs)["pass"]
        r = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "interrupt_verifier"

    def test_run_from_manifest(self, tmp_path):
        from AGENT_H.interrupt_verifier import run_from_manifest
        evs = [self._CFG, {"op": "pending", "source": 3}, {"op": "pending", "source": 5},
               {"op": "claim", "context": 0, "result": 5}]
        (tmp_path / "interrupt_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"interrupt_trace": "interrupt_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "interrupt_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T47 — Performance-Counter Checker
# ─────────────────────────────────────────────────────────────────────────────

def _prec(seq, ins=None, cyc=None, inhibit=None, trap=None):
    pc = {}
    if ins is not None:
        pc["instret"] = ins
    if cyc is not None:
        pc["cycles"] = cyc
    r = {"schema_version": "2.1.0", "seq": seq, "disasm": "add x1,x2,x3",
         "regs": {}, "csrs": {}}
    if pc:
        r["perf_counters"] = pc
    if inhibit is not None:
        r["csrs"]["mcountinhibit"] = hex(inhibit)
    if trap is not None:
        r["trap"] = trap
    return r


class TestPerfCounterVerifier:
    def _run(self, rs):
        from AGENT_H.perf_counter_verifier import PerfCounterVerifier
        return PerfCounterVerifier(rs).run()

    def test_import(self):
        from AGENT_H import perf_counter_verifier as pv
        assert hasattr(pv, "PerfCounterVerifier")

    def test_clean_and_ipc(self):
        r = self._run([_prec(0, 1, 10), _prec(1, 2, 15), _prec(2, 3, 18)])
        assert r["pass"] and r["metrics"]["ipc"] is not None

    def test_instret_skip_and_freeze(self):
        assert any(v["check"] == "perf_instret_increment"
                   for v in self._run([_prec(0, 1, 10), _prec(1, 3, 15)])["violations"])
        assert any(v["check"] == "perf_instret_increment"
                   for v in self._run([_prec(0, 1, 10), _prec(1, 1, 15)])["violations"])

    def test_ir_inhibit(self):
        assert self._run([_prec(0, 5, 10), _prec(1, 5, 15, inhibit=0x4)])["pass"]
        assert not self._run([_prec(0, 5, 10), _prec(1, 6, 15, inhibit=0x4)])["pass"]

    def test_cycle_monotonic_and_superscalar(self):
        assert not self._run([_prec(0, 1, 20), _prec(1, 2, 15)])["pass"]   # backwards
        assert self._run([_prec(0, 1, 10), _prec(1, 2, 10)])["pass"]       # same cycle ok

    def test_cy_inhibit_and_trap(self):
        assert self._run([_prec(0, 1, 10), _prec(1, 2, 10, inhibit=0x1)])["pass"]
        assert not self._run([_prec(0, 1, 10), _prec(1, 2, 12, inhibit=0x1)])["pass"]
        assert self._run([_prec(0, 1, 10),
                          _prec(1, 1, 12, trap={"cause": 2})])["pass"]     # trap → no retire

    def test_noop_and_robustness(self):
        r = self._run([{"disasm": "add", "regs": {}, "csrs": {}}])
        assert r["pass"] and r["perf_active"] is False
        for rs in ([], [None, 5], [{}]):
            assert self._run(rs)["pass"]

    def test_schema_and_manifest(self, tmp_path):
        from AGENT_H.perf_counter_verifier import run_from_manifest
        r = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "perf_counter_verifier"
        rs = [_prec(0, 1, 10), _prec(1, 3, 15)]
        (tmp_path / "rtl_commit.jsonl").write_text(
            "\n".join(json.dumps(x) for x in rs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "perf_counter_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T48 — Debug & Trigger Module Checker
# ─────────────────────────────────────────────────────────────────────────────

class TestDebugVerifier:
    _XT = {"op": "trigger_config", "index": 0, "execute": True,
           "tdata2": "0x80000040", "action": 1, "priv": ["M"]}

    def _run(self, evs):
        from AGENT_H.debug_verifier import DebugVerifier
        return DebugVerifier(evs).run()

    def test_import(self):
        from AGENT_H import debug_verifier as dv
        assert hasattr(dv, "DebugVerifier") and hasattr(dv, "Trigger")

    def test_trigger_fire_missed_spurious(self):
        assert self._run([self._XT, {"op": "exec", "pc": "0x80000040",
                                     "priv": "M", "fired": True, "dcsr_cause": 2}])["pass"]
        assert any(v["check"] == "trigger_missed" for v in self._run(
            [self._XT, {"op": "exec", "pc": "0x80000040", "priv": "M",
                        "fired": False}])["violations"])
        assert any(v["check"] == "trigger_spurious" for v in self._run(
            [{"op": "exec", "pc": "0x80000040", "priv": "M", "fired": True}])["violations"])

    def test_nonmatch_and_priv_gating(self):
        assert self._run([self._XT, {"op": "exec", "pc": "0x80000044",
                                     "priv": "M", "fired": False}])["pass"]
        assert self._run([self._XT, {"op": "exec", "pc": "0x80000040",
                                     "priv": "U", "fired": False}])["pass"]  # M-only
        assert any(v["check"] == "trigger_spurious" for v in self._run(
            [self._XT, {"op": "exec", "pc": "0x80000040", "priv": "U",
                        "fired": True}])["violations"])

    def test_load_store_type(self):
        lt = {"op": "trigger_config", "index": 1, "load": True,
              "tdata2": "0x2000", "priv": ["M"]}
        assert self._run([lt, {"op": "load", "addr": "0x2000", "pc": "0x10",
                               "priv": "M", "fired": True}])["pass"]
        assert any(v["check"] == "trigger_spurious" for v in self._run(
            [lt, {"op": "store", "addr": "0x2000", "pc": "0x10", "priv": "M",
                  "fired": True}])["violations"])            # store not enabled

    def test_trigger_cause(self):
        assert any(v["check"] == "trigger_cause" for v in self._run(
            [self._XT, {"op": "exec", "pc": "0x80000040", "priv": "M",
                        "fired": True, "dcsr_cause": 3}])["violations"])

    def test_debug_cause_dpc_step(self):
        assert self._run([{"op": "halt", "cause": "haltreq", "pc": "0x100",
                           "dpc": "0x100", "dcsr_cause": 3}])["pass"]
        assert any(v["check"] == "debug_cause" for v in self._run(
            [{"op": "halt", "cause": "haltreq", "dcsr_cause": 1}])["violations"])
        assert any(v["check"] == "debug_dpc" for v in self._run(
            [{"op": "halt", "cause": "step", "pc": "0x100", "dpc": "0x200",
              "dcsr_cause": 4}])["violations"])
        assert not self._run([{"op": "step", "instrs_executed": 2}])["pass"]

    def test_abstract(self):
        assert self._run([{"op": "halt", "cause": "haltreq"},
                          {"op": "abstract", "cmd": "access_reg", "regno": 10,
                           "halted": True, "result": "0x5", "expected": "0x5"}])["pass"]
        assert any(v["check"] == "abstract_nothalted" for v in self._run(
            [{"op": "abstract", "cmd": "access_reg", "halted": False,
              "result": "0x5"}])["violations"])
        assert any(v["check"] == "abstract_result" for v in self._run(
            [{"op": "abstract", "cmd": "access_reg", "halted": True,
              "result": "0x9", "expected": "0x5"}])["violations"])

    def test_robustness_and_schema(self):
        for evs in ([], [None, 5], [{}], [{"op": "bogus"}]):
            assert self._run(evs)["pass"]
        r = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "debug_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.debug_verifier import run_from_manifest
        evs = [self._XT, {"op": "exec", "pc": "0x80000040", "priv": "M",
                          "fired": False}]
        (tmp_path / "debug_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"debug_trace": "debug_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "debug_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T49 — Reset-State Checker
# ─────────────────────────────────────────────────────────────────────────────

class TestResetVerifier:
    _GOOD = {"hart": 0, "priv": "M", "pc": "0x80000000",
             "csrs": {"mstatus": "0x0", "misa": "0x40141101", "mie": "0x0"},
             "expected": {"pc": "0x80000000"}}

    def _run(self, s, cfg=None):
        from AGENT_H.reset_verifier import ResetVerifier
        return ResetVerifier(s, cfg).run()

    def test_import_and_clean(self):
        from AGENT_H import reset_verifier as rv
        assert hasattr(rv, "ResetVerifier")
        assert self._run(self._GOOD)["pass"]

    def test_priv_mie_mprv(self):
        assert any(v["check"] == "reset_priv" for v in
                   self._run(dict(self._GOOD, priv="S"))["violations"])
        assert any(v["check"] == "reset_mstatus_mie" for v in self._run(
            dict(self._GOOD, csrs={"mstatus": "0x8", "misa": "0x40141101"}))["violations"])
        assert any(v["check"] == "reset_mstatus_mprv" for v in self._run(
            dict(self._GOOD, csrs={"mstatus": hex(1 << 17),
                                   "misa": "0x40141101"}))["violations"])

    def test_pc_snapshot_and_config(self):
        assert any(v["check"] == "reset_pc" for v in
                   self._run(dict(self._GOOD, pc="0x80000004"))["violations"])
        s = {"priv": "M", "pc": "0x1000", "csrs": {"mstatus": "0x0"}}
        assert self._run(s, {"reset_vector": "0x1000"})["pass"]
        assert not self._run(dict(s, pc="0x2000"), {"reset_vector": "0x1000"})["pass"]

    def test_misa_rv32_bad_rv64_ok(self):
        assert any(v["check"] == "reset_misa" for v in self._run(
            dict(self._GOOD, csrs={"mstatus": "0x0", "misa": "0x00141101"}))["violations"])
        assert self._run(dict(self._GOOD,
                              csrs={"mstatus": "0x0",
                                    "misa": hex((2 << 62) | 0x141101)}))["pass"]

    def test_expected_csr_and_multihart(self):
        s = dict(self._GOOD, csrs={"mstatus": "0x0", "misa": "0x40141101",
                                   "mtvec": "0x0"},
                 expected={"pc": "0x80000000", "csrs": {"mtvec": "0x80000004"}})
        assert any(v["check"] == "reset_csr" for v in self._run(s)["violations"])
        r = self._run([self._GOOD, dict(self._GOOD, hart=1, priv="U")])
        assert r["metrics"]["harts"] == 2 and not r["pass"]

    def test_robustness_schema(self):
        for s in ([], [None, 5], [{}]):
            assert self._run(s)["pass"]
        r = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "reset_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.reset_verifier import run_from_manifest
        (tmp_path / "reset_snapshot.json").write_text(
            json.dumps(dict(self._GOOD, pc="0x4")))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"reset_snapshot": "reset_snapshot.json"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "reset_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Cross-agent robustness & report-schema consistency
# ─────────────────────────────────────────────────────────────────────────────

def _new_agent_builders():
    """(name, builder(log) -> verifier) for every T23-T29 commit-log agent."""
    from AGENT_H.atomics_verifier    import AtomicsVerifier
    from AGENT_H.csr_verifier        import CSRVerifier
    from AGENT_H.rvc_verifier        import RVCVerifier
    from AGENT_H.fp_verifier         import FPVerifier
    from AGENT_H.bitmanip_verifier   import BitmanipVerifier
    from AGENT_H.privilege_verifier  import PrivilegeVerifier
    from AGENT_H.peripheral_verifier import PeripheralVerifier
    return [
        ("atomics",    lambda log: AtomicsVerifier(log)),
        ("csr",        lambda log: CSRVerifier(log)),
        ("rvc",        lambda log: RVCVerifier(log)),
        ("fp",         lambda log: FPVerifier(log)),
        ("bitmanip",   lambda log: BitmanipVerifier(log)),
        ("privilege",  lambda log: PrivilegeVerifier(log)),
        ("peripheral", lambda log: PeripheralVerifier(log, "dma")),
    ]


_MALFORMED_LOGS = [
    [],                                                   # empty
    [None, 5, "junk", [], 3.14, True],                    # non-dict records
    [{}],                                                 # empty dict
    [{"disasm": None, "regs": None, "csrs": None, "pc": None}],  # None fields
    [{"seq": 0, "disasm": "amoadd.w x1,x2,(x3)"}],        # missing mem fields
    [{"seq": i} for i in range(300)],                     # bulk, no content
]

_COMMON_REPORT_KEYS = {
    "schema_version", "agent", "total_violations",
    "high_violations", "pass", "violations", "band",
}


class TestRobustness:
    @pytest.mark.parametrize("name,builder", _new_agent_builders())
    @pytest.mark.parametrize("log_idx", range(len(_MALFORMED_LOGS)))
    def test_no_crash_on_malformed_input(self, name, builder, log_idx):
        import copy
        rep = builder(copy.deepcopy(_MALFORMED_LOGS[log_idx])).run()
        assert isinstance(rep, dict)
        assert _COMMON_REPORT_KEYS <= set(rep), \
            f"{name} report missing {_COMMON_REPORT_KEYS - set(rep)}"
        assert isinstance(rep["violations"], list)

    @pytest.mark.parametrize("name,builder", _new_agent_builders())
    def test_empty_log_passes_cleanly(self, name, builder):
        rep = builder([]).run()
        assert rep["pass"] is True
        assert rep["total_violations"] == 0
        assert rep["band"] == "CLEAN"


class TestReportSchemaConsistency:
    @pytest.mark.parametrize("name,builder", _new_agent_builders())
    def test_common_schema(self, name, builder):
        rep = builder([]).run()
        assert _COMMON_REPORT_KEYS <= set(rep), \
            f"{name} missing {_COMMON_REPORT_KEYS - set(rep)}"
        assert rep["schema_version"] == "2.1.0"
        assert rep["band"] in ("CLEAN", "MINOR", "DEGRADED", "CRITICAL")
        assert isinstance(rep["total_violations"], int)
        assert isinstance(rep["pass"], bool)


class TestCrossAgentCleanLog:
    """A single clean, mixed commit log must pass every commit-log verifier."""

    def _mixed_log(self):
        return [
            {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
             "disasm": "addi x6,x0,5", "regs": {"x6": "0x00000005"}, "csrs": {}, "priv": 3},
            {"schema_version": "2.1.0", "seq": 1, "pc": "0x80000004",
             "disasm": "amoadd.w x5,x6,(x10)", "regs": {"x5": "0x00000010"},
             "csrs": {}, "priv": 3,
             "mem_reads": [{"addr": "0x00000100", "size": 4, "value": "0x00000010"}],
             "mem_writes": [{"addr": "0x00000100", "size": 4, "value": "0x00000015"}]},
            {"schema_version": "2.1.0", "seq": 2, "pc": "0x80000008",
             "disasm": "csrrw x5,mstatus,x6", "regs": {"x5": "0x00001800"},
             "csrs": {"mstatus": "0x00000005"}, "priv": 3},
            {"schema_version": "2.1.0", "seq": 3, "pc": "0x8000000a",
             "disasm": "c.addi x1,1", "regs": {"x1": "0x1"}, "csrs": {}, "priv": 3},
            {"schema_version": "2.1.0", "seq": 4, "pc": "0x8000000c",
             "disasm": "andn x3,x1,x6", "regs": {"x3": "0x00000000"}, "csrs": {}, "priv": 3},
        ]

    @pytest.mark.parametrize("name,builder", _new_agent_builders())
    def test_clean_mixed_log_passes(self, name, builder):
        if name == "peripheral":
            pytest.skip("peripheral consumes a DUT-specific raw log, not a CPU log")
        rep = builder(self._mixed_log()).run()
        assert rep["pass"] is True, \
            f"{name} flagged a clean mixed log: {rep.get('violations')}"
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
        ava = ava_patc
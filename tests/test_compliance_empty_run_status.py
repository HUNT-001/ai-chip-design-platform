"""#108: a compliance run that executed nothing is an infrastructure problem.

compute_exit_code already treats total == 0 as EXIT_CRASH ("nothing ran").
AGENT_A's lifecycle_status says an empty result is not evidence of a DUT
failure, so the manifest must say infra_error, not failed.  The expected
values here come from the issue and the schema vocabulary, not from the code.
"""
import json

import run_compliance as rc


def _empty_report():
    return rc.RunReport(
        timestamp="2025-01-01T00:00:00+00:00", isa="RV32IM",
        spike_bin="spike", spike_version="x", toolchain="x",
        run_dir="r", tests=(),
        summary={"total": 0, "pass": 0, "fail": 0, "error": 0, "skipped": 0,
                 "pass_rate_pct": 0.0, "tool_errors": [], "run_dir": "r"},
    )


def test_run_that_executed_nothing_is_infra_error_in_the_manifest(tmp_path, monkeypatch):
    class _Runner:
        def __init__(self, cfg):
            pass

        def run(self):
            return _empty_report()

    monkeypatch.setattr(rc, "ComplianceRunner", _Runner)
    manifest = tmp_path / "run_manifest.json"
    manifest.write_text(json.dumps({"rundir": str(tmp_path)}), encoding="utf-8")

    code = rc.run_compliance_manifest(manifest)

    after = json.loads(manifest.read_text(encoding="utf-8"))
    assert code == rc.EXIT_CRASH
    assert after["phases"]["compliance"]["status"] == "infra_error"
    assert after["status"] == "infra_error"

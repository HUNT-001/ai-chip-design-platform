"""#8 (part): phases.*.status must use the run-manifest schema's vocabulary.

``phase_result.status`` refs ``$defs.lifecycle_status`` in run_manifest.schema.json,
whose terminal success value is "passed"; "completed" is not in the enum.  The
expected value below comes from the schema and the issue, not from the producers.
"""
import importlib
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "AGENT_A" / "run_manifest.schema.json"


def _lifecycle_enum():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["lifecycle_status"]["enum"]


def test_schema_vocabulary_is_what_the_issue_says():
    enum = _lifecycle_enum()
    assert "passed" in enum and "completed" not in enum


def test_generate_tests_writes_a_schema_valid_generator_status(tmp_path):
    gt = importlib.import_module("AGENT_G.generate_tests")
    manifest = tmp_path / "run_manifest.json"
    manifest.write_text(json.dumps({"rundir": str(tmp_path)}))
    assert gt.generate_from_manifest(manifest) == 0
    status = json.loads(manifest.read_text())["phases"]["generator"]["status"]
    assert status == "passed"
    assert status in _lifecycle_enum()


def test_coverage_pipeline_writes_a_schema_valid_coverage_status(tmp_path):
    cp = importlib.import_module("coverage_pipeline")
    dat = tmp_path / "outputs" / "coverageraw" / "coverage.dat"
    dat.parent.mkdir(parents=True)
    dat.write_text(
        "# SystemC::Coverage-3\n"
        "C '7' 'riscv_core.sv' '1' '0' 'top.riscv_core' ''\n"
    )
    manifest = tmp_path / "run_manifest.json"
    manifest.write_text(json.dumps({"rundir": str(tmp_path), "run_id": "r"}))
    assert cp.main(["--manifest", str(manifest)]) == 0
    status = json.loads(manifest.read_text())["phases"]["coverage"]["status"]
    assert status == "passed"
    assert status in _lifecycle_enum()


def _coverage_status_violations(tmp_path, status, **extra):
    ml = importlib.import_module("manifest_contract")
    path = tmp_path / "m.json"
    data = {"phases": {"coverage": {"status": status}}}
    data.update(extra)
    path.write_text(json.dumps(data))
    return [v for v in ml.ManifestContract(path).validate() if "phases.coverage.status" in v]


def test_manifest_contract_accepts_passed(tmp_path):
    assert _coverage_status_violations(
        tmp_path, "passed", outputs={"coveragesummary": "coveragesummary.json"}) == []


def test_manifest_contract_rejects_an_invalid_status(tmp_path):
    assert _coverage_status_violations(
        tmp_path, "bogus", outputs={"coveragesummary": "coveragesummary.json"}) != []


def test_manifest_contract_rejects_the_schema_invalid_completed(tmp_path):
    assert _coverage_status_violations(
        tmp_path, "completed", outputs={"coveragesummary": "coveragesummary.json"}) != []


def test_manifest_contract_cross_field_rule_applies_to_passed(tmp_path):
    ml = importlib.import_module("manifest_contract")
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"phases": {"coverage": {"status": "passed"}}}))
    assert any("[CROSS]" in v and "coveragesummary" in v
               for v in ml.ManifestContract(path).validate())


def test_manifest_contract_phase_order_applies_to_passed(tmp_path):
    ml = importlib.import_module("manifest_contract")
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"phases": {
        "semantic": {"status": "pending"}, "testbench": {"status": "passed"}}}))
    with pytest.raises(ml.ManifestContractError):
        ml.ManifestContract(path).assert_phase_order()


def test_manifest_contract_phase_order_accepts_an_ordered_run(tmp_path):
    ml = importlib.import_module("manifest_contract")
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"phases": {
        "semantic": {"status": "passed"}, "testbench": {"status": "passed"}}}))
    ml.ManifestContract(path).assert_phase_order()

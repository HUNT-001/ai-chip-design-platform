"""#103: the coverage phase status enum in AGENT_F/manifest_contract.py must be
AGENT_A's lifecycle_status vocabulary.

The expected set is read from run_manifest.schema.json at test time, not copied
from the module under test.
"""
import importlib
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "AGENT_A" / "run_manifest.schema.json"


def _lifecycle_enum():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["lifecycle_status"]["enum"]


def _coverage_assertion():
    ml = importlib.import_module("manifest_contract")
    found = [a for a in ml.MANIFEST_ASSERTIONS
             if a.field == "phases.coverage.status" and a.enum is not None]
    assert len(found) == 1
    return found[0]


def _violations(tmp_path, status):
    ml = importlib.import_module("manifest_contract")
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"phases": {"coverage": {"status": status}}}))
    return [v for v in ml.ManifestContract(path).validate() if "phases.coverage.status" in v]


def test_schema_has_the_eighteen_values_this_issue_counts():
    assert len(_lifecycle_enum()) == 18


def test_coverage_status_enum_equals_lifecycle_status():
    enum = _coverage_assertion().enum
    assert sorted(enum) == sorted(_lifecycle_enum())


def test_coverage_status_enum_has_no_duplicates():
    enum = _coverage_assertion().enum
    assert len(enum) == len(set(enum))


def test_every_lifecycle_status_is_accepted_for_coverage_except_the_passed_cross_rule(tmp_path):
    for status in _lifecycle_enum():
        if status == "passed":
            continue  # 'passed' also needs outputs.coveragesummary; covered elsewhere
        assert _violations(tmp_path, status) == [], status


def test_infra_error_and_timeout_are_accepted_for_coverage(tmp_path):
    assert _violations(tmp_path, "infra_error") == []
    assert _violations(tmp_path, "timeout") == []


def test_running_and_completed_are_rejected_for_coverage(tmp_path):
    assert _violations(tmp_path, "running") != []
    assert _violations(tmp_path, "completed") != []

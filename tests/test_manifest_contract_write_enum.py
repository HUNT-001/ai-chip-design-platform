"""#114: the write path must enforce the phases.coverage.status enum.

assert_writable is the guard before a manifest update.  A status that is not
in AGENT_A's lifecycle_status vocabulary must be rejected, and a member of it
accepted.  The values are hand-picked from the schema's enum, not read from
the module under test.
"""
import importlib

import pytest


def _mc(tmp_path):
    ml = importlib.import_module("manifest_contract")
    return ml, ml.ManifestContract(tmp_path / "run_manifest.json")


def test_write_rejects_a_coverage_status_outside_the_enum(tmp_path):
    ml, contract = _mc(tmp_path)
    with pytest.raises(ml.ManifestContractError):
        contract.assert_writable({"phases.coverage.status": "not_a_lifecycle_status"})


def test_write_accepts_a_coverage_status_inside_the_enum(tmp_path):
    ml, contract = _mc(tmp_path)
    contract.assert_writable({"phases.coverage.status": "analyzing_coverage"})

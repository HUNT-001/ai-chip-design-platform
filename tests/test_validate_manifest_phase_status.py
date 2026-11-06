"""validate_manifest must hold phases.<name>.status to the lifecycle enum.

It checked only the top-level status, so a phase carrying a retired value
("pass", "completed") validated clean.  Manifests here are built by hand.
"""
import json
from pathlib import Path

import pytest

from AGENT_A import semantic_analyzer as sa
from AGENT_A.semantic_analyzer import validate_manifest

BASE = {"schema_version": "2.1.0", "run_id": "r", "run_dir": "/tmp", "status": "passed"}


def _man(phases):
    return dict(BASE, phases=phases)


@pytest.mark.parametrize("bad", ["completed", "pass"])
def test_a_phase_status_outside_the_enum_is_rejected(bad):
    errs = validate_manifest(_man({"iss": {"status": bad}}))
    assert len(errs) == 1, errs
    assert "phases.iss.status" in errs[0] and repr(bad) in errs[0]


@pytest.mark.parametrize("good", ["passed", "infra_error", "running_iss"])
def test_a_phase_status_in_the_enum_is_accepted(good):
    assert validate_manifest(_man({"iss": {"status": good}})) == []


def test_a_phase_without_a_status_is_accepted():
    assert validate_manifest(_man({"iss": {"records": 3}})) == []


def test_a_non_object_phase_entry_is_ignored():
    assert validate_manifest(_man({"iss": "pass", "rtl": None, "x": ["pass"]})) == []


def test_every_bad_phase_is_reported():
    errs = validate_manifest(_man({"iss": {"status": "pass"}, "rtl": {"status": "passed"},
                                   "compare": {"status": "completed"}}))
    assert len(errs) == 2
    assert any("phases.iss.status" in e for e in errs)
    assert any("phases.compare.status" in e for e in errs)


def test_the_schemas_own_example_still_validates():
    schema = json.loads(Path(sa.__file__).with_name("run_manifest.schema.json")
                        .read_text(encoding="utf-8"))
    assert schema["examples"], "schema carries no example; this would pass vacuously"
    for ex in schema["examples"]:
        assert validate_manifest(ex) == []

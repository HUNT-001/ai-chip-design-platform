"""#21: every AGENT_A schema must accept its own ``examples``.

An example the schema rejects documents a shape nobody can emit.  The check
is a round trip through jsonschema's Draft 2020-12 validator (``unevaluatedProperties``
needs 2020-12), and it fails loudly if jsonschema is not installed.
"""
import json
import pathlib

import jsonschema
import pytest

AGENT_A = pathlib.Path(__file__).resolve().parents[1] / "AGENT_A"
SCHEMAS = ["commitlog.schema.json", "run_manifest.schema.json"]


def _load(name):
    return json.loads((AGENT_A / name).read_text(encoding="utf-8"))


def _cases():
    out = []
    for name in SCHEMAS:
        for i in range(len(_load(name).get("examples", []))):
            out.append((name, i))
    return out


def test_every_schema_has_examples_to_check():
    # Anti-vacuity: an empty or renamed examples block must not pass silently.
    for name in SCHEMAS:
        assert _load(name).get("examples"), f"AGENT_A/{name} has no examples"


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_is_a_valid_2020_12_schema(name):
    jsonschema.Draft202012Validator.check_schema(_load(name))


@pytest.mark.parametrize("name,index", _cases())
def test_example_validates_against_its_own_schema(name, index):
    schema = _load(name)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(schema["examples"][index]),
                    key=lambda e: list(e.absolute_path))
    assert not errors, (
        f"FAIL AGENT_A/{name} example[{index}]: "
        + "; ".join(e.message for e in errors[:3])
    )

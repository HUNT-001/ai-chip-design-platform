"""#22: the CI "Validate JSON schemas" step must validate schemas, not just parse them.

Every first-party ``*.schema.json`` (``_legacy*`` and ``corpus*`` excluded) must be
a valid Draft 2020-12 schema, and each ``examples`` entry must validate against it.

Run as a script by CI (``python tests/test_schema_sweep.py``): it prints the
counts and exits non-zero on any failure or if it found no schemas at all.  The
same sweep runs under pytest, with known-positive and known-negative controls so
a sweep that looks at nothing, or a validator that accepts anything, cannot pass.

Not covered here: comparing the same-named schemas to each other (blocked on #20).
"""
import copy
import pathlib
import sys

import jsonschema
import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED_SCHEMA_COUNT = 8
EXCLUDED_TOP_LEVEL = ("_legacy", "corpus")


def find_schemas(root=REPO_ROOT):
    found = []
    for p in sorted(root.rglob("*.schema.json")):
        rel = p.relative_to(root)
        if rel.parts[0].startswith(EXCLUDED_TOP_LEVEL):
            continue
        found.append(rel)
    return found


def check_schema_document(schema, label):
    """Return a list of FAIL messages for one loaded schema and its examples."""
    failures = []
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        return [f"FAIL {label}: not a valid Draft 2020-12 schema: {exc.message}"]
    validator = jsonschema.Draft202012Validator(schema)
    for i, example in enumerate(schema.get("examples", [])):
        for err in sorted(validator.iter_errors(example), key=lambda e: list(e.absolute_path)):
            failures.append(f"FAIL {label} example[{i}]: {err.message}")
    return failures


def sweep(root=REPO_ROOT):
    """Return (schemas_checked, examples_checked, failures)."""
    import json
    schemas = find_schemas(root)
    examples = 0
    failures = []
    for rel in schemas:
        schema = json.loads((root / rel).read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            failures.append(f"FAIL {rel.as_posix()}: not a JSON object")
            continue
        examples += len(schema.get("examples", []))
        failures.extend(check_schema_document(schema, rel.as_posix()))
    return len(schemas), examples, failures


def main():
    n, ex, failures = sweep()
    if n == 0:
        print("FAIL: found no *.schema.json files; the sweep checked nothing")
        return 1
    for f in failures:
        print(f"  {f}")
    if failures:
        print(f"{len(failures)} schema failure(s) across {n} schema(s)")
        return 1
    print(f"All {n} schema(s) valid ({ex} example(s) checked)")
    return 0


# ── pytest ────────────────────────────────────────────────────────────────────

def test_sweep_finds_the_expected_schemas():
    found = find_schemas()
    assert len(found) >= 1
    assert len(found) == EXPECTED_SCHEMA_COUNT, [p.as_posix() for p in found]


def test_every_first_party_schema_and_example_validates():
    _, _, failures = sweep()
    assert not failures, "\n".join(failures)


def test_validator_rejects_a_bad_schema():
    bad = {"type": "not-a-real-type", "required": "should-be-an-array"}
    failures = check_schema_document(bad, "bad.schema.json")
    assert failures and "not a valid Draft 2020-12 schema" in failures[0]


def test_validator_rejects_a_bad_example():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"const": 1}},
              "unevaluatedProperties": False, "examples": [{"a": 1}, {"a": 2}, {"a": 1, "b": 0}]}
    failures = check_schema_document(copy.deepcopy(schema), "s.json")
    assert any(f.startswith("FAIL s.json example[1]") for f in failures)
    assert any(f.startswith("FAIL s.json example[2]") for f in failures)
    assert not any("example[0]" in f for f in failures)
    schema["examples"] = [{"a": 1}]
    assert check_schema_document(schema, "s.json") == []


def test_ci_schema_step_runs_this_sweep():
    """The step used to json.loads each file and nothing else (#22)."""
    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    runs = [step.get("run", "")
            for job in wf["jobs"].values() for step in job.get("steps", [])
            if step.get("name") == "Validate JSON schemas"]
    assert len(runs) == 1, "expected exactly one 'Validate JSON schemas' step"
    assert "tests/test_schema_sweep.py" in runs[0], runs[0]


if __name__ == "__main__":
    sys.exit(main())

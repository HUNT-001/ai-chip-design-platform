"""Producers must write AGENT_A's lifecycle_status vocabulary into the run manifest.

AGENT_A/run_manifest.schema.json ($defs.lifecycle_status) is the canonical set of
status values.  AGENT_B, C, D and E each grew their own ("pass", "completed",
"error", ...).  This guard scans the producers with ``ast`` for string literals
written to a status key and checks them against the enum read from the schema
file at test time, so the expected values do not come from the code under test.

What counts as a write: a ``"status"`` / ``"<dotted>.status"`` key in a dict that
is an argument of a manifest-writer call, or ``x["status"] = <literal>``.  Values
that are not literals (a variable, a call) are not seen; a conditional of two
literals is.

Ratchet: files in PENDING have not been migrated yet.  The test asserts they STILL
contain a non-canonical literal, so it fails when a pending file is already clean
and the set has to shrink as each agent migrates.
"""
import ast
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "AGENT_A" / "run_manifest.schema.json"

WRITER_CALLS = {
    "patch_manifest",
    "atomic_update_manifest",
    "update_manifest",
    "_write_manifest",
    "write_manifest",
    "_shared_atomic_update_manifest",
}

MIGRATED = ["AGENT_C/run_iss.py"]
PENDING = {
    "AGENT_E/run_compliance.py",
    "AGENT_B/backends/run_rtl.py",
    "AGENT_D/compare_commitlogs.py",
}


def lifecycle_enum():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return set(schema["$defs"]["lifecycle_status"]["enum"])


def _literals(node):
    """String literals a value expression can evaluate to (constants and conditionals)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literals(node.body) + _literals(node.orelse)
    return []


def _is_status_key(node):
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == "status" or node.value.endswith(".status"))
    )


def status_writes(source):
    """Return [(lineno, literal)] for every literal written to a status key."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in WRITER_CALLS:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        for key, value in zip(sub.keys, sub.values):
                            if key is not None and _is_status_key(key):
                                found += [(value.lineno, lit) for lit in _literals(value)]
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_status_key(target.slice):
                    found += [(node.lineno, lit) for lit in _literals(node.value)]
    return sorted(set(found))


def offenders(rel):
    enum = lifecycle_enum()
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    return [(ln, lit) for ln, lit in status_writes(source) if lit not in enum]


# ── the enum itself ──────────────────────────────────────────────────────────

def test_enum_is_read_from_the_schema_and_is_not_empty():
    enum = lifecycle_enum()
    assert {"passed", "failed", "infra_error", "running_iss", "timeout"} <= enum
    assert not ({"pass", "fail", "error", "completed", "running"} & enum)


# ── controls for the scanner ─────────────────────────────────────────────────

def test_scanner_control_finds_a_bad_literal_in_every_write_shape():
    src = (
        "patch_manifest(p, {'status': 'bogus_a'})\n"
        "atomic_update_manifest(p, {'phases.x.status': 'bogus_b', 'phases.x.error': 'ok'})\n"
        "patch_manifest(p, {'status': 'bogus_c' if ok else 'bogus_d'})\n"
        "manifest['status'] = 'bogus_e'\n"
    )
    assert {lit for _, lit in status_writes(src)} == {
        "bogus_a", "bogus_b", "bogus_c", "bogus_d", "bogus_e",
    }


def test_scanner_control_ignores_what_is_not_a_status_write():
    src = (
        "patch_manifest(p, {'phases.x.error': 'bogus', 'note': 'bogus'})\n"
        "other_call(p, {'status': 'bogus'})\n"
        "manifest['kind'] = 'bogus'\n"
        "patch_manifest(p, {'status': build_status})\n"
        "atomic_update_manifest(p, {'status': 'passed'})\n"
    )
    assert [lit for _, lit in status_writes(src)] == ["passed"]


# ── the producers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", MIGRATED)
def test_migrated_producer_writes_only_lifecycle_status(rel):
    bad = offenders(rel)
    assert bad == [], (
        f"{rel} writes status values outside AGENT_A's lifecycle_status: "
        + ", ".join(f"{rel}:{ln} {lit!r}" for ln, lit in bad)
    )


@pytest.mark.parametrize("rel", MIGRATED)
def test_migrated_producer_scan_is_not_vacuous(rel):
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert len(status_writes(source)) >= 15, "scanner no longer sees the producer's writes"


@pytest.mark.parametrize("rel", sorted(PENDING))
def test_pending_producer_still_has_a_noncanonical_literal(rel):
    assert offenders(rel), (
        f"{rel} is already clean: move it from PENDING to MIGRATED so it stays guarded"
    )

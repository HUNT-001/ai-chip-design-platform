"""Producers must write AGENT_A's lifecycle_status vocabulary into the run manifest.

AGENT_A/run_manifest.schema.json ($defs.lifecycle_status) is the canonical set of
status values.  AGENT_B, C, D and E each grew their own ("pass", "completed",
"error", ...).  This guard scans the producers with ``ast`` for string literals
written to a status key and checks them against the enum read from the schema
file at test time, so the expected values do not come from the code under test.

What counts as a write: a ``"status"`` / ``"<dotted>.status"`` key in a dict that
is an argument of a manifest-writer call, ``x["status"] = <literal>``, a dict
assigned to a subscript (``phases["generator"] = {"status": ...}``), or a dict
passed to ``.update()`` on something rooted at ``manifest``.  Values
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
    "commit_manifest",
}

# file -> minimum number of status literals the scanner must still see in it
MIGRATED = {
    "AGENT_C/run_iss.py": 15,
    "AGENT_E/run_compliance.py": 3,
    "AGENT_B/backends/run_rtl.py": 6,
    "AGENT_D/compare_commitlogs.py": 10,
    "AGENT_F/coverage_pipeline.py": 1,
    "AGENT_G/generate_tests.py": 1,
    "AGENT_I/agent_i_litmus.py": 8,
    "AGENT_J/agent_j_cdc.py": 8,
    "AGENT_K/agent_k_perf.py": 8,
    "AGENT_L/agent_l_equiv.py": 8,
    "AGENT_H/agent_h_intent.py": 3,
    "AGENT_H/atomics_verifier.py": 2,
    "AGENT_H/bitmanip_verifier.py": 2,
    "AGENT_H/branch_predictor_verifier.py": 2,
    "AGENT_H/bus_verifier.py": 2,
    "AGENT_H/cache_verifier.py": 2,
    "AGENT_H/contract_dsl.py": 2,
    "AGENT_H/csr_verifier.py": 2,
    "AGENT_H/fp_verifier.py": 2,
    "AGENT_H/peripheral_verifier.py": 2,
    "AGENT_H/pipeline_verifier.py": 2,
    "AGENT_H/privilege_verifier.py": 2,
    "AGENT_H/rv64_atomics_verifier.py": 2,
    "AGENT_H/rv64_verifier.py": 2,
    "AGENT_H/rvc_verifier.py": 2,
    "AGENT_H/security_intel.py": 1,
    "AGENT_H/sv_mmu_verifier.py": 2,
    "AGENT_H/temporal_checker.py": 2,
    "AGENT_H/tlb_verifier.py": 2,
    "AGENT_H/vm_verifier.py": 2,
}
# Nothing is pending; a producer that regresses is caught by MIGRATED instead.
PENDING = set()


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
            if (isinstance(func, ast.Attribute) and func.attr == "update"
                    and any(isinstance(n, ast.Name) and n.id == "manifest"
                            for n in ast.walk(func.value))):
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        for key, value in zip(arg.keys, arg.values):
                            if key is not None and _is_status_key(key):
                                found += [(value.lineno, lit) for lit in _literals(value)]
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                # a phase record built as a dict: phases["x"] = {"status": ...}
                if isinstance(target, ast.Subscript) and isinstance(node.value, ast.Dict):
                    for key, value in zip(node.value.keys, node.value.values):
                        if key is not None and _is_status_key(key):
                            found += [(value.lineno, lit) for lit in _literals(value)]
                if isinstance(target, ast.Subscript) and _is_status_key(target.slice):
                    found += [(node.lineno, lit) for lit in _literals(node.value)]
                # a whole manifest built as a dict literal: manifest = {... "status": ...}
                if (isinstance(target, ast.Name) and target.id == "manifest"
                        and isinstance(node.value, ast.Dict)):
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Dict):
                            for key, value in zip(sub.keys, sub.values):
                                if key is not None and _is_status_key(key):
                                    found += [(value.lineno, lit) for lit in _literals(value)]
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

@pytest.mark.parametrize("rel", sorted(MIGRATED))
def test_migrated_producer_writes_only_lifecycle_status(rel):
    bad = offenders(rel)
    assert bad == [], (
        f"{rel} writes status values outside AGENT_A's lifecycle_status: "
        + ", ".join(f"{rel}:{ln} {lit!r}" for ln, lit in bad)
    )


@pytest.mark.parametrize("rel", sorted(MIGRATED))
def test_migrated_producer_scan_is_not_vacuous(rel):
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert len(status_writes(source)) >= MIGRATED[rel], (
        f"scanner no longer sees {rel}'s writes"
    )


def test_scanner_control_sees_a_manifest_built_as_a_dict_literal():
    src = (
        "manifest = {'phases': {'build': {'status': 'bogus'}}}\n"
        "other = {'status': 'bogus2'}\n"
    )
    assert [lit for _, lit in status_writes(src)] == ["bogus"]


def test_scanner_control_sees_a_phase_dict_and_a_manifest_update():
    src = (
        "phases['gen'] = {'status': 'bogus_a'}\n"
        "manifest['phases']['p'].update({'status': 'bogus_b' if x else 'bogus_c'})\n"
        "other.update({'status': 'bogus_d'})\n"
        "phases['gen'] = {'note': 'bogus_e'}\n"
    )
    assert {lit for _, lit in status_writes(src)} == {"bogus_a", "bogus_b", "bogus_c"}


def test_agent_b_build_status_passed_through_a_variable_is_canonical():
    # `build_status` ends up in phases.build.status; the scanner cannot follow it, so
    # check every literal assigned to it by hand.
    source = (REPO_ROOT / "AGENT_B/backends/run_rtl.py").read_text(encoding="utf-8")
    assigned = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "build_status" for t in node.targets
        ):
            assigned += [(node.lineno, lit) for lit in _literals(node.value)]
    assert len(assigned) >= 2, "build_status is no longer assigned literals; re-read this check"
    bad = [(ln, lit) for ln, lit in assigned if lit not in lifecycle_enum()]
    assert bad == [], f"AGENT_B/backends/run_rtl.py build_status: {bad}"


def test_agent_e_status_passed_through_a_variable_is_canonical():
    # The scanner only sees literals.  AGENT_E also writes `"status": overall_status`,
    # so follow that variable by hand: every literal assigned to it must be a
    # lifecycle_status value, and there must be at least one such assignment.
    source = (REPO_ROOT / "AGENT_E/run_compliance.py").read_text(encoding="utf-8")
    assigned = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "overall_status" for t in node.targets
        ):
            assigned += [(node.lineno, lit) for lit in _literals(node.value)]
    assert assigned, "overall_status is no longer assigned a literal; re-read this check"
    bad = [(ln, lit) for ln, lit in assigned if lit not in lifecycle_enum()]
    assert bad == [], f"AGENT_E/run_compliance.py overall_status: {bad}"


def test_pending_producers_still_have_a_noncanonical_literal():
    # Not parametrized: with nothing pending a parametrized test would be collected
    # as a skip.  An empty PENDING passes; a stale entry fails.
    clean = [rel for rel in sorted(PENDING) if not offenders(rel)]
    assert clean == [], (
        f"{clean} already clean: move from PENDING to MIGRATED so they stay guarded"
    )

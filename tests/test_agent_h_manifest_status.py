"""#103: AGENT_H verifiers must write AGENT_A's lifecycle_status values to the manifest.

Drives run_from_manifest end to end, so the value is the one commit_manifest
actually writes.  Expected values are written by hand from the schema's
vocabulary ("passed" / "failed"), not read back from the code under test.
"""
import json

from AGENT_H import agent_h_intent, cache_verifier

RUNNING = "running_rtl"          # a valid lifecycle_status the producer must leave alone


def _run_dir(tmp_path, pc):
    rec = {"pc": pc, "insn": "0x00000013", "rd": 0, "rd_val": "0x0", "disasm": "addi x0, x0, 0"}
    for name in ("rtl_commit.jsonl", "iss_commit.jsonl"):
        (tmp_path / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(
        {"run_id": "r", "run_dir": str(tmp_path), "status": RUNNING}), encoding="utf-8")
    return manifest


def _after(manifest):
    return json.loads(manifest.read_text(encoding="utf-8"))


def test_a_passing_intent_check_writes_passed_and_leaves_the_run_status(tmp_path):
    manifest = _run_dir(tmp_path, "0x80000000")
    agent_h_intent.run_from_manifest(manifest)
    m = _after(manifest)
    assert m["phases"]["intent_check"]["status"] == "passed"
    assert m["status"] == RUNNING


def test_a_failing_intent_check_writes_failed_for_the_phase_and_the_run(tmp_path):
    manifest = _run_dir(tmp_path, "0x80000002")      # not 4-byte aligned
    agent_h_intent.run_from_manifest(manifest)
    m = _after(manifest)
    assert m["phases"]["intent_check"]["violations"] > 0
    assert m["phases"]["intent_check"]["status"] == "failed"
    assert m["status"] == "failed"


def test_a_passing_cache_check_writes_passed(tmp_path):
    manifest = _run_dir(tmp_path, "0x80000000")
    cache_verifier.run_from_manifest(manifest)
    assert _after(manifest)["phases"]["cache_check"]["status"] == "passed"

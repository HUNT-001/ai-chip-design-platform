"""AGENT_D's main_manifest must record AGENT_A's lifecycle_status values.

Expected values are hand-written from AGENT_A/interfaces.md: any input the
comparator cannot use (no rundir, missing or unparseable commit logs, an invalid
compare_config) is infra_error, and a comparison that ran is passed or failed.
Each case builds the smallest manifest that reaches one write site.
"""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _comparator():
    spec = importlib.util.spec_from_file_location(
        "status_compare_commitlogs", ROOT / "AGENT_D" / "compare_commitlogs.py")
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _entry(seq):
    return json.dumps({"seq": seq, "pc": 0x1000 + 4 * seq, "insn": 0x13})


def _run(tmp_path, manifest, rtl=None, iss=None):
    mod = _comparator()
    out = tmp_path / "outputs"
    out.mkdir()
    if rtl is not None:
        (out / "rtlcommit.jsonl").write_text(rtl, encoding="utf-8")
    if iss is not None:
        (out / "isscommitlog.jsonl").write_text(iss, encoding="utf-8")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"rundir": str(tmp_path), "seed": 1, **manifest}), encoding="utf-8")
    rc = mod.main_manifest(path)
    return rc, json.loads(path.read_text(encoding="utf-8"))


def test_missing_rundir_records_infra_error(tmp_path):
    mod = _comparator()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"seed": 1}), encoding="utf-8")
    assert mod.main_manifest(path) == mod.EXIT_CONFIG
    assert json.loads(path.read_text(encoding="utf-8"))["phases"]["compare"]["status"] == "infra_error"


def test_missing_commit_logs_record_infra_error_in_phase_and_top_level(tmp_path):
    rc, m = _run(tmp_path, {})
    assert rc == 3
    assert m["phases"]["compare"]["status"] == "infra_error"
    assert m["status"] == "infra_error"


def test_invalid_compare_config_records_infra_error(tmp_path):
    rc, m = _run(tmp_path, {"compare_config": {"ignored_csrs": ["zz"]}},
                 rtl=_entry(1) + "\n", iss=_entry(1) + "\n")
    assert rc == 3
    assert m["phases"]["compare"]["status"] == "infra_error"


def test_unparseable_commit_log_records_infra_error_in_phase_and_top_level(tmp_path):
    rc, m = _run(tmp_path, {"compare_config": {"max_parse_errors": 0}},
                 rtl="{not json\n", iss=_entry(1) + "\n")
    assert rc == 2
    assert m["phases"]["compare"]["status"] == "infra_error"
    assert m["status"] == "infra_error"


def test_identical_logs_record_passed(tmp_path):
    rc, m = _run(tmp_path, {}, rtl=_entry(1) + "\n", iss=_entry(1) + "\n")
    assert rc == 0
    assert m["phases"]["compare"]["status"] == "passed"
    assert m["status"] == "passed"

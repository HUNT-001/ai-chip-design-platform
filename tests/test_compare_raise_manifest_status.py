"""#111: when compare() raises, the compare phase must not be left stale.

The manifest is handed over with phases.compare.status "running"; the run
then fails inside compare() with an unexpected error.  The exit code says
infrastructure failure (EXIT_INFRA), so the manifest must say infra_error
too, as it already does for the other infrastructure failures in
main_manifest.  Expected values come from the issue and AGENT_A's
lifecycle_status, not from the code under test.
"""
import json

import compare_commitlogs as cc


def test_compare_raising_leaves_infra_error_not_a_stale_status(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "rtlcommit.jsonl").write_text("{}\n", encoding="utf-8")
    (out / "isscommitlog.jsonl").write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "run_manifest.json"
    manifest.write_text(json.dumps({
        "rundir": str(tmp_path),
        "phases": {"compare": {"status": "running"}},
    }), encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("comparator blew up")

    monkeypatch.setattr(cc, "compare", boom)

    code = cc.main_manifest(manifest)

    after = json.loads(manifest.read_text(encoding="utf-8"))
    assert code == cc.EXIT_INFRA
    assert after["phases"]["compare"]["status"] == "infra_error"
    assert after["status"] == "infra_error"

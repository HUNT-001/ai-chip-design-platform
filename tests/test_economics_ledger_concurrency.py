"""#32: ``EconomicsEngine.save_ledger`` is an unlocked read-modify-write.

Several verification campaigns share one ``verification_ledger.json`` (it lives
next to the run directories). Each ``save_ledger`` loads it, appends, and writes
it back, so two campaigns finishing together lose one another's entry, and two
writers that both find the file unreadable race on the ``.corrupt`` name.

The contract, from the issue and not from the handler:

* N separate processes each saving distinct campaigns to one ledger leave every
  campaign in it, and none of the saves raises;
* two processes that both meet an unreadable ledger leave exactly one backup
  holding the original bytes, both campaigns in the fresh ledger, and no error;
* a save that fails part-way leaves the previous ledger byte-for-byte, with no
  temp file behind.

Anti-vacuity. The workers are real ``subprocess`` children, and the test checks
the process ids they report are all different from each other and from this
one. A reference writer that holds the shared lock around load-modify-save
runs through the same harness and must lose nothing: if that control ever
fails, the harness is what is broken, not the engine. The lock is the one the
manifest writers use (``ava_shared.manifest_io._manifest_lock``), taken on
``<ledger>.lock``.
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Runs in a child process. argv: repo root, ledger dir, worker id, saves,
# mode, gate file, result file. Waits for the gate so the children start
# together instead of one finishing before the next has launched.
WORKER = textwrap.dedent("""
    import importlib.util, json, os, sys, time
    from pathlib import Path
    repo, root, wid, saves, mode, gate, out = sys.argv[1:8]
    saves = int(saves)
    sys.path.append(repo)
    sys.path.insert(0, str(Path(repo) / "AGENT_H"))  # as when run as a script
    spec = importlib.util.spec_from_file_location(
        "ee_worker", Path(repo) / "AGENT_H" / "economics_engine.py")
    ee = importlib.util.module_from_spec(spec)
    sys.modules["ee_worker"] = ee
    spec.loader.exec_module(ee)
    run = Path(root) / ("run%s" % wid)
    run.mkdir(exist_ok=True)
    ledger = Path(root) / "verification_ledger.json"
    engine = ee.EconomicsEngine(run, {"run_id": wid})
    while not os.path.exists(gate):
        time.sleep(0.001)
    errors = []
    for i in range(saves):
        campaign = {"id": "%s-%d" % (wid, i), "bugs_found": 1,
                    "tests_run": 1, "computed_at": "t"}
        try:
            if mode == "engine":
                engine.save_ledger(campaign)
            else:  # reference: the same sequence, serialised by the shared lock
                from ava_shared.manifest_io import _manifest_lock
                with _manifest_lock(ledger):
                    try:
                        data = json.loads(ledger.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        data = {"campaigns": []}
                    data["campaigns"].append(campaign)
                    tmp = ledger.with_name(ledger.name + ".ref%s.tmp" % wid)
                    tmp.write_text(json.dumps(data), encoding="utf-8")
                    os.replace(tmp, ledger)
        except Exception as exc:
            errors.append(repr(exc))
    Path(out).write_text(json.dumps({"pid": os.getpid(), "errors": errors}))
""")


def _run_workers(root, n, saves, mode, label="w"):
    """Start *n* workers together; return their reports."""
    script = root / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    gate = root / f"go_{label}"
    outs = [root / f"out_{label}{w}.json" for w in range(n)]
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(REPO), str(root), f"{label}{w}",
             str(saves), mode, str(gate), str(outs[w])],
            stderr=subprocess.PIPE, text=True)
        for w in range(n)
    ]
    time.sleep(1.0)            # let every child get past its imports
    gate.write_text("go", encoding="utf-8")
    reports = []
    for p, out in zip(procs, outs):
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, err[-2000:]
        reports.append(json.loads(out.read_text(encoding="utf-8")))
    pids = {r["pid"] for r in reports}
    assert len(pids) == n and os.getpid() not in pids, (
        "workers were not separate processes: %s vs %s" % (pids, os.getpid()))
    return reports


def _ids(ledger):
    return sorted(c["id"] for c in json.loads(ledger.read_text(encoding="utf-8"))["campaigns"])


N, SAVES = 4, 15


@pytest.mark.parametrize("mode", ["reference_locked", "engine"])
def test_concurrent_processes_lose_no_campaign(tmp_path, mode):
    reports = _run_workers(tmp_path, N, SAVES, mode)
    errors = [e for r in reports for e in r["errors"]]
    expected = sorted(f"w{w}-{i}" for w in range(N) for i in range(SAVES))
    ledger = tmp_path / "verification_ledger.json"
    assert errors == []
    assert _ids(ledger) == expected


CORRUPT = b"{corrupt"
TRIALS = 12


def test_two_processes_on_an_unreadable_ledger_keep_one_backup_and_both_campaigns(tmp_path):
    for t in range(TRIALS):
        root = tmp_path / f"t{t}"
        root.mkdir()
        ledger = root / "verification_ledger.json"
        ledger.write_bytes(CORRUPT)
        reports = _run_workers(root, 2, 1, "engine")
        assert [e for r in reports for e in r["errors"]] == [], f"trial {t}"
        assert _ids(ledger) == ["w0-0", "w1-0"], f"trial {t}"
        backups = sorted(p for p in root.iterdir() if ".corrupt" in p.name)
        assert [b.read_bytes() for b in backups] == [CORRUPT], (
            f"trial {t}: backups {[b.name for b in backups]}")


def test_failed_save_leaves_the_previous_ledger_and_no_temp_file(tmp_path, monkeypatch):
    import importlib
    ee = importlib.import_module("AGENT_H.economics_engine")

    run = tmp_path / "run"
    run.mkdir()
    ledger = tmp_path / "verification_ledger.json"
    engine = ee.EconomicsEngine(run, manifest={})
    engine.save_ledger({"id": "first", "bugs_found": 1, "tests_run": 1, "computed_at": "t"})
    before = ledger.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ee.json, "dump", boom)
    monkeypatch.setattr(ee.json, "dumps", boom)
    with pytest.raises(OSError):
        engine.save_ledger({"id": "second", "bugs_found": 1, "tests_run": 1, "computed_at": "t"})
    assert ledger.read_bytes() == before
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name not in (ledger.name, ledger.name + ".lock", "run")]
    assert leftovers == []

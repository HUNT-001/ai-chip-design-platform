"""Concurrent manifest updates must not be lost (#23).

Several agents update the same run manifest, and nothing stops two of them
from doing it at once. The contract of ``atomic_update_manifest`` is that an
update which returns normally is in the manifest afterwards. Two copies of the
function exist (AGENT_C and AGENT_D); both are held to that contract here.

The existing thread test in AGENT_D/test_comparator.py only asserts that the
file still parses as JSON, which a manifest that lost most of its updates
satisfies. These tests count the updates instead.

Each writer runs in its own process, because that is how agents share a
manifest in practice.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

COPIES = {
    "AGENT_C": REPO / "AGENT_C" / "run_iss.py",
    "AGENT_D": REPO / "AGENT_D" / "compare_commitlogs.py",
}

WRITERS = 2
UPDATES_PER_WRITER = 100

# One writer process: load the module by path, wait for the shared start time,
# apply UPDATES_PER_WRITER updates to distinct keys, and report which calls
# returned normally and when the writer was active.
WORKER = textwrap.dedent("""
    import importlib.util, json, sys, time
    from pathlib import Path

    src, manifest, wid, n, start_at = sys.argv[1:6]
    spec = importlib.util.spec_from_file_location("copy_under_test", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    sys.path.insert(0, str(Path(src).parent))
    spec.loader.exec_module(mod)

    while time.time() < float(start_at):
        pass
    began = time.time()
    ok, raised = [], []
    for i in range(int(n)):
        try:
            mod.atomic_update_manifest(Path(manifest), {f"w{wid}.k{i}": i})
            ok.append(i)
        except Exception as exc:
            raised.append(f"{type(exc).__name__}: {exc}")
    print(json.dumps({"ok": ok, "raised": raised,
                      "began": began, "ended": time.time()}))
""")


def _run_writers(src: Path, manifest: Path) -> list[dict]:
    start_at = time.time() + 1.0          # let every interpreter finish importing
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(src), str(manifest),
             str(w), str(UPDATES_PER_WRITER), str(start_at)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for w in range(WRITERS)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0 and out.strip(), f"writer crashed:\n{err[-2000:]}"
        results.append(json.loads(out.strip().splitlines()[-1]))
    return results


@pytest.mark.parametrize("copy", sorted(COPIES))
def test_concurrent_updates_are_all_kept(copy, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"run_id": "r"}), encoding="utf-8")

    results = _run_writers(COPIES[copy], manifest)

    # The control needs a control: if the writers never overlapped, a pass
    # would say nothing about concurrency.
    (a, b) = results
    assert a["began"] < b["ended"] and b["began"] < a["ended"], \
        "writers did not overlap in time; this run did not test concurrency"

    raised = [r for res in results for r in res["raised"]]
    assert raised == [], f"{len(raised)} updates raised, first: {raised[0]}"

    final = json.loads(manifest.read_text(encoding="utf-8"))
    missing = [
        f"w{w}.k{i}"
        for w, res in enumerate(results)
        for i in res["ok"]
        if final.get(f"w{w}", {}).get(f"k{i}") != i
    ]
    assert missing == [], (
        f"{len(missing)} of {WRITERS * UPDATES_PER_WRITER} updates returned "
        f"normally but are not in the manifest (first: {missing[:5]})"
    )
    assert final["run_id"] == "r"


@pytest.mark.parametrize("copy", sorted(COPIES))
def test_sequential_updates_are_all_kept(copy, tmp_path):
    """Baseline: with one writer every update must land. If this fails, the
    concurrent test's failure says nothing about concurrency."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"run_id": "r"}), encoding="utf-8")
    res = _run_writers_single(COPIES[copy], manifest)
    final = json.loads(manifest.read_text(encoding="utf-8"))
    assert res["raised"] == []
    assert final["w0"] == {f"k{i}": i for i in range(UPDATES_PER_WRITER)}


def _run_writers_single(src: Path, manifest: Path) -> dict:
    p = subprocess.run(
        [sys.executable, "-c", WORKER, str(src), str(manifest),
         "0", str(UPDATES_PER_WRITER), str(time.time())],
        capture_output=True, text=True, timeout=120,
    )
    assert p.returncode == 0 and p.stdout.strip(), f"writer crashed:\n{p.stderr[-2000:]}"
    return json.loads(p.stdout.strip().splitlines()[-1])

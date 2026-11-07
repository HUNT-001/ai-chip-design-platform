"""Contract for the shared ``ava_shared.manifest_io.atomic_update_manifest`` (#34).

The two existing copies (AGENT_C, AGENT_D) disagree on ordinary inputs.  The
shared implementation has one contract, written down here from the issue:

* missing manifest                      -> FileNotFoundError
* corrupt manifest                      -> json.JSONDecodeError, file untouched
* dotted key through a scalar           -> ManifestPathConflict (a ValueError),
                                           file untouched
* nested dotted update                  -> applied
* concurrent writers, distinct keys     -> every update survives

Each test imports the module inside its body so a missing module fails each
test individually instead of failing collection.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_missing_manifest_raises_file_not_found(tmp_path):
    from ava_shared.manifest_io import atomic_update_manifest

    with pytest.raises(FileNotFoundError):
        atomic_update_manifest(tmp_path / "manifest.json", {"a": 1})


def test_corrupt_manifest_raises_and_is_left_byte_identical(tmp_path):
    from ava_shared.manifest_io import atomic_update_manifest

    p = tmp_path / "manifest.json"
    p.write_bytes(b"{not json")
    with pytest.raises(json.JSONDecodeError):
        atomic_update_manifest(p, {"a": 1})
    assert p.read_bytes() == b"{not json"


def test_dotted_key_through_scalar_raises_conflict_and_leaves_file(tmp_path):
    from ava_shared.manifest_io import ManifestPathConflict, atomic_update_manifest

    assert issubclass(ManifestPathConflict, ValueError)
    p = tmp_path / "manifest.json"
    original = b'{"phases": "legacy"}'
    p.write_bytes(original)
    with pytest.raises(ManifestPathConflict):
        atomic_update_manifest(p, {"phases.iss.status": "done"})
    assert p.read_bytes() == original


def test_conflict_midway_through_updates_applies_none_of_them(tmp_path):
    from ava_shared.manifest_io import ManifestPathConflict, atomic_update_manifest

    p = tmp_path / "manifest.json"
    original = b'{"phases": "legacy", "ok": 0}'
    p.write_bytes(original)
    with pytest.raises(ManifestPathConflict):
        atomic_update_manifest(p, {"ok": 1, "phases.iss.status": "done"})
    assert p.read_bytes() == original


def test_nested_dotted_update_is_applied(tmp_path):
    from ava_shared.manifest_io import atomic_update_manifest

    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"keep": 1, "phases": {"iss": {"status": "running"}}}), encoding="utf-8")
    atomic_update_manifest(
        p, {"phases.iss.status": "completed", "outputs.iss_commitlog": "a.jsonl"}
    )
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "keep": 1,
        "phases": {"iss": {"status": "completed"}},
        "outputs": {"iss_commitlog": "a.jsonl"},
    }


def test_threads_updating_distinct_keys_all_survive(tmp_path):
    from ava_shared.manifest_io import atomic_update_manifest

    p = tmp_path / "manifest.json"
    p.write_text("{}", encoding="utf-8")
    threads_n, per_thread = 4, 25
    errors = []

    def work(wid):
        try:
            for i in range(per_thread):
                atomic_update_manifest(p, {f"w{wid}.k{i}": i})
        except Exception as exc:  # reported below, never swallowed
            errors.append(repr(exc))

    threads = [threading.Thread(target=work, args=(w,)) for w in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    expected = {f"w{w}": {f"k{i}": i for i in range(per_thread)} for w in range(threads_n)}
    assert json.loads(p.read_text(encoding="utf-8")) == expected


def test_importable_from_a_script_outside_the_repo_with_root_appended(tmp_path):
    """The bootstrap callers will use: append the repo root, then import.

    Runs in a subprocess with cwd elsewhere and PYTHONPATH removed, so it
    cannot pass through pytest's ``pythonpath`` or conftest.
    """
    script_dir = tmp_path / "script_dir"
    script_dir.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"phases": {}}', encoding="utf-8")
    script = script_dir / "caller.py"
    script.write_text(textwrap.dedent("""
        import sys
        from pathlib import Path
        sys.path.append(sys.argv[1])
        from ava_shared.manifest_io import atomic_update_manifest
        atomic_update_manifest(Path(sys.argv[2]), {"phases.iss.status": "done"})
    """), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    proc = subprocess.run(
        [sys.executable, str(script), str(REPO), str(manifest)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(manifest.read_text(encoding="utf-8")) == {"phases": {"iss": {"status": "done"}}}


def test_update_preserves_the_manifest_file_mode(tmp_path):
    import stat

    from ava_shared.manifest_io import atomic_update_manifest

    p = tmp_path / "manifest.json"
    p.write_text('{"a": 0}', encoding="utf-8")
    if sys.platform == "win32":
        atomic_update_manifest(p, {"a": 1})
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
        return
    os.chmod(p, 0o644)
    atomic_update_manifest(p, {"a": 1})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}

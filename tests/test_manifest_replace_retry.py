"""os.replace on the manifest can fail transiently on Windows.

When a reader holds the manifest open, os.replace(tmp, manifest) raises
PermissionError ([WinError 5]). The failure is transient, so
atomic_update_manifest must retry it a bounded number of times and only then
give up. The tests simulate the Windows behaviour on any platform by making
os.replace raise PermissionError a set number of times.
"""
import json
import os
import time

import pytest

from ava_shared import manifest_io


def _manifest(tmp_path):
    path = tmp_path / "run_manifest.json"
    path.write_text(json.dumps({"phases": {"iss": {"status": "pending"}}}), encoding="utf-8")
    return path


def test_replace_retried_after_transient_permission_error(tmp_path, monkeypatch):
    path = _manifest(tmp_path)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "[WinError 5] Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(manifest_io.os, "replace", flaky_replace)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    manifest_io.atomic_update_manifest(path, {"phases.iss.status": "passed"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "phases": {"iss": {"status": "passed"}}
    }
    assert calls["n"] == 3
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_persistent_permission_error_propagates_and_leaves_no_tmp(tmp_path, monkeypatch):
    path = _manifest(tmp_path)

    def always_denied(src, dst):
        raise PermissionError(13, "[WinError 5] Access is denied")

    monkeypatch.setattr(manifest_io.os, "replace", always_denied)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    with pytest.raises(PermissionError):
        manifest_io.atomic_update_manifest(path, {"phases.iss.status": "passed"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "phases": {"iss": {"status": "pending"}}
    }
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []

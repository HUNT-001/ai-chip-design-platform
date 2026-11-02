"""#39: nine AGENT_H run_from_manifest entry points must treat an unreadable
manifest as "could not verify" (3), like the 50 verifiers fixed in #36.

Written from the issue: missing/unreadable -> ERROR naming the path, return 3;
corrupt JSON -> same; programming errors (TypeError) are not swallowed.
"""
import importlib
import json
import logging
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

NINE = [
    "agent_h_intent", "confidence_scorer", "contract_dsl", "cross_domain",
    "economics_engine", "explainer", "formal_fuzzer", "security_intel",
    "temporal_checker",
]


def _mod(name):
    return importlib.import_module("AGENT_H." + name)


@pytest.mark.parametrize("name", NINE)
def test_missing_manifest_returns_3_and_logs_error(name, tmp_path, caplog):
    path = tmp_path / "nonexistent" / "manifest.json"
    with caplog.at_level(logging.ERROR):
        rc = _mod(name).run_from_manifest(path)
    assert rc == 3
    assert any(r.levelno == logging.ERROR and str(path) in r.getMessage()
               for r in caplog.records), "no ERROR record naming the manifest path"


@pytest.mark.parametrize("name", NINE)
def test_corrupt_manifest_returns_3_and_logs_error(name, tmp_path, caplog):
    path = tmp_path / "manifest.json"
    path.write_text("{not json")
    with caplog.at_level(logging.ERROR):
        rc = _mod(name).run_from_manifest(path)
    assert rc == 3
    assert any(r.levelno == logging.ERROR and str(path) in r.getMessage()
               for r in caplog.records), "no ERROR record naming the manifest path"


@pytest.mark.parametrize("name", NINE)
def test_programming_error_in_the_read_path_propagates(name, tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"run_dir": str(tmp_path)}))

    def boom(*a, **k):
        raise TypeError("bug in the read path")

    monkeypatch.setattr(json, "load", boom)
    with pytest.raises(TypeError):
        _mod(name).run_from_manifest(path)

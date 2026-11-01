"""#76: ``EconomicsEngine.save_ledger`` must not silently destroy an unreadable ledger.

The ledger is the cross-run record. When the existing file cannot be read
(truncated JSON, wrong shape, an I/O error) the old handler did ``pass`` and the
file was then overwritten with a ledger holding only the current campaign: the
history was gone and nothing said so.

The contract, from the issue and not from the handler:

* an unreadable ledger (OSError, JSON error, wrong shape) is moved aside to a
  non-clashing ``verification_ledger.json.corrupt[.N]`` byte-for-byte, a WARNING
  names both paths, and a fresh ledger holds the current campaign;
* a missing ledger is the normal first run: no backup, no warning;
* a valid ledger just appends, with no backup and no warning;
* a programming error (TypeError) in the read path propagates;
* if the move itself fails, ``save_ledger`` raises and leaves the original
  untouched. It never falls back to overwriting.
"""
import importlib
import json
import logging
import os
import shutil

import pytest

ee = importlib.import_module("AGENT_H.economics_engine")

CAMPAIGN = {"bugs_found": 1, "tests_run": 2, "computed_at": "t"}
CORRUPT = b"{corrupt"


@pytest.fixture
def paths(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    return run, tmp_path / "verification_ledger.json"


def _save(run, campaign=CAMPAIGN):
    return ee.EconomicsEngine(run, manifest={}).save_ledger(campaign)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_corrupt_ledger_is_preserved_warned_and_replaced(paths, caplog):
    run, ledger = paths
    ledger.write_bytes(CORRUPT)
    with caplog.at_level(logging.WARNING, logger=ee.logger.name):
        _save(run)
    backup = ledger.with_name(ledger.name + ".corrupt")
    assert backup.exists() and backup.read_bytes() == CORRUPT
    warns = _warnings(caplog)
    assert warns, "no WARNING for a ledger that was moved aside"
    text = " ".join(r.getMessage() for r in warns)
    assert str(ledger) in text and str(backup) in text
    assert json.loads(ledger.read_text())["campaigns"] == [CAMPAIGN]


def test_second_corrupt_event_does_not_clobber_the_first(paths):
    run, ledger = paths
    ledger.write_bytes(CORRUPT)
    _save(run)
    second = b"{also corrupt"
    ledger.write_bytes(second)
    _save(run)
    first_bak = ledger.with_name(ledger.name + ".corrupt")
    second_bak = ledger.with_name(ledger.name + ".corrupt.1")
    assert first_bak.read_bytes() == CORRUPT
    assert second_bak.exists() and second_bak.read_bytes() == second
    assert json.loads(ledger.read_text())["campaigns"] == [CAMPAIGN]


@pytest.mark.parametrize("body", [
    b'{"campaigns": 3}',
    b'{"campaigns": "x"}',
    b'[]',
])
def test_valid_json_of_the_wrong_shape_is_treated_as_corrupt(paths, caplog, body):
    run, ledger = paths
    ledger.write_bytes(body)
    with caplog.at_level(logging.WARNING, logger=ee.logger.name):
        _save(run)
    backup = ledger.with_name(ledger.name + ".corrupt")
    assert backup.exists() and backup.read_bytes() == body
    assert _warnings(caplog)
    assert json.loads(ledger.read_text())["campaigns"] == [CAMPAIGN]


def test_valid_ledger_appends_without_backup_or_warning(paths, caplog):
    run, ledger = paths
    _save(run, {"bugs_found": 5, "tests_run": 7, "computed_at": "t0"})
    with caplog.at_level(logging.WARNING, logger=ee.logger.name):
        _save(run)
    data = json.loads(ledger.read_text())
    assert [c["computed_at"] for c in data["campaigns"]] == ["t0", "t"]
    assert data["total_bugs_found"] == 6
    assert not list(ledger.parent.glob("*.corrupt*"))
    assert not _warnings(caplog)


def test_missing_ledger_is_created_without_backup_or_warning(paths, caplog):
    run, ledger = paths
    assert not ledger.exists()
    with caplog.at_level(logging.WARNING, logger=ee.logger.name):
        _save(run)
    assert json.loads(ledger.read_text())["campaigns"] == [CAMPAIGN]
    assert not list(ledger.parent.glob("*.corrupt*"))
    assert not _warnings(caplog)


def test_programming_error_in_the_read_path_propagates(paths, monkeypatch):
    run, ledger = paths
    _save(run)                       # a valid ledger, so the read path is reached
    before = ledger.read_bytes()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise TypeError("programming error")

    monkeypatch.setattr(ee.json, "load", boom)
    with pytest.raises(TypeError):
        _save(run)
    assert calls, "the stub was never reached: setup error, not a pass"
    assert ledger.read_bytes() == before
    assert not list(ledger.parent.glob("*.corrupt*"))


def test_failing_move_raises_and_leaves_the_original_untouched(paths, monkeypatch):
    run, ledger = paths
    ledger.write_bytes(CORRUPT)

    def refuse(*a, **k):
        raise OSError("cannot move")

    for mod, name in ((os, "rename"), (os, "replace"), (shutil, "move")):
        monkeypatch.setattr(mod, name, refuse)
    with pytest.raises(OSError):
        _save(run)
    assert ledger.read_bytes() == CORRUPT
    assert not list(ledger.parent.glob("*.corrupt*"))

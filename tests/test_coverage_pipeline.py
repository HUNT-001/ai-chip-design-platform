#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_coverage_pipeline.py — regression tests for AGENT_F/coverage_pipeline.py
=============================================================================

This module did not exist before. That is the substantive finding behind
issue #3: the manifest-mode trend-tracking path had no tests at all, so a call
with a nonexistent keyword argument shipped, raised TypeError on every single
invocation, and was caught by a handler written for "the database is
unavailable". The feature silently no-opped. No exception propagated, no
non-zero exit code, nothing in the manifest recorded that it had happened.

Two distinct defects are covered here, and they are worth keeping separate:

  1. the wrong call     -- db.record(metrics, run_id=...) against a signature
                           of record(metrics, seed=0, bug_count=0)
  2. the wrong handler  -- except (DatabaseError, Exception), which is just
                           `except Exception`, swallowing programming errors
                           alongside genuine database outages

Fixing only (1) would leave the next signature error equally invisible. (2) is
the reason a one-line bug survived to production, and it is the more important
of the two.

Note the shape of the test in ``test_record_actually_inserts_a_row``: it asserts
the database GAINED A ROW, not merely that no exception was raised. An
"assert it doesn't throw" test passes against the broken code, because the
broken code is precisely a swallowed throw.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

# ── Loaded by PATH, deliberately, rather than by putting AGENT_F on sys.path ──
#
# This file originally lived in AGENT_F/ alongside the module it tests, which is
# where AGENT_C and AGENT_D keep theirs.  That broke two unrelated tests, and
# the reason is worth recording: `ava_patched.py` exists BOTH at the repo root
# and in AGENT_F/, and the AGENT_F copy is older.  Placing a test file in
# AGENT_F/ makes pytest prepend that directory to sys.path under the default
# import mode, so `import ava_patched` anywhere in the session silently
# resolved to the stale copy and tests/test_agents.py failed with
# "AVA.__init__() got an unexpected keyword argument 'enable_extended'".
#
# Nothing was wrong with either test.  A new file in one directory changed what
# `import` meant for every other file in the run.  Loading the module under
# test by explicit path keeps that blast radius at zero, and living in tests/
# means CI actually collects these -- a regression test CI does not run is not
# a regression test.
#
# The duplicate module name is a real repo hazard and is not fixed here; see
# the follow-up issue on module-name collisions (ava_patched x2,
# CoverageDatabase x2).
_PIPELINE = (Path(__file__).resolve().parents[1]
             / "AGENT_F" / "coverage_pipeline.py")
_spec = importlib.util.spec_from_file_location("agent_f_coverage_pipeline",
                                               _PIPELINE)
coverage_pipeline = importlib.util.module_from_spec(_spec)
# Register BEFORE exec_module: @dataclass resolves annotations via
# sys.modules[cls.__module__], which is None for a module that is mid-exec and
# not yet registered.  Omitting this fails at the first dataclass with a bare
# "'NoneType' object has no attribute '__dict__'".
sys.modules[_spec.name] = coverage_pipeline
_spec.loader.exec_module(coverage_pipeline)

CoverageDatabase        = coverage_pipeline.CoverageDatabase
CoverageMetrics         = coverage_pipeline.CoverageMetrics
DatabaseError           = coverage_pipeline.DatabaseError
_is_recoverable_db_error = coverage_pipeline._is_recoverable_db_error


def _metrics(run_id: str = "seed_42") -> CoverageMetrics:
    m = CoverageMetrics()
    m.run_id = run_id
    return m


class TestRecordSignature(unittest.TestCase):
    """Issue #3, defect 1 — the call site did not match the signature."""

    def test_record_accepts_the_call_the_pipeline_makes(self):
        """Mirrors _run_manifest_mode() exactly.

        Written as the call site calls it, so that a future change to either
        one breaks this test rather than silently diverging again.
        """
        db = CoverageDatabase(":memory:")
        db.record(_metrics("seed_42"))          # must not raise

    def test_record_actually_inserts_a_row(self):
        """The assertion that matters.

        A test asserting only "no exception" passes against the broken code:
        the TypeError was raised and then swallowed. Only checking that the
        row landed distinguishes "it worked" from "it failed quietly".
        """
        db = CoverageDatabase(":memory:")
        db.record(_metrics("seed_7"), bug_count=3)
        rows = list(db._conn.execute(
            "SELECT run_id, bug_count FROM runs WHERE run_id = ?", ("seed_7",)))
        self.assertEqual(len(rows), 1, "record() did not insert a row")
        self.assertEqual(rows[0][1], 3)

    def test_run_id_travels_on_the_metrics_not_as_a_kwarg(self):
        """`record` takes the run identifier from CoverageMetrics.run_id.

        The broken call passed it as a keyword. This pins where the identifier
        is supposed to come from so the same mistake is not re-made.
        """
        db = CoverageDatabase(":memory:")
        db.record(_metrics("from_metrics"))
        stored = list(db._conn.execute("SELECT run_id FROM runs"))[0][0]
        self.assertEqual(stored, "from_metrics")


class TestDatabaseErrorPolicy(unittest.TestCase):
    """Issue #3, defect 2 — the handler that made a crash invisible.

    `except (DatabaseError, Exception)` is exactly `except Exception`: the
    first member is redundant and the second catches everything. A missing
    database file and a misspelled keyword argument are not the same kind of
    event, and treating them identically is what turned a one-line bug into a
    silently disabled feature.
    """

    def test_genuine_database_problems_are_recoverable(self):
        self.assertTrue(_is_recoverable_db_error(DatabaseError("disk gone")))
        self.assertTrue(_is_recoverable_db_error(sqlite3.OperationalError("locked")))
        self.assertTrue(_is_recoverable_db_error(OSError("no such file")))

    def test_programming_errors_are_not_recoverable(self):
        """These must propagate. A TypeError from a bad call signature is a
        defect in this repository, not a condition of the user's environment,
        and degrading gracefully in response to it hides the defect."""
        self.assertFalse(_is_recoverable_db_error(
            TypeError("record() got an unexpected keyword argument 'run_id'")))
        self.assertFalse(_is_recoverable_db_error(AttributeError("no attr")))
        self.assertFalse(_is_recoverable_db_error(NameError("undefined")))

    def test_the_original_bug_would_now_be_visible(self):
        """The exact exception from issue #3, classified.

        If this ever returns True again, the manifest-mode trend feature can
        silently disable itself once more.
        """
        try:
            CoverageDatabase(":memory:").record(_metrics(), run_id="seed_42")
        except TypeError as exc:
            self.assertFalse(
                _is_recoverable_db_error(exc),
                "a bad call signature is being treated as 'DB unavailable'")
        else:
            # The signature grew a run_id parameter at some point. That is a
            # legitimate change, but then the call site and this test should
            # have been updated together.
            self.skipTest("record() now accepts run_id; update the call site "
                          "and this test together")


if __name__ == "__main__":
    unittest.main(verbosity=2)

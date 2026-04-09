#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_comparator.py — AVA Contract Compliance Test Suite
========================================================
Comprehensive unit and integration tests for ``compare_commitlogs.py``
and ``bug_hypothesis.py``.

Covers:
  1. All 11 AVA canonical error codes are present and fire correctly
  2. Exit codes 0/1/2/3 map to the right conditions
  3. ``reprocmd`` / ``comparator_repro_cmd`` present in every bug report
  4. AVA manifest mode: pass / fail / config-error / infra-error paths
  5. Atomic write helpers (crash safety)
  6. Streaming memory footprint (delta window)
  7. Threaded reader: both readers complete and errors surface
  8. Hypothesis engine: all 11 codes yield at least one hypothesis
  9. Bug report schema version and required fields
  10. CLI integration tests

Run::

    python test_comparator.py
    python test_comparator.py -v          # verbose
    python test_comparator.py TestExitCodes  # run one class
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

# ── resolve sibling modules ────────────────────────────────────────────────
_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE))

from compare_commitlogs import (
    CommitEntry,
    CompareConfig,
    CompareResult,
    MismatchType,
    Severity,
    atomic_update_manifest,
    atomic_write,
    compare,
    compare_logs,
    compare_logs_batch,
    main,
    main_manifest,
    BatchEntry,
    ConfigError,
    LogFormatError,
    EXIT_PASS,
    EXIT_MISMATCH,
    EXIT_INFRA,
    EXIT_CONFIG,
)
from bug_hypothesis import HypothesisEngine, generate_hypotheses

# ═══════════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ═══════════════════════════════════════════════════════════════════════════════

def _mk(
    step: int, pc: int, instr: int = 0x00000013, *,
    trap:       bool           = False,
    rd:         Optional[int]  = None,
    rd_val:     Optional[int]  = None,
    csr_writes: Optional[list] = None,
    mem_op:     Optional[str]  = None,
    mem_addr:   Optional[int]  = None,
    mem_val:    Optional[int]  = None,
    mem_size:   Optional[int]  = None,
    trap_cause: Optional[int]  = None,
    trap_pc:    Optional[int]  = None,
    privilege:  Optional[str]  = None,
    disasm:     Optional[str]  = None,
) -> Dict[str, Any]:
    e: Dict[str, Any] = {
        "step": step, "pc": f"0x{pc:08x}",
        "instr": f"0x{instr:08x}", "trap": trap,
        "csr_writes": csr_writes or [],
    }
    if rd is not None:       e["rd"]       = rd
    if rd_val is not None:   e["rd_val"]   = f"0x{rd_val:08x}"
    if mem_op:
        e["mem_op"]   = mem_op
        e["mem_addr"] = f"0x{mem_addr:08x}" if mem_addr is not None else None
        e["mem_val"]  = f"0x{mem_val:08x}"  if mem_val  is not None else None
    if mem_size is not None:  e["mem_size"]   = mem_size
    if trap_cause is not None: e["trap_cause"] = f"0x{trap_cause:08x}"
    if trap_pc is not None:    e["trap_pc"]    = f"0x{trap_pc:08x}"
    if privilege:  e["privilege"] = privilege
    if disasm:     e["disasm"]    = disasm
    return e


class _LogPair:
    """Context manager: write two JSONL log files, clean up on exit."""

    def __init__(self, rtl: List[Dict], iss: List[Dict]) -> None:
        self._rtl = rtl
        self._iss = iss
        self.rtl_path = ""
        self.iss_path = ""

    def __enter__(self) -> "_LogPair":
        for attr, entries in (("rtl_path", self._rtl), ("iss_path", self._iss)):
            fd, path = tempfile.mkstemp(suffix=".jsonl")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
            setattr(self, attr, path)
        return self

    def __exit__(self, *_) -> None:
        for path in (self.rtl_path, self.iss_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _compare(rtl: List[Dict], iss: List[Dict], **cfg_kwargs) -> CompareResult:
    """Helper: write logs to temp files, run compare(), return result."""
    cfg = CompareConfig(**cfg_kwargs) if cfg_kwargs else None
    with _LogPair(rtl, iss) as pair:
        return compare(pair.rtl_path, pair.iss_path, cfg=cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. All 11 AVA canonical error codes
# ═══════════════════════════════════════════════════════════════════════════════

class TestAVA11Codes(unittest.TestCase):
    """Every AVA-required error code must be present in MismatchType and fire."""

    _REQUIRED = {
        "PCMISMATCH", "REGMISMATCH", "CSRMISMATCH", "MEMMISMATCH",
        "TRAPMISMATCH", "LENGTHMISMATCH", "SEQGAP", "X0WRITTEN",
        "ALIGNMENTERROR", "SCHEMAINVALID", "BINARYHASHMISMATCH",
    }

    def test_all_codes_defined(self):
        """All 11 codes must be enum values."""
        defined = {m.value for m in MismatchType}
        missing = self._REQUIRED - defined
        self.assertFalse(missing, f"Missing AVA codes: {missing}")

    def test_pcmismatch_fires(self):
        result = _compare(
            [_mk(1, 0x1000), _mk(2, 0x1008)],
            [_mk(1, 0x1000), _mk(2, 0x1004)],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "PCMISMATCH")

    def test_regmismatch_fires(self):
        result = _compare(
            [_mk(1, 0x1000, rd=1, rd_val=0)],
            [_mk(1, 0x1000, rd=1, rd_val=1)],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "REGMISMATCH")

    def test_csrmismatch_fires(self):
        result = _compare(
            [_mk(1, 0x1000, csr_writes=[{"csr": "0x300", "val": "0x1"}])],
            [_mk(1, 0x1000, csr_writes=[{"csr": "0x300", "val": "0x8"}])],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "CSRMISMATCH")

    def test_memmismatch_fires(self):
        result = _compare(
            [_mk(1, 0x1000, mem_op="load", mem_addr=0x2000, mem_val=0x1)],
            [_mk(1, 0x1000, mem_op="load", mem_addr=0x2000, mem_val=0x2)],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "MEMMISMATCH")

    def test_trapmismatch_fires(self):
        result = _compare(
            [_mk(1, 0x1000, trap=True, trap_cause=0xB)],
            [_mk(1, 0x1000, trap=False)],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "TRAPMISMATCH")

    def test_lengthmismatch_fires(self):
        result = _compare(
            [_mk(1, 0x1000)],
            [_mk(1, 0x1000), _mk(2, 0x1004)],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "LENGTHMISMATCH")

    def test_seqgap_fires(self):
        result = _compare(
            [_mk(1, 0x1000), _mk(3, 0x1008)],  # step gap: 1 → 3
            [_mk(1, 0x1000), _mk(3, 0x1008)],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "SEQGAP")

    def test_x0written_fires(self):
        result = _compare(
            [_mk(1, 0x1000, rd=0, rd_val=0xDEAD)],
            [_mk(1, 0x1000, rd=0, rd_val=0x0000)],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "X0WRITTEN")

    def test_alignmenterror_fires(self):
        """2-byte load at odd address → ALIGNMENTERROR."""
        result = _compare(
            [_mk(1, 0x1000, mem_op="load", mem_addr=0x2001, mem_val=0xAB, mem_size=2)],
            [_mk(1, 0x1000, mem_op="load", mem_addr=0x2001, mem_val=0xAB, mem_size=2)],
            check_alignment=True,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "ALIGNMENTERROR")

    def test_schemainvalid_fires(self):
        """rd > 31 → SCHEMAINVALID."""
        with _LogPair(
            [{"step": 1, "pc": "0x1000", "instr": "0x13", "trap": False,
              "csr_writes": [], "rd": 99, "rd_val": "0x1"}],
            [{"step": 1, "pc": "0x1000", "instr": "0x13", "trap": False,
              "csr_writes": [], "rd": 99, "rd_val": "0x1"}],
        ) as pair:
            result = compare(pair.rtl_path, pair.iss_path)
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "SCHEMAINVALID")

    def test_binaryhashmismatch_fires(self):
        """Expected SHA-256 mismatch → BINARYHASHMISMATCH at step 0."""
        with _LogPair([_mk(1, 0x1000)], [_mk(1, 0x1000)]) as pair:
            cfg = CompareConfig(
                expected_rtl_sha256="0000000000000000000000000000000000000000000000000000000000000000"
            )
            result = compare(pair.rtl_path, pair.iss_path, cfg=cfg)
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].mismatch_type.value, "BINARYHASHMISMATCH")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. AVA exit codes
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitCodes(unittest.TestCase):
    """Exit codes must map: 0=PASS, 1=MISMATCH, 2=INFRA, 3=CONFIG."""

    def test_exit_constants_correct(self):
        self.assertEqual(EXIT_PASS,     0)
        self.assertEqual(EXIT_MISMATCH, 1)
        self.assertEqual(EXIT_INFRA,    2)
        self.assertEqual(EXIT_CONFIG,   3)

    def test_pass_returns_0(self):
        with _LogPair([_mk(1, 0x1000)], [_mk(1, 0x1000)]) as p:
            rc = main([p.rtl_path, p.iss_path, "--quiet"])
        self.assertEqual(rc, EXIT_PASS)

    def test_mismatch_returns_1(self):
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0)],
            [_mk(1, 0x1000, rd=1, rd_val=1)],
        ) as p:
            rc = main([p.rtl_path, p.iss_path, "--quiet"])
        self.assertEqual(rc, EXIT_MISMATCH)

    def test_infra_error_returns_2(self):
        rc = main(["/tmp/__no_such_file_rtl__.jsonl",
                   "/tmp/__no_such_file_iss__.jsonl", "--quiet"])
        self.assertEqual(rc, EXIT_INFRA)

    def test_config_error_returns_3_bad_manifest(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w",
                                         delete=False) as f:
            json.dump({}, f)   # missing 'rundir'
            mpath = f.name
        try:
            rc = main(["--manifest", mpath, "--quiet"])
            self.assertEqual(rc, EXIT_CONFIG)
        finally:
            os.unlink(mpath)

    def test_config_error_returns_3_missing_logs(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = {"rundir": td, "seed": 1}
            mpath = Path(td) / "manifest.json"
            mpath.write_text(json.dumps(manifest))
            rc = main(["--manifest", str(mpath), "--quiet"])
            self.assertEqual(rc, EXIT_CONFIG)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. reprocmd in bug report
# ═══════════════════════════════════════════════════════════════════════════════

class TestReprocmd(unittest.TestCase):
    """AVA contract: bug_report.json must contain a reprocmd field."""

    def test_reprocmd_present_on_mismatch(self):
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0)],
            [_mk(1, 0x1000, rd=1, rd_val=1)],
        ) as p:
            result = compare(p.rtl_path, p.iss_path, seed=42,
                             rtl_bin="./sim", iss_bin="spike")
        report = result.to_bug_report()
        self.assertIsNotNone(report["reprocmd"],
                             "reprocmd must not be None on mismatch")
        self.assertIn("42", report["reprocmd"],
                      "reprocmd must include the seed")
        self.assertIn("sim", report["reprocmd"],
                      "reprocmd must reference the binary")

    def test_reprocmd_absent_on_pass(self):
        with _LogPair([_mk(1, 0x1000)], [_mk(1, 0x1000)]) as p:
            result = compare(p.rtl_path, p.iss_path)
        report = result.to_bug_report()
        self.assertIsNone(report["reprocmd"],
                          "reprocmd must be None when there are no mismatches")

    def test_comparator_repro_cmd_present(self):
        """comparator_repro_cmd is the re-run-comparator command."""
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0)],
            [_mk(1, 0x1000, rd=1, rd_val=1)],
        ) as p:
            result = compare(p.rtl_path, p.iss_path, seed=7)
        report = result.to_bug_report()
        self.assertIsNotNone(report["comparator_repro_cmd"])
        self.assertIn("compare_commitlogs", report["comparator_repro_cmd"])
        self.assertIn("--seed 7", report["comparator_repro_cmd"])

    def test_schema_version_is_3(self):
        with _LogPair([_mk(1, 0x1000)], [_mk(1, 0x1000)]) as p:
            result = compare(p.rtl_path, p.iss_path)
        report = result.to_bug_report()
        self.assertEqual(report["schema_version"], "3.0")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. AVA manifest mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestManifestMode(unittest.TestCase):
    """main_manifest() must implement the full AVA lifecycle contract."""

    def _write_manifest(self, rundir: str, **extras) -> Path:
        manifest = {"rundir": rundir, "seed": 42, **extras}
        p = Path(rundir) / "manifest.json"
        p.write_text(json.dumps(manifest))
        return p

    def test_pass_returns_0_updates_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "outputs"
            out.mkdir()
            e = _mk(1, 0x1000, rd=1, rd_val=1)
            (out / "rtlcommit.jsonl").write_text(json.dumps(e) + "\n")
            (out / "isscommitlog.jsonl").write_text(json.dumps(e) + "\n")
            mp = self._write_manifest(td)

            rc = main_manifest(mp)

            self.assertEqual(rc, EXIT_PASS)
            m = json.loads(mp.read_text())
            self.assertEqual(m["status"], "passed")
            self.assertEqual(m["phases"]["compare"]["status"], "passed")
            self.assertFalse((Path(td) / "bugreport.json").exists())

    def test_fail_returns_1_writes_bugreport(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "outputs"
            out.mkdir()
            rtl_e = _mk(1, 0x1000, rd=1, rd_val=0)
            iss_e = _mk(1, 0x1000, rd=1, rd_val=1)
            (out / "rtlcommit.jsonl").write_text(json.dumps(rtl_e) + "\n")
            (out / "isscommitlog.jsonl").write_text(json.dumps(iss_e) + "\n")
            mp = self._write_manifest(td, rtl_bin="./sim", iss_bin="spike")

            rc = main_manifest(mp)

            self.assertEqual(rc, EXIT_MISMATCH)
            m = json.loads(mp.read_text())
            self.assertEqual(m["status"], "failed")
            self.assertEqual(m["phases"]["compare"]["first_mismatch"], "REGMISMATCH")
            br_path = Path(td) / "bugreport.json"
            self.assertTrue(br_path.exists(), "bugreport.json must be created on failure")
            br = json.loads(br_path.read_text())
            self.assertIsNotNone(br["reprocmd"])
            self.assertIn("42", br["reprocmd"])

    def test_missing_logs_returns_3(self):
        with tempfile.TemporaryDirectory() as td:
            mp = self._write_manifest(td)
            rc = main_manifest(mp)
            self.assertEqual(rc, EXIT_CONFIG)
            m = json.loads(mp.read_text())
            self.assertEqual(m["phases"]["compare"]["status"], "error")

    def test_missing_rundir_returns_3(self):
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "manifest.json"
            mp.write_text(json.dumps({"seed": 1}))  # no rundir
            rc = main_manifest(mp)
            self.assertEqual(rc, EXIT_CONFIG)

    def test_nonexistent_manifest_returns_3(self):
        rc = main_manifest(Path("/tmp/__no_such_manifest_xyz__.json"))
        self.assertEqual(rc, EXIT_CONFIG)

    def test_manifest_updates_total_steps(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "outputs"
            out.mkdir()
            entries = [_mk(i, 0x1000 + i * 4) for i in range(1, 6)]
            for fname in ("rtlcommit.jsonl", "isscommitlog.jsonl"):
                (out / fname).write_text(
                    "\n".join(json.dumps(e) for e in entries) + "\n"
                )
            mp = self._write_manifest(td)
            main_manifest(mp)
            m = json.loads(mp.read_text())
            self.assertEqual(m["phases"]["compare"]["total_steps"], 5)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Atomic write helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite(unittest.TestCase):

    def test_atomic_write_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "output.json"
            atomic_write(p, '{"ok": true}')
            self.assertTrue(p.exists())
            self.assertEqual(json.loads(p.read_text())["ok"], True)

    def test_atomic_write_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.json"
            atomic_write(p, "hello")
            tmp = p.with_suffix(".tmp")
            self.assertFalse(tmp.exists(), ".tmp file must be removed after rename")

    def test_atomic_write_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.json"
            atomic_write(p, "first")
            atomic_write(p, "second")
            self.assertEqual(p.read_text(), "second")

    def test_atomic_update_manifest_nested(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manifest.json"
            p.write_text(json.dumps({"seed": 1, "phases": {}}))
            atomic_update_manifest(p, {
                "phases.compare.status": "passed",
                "status": "passed",
            })
            m = json.loads(p.read_text())
            self.assertEqual(m["phases"]["compare"]["status"], "passed")
            self.assertEqual(m["status"], "passed")
            self.assertEqual(m["seed"], 1)   # untouched

    def test_atomic_update_manifest_creates_nested_dicts(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manifest.json"
            p.write_text("{}")
            atomic_update_manifest(p, {"a.b.c": "deep"})
            m = json.loads(p.read_text())
            self.assertEqual(m["a"]["b"]["c"], "deep")

    def test_atomic_update_concurrent_safe(self):
        """Multiple threads writing to the same manifest must not corrupt it.

        atomic_update_manifest is NOT a CRDT: concurrent readers/writers have
        last-write-wins semantics for different keys.  The invariant is that
        the file always contains valid JSON (no partial writes / torn reads).
        """
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manifest.json"
            p.write_text(json.dumps({"counter": 0}))
            errors: list = []

            def _update(i):
                try:
                    atomic_update_manifest(p, {f"key_{i}": i})
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_update, args=(i,)) for i in range(20)]
            for t in threads: t.start()
            for t in threads: t.join()

            self.assertFalse(errors, f"Concurrent writes raised: {errors}")
            # File must always be valid JSON after all writes complete
            try:
                m = json.loads(p.read_text())
            except json.JSONDecodeError as exc:
                self.fail(f"Manifest corrupted after concurrent writes: {exc}")
            # All values in the file must be valid (no corrupt partial values)
            for v in m.values():
                self.assertIsNotNone(v)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Delta window memory footprint
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeltaWindow(unittest.TestCase):

    def test_window_depth_bounded(self):
        """Context window must never exceed cfg.window in length."""
        N   = 200
        WIN = 16
        entries = [_mk(i, 0x1000 + i * 4, rd=1, rd_val=i) for i in range(1, N + 1)]
        # Inject mismatch at the last step
        iss = list(entries)
        iss[-1] = dict(iss[-1]); iss[-1]["rd_val"] = "0x00000000"
        with _LogPair(entries, iss) as p:
            result = compare(p.rtl_path, p.iss_path, cfg=CompareConfig(window=WIN))
        self.assertFalse(result.passed)
        ctx_len = len(result.mismatches[0].context_window)
        self.assertLessEqual(ctx_len, WIN,
                             f"Context window has {ctx_len} entries, expected ≤ {WIN}")

    def test_window_contains_recent_commits(self):
        """The last entry in context_window must be the commit just before divergence."""
        entries = [_mk(i, 0x1000 + i * 4) for i in range(1, 10)]
        iss = list(entries)
        iss[-1] = dict(iss[-1]); iss[-1]["pc"] = "0x00002000"   # PC mismatch at step 9
        with _LogPair(entries, iss) as p:
            result = compare(p.rtl_path, p.iss_path, cfg=CompareConfig(window=32))
        self.assertFalse(result.passed)
        ctx = result.mismatches[0].context_window
        # Last entry in window should be step 9 (pushed before comparison)
        self.assertEqual(ctx[-1]["step"], 9)

    def test_zero_window_no_context(self):
        """window=0 must produce an empty context_window list."""
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0)],
            [_mk(1, 0x1000, rd=1, rd_val=1)],
        ) as p:
            result = compare(p.rtl_path, p.iss_path, cfg=CompareConfig(window=0))
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatches[0].context_window, [])


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Threaded reader
# ═══════════════════════════════════════════════════════════════════════════════

class TestThreadedReader(unittest.TestCase):

    def test_both_threads_complete(self):
        """A 1000-entry pass run must complete without deadlock."""
        N = 1000
        entries = [_mk(i, 0x1000 + i * 4) for i in range(1, N + 1)]
        with _LogPair(entries, entries) as p:
            result = compare(p.rtl_path, p.iss_path)
        self.assertTrue(result.passed)
        self.assertEqual(result.stats.total_steps, N)

    def test_rtl_thread_error_surfaces(self):
        """LogFormatError must be raised when RTL log file is missing."""
        with _LogPair([_mk(1, 0x1000)], [_mk(1, 0x1000)]) as p:
            with self.assertRaises(LogFormatError):
                compare("/tmp/__missing_rtl__.jsonl", p.iss_path)

    def test_iss_thread_error_surfaces(self):
        with _LogPair([_mk(1, 0x1000)], [_mk(1, 0x1000)]) as p:
            with self.assertRaises(LogFormatError):
                compare(p.rtl_path, "/tmp/__missing_iss__.jsonl")

    def test_parallel_identical_large(self):
        """Threaded compare of 5000-entry identical logs must PASS."""
        N = 5000
        entries = [_mk(i, (0x80000000 + i * 4) & 0xFFFFFFFF) for i in range(1, N + 1)]
        with _LogPair(entries, entries) as p:
            result = compare(p.rtl_path, p.iss_path,
                             cfg=CompareConfig(stop_on_first=True))
        self.assertTrue(result.passed)
        self.assertEqual(result.stats.total_steps, N)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Hypothesis engine — all 11 codes
# ═══════════════════════════════════════════════════════════════════════════════

class TestHypothesisEngine(unittest.TestCase):

    def setUp(self):
        self.engine = HypothesisEngine(max_hypotheses=3, min_confidence=0.4)

    def _mismatch(self, mtype: str, instr: int = 0, **kwargs) -> Dict:
        return {
            "mismatch_type": mtype,
            "step": 1,
            "rtl_entry": {"pc": "0x1000", "instr": f"0x{instr:08x}", **kwargs},
            "iss_entry": {},
        }

    def _assert_hyp(self, mtype: str, instr: int = 0, **kwargs):
        report = {"mismatches": [self._mismatch(mtype, instr, **kwargs)]}
        hyps = self.engine.generate_from_report(report)
        self.assertGreater(len(hyps), 0,
                           f"No hypotheses for {mtype}")
        self.assertIn("text", hyps[0])
        self.assertIn("confidence", hyps[0])
        self.assertIn("category", hyps[0])
        return hyps

    def test_pcmismatch_has_hypothesis(self):     self._assert_hyp("PCMISMATCH")
    def test_regmismatch_has_hypothesis(self):    self._assert_hyp("REGMISMATCH", 0x02208533)
    def test_csrmismatch_has_hypothesis(self):    self._assert_hyp("CSRMISMATCH", 0x30079073)
    def test_memmismatch_has_hypothesis(self):    self._assert_hyp("MEMMISMATCH", 0x00002003)
    def test_trapmismatch_has_hypothesis(self):   self._assert_hyp("TRAPMISMATCH")
    def test_lengthmismatch_has_hypothesis(self): self._assert_hyp("LENGTHMISMATCH")
    def test_seqgap_has_hypothesis(self):         self._assert_hyp("SEQGAP")
    def test_x0written_has_hypothesis(self):      self._assert_hyp("X0WRITTEN")
    def test_alignmenterror_has_hypothesis(self): self._assert_hyp("ALIGNMENTERROR")
    def test_schemainvalid_has_hypothesis(self):  self._assert_hyp("SCHEMAINVALID")
    def test_binaryhashmismatch_hypothesis(self): self._assert_hyp("BINARYHASHMISMATCH")

    def test_div_by_zero_high_confidence(self):
        """DIV-by-zero corner case must score ≥ 0.90."""
        hyps = self._assert_hyp("REGMISMATCH", 0x0220C533,  # DIV
                                 rs2_val="0x00000000")
        top = max(hyps, key=lambda h: h["confidence"])
        self.assertGreaterEqual(top["confidence"], 0.90)

    def test_int_min_neg1_high_confidence(self):
        """INT_MIN / -1 overflow must score ≥ 0.90."""
        hyps = self._assert_hyp("REGMISMATCH", 0x0220C533,
                                 rs1_val="0x80000000", rs2_val="0xFFFFFFFF")
        top = max(hyps, key=lambda h: h["confidence"])
        self.assertGreaterEqual(top["confidence"], 0.90)

    def test_mret_pc_hypothesis(self):
        """MRET instruction → MRET-specific hypothesis fires."""
        hyps = self._assert_hyp("PCMISMATCH", 0x30200073)  # MRET
        texts = " ".join(h["text"] for h in hyps).lower()
        self.assertIn("mret", texts)

    def test_max_hypotheses_respected(self):
        engine = HypothesisEngine(max_hypotheses=2)
        report = {"mismatches": [self._mismatch("REGMISMATCH", 0x02208533)]}
        hyps = engine.generate_from_report(report)
        self.assertLessEqual(len(hyps), 2)

    def test_confidence_sorted_descending(self):
        report = {"mismatches": [self._mismatch("REGMISMATCH", 0x02208533)]}
        hyps = self.engine.generate_from_report(report)
        confs = [h["confidence"] for h in hyps]
        self.assertEqual(confs, sorted(confs, reverse=True))

    def test_generate_from_compare_result(self):
        """generate_hypotheses() must accept a CompareResult object."""
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0)],
            [_mk(1, 0x1000, rd=1, rd_val=1)],
        ) as p:
            result = compare(p.rtl_path, p.iss_path)
        hyps = generate_hypotheses(result)
        self.assertGreater(len(hyps), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Bug report schema
# ═══════════════════════════════════════════════════════════════════════════════

class TestBugReportSchema(unittest.TestCase):

    _REQUIRED_KEYS = {
        "schema_version", "tool", "passed", "stats",
        "rtl_log", "iss_log", "rtl_sha256", "iss_sha256",
        "seed", "rtl_bin", "iss_bin",
        "reprocmd", "comparator_repro_cmd",
        "config", "mismatches", "ava_bugs",
    }

    def _report_for_mismatch(self) -> Dict:
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0)],
            [_mk(1, 0x1000, rd=1, rd_val=1)],
        ) as p:
            return compare(p.rtl_path, p.iss_path, seed=1).to_bug_report()

    def test_all_required_keys_present(self):
        report = self._report_for_mismatch()
        missing = self._REQUIRED_KEYS - set(report.keys())
        self.assertFalse(missing, f"Missing keys: {missing}")

    def test_ava_bugs_is_list_of_strings(self):
        report = self._report_for_mismatch()
        self.assertIsInstance(report["ava_bugs"], list)
        for b in report["ava_bugs"]:
            self.assertIsInstance(b, str)

    def test_ava_bugs_format(self):
        """Each ava_bug must match [SEVERITY] step=N TYPE: description."""
        report = self._report_for_mismatch()
        for bug in report["ava_bugs"]:
            self.assertRegex(bug, r"^\[.+\] step=\d+ \w+: .+")

    def test_mismatch_dict_has_required_fields(self):
        report = self._report_for_mismatch()
        m = report["mismatches"][0]
        for field_name in ("mismatch_type", "severity", "description",
                           "step", "differing_field", "rtl_value", "iss_value"):
            self.assertIn(field_name, m, f"Mismatch dict missing '{field_name}'")

    def test_stats_has_all_fields(self):
        report = self._report_for_mismatch()
        stats = report["stats"]
        for k in ("total_steps", "total_mismatches", "elapsed_s",
                  "first_divergence_step", "mismatch_by_type"):
            self.assertIn(k, stats)

    def test_sha256_checksums_present(self):
        report = self._report_for_mismatch()
        self.assertIsNotNone(report["rtl_sha256"])
        self.assertIsNotNone(report["iss_sha256"])
        # Should be 64-character hex strings
        self.assertRegex(report["rtl_sha256"], r"^[0-9a-f]{64}$")

    def test_report_is_json_serialisable(self):
        report = self._report_for_mismatch()
        try:
            serialised = json.dumps(report, default=str)
            self.assertGreater(len(serialised), 0)
        except (TypeError, ValueError) as exc:
            self.fail(f"Bug report is not JSON-serialisable: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. CLI integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestCLIIntegration(unittest.TestCase):

    def test_self_test_flag(self):
        rc = main(["--self-test"])
        self.assertEqual(rc, EXIT_PASS)

    def test_generate_sample_logs(self):
        with tempfile.TemporaryDirectory() as td:
            rc = main(["--generate-sample-logs", "--sample-dir", td])
            self.assertEqual(rc, EXIT_PASS)
            files = list(Path(td).glob("*.jsonl"))
            self.assertGreater(len(files), 0, "Sample logs must be generated")

    def test_bug_report_written(self):
        with tempfile.TemporaryDirectory() as td:
            with _LogPair(
                [_mk(1, 0x1000, rd=1, rd_val=0)],
                [_mk(1, 0x1000, rd=1, rd_val=1)],
            ) as p:
                out = str(Path(td) / "bug.json")
                rc = main([p.rtl_path, p.iss_path, "-o", out, "--quiet"])
            self.assertEqual(rc, EXIT_MISMATCH)
            self.assertTrue(Path(out).exists())
            report = json.loads(Path(out).read_text())
            self.assertFalse(report["passed"])

    def test_junit_xml_written(self):
        with tempfile.TemporaryDirectory() as td:
            with _LogPair(
                [_mk(1, 0x1000, rd=1, rd_val=0)],
                [_mk(1, 0x1000, rd=1, rd_val=1)],
            ) as p:
                out = str(Path(td) / "ci.xml")
                rc = main([p.rtl_path, p.iss_path, "--junit", out, "--quiet"])
            self.assertEqual(rc, EXIT_MISMATCH)
            content = Path(out).read_text()
            self.assertIn("testsuite", content)
            self.assertIn("failure", content)

    def test_markdown_written(self):
        with tempfile.TemporaryDirectory() as td:
            with _LogPair(
                [_mk(1, 0x1000, rd=1, rd_val=0)],
                [_mk(1, 0x1000, rd=1, rd_val=1)],
            ) as p:
                out = str(Path(td) / "report.md")
                rc = main([p.rtl_path, p.iss_path, "--markdown", out, "--quiet"])
            self.assertEqual(rc, EXIT_MISMATCH)
            content = Path(out).read_text()
            self.assertIn("# Commit Log Comparison Report", content)
            self.assertIn("MISMATCH", content)

    def test_all_mismatches_mode(self):
        """--all must collect every mismatch, not just the first."""
        with _LogPair(
            [_mk(i, 0x1000 + i*4, rd=i, rd_val=0) for i in range(1, 6)],
            [_mk(i, 0x1000 + i*4, rd=i, rd_val=i) for i in range(1, 6)],
        ) as p:
            rc = main([p.rtl_path, p.iss_path, "--all",
                       "--max-mismatches", "0", "--quiet"])
        self.assertEqual(rc, EXIT_MISMATCH)

    def test_xlen64_flag(self):
        """--xlen 64 must not crash and must correctly mask 64-bit values."""
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0xFFFFFFFF)],
            [_mk(1, 0x1000, rd=1, rd_val=0x00000000FFFFFFFF)],
        ) as p:
            rc = main([p.rtl_path, p.iss_path, "--xlen", "64", "--quiet"])
        self.assertEqual(rc, EXIT_PASS)

    def test_skip_fields(self):
        """--skip-fields privilege must suppress PRIVILEGEMISMATCH."""
        with _LogPair(
            [_mk(1, 0x1000, privilege="M")],
            [_mk(1, 0x1000, privilege="U")],
        ) as p:
            rc = main([p.rtl_path, p.iss_path,
                       "--skip-fields", "privilege", "--quiet"])
        self.assertEqual(rc, EXIT_PASS)

    def test_ignore_csrs(self):
        """--ignore-csrs 0xC00 must suppress CSRMISMATCH for cycle CSR."""
        with _LogPair(
            [_mk(1, 0x1000, csr_writes=[{"csr": "0xC00", "val": "0x1"}])],
            [_mk(1, 0x1000, csr_writes=[{"csr": "0xC00", "val": "0x2"}])],
        ) as p:
            rc = main([p.rtl_path, p.iss_path,
                       "--ignore-csrs", "0xC00", "--quiet"])
        self.assertEqual(rc, EXIT_PASS)

    def test_no_align_check(self):
        """--no-align-check must suppress ALIGNMENTERROR."""
        with _LogPair(
            [_mk(1, 0x1000, mem_op="load", mem_addr=0x2001, mem_val=0xAB, mem_size=4)],
            [_mk(1, 0x1000, mem_op="load", mem_addr=0x2001, mem_val=0xAB, mem_size=4)],
        ) as p:
            rc = main([p.rtl_path, p.iss_path, "--no-align-check", "--quiet"])
        self.assertEqual(rc, EXIT_PASS)

    def test_batch_mode(self):
        """--batch must process a manifest YAML-style JSON and report results."""
        with tempfile.TemporaryDirectory() as td:
            with _LogPair([_mk(1, 0x1000)], [_mk(1, 0x1000)]) as p1:
                with _LogPair(
                    [_mk(1, 0x1000, rd=1, rd_val=0)],
                    [_mk(1, 0x1000, rd=1, rd_val=1)],
                ) as p2:
                    manifest = {
                        "entries": [
                            {"rtl_log": p1.rtl_path, "iss_log": p1.iss_path,
                             "label": "pass_run"},
                            {"rtl_log": p2.rtl_path, "iss_log": p2.iss_path,
                             "label": "fail_run"},
                        ]
                    }
                    mpath = Path(td) / "batch.json"
                    mpath.write_text(json.dumps(manifest))
                    rc = main(["--batch", str(mpath), "--quiet"])
            self.assertEqual(rc, EXIT_MISMATCH)   # one fail → batch fails


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Correctness edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestCorrectnessEdgeCases(unittest.TestCase):

    def test_empty_logs_pass(self):
        with _LogPair([], []) as p:
            result = compare(p.rtl_path, p.iss_path)
        self.assertTrue(result.passed)
        self.assertEqual(result.stats.total_steps, 0)

    def test_xlen32_masks_high_bits(self):
        """0xFFFFFFFF == 0x00000000FFFFFFFF under xlen=32."""
        with _LogPair(
            [_mk(1, 0x1000, rd=1, rd_val=0xFFFFFFFF)],
            [_mk(1, 0x1000, rd=1, rd_val=0x00000000FFFFFFFF)],
        ) as p:
            result = compare(p.rtl_path, p.iss_path, cfg=CompareConfig(xlen=32))
        self.assertTrue(result.passed)

    def test_csr_order_insensitive_pass(self):
        with _LogPair(
            [_mk(1, 0x1000, csr_writes=[
                {"csr": "0x341", "val": "0x1000"},
                {"csr": "0x300", "val": "0x8"},
            ])],
            [_mk(1, 0x1000, csr_writes=[
                {"csr": "0x300", "val": "0x8"},
                {"csr": "0x341", "val": "0x1000"},
            ])],
        ) as p:
            result = compare(p.rtl_path, p.iss_path,
                             cfg=CompareConfig(csr_write_order_sensitive=False))
        self.assertTrue(result.passed)

    def test_csr_mask_ignores_masked_bits(self):
        """csr_masks={0x300: 0x1} compares only bit 0 of mstatus."""
        with _LogPair(
            [_mk(1, 0x1000, csr_writes=[{"csr": "0x300", "val": "0x9"}])],
            [_mk(1, 0x1000, csr_writes=[{"csr": "0x300", "val": "0x1"}])],
        ) as p:
            result = compare(p.rtl_path, p.iss_path,
                             cfg=CompareConfig(csr_masks={0x300: 0x1}))
        self.assertTrue(result.passed)

    def test_x0_both_zero_pass(self):
        with _LogPair(
            [_mk(1, 0x1000, rd=0, rd_val=0)],
            [_mk(1, 0x1000, rd=0, rd_val=0)],
        ) as p:
            result = compare(p.rtl_path, p.iss_path)
        self.assertTrue(result.passed)

    def test_bom_stripped(self):
        """UTF-8 BOM at start of file must be handled silently."""
        entry = json.dumps(_mk(1, 0x1000, rd=1, rd_val=1))
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(b"\xef\xbb\xbf" + entry.encode() + b"\n")
            rtl_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(entry.encode() + b"\n")
            iss_path = f.name
        try:
            result = compare(rtl_path, iss_path)
            self.assertTrue(result.passed)
        finally:
            os.unlink(rtl_path); os.unlink(iss_path)

    def test_malformed_json_tolerance(self):
        """max_parse_errors=5: malformed lines are skipped, valid entries compare."""
        valid1 = json.dumps(_mk(1, 0x1000))
        valid2 = json.dumps(_mk(2, 0x1004))
        rtl_content = f"{valid1}\nNOT JSON\n\n{valid2}\n"
        iss_content = f"{valid1}\n{valid2}\n"
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write(rtl_content); rtl_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write(iss_content); iss_path = f.name
        try:
            result = compare(rtl_path, iss_path,
                             cfg=CompareConfig(max_parse_errors=5))
            self.assertTrue(result.passed)
            self.assertEqual(result.stats.rtl_parse_warnings, 1)
        finally:
            os.unlink(rtl_path); os.unlink(iss_path)

    def test_comment_lines_skipped(self):
        """Lines starting with # or // are treated as comments."""
        valid = json.dumps(_mk(1, 0x1000))
        content = f"# this is a comment\n// another comment\n{valid}\n"
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write(content); p1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write(valid + "\n"); p2 = f.name
        try:
            result = compare(p1, p2)
            self.assertTrue(result.passed)
        finally:
            os.unlink(p1); os.unlink(p2)

    def test_gzip_log(self):
        """Gzip-compressed logs must be read transparently."""
        import gzip as _gzip
        entry = json.dumps(_mk(1, 0x1000, rd=1, rd_val=1))
        with tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False) as f:
            rtl_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False) as f:
            iss_path = f.name
        try:
            for path in (rtl_path, iss_path):
                with _gzip.open(path, "wt", encoding="utf-8") as gz:
                    gz.write(entry + "\n")
            result = compare(rtl_path, iss_path)
            self.assertTrue(result.passed)
        finally:
            os.unlink(rtl_path); os.unlink(iss_path)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Pretty test runner: show dots for pass, F for fail, E for error
    runner = unittest.TextTestRunner(
        verbosity=2 if "-v" in sys.argv else 1,
        stream=sys.stdout,
    )
    loader = unittest.TestLoader()

    # Allow running a specific class: python test_comparator.py TestExitCodes
    test_names = [a for a in sys.argv[1:] if not a.startswith("-")]
    if test_names:
        suite = unittest.TestSuite()
        for name in test_names:
            try:
                suite.addTests(loader.loadTestsFromName(name, module=sys.modules[__name__]))
            except AttributeError:
                print(f"Unknown test class: {name}", file=sys.stderr)
                sys.exit(1)
    else:
        suite = loader.loadTestsFromModule(sys.modules[__name__])

    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

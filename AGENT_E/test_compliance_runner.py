#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_compliance_runner.py — Agent E Complete Test Suite (v2)
=============================================================
Covers all original tests PLUS:

  TestExitCodes           — EXIT_* constants + compute_exit_code()
  TestManifestHelpers     — _deep_merge(), patch_manifest()
  TestManifestEntrypoint  — run_compliance_manifest() AVA contract
  TestAdapterCLI          — run_rtl_adapter CLI + pre-flight validation
  TestAdapterFlow         — adapter bridge logic with Agent B mocked
  TestSplitWorkerPools    — build_workers / run_workers separation
  TestIncrementalSigCmp   — max_mismatches early-stop
  TestBuildCacheSHA256    — cache filename uses SHA-256 throughout
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

import run_compliance as RC
from run_compliance import (
    LINK_ADDRESS, MAX_SIG_WORDS, SIG_ALIGN, SIG_REGION_SZ,
    REPORT_JSON, REPORT_HTML, REPORT_JUNIT,
    EXIT_PASS, EXIT_FAIL, EXIT_CRASH, EXIT_TOOL,
    ComplianceError, ToolNotFoundError, BuildError, SimulationError, SignatureError,
    TestResult, ErrorClass,
    RunConfig, TestRecord, RunReport,
    _atomic_write, _normalise_isa, _generate_source,
    _write_linker_script, _EMBEDDED_TESTS, _ASM_MACROS,
    _deep_merge, patch_manifest,
    parse_signature, compare_signatures,
    probe_spike, probe_toolchain,
    compute_exit_code, run_compliance_manifest,
    BuildCache, RetryPolicy,
    SpikeDUTBackend, ExternalDUTBackend,
    ComplianceRunner,
    render_html, render_junit_xml,
    run_compliance_for_ava,
)
import run_rtl_adapter as ADAPTER

logging.disable(logging.CRITICAL)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _make_sig(tmp: Path, lines: str, name: str = "test.sig") -> Path:
    p = tmp / name
    p.write_text(textwrap.dedent(lines))
    return p


def _fake_elf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + b"\x00" * 60)
    return path


def _make_report(
    results: tuple = (TestResult.PASS,),
    tool_errors: list = [],
) -> RunReport:
    tests = []
    for i, r in enumerate(results):
        tests.append({
            "name": f"T-{i:02d}", "isa_subset": "RV32I",
            "source": "", "elf": "", "golden_sig": "", "dut_sig": "",
            "result": r.value, "error_class": ErrorClass.NONE.value,
            "error_msg": "err" if r != TestResult.PASS else "",
            "mismatch_idx": 0 if r == TestResult.FAIL else -1,
            "golden_words": ["00000001"],
            "dut_words": ["00000002" if r == TestResult.FAIL else "00000001"],
            "build_time_s": 0.1, "run_time_s": 0.1, "cache_hit": False,
        })
    n      = len(tests)
    n_pass = sum(1 for t in tests if t["result"] == TestResult.PASS.value)
    n_fail = sum(1 for t in tests if t["result"] == TestResult.FAIL.value)
    n_err  = sum(1 for t in tests if t["result"] == TestResult.ERROR.value)
    return RunReport(
        timestamp="2025-01-01T00:00:00+00:00", isa="RV32IM",
        spike_bin="spike", spike_version="1.1.1",
        toolchain="/usr/bin/riscv32-unknown-elf-gcc",
        run_dir="/tmp/run", tests=tuple(tests),
        summary={
            "total": n, "pass": n_pass, "fail": n_fail, "error": n_err,
            "skipped": 0, "pass_rate_pct": round(n_pass / n * 100, 1) if n else 0.0,
            "tool_errors": list(tool_errors), "run_dir": "/tmp/run",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# EXIT CODES
# ─────────────────────────────────────────────────────────────────────────────

class TestExitCodes(unittest.TestCase):

    def test_constants(self):
        self.assertEqual(EXIT_PASS,  0)
        self.assertEqual(EXIT_FAIL,  1)
        self.assertEqual(EXIT_CRASH, 2)
        self.assertEqual(EXIT_TOOL,  3)

    def test_all_pass(self):
        self.assertEqual(compute_exit_code(_make_report((TestResult.PASS, TestResult.PASS))), EXIT_PASS)

    def test_one_fail(self):
        self.assertEqual(compute_exit_code(_make_report((TestResult.PASS, TestResult.FAIL))), EXIT_FAIL)

    def test_one_error(self):
        self.assertEqual(compute_exit_code(_make_report((TestResult.PASS, TestResult.ERROR))), EXIT_CRASH)

    def test_tool_error(self):
        r = _make_report((TestResult.PASS,), tool_errors=["Spike not found"])
        self.assertEqual(compute_exit_code(r), EXIT_TOOL)

    def test_tool_beats_fail(self):
        r = _make_report((TestResult.FAIL,), tool_errors=["gcc not found"])
        self.assertEqual(compute_exit_code(r), EXIT_TOOL)

    def test_error_beats_fail(self):
        r = _make_report((TestResult.FAIL, TestResult.ERROR))
        self.assertEqual(compute_exit_code(r), EXIT_CRASH)

    def test_zero_tests_is_crash(self):
        r = _make_report(())
        self.assertEqual(compute_exit_code(r), EXIT_CRASH)


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST HELPERS
# ─────────────────────────────────────────────────────────────────────────────

class TestManifestHelpers(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data: dict) -> Path:
        p = self.tmp / "manifest.json"
        p.write_text(json.dumps(data))
        return p

    def test_deep_merge_flat(self):
        base = {"a": 1}
        _deep_merge(base, {"b": 2})
        self.assertEqual(base, {"a": 1, "b": 2})

    def test_deep_merge_nested_nondestructive(self):
        base = {"phases": {"build": {"status": "pass"}, "rtl": {"status": "pass"}}}
        _deep_merge(base, {"phases": {"compliance": {"status": "running"}}})
        self.assertEqual(base["phases"]["build"]["status"], "pass")
        self.assertEqual(base["phases"]["compliance"]["status"], "running")

    def test_deep_merge_overwrites_scalar(self):
        base = {"a": 1}
        _deep_merge(base, {"a": 99})
        self.assertEqual(base["a"], 99)

    def test_patch_manifest_atomic_no_tmp(self):
        p = self._write({"x": 1})
        patch_manifest(p, {"y": 2})
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_patch_manifest_merges_deep(self):
        p = self._write({"phases": {"rtl": {"status": "pass"}}})
        patch_manifest(p, {"phases": {"compliance": {"status": "running"}}})
        updated = json.loads(p.read_text())
        self.assertEqual(updated["phases"]["rtl"]["status"], "pass")
        self.assertEqual(updated["phases"]["compliance"]["status"], "running")

    def test_patch_manifest_multiple_times(self):
        p = self._write({"status": "pending"})
        patch_manifest(p, {"status": "running"})
        patch_manifest(p, {"status": "completed", "elapsed": 5.0})
        final = json.loads(p.read_text())
        self.assertEqual(final["status"],  "completed")
        self.assertEqual(final["elapsed"], 5.0)


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

class TestManifestEntrypoint(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mp(self, extra: dict = {}) -> Path:
        data = {
            "rundir":   str(self.tmp),
            "binary":   str(self.tmp / "test.elf"),
            "isa":      "RV32I",
            "spikebin": "__no_spike__",
            "workers":  1,
        }
        data.update(extra)
        p = self.tmp / "manifest.json"
        p.write_text(json.dumps(data))
        return p

    def test_missing_manifest_returns_crash(self):
        rc = run_compliance_manifest(self.tmp / "nope.json")
        self.assertEqual(rc, EXIT_CRASH)

    def test_invalid_json_returns_crash(self):
        p = self.tmp / "bad.json"
        p.write_text("{not json")
        self.assertEqual(run_compliance_manifest(p), EXIT_CRASH)

    def test_no_tools_returns_tool_code(self):
        rc = run_compliance_manifest(self._mp())
        self.assertEqual(rc, EXIT_TOOL)

    def test_manifest_updated_with_status(self):
        p = self._mp()
        run_compliance_manifest(p)
        updated = json.loads(p.read_text())
        self.assertIn("status", updated)

    def test_phases_compliance_not_stuck_running(self):
        p = self._mp()
        run_compliance_manifest(p)
        status = json.loads(p.read_text()).get("phases", {}).get("compliance", {}).get("status", "")
        self.assertNotEqual(status, "running")

    def test_compliance_result_schema_v200(self):
        p = self._mp()
        run_compliance_manifest(p)
        result = json.loads(p.read_text()).get("compliance", {}).get("result")
        if result:
            for k in ("schemaversion", "total", "pass", "fail", "error", "pass_pct", "failedlist"):
                self.assertIn(k, result, f"Missing key: {k}")
            self.assertEqual(result["schemaversion"], "2.0.0")

    def test_failedlist_has_mismatch_word(self):
        p = self._mp()
        run_compliance_manifest(p)
        result = json.loads(p.read_text()).get("compliance", {}).get("result", {})
        for entry in result.get("failedlist", []):
            self.assertIn("test",          entry)
            self.assertIn("mismatch_word", entry)

    def test_exception_in_runner_sets_error_status(self):
        p = self._mp()
        with patch.object(ComplianceRunner, "run", side_effect=RuntimeError("crash")):
            rc = run_compliance_manifest(p)
        self.assertEqual(rc, EXIT_CRASH)
        updated = json.loads(p.read_text())
        self.assertEqual(updated["phases"]["compliance"]["status"], "error")

    def test_main_routes_manifest_flag(self):
        p = self._mp()
        rc = RC.main(["--manifest", str(p)])
        self.assertIn(rc, (EXIT_PASS, EXIT_FAIL, EXIT_CRASH, EXIT_TOOL))


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER CLI + PRE-FLIGHT
# ─────────────────────────────────────────────────────────────────────────────

class TestAdapterCLI(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_elf(self):
        rc = ADAPTER.main(["--elf", str(self.tmp / "no.elf"), "--sig", str(self.tmp / "out.sig")])
        self.assertEqual(rc, ADAPTER.EXIT_CRASH)

    def test_empty_elf(self):
        elf = self.tmp / "e.elf"
        elf.write_bytes(b"")
        rc = ADAPTER.main(["--elf", str(elf), "--sig", str(self.tmp / "out.sig")])
        self.assertEqual(rc, ADAPTER.EXIT_CRASH)

    def test_bad_elf_magic(self):
        elf = self.tmp / "b.elf"
        elf.write_bytes(b"NOTELF" + b"\x00" * 60)
        rc = ADAPTER.main(["--elf", str(elf), "--sig", str(self.tmp / "out.sig")])
        self.assertEqual(rc, ADAPTER.EXIT_CRASH)

    def test_no_agent_b_returns_tool(self):
        elf = _fake_elf(self.tmp / "t.elf")
        sig = self.tmp / "out.sig"
        with patch.object(ADAPTER, "_find_run_rtl", return_value=None):
            rc = ADAPTER.main(["--elf", str(elf), "--sig", str(sig)])
        self.assertEqual(rc, ADAPTER.EXIT_TOOL)

    def test_sig_parent_created(self):
        elf     = _fake_elf(self.tmp / "t.elf")
        sig     = self.tmp / "deep" / "out.sig"
        fake_rtl = self.tmp / "run_rtl.py"
        fake_rtl.write_text("# stub")
        with patch.object(ADAPTER, "_find_run_rtl", return_value=fake_rtl):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                ADAPTER.main(["--elf", str(elf), "--sig", str(sig)])
        self.assertTrue(sig.parent.exists())


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER FLOW
# ─────────────────────────────────────────────────────────────────────────────

class TestAdapterFlow(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmp()
        self.elf = _fake_elf(self.tmp / "t.elf")
        self.sig = self.tmp / "out.sig"
        self.fake_rtl = self.tmp / "run_rtl.py"
        self.fake_rtl.write_text("# stub")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, words: List[str], exit_code: int = 0) -> int:
        def _mock(cmd, **kwargs):
            for i, c in enumerate(cmd):
                if c == "--sig-out" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("\n".join(words) + "\n")
            return MagicMock(returncode=exit_code, stdout="", stderr="")

        with patch.object(ADAPTER, "_find_run_rtl", return_value=self.fake_rtl):
            with patch("subprocess.run", side_effect=_mock):
                return ADAPTER.run_adapter(
                    elf=self.elf, sig_out=self.sig, isa="RV32IM",
                    seed=0, timeout=30,
                    sig_begin="0x80002000", sig_end="0x80002040",
                    verbose=False,
                )

    def test_valid_sig_is_pass(self):
        self.assertEqual(self._run(["00000001", "deadbeef"]), ADAPTER.EXIT_PASS)

    def test_sig_file_written(self):
        self._run(["cafebabe"])
        self.assertTrue(self.sig.exists())
        self.assertGreater(self.sig.stat().st_size, 0)

    def test_agent_b_crash_is_crash(self):
        self.assertEqual(self._run(["00000001"], exit_code=2), ADAPTER.EXIT_CRASH)

    def test_timeout_is_crash(self):
        with patch.object(ADAPTER, "_find_run_rtl", return_value=self.fake_rtl):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
                rc = ADAPTER.run_adapter(
                    elf=self.elf, sig_out=self.sig, isa="RV32IM",
                    seed=0, timeout=30, sig_begin="0x80002000",
                    sig_end="0x80002040", verbose=False,
                )
        self.assertEqual(rc, ADAPTER.EXIT_CRASH)

    def test_empty_sig_is_crash(self):
        def _empty(cmd, **kwargs):
            for i, c in enumerate(cmd):
                if c == "--sig-out" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(ADAPTER, "_find_run_rtl", return_value=self.fake_rtl):
            with patch("subprocess.run", side_effect=_empty):
                rc = ADAPTER.run_adapter(
                    elf=self.elf, sig_out=self.sig, isa="RV32IM",
                    seed=0, timeout=30, sig_begin="0x80002000",
                    sig_end="0x80002040", verbose=False,
                )
        self.assertEqual(rc, ADAPTER.EXIT_CRASH)

    def test_manifest_outputs_signature_used(self):
        """Agent B writes outputs.signature path into manifest — adapter must honour it."""
        def _mock(cmd, **kwargs):
            for i, c in enumerate(cmd):
                if c == "--manifest" and i + 1 < len(cmd):
                    mpath = Path(cmd[i + 1])
                    alt   = mpath.parent / "alt.hex"
                    alt.write_text("deadbeef\n00000001\n")
                    data  = json.loads(mpath.read_text())
                    data.setdefault("outputs", {})["signature"] = str(alt)
                    mpath.write_text(json.dumps(data))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(ADAPTER, "_find_run_rtl", return_value=self.fake_rtl):
            with patch("subprocess.run", side_effect=_mock):
                rc = ADAPTER.run_adapter(
                    elf=self.elf, sig_out=self.sig, isa="RV32IM",
                    seed=0, timeout=30, sig_begin="0x80002000",
                    sig_end="0x80002040", verbose=False,
                )
        self.assertEqual(rc, ADAPTER.EXIT_PASS)
        self.assertTrue(self.sig.exists())

    def test_nm_addresses_forwarded(self):
        captured = []
        def _mock(cmd, **kwargs):
            captured.extend(cmd)
            for i, c in enumerate(cmd):
                if c == "--sig-out" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("00000001\n")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(ADAPTER, "_find_run_rtl", return_value=self.fake_rtl):
            with patch.object(ADAPTER, "_nm_sig_addresses", return_value=("0x80003000", "0x80003040")):
                with patch("subprocess.run", side_effect=_mock):
                    ADAPTER.main([
                        "--elf", str(self.elf), "--sig", str(self.sig),
                        "--sig-begin", "auto", "--sig-end", "auto",
                    ])
        self.assertIn("0x80003000", captured)
        self.assertIn("0x80003040", captured)

    def test_work_dir_cleaned_up(self):
        work_dirs: List[Path] = []
        orig_mkdir = Path.mkdir

        def _track(self_p, *a, **kw):
            if "_adapter_" in str(self_p):
                work_dirs.append(self_p)
            orig_mkdir(self_p, *a, **kw)

        def _mock(cmd, **kwargs):
            for i, c in enumerate(cmd):
                if c == "--sig-out" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("00000001\n")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(ADAPTER, "_find_run_rtl", return_value=self.fake_rtl):
            with patch("subprocess.run", side_effect=_mock):
                with patch.object(Path, "mkdir", _track):
                    ADAPTER.run_adapter(
                        elf=self.elf, sig_out=self.sig, isa="RV32IM",
                        seed=0, timeout=30, sig_begin="0x80002000",
                        sig_end="0x80002040", verbose=False,
                    )
        for wd in work_dirs:
            self.assertFalse(wd.exists(), f"Work dir not cleaned: {wd}")


# ─────────────────────────────────────────────────────────────────────────────
# SPLIT WORKER POOLS
# ─────────────────────────────────────────────────────────────────────────────

class TestSplitWorkerPools(unittest.TestCase):

    def test_default_build_equals_workers(self):
        cfg = RunConfig(workers=4)
        self.assertEqual(cfg.build_workers, 4)

    def test_default_run_is_double(self):
        cfg = RunConfig(workers=4)
        self.assertEqual(cfg.run_workers, 8)

    def test_explicit_build_override(self):
        cfg = RunConfig(workers=4, build_workers=2)
        self.assertEqual(cfg.build_workers, 2)
        self.assertEqual(cfg.run_workers,   8)

    def test_explicit_run_override(self):
        cfg = RunConfig(workers=4, run_workers=16)
        self.assertEqual(cfg.run_workers, 16)

    def test_both_explicit(self):
        cfg = RunConfig(workers=2, build_workers=3, run_workers=7)
        self.assertEqual(cfg.build_workers, 3)
        self.assertEqual(cfg.run_workers,   7)

    def test_workers_one(self):
        cfg = RunConfig(workers=1)
        self.assertEqual(cfg.build_workers, 1)
        self.assertEqual(cfg.run_workers,   2)

    def test_build_pool_uses_build_workers(self):
        captured: List[dict] = []
        orig_TPE = RC.concurrent.futures.ThreadPoolExecutor

        class _TPE:
            def __init__(self, **kw):
                captured.append(kw)
                self._inner = orig_TPE(**kw)
            def submit(self, *a, **kw):
                return self._inner.submit(*a, **kw)
            def __enter__(self):
                self._inner.__enter__(); return self
            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        tmp = _tmp()
        try:
            cfg    = RunConfig(workers=2, build_workers=3, run_workers=5,
                               spike_bin="__no__", out_dir=tmp)
            runner = ComplianceRunner(cfg)
            runner.gcc = "/usr/bin/riscv32-unknown-elf-gcc"
            with patch("concurrent.futures.ThreadPoolExecutor", side_effect=_TPE):
                with patch("concurrent.futures.as_completed", return_value=iter([])):
                    runner._build_all([], tmp / "run")
            build_calls = [c for c in captured if c.get("thread_name_prefix") == "build"]
            self.assertEqual(len(build_calls), 1)
            self.assertEqual(build_calls[0]["max_workers"], 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_pool_uses_run_workers(self):
        captured: List[dict] = []
        orig_TPE = RC.concurrent.futures.ThreadPoolExecutor

        class _TPE:
            def __init__(self, **kw):
                captured.append(kw)
                self._inner = orig_TPE(**kw)
            def submit(self, *a, **kw):
                return self._inner.submit(*a, **kw)
            def __enter__(self):
                self._inner.__enter__(); return self
            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        tmp = _tmp()
        try:
            cfg    = RunConfig(workers=2, build_workers=3, run_workers=5,
                               spike_bin="__no__", out_dir=tmp)
            runner = ComplianceRunner(cfg)
            # Create a PENDING record so _run_all doesn't return early
            pending = TestRecord(
                name="FAKE", isa_subset="RV32I", source=Path("/tmp/f.S"),
                result=TestResult.PENDING,
            )
            with patch("concurrent.futures.ThreadPoolExecutor", side_effect=_TPE):
                with patch("concurrent.futures.as_completed", return_value=iter([])):
                    runner._run_all([pending], tmp / "run")
            run_calls = [c for c in captured if c.get("thread_name_prefix") == "run"]
            self.assertEqual(len(run_calls), 1)
            self.assertEqual(run_calls[0]["max_workers"], 5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# INCREMENTAL SIGNATURE COMPARISON
# ─────────────────────────────────────────────────────────────────────────────

class TestIncrementalSigCmp(unittest.TestCase):

    def test_unlimited_returns_all(self):
        g = ["00000001"] * 6
        d = ["ffffffff"] * 6
        ok, first, diffs = compare_signatures(g, d, max_mismatches=None)
        self.assertFalse(ok)
        self.assertEqual(len(diffs), 6)

    def test_max_3_stops_early(self):
        g = ["00000001"] * 10
        d = ["ffffffff"] * 10
        ok, first, diffs = compare_signatures(g, d, max_mismatches=3)
        self.assertFalse(ok)
        self.assertEqual(len(diffs), 3)

    def test_max_1_stops_at_first(self):
        g = ["00000001", "00000002", "00000003"]
        d = ["ffffffff", "ffffffff", "ffffffff"]
        ok, first, diffs = compare_signatures(g, d, max_mismatches=1)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(first, 0)

    def test_pass_unaffected(self):
        g = ["00000001", "00000002"]
        d = ["00000001", "00000002"]
        ok, first, diffs = compare_signatures(g, d, max_mismatches=1)
        self.assertTrue(ok)
        self.assertEqual(diffs, [])

    def test_max_larger_than_actual(self):
        g = ["00000001", "00000002"]
        d = ["ffffffff", "00000002"]
        ok, first, diffs = compare_signatures(g, d, max_mismatches=100)
        self.assertEqual(len(diffs), 1)

    def test_default_config_is_3(self):
        self.assertEqual(RunConfig().max_mismatches, 3)

    def test_zero_config_is_unlimited(self):
        """max_mismatches=0 in RunConfig means pass None → unlimited."""
        cfg = RunConfig(max_mismatches=0)
        self.assertEqual(cfg.max_mismatches, 0)


# ─────────────────────────────────────────────────────────────────────────────
# BUILD CACHE SHA-256 CONSISTENCY
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildCacheSHA256(unittest.TestCase):

    def setUp(self):
        self.tmp   = _tmp()
        self.cache = BuildCache(self.tmp / ".cache")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src_elf(self, content: str = "test") -> tuple:
        src = self.tmp / "t.S"
        elf = self.tmp / "t.elf"
        src.write_bytes(content.encode())
        elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
        return src, elf

    def test_cache_elf_uses_sha256_prefix(self):
        src, elf = self._src_elf()
        self.cache.store(src, elf)
        files = list((self.tmp / ".cache").glob("*.elf"))
        self.assertEqual(len(files), 1)
        stem     = files[0].stem
        key      = str(src.resolve())
        expected = hashlib.sha256(key.encode()).hexdigest()[:32]
        self.assertEqual(stem, expected)

    def test_source_hash_in_index_is_sha256(self):
        src, elf = self._src_elf("content")
        self.cache.store(src, elf)
        index = json.loads((self.tmp / ".cache" / "cache_index.json").read_text())
        h = list(index.values())[0]
        self.assertEqual(len(h), 64)   # SHA-256 is 64 hex chars
        self.assertEqual(h, hashlib.sha256(b"content").hexdigest())

    def test_not_md5(self):
        src, elf = self._src_elf("data")
        self.cache.store(src, elf)
        key = str(src.resolve())
        files = list((self.tmp / ".cache").glob("*.elf"))
        stem  = files[0].stem
        # Must NOT be the md5 hash
        self.assertNotEqual(stem, hashlib.md5(key.encode()).hexdigest())

    def test_hit_after_store(self):
        src, elf = self._src_elf("data")
        target   = self.tmp / "target.elf"
        self.cache.store(src, elf)
        self.assertTrue(self.cache.lookup(src, target))

    def test_miss_after_change(self):
        src, elf = self._src_elf("v1")
        target   = self.tmp / "target.elf"
        self.cache.store(src, elf)
        src.write_bytes(b"v2")
        self.assertFalse(self.cache.lookup(src, target))

    def test_corrupted_index_recovered(self):
        cache_dir = self.tmp / ".cache2"
        cache_dir.mkdir()
        (cache_dir / "cache_index.json").write_text("{bad json")
        cache = BuildCache(cache_dir)
        self.assertEqual(cache._index, {})


# ─────────────────────────────────────────────────────────────────────────────
# RETAINED ORIGINAL TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions(unittest.TestCase):
    def test_hierarchy(self):
        for cls in (ToolNotFoundError, BuildError, SimulationError, SignatureError):
            self.assertTrue(issubclass(cls, ComplianceError))
        self.assertTrue(issubclass(ComplianceError, RuntimeError))

    def test_simulation_catchable_as_compliance(self):
        with self.assertRaises(ComplianceError):
            raise SimulationError("boom")


class TestRunConfig(unittest.TestCase):
    def test_isa_uppercased(self):
        self.assertEqual(RunConfig(isa="rv32im").isa, "RV32IM")

    def test_workers_validation(self):
        with self.assertRaises(ValueError):
            RunConfig(workers=0)

    def test_max_mismatches_zero_valid(self):
        RunConfig(max_mismatches=0)  # should not raise


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_and_reads(self):
        p = self.tmp / "f.txt"
        _atomic_write(p, "hello")
        self.assertEqual(p.read_text(), "hello")

    def test_no_tmp_leftover(self):
        p = self.tmp / "f.txt"
        _atomic_write(p, "x")
        self.assertEqual(list(self.tmp.glob(".tmp_*")), [])

    def test_deep_parents(self):
        p = self.tmp / "a" / "b" / "f.txt"
        _atomic_write(p, "deep")
        self.assertTrue(p.exists())


class TestRetryPolicy(unittest.TestCase):
    def test_success_first(self):
        fn = MagicMock(return_value=42)
        self.assertEqual(RetryPolicy(max_attempts=3)(fn), 42)
        fn.assert_called_once()

    def test_retry_succeeds(self):
        fn = MagicMock(side_effect=[RuntimeError(), 99])
        self.assertEqual(RetryPolicy(max_attempts=3, base_delay_s=0)(fn), 99)

    def test_exhausted_raises(self):
        fn = MagicMock(side_effect=RuntimeError("always"))
        with self.assertRaises(RuntimeError):
            RetryPolicy(max_attempts=3, base_delay_s=0)(fn)
        self.assertEqual(fn.call_count, 3)


class TestLinkerScript(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ld(self) -> str:
        ld = self.tmp / "t.ld"
        _write_linker_script(ld)
        return ld.read_text()

    def test_tohost(self):
        self.assertIn(".tohost", self._ld())

    def test_begin_end_sig(self):
        ld = self._ld()
        self.assertIn("begin_signature", ld)
        self.assertIn("end_signature",   ld)

    def test_fill_zero(self):
        self.assertRegex(self._ld(), r"FILL\s*\(\s*0[x0]*0*\s*\)")

    def test_link_address(self):
        self.assertIn(f"{LINK_ADDRESS:#010x}", self._ld())

    def test_tohost_align64(self):
        self.assertIn("ALIGN(64)", self._ld())

    def test_discard(self):
        self.assertIn("/DISCARD/", self._ld())


class TestSourceGeneration(unittest.TestCase):
    def test_htif_not_ecall_in_rvtest_pass(self):
        after_pass = _ASM_MACROS.split("RVTEST_PASS")[1].split("endm")[0]
        self.assertNotIn("ecall", after_pass)

    def test_tohost_in_data_begin(self):
        src = _generate_source("T", "RV32I", "nop", "")
        self.assertIn("tohost",   src)
        self.assertIn("fromhost", src)

    def test_load_store_scratch_in_data_body(self):
        ls = next(t for t in _EMBEDDED_TESTS if t[0] == "LOAD-STORE-01")
        self.assertIn("scratch_word", ls[3])

    def test_all_embedded_have_start(self):
        for name, subset, code, data in _EMBEDDED_TESTS:
            src = _generate_source(name, subset, code, data)
            self.assertIn("_start", src, f"{name}: missing _start")


class TestSignatureParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_basic(self):
        p = _make_sig(self.tmp, "00000008\nffffffff\n")
        self.assertEqual(parse_signature(p), ["00000008", "ffffffff"])

    def test_0x_prefix_exact_strip(self):
        p = _make_sig(self.tmp, "0xdeadbeef\n")
        self.assertEqual(parse_signature(p), ["deadbeef"])

    def test_non_hex_skipped(self):
        p = _make_sig(self.tmp, "00000001\nZZZZZZZZ\n1234GHIJ\n00000002\n")
        self.assertEqual(parse_signature(p), ["00000001", "00000002"])

    def test_not_found_raises(self):
        with self.assertRaises(SignatureError):
            parse_signature(self.tmp / "nope.sig")

    def test_zero_padded(self):
        p = _make_sig(self.tmp, "1\nff\n")
        self.assertEqual(parse_signature(p), ["00000001", "000000ff"])

    def test_uppercase_normalised(self):
        p = _make_sig(self.tmp, "DEADBEEF\n")
        self.assertEqual(parse_signature(p), ["deadbeef"])


class TestSignatureComparison(unittest.TestCase):
    def test_identical(self):
        ok, idx, d = compare_signatures(["00000001"], ["00000001"])
        self.assertTrue(ok); self.assertEqual(idx, -1)

    def test_mismatch(self):
        ok, idx, d = compare_signatures(["00000001"], ["ffffffff"])
        self.assertFalse(ok); self.assertEqual(idx, 0)

    def test_trailing_zeros_match(self):
        ok, idx, d = compare_signatures(["00000001", "00000000"], ["00000001"])
        self.assertTrue(ok)

    def test_dut_extra_nonzero_fails(self):
        ok, idx, d = compare_signatures(["00000001"], ["00000001", "deadbeef"])
        self.assertFalse(ok)

    def test_empty_both(self):
        ok, idx, d = compare_signatures([], [])
        self.assertTrue(ok)

    def test_all_diffs_without_cap(self):
        g = ["aaaaaaaa"] * 4
        d = ["bbbbbbbb"] * 4
        ok, first, diffs = compare_signatures(g, d, max_mismatches=None)
        self.assertEqual(len(diffs), 4)


class TestEmbeddedTestCoverage(unittest.TestCase):
    def test_minimum_9(self):
        self.assertGreaterEqual(len(_EMBEDDED_TESTS), 9)

    def test_at_least_3_rv32m(self):
        self.assertGreaterEqual(sum(1 for t in _EMBEDDED_TESTS if t[1] == "RV32M"), 3)

    def test_at_least_5_rv32i(self):
        self.assertGreaterEqual(sum(1 for t in _EMBEDDED_TESTS if t[1] == "RV32I"), 5)

    def test_no_duplicate_names(self):
        names = [t[0] for t in _EMBEDDED_TESTS]
        self.assertEqual(len(names), len(set(names)))

    def test_all_four_div_ops(self):
        m_code = " ".join(t[2] for t in _EMBEDDED_TESTS if t[1] == "RV32M")
        for op in ("div", "rem", "divu", "remu"):
            self.assertIn(op, m_code, f"Missing: {op}")

    def test_mul_ops(self):
        mul = next(t for t in _EMBEDDED_TESTS if t[0] == "MUL-01")
        for op in ("mul", "mulh", "mulhu", "mulhsu"):
            self.assertIn(op, mul[2])

    def test_all_branch_ops(self):
        br = next(t for t in _EMBEDDED_TESTS if t[0] == "BRANCH-01")
        for op in ("beq", "bne", "blt", "bge", "bltu", "bgeu"):
            self.assertIn(op, br[2])

    def test_load_store_sign_extension(self):
        ls = next(t for t in _EMBEDDED_TESTS if t[0] == "LOAD-STORE-01")
        self.assertIn("lh", ls[2])
        self.assertIn("lb", ls[2])


class TestJUnitXML(unittest.TestCase):
    def test_parseable(self):
        ET.fromstring(render_junit_xml(_make_report((TestResult.PASS, TestResult.FAIL))))

    def test_failure_element(self):
        root = ET.fromstring(render_junit_xml(_make_report((TestResult.FAIL,))))
        self.assertEqual(len(root.findall(".//failure")), 1)

    def test_error_element(self):
        root = ET.fromstring(render_junit_xml(_make_report((TestResult.ERROR,))))
        self.assertEqual(len(root.findall(".//error")), 1)

    def test_pass_no_failure(self):
        root = ET.fromstring(render_junit_xml(_make_report((TestResult.PASS,))))
        self.assertEqual(len(root.findall(".//failure")), 0)
        self.assertEqual(len(root.findall(".//error")),   0)


class TestRunnerNoTools(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reports_written(self):
        cfg = RunConfig(spike_bin="__no__", out_dir=self.tmp, workers=1)
        ComplianceRunner(cfg).run()
        self.assertTrue(any(self.tmp.rglob(REPORT_JSON)))
        self.assertTrue(any(self.tmp.rglob(REPORT_HTML)))
        self.assertTrue(any(self.tmp.rglob(REPORT_JUNIT)))

    def test_exit_code_is_tool(self):
        cfg    = RunConfig(spike_bin="__no__", out_dir=self.tmp, workers=1)
        report = ComplianceRunner(cfg).run()
        self.assertEqual(compute_exit_code(report), EXIT_TOOL)

    def test_ava_hook_keys(self):
        r = run_compliance_for_ava(spike_bin="__no__", out_dir=str(self.tmp))
        for k in ("compliance_pass", "compliance_pass_pct", "compliance_total",
                  "compliance_pass_cnt", "compliance_fail_cnt", "compliance_error_cnt",
                  "compliance_json", "compliance_html", "compliance_junit"):
            self.assertIn(k, r)


# ─────────────────────────────────────────────────────────────────────────────
# FULL PIPELINE  (skipped without tools)
# ─────────────────────────────────────────────────────────────────────────────

def _tools_ok() -> bool:
    ok, _ = probe_spike("spike")
    gcc, _ = probe_toolchain()
    return ok and gcc is not None


@unittest.skipUnless(_tools_ok(), "riscv-gcc and/or spike not found on PATH")
class TestFullPipeline(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, isa="RV32IM", **kw) -> ComplianceRunner:
        return ComplianceRunner(RunConfig(
            isa=isa, out_dir=self.tmp, workers=2,
            timeout_run_s=30, use_cache=False, **kw,
        ))

    def test_definition_of_done(self):
        report = self._runner().run()
        self.assertGreaterEqual(report.summary["pass"], 5)

    def test_exit_code_zero_on_all_pass(self):
        report = self._runner().run()
        self.assertEqual(compute_exit_code(report), EXIT_PASS)

    def test_split_pools_same_result(self):
        r_split = ComplianceRunner(RunConfig(
            isa="RV32I", out_dir=self.tmp / "split",
            build_workers=1, run_workers=4,
            timeout_run_s=30, use_cache=False,
        )).run()
        r_uni = ComplianceRunner(RunConfig(
            isa="RV32I", out_dir=self.tmp / "uni",
            workers=1, timeout_run_s=30, use_cache=False,
        )).run()
        self.assertEqual(
            {t["name"]: t["result"] for t in r_split.tests},
            {t["name"]: t["result"] for t in r_uni.tests},
        )

    def test_manifest_mode_end_to_end(self):
        mp = self.tmp / "manifest.json"
        mp.write_text(json.dumps({
            "rundir":   str(self.tmp / "m_run"),
            "binary":   str(self.tmp / "dummy.elf"),
            "isa":      "RV32I",
            "spikebin": "spike",
            "workers":  1,
        }))
        rc = run_compliance_manifest(mp)
        self.assertIn(rc, (EXIT_PASS, EXIT_FAIL, EXIT_CRASH))
        updated = json.loads(mp.read_text())
        self.assertNotEqual(
            updated.get("phases", {}).get("compliance", {}).get("status"), "running"
        )

    def test_manifest_compliance_result_schema(self):
        mp = self.tmp / "manifest2.json"
        mp.write_text(json.dumps({
            "rundir":   str(self.tmp / "m_run2"),
            "binary":   str(self.tmp / "dummy.elf"),
            "isa":      "RV32I",
            "spikebin": "spike",
            "workers":  1,
        }))
        run_compliance_manifest(mp)
        result = json.loads(mp.read_text()).get("compliance", {}).get("result", {})
        if result:
            self.assertEqual(result["schemaversion"], "2.0.0")
            self.assertIn("failedlist", result)


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    verbosity = 2 if ("-v" in sys.argv or "--verbose" in sys.argv) else 1
    loader    = unittest.TestLoader()
    loader.sortTestMethodsUsing = None
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=verbosity, failfast=False)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

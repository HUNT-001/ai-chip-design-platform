#!/usr/bin/env python3
"""
tests/test_run_iss_integration.py
==================================
Integration tests for Agent C pipeline — schema v2.0.0.

All assertions use v2.0.0 wire names:
  src, regs, csrs, mem, schema_version, hart, fpregs

Tests cover:
  - write_commitlog() end-to-end (parser → JSONL)
  - validate_commitlog() schema v2.0.0 checks
  - atomic_update_manifest() dotted-key writes + crash-safe rename
  - run_iss_manifest() full contract: EXIT_CONFIG / EXIT_INFRA / EXIT_PASS
  - Exit code constants
  - ISSEfficiencyTracker basic DB round-trip
  - Full CLI path (mocked Spike subprocess)

Run: python tests/test_run_iss_integration.py
"""
from __future__ import annotations

import json, os, re, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "sim"))

import run_iss
from iss_efficiency import ISSEfficiencyTracker

# ── Fixtures ──────────────────────────────────────────────────────────────────

# 20 Spike --log-commits lines → 11 distinct (pc,instr) pairs → 11 commits
FIXTURE_B = """\
core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0
core   0: 3 0x80000000 (0x00000297) x5  0x80000000
core   0: 3 0x80000004 (0x02028593) addi    a1, t0, 32
core   0: 3 0x80000004 (0x02028593) x11 0x80000020
core   0: 3 0x80000008 (0x00002223) sw      zero, 4(zero)
core   0: 3 0x80000008 (0x00002223) mem 0x00000004 0x00000000
core   0: 3 0x8000000c (0x00500513) li      a0, 5
core   0: 3 0x8000000c (0x00500513) x10 0x00000005
core   0: 3 0x80000010 (0x00700593) li      a1, 7
core   0: 3 0x80000010 (0x00700593) x11 0x00000007
core   0: 3 0x80000014 (0x02b50633) mul     a2, a0, a1
core   0: 3 0x80000014 (0x02b50633) x12 0x00000023
core   0: 3 0x80000018 (0x02b54633) div     a2, a0, a1
core   0: 3 0x80000018 (0x02b54633) x12 0x00000000
core   0: 3 0x8000001c (0x02b56633) rem     a2, a0, a1
core   0: 3 0x8000001c (0x02b56633) x12 0x00000005
core   0: 3 0x80000020 (0x00000013) nop
core   0: 3 0x80000024 (0x00100513) li      a0, 1
core   0: 3 0x80000024 (0x00100513) x10 0x00000001
core   0: 3 0x80000028 (0x00100073) ebreak
"""

FIXTURE_A = """\
core   0: 0x80000000 (0x00000297) auipc   t0, 0x0
core   0: 0x80000004 (0x02028593) addi    a1, t0, 32
core   0: 0x80000008 (0x00002223) sw      zero, 4(zero)
core   0: 0x8000000c (0x30200073) mret
core   0: 0x80000010 (0x00000013) nop
"""

EXPECTED_B = 11   # distinct (pc,instr) pairs in FIXTURE_B
EXPECTED_A = 5

PC_RE    = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
INSTR_RE = re.compile(r"^0x[0-9a-fA-F]{4,8}$")
V2_REQUIRED = {"schema_version", "seq", "pc", "instr", "src", "hart", "fpregs"}
V2_OLD_KEYS = {"source", "reg_writes", "csr_writes", "mem_access"}


def assert_v2_commitlog(tc: unittest.TestCase, path: Path) -> list:
    """Full v2.0.0 structural validation of a JSONL file."""
    records = []
    with open(path) as f:
        for i, raw in enumerate(f):
            raw = raw.strip()
            if not raw:
                continue
            r = json.loads(raw)
            # Mandatory fields
            missing = V2_REQUIRED - r.keys()
            tc.assertFalse(missing, f"Line {i} missing: {missing}")
            # New mandatory values
            tc.assertEqual(r["schema_version"], "2.0.0", f"Line {i} schema_version")
            tc.assertEqual(r["hart"], 0, f"Line {i} hart")
            tc.assertIsNone(r["fpregs"], f"Line {i} fpregs")
            tc.assertEqual(r["src"], "iss", f"Line {i} src")
            # No old key leakage
            leaked = V2_OLD_KEYS & r.keys()
            tc.assertFalse(leaked, f"Line {i} old keys leaked: {leaked}")
            # Format checks
            tc.assertRegex(r["pc"],    PC_RE)
            tc.assertRegex(r["instr"], INSTR_RE)
            tc.assertEqual(r["seq"], len(records))
            records.append(r)
    return records


# ── write_commitlog ───────────────────────────────────────────────────────────

class TestWriteCommitlog(unittest.TestCase):

    def _run(self, fixture, fmt, expected):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "iss.commitlog.jsonl"
            count = run_iss.write_commitlog(fixture, p, fmt, None)
            self.assertEqual(count, expected, f"Expected {expected} records, got {count}")
            records = assert_v2_commitlog(self, p)
            self.assertEqual(len(records), expected)
            return records

    def test_format_b(self):    self._run(FIXTURE_B, "B", EXPECTED_B)
    def test_format_a(self):    self._run(FIXTURE_A, "A", EXPECTED_A)

    def test_max_records_cap(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.jsonl"
            count = run_iss.write_commitlog(FIXTURE_B, p, "B", max_records=3)
            self.assertEqual(count, 3)

    def test_regs_have_values(self):
        """regs must carry {rd, value} — not just index."""
        records = self._run(FIXTURE_B, "B", EXPECTED_B)
        auipc = records[0]
        self.assertIn("regs", auipc)
        self.assertEqual(auipc["regs"][0]["rd"], 5)
        self.assertEqual(auipc["regs"][0]["value"], "0x80000000",
                         "reg value must be preserved — Agent D needs it")

    def test_mem_renamed(self):
        records = self._run(FIXTURE_B, "B", EXPECTED_B)
        sw_rec = records[2]   # sw zero,4(zero)
        self.assertIn("mem", sw_rec)
        self.assertNotIn("mem_access", sw_rec)
        self.assertEqual(sw_rec["mem"]["addr"], "0x00000004")

    def test_no_regs_in_fmt_a(self):
        records = self._run(FIXTURE_A, "A", EXPECTED_A)
        for r in records:
            self.assertNotIn("regs", r)


# ── validate_commitlog v2.0.0 ────────────────────────────────────────────────

class TestValidateCommitlog(unittest.TestCase):

    def _write(self, d, fixture, fmt):
        p = Path(d) / "out.jsonl"
        run_iss.write_commitlog(fixture, p, fmt, None)
        return p

    def test_valid_b_passes(self):
        with tempfile.TemporaryDirectory() as d:
            errs = run_iss.validate_commitlog(self._write(d, FIXTURE_B, "B"))
            self.assertEqual(errs, [])

    def test_valid_a_passes(self):
        with tempfile.TemporaryDirectory() as d:
            errs = run_iss.validate_commitlog(self._write(d, FIXTURE_A, "A"))
            self.assertEqual(errs, [])

    def test_missing_schema_version_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.jsonl"
            p.write_text(json.dumps({
                "seq": 0, "pc": "0x80000000", "instr": "0x00000013",
                "src": "iss", "hart": 0, "fpregs": None,
                # schema_version deliberately absent
            }) + "\n")
            errs = run_iss.validate_commitlog(p)
            self.assertTrue(any("schema_version" in e for e in errs))

    def test_wrong_schema_version_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.jsonl"
            p.write_text(json.dumps({
                "schema_version": "1.0.0",   # wrong
                "seq": 0, "pc": "0x80000000", "instr": "0x00000013",
                "src": "iss", "hart": 0, "fpregs": None,
            }) + "\n")
            errs = run_iss.validate_commitlog(p)
            self.assertTrue(any("schema_version" in e for e in errs))

    def test_missing_src_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.jsonl"
            p.write_text(json.dumps({
                "schema_version": "2.0.0",
                "seq": 0, "pc": "0x80000000", "instr": "0x00000013",
                # src absent
                "hart": 0, "fpregs": None,
            }) + "\n")
            errs = run_iss.validate_commitlog(p)
            self.assertTrue(any("src" in e for e in errs))

    def test_invalid_pc_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.jsonl"
            p.write_text(json.dumps({
                "schema_version": "2.0.0",
                "seq": 0, "pc": "BADHEX", "instr": "0x00000013",
                "src": "iss", "hart": 0, "fpregs": None,
            }) + "\n")
            errs = run_iss.validate_commitlog(p)
            self.assertTrue(any("pc" in e for e in errs))


# ── atomic_update_manifest ────────────────────────────────────────────────────

class TestAtomicUpdateManifest(unittest.TestCase):

    def test_simple_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "manifest.json"
            p.write_text(json.dumps({"status": "pending"}))
            run_iss.atomic_update_manifest(p, {"status": "running"})
            self.assertEqual(json.loads(p.read_text())["status"], "running")

    def test_dotted_key_creates_nesting(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "manifest.json"
            p.write_text(json.dumps({}))
            run_iss.atomic_update_manifest(p, {
                "phases.iss.status": "completed",
                "phases.iss.duration_s": 1.23,
                "outputs.iss_commitlog": "outputs/iss_commitlog.jsonl",
            })
            m = json.loads(p.read_text())
            self.assertEqual(m["phases"]["iss"]["status"], "completed")
            self.assertAlmostEqual(m["phases"]["iss"]["duration_s"], 1.23)
            self.assertEqual(m["outputs"]["iss_commitlog"], "outputs/iss_commitlog.jsonl")

    def test_no_tmp_file_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "manifest.json"
            p.write_text(json.dumps({"x": 1}))
            run_iss.atomic_update_manifest(p, {"x": 2})
            # .tmp file must be gone (rename is atomic on POSIX)
            self.assertFalse(p.with_suffix(".tmp").exists())

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "manifest.json"
            p.write_text(json.dumps({"phases": {"iss": {"status": "pending"}}}))
            run_iss.atomic_update_manifest(p, {"phases.iss.status": "completed"})
            run_iss.atomic_update_manifest(p, {"phases.iss.status": "completed"})
            m = json.loads(p.read_text())
            self.assertEqual(m["phases"]["iss"]["status"], "completed")


# ── Exit code constants ───────────────────────────────────────────────────────

class TestExitCodes(unittest.TestCase):
    def test_pass(self):   self.assertEqual(run_iss.EXIT_PASS,   0)
    def test_infra(self):  self.assertEqual(run_iss.EXIT_INFRA,  2)
    def test_config(self): self.assertEqual(run_iss.EXIT_CONFIG, 3)


# ── run_iss_manifest() contract ───────────────────────────────────────────────

class TestRunIssManifest(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _make_manifest(self, **extra) -> Path:
        run_dir = self.tmpdir / "run"
        elf     = self.tmpdir / "test.elf"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
        data = {"rundir": str(run_dir), "binary": str(elf), "isa": "rv32im", **extra}
        p = self.tmpdir / "manifest.json"
        p.write_text(json.dumps(data))
        return p

    def _mock_spike(self, fixture=FIXTURE_B):
        caps = {"found": True, "version": "mock", "has_log_commits": True,
                "has_commit_log": False, "has_l_flag": True, "spike_path": "spike"}
        return (
            patch.object(run_iss, "probe_spike",        return_value=caps),
            patch.object(run_iss, "run_spike_process",  return_value=(0, fixture)),
        )

    def test_pass_exit_code(self):
        mp = self._make_manifest()
        p1, p2 = self._mock_spike()
        with p1, p2:
            rc = run_iss.run_iss_manifest(mp)
        self.assertEqual(rc, run_iss.EXIT_PASS)

    def test_commitlog_created(self):
        mp = self._make_manifest()
        p1, p2 = self._mock_spike()
        with p1, p2:
            run_iss.run_iss_manifest(mp)
        run_dir = Path(json.loads(mp.read_text())["rundir"])
        self.assertTrue((run_dir / "outputs" / "iss_commitlog.jsonl").exists())

    def test_manifest_phases_written(self):
        mp = self._make_manifest()
        p1, p2 = self._mock_spike()
        with p1, p2:
            run_iss.run_iss_manifest(mp)
        m = json.loads(mp.read_text())
        self.assertEqual(m["phases"]["iss"]["status"], "completed")
        self.assertEqual(m["outputs"]["iss_commitlog"], "outputs/iss_commitlog.jsonl")
        self.assertIn("commit_count", m["phases"]["iss"])
        self.assertEqual(m["phases"]["iss"]["commit_count"], EXPECTED_B)

    def test_output_is_v2_schema(self):
        mp = self._make_manifest()
        p1, p2 = self._mock_spike()
        with p1, p2:
            run_iss.run_iss_manifest(mp)
        run_dir = Path(json.loads(mp.read_text())["rundir"])
        records = assert_v2_commitlog(self, run_dir / "outputs" / "iss_commitlog.jsonl")
        self.assertEqual(len(records), EXPECTED_B)

    def test_missing_binary_field(self):
        """Manifest without 'binary' → EXIT_CONFIG."""
        run_dir = self.tmpdir / "run"
        mp = self.tmpdir / "manifest.json"
        mp.write_text(json.dumps({"rundir": str(run_dir), "isa": "rv32im"}))
        rc = run_iss.run_iss_manifest(mp)
        self.assertEqual(rc, run_iss.EXIT_CONFIG)
        m = json.loads(mp.read_text())
        self.assertEqual(m["phases"]["iss"]["status"], "error")

    def test_missing_elf_file(self):
        """ELF path in manifest does not exist → EXIT_CONFIG."""
        mp = self.tmpdir / "manifest.json"
        mp.write_text(json.dumps({
            "rundir": str(self.tmpdir / "run"),
            "binary": "/nonexistent/prog.elf",
            "isa": "rv32im",
        }))
        rc = run_iss.run_iss_manifest(mp)
        self.assertEqual(rc, run_iss.EXIT_CONFIG)

    def test_spike_not_found(self):
        """Spike binary missing → EXIT_INFRA."""
        mp = self._make_manifest()
        caps = {"found": False, "version": "x", "has_log_commits": False,
                "has_commit_log": False, "has_l_flag": True, "spike_path": "spike"}
        with patch.object(run_iss, "probe_spike", return_value=caps):
            rc = run_iss.run_iss_manifest(mp)
        self.assertEqual(rc, run_iss.EXIT_INFRA)

    def test_spike_empty_output(self):
        """Spike produces no output → EXIT_INFRA."""
        mp = self._make_manifest()
        p1, p2 = self._mock_spike(fixture="")
        with p1, p2:
            rc = run_iss.run_iss_manifest(mp)
        self.assertEqual(rc, run_iss.EXIT_INFRA)

    def test_manifest_not_found(self):
        """Non-existent manifest path → EXIT_CONFIG."""
        rc = run_iss.run_iss_manifest(self.tmpdir / "nonexistent.json")
        self.assertEqual(rc, run_iss.EXIT_CONFIG)

    def test_fmt_a_fallback(self):
        """When Spike lacks --log-commits, FORMAT A is parsed."""
        mp = self._make_manifest()
        caps = {"found": True, "version": "mock", "has_log_commits": False,
                "has_commit_log": False, "has_l_flag": True, "spike_path": "spike"}
        with patch.object(run_iss, "probe_spike", return_value=caps), \
             patch.object(run_iss, "run_spike_process", return_value=(0, FIXTURE_A)):
            rc = run_iss.run_iss_manifest(mp)
        self.assertEqual(rc, run_iss.EXIT_PASS)
        run_dir = Path(json.loads(mp.read_text())["rundir"])
        records = assert_v2_commitlog(self, run_dir / "outputs" / "iss_commitlog.jsonl")
        self.assertEqual(len(records), EXPECTED_A)


# ── Full CLI main() with --manifest ──────────────────────────────────────────

class TestCLIManifestMode(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_manifest_flag_routes_to_manifest_mode(self):
        run_dir = self.tmpdir / "run"
        elf     = self.tmpdir / "t.elf"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
        mp = self.tmpdir / "manifest.json"
        mp.write_text(json.dumps({"rundir": str(run_dir), "binary": str(elf), "isa": "rv32im"}))

        caps = {"found": True, "version": "mock", "has_log_commits": True,
                "has_commit_log": False, "has_l_flag": True, "spike_path": "spike"}
        with patch.object(run_iss, "probe_spike", return_value=caps), \
             patch.object(run_iss, "run_spike_process", return_value=(0, FIXTURE_B)):
            rc = run_iss.main(["--manifest", str(mp)])
        self.assertEqual(rc, run_iss.EXIT_PASS)


# ── Full CLI legacy mode ──────────────────────────────────────────────────────

class TestCLILegacyMode(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _run_legacy(self, fixture=FIXTURE_B, extra_argv=None):
        run_dir = self.tmpdir / "run"
        elf     = self.tmpdir / "t.elf"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
        caps = {"found": True, "version": "mock", "has_log_commits": True,
                "has_commit_log": False, "has_l_flag": True, "spike_path": "spike"}
        argv = ["--isa", "RV32IM", "--elf", str(elf), "--out", str(run_dir),
                "--validate"] + (extra_argv or [])
        with patch.object(run_iss, "probe_spike", return_value=caps), \
             patch.object(run_iss, "run_spike_process", return_value=(0, fixture)):
            return run_iss.main(argv), run_dir

    def test_legacy_pass(self):
        rc, run_dir = self._run_legacy()
        self.assertEqual(rc, run_iss.EXIT_PASS)

    def test_legacy_v2_output(self):
        _, run_dir = self._run_legacy()
        records = assert_v2_commitlog(self, run_dir / "iss.commitlog.jsonl")
        self.assertEqual(len(records), EXPECTED_B)

    def test_missing_elf(self):
        rc = run_iss.main(["--isa", "RV32IM", "--elf", "/bad/path.elf",
                           "--out", str(self.tmpdir)])
        self.assertEqual(rc, run_iss.EXIT_CONFIG)

    def test_spike_not_found_legacy(self):
        elf = self.tmpdir / "t.elf"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
        caps = {"found": False, "version": "x", "has_log_commits": False,
                "has_commit_log": False, "has_l_flag": True, "spike_path": "spike"}
        with patch.object(run_iss, "probe_spike", return_value=caps):
            rc = run_iss.main(["--isa", "RV32IM", "--elf", str(elf),
                               "--out", str(self.tmpdir / "run")])
        self.assertEqual(rc, run_iss.EXIT_INFRA)


# ── ISSEfficiencyTracker ──────────────────────────────────────────────────────

class TestISSEfficiencyTracker(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "test.db"

    def tearDown(self):
        self._td.cleanup()

    def test_record_and_retrieve(self):
        with ISSEfficiencyTracker(self.db) as t:
            t.record_run("rv32im", 50000, 1.5, "log_commits", 1)
            counts = t.recent_commit_counts("rv32im", 10)
            self.assertEqual(counts, [50000])

    def test_no_plateau_insufficient_data(self):
        with ISSEfficiencyTracker(self.db) as t:
            for i in range(5):   # fewer than window=10
                t.record_run("rv32im", 50000 + i, 1.0, "log_commits", 1)
            self.assertFalse(t.is_plateau("rv32im", window=10))

    def test_plateau_detected(self):
        with ISSEfficiencyTracker(self.db) as t:
            # 10 runs with near-identical counts → variance ≈ 0
            for _ in range(10):
                t.record_run("rv32im", 50000, 1.0, "log_commits", 1)
            self.assertTrue(t.is_plateau("rv32im", window=10, variance_threshold=500))

    def test_no_plateau_high_variance(self):
        with ISSEfficiencyTracker(self.db) as t:
            for i in range(10):
                t.record_run("rv32im", 1000 + i * 10000, 1.0, "log_commits", 1)
            self.assertFalse(t.is_plateau("rv32im", window=10, variance_threshold=500))

    def test_stats(self):
        with ISSEfficiencyTracker(self.db) as t:
            t.record_run("rv32im", 1000, 1.0, "log_commits", 1)
            t.record_run("rv32im", 2000, 2.0, "log_commits", 1)
            s = t.stats("rv32im")
            self.assertEqual(s["total_runs"], 2)
            self.assertEqual(s["total_commits"], 3000)
            self.assertAlmostEqual(s["avg_duration_s"], 1.5, places=1)

    def test_different_isa_isolation(self):
        with ISSEfficiencyTracker(self.db) as t:
            for _ in range(10):
                t.record_run("rv32im",  50000, 1.0, "log_commits", 1)
            for _ in range(10):
                t.record_run("rv64gc", 10000, 1.0, "log_commits", 1)
            # rv64gc plateau doesn't affect rv32im
            self.assertTrue(t.is_plateau("rv32im", window=10))
            self.assertTrue(t.is_plateau("rv64gc", window=10))

    def test_context_manager(self):
        with ISSEfficiencyTracker(self.db) as t:
            t.record_run("rv32im", 100, 0.1, "log_commits", 1)
        # DB closed — re-open should still have data
        with ISSEfficiencyTracker(self.db) as t:
            self.assertEqual(len(t.recent_commit_counts("rv32im")), 1)


# ── manifest + iss_efficiency round-trip ─────────────────────────────────────

class TestManifestEfficiencyIntegration(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_db_created_after_manifest_run(self):
        run_dir = self.tmpdir / "run"
        elf = self.tmpdir / "t.elf"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
        mp = self.tmpdir / "manifest.json"
        mp.write_text(json.dumps({"rundir": str(run_dir), "binary": str(elf), "isa": "rv32im"}))
        caps = {"found": True, "version": "mock", "has_log_commits": True,
                "has_commit_log": False, "has_l_flag": True, "spike_path": "spike"}
        with patch.object(run_iss, "probe_spike", return_value=caps), \
             patch.object(run_iss, "run_spike_process", return_value=(0, FIXTURE_B)):
            run_iss.run_iss_manifest(mp)
        db = run_dir / "iss_metrics.db"
        self.assertTrue(db.exists(), "iss_metrics.db should be created by manifest mode")
        with ISSEfficiencyTracker(db) as t:
            counts = t.recent_commit_counts("rv32im")
        self.assertEqual(counts, [EXPECTED_B])


# ── Real Spike gate (skipped unless RV32IM_ELF set) ──────────────────────────

class TestRealSpike(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        cls.elf  = os.environ.get("RV32IM_ELF")
        if not (cls.elf and Path(cls.elf).exists() and shutil.which("spike")):
            raise unittest.SkipTest(
                "Set RV32IM_ELF=<path> and ensure 'spike' is on PATH."
            )

    def test_real_spike_v2_output(self):
        with tempfile.TemporaryDirectory() as d:
            rc = run_iss.main([
                "--isa", "RV32IM", "--elf", self.elf,
                "--out", d, "--validate", "--max-instrs", "50000",
            ])
            self.assertEqual(rc, run_iss.EXIT_PASS)
            records = assert_v2_commitlog(self, Path(d) / "iss.commitlog.jsonl")
            self.assertGreater(len(records), 0)
            print(f"\n  Real Spike: {len(records)} v2.0.0 commit records")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""
tests/test_spike_parser.py
==========================
Unit tests for sim/spike_parser.py — AVA schema v2.0.0.

All field names use the v2.0.0 wire names:
  src (not source), regs (not reg_writes), csrs (not csr_writes), mem (not mem_access)

Run: python tests/test_spike_parser.py
"""
from __future__ import annotations
import json, re, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "sim"))
from spike_parser import detect_format, parse_spike_log, _reg_idx, _hex, SCHEMA_VERSION

# ── Synthetic Spike log samples ──────────────────────────────────────────────

FMT_A = """\
core   0: 0x80000000 (0x00000297) auipc   t0, 0x0
core   0: 0x80000004 (0x02028593) addi    a1, t0, 32
core   0: 0x80000008 (0x00002223) sw      zero, 4(zero)
core   0: 0x8000000c (0x30200073) mret
"""

FMT_B_BASIC = """\
core   0: 3 0x80000000 (0x00000297) x5  0x80000000
core   0: 3 0x80000004 (0x02028593) x11 0x80000020
core   0: 3 0x80000008 (0x00002223) mem 0x00000004 0x00000000
core   0: 3 0x8000000c (0x30200073) x0  0x00000000
"""

FMT_B_DISASM_THEN_WB = """\
core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0
core   0: 3 0x80000000 (0x00000297) x5  0x80000000
core   0: 3 0x80000004 (0x02028593) addi    a1, t0, 32
core   0: 3 0x80000004 (0x02028593) x11 0x80000020
"""

FMT_B_CSR = """\
core   0: 3 0x80000100 (0x30200073) csrr    t0, mstatus
core   0: 3 0x80000100 (0x30200073) csr 0x300 0x00001800
core   0: 3 0x80000104 (0x00028513) mv      a0, t0
core   0: 3 0x80000104 (0x00028513) x10 0x00001800
"""

FMT_B_TRAP = """\
core   0: 3 0x80000200 (0x00002003) lw      zero, 0(zero)
core   0: exception load_access_fault, epc 0x80000200
core   0: 3 0x80000204 (0x00000013) nop
"""

FMT_B_INLINE_WB = """\
core   0: 3 0x80000000 (0x00000297) auipc   t0, 0x0 x5 0x80000000
core   0: 3 0x80000004 (0x02028593) addi    a1, t0, 32 x11 0x80000020
"""

FMT_B_MIXED_PRIV = """\
core   0: 3 0x80000000 (0x00000013) nop
core   0: 0 0x00000100 (0x00000013) nop
core   0: 1 0x00001000 (0x00000013) nop
"""


def _parse(text, fmt=None):
    return parse_spike_log(text, source="iss", fmt=fmt)


# ── Helpers ───────────────────────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):
    def test_hex_basic(self):   self.assertEqual(_hex(0x80000000, 8), "0x80000000")
    def test_hex_zero(self):    self.assertEqual(_hex(0, 8), "0x00000000")
    def test_reg_numeric(self): self.assertEqual(_reg_idx("x5"), 5)
    def test_reg_abi(self):     self.assertEqual(_reg_idx("t0"), 5)
    def test_reg_a0(self):      self.assertEqual(_reg_idx("a0"), 10)
    def test_reg_s11(self):     self.assertEqual(_reg_idx("s11"), 27)
    def test_reg_invalid(self): self.assertIsNone(_reg_idx("pc"))
    def test_schema_version(self): self.assertEqual(SCHEMA_VERSION, "2.0.0")


# ── Schema v2.0.0 mandatory fields ───────────────────────────────────────────

class TestSchemaV2Mandatory(unittest.TestCase):
    """Every record must carry the new mandatory v2.0.0 fields."""

    REQUIRED = {"schema_version", "seq", "pc", "instr", "src", "hart", "fpregs"}

    def _assert_v2(self, text, fmt=None):
        for i, r in enumerate(_parse(text, fmt=fmt)):
            missing = self.REQUIRED - r.keys()
            self.assertFalse(missing, f"Record {i} missing: {missing}")
            self.assertEqual(r["schema_version"], "2.0.0",
                             f"Record {i} wrong schema_version")
            self.assertEqual(r["src"], "iss", f"Record {i} wrong src")
            self.assertEqual(r["hart"], 0, f"Record {i} wrong hart")
            self.assertIsNone(r["fpregs"], f"Record {i} fpregs not None")

    def test_fmt_a(self):           self._assert_v2(FMT_A, "A")
    def test_fmt_b_basic(self):     self._assert_v2(FMT_B_BASIC, "B")
    def test_fmt_b_csr(self):       self._assert_v2(FMT_B_CSR, "B")
    def test_fmt_b_trap(self):      self._assert_v2(FMT_B_TRAP, "B")
    def test_fmt_b_inline(self):    self._assert_v2(FMT_B_INLINE_WB, "B")
    def test_auto_detect(self):     self._assert_v2(FMT_B_BASIC)


# ── src field (renamed from 'source') ────────────────────────────────────────

class TestSrcField(unittest.TestCase):
    def test_src_present(self):
        r = _parse(FMT_B_BASIC, "B")[0]
        self.assertIn("src", r)
        self.assertEqual(r["src"], "iss")

    def test_source_absent(self):
        """Old 'source' key must NOT appear in output."""
        r = _parse(FMT_B_BASIC, "B")[0]
        self.assertNotIn("source", r, "'source' key should not exist in v2.0.0 output")

    def test_src_custom_value(self):
        records = parse_spike_log(FMT_B_BASIC, source="rtl", fmt="B")
        self.assertEqual(records[0]["src"], "rtl")


# ── regs field (renamed from 'reg_writes', values preserved) ─────────────────

class TestRegsField(unittest.TestCase):
    def test_regs_key_present(self):
        r = _parse(FMT_B_BASIC, "B")[0]   # auipc → x5=0x80000000
        self.assertIn("regs", r)

    def test_reg_writes_absent(self):
        """Old 'reg_writes' key must NOT appear."""
        r = _parse(FMT_B_BASIC, "B")[0]
        self.assertNotIn("reg_writes", r, "'reg_writes' must not exist in v2.0.0")

    def test_regs_preserves_values(self):
        """Values must NOT be dropped — Agent D needs them for reg mismatch detection."""
        r = _parse(FMT_B_BASIC, "B")[0]
        regs = r["regs"]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["rd"], 5)
        self.assertEqual(regs[0]["value"], "0x80000000",
                         "reg value must be preserved (not dropped to index-only)")

    def test_regs_structure(self):
        """regs items must be {rd: int, value: str}."""
        for r in _parse(FMT_B_BASIC, "B"):
            for rw in r.get("regs", []):
                self.assertIn("rd", rw)
                self.assertIn("value", rw)
                self.assertIsInstance(rw["rd"], int)
                self.assertIsInstance(rw["value"], str)

    def test_x0_suppressed(self):
        """x0 writes must not appear in regs (x0 is always 0)."""
        for r in _parse(FMT_B_BASIC, "B"):
            for rw in r.get("regs", []):
                self.assertNotEqual(rw["rd"], 0)

    def test_fmt_a_no_regs(self):
        """FORMAT A has no writeback data — regs must be absent."""
        for r in _parse(FMT_A, "A"):
            self.assertNotIn("regs", r)


# ── csrs field (renamed from 'csr_writes') ───────────────────────────────────

class TestCsrsField(unittest.TestCase):
    def test_csrs_key_present(self):
        r = _parse(FMT_B_CSR, "B")[0]
        self.assertIn("csrs", r)

    def test_csr_writes_absent(self):
        r = _parse(FMT_B_CSR, "B")[0]
        self.assertNotIn("csr_writes", r, "'csr_writes' must not exist in v2.0.0")

    def test_csrs_values_preserved(self):
        csrs = _parse(FMT_B_CSR, "B")[0]["csrs"]
        self.assertEqual(len(csrs), 1)
        self.assertEqual(csrs[0]["addr"], "0x300")
        self.assertEqual(csrs[0]["value"], "0x00001800")
        self.assertEqual(csrs[0].get("name"), "mstatus")


# ── mem field (renamed from 'mem_access') ────────────────────────────────────

class TestMemField(unittest.TestCase):
    def test_mem_key_present(self):
        r = _parse(FMT_B_BASIC, "B")[2]   # sw → mem
        self.assertIn("mem", r)

    def test_mem_access_absent(self):
        r = _parse(FMT_B_BASIC, "B")[2]
        self.assertNotIn("mem_access", r, "'mem_access' must not exist in v2.0.0")

    def test_mem_addr(self):
        m = _parse(FMT_B_BASIC, "B")[2]["mem"]
        self.assertEqual(m["addr"], "0x00000004")


# ── FORMAT A ──────────────────────────────────────────────────────────────────

class TestFormatA(unittest.TestCase):
    def setUp(self): self.R = _parse(FMT_A, fmt="A")
    def test_count(self):   self.assertEqual(len(self.R), 4)
    def test_seq(self):     [self.assertEqual(r["seq"], i) for i, r in enumerate(self.R)]
    def test_pc(self):      self.assertEqual(self.R[0]["pc"], "0x80000000")
    def test_instr(self):   self.assertEqual(self.R[0]["instr"], "0x00000297")
    def test_disasm(self):  self.assertIn("auipc", self.R[0].get("disasm", ""))


# ── FORMAT B basic ────────────────────────────────────────────────────────────

class TestFormatBBasic(unittest.TestCase):
    def setUp(self): self.R = _parse(FMT_B_BASIC, fmt="B")
    def test_count(self):   self.assertEqual(len(self.R), 4)
    def test_priv_m(self):  [self.assertEqual(r.get("priv"), "M") for r in self.R]

    def test_x5_reg(self):
        rgs = self.R[0]["regs"]
        self.assertEqual(rgs[0]["rd"], 5)
        self.assertEqual(rgs[0]["value"], "0x80000000")

    def test_a1_reg(self):
        self.assertEqual(self.R[1]["regs"][0]["rd"], 11)

    def test_mem_present(self):
        self.assertIn("mem", self.R[2])
        self.assertEqual(self.R[2]["mem"]["addr"], "0x00000004")


# ── FORMAT B disasm-then-wb ───────────────────────────────────────────────────

class TestFormatBDisasmThenWB(unittest.TestCase):
    def setUp(self): self.R = _parse(FMT_B_DISASM_THEN_WB, fmt="B")
    def test_count(self):  self.assertEqual(len(self.R), 2)
    def test_disasm(self): self.assertIn("auipc", self.R[0].get("disasm", ""))
    def test_regs(self):   self.assertEqual(self.R[0]["regs"][0]["rd"], 5)


# ── FORMAT B CSR ──────────────────────────────────────────────────────────────

class TestFormatBCSR(unittest.TestCase):
    def setUp(self): self.R = _parse(FMT_B_CSR, fmt="B")
    def test_count(self): self.assertEqual(len(self.R), 2)
    def test_csrs(self):
        csrs = self.R[0]["csrs"]
        self.assertEqual(csrs[0]["addr"], "0x300")
        self.assertEqual(csrs[0]["value"], "0x00001800")
        self.assertEqual(csrs[0].get("name"), "mstatus")


# ── FORMAT B trap ─────────────────────────────────────────────────────────────

class TestFormatBTrap(unittest.TestCase):
    def setUp(self): self.R = _parse(FMT_B_TRAP, fmt="B")
    def test_count(self): self.assertEqual(len(self.R), 2)
    def test_trap(self):
        t = self.R[0]["trap"]
        self.assertEqual(t["cause"], "0x00000005")
        self.assertFalse(t["is_interrupt"])
    def test_nop_no_trap(self): self.assertNotIn("trap", self.R[1])


# ── FORMAT B inline writeback ─────────────────────────────────────────────────

class TestFormatBInline(unittest.TestCase):
    def setUp(self): self.R = _parse(FMT_B_INLINE_WB, fmt="B")
    def test_count(self): self.assertEqual(len(self.R), 2)
    def test_disasm_clean(self):
        self.assertIn("auipc", self.R[0].get("disasm", ""))
        self.assertNotIn("0x80000000", self.R[0].get("disasm", ""))
    def test_regs(self): self.assertEqual(self.R[0]["regs"][0]["rd"], 5)


# ── Privilege decode ──────────────────────────────────────────────────────────

class TestPrivDecode(unittest.TestCase):
    def setUp(self): self.R = _parse(FMT_B_MIXED_PRIV, fmt="B")
    def test_machine(self):    self.assertEqual(self.R[0]["priv"], "M")
    def test_user(self):       self.assertEqual(self.R[1]["priv"], "U")
    def test_supervisor(self): self.assertEqual(self.R[2]["priv"], "S")


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):
    def test_empty(self):       self.assertEqual(_parse("", "B"), [])
    def test_whitespace(self):  self.assertEqual(_parse("\n\n", "B"), [])
    def test_noise_ignored(self):
        R = _parse("bbl loader\ncore   0: 3 0x80000000 (0x00000013) x0 0x0\nDone\n", "B")
        self.assertEqual(len(R), 1)
    def test_jsonl_roundtrip(self):
        for r in _parse(FMT_B_BASIC, "B"):
            self.assertEqual(json.loads(json.dumps(r))["seq"], r["seq"])
    def test_auto_detect(self):
        self.assertEqual(len(_parse(FMT_B_BASIC)), len(_parse(FMT_B_BASIC, "B")))


# ── Full schema conformance ───────────────────────────────────────────────────

class TestFullSchemaConformance(unittest.TestCase):
    PC_RE    = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
    INSTR_RE = re.compile(r"^0x[0-9a-fA-F]{4,8}$")
    REQUIRED = {"schema_version", "seq", "pc", "instr", "src", "hart", "fpregs"}

    def _check(self, text, fmt=None):
        for i, r in enumerate(_parse(text, fmt=fmt)):
            missing = self.REQUIRED - r.keys()
            self.assertFalse(missing, f"rec {i} missing {missing}")
            self.assertRegex(r["pc"],    self.PC_RE)
            self.assertRegex(r["instr"], self.INSTR_RE)
            self.assertIn(r["src"], ("rtl","iss","formal"))
            self.assertEqual(r["seq"], i)
            self.assertEqual(r["schema_version"], "2.0.0")
            self.assertNotIn("source",     r, "old key 'source' leaked")
            self.assertNotIn("reg_writes", r, "old key 'reg_writes' leaked")
            self.assertNotIn("csr_writes", r, "old key 'csr_writes' leaked")
            self.assertNotIn("mem_access", r, "old key 'mem_access' leaked")

    def test_fmt_a(self):           self._check(FMT_A, "A")
    def test_fmt_b_basic(self):     self._check(FMT_B_BASIC, "B")
    def test_fmt_b_csr(self):       self._check(FMT_B_CSR, "B")
    def test_fmt_b_trap(self):      self._check(FMT_B_TRAP, "B")
    def test_fmt_b_inline(self):    self._check(FMT_B_INLINE_WB, "B")
    def test_auto_detect(self):     self._check(FMT_B_BASIC)


if __name__ == "__main__":
    unittest.main(verbosity=2)

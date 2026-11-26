"""A record's register writes are compared as a whole, by both comparators (#159).

The rule, written from docs/dut_commit_log.md and #159, not from either
comparator: when both sides report register writes on a record (`regs`, or a
legacy `rd`), the two sides must write the same set of registers with the same
values.  `{}` against a write, a write against `{}`, an extra register, a
missing register, a different register or a different value is a register
finding.  Before #159 the orchestrator read only the first register of each
side and skipped an empty side, and AGENT_D read only the first register write,
so all of those passed except a differing first value.

Normalisation, the same in both comparators:

* x0 is architecturally zero, and the schema forbids it in `regs`.  A write to
  x0 (or `zero`) is ignored on both sides, so `{"x0": "0x00000000"}` equals
  `{}`.  AGENT_D's separate x0 invariant (x0 written non-zero is a finding of
  its own) is not part of this rule and is not changed by it.
* A register name is its index: `a0` and `x10` are the same register.
* A value is a number: `0xa` and `0x0000000a` are the same value.

A side that does not report register writes is not part of this rule: the
#164/#192 rules for it are unchanged.

The mutation-table rows are checked on both real tables (the recorded Spike
logs and the recorded Ibex run), and the fabricated pairs are run through both
comparators, which must agree with the rule and so with each other.
"""
import copy
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


table = _load("comparator_mutation_table", REPO_ROOT / "tests" / "test_comparator_mutation_table.py")
ibex = _load("ibex_comparator_validation", REPO_ROOT / "tests" / "test_ibex_comparator_validation.py")
cmp_ = ibex.cmp_

COMPARATORS = ("orchestrator", "agent_d")

#: The register-write rows of the mutation table this rule covers.
#: register_name is the same rule seen from the name: a different register
#: written with the same value is a different set of registers.
ROWS = ("later_register_write", "missing_write_rtl", "missing_write_iss",
        "extra_write", "register_name")


def run(comparator, rtl, iss, extra=()):
    with tempfile.TemporaryDirectory() as td:
        if comparator == "orchestrator":
            return cmp_.orchestrator(rtl, iss, Path(td) / "orch")
        return cmp_.agent_d(rtl, iss, Path(td), extra)


def register_finding(comparator, res):
    """True when the result is a mismatch, and every mismatch is a register one."""
    if comparator == "orchestrator":
        found = res["findings"]
        return bool(found) and all(f.startswith("register_mismatch: ") for f in found)
    found = res.get("mismatches", [])
    return res["exit"] == 1 and bool(found) and all(m.startswith("REGMISMATCH ") for m in found)


def outcome(comparator, res):
    if comparator == "orchestrator":
        if res["findings"]:
            return "finding"
        return "incomplete" if res["unobserved"] else "pass"
    return {0: "pass", 1: "finding", 3: "incomplete"}.get(res["exit"], f"error({res['exit']})")


# ── the rows, on the recorded Spike logs ─────────────────────────────────────

@pytest.mark.parametrize("comparator", COMPARATORS)
@pytest.mark.parametrize("row", ROWS)
def test_a_register_write_row_is_a_register_finding_on_the_spike_table(row, comparator):
    rtl, iss = table._mutant(row)
    assert rtl != iss, f"{row}: the mutant changed nothing"
    res = run(comparator, rtl, iss)
    assert register_finding(comparator, res), (row, comparator, res)


# ── the rows, on the recorded Ibex run ───────────────────────────────────────

@pytest.mark.parametrize("comparator", COMPARATORS)
@pytest.mark.parametrize("row", ROWS)
def test_a_register_write_row_is_a_register_finding_on_the_ibex_table(row, comparator):
    rtl, iss = ibex.mutant(row)
    assert (rtl, iss) != ibex.pair(ibex.BASE_PROG, "halt"), f"{row}: the mutant changed nothing"
    res = run(comparator, rtl, iss, ibex.AGENT_D_EXTRA)
    assert register_finding(comparator, res), (row, comparator, res)


def test_the_later_write_row_differs_only_in_its_second_register():
    """The row must leave the first register equal, or it is the first-value
    row again and says nothing about the second write."""
    rtl, iss = table._mutant("later_register_write")
    (i, (r, s)), = [(k, (a, b)) for k, (a, b) in enumerate(zip(rtl, iss)) if a != b]
    (r1, v1), (r2, rv2) = r["regs"].items()
    (s1, w1), (s2, sv2) = s["regs"].items()
    assert (r1, v1) == (s1, w1), "the first register differs"
    assert r2 == s2 and rv2 != sv2, "the second register does not differ in value only"


@pytest.mark.parametrize("comparator", COMPARATORS)
def test_the_later_write_finding_names_the_second_register(comparator):
    rtl, iss = table._mutant("later_register_write")
    res = run(comparator, rtl, iss)
    text = " ".join(res["findings"] if comparator == "orchestrator" else res.get("mismatches", []))
    assert "x30" in text, (comparator, text)


# ── controls: unmutated real logs give no register finding ──────────────────

@pytest.mark.parametrize("comparator", COMPARATORS)
@pytest.mark.parametrize("fixture", sorted(table.BASE))
def test_an_unmutated_spike_log_passes(fixture, comparator):
    log = table.BASE[fixture]
    res = run(comparator, copy.deepcopy(log), copy.deepcopy(log))
    assert outcome(comparator, res) == "pass", (fixture, comparator, res)


@pytest.mark.parametrize("comparator", COMPARATORS)
@pytest.mark.parametrize("prog", ibex.PROGRAMS)
def test_an_unmutated_ibex_run_has_no_register_finding(prog, comparator):
    """Every recorded Ibex program against Spike on the same ELF: the clean run
    must stay clean under the whole-record rule, at the halt and raw (the raw
    view carries the harness's declared halt)."""
    for view in ("halt", "raw"):
        res = ibex.result(prog, view, comparator)
        assert not ibex.has_mismatch(comparator, res), (prog, view, comparator, res)


def test_the_ibex_runs_write_registers_and_carry_no_x0():
    """The controls above mean something only if the logs hold register
    writes.  No producer writes x0 into `regs` today: the Ibex trace prints
    x0=0x00000000 for writes to x0, and the converter drops them."""
    writes = 0
    for prog in ibex.PROGRAMS:
        for log in ibex._pair(prog):
            for r in cmp_.split(log)[1]:
                assert "x0" not in r["regs"], (prog, r)
                writes += bool(r["regs"])
    assert writes > 50, writes
    for fixture in table.BASE:
        assert not any("x0" in (e.get("regs") or {}) for e in table.BASE[fixture])


# ── both comparators on fabricated pairs: the rule, case by case ─────────────

N = 4
AT = 2
A, B, C = "0x0000000a", "0x0000000b", "0x0000000c"


def _rec(i, regs):
    return {"schema_version": "2.1.0", "seq": i, "hart": 0,
            "pc": f"0x{0x80000000 + 4 * i:08x}", "instr": "0x00a00513",
            "priv": "M", "trap": None, "csrs": {}, "mem_writes": [], "regs": regs}


def _pair(rtl_regs, iss_regs):
    """Four records per side, equal except for the register writes of record AT."""
    rtl = [_rec(i, {"x5": f"0x{i:08x}"}) for i in range(N)]
    iss = copy.deepcopy(rtl)
    rtl[AT]["regs"], iss[AT]["regs"] = copy.deepcopy(rtl_regs), copy.deepcopy(iss_regs)
    return rtl, iss


#: name -> (rtl regs, iss regs, expected outcome), expectation from the rule.
CASES = {
    "one_write_equal":        ({"x10": A}, {"x10": A}, "pass"),
    "two_writes_equal":       ({"x10": A, "x11": B}, {"x10": A, "x11": B}, "pass"),
    "two_writes_reordered":   ({"x11": B, "x10": A}, {"x10": A, "x11": B}, "pass"),
    "no_write_either_side":   ({}, {}, "pass"),
    "value_differs":          ({"x10": B}, {"x10": A}, "finding"),
    "first_of_two_differs":   ({"x10": C, "x11": B}, {"x10": A, "x11": B}, "finding"),
    "second_of_two_differs":  ({"x10": A, "x11": C}, {"x10": A, "x11": B}, "finding"),
    "missing_write_rtl":      ({}, {"x10": A}, "finding"),
    "missing_write_iss":      ({"x10": A}, {}, "finding"),
    "extra_register_rtl":     ({"x10": A, "x11": B}, {"x10": A}, "finding"),
    "extra_register_iss":     ({"x10": A}, {"x10": A, "x11": B}, "finding"),
    "other_register":         ({"x11": A}, {"x10": A}, "finding"),
    "x0_against_none":        ({"x0": "0x00000000"}, {}, "pass"),
    "none_against_x0":        ({}, {"x0": "0x00000000"}, "pass"),
    "x0_beside_a_write":      ({"x10": A, "x0": "0x00000000"}, {"x10": A}, "pass"),
    "zero_by_abi_name":       ({"zero": "0x00000000"}, {}, "pass"),
    "x0_does_not_hide_a_miss": ({"x0": "0x00000000"}, {"x10": A}, "finding"),
    "abi_name_same_register": ({"a0": A}, {"x10": A}, "pass"),
    "abi_name_other_register": ({"a1": A}, {"x10": A}, "finding"),
    "short_hex_same_value":   ({"x10": "0xa"}, {"x10": A}, "pass"),
    "upper_hex_same_value":   ({"x10": "0x0000000A"}, {"x10": A}, "pass"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_both_comparators_apply_the_rule(name):
    rtl_regs, iss_regs, expect = CASES[name]
    rtl, iss = _pair(rtl_regs, iss_regs)
    got = {c: outcome(c, run(c, rtl, iss)) for c in COMPARATORS}
    assert got == {c: expect for c in COMPARATORS}, (name, got)
    if expect == "finding":
        for c in COMPARATORS:
            assert register_finding(c, run(c, rtl, iss)), (name, c)


def test_the_cases_are_not_vacuous():
    outcomes = [e for *_, e in CASES.values()]
    assert set(outcomes) == {"pass", "finding"}
    assert len(CASES) >= 21, "cases were deleted"
    # the base pair writes a register on every record, so a comparator that
    # reads nothing would pass the "pass" cases and fail every "finding" case
    rtl, _ = _pair({}, {})
    assert sum(bool(r["regs"]) for r in rtl) == N - 1


# ── the orchestrator's real-run shape: COMMIT text against Spike ─────────────
#
# A real orchestrator run reads the DUT's COMMIT lines with
# coverage_pipeline.parse_dut_commit_log, which gives every entry a legacy
# `rd`/`rd_val` ("" when the line has no rd=), and Spike's log with regs.  A
# legacy `rd` is a report: "" is "no register written", as {} is.

def _commit_text_pair(rtl_rd, rtl_val, iss_regs):
    import AGENT_F.coverage_pipeline as cp
    line = "COMMIT pc=0x80000008 instr=0x00a00513" + (f" rd={rtl_rd} val={rtl_val}" if rtl_rd else "")
    rtl_e, = cp.parse_dut_commit_log(line)
    assert "regs" not in rtl_e and rtl_e["rd"] == (rtl_rd or "")
    iss_e = {"pc": "0x80000008", "instr": "0x00a00513", "regs": iss_regs}
    return [rtl_e], [iss_e]


@pytest.mark.parametrize("rtl_rd,rtl_val,iss_regs,expect", [
    ("x10", A, {"x10": A}, "pass"),
    ("", "", {}, "pass"),
    ("", "", {"x10": A}, "finding"),
    ("x10", A, {}, "finding"),
    ("x11", A, {"x10": A}, "finding"),
    ("x10", "0xa", {"x10": A}, "pass"),
    ("x0", "0x00000000", {}, "pass"),
], ids=["equal", "none_none", "missing_rtl", "missing_iss", "other_register",
        "short_hex", "x0_against_none"])
def test_the_orchestrator_compares_commit_text_against_spike(rtl_rd, rtl_val, iss_regs, expect):
    from ava_patched import CommitLogComparator, MismatchKind
    rtl, iss = _commit_text_pair(rtl_rd, rtl_val, iss_regs)
    bugs, report = CommitLogComparator().compare_with_report(rtl, iss)
    assert report["not_reported"] == []
    assert ("finding" if bugs else "pass") == expect, [b.description for b in bugs]
    assert all(b.kind is MismatchKind.REGISTER for b in bugs)

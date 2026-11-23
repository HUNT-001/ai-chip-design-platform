"""AGENT_D: a `csrs` field one side does not report must not pass or fail (#176).

#164 gave AGENT_D one rule per compared field: reported-empty ([]) is not absent
(the key missing or null); exactly one side absent is exit 3 / status
"incomplete" / reason `field_not_reported:<field>:<side>`, and it is not a
mismatch; neither side reporting is counted as not compared.  `csrs` was left
out of that rule, so an absent one was read as "no CSR writes":

  * the other side wrote a CSR  -> a CSR mismatch (exit 1), a false finding;
  * the other side wrote none   -> PASS (exit 0), CSRs counted as verified.

The cases below are written from that rule, not from the code.  Inputs are the
recorded Spike logs (tests/fixtures/spike/, PROVENANCE.md) through the same
canonical shape tests/test_comparator_mutation_table.py uses, and the real Ibex
/ Spike pairs of validation/ibex/ (#175), where Ibex's text trace never carries
CSR values.  Each synthetic case removes `csrs` from one side and nothing else.
"""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CLI = REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"
VAL = REPO_ROOT / "validation" / "ibex"
FIX = VAL / "fixtures"
PROGRAMS = ("arith_branch", "muldiv", "loadstore", "csr")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


table = _load("csrs_mutation_table", REPO_ROOT / "tests" / "test_comparator_mutation_table.py")
ibex = _load("csrs_ibex_trace", VAL / "trace_to_commitlog.py")
spike = _load("csrs_spike_conv", VAL / "spike_to_commitlog.py")
cmp_ = _load("csrs_ibex_compare", VAL / "compare.py")

CSR_TRAP = table.BASE["csr_trap"]      # real CSR writes and a machine ecall
SMOKE = table.BASE["smoke"]            # no CSR writes at all, and two stores


def _write(path, log):
    path.write_text("\n".join(json.dumps(e) for e in log) + "\n", encoding="utf-8")


def _run_cli(tmp_path, rtl, iss, *extra):
    r, i, rep = tmp_path / "rtl.jsonl", tmp_path / "iss.jsonl", tmp_path / "report.json"
    _write(r, rtl)
    _write(i, iss)
    if rep.exists():
        rep.unlink()
    proc = subprocess.run(
        [sys.executable, str(CLI), str(r), str(i), "--bug-report", str(rep), "--quiet", *extra],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    report = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else None
    return proc.returncode, report, (proc.stdout or "") + (proc.stderr or "")


#: The CLI stops at the first mismatch by default, which would hide a false
#: CSR finding behind the real one that comes earlier.
EVERY_MISMATCH = ("--all", "--max-mismatches", "0")


def _first(log, pred):
    return next(i for i, e in enumerate(log) if pred(e))


def _absent(entry, form):
    """Remove the field the way a producer does: no key at all, or an explicit null."""
    if form == "missing":
        entry.pop("csrs")
    else:
        entry["csrs"] = None


# which entries lose the field: one that wrote a CSR, one that wrote none, all of them
SCOPES = {
    "csr_writing_entry": lambda log: [_first(log, lambda e: e["csrs"])],
    "empty_entry": lambda log: [_first(log, lambda e: not e["csrs"])],
    "every_entry": lambda log: list(range(len(log))),
}


def _drop(side, scope, form):
    rtl, iss = copy.deepcopy(CSR_TRAP), copy.deepcopy(CSR_TRAP)
    for i in SCOPES[scope](CSR_TRAP):
        _absent((rtl if side == "rtl" else iss)[i], form)
    return rtl, iss


def test_the_fixture_is_what_the_cases_assume():
    assert sum(1 for e in CSR_TRAP if e["csrs"]) >= 4, "the log must hold real CSR writes"
    assert any(not e["csrs"] for e in CSR_TRAP)
    assert not any(e["csrs"] for e in SMOKE) and sum(len(e["mem_writes"]) for e in SMOKE) == 2


# the anti-vacuity control first: with csrs on both sides this passes, so a 3 below means something
def test_identical_logs_with_csrs_exit_0(tmp_path):
    rc, report, out = _run_cli(tmp_path, copy.deepcopy(CSR_TRAP), copy.deepcopy(CSR_TRAP))
    assert rc == 0, out


@pytest.mark.parametrize("form", ["missing", "null"])
@pytest.mark.parametrize("scope", sorted(SCOPES))
@pytest.mark.parametrize("side", ["rtl", "iss"])
def test_csrs_only_one_side_reports_is_exit_3_and_named(tmp_path, side, scope, form):
    rtl, iss = _drop(side, scope, form)
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 3, f"csrs absent ({form}) on {side}, {scope}: exit {rc}\n{out[-800:]}"
    assert report["status"] == "incomplete", report.get("status")
    assert report["incomplete_reasons"] == [f"field_not_reported:csrs:{side}"], report
    assert report["mismatches"] == [], "not reporting csrs is not a CSR mismatch"
    assert report["passed"] is False


def test_csrs_neither_side_reports_is_counted_not_compared(tmp_path):
    rtl, iss = copy.deepcopy(CSR_TRAP), copy.deepcopy(CSR_TRAP)
    for e in rtl + iss:
        e.pop("csrs")
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 0, out
    # a passing run writes no bug report; the API carries the count
    sys.path.insert(0, str(REPO_ROOT / "AGENT_D"))
    import compare_commitlogs as cc
    result = cc.compare_logs(str(tmp_path / "rtl.jsonl"), str(tmp_path / "iss.jsonl"))
    assert result.passed and result.not_compared.get("csrs") == len(CSR_TRAP), result.not_compared


@pytest.mark.parametrize("side", ["rtl", "iss"])
def test_a_reported_empty_csrs_against_a_write_is_a_mismatch_not_a_gap(tmp_path, side):
    rtl, iss = copy.deepcopy(CSR_TRAP), copy.deepcopy(CSR_TRAP)
    (rtl if side == "rtl" else iss)[_first(CSR_TRAP, lambda e: e["csrs"])]["csrs"] = {}
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 1, out
    assert report["status"] == "failed" and report["incomplete_reasons"] == []
    assert {m["mismatch_type"] for m in report["mismatches"]} == {"CSRMISMATCH"}, report["mismatches"]


def test_a_reported_empty_csrs_on_both_sides_is_clean(tmp_path):
    """Reported-empty is a report: no CSR writes, said by both, is a pass."""
    rtl, iss = copy.deepcopy(SMOKE), copy.deepcopy(SMOKE)
    assert all(e["csrs"] == {} for e in rtl + iss)
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 0, out


@pytest.mark.parametrize("side", ["rtl", "iss"])
def test_a_csr_value_that_differs_when_both_report_is_still_exit_1(tmp_path, side):
    """A control: green before and after the fix, so exit 3 is not "everything changed"."""
    rtl, iss = copy.deepcopy(CSR_TRAP), copy.deepcopy(CSR_TRAP)
    i = _first(CSR_TRAP, lambda e: e["csrs"])
    victim = rtl if side == "rtl" else iss
    name = next(iter(victim[i]["csrs"]))
    victim[i]["csrs"][name] = table._flip(victim[i]["csrs"][name])
    rc, report, out = _run_cli(tmp_path, rtl, iss, *EVERY_MISMATCH)
    assert rc == 1 and report["status"] == "failed", out
    assert [m["mismatch_type"] for m in report["mismatches"]] == ["CSRMISMATCH"], report["mismatches"]


def _break_a_register(rtl, log):
    i = _first(log, lambda e: e["regs"])
    name = next(iter(rtl[i]["regs"]))
    rtl[i]["regs"][name] = table._flip(rtl[i]["regs"][name])
    return "REGMISMATCH"


def _break_a_store(rtl, log):
    i = _first(log, lambda e: e["mem_writes"])
    rtl[i]["mem_writes"][0]["data"] = "0x0badf00d"
    return "MEMMISMATCH"


@pytest.mark.parametrize("breaker,base", [(_break_a_register, CSR_TRAP), (_break_a_store, SMOKE)],
                         ids=["register", "store"])
def test_a_mismatch_outranks_an_unreported_csrs(tmp_path, breaker, base):
    """A finding outranks "could not check", and the unreported csrs is not
    turned into a second, false finding on the way."""
    rtl, iss = copy.deepcopy(base), copy.deepcopy(base)
    kind = breaker(rtl, base)
    for e in rtl:
        e.pop("csrs")
    rc, report, out = _run_cli(tmp_path, rtl, iss, *EVERY_MISMATCH)
    assert rc == 1 and report["status"] == "failed", out
    assert [m["mismatch_type"] for m in report["mismatches"]] == [kind], report["mismatches"]
    assert "field_not_reported:csrs:rtl" in report["incomplete_reasons"], report["incomplete_reasons"]


def test_skipping_csr_writes_by_name_is_an_explicit_choice_and_exits_0(tmp_path):
    rtl, iss = _drop("rtl", "every_entry", "missing")
    rc, _, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 3, out
    rc, _, out = _run_cli(tmp_path, rtl, iss, "--skip-fields", "csr_writes")
    assert rc == 0, out


# ── the real Ibex / Spike pairs (#175): Ibex's text trace never reports CSRs ──

def _pair(prog):
    rtl = ibex.convert_file(str(FIX / f"{prog}.ibex.log"))
    iss = spike.convert((FIX / f"{prog}.spike.log").read_text(encoding="utf-8"), FIX / f"{prog}.elf")
    cut = cmp_.cut_at_halt(rtl, iss)
    assert cut is not None, prog
    return cut[0], cut[1]


def _agent_d(rtl, iss, *extra):
    with tempfile.TemporaryDirectory() as td:
        return cmp_.agent_d(rtl, iss, Path(td), extra)


def test_the_real_pairs_are_what_the_cases_assume():
    for prog in PROGRAMS:
        rtl, iss = _pair(prog)
        assert all("csrs" not in e for e in rtl), f"{prog}: the Ibex log carries csrs"
        assert all("csrs" in e for e in iss), f"{prog}: Spike omits csrs"
    assert sum(1 for e in _pair("csr")[1] if e["csrs"]) == 6      # the six CSR writes
    assert not any(e["csrs"] for e in _pair("arith_branch")[1])  # none: the silent-pass case


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_real_ibex_run_is_incomplete_on_csrs_not_a_csr_mismatch_nor_a_pass(prog):
    rtl, iss = _pair(prog)
    res = _agent_d(rtl, iss, "--skip-fields", "privilege")
    assert res["exit"] == 3, (prog, res)
    assert res["incomplete_reasons"] == ["field_not_reported:csrs:rtl"], res
    assert res["mismatches"] == [], res["mismatches"]


def test_the_real_csr_run_names_csrs_and_privilege_when_nothing_is_skipped():
    rtl, iss = _pair("csr")
    res = _agent_d(rtl, iss)
    assert res["exit"] == 3, res
    assert sorted(res["incomplete_reasons"]) == [
        "field_not_reported:csrs:rtl", "field_not_reported:privilege:rtl"], res
    assert res["mismatches"] == []


def test_the_real_csr_run_with_the_sides_swapped_names_the_iss_side():
    rtl, iss = _pair("csr")
    res = _agent_d(iss, rtl, "--skip-fields", "privilege")      # Spike as RTL, Ibex as ISS
    assert res["exit"] == 3, res
    assert res["incomplete_reasons"] == ["field_not_reported:csrs:iss"], res
    assert res["mismatches"] == []

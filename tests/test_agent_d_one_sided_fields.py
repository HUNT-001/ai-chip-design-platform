"""AGENT_D: a field one side reports and the other does not must not pass (#164).

Rule, from the issue and the orchestrator half (#170), per compared field:

  * present on both sides            -> compare (a difference is exit 1, a mismatch)
  * reported empty ([]) is not absent (the key missing)
  * exactly one side does not report -> exit 3, "could not compare": the result
                                        JSON has status "incomplete" and
                                        incomplete_reasons ["field_not_reported:<field>:<side>"].
                                        Not a mismatch.
  * neither side reports             -> counted as not compared, not as equal

Inputs are the recorded Spike logs in tests/fixtures/spike/ (PROVENANCE.md),
parsed by the real parser and written in the canonical JSONL AGENT_D reads.
Spike prints no access size and no trap value, so those two are filled in
identically on both sides: the store size from the instruction word's funct3
(sw = 4 bytes) and `tval` as 0.  Each case then removes one field from one
side of one entry and nothing else.
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import AGENT_F.coverage_pipeline as cp

FIX = REPO_ROOT / "tests" / "fixtures" / "spike"
CLI = REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"


def _canonical(name, upto):
    """The first `upto` aligned entries of a recorded Spike log, canonical JSONL shape."""
    log = cp.parse_spike_commit_log((FIX / f"{name}.log").read_text(encoding="utf-8"))
    aligned, reason = cp.align_to_entry(log, cp.elf_entry_pc(FIX / f"{name}.elf"))
    assert reason is None
    out = []
    for i, e in enumerate(aligned[:upto]):
        stores = [{"addr": m["addr"], "size": 1 << (int(e["instr"], 16) >> 12 & 3), "data": m["data"]}
                  for m in e["mem"] if m["op"] == "store"]
        trap = None
        if e["trap"]:
            trap = {"cause": e["trap"]["cause"], "tval": "0x00000000", "epc": e["trap"]["epc"]}
        out.append({"schema_version": "2.1.0", "seq": i, "hart": 0, "pc": e["pc"],
                    "instr": e["instr"], "priv": e["priv"] or "M", "trap": trap,
                    "regs": dict(e["regs"]), "csrs": {}, "mem_writes": stores})
    return out


def _write(path, log):
    path.write_text("\n".join(json.dumps(e) for e in log) + "\n", encoding="utf-8")


def _run_cli(tmp_path, rtl, iss, *extra):
    r, i, rep = tmp_path / "rtl.jsonl", tmp_path / "iss.jsonl", tmp_path / "report.json"
    _write(r, rtl)
    _write(i, iss)
    proc = subprocess.run(
        [sys.executable, str(CLI), str(r), str(i), "--bug-report", str(rep), "--quiet", *extra],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    report = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else None
    return proc.returncode, report, (proc.stdout or "") + (proc.stderr or "")


def _first(log, pred):
    return next(i for i, e in enumerate(log) if pred(e))


SMOKE = _canonical("rv32im_smoke", 68)          # through the terminal self-loop at 67
TRAP = _canonical("csr_trap", 18)               # through the machine ecall at 15


def _store_entry(log):
    return _first(log, lambda e: e["mem_writes"])


# field -> (which log, entry chooser, how to remove it from that entry)
CASES = {
    "regs":       (SMOKE, lambda l: _first(l, lambda e: e["regs"]),  lambda e: e.pop("regs")),
    "privilege":  (SMOKE, lambda l: 3,                               lambda e: e.pop("priv")),
    "stores":     (SMOKE, _store_entry,                              lambda e: e.pop("mem_writes")),
    "store_addr": (SMOKE, _store_entry,                              lambda e: e["mem_writes"][0].pop("addr")),
    "store_data": (SMOKE, _store_entry,                              lambda e: e["mem_writes"][0].pop("data")),
    "store_size": (SMOKE, _store_entry,                              lambda e: e["mem_writes"][0].pop("size")),
    "trap_cause": (TRAP,  lambda l: 15,                              lambda e: e["trap"].pop("cause")),
    "trap_tval":  (TRAP,  lambda l: 15,                              lambda e: e["trap"].pop("tval")),
    "trap_pc":    (TRAP,  lambda l: 15,                              lambda e: e["trap"].pop("epc")),
}


def _drop(field, side):
    base, at, remove = CASES[field]
    rtl, iss = copy.deepcopy(base), copy.deepcopy(base)
    remove((rtl if side == "rtl" else iss)[at(base)])
    return rtl, iss


def test_the_fixtures_are_what_the_cases_assume():
    assert sum(len(e["mem_writes"]) for e in SMOKE) == 2, "the smoke log has 2 stores"
    assert TRAP[15]["trap"]["cause"] == "0x0000000b"
    assert all(e["mem_writes"] == [] or e["mem_writes"][0]["size"] == 4 for e in SMOKE)


# the anti-vacuity controls first: both sides equal passes, so a 3 below means something
def test_identical_real_logs_exit_0(tmp_path):
    rc, report, out = _run_cli(tmp_path, SMOKE, copy.deepcopy(SMOKE))
    assert rc == 0, out
    rc, report, out = _run_cli(tmp_path, TRAP, copy.deepcopy(TRAP))
    assert rc == 0, out


@pytest.mark.parametrize("side", ["rtl", "iss"])
@pytest.mark.parametrize("field", sorted(CASES))
def test_a_field_only_one_side_reports_is_exit_3_and_named(tmp_path, field, side):
    rtl, iss = _drop(field, side)
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 3, f"{field} absent on {side}: exit {rc}\n{out[-800:]}"
    assert report["status"] == "incomplete", report.get("status")
    assert report["incomplete_reasons"] == [f"field_not_reported:{field}:{side}"], report
    assert report["mismatches"] == [], "not reporting a field is not a mismatch"
    assert report["passed"] is False


def test_a_reported_empty_list_is_not_an_absent_key(tmp_path):
    """The ISS reports `mem_writes: []` on every entry without a store. A DUT that
    reports [] too is clean; a DUT that leaves the key out has not reported."""
    rc, _, out = _run_cli(tmp_path, copy.deepcopy(SMOKE), copy.deepcopy(SMOKE))
    assert rc == 0, out
    rtl = copy.deepcopy(SMOKE)
    for e in rtl:
        e.pop("mem_writes")
    rc, report, out = _run_cli(tmp_path, rtl, copy.deepcopy(SMOKE))
    assert rc == 3 and report["status"] == "incomplete", out
    assert "field_not_reported:stores:rtl" in report["incomplete_reasons"]


def test_a_reported_empty_store_list_against_a_store_is_a_mismatch_not_a_gap(tmp_path):
    rtl, iss = copy.deepcopy(SMOKE), copy.deepcopy(SMOKE)
    rtl[_store_entry(SMOKE)]["mem_writes"] = []
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 1, out
    assert report["status"] == "failed" and report["incomplete_reasons"] == []


@pytest.mark.parametrize("what", ["store_data", "register", "privilege"])
def test_present_on_both_but_different_is_still_a_mismatch(tmp_path, what):
    """A control: green before and after the fix, so exit 3 is not "everything changed"."""
    rtl, iss = copy.deepcopy(SMOKE), copy.deepcopy(SMOKE)
    if what == "store_data":
        rtl[_store_entry(SMOKE)]["mem_writes"][0]["data"] = "0x0badf00d"
    elif what == "register":
        i = _first(SMOKE, lambda e: e["regs"])
        name = next(iter(rtl[i]["regs"]))
        rtl[i]["regs"][name] = "0x0badf00d"
    else:
        rtl[3]["priv"] = "U" if rtl[3]["priv"] != "U" else "M"
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 1, f"{what}: exit {rc}\n{out[-600:]}"
    assert report["mismatches"], report     # the control: a difference is still exit 1


def test_a_mismatch_outranks_a_field_that_could_not_be_compared(tmp_path):
    rtl, iss = _drop("privilege", "rtl")
    rtl[_store_entry(SMOKE)]["mem_writes"][0]["data"] = "0x0badf00d"
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 1 and report["status"] == "failed", out


def test_a_field_neither_side_reports_is_counted_not_compared(tmp_path):
    rtl, iss = copy.deepcopy(SMOKE), copy.deepcopy(SMOKE)
    for e in rtl + iss:
        e.pop("priv")
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 0, out
    # a passing run writes no bug report; the API carries the count
    sys.path.insert(0, str(REPO_ROOT / "AGENT_D"))
    import compare_commitlogs as cc
    r, i = tmp_path / "rtl.jsonl", tmp_path / "iss.jsonl"
    result = cc.compare_logs(str(r), str(i))
    assert result.passed and result.not_compared.get("privilege") == len(SMOKE), result.not_compared


def test_skipping_a_field_by_name_is_an_explicit_choice_and_exits_0(tmp_path):
    """Spike prints no store size. A caller comparing against it says so with
    --skip-fields; nothing is skipped silently."""
    rtl, iss = _drop("store_size", "iss")
    rc, report, out = _run_cli(tmp_path, rtl, iss)
    assert rc == 3, out
    rc, report, out = _run_cli(tmp_path, rtl, iss, "--skip-fields", "mem_size")
    assert rc == 0, out


def test_the_manifest_records_infra_error_with_the_reason(tmp_path):
    rtl, iss = _drop("stores", "rtl")
    out = tmp_path / "outputs"
    out.mkdir()
    _write(out / "rtlcommit.jsonl", rtl)
    _write(out / "isscommitlog.jsonl", iss)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rundir": str(tmp_path), "seed": 1}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(CLI), "--manifest", str(manifest), "--quiet"],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert m["phases"]["compare"]["status"] == "infra_error"
    assert m["phases"]["compare"]["error"] == "field_not_reported:stores:rtl"
    assert m["status"] == "infra_error"

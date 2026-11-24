"""The orchestrator and the other log readers honour fields_reported (#177, PR 3 of 4).

A commit log may open with one header record,

    {"_header": {"schema_version": "2.2.0", "fields_reported": [...]}}

(docs/dut_commit_log.md section 4).  The orchestrator is held here to the rules
AGENT_D follows since PR 2.  They are written from that document and #177:

  * the header is not an instruction.  It is never compared or counted, so it is
    not in the "RTL=n ISS=m" counts or in the program-end alignment;
  * header present = authoritative.  A listed field missing from a record, a
    record carrying an unlisted field, a legacy key name, a header that is
    duplicated or not first, or a header with no records is a malformed log.
    The orchestrator's verdict for that is incomplete (exit 3), with a reason
    naming the side, the line and the field.  It is never a pass and never a
    finding.  (AGENT_D exits 2 for the same log: unreadable input.)
  * a field one side lists and the other does not: incomplete,
    `field_not_reported:<field>:<side>`.  This covers trap;
  * a field neither side lists: recorded under fields_not_compared;
  * a mismatch in a compared field still outranks could-not-check;
  * a headerless (legacy) log keeps today's behaviour, and the verdict records
    `fields_reported: "undeclared (legacy)"` for that side;
  * the declared halt (#181) still works on headered logs.

The cases are in tests/fields_reported_cases.py, built from the real fixtures.
The orchestrator is run the way a real run runs it: `SpikeISS._compare_results`,
then the run's own verdict fold (`with_unobserved`, `verdict_from_reports`,
`exit_code_for_status`).
"""
import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ava_patched import (SpikeISS, exit_code_for_status, verdict_from_reports,
                         with_unobserved)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fc = _load("fields_reported_cases", REPO_ROOT / "tests" / "fields_reported_cases.py")
CASES = fc.build_cases()
LEGACY = fc.LEGACY


def orchestrate(tmp_path, rtl, iss, rtl_ended=None):
    """(exit code, comparison) as a real run computes them.  The instruction
    counts are what a caller holding the log as a list would pass: its length."""
    rtl_results = {"commit_log": copy.deepcopy(rtl), "instructions": len(rtl)}
    if rtl_ended is not None:
        rtl_results["rtl_ended"] = copy.deepcopy(rtl_ended)
    sim = SpikeISS(timeout=30, run_dir=tmp_path / "orch")
    c = asyncio.run(sim._compare_results(
        rtl_results, {"commit_log": copy.deepcopy(iss), "instructions": len(iss)}))
    status = verdict_from_reports(with_unobserved({}, list(c.get("unobserved", []))), c["bugs"])
    return exit_code_for_status(status), c


def _records(log):
    return [e for e in log if "_header" not in e]


# ── every case: the verdict the rules give ───────────────────────────────────

@pytest.mark.parametrize("name", sorted(CASES))
def test_the_orchestrator_follows_the_header_rules(tmp_path, name):
    case = CASES[name]
    rc, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    want = fc.EXPECTED_EXIT[case.expect][0]
    assert rc == want, (f"{name}: expected {case.expect} (exit {want}), got exit {rc}; "
                        f"bugs={[b['description'][:120] for b in c['bugs']]} "
                        f"unobserved={c.get('unobserved')}")
    if case.reason is not None:
        assert case.reason in c["unobserved"], (name, c["unobserved"])
    if case.expect == "malformed":
        # not a pass, and not a finding either: nothing was compared
        assert c["bugs"] == [], (name, c["bugs"])
        assert all(u.startswith("malformed_log:") for u in c["unobserved"]), c["unobserved"]


def test_the_cases_are_not_vacuous():
    outcomes = {c.expect for c in CASES.values()}
    assert outcomes == {"pass", "finding", "incomplete", "malformed"}
    assert len(CASES) >= 21, "cases were deleted"
    assert sum(1 for c in CASES.values() if c.expect == "malformed") >= 9


# ── the header is not an instruction ─────────────────────────────────────────

@pytest.mark.parametrize("prog", ["loadstore", "arith_branch", "muldiv"])
@pytest.mark.parametrize("sides", ["both", "rtl", "iss"])
def test_the_header_is_never_counted_or_compared(tmp_path, prog, sides):
    """A headered copy gives the counts, the program end, the warnings and the
    findings of the same records with no header."""
    rtl, iss = fc.ibex_pair(prog)
    fields = fc.COMMON + ["trap"]
    rtl_d, iss_d = fc.declare(rtl, fields), fc.declare(iss, fields)
    ended = {"reason": "halt_write", "addr": rtl[-1]["mem_writes"][-1]["addr"]}
    plain_rc, plain = orchestrate(tmp_path, _records(rtl_d), _records(iss_d), ended)
    hdr_rc, hdr = orchestrate(
        tmp_path,
        rtl_d if sides in ("both", "rtl") else _records(rtl_d),
        iss_d if sides in ("both", "iss") else _records(iss_d), ended)
    assert plain_rc == 0, (prog, plain["bugs"], plain["unobserved"])
    assert hdr_rc == plain_rc, (prog, sides, hdr["bugs"], hdr["unobserved"])
    for key in ("rtl_entries", "iss_entries", "program_end", "warnings", "bugs"):
        assert hdr[key] == plain[key], (prog, sides, key, hdr[key], plain[key])
    assert hdr["rtl_entries"] == len(rtl) and hdr["iss_entries"] == len(iss)
    assert hdr["program_end"]["index"] == len(rtl), hdr["program_end"]


def test_the_counts_name_records_not_lines(tmp_path):
    """The "RTL=n ISS=m" text counts records, header excluded."""
    rtl, iss = fc.ibex_pair("loadstore")
    fields = fc.COMMON + ["trap"]
    ended = {"reason": "halt_write", "addr": rtl[-1]["mem_writes"][-1]["addr"]}
    _, c = orchestrate(tmp_path, fc.declare(rtl, fields), fc.declare(iss, fields), ended)
    counts = [w for w in c["warnings"] if "RTL=" in w]
    assert counts == [f"Instruction count diverges: RTL={len(rtl)} ISS={len(iss)}"], counts


# ── what the verdict records ─────────────────────────────────────────────────

def test_the_verdict_records_each_sides_declaration(tmp_path):
    case = CASES["natural_declarations"]
    _, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    assert c["fields_reported"] == {"rtl": case.rtl[0]["_header"]["fields_reported"],
                                    "iss": case.iss[0]["_header"]["fields_reported"]}
    case = CASES["rtl_legacy_iss_declared"]
    _, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    assert c["fields_reported"] == {"rtl": LEGACY,
                                    "iss": case.iss[0]["_header"]["fields_reported"]}


def test_a_field_neither_side_lists_is_recorded_as_not_compared(tmp_path):
    case = CASES["trap_listed_by_neither"]
    _, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    assert "trap" in c["fields_not_compared"], c["fields_not_compared"]
    case = CASES["same_declarations"]
    _, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    assert "trap" not in c["fields_not_compared"], c["fields_not_compared"]


def test_legacy_logs_are_judged_as_before(tmp_path):
    """No header on either side: the legacy marker on both, nothing added to
    fields_not_compared from a declaration, and no trap comparison (#159 keeps
    that gap open for legacy logs; tests/test_comparator_mutation_table.py
    holds it)."""
    rtl, iss = fc.ibex_pair("loadstore")
    ended = {"reason": "halt_write", "addr": rtl[-1]["mem_writes"][-1]["addr"]}
    rc, c = orchestrate(tmp_path, rtl, iss, ended)
    assert rc == 0, (c["bugs"], c["unobserved"])
    assert c["fields_reported"] == {"rtl": LEGACY, "iss": LEGACY}
    assert set(c["fields_not_compared"]) <= {"stores", "store_size"}, c["fields_not_compared"]
    trap_log = fc.spike_canonical("csr_trap", 18)
    t = next(n for n, e in enumerate(trap_log) if e["trap"])
    mutant = copy.deepcopy(trap_log)
    mutant[t]["trap"]["cause"] = "0x00000002"
    rc, c = orchestrate(tmp_path, mutant, trap_log)
    assert rc == 0 and c["fields_reported"] == {"rtl": LEGACY, "iss": LEGACY}


def test_a_malformed_reason_names_side_line_and_field(tmp_path):
    case = CASES["listed_field_missing"]
    _, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    text = " ".join(c["warnings"])
    assert "malformed log" in text and "'regs'" in text and "[rtl]" in text, c["warnings"]


def test_a_register_mismatch_outranks_the_unreported_trap(tmp_path):
    case = CASES["register_mismatch_and_unreported_trap"]
    rc, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    assert rc == 1
    assert any(b["kind"] == "register_mismatch" for b in c["bugs"]), c["bugs"]
    assert "field_not_reported:trap:rtl" in c["unobserved"], c["unobserved"]


def test_a_trap_difference_is_a_trap_finding(tmp_path):
    for name in ("trap_cause_differs", "trap_missing_on_rtl"):
        case = CASES[name]
        _, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
        assert [b["kind"] for b in c["bugs"]] == ["trap_mismatch"], (name, c["bugs"])


def test_the_declared_halt_holds_on_headered_logs(tmp_path):
    case = CASES["same_declarations"]
    _, c = orchestrate(tmp_path, case.rtl, case.iss, case.rtl_ended)
    pe = c["program_end"]
    assert pe["end"] == "rtl_stopped_before_terminal_loop", pe
    assert pe["index"] == len(case.rtl) - 1 and pe["rtl_ended"] == case.rtl_ended, pe


# ── the other readers ────────────────────────────────────────────────────────

def _run_iss():
    return _load("fr_run_iss", REPO_ROOT / "AGENT_C" / "run_iss.py")


def _agent_c_log(run_iss, tmp_path):
    """A real Spike log (tests/fixtures/spike) through AGENT_C's own writer."""
    out = tmp_path / "iss_commitlog.jsonl"
    text = (fc.SPIKE_FIX / "rv32im_smoke.log").read_text(encoding="utf-8")
    assert run_iss.write_commitlog(text, out, None, 60) > 0
    recs = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert run_iss.validate_commitlog(out) == [], "the headerless log must be valid first"
    return recs


def _write(path, log):
    path.write_text("".join(json.dumps(e) + "\n" for e in log), encoding="utf-8")
    return path


def test_run_iss_validate_commitlog_accepts_a_headered_log(tmp_path):
    run_iss = _run_iss()
    recs = _agent_c_log(run_iss, tmp_path)
    p = _write(tmp_path / "declared.jsonl", fc.declare(recs, fc.fields_of(recs)))
    assert run_iss.validate_commitlog(p) == []


@pytest.mark.parametrize("mutation,needle", [
    ("drop_listed", "'regs'"), ("add_unlisted", "'cycle'"), ("header_twice", "header"),
    ("header_only", "no records"), ("alias", "'rd'"),
])
def test_run_iss_validate_commitlog_rejects_a_malformed_one(tmp_path, mutation, needle):
    run_iss = _run_iss()
    recs = _agent_c_log(run_iss, tmp_path)
    log = fc.declare(recs, fc.fields_of(recs))
    if mutation == "drop_listed":
        del log[4]["regs"]
    elif mutation == "add_unlisted":
        log[4]["cycle"] = None
    elif mutation == "header_twice":
        log.insert(1, copy.deepcopy(log[0]))
    elif mutation == "header_only":
        log = log[:1]
    elif mutation == "alias":
        log[4]["rd"] = 10
    errs = run_iss.validate_commitlog(_write(tmp_path / "bad.jsonl", log))
    assert errs and any("malformed" in e and needle in e for e in errs), errs


def test_semantic_analyzer_accepts_a_headered_log():
    from AGENT_A.semantic_analyzer import SemanticAnalyzer, validate_record
    rtl, _ = fc.ibex_pair("loadstore")          # Ibex records carry disasm
    log = fc.declare(rtl, fc.fields_of(rtl))
    assert validate_record(log[0], 0) == []
    rep = SemanticAnalyzer(commit_log=log).analyze()
    assert rep["schema_valid"], rep["record_errors"][:5]
    assert rep["records_checked"] == len(rtl)


@pytest.mark.parametrize("mutation,needle", [
    ("drop_listed", "'regs'"), ("add_unlisted", "'priv'"), ("header_not_first", "header"),
    ("header_only", "no records"),
])
def test_semantic_analyzer_rejects_a_malformed_one(mutation, needle):
    from AGENT_A.semantic_analyzer import SemanticAnalyzer, validate_record
    rtl, _ = fc.ibex_pair("loadstore")
    log = fc.declare(rtl, fc.fields_of(rtl))
    if mutation == "drop_listed":
        del log[4]["regs"]
    elif mutation == "add_unlisted":
        log[4]["priv"] = None
    elif mutation == "header_not_first":
        log.insert(3, log.pop(0))
        assert validate_record(log[3], 3) != []
    elif mutation == "header_only":
        log = log[:1]
    rep = SemanticAnalyzer(commit_log=log).analyze()
    assert not rep["schema_valid"]
    assert any("malformed" in e and needle in e for e in rep["record_errors"]), rep["record_errors"]


def test_the_readers_share_agent_ds_rules():
    """The shared reader's field lists are the schema's, and AGENT_D's copies
    agree with them, so no reader can drift from another."""
    from AGENT_A import commitlog_header as ch
    import AGENT_D.compare_commitlogs as d
    assert sorted(ch.DECLARABLE_FIELDS) == fc.DECLARABLE
    assert ch.DECLARABLE_FIELDS == d.DECLARABLE_FIELDS
    assert ch.IDENTITY_FIELDS == d.IDENTITY_FIELDS
    assert ch.LEGACY_KEYS == d._LEGACY_KEYS
    assert ch.LEGACY_FIELDS_REPORTED == d.LEGACY_FIELDS_REPORTED == LEGACY

"""AGENT_D reads a commit log's fields_reported header (#177, PR 2 of 4).

The format is docs/dut_commit_log.md section 4 and AGENT_A/commitlog.schema.json
2.2.0.  A log may open with one header record,

    {"_header": {"schema_version": "2.2.0", "fields_reported": [...]}}

and the rules AGENT_D is held to here, written from that document and #177:

  * the header is not an instruction: it is never compared or counted;
  * header present = authoritative.  A listed field missing from a record, or a
    record carrying a field the list does not name, is a malformed log.  For a
    log that lists trap, `trap: null` is "no trap here" and an absent key is
    malformed.  A malformed log is unreadable input -- exit 2, the reason naming
    the field and the side -- never 0 or 1 as if it had been compared;
  * a field one side lists and the other does not: exit 3, incomplete,
    `field_not_reported:<field>:<side>`.  This now covers trap;
  * a field neither side lists: recorded under fields_not_compared;
  * a real mismatch in a compared field still outranks could-not-check (exit 1);
  * a headerless (legacy) log keeps today's per-field inference, and the result
    records `fields_reported: "undeclared (legacy)"` for that side;
  * --skip-fields keeps working for every field, trap included.

Inputs are the recorded logs in the tree, never hand-written records: the Spike
fixtures in tests/fixtures/spike/ (parsed by the real parser) and the Ibex/Spike
pairs in validation/ibex/fixtures/ (converted by the real converters).  The
headered variants are built here by `declare`, which prepends a header and puts
every listed field on every record -- null where the record has nothing to say,
which for trap is "no trap".  Every case runs AGENT_D's CLI in a subprocess, the
way a real run does.
"""
import copy
import importlib.util
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
SCHEMA = REPO_ROOT / "AGENT_A" / "commitlog.schema.json"
VAL = REPO_ROOT / "validation" / "ibex"

EXIT_PASS, EXIT_MISMATCH, EXIT_MALFORMED, EXIT_INCOMPLETE = 0, 1, 2, 3
LEGACY = "undeclared (legacy)"

#: what a Spike record carries, as `_canonical` writes it
SPIKE_FIELDS = ["csrs", "mem_writes", "priv", "regs", "trap"]
NO_TRAP = [f for f in SPIKE_FIELDS if f != "trap"]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclasses look their module up by name
    spec.loader.exec_module(mod)
    return mod


def _canonical(name, upto):
    """The first `upto` aligned entries of a recorded Spike log, canonical JSONL
    (the shape tests/test_agent_d_one_sided_fields.py uses).  Spike prints no
    store size and no tval: the size comes from funct3, tval is 0 on both sides."""
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
                    "instr": e["instr"], "instr_width": 4, "_src": "iss",
                    "priv": e["priv"] or "M", "trap": trap,
                    "regs": dict(e["regs"]), "csrs": {}, "mem_writes": stores})
    return out


SMOKE = _canonical("rv32im_smoke", 68)      # through the terminal self-loop
TRAP = _canonical("csr_trap", 18)           # through the machine ecall


def _trap_index(log):
    return next(i for i, e in enumerate(log) if e["trap"])


def _reg_index(log):
    return next(i for i, e in enumerate(log) if e["regs"] and "x0" not in e["regs"])


def header(fields):
    return {"_header": {"schema_version": "2.2.0", "fields_reported": list(fields)}}


def declare(log, fields):
    """A real log as a declared 2.2.0 log listing `fields`: the header first,
    every listed field on every record (null where the record has nothing to
    say; trap: null is "no trap"), every unlisted optional field removed."""
    recs = copy.deepcopy(log)
    for r in recs:
        r["schema_version"] = "2.2.0"
        for f in DECLARABLE:
            if f not in fields:
                r.pop(f, None)
        for f in fields:
            r.setdefault(f, None)
    return [header(fields)] + recs


def _write(path, log):
    path.write_text("\n".join(json.dumps(e) for e in log) + "\n", encoding="utf-8")


def run(tmp_path, rtl, iss, *extra):
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


def verdict(report):
    """What a comparison decided, without the paths and timings."""
    return {"status": report["status"],
            "incomplete_reasons": report["incomplete_reasons"],
            "not_compared": report["not_compared"],
            "mismatches": [(m["mismatch_type"], m["step"], m["differing_field"]) for m in report["mismatches"]]}


def _schema():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


DECLARABLE = (_schema()["$defs"]["log_header"]["properties"]["_header"]
              ["properties"]["fields_reported"]["items"]["enum"])


# ── the inputs ───────────────────────────────────────────────────────────────

def test_the_fixtures_are_what_the_cases_assume():
    assert len(SMOKE) == 68 and not any(e["trap"] for e in SMOKE)
    assert sum(1 for e in TRAP if e["trap"]) == 1
    assert sorted({k for e in TRAP for k in e} & set(DECLARABLE)) == SPIKE_FIELDS


def test_agent_d_knows_exactly_the_fields_a_header_may_list():
    """AGENT_D keeps its own copy of the list, so it runs without AGENT_A on
    its path; this holds the copy to the schema."""
    cc = _load("cc_fields_reported", CLI)
    assert sorted(cc.DECLARABLE_FIELDS) == sorted(DECLARABLE)


# ── the header is not an instruction ─────────────────────────────────────────

@pytest.mark.parametrize("extra", [(), ("--max-parse-errors", "0")], ids=["default", "no-parse-errors"])
def test_the_header_is_skipped_and_never_counted(tmp_path, extra):
    log = declare(SMOKE, SPIKE_FIELDS)
    rc, rep, out = run(tmp_path, log, log, *extra)
    assert rc == EXIT_PASS, out
    assert rep["stats"]["total_steps"] == len(SMOKE)
    assert rep["stats"]["rtl_parse_warnings"] == 0 and rep["stats"]["iss_parse_warnings"] == 0


@pytest.mark.parametrize("name, log", [("smoke", SMOKE), ("trap", TRAP)])
def test_both_sides_declaring_the_same_fields_compare_as_today(tmp_path, name, log):
    rc0, legacy, _ = run(tmp_path, log, log)
    rc1, declared, out = run(tmp_path, declare(log, SPIKE_FIELDS), declare(log, SPIKE_FIELDS))
    assert rc1 == rc0 == EXIT_PASS, out
    assert verdict(declared) == verdict(legacy)
    assert declared["fields_reported"] == {"rtl": SPIKE_FIELDS, "iss": SPIKE_FIELDS}
    assert declared["fields_not_compared"] == []


def test_the_same_mismatch_is_found_declared_as_undeclared(tmp_path):
    rtl = copy.deepcopy(TRAP)
    k = _reg_index(rtl)
    reg = next(iter(rtl[k]["regs"]))
    rtl[k]["regs"][reg] = "0xdeadbeef"
    rc0, legacy, _ = run(tmp_path, rtl, TRAP)
    rc1, declared, out = run(tmp_path, declare(rtl, SPIKE_FIELDS), declare(TRAP, SPIKE_FIELDS))
    assert rc1 == rc0 == EXIT_MISMATCH, out
    assert verdict(declared) == verdict(legacy)


# ── trap, the field a header makes reportable ────────────────────────────────

@pytest.mark.parametrize("name, log", [("smoke", SMOKE), ("trap", TRAP)])
@pytest.mark.parametrize("side", ["rtl", "iss"])
def test_trap_listed_by_one_side_only_is_exit_3_and_named(tmp_path, name, log, side):
    """On csr_trap the side that cannot see traps would otherwise read as a core
    that did not take the ecall: a trap mismatch that is not one."""
    blind, sees = declare(log, NO_TRAP), declare(log, SPIKE_FIELDS)
    rtl, iss = (blind, sees) if side == "rtl" else (sees, blind)
    rc, rep, out = run(tmp_path, rtl, iss)
    assert rc == EXIT_INCOMPLETE, out
    assert rep["status"] == "incomplete"
    assert rep["incomplete_reasons"] == [f"field_not_reported:trap:{side}"]
    assert rep["mismatches"] == []
    assert rep["fields_reported"][side] == NO_TRAP


@pytest.mark.parametrize("name, log", [("smoke", SMOKE), ("trap", TRAP)])
def test_trap_listed_by_neither_side_is_recorded_not_compared(tmp_path, name, log):
    rc, rep, out = run(tmp_path, declare(log, NO_TRAP), declare(log, NO_TRAP))
    assert rc == EXIT_PASS, out
    assert rep["fields_not_compared"] == ["trap"]
    assert rep["incomplete_reasons"] == []


def test_a_trap_mismatch_when_both_list_trap_is_exit_1(tmp_path):
    iss = declare(TRAP, SPIKE_FIELDS)
    rtl = copy.deepcopy(iss)
    rtl[1 + _trap_index(TRAP)]["trap"]["cause"] = "0x00000002"
    rc, rep, out = run(tmp_path, rtl, iss)
    assert rc == EXIT_MISMATCH, out
    assert [m["differing_field"] for m in rep["mismatches"]] == ["trap_cause"]


def test_a_trap_one_side_did_not_take_is_exit_1_when_both_list_trap(tmp_path):
    """trap: null under a header is a report ("no trap"), so it is compared."""
    iss = declare(TRAP, SPIKE_FIELDS)
    rtl = copy.deepcopy(iss)
    rtl[1 + _trap_index(TRAP)]["trap"] = None
    rc, rep, out = run(tmp_path, rtl, iss)
    assert rc == EXIT_MISMATCH, out
    assert rep["mismatches"][0]["mismatch_type"] == "TRAPMISMATCH"


@pytest.mark.parametrize("side", ["rtl", "iss"])
def test_a_register_mismatch_outranks_an_unreported_trap(tmp_path, side):
    rtl, iss = declare(TRAP, NO_TRAP), declare(TRAP, SPIKE_FIELDS)
    if side == "iss":
        rtl, iss = iss, rtl
    k = 1 + _reg_index(TRAP)
    reg = next(iter(rtl[k]["regs"]))
    rtl[k]["regs"][reg] = "0xdeadbeef"
    rc, rep, out = run(tmp_path, rtl, iss, "--all", "--max-mismatches", "0")
    assert rc == EXIT_MISMATCH, out
    assert rep["status"] == "failed"
    assert [m["differing_field"] for m in rep["mismatches"]] == ["rd_val"]
    assert rep["incomplete_reasons"] == [f"field_not_reported:trap:{side}"]


# ── legacy logs keep today's behaviour ───────────────────────────────────────

@pytest.mark.parametrize("name, log", [("smoke", SMOKE), ("trap", TRAP)])
def test_a_legacy_pair_is_unchanged_and_recorded_undeclared(tmp_path, name, log):
    rc, rep, out = run(tmp_path, log, log)
    assert rc == EXIT_PASS, out
    assert rep["fields_reported"] == {"rtl": LEGACY, "iss": LEGACY}
    assert rep["fields_not_compared"] == []
    assert rep["incomplete_reasons"] == [] and rep["mismatches"] == []


def test_a_legacy_log_without_trap_still_reads_as_no_trap(tmp_path):
    """Today's inference, kept for headerless logs: an absent trap is "none
    happened", so the ecall the other side took is a trap mismatch."""
    rtl = [{k: v for k, v in e.items() if k != "trap"} for e in TRAP]
    rc, rep, out = run(tmp_path, rtl, TRAP)
    assert rc == EXIT_MISMATCH, out
    assert rep["mismatches"][0]["mismatch_type"] == "TRAPMISMATCH"
    assert rep["fields_reported"]["rtl"] == LEGACY


@pytest.mark.parametrize("side", ["rtl", "iss"])
def test_a_header_without_trap_against_a_legacy_log_is_exit_3(tmp_path, side):
    """A legacy log is read as reporting trap (absent = no trap, as today), so a
    headered side that does not list it is the one-sided case."""
    blind = declare(TRAP, NO_TRAP)
    rtl, iss = (blind, TRAP) if side == "rtl" else (TRAP, blind)
    rc, rep, out = run(tmp_path, rtl, iss)
    assert rc == EXIT_INCOMPLETE, out
    assert rep["incomplete_reasons"] == [f"field_not_reported:trap:{side}"]
    other = "iss" if side == "rtl" else "rtl"
    assert rep["fields_reported"] == {side: NO_TRAP, other: LEGACY}


def test_a_header_with_trap_against_a_legacy_log_compares_as_today(tmp_path):
    rc0, legacy, _ = run(tmp_path, TRAP, TRAP)
    rc1, mixed, out = run(tmp_path, declare(TRAP, SPIKE_FIELDS), TRAP)
    assert rc1 == rc0 == EXIT_PASS, out
    assert verdict(mixed) == verdict(legacy)
    assert mixed["fields_reported"] == {"rtl": SPIKE_FIELDS, "iss": LEGACY}


def test_a_real_ibex_pair_names_the_trap_channel_once_declared(tmp_path):
    """The case #177 is about.  The recorded Ibex trace has no trap channel; as a
    legacy log it reads as "no traps" and the pair passes.  Declared the way its
    converter can report it, the gap is named."""
    ibex = _load("fr_d_trace_to_commitlog", VAL / "trace_to_commitlog.py")
    spike = _load("fr_d_spike_to_commitlog", VAL / "spike_to_commitlog.py")
    cmp_ = _load("fr_d_ibex_compare", VAL / "compare.py")
    fx = VAL / "fixtures"
    rtl = ibex.convert_file(str(fx / "muldiv.ibex.log"))
    iss = spike.convert((fx / "muldiv.spike.log").read_text(encoding="utf-8"), fx / "muldiv.elf")
    cut = cmp_.cut_at_halt(rtl, iss)
    rtl, iss = cut[0], cut[1]
    skips = ("--skip-fields", "privilege", "csr_writes")      # as the Ibex validation runs it
    rc0, legacy, out0 = run(tmp_path, rtl, iss, *skips)
    assert rc0 == EXIT_PASS, out0
    ibex_fields = sorted({k for e in rtl for k in e} & set(DECLARABLE))
    assert "trap" not in ibex_fields
    iss_fields = sorted({k for e in iss for k in e} & set(DECLARABLE) | {"trap"})
    rc, rep, out = run(tmp_path, declare(rtl, ibex_fields), declare(iss, iss_fields), *skips)
    assert rc == EXIT_INCOMPLETE, out
    assert rep["incomplete_reasons"] == ["field_not_reported:trap:rtl"]


# ── --skip-fields ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("side", ["rtl", "iss"])
def test_skip_fields_trap_still_works(tmp_path, side):
    blind, sees = declare(TRAP, NO_TRAP), declare(TRAP, SPIKE_FIELDS)
    rtl, iss = (blind, sees) if side == "rtl" else (sees, blind)
    rc, rep, out = run(tmp_path, rtl, iss, "--skip-fields", "trap")
    assert rc == EXIT_PASS, out
    assert "trap" in rep["config"]["skip_fields"]
    assert rep["incomplete_reasons"] == []


# ── malformed: exit 2, never a verdict ───────────────────────────────────────

def _assert_malformed(rc, rep, out, side, *words):
    assert rc == EXIT_MALFORMED, out
    assert rep is None, "a malformed log must not produce a comparison verdict"
    assert f"[{side.upper()}]" in out, out
    assert "malformed" in out.lower(), out
    for w in words:
        assert w in out, (w, out)


@pytest.mark.parametrize("side", ["rtl", "iss"])
@pytest.mark.parametrize("field", SPIKE_FIELDS)
def test_a_listed_field_missing_from_a_record_is_malformed(tmp_path, field, side):
    bad, good = declare(TRAP, SPIKE_FIELDS), declare(TRAP, SPIKE_FIELDS)
    del bad[5][field]
    rtl, iss = (bad, good) if side == "rtl" else (good, bad)
    _assert_malformed(*run(tmp_path, rtl, iss), side, repr(field))


def test_a_listed_trap_absent_on_a_record_is_malformed_not_no_trap(tmp_path):
    bad, good = declare(SMOKE, SPIKE_FIELDS), declare(SMOKE, SPIKE_FIELDS)
    for r in bad[1:]:
        del r["trap"]
    _assert_malformed(*run(tmp_path, bad, good), "rtl", "'trap'")


@pytest.mark.parametrize("side", ["rtl", "iss"])
@pytest.mark.parametrize("field, value", [
    ("trap", None), ("csrs", {}), ("cycle", 7),
    ("trap", {"cause": "0x0000000b", "tval": "0x00000000", "epc": "0x80000010"}),
], ids=["trap-null", "csrs-empty", "cycle", "trap-taken"])
def test_a_record_carrying_an_unlisted_field_is_malformed(tmp_path, field, value, side):
    """Even null: a producer may not claim less than it emits (rule 4)."""
    fields = [f for f in SPIKE_FIELDS if f != field]
    bad, good = declare(TRAP, fields), declare(TRAP, fields)
    bad[3][field] = value
    rtl, iss = (bad, good) if side == "rtl" else (good, bad)
    _assert_malformed(*run(tmp_path, rtl, iss), side, repr(field))


def test_a_legacy_key_under_a_header_is_malformed(tmp_path):
    """rd/rd_val is AGENT_D's legacy spelling of regs; under a header that does
    not list regs it would report registers the header says are not reported."""
    fields = [f for f in SPIKE_FIELDS if f != "regs"]
    bad, good = declare(TRAP, fields), declare(TRAP, fields)
    bad[3]["rd"], bad[3]["rd_val"] = 10, "0x00000001"
    _assert_malformed(*run(tmp_path, bad, good), "rtl", "'rd'")


@pytest.mark.parametrize("where", ["second", "late", "late-in-legacy"])
def test_a_header_that_is_not_first_or_is_duplicated_is_malformed(tmp_path, where):
    good = declare(SMOKE, SPIKE_FIELDS)
    if where == "second":
        bad = [good[0]] + good
    elif where == "late":
        bad = good[:5] + [good[0]] + good[5:]
    else:
        bad = SMOKE[:5] + [header(SPIKE_FIELDS)] + SMOKE[5:]
        good = SMOKE
    _assert_malformed(*run(tmp_path, good, bad), "iss", "header")


@pytest.mark.parametrize("hdr", [
    {"_header": {"schema_version": "2.2.0", "fields_reported": ["regs", "pc"]}},
    {"_header": {"schema_version": "2.2.0", "fields_reported": ["regs", "stores"]}},
    {"_header": {"schema_version": "2.2.0", "fields_reported": ["regs", "regs"]}},
    {"_header": {"schema_version": "2.2.0", "fields_reported": "regs"}},
    {"_header": {"schema_version": "2.1.0", "fields_reported": ["regs"]}},
    {"_header": {"schema_version": "2.2.0", "fields_reported": ["regs"], "rtl_ended": {}}},
    {"_header": {"schema_version": "2.2.0"}},
    {"_header": ["regs"]},
    {"_header": {"schema_version": "2.2.0", "fields_reported": ["regs"]}, "seq": 0},
], ids=["identity", "label", "duplicate", "not-a-list", "version", "extra-key",
        "no-list", "not-an-object", "extra-top-level-key"])
def test_a_header_the_format_does_not_define_is_malformed(tmp_path, hdr):
    good = declare(SMOKE, ["regs"])
    bad = [hdr] + good[1:]
    _assert_malformed(*run(tmp_path, good, bad), "iss", "header")


def test_a_header_with_no_records_is_malformed(tmp_path):
    rc, rep, out = run(tmp_path, [header(SPIKE_FIELDS)], [header(SPIKE_FIELDS)])
    assert rc == EXIT_MALFORMED, out
    assert rep is None


def test_a_malformed_record_after_a_real_mismatch_is_still_malformed(tmp_path):
    """Stopping on the first mismatch must not turn an unreadable log into a
    verdict about the part that was read."""
    good = declare(SMOKE, SPIKE_FIELDS)
    bad = copy.deepcopy(good)
    k = 1 + _reg_index(SMOKE)
    reg = next(iter(bad[k]["regs"]))
    bad[k]["regs"][reg] = "0xdeadbeef"
    del bad[-1]["priv"]
    _assert_malformed(*run(tmp_path, bad, good), "rtl", "'priv'")


def test_a_malformed_log_is_exit_2_through_the_manifest_too(tmp_path):
    """AVA's contract mode: unreadable input is EXIT_INFRA there as well."""
    rundir = tmp_path / "run"
    (rundir / "outputs").mkdir(parents=True)
    bad = declare(SMOKE, SPIKE_FIELDS)
    del bad[4]["regs"]
    _write(rundir / "outputs" / "rtlcommit.jsonl", bad)
    _write(rundir / "outputs" / "isscommitlog.jsonl", declare(SMOKE, SPIKE_FIELDS))
    manifest = rundir / "manifest.json"
    manifest.write_text(json.dumps({"rundir": str(rundir), "seed": 1}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(CLI), "--manifest", str(manifest)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == EXIT_MALFORMED, proc.stdout + proc.stderr
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    assert recorded["status"] == "infra_error"
    error = json.dumps(recorded)
    assert "'regs'" in error and "malformed" in error.lower(), error

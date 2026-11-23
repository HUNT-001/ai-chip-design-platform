"""The commit-log declares which fields it reports: `fields_reported` (#177, PR 1 of 4).

Written from the approved #177 design and the schema, not from the validator:

* A log may open with one header record, `{"_header": {"schema_version": "2.2.0",
  "fields_reported": [...]}}`.  It is the first line of the JSONL, so the
  declaration travels with the log and cannot be separated from it.
* Rule 1: with a header, the list is authoritative.  A listed field missing from a
  record makes the log malformed; a field not listed is not reported.
* Rule 4: a record that carries a field the list does not name makes the log
  malformed.  A producer may not claim less than it emits.
* Only fields a producer may leave out can be listed: every record property
  except the seven every record carries.  Anything else in the list is unknown.
* A log with no header is legacy.  It is still valid, the per-field inference of
  #164/#176 applies to it, and the report says `fields_reported: "undeclared"`.

Rules 2 and 3 are the comparators' job (PR 2/3 of #177) and are not tested here.

The legacy-accept cases use the real Spike and Ibex fixtures, converted the way
the Ibex validation converts them.
"""
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "AGENT_A" / "commitlog.schema.json"
VALIDATOR = REPO_ROOT / "AGENT_A" / "commitlog_validator.py"
IBEX_FIX = REPO_ROOT / "validation" / "ibex" / "fixtures"
SPIKE_FIX = REPO_ROOT / "tests" / "fixtures" / "spike"

#: what every record carries whatever the producer can see; never declarable
IDENTITY = {"schema_version", "hart", "seq", "pc", "instr", "instr_width", "_src"}

#: the trap the schema's own `ecall` example takes
A_TRAP = {"cause": "0x0000000b", "tval": "0x00000000", "tvec": "0x80000080",
          "epc": "0x80000040", "is_interrupt": False, "exception_code": 11}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cv():
    if not VALIDATOR.exists():
        pytest.fail(f"{VALIDATOR.relative_to(REPO_ROOT)} does not exist")
    return _load("commitlog_validator_under_test", VALIDATOR)


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


_SPIKE = _load("fr_spike_to_commitlog", REPO_ROOT / "validation" / "ibex" / "spike_to_commitlog.py")
_IBEX = _load("fr_trace_to_commitlog", REPO_ROOT / "validation" / "ibex" / "trace_to_commitlog.py")


def _spike(log, elf):
    return _SPIKE.convert(log.read_text(encoding="utf-8"), elf)


#: every real log in the tree, by fixture
REAL_LOGS = {
    **{f"spike/{p.stem}": (lambda p=p: _spike(p.with_suffix(".log"), p))
       for p in sorted(SPIKE_FIX.glob("*.elf"))},
    **{f"ibex-spike/{p.stem}": (lambda p=p: _spike(IBEX_FIX / f"{p.stem}.spike.log", p))
       for p in sorted(IBEX_FIX.glob("*.elf"))},
    **{f"ibex/{p.name.split('.')[0]}": (lambda p=p: _IBEX.convert_file(str(p)))
       for p in sorted(IBEX_FIX.glob("*.ibex.log"))},
}

#: #184: the Spike converter writes a trap with no tvec and a null priv
SPIKE_TRAP_XFAIL = pytest.mark.xfail(
    strict=True, reason="#184: converted Spike trap record has no tvec and priv null")


def _real_log_params():
    return [pytest.param(name, id=name,
                         marks=[SPIKE_TRAP_XFAIL] if name == "spike/csr_trap" else [])
            for name in REAL_LOGS]


_CACHE = {}


def real(name):
    if name not in _CACHE:
        _CACHE[name] = REAL_LOGS[name]()
    return copy.deepcopy(_CACHE[name])


def small(name):
    """A real log cut short, keeping every store, so a mutation test stays fast."""
    recs = real(name)
    return recs[:40] + [r for r in recs[40:] if "mem_writes" in r]


def header(fields, version="2.2.0"):
    return {"_header": {"schema_version": version, "fields_reported": list(fields)}}


def declare(records, fields=None):
    """The same records as a declared 2.2.0 log: every listed field on every
    record, null where the record has nothing to say (no store, no trap)."""
    recs = copy.deepcopy(records)
    if fields is None:
        fields = sorted({k for r in recs for k in r} - IDENTITY)
    for r in recs:
        r["schema_version"] = "2.2.0"
        for f in fields:
            r.setdefault(f, None)
    return [header(fields)] + recs


def mentions(errors, *words):
    return any(all(w in e for w in words) for e in errors)


# ── the schema ───────────────────────────────────────────────────────────────

def test_the_schema_is_2_2_0(schema):
    assert schema["$id"].endswith("/v2.2.0")
    assert "2.2.0" in json.dumps(schema["$defs"]["schema_version"])


def test_every_record_must_carry_exactly_the_identity_fields(schema):
    assert set(schema["required"]) == IDENTITY


def test_the_declarable_fields_are_every_record_property_but_the_identity(schema):
    items = schema["$defs"]["log_header"]["properties"]["_header"]["properties"]["fields_reported"]["items"]
    assert set(items["enum"]) == set(schema["properties"]) - IDENTITY


def test_the_header_examples_meet_the_header_definition(schema):
    import jsonschema
    hs = {"$defs": schema["$defs"], "$ref": "#/$defs/log_header"}
    examples = schema["$defs"]["log_header"].get("examples", [])
    assert examples, "no header example in the schema"
    v = jsonschema.Draft202012Validator(hs)
    for ex in examples:
        assert sorted(e.message for e in v.iter_errors(ex)) == []


# ── legacy: no header ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", _real_log_params())
def test_a_real_headerless_log_is_valid_and_undeclared(cv, name):
    recs = real(name)
    rep = cv.validate_records(recs)
    assert rep["errors"] == [] and rep["valid"] is True, rep["errors"][:5]
    assert rep["fields_reported"] == "undeclared"
    assert rep["records"] == len(recs)


def test_a_headerless_ibex_log_without_csrs_or_priv_is_still_valid(cv):
    recs = real("ibex/csr")
    assert all("csrs" not in r and "priv" not in r for r in recs)
    assert cv.validate_records(recs)["valid"] is True


# ── declared: the header lists exactly what the records carry ────────────────

@pytest.mark.parametrize("name", ["spike/rv32im_smoke", "ibex/loadstore", "ibex/csr"])
def test_a_header_listing_exactly_what_the_records_carry_is_valid(cv, name):
    log = declare(small(name))
    rep = cv.validate_records(log)
    assert rep["errors"] == [] and rep["valid"] is True, rep["errors"][:5]
    assert rep["fields_reported"] == sorted(log[0]["_header"]["fields_reported"])
    assert rep["records"] == len(log) - 1, "the header is not a record"


def test_an_ibex_header_need_not_list_what_ibex_cannot_see(cv):
    rep = cv.validate_records(declare(small("ibex/csr")))
    assert rep["valid"] is True
    assert not {"csrs", "priv", "trap", "perf_counters"} & set(rep["fields_reported"])


# (a) a listed field missing from a record

@pytest.mark.parametrize("name", ["spike/rv32im_smoke", "ibex/loadstore"])
def test_a_listed_field_missing_from_any_record_is_malformed(cv, name):
    log = declare(small(name))
    fields = log[0]["_header"]["fields_reported"]
    assert fields
    for f in fields:
        bad = copy.deepcopy(log)
        del bad[3][f]
        rep = cv.validate_records(bad)
        assert rep["valid"] is False, f
        assert mentions(rep["errors"], f, "line 4"), (f, rep["errors"])


# (b) a record carrying a field the header does not list

@pytest.mark.parametrize("name", ["spike/rv32im_smoke", "ibex/loadstore"])
def test_a_record_carrying_an_unlisted_field_is_malformed(cv, name):
    log = declare(small(name))
    fields = log[0]["_header"]["fields_reported"]
    for f in fields:
        bad = copy.deepcopy(log)
        bad[0]["_header"]["fields_reported"] = [x for x in fields if x != f]
        rep = cv.validate_records(bad)
        assert rep["valid"] is False, f
        assert mentions(rep["errors"], f), (f, rep["errors"])


def test_an_unlisted_field_set_to_null_still_counts_as_carried(cv):
    log = declare(small("ibex/csr"))
    log[2]["csrs"] = None
    assert cv.validate_records(log)["valid"] is False


# (c) an unknown name in the list

@pytest.mark.parametrize("unknown", ["csr_writes", "stores", "privilege", "pc", "seq", "x-foo"])
def test_an_unknown_or_identity_name_in_the_list_is_rejected(cv, unknown):
    log = declare(small("ibex/csr"))
    log[0]["_header"]["fields_reported"].append(unknown)
    rep = cv.validate_records(log)
    assert rep["valid"] is False
    assert mentions(rep["errors"], unknown), rep["errors"]


def test_a_name_listed_twice_is_rejected(cv):
    log = declare(small("ibex/csr"))
    log[0]["_header"]["fields_reported"].append("regs")
    assert cv.validate_records(log)["valid"] is False


@pytest.mark.parametrize("bad", ["all", None, {"regs": True}])
def test_fields_reported_must_be_a_list(cv, bad):
    log = declare(small("ibex/csr"))
    log[0]["_header"]["fields_reported"] = bad
    assert cv.validate_records(log)["valid"] is False


def test_a_header_with_a_key_the_format_does_not_define_is_rejected(cv):
    log = declare(small("ibex/csr"))
    log[0]["_header"]["rtl_ended"] = {"reason": "halt_write", "addr": "0x00020008"}
    assert cv.validate_records(log)["valid"] is False


# (d) one header, first

def test_a_second_header_is_rejected(cv):
    log = declare(small("ibex/csr"))
    log.insert(5, copy.deepcopy(log[0]))
    rep = cv.validate_records(log)
    assert rep["valid"] is False
    assert mentions(rep["errors"], "line 6", "header"), rep["errors"]


def test_a_header_that_is_not_first_is_rejected(cv):
    log = declare(small("ibex/csr"))
    log.insert(1, log.pop(0))
    rep = cv.validate_records(log)
    assert rep["valid"] is False
    assert mentions(rep["errors"], "line 2", "header"), rep["errors"]


def test_a_header_late_in_a_headerless_log_is_rejected(cv):
    recs = real("ibex/csr")
    recs.insert(3, header(["regs"]))
    assert cv.validate_records(recs)["valid"] is False


# versions

def test_a_header_must_say_2_2_0(cv):
    log = declare(small("ibex/csr"))
    log[0]["_header"]["schema_version"] = "2.1.0"
    assert cv.validate_records(log)["valid"] is False


def test_a_2_1_0_record_under_a_header_is_rejected(cv):
    log = declare(small("ibex/csr"))
    log[4]["schema_version"] = "2.1.0"
    rep = cv.validate_records(log)
    assert rep["valid"] is False
    assert mentions(rep["errors"], "line 5"), rep["errors"]


# ── trap ─────────────────────────────────────────────────────────────────────

def test_a_listed_trap_that_is_null_on_every_record_is_valid(cv):
    """Listed and null everywhere: the producer reports traps and none occurred."""
    log = declare(small("spike/rv32im_smoke"))
    assert "trap" in log[0]["_header"]["fields_reported"]
    assert all(r["trap"] is None for r in log[1:])
    assert cv.validate_records(log)["valid"] is True


def test_a_listed_trap_with_a_trap_taken_is_valid(cv):
    log = declare(small("spike/rv32im_smoke"))
    log[6]["trap"] = copy.deepcopy(A_TRAP)
    assert cv.validate_records(log)["valid"] is True


def test_a_listed_trap_absent_from_a_record_is_malformed(cv):
    """Under a header, absent is not a way to say "no trap"; null is."""
    log = declare(small("spike/rv32im_smoke"))
    del log[6]["trap"]
    rep = cv.validate_records(log)
    assert rep["valid"] is False
    assert mentions(rep["errors"], "trap", "line 7"), rep["errors"]


def test_an_unlisted_trap_that_a_record_carries_is_malformed(cv):
    log = declare(small("ibex/csr"))
    assert "trap" not in log[0]["_header"]["fields_reported"]
    log[6]["trap"] = copy.deepcopy(A_TRAP)
    rep = cv.validate_records(log)
    assert rep["valid"] is False
    assert mentions(rep["errors"], "trap", "line 7"), rep["errors"]


# ── what is not a log ────────────────────────────────────────────────────────

def test_a_log_with_no_records_is_not_valid(cv):
    assert cv.validate_records([])["valid"] is False
    assert cv.validate_records([header(["regs"])])["valid"] is False


def test_a_line_that_is_not_json_is_malformed(cv):
    lines = [json.dumps(r) for r in small("ibex/csr")]
    lines.insert(2, "core   0: 0x80000000 (0x00000013)")
    rep = cv.validate_lines(lines)
    assert rep["valid"] is False
    assert mentions(rep["errors"], "line 3"), rep["errors"]


def test_a_record_that_breaks_the_record_schema_is_malformed(cv):
    recs = small("ibex/csr")
    recs[1]["regs"] = {"x0": "0x00000001"}
    assert cv.validate_records(recs)["valid"] is False


# ── the CLI ──────────────────────────────────────────────────────────────────

def _cli(*args):
    return subprocess.run([sys.executable, str(VALIDATOR), *map(str, args)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)


def _write(path, objs):
    path.write_text("".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")
    return path


def test_the_cli_exits_0_on_a_valid_log(tmp_path):
    p = _write(tmp_path / "ok.jsonl", declare(small("ibex/csr")))
    proc = _cli(p)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_cli_exits_0_on_a_legacy_log_and_says_undeclared(tmp_path):
    p = _write(tmp_path / "legacy.jsonl", small("ibex/csr"))
    proc = _cli(p)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "undeclared" in proc.stdout


def test_the_cli_exits_1_on_a_malformed_log(tmp_path):
    log = declare(small("ibex/csr"))
    del log[2]["regs"]
    proc = _cli(_write(tmp_path / "bad.jsonl", log))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "regs" in proc.stdout


def test_the_cli_exits_3_when_it_cannot_read_the_log(tmp_path):
    proc = _cli(tmp_path / "missing.jsonl")
    assert proc.returncode == 3, proc.stdout + proc.stderr


def test_the_cli_verdict_does_not_depend_on_the_console_encoding(tmp_path):
    """#147: a log's own non-ASCII text must not turn exit 1 (malformed) into a
    crash under an ASCII console, which would also exit 1 for the wrong reason."""
    rec = dict(small("ibex/csr")[0], _src="rétl", disasm="addi é")
    p = _write(tmp_path / "na.jsonl", [rec])
    import os
    for enc in ("utf-8", "ascii"):
        env = dict(os.environ, PYTHONIOENCODING=enc)
        proc = subprocess.run([sys.executable, str(VALIDATOR), str(p)], cwd=REPO_ROOT, env=env,
                              capture_output=True, timeout=120)
        out = proc.stdout + proc.stderr
        assert b"UnicodeEncodeError" not in out and b"Traceback" not in out, (enc, out[-400:])
        assert proc.returncode == 1, (enc, out[-400:])
        assert b"MALFORMED" in proc.stdout and b"_src" in proc.stdout, (enc, out[-400:])

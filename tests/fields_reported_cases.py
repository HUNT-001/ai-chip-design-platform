"""Headered variants of real commit logs, shared by the #177 (PR 3 of 4) tests.

Not a test module.  tests/test_orchestrator_fields_reported.py holds the
orchestrator to these cases.  tests/test_comparator_differential.py holds the
orchestrator and AGENT_D to each other on the same cases.

Every expectation is written from docs/dut_commit_log.md section 4 and #177,
not from either comparator:

  pass        nothing wrong was found and every compared field was compared
  finding     a compared field differs, or the program end does (exit 1)
  incomplete  a field one side lists and the other does not (exit 3)
  malformed   a declared log breaks its own header: nothing is compared.  The
              orchestrator says "could not verify" (exit 3).  AGENT_D says
              "unreadable input" (exit 2).  It is never a pass and never a finding

Inputs are the recorded logs in the tree, converted by the real converters:
the Ibex/Spike pairs in validation/ibex/fixtures/ and the Spike logs in
tests/fixtures/spike/.  `declare` prepends a header and puts every listed
field on every record.  The value is null where the record has nothing to
say, which for trap means "no trap" and for mem_writes "no store" (#192).
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
VAL = REPO_ROOT / "validation" / "ibex"
SPIKE_FIX = REPO_ROOT / "tests" / "fixtures" / "spike"
SCHEMA = REPO_ROOT / "AGENT_A" / "commitlog.schema.json"

LEGACY = "undeclared (legacy)"
#: What validation/ibex/compare.py skips in AGENT_D: the fields an Ibex trace
#: cannot report and the orchestrator does not compare.
IBEX_SKIPS = ("--skip-fields", "privilege", "csr_writes")

_schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
DECLARABLE = sorted(_schema["$defs"]["log_header"]["properties"]["_header"]
                    ["properties"]["fields_reported"]["items"]["enum"])


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclasses look their module up by name
    spec.loader.exec_module(mod)
    return mod


def header(fields) -> Dict[str, Any]:
    return {"_header": {"schema_version": "2.2.0", "fields_reported": list(fields)}}


def declare(log: List[Dict[str, Any]], fields) -> List[Dict[str, Any]]:
    """A real log as a declared 2.2.0 log listing `fields`: the header first,
    every unlisted declarable field removed.  It fills in nothing: a listed
    field missing from a record is a ValueError, because the test, not the
    log, would be inventing it (#192).  A caller that means to supply a value
    says so with `with_default`."""
    recs = copy.deepcopy(log)
    for r in recs:
        r["schema_version"] = "2.2.0"
        for f in DECLARABLE:
            if f not in fields:
                r.pop(f, None)
        for f in fields:
            if f not in r:
                raise ValueError(f"declare: record seq={r.get('seq')} has no {f!r}, which "
                                 f"the header would list; the log does not report it")
    return [header(fields)] + recs


def with_default(log: List[Dict[str, Any]], field: str, value: Any) -> List[Dict[str, Any]]:
    """A copy of `log` with `field` set to `value` on every record that lacks
    it: the caller states what it supplies that the producer did not write."""
    return [dict(r, **{field: copy.deepcopy(value)}) if field not in r else copy.deepcopy(r)
            for r in log]


def fields_on_every_record(log) -> List[str]:
    """The declarable fields every record of `log` carries."""
    return sorted(set(DECLARABLE).intersection(*(set(e) for e in log)))


def fields_of(log) -> List[str]:
    return sorted({k for e in log for k in e} & set(DECLARABLE))


def ibex_pair(prog: str):
    """(Ibex log, Spike log) for one validation program, headerless, as the
    real converters write them."""
    ibex = _load("fr_cases_trace_to_commitlog", VAL / "trace_to_commitlog.py")
    spike = _load("fr_cases_spike_to_commitlog", VAL / "spike_to_commitlog.py")
    fix = VAL / "fixtures"
    return (ibex.convert_file(str(fix / f"{prog}.ibex.log")),
            spike.convert((fix / f"{prog}.spike.log").read_text(encoding="utf-8"),
                          fix / f"{prog}.elf"))


def spike_canonical(name: str, upto: int) -> List[Dict[str, Any]]:
    """The first `upto` aligned entries of a recorded Spike log as canonical
    JSONL (the shape tests/test_agent_d_fields_reported.py uses).  Spike prints
    no store size and no tval: the size comes from funct3, and tval is 0."""
    cp = _load("fr_cases_coverage_pipeline", REPO_ROOT / "AGENT_F" / "coverage_pipeline.py")
    log = cp.parse_spike_commit_log((SPIKE_FIX / f"{name}.log").read_text(encoding="utf-8"))
    aligned, reason = cp.align_to_entry(log, cp.elf_entry_pc(SPIKE_FIX / f"{name}.elf"))
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


class Case(NamedTuple):
    rtl: List[Dict[str, Any]]
    iss: List[Dict[str, Any]]
    rtl_ended: Optional[Dict[str, Any]]
    expect: str                 # pass | finding | incomplete | malformed
    reason: Optional[str]       # the label the orchestrator's verdict must carry
    agent_d_skips: tuple        # AGENT_D --skip-fields for this pair


EXPECTED_EXIT = {
    # outcome -> (orchestrator, AGENT_D).  The one difference is a malformed
    # log: AGENT_D exits 2 for any unreadable input, and the orchestrator's
    # convention is that anything it cannot verify is incomplete, 3.
    "pass": (0, 0), "finding": (1, 1), "incomplete": (3, 3), "malformed": (3, 2),
}

#: The fields the Ibex/Spike pair can both declare: what each side reports and
#: the orchestrator compares, plus the cycle annotation.
COMMON = ["cycle", "mem_writes", "regs"]
SPIKE_FIELDS = ["csrs", "mem_writes", "priv", "regs", "trap"]


def _flip_first_reg(log, start):
    k = next(m for m in range(start, len(log)) if log[m].get("regs")
             and "x0" not in log[m]["regs"])
    name = next(iter(log[k]["regs"]))
    log[k]["regs"][name] = f"0x{int(log[k]['regs'][name], 16) ^ 0x10:08x}"
    return log


def build_cases() -> Dict[str, Case]:
    rtl, iss = ibex_pair("loadstore")
    rtl_fields, iss_fields = fields_of(rtl), fields_of(iss)
    ended = {"reason": "halt_write", "addr": rtl[-1]["mem_writes"][-1]["addr"]}
    both = COMMON + ["trap"]
    K = 5                       # a record in the middle; it is line K + 2 under a header
    # What these cases supply that the converters do not write, said here
    # because declare() fills in nothing: the Ibex trace cannot see traps, so
    # an Ibex log that lists trap is told "no trap" on every record; Spike has
    # no cycle count, so a Spike log that lists cycle gets the schema's null.
    rtl_t = with_default(rtl, "trap", None)
    iss_c = with_default(iss, "cycle", None)

    def same():
        return declare(rtl_t, both), declare(iss_c, both)

    def ibex(r, i, expect, reason=None):
        return Case(r, i, ended, expect, reason, IBEX_SKIPS)

    cases: Dict[str, Case] = {}
    cases["same_declarations"] = ibex(*same(), "pass")
    cases["natural_declarations"] = ibex(
        declare(rtl, rtl_fields), declare(iss, iss_fields), "incomplete",
        "field_not_reported:trap:rtl")
    cases["trap_listed_by_rtl_only"] = ibex(
        declare(rtl_t, both), declare(iss_c, COMMON), "incomplete", "field_not_reported:trap:iss")
    cases["trap_listed_by_neither"] = ibex(declare(rtl, COMMON), declare(iss_c, COMMON), "pass")
    cases["regs_listed_by_rtl_only"] = ibex(
        declare(rtl_t, both), declare(iss_c, ["cycle", "mem_writes", "trap"]), "incomplete",
        "field_not_reported:regs:iss")
    # The headerless RTL's null is "not reported"; the declared ISS's null is
    # "no store" (#192).  So stores is reported by one side only.
    cases["rtl_legacy_iss_declared"] = ibex(copy.deepcopy(rtl), declare(iss, iss_fields),
                                            "incomplete", "field_not_reported:stores:rtl")
    cases["rtl_declared_iss_legacy"] = ibex(
        declare(rtl, rtl_fields), copy.deepcopy(iss), "incomplete", "field_not_reported:trap:rtl")
    cases["register_mismatch_and_unreported_trap"] = ibex(
        _flip_first_reg(declare(rtl, rtl_fields), 1), declare(iss, iss_fields), "finding")

    r, i = same()
    cases["declared_halt_one_short"] = ibex(r[:-1], i, "finding")

    # malformed: the RTL log breaks its header at record K (line K + 2)
    def broken(mutate, side="rtl"):
        r, i = same()
        mutate(r if side == "rtl" else i)
        return r, i

    def drop_regs(log):
        del log[1 + K]["regs"]

    def add_priv(log):
        log[1 + K]["priv"] = None

    def add_rd(log):
        log[1 + K]["rd"] = 10

    def header_third(log):
        log.insert(3, log.pop(0))

    def header_twice(log):
        log.insert(1, copy.deepcopy(log[0]))

    def lists_a_label(log):
        log[0]["_header"]["fields_reported"].append("stores")

    line = f"line{K + 2}"
    cases["listed_field_missing"] = ibex(*broken(drop_regs), "malformed",
                                         f"malformed_log:rtl:{line}:regs")
    cases["unlisted_field_present"] = ibex(*broken(add_priv), "malformed",
                                           f"malformed_log:rtl:{line}:priv")
    cases["alias_key"] = ibex(*broken(add_rd), "malformed", f"malformed_log:rtl:{line}:rd")
    cases["header_not_first"] = ibex(*broken(header_third), "malformed",
                                     "malformed_log:rtl:line4:header")
    cases["header_twice"] = ibex(*broken(header_twice), "malformed",
                                 "malformed_log:rtl:line2:header")
    cases["header_lists_a_comparator_label"] = ibex(*broken(lists_a_label), "malformed",
                                                    "malformed_log:rtl:line1:stores")
    cases["iss_listed_field_missing"] = ibex(*broken(drop_regs, "iss"), "malformed",
                                             f"malformed_log:iss:{line}:regs")
    r, i = same()
    cases["header_and_no_records"] = ibex(r[:1], i, "malformed", "malformed_log:rtl:end:header")
    r, i = same()
    _flip_first_reg(r, 1)
    del r[-3]["regs"]
    cases["malformed_after_a_mismatch"] = ibex(
        r, i, "malformed", f"malformed_log:rtl:line{len(r) - 2}:regs")

    # trap values: a real machine ecall (tests/fixtures/spike/csr_trap), both
    # sides listing trap.  Spike vs itself, so only the mutated field differs.
    trap_log = spike_canonical("csr_trap", 18)
    t = next(n for n, e in enumerate(trap_log) if e["trap"])

    def trap_pair(mutate):
        r = declare(trap_log, SPIKE_FIELDS)
        mutate(r[1 + t])
        return Case(r, declare(trap_log, SPIKE_FIELDS), None, "finding", None, ())

    cases["trap_cause_differs"] = trap_pair(
        lambda e: e["trap"].__setitem__("cause", "0x00000002"))
    cases["trap_missing_on_rtl"] = trap_pair(lambda e: e.__setitem__("trap", None))
    cases["trap_listed_both_equal"] = Case(
        declare(trap_log, SPIKE_FIELDS), declare(trap_log, SPIKE_FIELDS), None, "pass", None, ())

    # null under a header (#192).  Both logs list mem_writes, so a non-store's
    # "mem_writes": null is "reported, none" -- the schema allows no empty list,
    # so null is the only way to say it.  A store on one side against that null
    # is a store mismatch, a finding, never "could not check".
    def no_store(r, i):
        return next(m for m in range(2, len(r)) if r[m].get("mem_writes") is None
                    and i[m].get("mem_writes") is None and not i[m].get("trap"))

    r, i = same()
    r[no_store(r, i)]["mem_writes"] = [dict(SPURIOUS_STORE)]
    cases["spurious_store_under_headers"] = ibex(r, i, "finding")
    r, i = same()
    k = next(m for m in range(1, len(r) - 1) if i[m].get("mem_writes"))   # not the halt store
    r[k]["mem_writes"] = None
    cases["dropped_store_under_headers"] = ibex(r, i, "finding")
    # listed by one side only: still not reported, whatever the other side holds
    cases["mem_writes_listed_by_rtl_only"] = ibex(
        declare(rtl_t, both), declare(iss_c, ["cycle", "regs", "trap"]), "incomplete",
        "field_not_reported:stores:iss")
    # headerless: null keeps meaning "not reported", exactly as before #192
    r, i = copy.deepcopy(rtl), copy.deepcopy(iss)
    r[no_store(r, i)]["mem_writes"] = [dict(SPURIOUS_STORE)]
    cases["legacy_spurious_store_is_still_not_reported"] = ibex(
        r, i, "incomplete", "field_not_reported:stores:iss")
    # A headerless store against a declared null is a store against "no
    # store": a finding, which outranks the not-reported stores elsewhere.
    # Only a headerless null stays "not reported".
    r, i = copy.deepcopy(rtl), declare(iss, iss_fields)
    r[no_store([None] + r, i) - 1]["mem_writes"] = [dict(SPURIOUS_STORE)]
    cases["legacy_store_against_declared_null"] = ibex(r, i, "finding")

    # regs, csrs and priv have no null in the schema, so under a header that
    # lists them a null is a malformed log, not "none" and not "not reported".
    def null_regs(log):
        log[1 + K]["regs"] = None

    cases["regs_null_under_header"] = ibex(*broken(null_regs), "malformed",
                                           f"malformed_log:rtl:{line}:regs")
    cases["iss_regs_null_under_header"] = ibex(*broken(null_regs, "iss"), "malformed",
                                               f"malformed_log:iss:{line}:regs")
    for f in ("csrs", "priv"):
        r = declare(trap_log, SPIKE_FIELDS)
        r[1 + K][f] = None
        cases[f"{f}_null_under_header"] = Case(
            r, declare(trap_log, SPIKE_FIELDS), None, "malformed",
            f"malformed_log:rtl:{line}:{f}", ())
    return cases


#: A store no record of the fixtures makes.
SPURIOUS_STORE = {"addr": "0x00001000", "size": 4, "data": "0xdeadbeef"}

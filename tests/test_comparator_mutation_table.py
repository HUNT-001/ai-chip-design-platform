"""Single-field mutants of the real Spike logs, through both comparators (#159, #160).

The differential test (#137) varies only `mem_writes`, a field no parser emits
(#160), so it cannot say which real fields the comparators read. This table
does: each row takes a recorded Spike log (tests/fixtures/spike/, see
PROVENANCE.md), changes exactly one thing on one side, and asks whether the
orchestrator's comparator (`SpikeISS._compare_results`, the path a real run
takes) and AGENT_D's CLI notice.

Expected for every row, written from the schema and not from the code: DETECTED,
meaning a finding, or "could not compare" (AGENT_D exit 3, the orchestrator's
`unobserved`), or a mismatch (AGENT_D exit 1). A row a comparator does not
detect today is listed in KNOWN_GAPS with its issue and runs as a strict xfail,
so fixing it fails the build until the entry is removed.

Two rows run on a declared copy of the smoke log (`smoke_declared`: a
fields_reported header listing what Spike reports, and `"mem_writes": null` on
every non-store, the only "no store" the schema allows under a header).  For
those rows the schema says more than DETECTED: null under a header is
"reported, none" (#192), so a store against it is a store mismatch, and the
row asks for a finding (FINDING_ROWS), not "could not compare".

Spike prints no store size and no trap value, so those two are filled in
identically on both sides (size from the instruction's funct3, `tval` as 0);
AGENT_D is run with `--skip-fields mem_size` only where the table says so.
"""
import asyncio
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
from ava_patched import SpikeISS

FIX = REPO_ROOT / "tests" / "fixtures" / "spike"
CLI = REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"


def _canonical(name, upto):
    log = cp.with_trap_vectors(
        cp.parse_spike_commit_log((FIX / f"{name}.log").read_text(encoding="utf-8")))
    aligned, reason = cp.align_to_entry(log, cp.elf_entry_pc(FIX / f"{name}.elf"))
    assert reason is None
    out = []
    for i, e in enumerate(aligned[:upto]):
        stores = [{"addr": m["addr"], "size": 1 << (int(e["instr"], 16) >> 12 & 3), "data": m["data"]}
                  for m in e["mem"] if m["op"] == "store"]
        trap = None
        if e["trap"]:
            trap = {"cause": e["trap"]["cause"], "tval": "0x00000000", "epc": e["trap"]["epc"]}
            if "tvec" in e["trap"]:
                trap["tvec"] = e["trap"]["tvec"]
        out.append({"schema_version": "2.1.0", "seq": i, "hart": 0, "pc": e["pc"],
                    "instr": e["instr"], "priv": e["priv"] or "M", "trap": trap,
                    "regs": dict(e["regs"]), "csrs": dict(e["csrs"]), "mem_writes": stores})
    return out


#: What Spike reports, as validation/ibex/spike_to_commitlog.py declares it.
SPIKE_FIELDS = ["csrs", "mem_writes", "priv", "regs", "trap"]


def _declared(log):
    """`log` as a declared 2.2.0 log: a header listing SPIKE_FIELDS, and null
    for a non-store's mem_writes, as the converters write it (#177, #192)."""
    recs = [dict(e, schema_version="2.2.0", mem_writes=e["mem_writes"] or None) for e in log]
    return [{"_header": {"schema_version": "2.2.0", "fields_reported": SPIKE_FIELDS}}] + recs


BASE = {
    # Each ends two entries past the program's terminal self-loop (index 67 and
    # 27), as a real log does: Spike keeps retiring `j .` until it polls tohost.
    "smoke": _canonical("rv32im_smoke", 70),
    "csr_trap": _canonical("csr_trap", 30),
}
BASE["smoke_declared"] = _declared(BASE["smoke"])
PROGRAM_END = {"smoke": 67, "csr_trap": 27}


def _first(log, pred, after=0):
    return next(i for i, e in enumerate(log) if i >= after and pred(e))


def _flip(hexstr):
    return f"0x{int(hexstr, 16) ^ 0x10:08x}"


# ── mutants: each changes one thing. They get the two logs and the baseline ──

def m_instr_word(rtl, iss, base):
    rtl[5]["instr"] = _flip(rtl[5]["instr"])


def m_register_name(rtl, iss, base):
    i = _first(base, lambda e: e["regs"])
    (name, val), = rtl[i]["regs"].items()
    rtl[i]["regs"] = {"x31" if name != "x31" else "x30": val}


def m_first_register_value(rtl, iss, base):
    i = _first(base, lambda e: e["regs"])
    name = next(iter(rtl[i]["regs"]))
    rtl[i]["regs"][name] = _flip(rtl[i]["regs"][name])


def _second_write(base_logs):
    """Both sides get a second register write on one entry, equal. The mutant
    then changes it on the RTL side."""
    i = _first(base_logs, lambda e: e["regs"])
    return i


def m_later_register_write(rtl, iss, base):
    i = _second_write(base)
    for log in (rtl, iss):
        log[i]["regs"]["x30"] = "0x00000011"
    rtl[i]["regs"]["x30"] = "0x00000012"


def m_missing_write_rtl(rtl, iss, base):
    rtl[_first(base, lambda e: e["regs"])]["regs"] = {}


def m_missing_write_iss(rtl, iss, base):
    iss[_first(base, lambda e: e["regs"])]["regs"] = {}


def m_extra_write(rtl, iss, base):
    i = _first(base, lambda e: not e["regs"])
    rtl[i]["regs"] = {"x31": "0x00000001"}


def m_csr_value(rtl, iss, base):
    i = _first(base, lambda e: e["csrs"])
    name = next(iter(rtl[i]["csrs"]))
    rtl[i]["csrs"][name] = _flip(rtl[i]["csrs"][name])


def m_csr_name(rtl, iss, base):
    """The RTL writes a different CSR (mstatush for mscratch) with the same value."""
    i = _first(base, lambda e: "mscratch" in e["csrs"])
    rtl[i]["csrs"] = {"mstatush": rtl[i]["csrs"]["mscratch"]}


def m_csr_missing_rtl(rtl, iss, base):
    rtl[_first(base, lambda e: e["csrs"])]["csrs"] = {}


def m_privilege(rtl, iss, base):
    rtl[3]["priv"] = "U"


def m_trap_cause(rtl, iss, base):
    i = _first(base, lambda e: e["trap"])
    rtl[i]["trap"]["cause"] = "0x00000002"


def m_trap_missing_rtl(rtl, iss, base):
    rtl[_first(base, lambda e: e["trap"])]["trap"] = None


def m_store_address(rtl, iss, base):
    i = _first(base, lambda e: e["mem_writes"])
    rtl[i]["mem_writes"][0]["addr"] = _flip(rtl[i]["mem_writes"][0]["addr"])


def m_store_data(rtl, iss, base):
    i = _first(base, lambda e: e["mem_writes"])
    rtl[i]["mem_writes"][0]["data"] = _flip(rtl[i]["mem_writes"][0]["data"])


def m_store_missing_rtl(rtl, iss, base):
    rtl[_first(base, lambda e: e["mem_writes"])]["mem_writes"] = []


def m_store_missing_iss(rtl, iss, base):
    iss[_first(base, lambda e: e["mem_writes"])]["mem_writes"] = []


#: A store no record of the fixtures makes.
SPURIOUS_STORE = {"addr": "0x00001000", "size": 4, "data": "0xdeadbeef"}


def m_spurious_store(rtl, iss, base):
    """The RTL stores on a record where the ISS reports no store (#192)."""
    i = _first(base, lambda e: "_header" not in e and not e.get("mem_writes")
               and not e.get("trap"), after=3)
    rtl[i]["mem_writes"] = [dict(SPURIOUS_STORE)]


def m_store_dropped(rtl, iss, base):
    """The RTL reports no store, as null, where the ISS stores (#192)."""
    rtl[_first(base, lambda e: e.get("mem_writes"))]["mem_writes"] = None


def _ends_before_the_program_ends(back):
    """The RTL log stops `back` entries before the terminal self-loop."""
    def mutant(rtl, iss, base):
        end = PROGRAM_END["smoke"]
        del rtl[end - back:]
    return mutant


def _runs_on(n):
    """The RTL log carries `n` more instructions after the program's end."""
    def mutant(rtl, iss, base):
        for k in range(1, n + 1):
            e = copy.deepcopy(rtl[-1])
            e["seq"] = len(rtl)
            e["pc"] = f"0x{int(rtl[-1]['pc'], 16) + 4 * k:08x}"
            e["regs"], e["mem_writes"] = {}, []
            rtl.append(e)
    return mutant


#: row id -> (fixture, mutant)
ROWS = {
    "instr_word":              ("smoke",    m_instr_word),
    "register_name":           ("smoke",    m_register_name),
    "first_register_value":    ("smoke",    m_first_register_value),
    "later_register_write":    ("smoke",    m_later_register_write),
    "missing_write_rtl":       ("smoke",    m_missing_write_rtl),
    "missing_write_iss":       ("smoke",    m_missing_write_iss),
    "extra_write":             ("smoke",    m_extra_write),
    "csr_value":               ("csr_trap", m_csr_value),
    "csr_name":                ("csr_trap", m_csr_name),
    "csr_missing_rtl":         ("csr_trap", m_csr_missing_rtl),
    "privilege":               ("smoke",    m_privilege),
    "trap_cause":              ("csr_trap", m_trap_cause),
    "trap_missing_rtl":        ("csr_trap", m_trap_missing_rtl),
    "store_address":           ("smoke",    m_store_address),
    "store_data":              ("smoke",    m_store_data),
    "store_missing_rtl":       ("smoke",    m_store_missing_rtl),
    "store_missing_iss":       ("smoke",    m_store_missing_iss),
    "spurious_store_declared": ("smoke_declared", m_spurious_store),
    "store_dropped_declared":  ("smoke_declared", m_store_dropped),
    "short_log_1":             ("smoke",    _ends_before_the_program_ends(0)),
    "short_log_20pct":         ("smoke",    _ends_before_the_program_ends(13)),
    "long_log_1":              ("smoke",    _runs_on(1)),
    "long_log_20pct":          ("smoke",    _runs_on(14)),
}

#: Rows the schema says are a finding (exit 1, a bug), not "could not compare".
FINDING_ROWS = frozenset({"spurious_store_declared", "store_dropped_declared"})

#: (row, comparator) -> issue.  Rows the comparator does not detect today.
#: Strict xfail: when one is fixed the build fails until the entry is removed.
KNOWN_GAPS = {
    # orchestrator: no comparison of the instruction word, CSRs, privilege or traps;
    # the walk stops at the shorter log (#159).  Register writes are compared
    # as a whole, register names included, since #159's register-writes group.
    ("instr_word", "orchestrator"): "#159",
    ("csr_value", "orchestrator"): "#159",
    ("csr_name", "orchestrator"): "#159",
    ("csr_missing_rtl", "orchestrator"): "#159",
    ("privilege", "orchestrator"): "#159",
    ("trap_cause", "orchestrator"): "#159",
    ("trap_missing_rtl", "orchestrator"): "#159",
    ("long_log_1", "orchestrator"): "#159",
    # AGENT_D resolves a CSR name it does not know to address 0 (#173)
    ("csr_name", "agent_d"): "#173",
}


# ── the two comparators, run the way a real run runs them ────────────────────

def _orchestrator_outcome(tmp_path, rtl, iss):
    sim = SpikeISS(timeout=30, run_dir=tmp_path / "cmp")
    comparison = asyncio.run(sim._compare_results(
        {"commit_log": copy.deepcopy(rtl), "instructions": len(rtl)},
        {"commit_log": copy.deepcopy(iss), "instructions": len(iss)}))
    if comparison["bugs"]:
        return "finding"
    return "incomplete" if comparison.get("unobserved") else "pass"


def _agent_d_outcome(tmp_path, rtl, iss):
    r, i = tmp_path / "rtl.jsonl", tmp_path / "iss.jsonl"
    for path, log in ((r, rtl), (i, iss)):
        path.write_text("\n".join(json.dumps(e) for e in log) + "\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(CLI), str(r), str(i), "--quiet"],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode in (0, 1, 3), f"AGENT_D could not run: {proc.returncode} {proc.stderr[-400:]}"
    return {0: "pass", 1: "finding", 3: "incomplete"}[proc.returncode]


OUTCOMES = {"orchestrator": _orchestrator_outcome, "agent_d": _agent_d_outcome}


def _detects(outcome):
    return lambda tmp_path, rtl, iss: outcome(tmp_path, rtl, iss) != "pass"


COMPARATORS = {name: _detects(outcome) for name, outcome in OUTCOMES.items()}


def _mutant(row):
    fixture, mutate = ROWS[row]
    base = BASE[fixture]
    rtl, iss = copy.deepcopy(base), copy.deepcopy(base)
    mutate(rtl, iss, base)
    return rtl, iss


def _params():
    out = []
    for row in ROWS:
        for comparator in COMPARATORS:
            marks = []
            if (row, comparator) in KNOWN_GAPS:
                marks.append(pytest.mark.xfail(
                    strict=True, reason=f"{KNOWN_GAPS[(row, comparator)]}: not detected today"))
            out.append(pytest.param(row, comparator, marks=marks, id=f"{row}-{comparator}"))
    return out


@pytest.mark.parametrize("row,comparator", _params())
def test_a_single_field_mutant_is_detected(tmp_path, row, comparator):
    rtl, iss = _mutant(row)
    assert rtl != iss, f"{row}: the mutant changed nothing, so the row proves nothing"
    if row in FINDING_ROWS:
        got = OUTCOMES[comparator](tmp_path, rtl, iss)
        assert got == "finding", f"{comparator}: {row} is a finding, got {got}"
        return
    assert COMPARATORS[comparator](tmp_path, rtl, iss), (
        f"{comparator} did not notice {row}")


# ── anti-vacuity ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fixture", sorted(BASE))
@pytest.mark.parametrize("comparator", sorted(COMPARATORS))
def test_an_unmutated_log_passes_both_comparators(tmp_path, fixture, comparator):
    log = BASE[fixture]
    assert not COMPARATORS[comparator](tmp_path, copy.deepcopy(log), copy.deepcopy(log)), (
        f"{comparator} flags the unmutated {fixture} log, so every 'detected' row means nothing")


def test_the_table_has_a_floor_and_every_gap_names_a_real_row():
    assert len(ROWS) >= 23, "rows were deleted"
    assert FINDING_ROWS <= set(ROWS)
    assert len(COMPARATORS) == 2
    for (row, comparator), issue in KNOWN_GAPS.items():
        assert row in ROWS and comparator in COMPARATORS, (row, comparator)
        assert issue.startswith("#"), f"{row}/{comparator}: a gap needs an issue number"


def test_the_fixtures_hold_what_the_rows_need():
    assert any(e["csrs"] for e in BASE["csr_trap"]) and any(e["trap"] for e in BASE["csr_trap"])
    assert sum(len(e["mem_writes"]) for e in BASE["smoke"]) == 2
    assert not any(e["trap"] or e["csrs"] for e in BASE["smoke"])
    hdr, *recs = BASE["smoke_declared"]
    assert hdr["_header"]["fields_reported"] == SPIKE_FIELDS
    assert sum(1 for e in recs if e["mem_writes"] is None) == len(recs) - 2

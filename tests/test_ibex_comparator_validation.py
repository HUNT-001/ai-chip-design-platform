"""Both comparators on a real Ibex run, against Spike on the same ELF (#175).

The logs in validation/ibex/fixtures/ are recorded output, never edited (see
validation/ibex/PROVENANCE.md). The Ibex side is lowRISC Ibex simple_system's
tracer output under Verilator, converted by validation/ibex/trace_to_commitlog.py.
The Spike side is `--log-commits` output on the same ELF, converted by
validation/ibex/spike_to_commitlog.py. Both comparators run exactly the way a
real run runs them, through validation/ibex/compare.py.

The criteria are #175's, written before any data existed:

  (a) a clean run gives a verdict consistent with the fields the trace reports;
  (b) every field the trace cannot report (CSR values, privilege, trap, perf)
      is named as not compared / incomplete, never silently dropped;
  (c) the mutation table (tests/test_comparator_mutation_table.py) shows the
      same detection pattern on the Ibex log as on the Spike logs;
  (d) every divergence is reproduced and triaged before it is called a bug.

Where today's comparators fall short, the case is a strict xfail naming its
issue. Fixing the issue fails the build until the entry is removed.

Two views of each pair are used. "raw" is what a real run hands over. "at
halt" cuts both logs at the store that stops the program: Ibex's SimCtrl
ends the simulation on it, so its trace stops there, while Spike runs on
until it polls tohost. The difference between the two views is #178, and
test_the_raw_divergence_is_only_the_program_end pins it to exactly that, so
the raw xfails cannot hide anything else.
"""
import copy
import functools
import hashlib
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

VAL = REPO_ROOT / "validation" / "ibex"
FIX = VAL / "fixtures"
PROGRAMS = ("arith_branch", "muldiv", "loadstore", "csr")
COMPARATORS = ("orchestrator", "agent_d")

#: Instructions Retired, from each Ibex run's own performance counters
#: (sim.stdout; recorded in PROVENANCE.md).  The trace must hold exactly these.
IBEX_RETIRED = {"arith_branch": 58, "muldiv": 25, "loadstore": 29, "csr": 16}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ibex = _load("ibex_trace_to_commitlog", VAL / "trace_to_commitlog.py")
spike = _load("ibex_spike_to_commitlog", VAL / "spike_to_commitlog.py")
cmp_ = _load("ibex_compare", VAL / "compare.py")
table = _load("comparator_mutation_table", REPO_ROOT / "tests" / "test_comparator_mutation_table.py")


@functools.lru_cache(maxsize=None)
def _pair(prog):
    rtl = ibex.convert_file(str(FIX / f"{prog}.ibex.log"))
    iss = spike.convert((FIX / f"{prog}.spike.log").read_text(encoding="utf-8"), FIX / f"{prog}.elf")
    return rtl, iss


def pair(prog, view):
    rtl, iss = _pair(prog)
    if view == "raw":
        return copy.deepcopy(rtl), copy.deepcopy(iss)
    cut = cmp_.cut_at_halt(rtl, iss)
    assert cut is not None, f"{prog}: no common halt store"
    return copy.deepcopy(cut[0]), copy.deepcopy(cut[1])


def run(comparator, rtl, iss, agent_d_extra=()):
    with tempfile.TemporaryDirectory() as td:
        if comparator == "orchestrator":
            return cmp_.orchestrator(rtl, iss, Path(td) / "orch")
        return cmp_.agent_d(rtl, iss, Path(td), agent_d_extra)


@functools.lru_cache(maxsize=None)
def _result(prog, view, comparator):
    return run(comparator, *pair(prog, view))


def result(prog, view, comparator):
    return copy.deepcopy(_result(prog, view, comparator))


def has_mismatch(comparator, res):
    if comparator == "orchestrator":
        return bool(res["findings"])
    return res["exit"] == 1


def xfail(issue, why):
    return pytest.mark.xfail(strict=True, reason=f"{issue}: {why}")


# ── the recording ────────────────────────────────────────────────────────────

def _provenance_hashes():
    text = (VAL / "PROVENANCE.md").read_text(encoding="utf-8")
    return dict(re.findall(r"^\| `([\w.]+\.(?:elf|log))` \| `([0-9a-f]{64})` \|", text, re.M))


def test_every_fixture_is_byte_identical_to_the_recording():
    recorded = _provenance_hashes()
    on_disk = sorted(p.name for p in FIX.iterdir() if p.suffix in (".elf", ".log"))
    assert len(on_disk) == 3 * len(PROGRAMS), on_disk
    assert sorted(recorded) == on_disk, "PROVENANCE.md and fixtures/ list different files"
    for name in on_disk:
        assert hashlib.sha256((FIX / name).read_bytes()).hexdigest() == recorded[name], name


@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_ibex_trace_holds_every_retired_instruction(prog):
    """Against the core's own counter, so a truncated trace cannot pass."""
    rtl, _ = _pair(prog)
    assert len(rtl) == IBEX_RETIRED[prog]
    assert cmp_.is_halt_store(rtl[-1]), "the trace does not end at the halt store"


@pytest.mark.parametrize("prog", PROGRAMS)
def test_both_sides_reach_the_same_halt_store(prog):
    rtl, iss = _pair(prog)
    cut = cmp_.cut_at_halt(rtl, iss)
    assert cut is not None
    assert cut[2]["rtl_dropped"] == 0 and cut[2]["iss_dropped"] > 0


# ── (a) a clean run ──────────────────────────────────────────────────────────

def _a_params(view):
    out = []
    for prog in PROGRAMS:
        for c in COMPARATORS:
            marks = []
            if view == "raw":
                marks.append(xfail("#178", "Ibex stops on the halt store and never retires "
                                           "the `j .` Spike's end check waits for"))
            elif (prog, c) == ("csr", "agent_d"):
                marks.append(xfail("#176", "AGENT_D reads the absent csrs as 'no writes' and "
                                           "reports a CSR mismatch"))
            out.append(pytest.param(prog, c, marks=marks, id=f"{prog}-{c}"))
    return out


@pytest.mark.parametrize("prog,comparator", _a_params("halt"))
def test_a_every_reported_field_agrees_at_the_halt(prog, comparator):
    res = result(prog, "halt", comparator)
    assert not has_mismatch(comparator, res), res


@pytest.mark.parametrize("prog,comparator", _a_params("raw"))
def test_a_a_raw_run_gives_no_mismatch(prog, comparator):
    res = result(prog, "raw", comparator)
    assert not has_mismatch(comparator, res), res


@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_raw_divergence_is_only_the_program_end(prog):
    """(d): triaged, not tolerated. The only raw finding allowed is the
    end-of-test difference (#178), plus the csr program's CSR false positive
    (#176). Any other finding fails here."""
    o = result(prog, "raw", "orchestrator")
    assert o["findings"], "the raw run no longer diverges: remove the #178 xfails"
    for f in o["findings"]:
        assert f.startswith("instruction_count_mismatch: ") and (
            "Program end differs" in f or "Instruction count diverges" in f), f
    a = result(prog, "raw", "agent_d")
    kinds = {m.split()[0] for m in a["mismatches"]}
    allowed = {"LENGTHMISMATCH"} | ({"CSRMISMATCH"} if prog == "csr" else set())
    assert kinds and kinds <= allowed, a["mismatches"]


# ── (b) what the trace cannot report ─────────────────────────────────────────

#: record field -> the token a comparator's report must name it by
UNREPORTABLE = {"priv": "privilege", "csrs": "csr", "trap": "trap", "perf_counters": "perf"}


def named(comparator, res, token):
    if comparator == "orchestrator":
        labels = list(res["unobserved"]) + list(res["fields_not_compared"])
    else:
        labels = list(res.get("incomplete_reasons", [])) + list(res.get("not_compared", {}))
    return any(token in label for label in labels)


def _b_params():
    out = []
    for prog in PROGRAMS:
        for c in COMPARATORS:
            for field, token in UNREPORTABLE.items():
                if c == "orchestrator":
                    marks = [xfail("#159", "the orchestrator compares no CSRs, privilege, "
                                           "traps or perf, and says so nowhere")]
                elif field == "csrs":
                    marks = [xfail("#176", "an absent csrs is read as 'no CSR writes'")]
                elif field == "trap":
                    marks = [xfail("#177", "the schema reads an absent trap as 'no trap'")]
                elif field == "perf_counters":
                    marks = [xfail("#177", "ignored by design, but never named as not compared")]
                else:
                    marks = []
                out.append(pytest.param(prog, c, field, token, marks=marks,
                                        id=f"{prog}-{c}-{field}"))
    return out


@pytest.mark.parametrize("prog,comparator,field,token", _b_params())
def test_b_a_field_the_trace_cannot_report_is_named(prog, comparator, field, token):
    rtl, _ = pair(prog, "halt")
    assert all(field not in r for r in rtl), f"{field} is not absent from the Ibex log"
    res = result(prog, "halt", comparator)
    assert named(comparator, res, token), (field, res)


# ── (c) the mutation table on the real Ibex log ──────────────────────────────

#: The table's base for these rows is loadstore: it has register writes and
#: sb/sh/sw stores.  AGENT_D runs with privilege skipped -- a field this
#: channel cannot report, named under (b) -- or its unmutated baseline is
#: already "incomplete" and every row would count as detected.
BASE_PROG = "loadstore"
AGENT_D_EXTRA = ("--skip-fields", "privilege")

#: Rows whose mutant changes a field the Ibex trace never carries.  The RTL
#: side cannot exhibit them in this channel; (b) covers those fields.
INEXPRESSIBLE = {
    "csr_value": "csrs", "csr_name": "csrs", "csr_missing_rtl": "csrs",
    "privilege": "priv",
    "trap_cause": "trap", "trap_missing_rtl": "trap",
}


def _length_rows(base):
    """The table's length rows, at this log's end.  The table cuts at the Spike
    self-loop (index 67 of rv32im_smoke); here the program ends at the halt
    store, and 20% of the program is rounded from its length."""
    end = len(base) - 1
    n20 = round(0.2 * len(base))

    def short(back):
        def m(rtl, iss, b):
            del rtl[end - back:]
        return m

    def runs_on(n):
        def m(rtl, iss, b):
            for k in range(1, n + 1):
                e = copy.deepcopy(rtl[-1])
                e["seq"] = len(rtl)
                e["pc"] = f"0x{int(rtl[-1]['pc'], 16) + 4 * k:08x}"
                e["regs"] = {}
                e.pop("mem_writes", None)
                rtl.append(e)
        return m

    return {"short_log_1": short(0), "short_log_20pct": short(n20),
            "long_log_1": runs_on(1), "long_log_20pct": runs_on(n20)}


def mutant(row):
    rtl, iss = pair(BASE_PROG, "halt")
    base = copy.deepcopy(rtl)
    mutate = _length_rows(base).get(row) or table.ROWS[row][1]
    # The table's mutants pick their entry with e["mem_writes"]; here a
    # non-store has none (schema), so they choose from a view where it is [].
    view = [dict(e, mem_writes=e.get("mem_writes", [])) for e in base]
    mutate(rtl, iss, view)
    return rtl, iss


#: (row, comparator) whose detection here differs from the Spike table.
DIFFERS_FROM_SPIKE = {
    ("short_log_1", "orchestrator"): xfail(
        "#178", "with no Spike self-loop left at the halt, only the 5% length tolerance "
                "remains, and one entry short of 29 is 3.4%"),
}


def _c_params():
    out = []
    for row in table.ROWS:
        if row in INEXPRESSIBLE:
            continue
        for c in COMPARATORS:
            marks = [DIFFERS_FROM_SPIKE[(row, c)]] if (row, c) in DIFFERS_FROM_SPIKE else []
            out.append(pytest.param(row, c, marks=marks, id=f"{row}-{c}"))
    return out


def detects(comparator, rtl, iss):
    res = run(comparator, rtl, iss, AGENT_D_EXTRA)
    if comparator == "orchestrator":
        return bool(res["findings"]) or bool(res["unobserved"])
    return res["exit"] in (1, 3)


@pytest.mark.parametrize("row,comparator", _c_params())
def test_c_detection_matches_the_spike_table(row, comparator):
    rtl, iss = mutant(row)
    assert (rtl, iss) != pair(BASE_PROG, "halt"), f"{row}: the mutant changed nothing"
    spike_detects = (row, comparator) not in table.KNOWN_GAPS
    assert detects(comparator, rtl, iss) == spike_detects, (
        f"{row}/{comparator}: Spike table {'detects' if spike_detects else 'misses'} it, "
        f"the Ibex log does not agree")


@pytest.mark.parametrize("comparator", COMPARATORS)
def test_c_the_unmutated_ibex_base_passes(comparator):
    assert not detects(comparator, *pair(BASE_PROG, "halt")), (
        f"{comparator} flags the unmutated Ibex base, so every 'detected' row means nothing")


def test_c_every_table_row_is_run_or_shown_inexpressible():
    rtl, _ = pair(BASE_PROG, "halt")
    expressible = set(table.ROWS) - set(INEXPRESSIBLE)
    assert len(expressible) >= 15 and set(INEXPRESSIBLE) <= set(table.ROWS)
    for row, field in INEXPRESSIBLE.items():
        assert all(field not in r for r in rtl), f"{row}: the Ibex log does carry {field}"

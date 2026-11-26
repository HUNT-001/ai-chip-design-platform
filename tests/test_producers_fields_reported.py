"""The producers write the fields_reported header (#177, PR 4 of 4).

Written from docs/dut_commit_log.md (sections 4, 6 and 7), AGENT_A/commitlog.schema.json
2.2.0, and what each source log prints, not from the converters:

* Ibex's text trace (validation/ibex/fixtures/*.ibex.log; rtl/ibex_tracer.sv)
  prints cycle, the disassembly, the rd write and the store address and data.
  It prints no CSR value, no privilege and no trap cause, tval or epc, so its
  header lists exactly cycle, disasm, mem_writes and regs.
* Spike's commit log (tests/fixtures/spike/*.log, validation/ibex/fixtures/*.spike.log)
  prints the register write, the explicit CSR writes, the store, the privilege
  on every commit line and the trap cause and epc on an exception line.  It has
  no cycle.  Its header lists exactly csrs, mem_writes, priv, regs and trap.
* Under a header every listed field is on every record and null is a value:
  `trap: null` where no trap is taken, `mem_writes: null` on a non-store (the
  schema allows no empty list).
* The privilege digit on a Spike commit line is the mode the instruction ran in
  (riscv-isa-sim execute.cc, commit_log_stash_privilege: `last_inst_priv =
  state->prv`, stashed before the instruction executes).  An exception line
  prints none.  The trap was taken from the mode of the line before it, unless
  that line is an mret/sret/dret (they change the mode) or a trap itself.  Then
  priv is not derivable and the converter must refuse, never write null.
* With no mtvec write logged before a trap, tvec is not derivable (#184): the
  converter must refuse, never drop the trap or invent a tvec.
* An interrupt gets no tvec from mtvec's BASE: in vectored mode it goes to
  BASE + 4 * cause.
* A Spike line the parser does not recognise (an interrupt line, for one) is
  refused, never dropped.

The verdict table runs both comparators on every fixture pair, from the files
each producer's CLI writes, before (main at a9840be, recorded below) and after.
"""
import copy
import functools
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

VAL = REPO_ROOT / "validation" / "ibex"
IBEX_FIX = VAL / "fixtures"
SPIKE_FIX = REPO_ROOT / "tests" / "fixtures" / "spike"
PROGRAMS = ("arith_branch", "csr", "loadstore", "muldiv")
SPIKE_STEMS = ("csr_trap", "rv32im_smoke")

#: from what each source log prints (module docstring)
IBEX_DECLARED = ["cycle", "disasm", "mem_writes", "regs"]
SPIKE_DECLARED = ["csrs", "mem_writes", "priv", "regs", "trap"]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


IBEX = _load("p4_trace_to_commitlog", VAL / "trace_to_commitlog.py")
SPIKE = _load("p4_spike_to_commitlog", VAL / "spike_to_commitlog.py")
CMP = _load("p4_compare", VAL / "compare.py")
CV = _load("p4_commitlog_validator", REPO_ROOT / "AGENT_A" / "commitlog_validator.py")
import AGENT_F.coverage_pipeline as cp  # noqa: E402
from AGENT_A.commitlog_header import DECLARABLE_FIELDS  # noqa: E402


def _ibex_cmd(conv_dir, prog, out):
    return [sys.executable, str(Path(conv_dir) / "trace_to_commitlog.py"),
            str(IBEX_FIX / f"{prog}.ibex.log"), "-o", str(out)]


def _spike_cmd(conv_dir, log, elf, out):
    return [sys.executable, str(Path(conv_dir) / "spike_to_commitlog.py"),
            str(log), str(elf), "-o", str(out)]


def _run(cmd):
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc


#: every real fixture, as (producer, name, command builder)
SOURCES = {
    **{f"ibex/{p}": ("ibex", lambda d, o, p=p: _ibex_cmd(d, p, o)) for p in PROGRAMS},
    **{f"ibex-spike/{p}": ("spike", lambda d, o, p=p: _spike_cmd(
        d, IBEX_FIX / f"{p}.spike.log", IBEX_FIX / f"{p}.elf", o)) for p in PROGRAMS},
    **{f"spike/{s}": ("spike", lambda d, o, s=s: _spike_cmd(
        d, SPIKE_FIX / f"{s}.log", SPIKE_FIX / f"{s}.elf", o)) for s in SPIKE_STEMS},
}


@functools.lru_cache(maxsize=None)
def _written(conv_dir, name):
    """The JSONL a producer's CLI writes for a fixture, as text."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "log.jsonl"
        _run(SOURCES[name][1](conv_dir, out))
        return out.read_text(encoding="utf-8")


def written(name, conv_dir=str(VAL)):
    return _written(conv_dir, name)


def log_of(name, conv_dir=str(VAL)):
    return [json.loads(x) for x in written(name, conv_dir).splitlines() if x.strip()]


def test_every_real_fixture_is_listed():
    """Anti-vacuity: a new fixture is held to these tests the day it lands."""
    found = {f"ibex/{p.name.split('.')[0]}" for p in IBEX_FIX.glob("*.ibex.log")}
    found |= {f"ibex-spike/{p.name.split('.')[0]}" for p in IBEX_FIX.glob("*.spike.log")}
    found |= {f"spike/{p.stem}" for p in SPIKE_FIX.glob("*.log")}
    assert found == set(SOURCES) and len(found) == 10


# ── each producer's output ───────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(SOURCES))
def test_the_output_starts_with_a_header_and_passes_the_strict_validator(name, tmp_path):
    lines = written(name).splitlines()
    first = json.loads(lines[0])
    assert set(first) == {"_header"}, f"{name}: the first line is not a header: {lines[0]}"
    p = tmp_path / "log.jsonl"
    p.write_text(written(name), encoding="utf-8")
    rep = CV.validate_file(str(p))
    assert rep["valid"] is True, (name, rep["errors"][:5])
    assert rep["records"] == len(lines) - 1


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_the_header_lists_exactly_what_the_producer_can_report(name):
    want = IBEX_DECLARED if SOURCES[name][0] == "ibex" else SPIKE_DECLARED
    header, *recs = log_of(name)
    assert sorted(header["_header"]["fields_reported"]) == want
    assert header["_header"]["schema_version"] == "2.2.0"
    assert recs
    for r in recs:
        assert sorted(k for k in r if k in DECLARABLE_FIELDS) == want, (name, r["seq"])
        assert r["schema_version"] == "2.2.0"


@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_ibex_header_lists_no_csrs_priv_or_trap(prog):
    header, *recs = log_of(f"ibex/{prog}")
    assert {"csrs", "priv", "trap"}.isdisjoint(header["_header"]["fields_reported"])
    assert all({"csrs", "priv", "trap"}.isdisjoint(r) for r in recs)


@pytest.mark.parametrize("name", sorted(n for n in SOURCES if SOURCES[n][0] == "spike"))
def test_every_non_trapping_spike_record_has_trap_null(name):
    _, *recs = log_of(name)
    traps = [r for r in recs if r["trap"] is not None]
    assert all("trap" in r for r in recs)
    assert len(traps) == (1 if name == "spike/csr_trap" else 0), [r["seq"] for r in traps]


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_mem_writes_is_null_exactly_where_no_store_is_made(name):
    _, *recs = log_of(name)
    for r in recs:
        is_store = (int(r["instr"], 16) & 0x7f) == 0b0100011 and r.get("trap") is None
        assert (r["mem_writes"] is not None) == is_store, r


# ── priv on a Spike trap record ──────────────────────────────────────────────

CSR_TRAP_LOG = (SPIKE_FIX / "csr_trap.log").read_text(encoding="utf-8")
CSR_TRAP_ELF = SPIKE_FIX / "csr_trap.elf"
#: csr_trap.log line 40 and 42, by hand: the line before the trap ran in M (3)
BEFORE_TRAP = "core   0: 3 0x80000038 (0x00000493) x9  0x00000000"
EXCEPTION = "core   0: exception trap_machine_ecall, epc 0x8000003c"
MTVEC_WRITE = "core   0: 3 0x80000008 (0x30529073) c773_mtvec 0x80000070"


def test_the_hand_checked_lines_are_in_the_fixture():
    lines = CSR_TRAP_LOG.splitlines()
    assert lines[39] == BEFORE_TRAP and lines[41] == EXCEPTION and lines[15] == MTVEC_WRITE


def test_the_trap_takes_the_mode_of_the_line_before_it():
    (t,) = [r for r in SPIKE.convert(CSR_TRAP_LOG, CSR_TRAP_ELF) if r["trap"]]
    assert t["pc"] == "0x8000003c" and t["priv"] == "M"


@pytest.mark.parametrize("word,name", [(0x30200073, "mret"), (0x10200073, "sret"),
                                       (0x7b200073, "dret")])
def test_a_mode_changing_instruction_before_the_trap_is_refused(word, name):
    log = CSR_TRAP_LOG.replace(BEFORE_TRAP, f"core   0: 3 0x80000038 (0x{word:08x})")
    with pytest.raises(ValueError, match=r"epc 0x8000003c.*" + name):
        SPIKE.convert(log, CSR_TRAP_ELF)


def test_a_trap_right_after_a_trap_is_refused():
    log = CSR_TRAP_LOG.replace(BEFORE_TRAP, "core   0: exception trap_illegal_instruction, "
                                            "epc 0x80000038")
    with pytest.raises(ValueError, match=r"epc 0x8000003c.*a trap too"):
        SPIKE.convert(log, CSR_TRAP_ELF)


# ── tvec, unrecognised lines, interrupts ─────────────────────────────────────

def test_a_trap_with_no_earlier_mtvec_write_is_refused():
    log = CSR_TRAP_LOG.replace(MTVEC_WRITE + "\n", "")
    with pytest.raises(ValueError, match=r"epc 0x8000003c.*mtvec"):
        SPIKE.convert(log, CSR_TRAP_ELF)


def test_an_unrecognised_spike_line_is_refused_not_dropped():
    """A stand-in for an interrupt line: Spike names interrupts outside
    `trap_*`, so the parser files such a line as unknown."""
    line = "core   0: exception interrupt #7, epc 0x80000038"
    assert cp.classify_spike_line(line) == "unknown"
    log = CSR_TRAP_LOG.replace(BEFORE_TRAP + "\n", BEFORE_TRAP + "\n" + line + "\n")
    with pytest.raises(ValueError, match="interrupt #7"):
        SPIKE.convert(log, CSR_TRAP_ELF)


@pytest.mark.parametrize("name", sorted(n for n in SOURCES if SOURCES[n][0] == "spike"))
def test_no_real_spike_fixture_has_an_unrecognised_line(name):
    log = {"ibex-spike": lambda s: IBEX_FIX / f"{s}.spike.log",
           "spike": lambda s: SPIKE_FIX / f"{s}.log"}[name.split("/")[0]](name.split("/")[1])
    assert cp.parse_spike_log(log.read_text(encoding="utf-8")).unclassified == []


def _entry(trap=None, csrs=None):
    return {"pc": "0x80000000", "instr": "0x00000013", "regs": {}, "csrs": csrs or {},
            "mem": [], "priv": "M", "trap": trap}


def test_an_interrupt_gets_no_tvec_from_mtvec():
    """Vectored mtvec (MODE 1): an exception goes to BASE, an interrupt to
    BASE + 4 * cause.  BASE would be the wrong vector for the interrupt."""
    exc = {"cause": "0x0000000b", "epc": "0x80000000", "is_interrupt": False}
    irq = {"cause": "0x80000007", "epc": "0x80000000", "is_interrupt": True}
    out = cp.with_trap_vectors([_entry(csrs={"mtvec": "0x80000101"}),
                                _entry(trap=exc), _entry(trap=irq)])
    assert out[1]["trap"]["tvec"] == "0x80000100"
    assert "tvec" not in out[2]["trap"]


# ── the verdict table: both comparators, every fixture pair ──────────────────

def _orch(res):
    return {"verdict": res["verdict"], "findings": len(res["findings"]),
            "unobserved": sorted(res["unobserved"]),
            "not_compared": sorted(res["fields_not_compared"])}


def _agent_d(res):
    return {"exit": res["exit"], "incomplete": sorted(res.get("incomplete_reasons", [])),
            "not_compared": sorted(res.get("not_compared", {})),
            "mismatches": res.get("n_mismatches")}


def verdict_table(conv_dir=str(VAL)):
    """{pair/view: summary} from the files the CLIs in `conv_dir` write.

    Ibex pairs go through validation/ibex/compare.compare_pair with no
    --skip-fields.  A Spike fixture with no RTL run is compared with itself
    (the RTL copy relabelled `_src: rtl`)."""
    table = {}
    for prog in PROGRAMS:
        res = CMP.compare_pair(log_of(f"ibex/{prog}", conv_dir), log_of(f"ibex-spike/{prog}", conv_dir))
        for view in ("orchestrator", "orchestrator_at_halt"):
            table[f"{prog}/{view}"] = _orch(res[view])
        for view in ("agent_d_raw", "agent_d_cut", "agent_d_at_halt"):
            table[f"{prog}/{view}"] = _agent_d(res[view])
    for stem in SPIKE_STEMS:
        iss = log_of(f"spike/{stem}", conv_dir)
        rtl = [dict(r, _src="rtl") if "_src" in r else copy.deepcopy(r) for r in iss]
        with tempfile.TemporaryDirectory() as td:
            table[f"spike-self/{stem}/orchestrator"] = _orch(CMP.orchestrator(rtl, iss, Path(td) / "o"))
            table[f"spike-self/{stem}/agent_d"] = _agent_d(CMP.agent_d(rtl, iss, Path(td)))
    return table


def _o(verdict, unobserved=(), not_compared=("stores",)):
    return {"verdict": verdict, "findings": 0, "unobserved": list(unobserved),
            "not_compared": list(not_compared)}


def _d(exit_, incomplete=(), not_compared=("stores",)):
    return {"exit": exit_, "incomplete": list(incomplete), "not_compared": list(not_compared),
            "mismatches": 0}


RTL_UNDECLARED = ["field_not_reported:csrs:rtl", "field_not_reported:privilege:rtl"]
TRAP_RTL = "field_not_reported:trap:rtl"


def _ibex_rows(orch, agent_d):
    rows = {}
    for prog in PROGRAMS:
        rows[f"{prog}/orchestrator"] = rows[f"{prog}/orchestrator_at_halt"] = orch
        for v in ("agent_d_raw", "agent_d_cut", "agent_d_at_halt"):
            rows[f"{prog}/{v}"] = agent_d
    return rows


#: recorded with main's converters (a9840be): every log headerless.
BEFORE = {
    **_ibex_rows(_o("pass"), _d(3, RTL_UNDECLARED)),
    **{f"spike-self/{s}/orchestrator": _o("pass") for s in SPIKE_STEMS},
    "spike-self/csr_trap/agent_d": _d(0, not_compared=("privilege", "stores")),
    "spike-self/rv32im_smoke/agent_d": _d(0),
}

#: with the header.  Both headers list mem_writes, so a non-store's
#: `mem_writes: null` is "no store", compared and equal, and `stores` leaves
#: not_compared in every cell (#192).
AFTER = {
    **_ibex_rows(_o("incomplete", [TRAP_RTL], not_compared=()),
                 _d(3, sorted(RTL_UNDECLARED + [TRAP_RTL]), not_compared=())),
    **{f"spike-self/{s}/orchestrator": _o("pass", not_compared=()) for s in SPIKE_STEMS},
    **{f"spike-self/{s}/agent_d": _d(0, not_compared=()) for s in SPIKE_STEMS},
}

#: Every row whose verdict changes, and why.
VERDICT_CHANGES = {
    **{f"{p}/{v}": "pass -> incomplete: the Ibex header leaves trap out, so the trap "
                   "channel is named field_not_reported:trap:rtl instead of passing "
                   "as 'no traps' (#177)"
       for p in PROGRAMS for v in ("orchestrator", "orchestrator_at_halt")},
}

#: Rows whose verdict holds but whose coverage moves, and why.
COVERAGE_CHANGES = {
    **{f"{p}/{v}": "exit 3 both times; field_not_reported:trap:rtl is added to csrs and "
                   "privilege, so the trap gap is named instead of read as 'no trap'; "
                   "stores leaves not_compared (#192)"
       for p in PROGRAMS for v in ("agent_d_raw", "agent_d_cut", "agent_d_at_halt")},
    "spike-self/csr_trap/agent_d": "exit 0 both times; privilege leaves not_compared: the "
                                   "trap record now carries the derived priv; stores "
                                   "leaves it too (#192)",
    "spike-self/rv32im_smoke/agent_d": "exit 0 both times; stores leaves not_compared: a "
                                       "declared null is 'no store' (#192)",
    **{f"spike-self/{s}/orchestrator": "pass both times; stores leaves not_compared: a "
                                       "declared null is 'no store' (#192)"
       for s in SPIKE_STEMS},
}


def test_every_coverage_change_is_listed():
    moved = {k for k in AFTER if BEFORE[k] != AFTER[k]} - set(VERDICT_CHANGES)
    assert moved == set(COVERAGE_CHANGES), sorted(moved ^ set(COVERAGE_CHANGES))

_ORDER = {"pass": 0, "incomplete": 1, "mismatch": 2}
_EXIT_ORDER = {0: 0, 3: 1, 1: 2}


def _verdict(cell):
    return cell["verdict"] if "verdict" in cell else {0: "pass", 3: "incomplete",
                                                      1: "mismatch"}.get(cell["exit"])


def test_the_before_table_covers_every_pair_and_view():
    assert set(BEFORE) == set(AFTER) and len(AFTER) == 4 * 5 + 2 * 2


def test_every_change_is_the_same_or_strictly_more_informative():
    for k in AFTER:
        b, a = BEFORE[k], AFTER[k]
        assert b.get("findings", 0) == a.get("findings", 0) and b.get("mismatches") == a.get("mismatches"), k
        assert _ORDER[_verdict(a)] >= _ORDER[_verdict(b)], k
        if _verdict(b) == "mismatch":
            assert _verdict(a) == "mismatch", k
        assert set(b.get("incomplete", b.get("unobserved", []))) <= set(
            a.get("incomplete", a.get("unobserved", []))), k
    changed = {k for k in AFTER if _verdict(BEFORE[k]) != _verdict(AFTER[k])}
    assert changed == set(VERDICT_CHANGES), sorted(changed ^ set(VERDICT_CHANGES))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_every_field_the_ibex_side_does_not_report_is_named(prog):
    for v in ("agent_d_raw", "agent_d_cut", "agent_d_at_halt"):
        assert {"field_not_reported:csrs:rtl", "field_not_reported:privilege:rtl",
                TRAP_RTL} <= set(AFTER[f"{prog}/{v}"]["incomplete"])
    for v in ("orchestrator", "orchestrator_at_halt"):
        assert TRAP_RTL in AFTER[f"{prog}/{v}"]["unobserved"]


def test_the_converted_logs_round_trip_through_both_comparators():
    """The live table, from files the CLIs wrote, is the AFTER table."""
    live = verdict_table()
    assert live == AFTER, {k: live.get(k) for k in AFTER if live.get(k) != AFTER[k]}

"""AGENT_D accepts an RTL log that ends before the terminal self-loop only if it says why (#180).

#178 gave the orchestrator's comparator one program-end exception: a testbench
that halts on a store never retires the `j .` after it, so its DUT side may
declare `{"reason": "halt_write", "addr": "0x..."}` and the early end is
accepted only if the log bears it out (the last RTL entry is a store to that
address, and everything the ISS has after the RTL's last entry is the terminal
self-loop and its repeats).  AGENT_D had no such rule: it reported
LENGTHMISMATCH on every correct run whose Spike log ends in the tohost spin.

The rule under test is the same one, with the same trust model: the declaration
travels beside the log (the commit-log schema has no log-level slot), is
checked against the log and never trusted, and a declaration that fails a check
is a finding of its own.  AGENT_D takes it as `--rtl-ended '<json>'`.

Inputs are the four real Ibex runs and Spike's logs on the same ELFs
(validation/ibex/fixtures/, #175), never edited, with the two fields the Ibex
trace cannot report skipped by name exactly as the harness does.
"""
import copy
import functools
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

VAL = REPO_ROOT / "validation" / "ibex"
FIX = VAL / "fixtures"
CLI = REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"
PROGRAMS = ("arith_branch", "muldiv", "loadstore", "csr")

#: What validation/ibex/compare.py skips: the fields an Ibex trace cannot report.
SKIPS = ("--skip-fields", "privilege", "csr_writes")
ABSENT = object()


class Raw(str):
    """Command-line text passed as typed, not JSON-encoded from a value."""


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ibex = _load("agent_d_halt_trace_to_commitlog", VAL / "trace_to_commitlog.py")
spike = _load("agent_d_halt_spike_to_commitlog", VAL / "spike_to_commitlog.py")
table = _load("agent_d_halt_mutation_table", REPO_ROOT / "tests" / "test_comparator_mutation_table.py")


@functools.lru_cache(maxsize=None)
def _pair(prog):
    rtl = ibex.convert_file(str(FIX / f"{prog}.ibex.log"))
    iss = spike.convert((FIX / f"{prog}.spike.log").read_text(encoding="utf-8"),
                        FIX / f"{prog}.elf")
    return rtl, iss


def logs(prog):
    rtl, iss = _pair(prog)
    return copy.deepcopy(rtl), copy.deepcopy(iss)


def halt_addr(rtl):
    return rtl[-1]["mem_writes"][-1]["addr"]


def declared(addr):
    return {"reason": "halt_write", "addr": addr}


def renumbered(log):
    """Step numbers contiguous again after a case inserted or dropped entries."""
    for n, e in enumerate(log):
        e["seq"] = n
    return log


def agent_d(tmp_path, rtl, iss, rtl_ended=ABSENT, flags=("--all", "--max-mismatches", "0"),
            skips=SKIPS):
    """Run AGENT_D's CLI the way the harness does; (exit code, bug report)."""
    r, i, rep = tmp_path / "rtl.jsonl", tmp_path / "iss.jsonl", tmp_path / "report.json"
    for path, log in ((r, rtl), (i, iss)):
        path.write_text("".join(json.dumps(e) + "\n" for e in log), encoding="utf-8")
    if rep.exists():
        rep.unlink()
    cmd = [sys.executable, str(CLI), str(r), str(i), *flags, "--quiet",
           "--bug-report", str(rep), *skips]
    if rtl_ended is not ABSENT:
        cmd += ["--rtl-ended", rtl_ended if isinstance(rtl_ended, Raw) else json.dumps(rtl_ended)]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode in (0, 1, 3), f"AGENT_D could not run: {proc.returncode} {proc.stderr[-600:]}"
    assert rep.exists(), f"no report written: {proc.stdout[-300:]} {proc.stderr[-600:]}"
    return proc.returncode, json.loads(rep.read_text(encoding="utf-8"))


def kinds(report):
    return {m.get("mismatch_type") or m.get("type") for m in report["mismatches"]}


def texts(report):
    return [m["description"] for m in report["mismatches"]]


def says_end_differs(report):
    return "LENGTHMISMATCH" in kinds(report)


def rejects_declaration(report):
    return any("declared" in t.lower() and "rejected" in t.lower() for t in texts(report))


# ── the fixtures hold what these cases need ──────────────────────────────────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_fixture_ends_at_a_halt_store_and_spike_runs_on(prog):
    """Anti-vacuity: the early end really is the case under test."""
    rtl, iss = logs(prog)
    n = len(rtl)
    assert any(w["addr"] == halt_addr(rtl) for w in rtl[-1]["mem_writes"])
    assert all(e["pc"] != rtl[-1]["pc"] for e in rtl[:-1]), "the halt store is not unique"
    assert iss[n]["pc"] == iss[n + 1]["pc"] != rtl[-1]["pc"]
    assert len({e["pc"] for e in iss[n:]}) == 1 and len(iss) > n + 10


# ── accepted: declared, last entry is the store, only the loop remains ───────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declared_halt_write_ending_before_the_loop_passes(tmp_path, prog):
    rtl, iss = logs(prog)
    rc, rep = agent_d(tmp_path, rtl, iss, declared(halt_addr(rtl)))
    assert rc == 0 and rep["status"] == "passed" and rep["mismatches"] == [], (rc, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_it_passes_under_the_default_flags_too(tmp_path, prog):
    """The run a user types, without --all: the first mismatch would stop it."""
    rtl, iss = logs(prog)
    rc, rep = agent_d(tmp_path, rtl, iss, declared(halt_addr(rtl)), flags=())
    assert rc == 0, (rc, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_report_records_what_was_declared(tmp_path, prog):
    rtl, iss = logs(prog)
    addr = halt_addr(rtl)
    rc, rep = agent_d(tmp_path, rtl, iss, declared(addr))
    pe = rep.get("program_end")
    assert pe is not None, "an accepted early end must not be silent"
    assert pe["end"] == "rtl_stopped_before_terminal_loop"
    assert pe["rtl_ended"] == {"reason": "halt_write", "addr": addr}
    assert int(pe["pc"], 16) == int(iss[len(rtl)]["pc"], 16)
    assert pe["index"] == len(rtl) and pe["rtl_repeats_dropped"] == 0
    assert pe["iss_repeats_dropped"] == len(iss) - len(rtl)
    assert rep["config"]["rtl_ended"] == declared(addr)


# ── refused: no declaration ──────────────────────────────────────────────────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_the_same_logs_without_a_declaration_still_fail(tmp_path, prog):
    rtl, iss = logs(prog)
    rc, rep = agent_d(tmp_path, rtl, iss)
    assert rc == 1 and kinds(rep) == {"LENGTHMISMATCH"}, (rc, texts(rep))
    assert not rejects_declaration(rep), "nothing was declared, so nothing can be rejected"
    assert rep.get("program_end") is None


# ── refused: the declaration does not hold against the log ───────────────────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_for_another_address_fails(tmp_path, prog):
    rtl, iss = logs(prog)
    wrong = f"0x{int(halt_addr(rtl), 16) + 8:08x}"
    rc, rep = agent_d(tmp_path, rtl, iss, declared(wrong))
    assert rc == 1 and says_end_differs(rep) and rejects_declaration(rep), (rc, texts(rep))
    assert rep.get("program_end") is None


@pytest.mark.parametrize("prog", PROGRAMS)
@pytest.mark.parametrize("back", [1, 2, 3])
def test_a_declaration_on_a_log_that_stops_short_of_the_halt_store_fails(tmp_path, prog, back):
    """back=1 is the log missing its final store; 2 and 3 stop earlier still."""
    rtl, iss = logs(prog)
    rc, rep = agent_d(tmp_path, rtl[:-back], iss, declared(halt_addr(rtl)))
    assert rc == 1 and says_end_differs(rep) and rejects_declaration(rep), (rc, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_a_log_truncated_mid_program_fails(tmp_path, prog):
    rtl, iss = logs(prog)
    for cut in (1, len(rtl) // 4, len(rtl) // 2, 3 * len(rtl) // 4):
        rc, rep = agent_d(tmp_path, rtl[:cut], iss, declared(halt_addr(rtl)))
        assert rc == 1 and says_end_differs(rep) and rejects_declaration(rep), (cut, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_fails_when_the_loop_is_not_the_only_remainder(tmp_path, prog):
    """The RTL ends on the halt store, but Spike retired another instruction
    between that store and the loop, so the log stopped short of the program."""
    rtl, iss = logs(prog)
    n = len(rtl)
    extra = {"seq": n, "pc": "0x00100ff0", "instr": "0x00000013", "regs": {}}
    iss = renumbered(iss[:n] + [extra] + iss[n:])
    rc, rep = agent_d(tmp_path, rtl, iss, declared(halt_addr(rtl)))
    assert rc == 1 and says_end_differs(rep) and rejects_declaration(rep), (rc, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declared_log_with_an_instruction_after_the_loop_fails(tmp_path, prog):
    rtl, iss = logs(prog)
    n = len(rtl)
    loop = copy.deepcopy(iss[n])
    after = {"seq": n + 1, "pc": f"0x{int(loop['pc'], 16) + 4:08x}",
             "instr": "0x00000013", "regs": {}}
    rc, rep = agent_d(tmp_path, renumbered(rtl + [loop, after]), iss, declared(halt_addr(rtl)))
    assert rc == 1 and rejects_declaration(rep), (rc, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_a_log_that_kept_running_into_the_loop_fails(tmp_path, prog):
    """It says it stopped on the halt write, but the log goes on to retire the
    loop, so the stop reason it gave is not what ended the log."""
    rtl, iss = logs(prog)
    n = len(rtl)
    spin = [copy.deepcopy(iss[n + k]) for k in range(3)]
    rc, rep = agent_d(tmp_path, renumbered(rtl + spin), iss, declared(halt_addr(rtl)))
    assert rc == 1 and rejects_declaration(rep), (rc, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_is_not_a_licence_for_a_remainder_with_no_terminal_loop(tmp_path, prog):
    """Valid declaration, valid last store, but what Spike has after it is not
    a self-loop: the early end is still a LENGTHMISMATCH."""
    rtl, iss = logs(prog)
    n = len(rtl)
    tail = [{"seq": n + k, "pc": f"0x{0x00100f00 + 4 * k:08x}", "instr": "0x00000013", "regs": {}}
            for k in range(2 * n)]
    rc, rep = agent_d(tmp_path, rtl, renumbered(iss[:n] + tail), declared(halt_addr(rtl)))
    assert rc == 1 and says_end_differs(rep), (rc, texts(rep))
    assert rep.get("program_end") is None


@pytest.mark.parametrize("bad", [
    {}, [], "halt_write", 7,
    {"reason": "timeout", "addr": "ADDR"},
    {"reason": "halt_write"},
    {"addr": "ADDR"},
    {"reason": "halt_write", "addr": "not-an-address"},
    {"reason": "halt_write", "addr": None},
], ids=lambda b: repr(b)[:40])
def test_a_malformed_declaration_is_a_finding_not_ignored(tmp_path, bad):
    rtl, iss = logs("loadstore")
    if isinstance(bad, dict):
        bad = {k: (halt_addr(rtl) if v == "ADDR" else v) for k, v in bad.items()}
    rc, rep = agent_d(tmp_path, rtl, iss, bad)
    assert rc == 1 and says_end_differs(rep) and rejects_declaration(rep), (rc, texts(rep))


def test_a_declaration_that_is_not_json_is_a_finding_not_a_crash(tmp_path):
    rtl, iss = logs("loadstore")
    rc, rep = agent_d(tmp_path, rtl, iss, Raw("halt_write@" + halt_addr(rtl)))
    assert rc == 1 and rejects_declaration(rep), (rc, texts(rep))


# ── a declaration is never a blanket pass ────────────────────────────────────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_does_not_hide_a_divergence_before_the_halt(tmp_path, prog):
    rtl, iss = logs(prog)
    k = next(n for n, e in enumerate(rtl) if e["regs"])
    name = next(iter(rtl[k]["regs"]))
    rtl[k]["regs"][name] = f"0x{int(rtl[k]['regs'][name], 16) ^ 0x10:08x}"
    rc, rep = agent_d(tmp_path, rtl, iss, declared(halt_addr(rtl)))
    assert rc == 1 and kinds(rep) - {"LENGTHMISMATCH"}, (rc, texts(rep))
    assert not rejects_declaration(rep), "the declaration itself was true"


# ── logs cut at the halt on both sides: nothing to align, still verified ─────

@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_logs_both_cut_at_the_halt_store_is_accepted(tmp_path, prog):
    rtl, iss = logs(prog)
    n = len(rtl)
    rc, rep = agent_d(tmp_path, rtl, iss[:n], declared(halt_addr(rtl)))
    assert rc == 0 and rep["mismatches"] == [], (rc, texts(rep))


@pytest.mark.parametrize("prog", PROGRAMS)
def test_a_declaration_on_logs_cut_one_entry_short_of_the_halt_fails(tmp_path, prog):
    """Both logs end together, so no length check can see it; the declaration's
    own check (the last RTL entry is the halt store) is what catches it."""
    rtl, iss = logs(prog)
    n = len(rtl)
    rc, rep = agent_d(tmp_path, rtl[:-1], iss[:n - 1], declared(halt_addr(rtl)))
    assert rc == 1 and rejects_declaration(rep), (rc, texts(rep))


# ── the Spike-fixture length rows keep their status ──────────────────────────

LENGTH_ROWS = ("short_log_1", "short_log_20pct", "long_log_1", "long_log_20pct")


@pytest.mark.parametrize("row", LENGTH_ROWS)
def test_the_spike_fixture_length_rows_still_fail_with_no_declaration(tmp_path, row):
    rtl, iss = table._mutant(row)
    rc, rep = agent_d(tmp_path, rtl, iss, skips=())
    assert rc == 1 and kinds(rep) == {"LENGTHMISMATCH"}, (rc, texts(rep))


@pytest.mark.parametrize("row", LENGTH_ROWS)
def test_the_spike_fixture_length_rows_still_fail_with_a_declaration(tmp_path, row):
    """No Spike store at this address, so no declaration can be borne out."""
    rtl, iss = table._mutant(row)
    rc, rep = agent_d(tmp_path, rtl, iss, declared("0x00000004"), skips=())
    assert rc == 1 and rejects_declaration(rep), (rc, texts(rep))

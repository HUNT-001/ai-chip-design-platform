"""#137: the orchestrator's comparator and AGENT_D must agree on pass/fail.

Fabricated commit-log pairs that differ only in stores are run through
ava_patched.CommitLogComparator and through AGENT_D's compare_commitlogs CLI
(exit 0 clean, 1 mismatch).  The expectation for each pair is written from the
schema (a store differs, or it does not), then both tools are held to it and to
each other.  A disagreement that is not a bug fixed here is listed in
KNOWN_DIVERGENCE with its issue, so it stays visible and cannot widen.

The second half does the same for the program end (#178, #180): real Ibex runs
whose log stops at a halt store before Spike's terminal self-loop, with and
without the DUT's declaration of that halt, and with declarations the logs do
not bear out.  The orchestrator is run the way a real run runs it
(`SpikeISS._compare_results`, which applies `align_program_end`); AGENT_D gets
the same declaration as `--rtl-ended`.
"""
import asyncio
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

from ava_patched import CommitLogComparator, SpikeISS

N = 6
BASE = dict(schema_version="2.1.0", hart=0, priv="M", trap=None,
            instr="0x00a12023", regs={}, csrs={})


def _e(i, writes="__none__"):
    e = dict(BASE, seq=i, pc=f"0x{0x80000000 + 4 * i:08x}")
    if writes != "__none__":
        e["mem_writes"] = writes
    return e


def _w(addr="0x80001000", size=4, data="0x0000002a"):
    return {"addr": addr, "size": size, "data": data}


def _pair(rtl_w, iss_w, at=3):
    """Six entries per side; only entry `at` can differ."""
    rtl = [_e(i, [_w()]) for i in range(N)]
    iss = [_e(i, [_w()]) for i in range(N)]
    rtl[at] = _e(at, rtl_w)
    iss[at] = _e(at, iss_w)
    return rtl, iss


# name -> (rtl, iss, expected_to_fail)  -- expectation from the schema.
CASES = {
    "equal": (*_pair([_w()], [_w()]), False),
    "store_address_differs": (*_pair([_w(addr="0x80002000")], [_w()]), True),
    "store_size_differs": (*_pair([_w(size=2)], [_w()]), True),
    "store_data_differs": (*_pair([_w(data="0xbadbad00")], [_w()]), True),
    "missing_store_rtl": (*_pair([], [_w()]), True),
    "missing_store_iss": (*_pair([_w()], []), True),
    "one_sided_key": (
        [_e(i, [_w()]) for i in range(N)],
        [_e(i, [_w()]) if i != 3 else _e(i) for i in range(N)], True),
    "hex_vs_int_same_value": (
        *_pair([_w(addr=0x80001000, data=42)], [_w()]), False),
}

#: case -> (issue, which tool is wrong).  Empty unless a real divergence is found.
KNOWN_DIVERGENCE = {}


def _agent_d_fails(tmp_path, name, rtl, iss):
    paths = []
    for tag, log in (("rtl", rtl), ("iss", iss)):
        p = tmp_path / f"{name}_{tag}.jsonl"
        p.write_bytes(("\n".join(json.dumps(e) for e in log) + "\n").encode("utf-8"))
        paths.append(str(p))
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"), *paths],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode in (0, 1, 3), f"AGENT_D could not run: rc={r.returncode} {out[-600:]}"
    if r.returncode == 3:
        # exit 3 is "could not compare" (#164): a field only one side reports.
        # It must say so; a configuration error would not be a store case.
        assert "could not compare: field_not_reported" in out, out[-800:]
        return True
    # rc 1 must be a store finding, not some other reason to exit 1
    assert (r.returncode == 1) == ("MEMMISMATCH" in out), (
        f"AGENT_D rc={r.returncode} does not match its own report:\n{out[-1200:]}")
    return r.returncode == 1


@pytest.mark.parametrize("name", sorted(CASES))
def test_orchestrator_and_agent_d_agree(tmp_path, name):
    rtl, iss, expect_fail = CASES[name]
    # A field one side did not report is "not clean": a finding, or since #164 a
    # field_not_reported reason that makes the run incomplete. Either way it is
    # not a pass; only a silent skip would be.
    bugs, report = CommitLogComparator().compare_with_report(rtl, iss)
    ava = bool(bugs) or bool(report["not_reported"])
    agent_d = _agent_d_fails(tmp_path, name, rtl, iss)
    if name in KNOWN_DIVERGENCE:
        assert ava != agent_d, (
            f"{name} no longer diverges; remove it from KNOWN_DIVERGENCE "
            f"({KNOWN_DIVERGENCE[name]})")
        assert ava == expect_fail, f"{name}: the orchestrator must still be right"
        return
    assert agent_d == expect_fail, f"AGENT_D disagrees with the schema on {name}"
    assert ava == expect_fail, (
        f"{name}: expected fail={expect_fail}, orchestrator fail={ava}, AGENT_D fail={agent_d}")


def test_the_pairs_are_not_vacuous():
    """At least one pair must fail and one must pass, or agreement means nothing."""
    outcomes = {f for *_, f in CASES.values()}
    assert outcomes == {True, False}


# ── program end: a declared halt write (#178, #180) ──────────────────────────

VAL = REPO_ROOT / "validation" / "ibex"
#: What validation/ibex/compare.py skips: the fields an Ibex trace cannot report.
IBEX_SKIPS = ("--skip-fields", "privilege", "csr_writes")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ibex_pair(prog):
    ibex = _load("differential_trace_to_commitlog", VAL / "trace_to_commitlog.py")
    spike = _load("differential_spike_to_commitlog", VAL / "spike_to_commitlog.py")
    fix = VAL / "fixtures"
    return (ibex.convert_file(str(fix / f"{prog}.ibex.log")),
            spike.convert((fix / f"{prog}.spike.log").read_text(encoding="utf-8"),
                          fix / f"{prog}.elf"))


def _renumbered(log):
    for n, e in enumerate(log):
        e["seq"] = n
    return log


def _halt_cases():
    """name -> (rtl, iss, declaration, expected_to_fail); expectations from #178."""
    rtl, iss = _ibex_pair("loadstore")
    n = len(rtl)
    addr = rtl[-1]["mem_writes"][-1]["addr"]
    ok = {"reason": "halt_write", "addr": addr}

    def logs():
        return copy.deepcopy(rtl), copy.deepcopy(iss)

    def with_regs_flipped():
        r, i = logs()
        k = next(m for m, e in enumerate(r) if e["regs"])
        name = next(iter(r[k]["regs"]))
        r[k]["regs"][name] = f"0x{int(r[k]['regs'][name], 16) ^ 0x10:08x}"
        return r, i

    def extra_instruction():
        r, i = logs()
        extra = {"seq": n, "pc": "0x00100ff0", "instr": "0x00000013", "regs": {}}
        return r, _renumbered(i[:n] + [extra] + i[n:])

    def instruction_after_loop():
        r, i = logs()
        loop = copy.deepcopy(i[n])
        after = {"seq": n + 1, "pc": f"0x{int(loop['pc'], 16) + 4:08x}",
                 "instr": "0x00000013", "regs": {}}
        return _renumbered(r + [loop, after]), i

    def ran_into_the_loop():
        r, i = logs()
        return _renumbered(r + [copy.deepcopy(i[n + k]) for k in range(3)]), i

    def no_terminal_loop():
        r, i = logs()
        tail = [{"seq": n + k, "pc": f"0x{0x00100f00 + 4 * k:08x}", "instr": "0x00000013",
                 "regs": {}} for k in range(2 * n)]
        return r, _renumbered(i[:n] + tail)

    cases = {
        "declared_halt": (*logs(), ok, False),
        "no_declaration": (*logs(), None, True),
        "wrong_address": (*logs(), {"reason": "halt_write", "addr": f"0x{int(addr, 16) + 8:08x}"}, True),
        "wrong_reason": (*logs(), {"reason": "timeout", "addr": addr}, True),
        "not_a_mapping": (*logs(), "halt_write", True),
        "no_address": (*logs(), {"reason": "halt_write"}, True),
        "stops_one_short": (rtl[:-1], iss, ok, True),
        "stops_mid_program": (rtl[:n // 2], iss, ok, True),
        "both_cut_at_halt": (rtl, iss[:n], ok, False),
        "both_cut_one_short": (rtl[:-1], iss[:n - 1], ok, True),
        "extra_instruction_before_loop": (*extra_instruction(), ok, True),
        "instruction_after_loop": (*instruction_after_loop(), ok, True),
        "ran_into_the_loop": (*ran_into_the_loop(), ok, True),
        "remainder_is_not_a_loop": (*no_terminal_loop(), ok, True),
        "divergence_before_the_halt": (*with_regs_flipped(), ok, True),
    }
    return {k: (copy.deepcopy(r), copy.deepcopy(i), d, f) for k, (r, i, d, f) in cases.items()}


HALT_CASES = _halt_cases()

#: case -> (issue, which tool is wrong).  Empty unless a real divergence is found.
KNOWN_HALT_DIVERGENCE = {}


def _orchestrator_fails_on_a_halt(tmp_path, rtl, iss, declaration):
    rtl_results = {"commit_log": copy.deepcopy(rtl), "instructions": len(rtl)}
    if declaration is not None:
        rtl_results["rtl_ended"] = copy.deepcopy(declaration)
    sim = SpikeISS(timeout=30, run_dir=tmp_path / "orch")
    comparison = asyncio.run(sim._compare_results(
        rtl_results, {"commit_log": copy.deepcopy(iss), "instructions": len(iss)}))
    return bool(comparison["bugs"])


def _agent_d_fails_on_a_halt(tmp_path, rtl, iss, declaration):
    paths = []
    for tag, log in (("rtl", rtl), ("iss", iss)):
        p = tmp_path / f"{tag}.jsonl"
        p.write_bytes(("\n".join(json.dumps(e) for e in log) + "\n").encode("utf-8"))
        paths.append(str(p))
    cmd = [sys.executable, str(REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"), *paths,
           "--quiet", *IBEX_SKIPS]
    if declaration is not None:
        cmd += ["--rtl-ended", json.dumps(declaration)]
    r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode in (0, 1), (
        f"AGENT_D rc={r.returncode} (3 would be 'could not compare'): {(r.stdout + r.stderr)[-600:]}")
    return r.returncode == 1


@pytest.mark.parametrize("name", sorted(HALT_CASES))
def test_orchestrator_and_agent_d_agree_on_a_declared_halt(tmp_path, name):
    rtl, iss, declaration, expect_fail = HALT_CASES[name]
    ava = _orchestrator_fails_on_a_halt(tmp_path, rtl, iss, declaration)
    agent_d = _agent_d_fails_on_a_halt(tmp_path, rtl, iss, declaration)
    if name in KNOWN_HALT_DIVERGENCE:
        assert ava != agent_d, (
            f"{name} no longer diverges; remove it from KNOWN_HALT_DIVERGENCE "
            f"({KNOWN_HALT_DIVERGENCE[name]})")
        return
    assert ava == expect_fail, f"{name}: the orchestrator expected fail={expect_fail}, got {ava}"
    assert agent_d == expect_fail, f"{name}: AGENT_D expected fail={expect_fail}, got {agent_d}"


def test_the_halt_cases_are_not_vacuous():
    """Both outcomes occur, and the accepted early end is the case that matters."""
    outcomes = {f for *_, f in HALT_CASES.values()}
    assert outcomes == {True, False}
    assert len(HALT_CASES) >= 15, "cases were deleted"
    rtl, iss, declaration, expect_fail = HALT_CASES["declared_halt"]
    assert len(rtl) < len(iss) and declaration is not None and not expect_fail


# ── fields_reported: the log header (#177) ────────────────────────────────────
#
# Headered variants of the real Ibex and Spike logs (tests/fields_reported_cases.py),
# each with an outcome written from docs/dut_commit_log.md section 4.  Both tools
# must reach that outcome.  The exit codes are the same except for one
# outcome, which is mapped here and not hidden:
#
#   outcome      orchestrator   AGENT_D
#   pass         0              0
#   finding      1              1
#   incomplete   3              3      field_not_reported:<field>:<side>
#   malformed    3              2      a declared log that breaks its header
#
# AGENT_D exits 2 for any input it cannot read.  The orchestrator's convention
# is that anything it cannot verify is incomplete, 3.  Neither is a pass and
# neither is a finding.

_fc_spec = importlib.util.spec_from_file_location(
    "fields_reported_cases", REPO_ROOT / "tests" / "fields_reported_cases.py")
fc = importlib.util.module_from_spec(_fc_spec)
sys.modules["fields_reported_cases"] = fc
_fc_spec.loader.exec_module(fc)
FIELDS_CASES = fc.build_cases()

#: case -> (issue, which tool is wrong).  Empty unless a real divergence is found.
KNOWN_FIELDS_DIVERGENCE = {}


def _orchestrator_exit(tmp_path, case):
    from ava_patched import exit_code_for_status, verdict_from_reports, with_unobserved
    rtl_results = {"commit_log": copy.deepcopy(case.rtl), "instructions": len(case.rtl)}
    if case.rtl_ended is not None:
        rtl_results["rtl_ended"] = copy.deepcopy(case.rtl_ended)
    sim = SpikeISS(timeout=30, run_dir=tmp_path / "orch")
    c = asyncio.run(sim._compare_results(
        rtl_results, {"commit_log": copy.deepcopy(case.iss), "instructions": len(case.iss)}))
    status = verdict_from_reports(with_unobserved({}, list(c.get("unobserved", []))), c["bugs"])
    return exit_code_for_status(status), c


def _agent_d_exit(tmp_path, case):
    paths = []
    for tag, log in (("rtl", case.rtl), ("iss", case.iss)):
        p = tmp_path / f"fr_{tag}.jsonl"
        p.write_bytes("".join(json.dumps(e) + "\n" for e in log).encode("utf-8"))
        paths.append(str(p))
    cmd = [sys.executable, str(REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"), *paths,
           *case.agent_d_skips]
    if case.rtl_ended is not None:
        cmd += ["--rtl-ended", json.dumps(case.rtl_ended)]
    r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


@pytest.mark.parametrize("name", sorted(FIELDS_CASES))
def test_orchestrator_and_agent_d_agree_on_fields_reported(tmp_path, name):
    case = FIELDS_CASES[name]
    want_orch, want_d = fc.EXPECTED_EXIT[case.expect]
    orch, c = _orchestrator_exit(tmp_path, case)
    agent_d, out = _agent_d_exit(tmp_path, case)
    if name in KNOWN_FIELDS_DIVERGENCE:
        assert (orch, agent_d) != (want_orch, want_d), (
            f"{name} no longer diverges; remove it from KNOWN_FIELDS_DIVERGENCE "
            f"({KNOWN_FIELDS_DIVERGENCE[name]})")
        return
    assert agent_d == want_d, f"{name}: AGENT_D expected {want_d}, got {agent_d}:\n{out[-800:]}"
    assert orch == want_orch, (
        f"{name}: orchestrator expected {want_orch}, got {orch}; "
        f"bugs={[b['description'][:120] for b in c['bugs']]} unobserved={c.get('unobserved')}")
    # Same reason on both sides, not just the same exit code.
    if case.expect == "incomplete":
        assert case.reason in out and case.reason in c["unobserved"], (name, c["unobserved"])
    if case.expect == "malformed":
        side, field = case.reason.split(":")[1], case.reason.split(":")[3]
        assert "malformed log" in out and f"[{side.upper()}]" in out, out[-800:]
        assert field == "header" or f"'{field}'" in out, out[-800:]
        assert case.reason in c["unobserved"] and c["bugs"] == [], (name, c["unobserved"])


def test_the_fields_cases_are_not_vacuous():
    assert {c.expect for c in FIELDS_CASES.values()} == set(fc.EXPECTED_EXIT)
    assert len(FIELDS_CASES) >= 29, "cases were deleted"


def _stores_not_compared(tmp_path, rtl, iss, skips):
    """(orchestrator, AGENT_D) count of entries whose stores were not compared."""
    case = fc.Case(rtl, iss, None, "pass", None, skips)
    _, c = _orchestrator_exit(tmp_path, case)
    rep = tmp_path / "nc_report.json"
    paths = []
    for tag, log in (("rtl", rtl), ("iss", iss)):
        p = tmp_path / f"nc_{tag}.jsonl"
        p.write_bytes("".join(json.dumps(e) + "\n" for e in log).encode("utf-8"))
        paths.append(str(p))
    r = subprocess.run([sys.executable, str(REPO_ROOT / "AGENT_D" / "compare_commitlogs.py"),
                        *paths, "--quiet", "--bug-report", str(rep), *skips],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert rep.exists(), (r.stdout + r.stderr)[-800:]
    d = json.loads(rep.read_text(encoding="utf-8"))
    return c["fields_not_compared"].get("stores", 0), d["not_compared"].get("stores", 0)


def test_a_null_both_sides_list_is_compared_as_no_store(tmp_path):
    """#192: under headers that both list mem_writes, null against null is two
    reports of "no store here", compared and equal, so no entry's stores go
    uncompared.  Headerless, the same nulls stay "not reported" by both sides,
    and are counted as not compared, as before."""
    rtl, iss = fc.ibex_pair("loadstore")
    iss = iss[:len(rtl)]                # Ibex stops on the halt store; cut Spike there too
    nulls = sum(1 for a, b in zip(rtl, iss) if a.get("mem_writes") is None
                and b.get("mem_writes") is None)
    assert nulls > 10, "the pair has too few non-stores to say anything"
    both = fc.COMMON + ["trap"]
    # Ibex cannot see traps and Spike has no cycle: supplied as null, as in fc.
    declared = _stores_not_compared(
        tmp_path, fc.declare(fc.with_default(rtl, "trap", None), both),
        fc.declare(fc.with_default(iss, "cycle", None), both), fc.IBEX_SKIPS)
    assert declared == (0, 0), declared
    legacy = _stores_not_compared(tmp_path, rtl, iss, fc.IBEX_SKIPS)
    assert legacy == (nulls, nulls), legacy

"""#10 (part): a per-record handler may not turn a defect into a pass.

Each of these AGENT_H verifiers runs its checker once per commit-log record
inside ``except Exception: logger.warning(...)``. If the checker raises, the
record is skipped, the warning scrolls past, and the verifier can finish and
report a pass over a record it never checked -- a defect in this repository
read as "nothing wrong with the design".

The contract, from the issue and not from the handlers:

* A programming error (TypeError, AttributeError, NameError) propagates.
* An error that says "this input record is malformed" (ValueError, KeyError,
  IndexError) is tolerated, but the record is counted as unchecked and the
  verdict is never a pass: ``records_unchecked`` in the report, ``pass`` False,
  and ``run_from_manifest`` returns 3 ("could not verify", #36), not 0.

Every case first asserts the stubbed checker was actually reached, so a record
that never gets that far fails as a setup error and cannot pass for free.
"""
import importlib
import json

import pytest

# name, module, class, method the handler wraps, how to build the verifier
CASES = [
    ("atomics", "atomics_verifier", "AtomicsVerifier", "_check_amo",
     "log", "amoadd.w x5,x6,(x10)"),
    ("bitmanip", "bitmanip_verifier", "BitmanipVerifier", "_check",
     "log", "clz x5,x6"),
    ("branch_predictor", "branch_predictor_verifier", "BranchPredictorVerifier",
     "_check", "log", "beq x1,x2,8"),
    ("bus", "bus_verifier", "BusVerifier", "_check_txn", "bus", None),
    ("cache", "cache_verifier", "CacheVerifier", "_check_record",
     "cache", "lw x5,0(x6)"),
    ("csr", "csr_verifier", "CSRVerifier", "_check_fencei",
     "log", "csrrw x5,mstatus,x6"),
    ("fp", "fp_verifier", "FPVerifier", "_check", "log", "fadd.s f1,f2,f3"),
    ("peripheral", "peripheral_verifier", "PeripheralVerifier", "check_record",
     "periph", None),
    # group a2
    ("peripheral_finalize", "peripheral_verifier", "PeripheralVerifier",
     "finalize", "periph", None),
    ("pipeline", "pipeline_verifier", "PipelineVerifier", "_classify",
     "log", "addi x1,x1,1"),
    ("privilege", "privilege_verifier", "PrivilegeVerifier", "_check_ret_target",
     "log", "mret"),
    ("rv64_atomics", "rv64_atomics_verifier", "RV64AtomicsVerifier", "_check_amo",
     "log", "amoadd.d x5,x6,(x10)"),
    ("rv64", "rv64_verifier", "RV64Verifier", "_check", "log", "addw x1,x2,x3"),
    ("rvc", "rvc_verifier", "RVCVerifier", "_check_reg_constraint",
     "log", "c.addi x1,1"),
    ("sv_mmu", "sv_mmu_verifier", "SvMMUVerifier", "_check_record",
     "log", "lw x5,0(x6)"),
    ("tlb", "tlb_verifier", "TLBVerifier", "_check_record",
     "log", "lw x5,0(x6)"),
    ("vm", "vm_verifier", "VMVerifier", "_check_record", "log", "lw x5,0(x6)"),
]
IDS = [c[0] for c in CASES]
CACHE_CFG = {"sets": 64, "ways": 4, "line_size": 64, "policy": "lru"}
BUS_TXN = {"protocol": "axi", "channel": "AW", "addr": "0x100", "seq": 0}
RAW = [{"channel": 0, "op": "READ", "src_addr": "0x1000", "length": 64},
       {"channel": 0, "op": "WRITE", "dst_addr": "0x2000", "length": 64},
       {"channel": 0, "op": "DONE"}]


def _rec(disasm):
    return {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
            "insn": "0x4505" if (disasm or "").startswith("c.") else "0x00000013",
            "disasm": disasm or "nop",
            "regs": {"x5": "0x1"}, "csrs": {},
            "reads": [{"addr": "0x100", "size": 4, "value": "0x1"}],
            "writes": [{"addr": "0x100", "size": 4, "value": "0x2"}],
            "bus": BUS_TXN}


def _build(case, extra=None, only=None):
    _, modname, clsname, _, kind, disasm = case
    mod = importlib.import_module(f"AGENT_H.{modname}")
    cls = getattr(mod, clsname)
    rec = _rec(disasm)
    recs = [only] if only is not None else [rec] + ([extra] if extra is not None else [])
    if kind == "log":
        return mod, cls, lambda: cls(recs, recs)
    if kind == "cache":
        return mod, cls, lambda: cls(recs, recs, config=CACHE_CFG)
    if kind == "bus":
        return mod, cls, lambda: cls([BUS_TXN])
    return mod, cls, lambda: cls(RAW, "dma")


def _patch(monkeypatch, case, mod, cls, make, exc):
    """Make the wrapped checker raise `exc`; returns a call counter."""
    target = cls
    if case[4] == "periph":
        target = type(make().checker)        # the per-DUT protocol checker
    calls = {"n": 0}

    def raiser(self, *a, **k):
        calls["n"] += 1
        raise exc("injected by test")

    monkeypatch.setattr(target, case[3], raiser)
    return calls


@pytest.mark.parametrize("exc", [TypeError, AttributeError, NameError])
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_programming_errors_propagate(case, exc, monkeypatch):
    mod, cls, make = _build(case)
    calls = _patch(monkeypatch, case, mod, cls, make, exc)
    verifier = make()
    swallowed = None
    try:
        verifier.run()
    except exc:
        swallowed = False
    else:
        swallowed = True
    assert calls["n"] >= 1, (
        f"setup: {case[0]}'s checker {case[3]} was never reached, so this case "
        f"proves nothing")
    assert not swallowed, (
        f"AGENT_H/{case[1]}.py: a {exc.__name__} from the checker was swallowed "
        f"by the per-record handler; the verifier went on to report a result")


@pytest.mark.parametrize("exc", [ValueError, KeyError, IndexError])
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_malformed_record_is_counted_and_never_a_pass(case, exc, monkeypatch):
    mod, cls, make = _build(case)
    calls = _patch(monkeypatch, case, mod, cls, make, exc)
    report = make().run()
    assert calls["n"] >= 1, (
        f"setup: {case[0]}'s checker {case[3]} was never reached, so this case "
        f"proves nothing")
    assert report.get("records_unchecked") == calls["n"], (
        f"AGENT_H/{case[1]}.py: a record the checker could not process was not "
        f"counted as unchecked (records_unchecked={report.get('records_unchecked')!r}, "
        f"checker failures={calls['n']})")
    assert report["pass"] is False, (
        f"AGENT_H/{case[1]}.py: reported a pass over a record it never checked")


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_run_from_manifest_says_could_not_verify(case, tmp_path, monkeypatch):
    mod, cls, make = _build(case)
    _patch(monkeypatch, case, mod, cls, make, ValueError)
    rec = _rec(case[5])
    (tmp_path / "rtl_commit.jsonl").write_text(json.dumps(rec) + "\n")
    (tmp_path / "iss_commit.jsonl").write_text(json.dumps(rec) + "\n")
    (tmp_path / "raw_rtl.jsonl").write_text(
        "\n".join(json.dumps(r) for r in RAW) + "\n")
    m = tmp_path / "run_manifest.json"
    m.write_text(json.dumps({
        "schema_version": "2.1.0", "run_id": "r10", "run_dir": str(tmp_path),
        "agent_config": {"dut_class": "dma"}, "cache_config": CACHE_CFG}))
    rc = mod.run_from_manifest(m)
    assert rc == 3, (
        f"AGENT_H/{case[1]}.py: run_from_manifest returned {rc} for a run that "
        f"skipped a record it could not check; 3 means 'could not verify' (#36)")


# --------------------------------------------------------------------------- #
# Type-confused records: a field of the wrong type (an int where a string or   #
# dict is expected) makes the checker raise AttributeError or TypeError. That  #
# is a bad record, not a defect in the checker, so it must be counted as       #
# unchecked and skipped, not crash the run. A well-typed record whose checker  #
# raises AttributeError is a defect and still propagates (the first test above #
# covers it for every case).                                                   #
# The fields are the ones that crash each verifier today when mistyped, found  #
# by feeding it a wrongly typed value per field. bus, rvc and peripheral do    #
# not crash on any, so they have no case here.                                 #
# --------------------------------------------------------------------------- #

CONFUSED = {
    "atomics": [("disasm", 5), ("regs", 5)],
    "bitmanip": [("disasm", 5), ("regs", 5)],
    "branch_predictor": [("disasm", 5), ("regs", 5)],
    "cache": [("mem_reads", 5), ("mem_writes", 5)],
    "csr": [("disasm", 5), ("regs", 5), ("csrs", 5)],
    "fp": [("disasm", 5), ("regs", 5), ("csrs", 5)],
    "pipeline": [("disasm", 5), ("regs", 5), ("perf_counters", 5), ("seq", "zz")],
    "privilege": [("disasm", 5), ("csrs", 5)],
    "rv64_atomics": [("disasm", 5), ("regs", 5)],
    "rv64": [("disasm", 5), ("regs", 5)],
    "sv_mmu": [("csrs", 5)],
    "tlb": [("disasm", 5), ("regs", 5), ("csrs", 5)],
    "vm": [("csrs", 5)],
}
CONFUSED_PARAMS = [(c, f, v) for c in CASES for f, v in CONFUSED.get(c[0], [])]


def test_every_verifier_with_a_confusable_field_has_a_case():
    covered = {c[0] for c, _, _ in CONFUSED_PARAMS}
    assert len(covered) == 13, covered     # floor: the 13 found by the field survey


@pytest.mark.parametrize("case,field,value", CONFUSED_PARAMS,
                         ids=[f"{c[0]}-{f}" for c, f, _ in CONFUSED_PARAMS])
def test_type_confused_record_is_counted_not_crashed(case, field, value):
    # one well-typed record beside the confused one: rv64 only checks a log it
    # recognises as RV64 from well-typed evidence, and the count must be exact
    confused = dict(_rec(case[5]), **{field: value})
    mod, cls, make = _build(case, extra=confused)
    try:
        report = make().run()
    except Exception as exc:
        pytest.fail(f"AGENT_H/{case[1]}.py: {field}={value!r} crashed the run with "
                    f"{type(exc).__name__}: {exc}")
    assert report.get("records_unchecked") == 1, (
        f"AGENT_H/{case[1]}.py: a record with {field}={value!r} was not counted "
        f"as unchecked (records_unchecked={report.get('records_unchecked')!r})")
    assert report["pass"] is False, (
        f"AGENT_H/{case[1]}.py: reported a pass over a record it could not check")


# --------------------------------------------------------------------------- #
# The pre-scan gap. atomics, fp and rv64 scan the whole log in the constructor #
# to pick a mode, and skip a mistyped record there. For rv64 the mode decides  #
# whether the check loop runs at all, so a mistyped record that was the only   #
# evidence left the verifier "not RV64", checked nothing, counted nothing and  #
# returned 0. The log below has no well-typed record to carry the mode.        #
# atomics and fp run their loop whatever the mode, so they are controls.       #
# --------------------------------------------------------------------------- #

PRESCAN = [("rv64", "addw x1,x2,x3", "regs", 5),
           ("atomics", "amoadd.d x5,x6,(x10)", "regs", 5),
           ("fp", "fld f1,0(x2)", "regs", 5)]


@pytest.mark.parametrize("name,disasm,field,value", PRESCAN,
                         ids=[p[0] for p in PRESCAN])
def test_only_evidence_record_mistyped_is_not_a_pass(name, disasm, field, value,
                                                     tmp_path):
    case = next(c for c in CASES if c[0] == name)
    confused = dict(_rec(disasm), **{field: value})
    mod, cls, make = _build(case, only=confused)
    report = make().run()
    assert len(make().rtl_log) == 1, "setup: the confused record must be the whole log"
    if "rv64_detected" in report:
        assert report["rv64_detected"] is False, (
            "setup: mode was detected, so this is not the pre-scan case")
    assert report.get("records_unchecked", 0) >= 1, (
        f"AGENT_H/{case[1]}.py: the only record was mistyped and was not counted "
        f"as unchecked (records_unchecked={report.get('records_unchecked')!r})")
    assert report["pass"] is False, (
        f"AGENT_H/{case[1]}.py: reported a pass over a log it could not check")
    line = json.dumps(confused) + chr(10)
    (tmp_path / "rtl_commit.jsonl").write_text(line)
    (tmp_path / "iss_commit.jsonl").write_text(line)
    m = tmp_path / "run_manifest.json"
    m.write_text(json.dumps({"schema_version": "2.1.0", "run_id": "r10",
                             "run_dir": str(tmp_path)}))
    rc = mod.run_from_manifest(m)
    assert rc == 3, (
        f"AGENT_H/{case[1]}.py: run_from_manifest returned {rc} for a log whose "
        f"only record could not be checked; 3 means 'could not verify' (#36)")

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
]
IDS = [c[0] for c in CASES]
CACHE_CFG = {"sets": 64, "ways": 4, "line_size": 64, "policy": "lru"}
BUS_TXN = {"protocol": "axi", "channel": "AW", "addr": "0x100", "seq": 0}
RAW = [{"channel": 0, "op": "READ", "src_addr": "0x1000", "length": 64},
       {"channel": 0, "op": "DONE"}]


def _rec(disasm):
    return {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
            "insn": "0x00000013", "disasm": disasm or "nop",
            "regs": {"x5": "0x1"}, "csrs": {},
            "reads": [{"addr": "0x100", "size": 4, "value": "0x1"}],
            "writes": [{"addr": "0x100", "size": 4, "value": "0x2"}],
            "bus": BUS_TXN}


def _build(case):
    _, modname, clsname, _, kind, disasm = case
    mod = importlib.import_module(f"AGENT_H.{modname}")
    cls = getattr(mod, clsname)
    rec = _rec(disasm)
    if kind == "log":
        return mod, cls, lambda: cls([rec], [rec])
    if kind == "cache":
        return mod, cls, lambda: cls([rec], [rec], config=CACHE_CFG)
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

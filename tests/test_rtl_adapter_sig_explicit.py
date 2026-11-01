"""#81: an explicit --sig-begin/--sig-end is never replaced by nm.

main() entered the nm branch when *either* side was "auto" and, once nm
resolved both symbols, assigned both, so an explicit address on the other
side was silently swapped for the symbol-table one and the signature was
dumped from the wrong region.

The contract, from the decision on the issue:

* only a side given as "auto" is filled from nm; an explicit side reaches the
  dump call unchanged, whether or not nm has a symbol for it;
* both explicit: nm is not consulted;
* an "auto" side nm cannot resolve still fails with EXIT_TOOL and dumps
  nothing, as in #75.
"""
import importlib

import pytest

ra = importlib.import_module("AGENT_E.run_rtl_adapter")

NM_BEGIN = "80003000 D begin_signature\n"
NM_END = "80003040 D end_signature\n"
NM_BOTH = NM_BEGIN + NM_END + "80000000 T _start\n"


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    """main() on a valid-looking ELF, with nm and the dump call stubbed."""
    elf = tmp_path / "t.elf"
    elf.write_bytes(b"\x7fELF" + b"\0" * 60)
    sig = tmp_path / "out" / "t.sig"
    dumped, nm_calls, nm_out = [], [], {}

    def fake_run_adapter(**kw):
        dumped.append((kw["sig_begin"], kw["sig_end"]))
        return ra.EXIT_PASS

    def fake_nm(*args, **kwargs):
        nm_calls.append(args)
        return nm_out["text"]

    monkeypatch.setattr(ra, "run_adapter", fake_run_adapter)
    monkeypatch.setattr(ra.shutil, "which", lambda c: "/fake/" + c)
    monkeypatch.setattr(ra.subprocess, "check_output", fake_nm)

    def run(begin, end, nm=NM_BOTH):
        nm_out["text"] = nm
        rc = ra.main(["--elf", str(elf), "--sig", str(sig),
                      "--sig-begin", begin, "--sig-end", end])
        return rc, dumped, nm_calls
    return run


@pytest.mark.parametrize("begin, end, expected", [
    ("0x80004000", "auto", ("0x80004000", "0x80003040")),
    ("auto", "0x80004040", ("0x80003000", "0x80004040")),
], ids=["begin-explicit-end-auto", "begin-auto-end-explicit"])
def test_an_explicit_side_is_never_replaced(adapter, begin, end, expected):
    rc, dumped, nm_calls = adapter(begin, end)
    assert nm_calls, "nm was never consulted for the auto side"
    assert rc == ra.EXIT_PASS
    assert dumped == [expected]


@pytest.mark.parametrize("begin, end, nm, expected", [
    ("0x80004000", "auto", NM_END, ("0x80004000", "0x80003040")),
    ("auto", "0x80004040", NM_BEGIN, ("0x80003000", "0x80004040")),
], ids=["begin-explicit-no-begin-symbol", "end-explicit-no-end-symbol"])
def test_the_explicit_sides_symbol_is_not_needed(adapter, begin, end, nm, expected):
    rc, dumped, nm_calls = adapter(begin, end, nm)
    assert nm_calls, "nm was never consulted for the auto side"
    assert rc == ra.EXIT_PASS
    assert dumped == [expected]


def test_both_auto_take_both_from_nm(adapter):
    rc, dumped, nm_calls = adapter("auto", "auto")
    assert nm_calls
    assert rc == ra.EXIT_PASS
    assert dumped == [("0x80003000", "0x80003040")]


def test_both_explicit_do_not_consult_nm(adapter):
    rc, dumped, nm_calls = adapter("0x80004000", "0x80004040")
    assert nm_calls == []
    assert rc == ra.EXIT_PASS
    assert dumped == [("0x80004000", "0x80004040")]


@pytest.mark.parametrize("begin, end, nm", [
    ("0x80004000", "auto", NM_BEGIN),
    ("auto", "0x80004040", NM_END),
], ids=["end-auto-unresolved", "begin-auto-unresolved"])
def test_an_unresolved_auto_side_still_fails(adapter, begin, end, nm):
    rc, dumped, nm_calls = adapter(begin, end, nm)
    assert nm_calls, "nm was never consulted: setup error, not a pass"
    assert rc == ra.EXIT_TOOL
    assert dumped == [], "the dump ran with an unresolved auto side"

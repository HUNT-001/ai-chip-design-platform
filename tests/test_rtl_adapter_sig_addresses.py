"""#75: ``--sig-begin/--sig-end auto`` must not fall back to guessed addresses.

``run_rtl_adapter.main`` resolves ``auto`` addresses from the ELF symbol table
via ``nm``. When they cannot be resolved the old code logged a warning and dumped
the signature from the hard-coded ``0x80002000 .. 0x80002040``, which may lie
outside the real signature region and give a wrong compliance signature.

The contract, from the issue and not from the handler:

* ``auto`` requested and the addresses unresolvable (``nm`` unreadable, exits
  nonzero, times out, or the symbols are absent): ``main`` returns the module's
  tool-error exit code (``EXIT_TOOL``, the code the module already uses for "could
  not run what is needed"), logs an ERROR naming the ELF, writes no signature and
  never reaches the dump call;
* ``auto`` and ``nm`` resolves them: the run proceeds with exactly those;
* explicit addresses: ``nm`` is not consulted and the run proceeds;
* a programming error (TypeError) inside the resolver propagates.
"""
import importlib
import logging
import subprocess

import pytest

ra = importlib.import_module("AGENT_E.run_rtl_adapter")

NM_OK = (
    "80003000 D begin_signature\n"
    "80003040 D end_signature\n"
    "80000000 T _start\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A valid-looking ELF, a fake ``nm`` on PATH, and a stub for the dump call."""
    elf = tmp_path / "t.elf"
    elf.write_bytes(b"\x7fELF" + b"\0" * 60)
    sig = tmp_path / "out" / "t.sig"
    dumped = []
    nm_calls = []

    def fake_run_adapter(**kw):
        dumped.append(kw)
        return ra.EXIT_PASS

    monkeypatch.setattr(ra, "run_adapter", fake_run_adapter)
    monkeypatch.setattr(ra.shutil, "which", lambda c: "/fake/" + c)

    class E:
        pass

    e = E()
    e.elf, e.sig, e.dumped, e.nm_calls = elf, sig, dumped, nm_calls
    e.monkeypatch = monkeypatch

    def nm(result):
        """Make ``nm`` return a string or raise the given exception."""
        def check_output(*a, **k):
            nm_calls.append(a)
            if isinstance(result, BaseException):
                raise result
            return result
        monkeypatch.setattr(ra.subprocess, "check_output", check_output)
    e.nm = nm

    def run(*extra):
        return ra.main(["--elf", str(elf), "--sig", str(sig), *extra])
    e.run = run
    return e


@pytest.mark.parametrize("failure", [
    OSError("nm cannot read the file"),
    subprocess.CalledProcessError(1, "nm"),
    subprocess.TimeoutExpired("nm", 10),
    "80000000 T _start\n",           # nm works, the signature symbols are absent
], ids=["oserror", "nonzero-exit", "timeout", "no-symbols"])
def test_auto_with_unresolvable_addresses_fails_and_dumps_nothing(env, caplog, failure):
    env.nm(failure)
    with caplog.at_level(logging.ERROR, logger=ra.log.name):
        rc = env.run("--sig-begin", "auto", "--sig-end", "auto")
    assert env.nm_calls, "nm was never consulted: setup error, not a pass"
    assert rc == ra.EXIT_TOOL
    assert env.dumped == [], "the dump ran with guessed addresses"
    assert not env.sig.exists()
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(env.elf.name in m for m in errors), errors


def test_auto_with_resolved_addresses_uses_exactly_those(env):
    env.nm(NM_OK)
    rc = env.run("--sig-begin", "auto", "--sig-end", "auto")
    assert rc == ra.EXIT_PASS
    assert len(env.dumped) == 1
    assert env.dumped[0]["sig_begin"] == "0x80003000"
    assert env.dumped[0]["sig_end"] == "0x80003040"


def test_explicit_addresses_do_not_consult_nm(env):
    env.nm(OSError("must not be called"))
    rc = env.run("--sig-begin", "0x80004000", "--sig-end", "0x80004040")
    assert env.nm_calls == []
    assert rc == ra.EXIT_PASS
    assert env.dumped[0]["sig_begin"] == "0x80004000"
    assert env.dumped[0]["sig_end"] == "0x80004040"


def test_programming_error_in_the_resolver_propagates(env):
    env.nm(TypeError("programming error"))
    with pytest.raises(TypeError):
        env.run("--sig-begin", "auto", "--sig-end", "auto")
    assert env.nm_calls, "the stub was never reached: setup error, not a pass"
    assert env.dumped == []

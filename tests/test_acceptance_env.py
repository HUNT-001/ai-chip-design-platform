"""The scrubbed environment acceptance_e2e.py uses for case c.

Case c needs "no tools on PATH".  On Windows a child Python also needs
SYSTEMROOT, or `import asyncio` fails with WinError 10106 before the CLI
runs at all, so case c reports a harness failure instead of the verdict.
Same shape as tests/test_ava_console_encoding.py::_env.
"""
import importlib.util
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_PATH = "/host/real/bin"


def _load():
    spec = importlib.util.spec_from_file_location(
        "acceptance_e2e", REPO_ROOT / "scripts" / "acceptance_e2e.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _case_c_env(monkeypatch, tmp_path):
    """The env case c hands to its child, captured without running it."""
    mod = _load()
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rtl = tmp_path / "demo_core.sv"
    rtl.write_text(mod.RTL, encoding="utf-8")
    mod.run("c", tmp_path, rtl)
    return seen["env"]


def test_case_c_keeps_systemroot_when_the_host_has_it(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    env = _case_c_env(monkeypatch, tmp_path)
    assert env.get("SYSTEMROOT") == r"C:\Windows"


def test_case_c_omits_systemroot_when_the_host_lacks_it(monkeypatch, tmp_path):
    monkeypatch.delenv("SYSTEMROOT", raising=False)
    env = _case_c_env(monkeypatch, tmp_path)
    assert "SYSTEMROOT" not in env


def test_case_c_never_carries_the_host_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", HOST_PATH)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    env = _case_c_env(monkeypatch, tmp_path)
    assert env["PATH"] == "/nonexistent"
    assert HOST_PATH not in "".join(env.values())
    # anti-vacuity: the host really had a PATH to leak
    assert HOST_PATH == __import__("os").environ["PATH"]

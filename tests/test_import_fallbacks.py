"""#10 (part): import fallbacks that hid a broken module (the #41 shape).

Six sites wrapped an import in ``except Exception`` (or ``except Exception: pass``)
and carried on without the module, so a NameError / SyntaxError / TypeError raised
*while importing* a repo module, or a repo module that was simply gone, looked
exactly like "optional feature not installed". #41 was this: a bare import that
always failed from the repo root and was logged away.

The contract, from the issue and not from the handlers:

(a) an optional third-party dependency that is absent degrades the feature, logs
    at WARNING (not DEBUG), and leaves the module's own "unavailable" marker set;
(b) an error raised while importing a repo module (NameError, SyntaxError)
    propagates; and
(c) a missing repo-internal module re-raises: that is a broken install, not an
    optional feature.

(a) applies only where an optional third-party dependency is actually in play
(``_try_import``). The other sites import repo modules that use nothing outside
the standard library, so they have only (b) and (c).

Not covered here, left on purpose: ``AGENT_F/coverage_pipeline.py`` ``ExtDB``
import. Narrowing it to ImportError would make every root run raise, and making
the import resolve changes ``top_cold`` from the inline cold lines to ``[]``; that
is a behaviour decision tracked as its own issue.

The failing input is the only input in every case. Each case also states what the
unblocked call does, so a setup that never reaches the import cannot pass.
"""
import importlib
import importlib.abc
import importlib.machinery
import json
import logging
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOE = os.path.join(ROOT, "voe")

EXC = {"NameError": NameError, "SyntaxError": SyntaxError}
BROKEN = {k: v("raised while importing") for k, v in EXC.items()}


class _RaisingLoader(importlib.abc.Loader):
    def __init__(self, exc):
        self.exc = exc

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise self.exc


class _RaisingFinder(importlib.abc.MetaPathFinder):
    def __init__(self, fullname, exc):
        self.fullname, self.exc = fullname, exc

    def find_spec(self, fullname, path, target=None):
        if fullname == self.fullname:
            return importlib.machinery.ModuleSpec(fullname, _RaisingLoader(self.exc))
        return None


def _forget(monkeypatch, fullname):
    """Drop a module so the next import has to find it again."""
    monkeypatch.delitem(sys.modules, fullname, raising=False)
    parent, _, child = fullname.rpartition(".")
    if parent and parent in sys.modules:
        monkeypatch.delattr(sys.modules[parent], child, raising=False)


def make_missing(monkeypatch, fullname):
    _forget(monkeypatch, fullname)
    monkeypatch.setitem(sys.modules, fullname, None)     # import raises ImportError


def make_broken(monkeypatch, fullname, exc):
    _forget(monkeypatch, fullname)
    monkeypatch.setattr(sys, "meta_path", [_RaisingFinder(fullname, exc)] + sys.meta_path)


# ── site 1: ava_patched._try_import ────────────────────────────────────────────

@pytest.fixture
def ava(monkeypatch):
    import ava_patched
    return ava_patched


def _write_module(tmp_path, monkeypatch, name, body):
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return name


def test_try_import_control_loads_a_good_module(ava, tmp_path, monkeypatch):
    name = _write_module(tmp_path, monkeypatch, "q7_good_mod", "VALUE = 1\n")
    assert ava._try_import(name, "Good").VALUE == 1


def test_try_import_absent_optional_dependency_degrades_and_warns(
        ava, tmp_path, monkeypatch, caplog):
    name = _write_module(tmp_path, monkeypatch, "q7_needs_dep",
                         "import q7_dependency_that_is_not_installed\n")
    with caplog.at_level(logging.DEBUG, logger="ava_patched"):
        mod = ava._try_import(name, "NeedsDep")
    assert mod is None
    warned = [r for r in caplog.records
              if r.levelno >= logging.WARNING and "NeedsDep" in r.getMessage()]
    assert warned, [(r.levelname, r.getMessage()) for r in caplog.records]


@pytest.mark.parametrize("kind", BROKEN)
def test_try_import_error_raised_while_importing_propagates(
        ava, tmp_path, monkeypatch, kind):
    body = {"NameError": "VALUE = q7_undefined_name\n",
            "SyntaxError": "def broken(:\n"}[kind]
    name = _write_module(tmp_path, monkeypatch, f"q7_broken_{kind.lower()}", body)
    with pytest.raises(EXC[kind]):
        ava._try_import(name, "Broken")


def test_try_import_missing_repo_module_reraises(ava, tmp_path, monkeypatch):
    name = _write_module(tmp_path, monkeypatch, "q7_needs_repo_mod",
                         "import AGENT_H.q7_no_such_repo_module\n")
    with pytest.raises(ImportError):
        ava._try_import(name, "NeedsRepoMod")


def test_try_import_missing_target_module_reraises(ava):
    with pytest.raises(ImportError):
        ava._try_import("AGENT_H.q7_no_such_repo_module", "Missing")


# ── site 2: AGENT_B/testbench_generator.run_from_manifest ─────────────────────

@pytest.fixture
def tbgen_manifest(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "AGENT_B"))
    import testbench_generator
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    mp = tmp_path / "run_manifest.json"
    mp.write_text(json.dumps({"run_dir": str(tmp_path), "rtl_dir": str(rtl)}),
                  encoding="utf-8")
    return testbench_generator, str(mp)


def test_testbench_generator_control_runs_when_nothing_is_blocked(tbgen_manifest):
    tbgen, mp = tbgen_manifest
    assert tbgen.run_from_manifest(mp) == 0


@pytest.mark.parametrize("kind", BROKEN)
def test_testbench_generator_error_while_importing_rtl_graph_propagates(
        tbgen_manifest, monkeypatch, kind):
    tbgen, mp = tbgen_manifest
    make_broken(monkeypatch, "AGENT_H.rtl_graph", BROKEN[kind])
    with pytest.raises(EXC[kind]):
        tbgen.run_from_manifest(mp)


def test_testbench_generator_missing_rtl_graph_reraises(tbgen_manifest, monkeypatch):
    tbgen, mp = tbgen_manifest
    make_missing(monkeypatch, "AGENT_H.rtl_graph")
    with pytest.raises(ImportError):
        tbgen.run_from_manifest(mp)


# ── sites 3-4: voe/obligation_state.probe_structure (two imports of rtl_graph) ─

@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(VOE)
    obligation_state = importlib.import_module("obligation_state")
    rtl = tmp_path / "d.sv"
    rtl.write_text("module d(input clk, input a, output reg q);\n"
                   "  always @(posedge clk) q <= a;\nendmodule\n", encoding="utf-8")
    return obligation_state.probe_structure, str(rtl)


def test_probe_structure_control_runs_when_nothing_is_blocked(probe):
    fn, rtl = probe
    assert fn(rtl)[1] is True            # sequential


@pytest.mark.parametrize("kind", BROKEN)
def test_probe_structure_error_while_importing_rtl_graph_propagates(
        probe, monkeypatch, kind):
    fn, rtl = probe
    make_broken(monkeypatch, "AGENT_H.rtl_graph", BROKEN[kind])
    with pytest.raises(EXC[kind]):
        fn(rtl)


def test_probe_structure_missing_rtl_graph_reraises(probe, monkeypatch):
    fn, rtl = probe
    make_missing(monkeypatch, "AGENT_H.rtl_graph")
    with pytest.raises(ImportError):
        fn(rtl)


# ── site 5: voe/workers.Worker.execute imports obligation_state ───────────────

@pytest.fixture
def static_worker(monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "phase3"))
    monkeypatch.syspath_prepend(VOE)
    workers = importlib.import_module("workers")
    importlib.import_module("obligation_state")
    learned = []

    class Static:
        def check(self, _name):
            return types.SimpleNamespace(status="proved", witness="")

        def rtl_for(self, _phi):
            return "unused.sv"

    class Board:
        def get(self, _phi):
            return types.SimpleNamespace(kind="invariant")   # not structural

    w = workers.Worker("w", "skeptic", kern=None, formal=None, sim=None,
                       static=Static())
    w.learn_structure = lambda *a: learned.append(a)
    ks = types.SimpleNamespace(n_eff=lambda phi: 0)
    return (lambda: w.execute(ks, Board(), "phi", "static")), learned


def test_worker_execute_control_reaches_learn_structure(static_worker, monkeypatch):
    run, learned = static_worker
    import obligation_state
    monkeypatch.setattr(obligation_state, "probe_structure",
                        lambda rtl: (False, True, "small"))
    run()
    assert learned == [("phi", False, True, "small")]


@pytest.mark.parametrize("kind", BROKEN)
def test_worker_execute_error_while_importing_obligation_state_propagates(
        static_worker, monkeypatch, kind):
    run, _ = static_worker
    make_broken(monkeypatch, "obligation_state", BROKEN[kind])
    with pytest.raises(EXC[kind]):
        run()


def test_worker_execute_missing_obligation_state_reraises(static_worker, monkeypatch):
    run, _ = static_worker
    make_missing(monkeypatch, "obligation_state")
    with pytest.raises(ImportError):
        run()

"""#32: an AGENT_H verifier must not erase a manifest update made while it ran.

Every verifier's ``run_from_manifest`` read the manifest up front, ran the whole
verifier, then wrote its in-memory copy back. Any update another agent landed
through the locked ``atomic_update_manifest`` in between was discarded, and the
verifier still returned 0. The window is the whole verifier run.

The contract written here comes from the issue, not from the code under test:
after ``run_from_manifest`` returns, ``phases.compare.status`` written by a
concurrent writer is still there, and the verifier's own keys are there too.

Two kinds of case, because either alone can pass while proving nothing:

* ``test_issue_repro_*`` runs the issue's repro in a subprocess with a bare
  ``PYTHONPATH``, so it covers the verifiers loaded by file path (script
  mode), not just the ``AGENT_H.<name>`` package import pytest resolves.
* ``test_concurrent_update_survives`` runs it for every verifier that writes
  the manifest back. The set is discovered by scanning the AST, never listed
  by name, with a floor so broken discovery cannot pass vacuously. Each case
  also asserts the verifier really wrote its own keys: a verifier that skipped
  early never reached its write-back and has not been tested at all.
"""
import ast
import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_H_DIR = REPO_ROOT / "AGENT_H"
EXPECTED_AT_LEAST = 26      # the count in #32; a floor, not an exact list


def _writes_back_manifest(path):
    """True if the file's run_from_manifest mutates the manifest it loaded."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "run_from_manifest"):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "manifest"
                    and node.func.attr == "setdefault"):
                return True
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                            and t.value.id == "manifest"):
                        return True
    return False


WRITERS = sorted(p.stem for p in AGENT_H_DIR.glob("*.py") if _writes_back_manifest(p))


def test_discovery_found_the_writers():
    assert len(WRITERS) >= EXPECTED_AT_LEAST, (
        f"only {len(WRITERS)} AGENT_H modules write the manifest back; #32 counted "
        f"{EXPECTED_AT_LEAST}. Either one stopped, or discovery is broken.")


# --------------------------------------------------------------------------- #
# The issue's repro, verbatim in substance, in a subprocess.                   #
# --------------------------------------------------------------------------- #

_REPRO = textwrap.dedent('''
    import importlib.util, json, sys, tempfile
    from pathlib import Path
    repo = Path(sys.argv[1]).resolve()
    def load(name, rel):
        spec = importlib.util.spec_from_file_location(name, repo / rel)
        mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
        sys.path.insert(0, str((repo / rel).parent)); spec.loader.exec_module(mod); return mod
    cache = load("cv", "AGENT_H/cache_verifier.py")
    cmp_  = load("cc", "AGENT_D/compare_commitlogs.py")

    run = Path(tempfile.mkdtemp())
    rec = {"pc": "0x80000000", "insn": "0x00000013", "rd": 0, "rd_val": "0x0"}
    for n in ("rtl_commit.jsonl", "iss_commit.jsonl"):
        (run / n).write_text(json.dumps(rec) + "\\n")
    m = run / "manifest.json"
    m.write_text(json.dumps({"run_id": "r", "run_dir": str(run)}))

    real = cache.CacheVerifier.run
    def run_with_concurrent_writer(self):
        cmp_.atomic_update_manifest(m, {"phases.compare.status": "passed"})
        return real(self)
    cache.CacheVerifier.run = run_with_concurrent_writer

    rc = cache.run_from_manifest(m)
    after = json.loads(m.read_text())
    print(json.dumps({"rc": rc,
                      "compare": after.get("phases", {}).get("compare"),
                      "own_phase": after.get("phases", {}).get("cache_check"),
                      "own_output": after.get("outputs", {}).get("cache_report")}))
''')


def test_issue_repro_compare_survives_in_script_mode(tmp_path):
    script = tmp_path / "repro32.py"
    script.write_text(_REPRO, encoding="utf-8")
    env = {k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(script), str(REPO_ROOT)],
                          cwd=str(tmp_path), env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["own_phase"] is not None and out["own_output"] is not None, (
        f"the verifier never wrote its own keys, so the case proves nothing: {out}")
    assert out["compare"] == {"status": "passed"}, (
        f"phases.compare was erased by the verifier's write-back: {out}")


# --------------------------------------------------------------------------- #
# Every verifier that writes the manifest back.                                #
# --------------------------------------------------------------------------- #

def _build_run(tmp_path):
    """One run directory rich enough that every writer reaches its write-back.

    The verifiers skip early on inputs they do not recognise, and a skip never
    reaches the write-back, so a generic manifest would let some of them pass
    for free. The superset: a commit log carrying a 64-bit atomic and a bus
    transaction, a raw DUT log, and a bug report.
    """
    base_rec = {"schema_version": "2.1.0", "pc": "0x80000000", "insn": "0x00000013",
                "rd": 0, "rd_val": "0x0", "regs": {}, "csrs": {}}
    recs = [
        dict(base_rec, seq=0, disasm="li x6,0x100000000", regs={"x6": "0x100000000"}),
        dict(base_rec, seq=1, disasm="amoadd.d x5,x6,(x10)",
             regs={"x5": "0x200000000"},
             reads=[{"addr": "0x100", "size": 8, "value": "0x200000000"}],
             writes=[{"addr": "0x100", "size": 8, "value": "0x300000000"}],
             bus={"protocol": "axi", "channel": "AW", "addr": "0x100"}),
    ]
    for name in ("rtl_commit.jsonl", "iss_commit.jsonl"):
        (tmp_path / name).write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    raw = [{"channel": 0, "op": "READ", "src_addr": "0x1000", "length": 64},
           {"channel": 0, "op": "WRITE", "dst_addr": "0x2000", "length": 64},
           {"channel": 0, "op": "DONE"}]
    (tmp_path / "raw_rtl.jsonl").write_text("\n".join(json.dumps(r) for r in raw) + "\n")
    (tmp_path / "bug_report.json").write_text(json.dumps({
        "kind": "register_mismatch", "mismatch_class": "alu_result",
        "severity": "HIGH", "seq": 2, "pc": "0x80000008",
        "disasm": "add x3,x1,x2", "rtl_val": "0x00000004",
        "iss_val": "0x00000003", "description": "ALU result mismatch on ADD"}))
    base = {"schema_version": "2.1.0", "run_id": "r32", "run_dir": str(tmp_path),
            "agent_config": {"dut_class": "dma"}}
    m = tmp_path / "run_manifest.json"
    m.write_text(json.dumps(base))
    return m, base


@pytest.mark.parametrize("name", WRITERS)
def test_concurrent_update_survives(name, tmp_path, monkeypatch):
    import compare_commitlogs as cc          # AGENT_D, on pyproject's pythonpath
    mod = importlib.import_module(f"AGENT_H.{name}")
    m, base = _build_run(tmp_path)

    # The concurrent writer lands right after the verifier reads the manifest,
    # i.e. at the start of the window #32 describes, through the locked path.
    real_load = json.load
    fired = []

    def load_then_write(f, *a, **kw):
        data = real_load(f, *a, **kw)
        if not fired and getattr(f, "name", None) == str(m):
            fired.append(True)
            cc.atomic_update_manifest(m, {"phases.compare.status": "passed"})
        return data

    monkeypatch.setattr(json, "load", load_then_write)
    try:
        mod.run_from_manifest(m)
    finally:
        monkeypatch.setattr(json, "load", real_load)
    assert fired, f"{name} never read the manifest through json.load; the case is blind"

    after = json.loads(m.read_text())
    only_the_writer = json.loads(json.dumps(base))
    only_the_writer.setdefault("phases", {})["compare"] = {"status": "passed"}
    assert after != only_the_writer, (
        f"{name} wrote nothing of its own, so it never reached its write-back "
        f"with this fixture and has not been tested")
    assert after.get("phases", {}).get("compare") == {"status": "passed"}, (
        f"AGENT_H/{name}.py: a locked manifest update made while the verifier "
        f"ran was erased by its write-back (#32). phases={after.get('phases')}")

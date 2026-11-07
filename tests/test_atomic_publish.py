"""#25: temp-file publishes must use os.replace, never rename.

On POSIX rename and replace behave the same, so a behavioural test cannot see the
defect here; on Windows rename raises FileExistsError when the target exists, so
every second write of a manifest fails.  The first test therefore scans the
production sources statically.  The second pins the other half of the issue:
atomic_write must not remove the destination before publishing, or a crash in
the gap destroys the previous version.
"""
import ast
import importlib
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCLUDED_TOP_LEVEL = ("corpus", "_legacy", "tests", ".")

#: Renames that are not a temp-file publish.  Keyed by (file, source text of the
#: call) rather than line number so unrelated edits above do not break it.
NON_PUBLISH_RENAMES = {
    ("AGENT_H/economics_engine.py", "ledger_path.rename(backup)"):
        "moves an unreadable ledger aside to a free '.corrupt' name; the loop "
        "above it guarantees the destination does not exist",
}


def production_files(root=REPO_ROOT):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if rel.parts[0].startswith(EXCLUDED_TOP_LEVEL):
            continue
        if p.name.startswith("test_") or p.name == "conftest.py":
            continue
        yield p


def rename_calls(source):
    """(lineno, source text) of every `<expr>.rename(...)` call in *source*."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "rename"):
            out.append((node.lineno, ast.unparse(node)))
    return sorted(out)


def test_scanner_flags_a_rename_and_ignores_replace():
    # Known positive and negative for the scan itself.
    assert rename_calls("tmp.rename(path)\nos.rename(a, b)") == [
        (1, "tmp.rename(path)"), (2, "os.rename(a, b)")]
    assert rename_calls("tmp.replace(path)\nos.replace(a, b)") == []


def test_no_temp_publish_uses_rename():
    files = list(production_files())
    assert len(files) > 100, "scan found almost no files; the sweep is looking at nothing"

    leftovers, seen_allowed = [], set()
    for p in files:
        rel = p.relative_to(REPO_ROOT).as_posix()
        for lineno, text in rename_calls(p.read_text(encoding="utf-8")):
            if (rel, text) in NON_PUBLISH_RENAMES:
                seen_allowed.add((rel, text))
            else:
                leftovers.append(f"{rel}:{lineno}: {text}")

    stale = set(NON_PUBLISH_RENAMES) - seen_allowed
    assert not stale, f"allowlist entries that no longer exist: {sorted(stale)}"
    assert not leftovers, "temp-file publish via rename (use os.replace):\n" + "\n".join(leftovers)


def test_atomic_write_never_removes_the_destination(tmp_path, monkeypatch):
    cc = importlib.import_module("AGENT_D.compare_commitlogs")
    dest = tmp_path / "run_manifest.json"
    dest.write_text("old", encoding="utf-8")

    removed = []

    def refuse(*args, **kwargs):
        removed.append(args)
        raise OSError("the destination must not be removed before it is replaced")

    monkeypatch.setattr(pathlib.Path, "unlink", refuse)
    monkeypatch.setattr(cc.os, "remove", refuse)
    monkeypatch.setattr(cc.os, "unlink", refuse)
    # The unlink-first branch is Windows-only; force it so POSIX exercises it too.
    monkeypatch.setattr(sys, "platform", "win32")

    cc.atomic_write(dest, "new")

    monkeypatch.undo()
    assert not removed, f"atomic_write tried to remove files first: {removed}"
    assert dest.read_text(encoding="utf-8") == "new"

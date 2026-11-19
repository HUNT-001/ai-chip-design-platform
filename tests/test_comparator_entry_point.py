"""Production code must call CommitLogComparator.compare_with_report, never compare (#164).

`compare` returns only the bugs. It drops `not_reported`, the list of fields
exactly one side reported, so a verdict built on it passes a run that could not
verify those fields. This holds every non-test caller to the entry point that
keeps the list. Receivers are matched by shape (the class, an instance of it,
or the orchestrator's `_comparator` attribute) so AGENT_D's unrelated
`_FieldComparator.compare` is not flagged.
"""
import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _is_comparator_receiver(node):
    if isinstance(node, ast.Name):
        return node.id == "CommitLogComparator"
    if isinstance(node, ast.Call):
        return _is_comparator_receiver(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr in ("CommitLogComparator", "_comparator")
    return False


def compare_calls(source):
    """Line numbers of `<CommitLogComparator-ish>.compare(...)` calls in `source`."""
    return [n.lineno for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "compare" and _is_comparator_receiver(n.func.value)]


def _production_files():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout.split()
    return [f for f in out
            if not f.startswith(("tests/", "corpus/", "_legacy/"))
            and not Path(f).name.startswith("test_")]


def test_the_scanner_sees_each_way_of_calling_the_wrapper():
    """Anti-vacuity: the shapes this test exists to catch are caught, and the
    unrelated AGENT_D comparator is not."""
    assert compare_calls("CommitLogComparator().compare(a, b)") == [1]
    assert compare_calls("CommitLogComparator.compare(c, a, b)") == [1]
    assert compare_calls("ava_patched.CommitLogComparator().compare(a, b)") == [1]
    assert compare_calls("self._comparator.compare(a, b)") == [1]
    assert compare_calls("lambda: self._comparator.compare(a, b)") == [1]
    assert compare_calls("self._comparator.compare_with_report(a, b)") == []
    assert compare_calls("_FieldComparator(cfg).compare(a, b)") == []


def test_no_production_file_calls_the_wrapper_that_drops_not_reported():
    files = _production_files()
    assert len(files) > 100 and "ava_patched.py" in files, "file discovery is broken"
    offenders = {}
    for f in files:
        lines = compare_calls((ROOT / f).read_text(encoding="utf-8", errors="replace"))
        if lines:
            offenders[f] = lines
    assert not offenders, (
        f"call compare_with_report so not_reported reaches the verdict: {offenders}")

"""Console output of the AVA agents must be encodable in the Windows console
code page (cp1252).

On Windows, ``print`` of a character cp1252 cannot encode raises
UnicodeEncodeError and kills the run after the work is done.  The em dash and
the bullet are in cp1252 and are fine; box-drawing characters, block elements,
arrows, check marks and the like are not.

This scans the AVA production sources (AGENT_*, ava_patched.py, ava.py) with
``ast``.  A string constant counts if it is an argument of a ``print`` /
``sys.stdout.write`` / ``sys.stderr.write`` call, or if it is assigned (earlier
in the same function) to a name that call uses.  Logging calls are out of scope:
the logging module reports an encode error on stderr and carries on.

voe*/ and docs/ scripts are research tooling, not AVA, and are not scanned.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPT_TOP_LEVEL = ("voe", "docs", "phase3", "corpus", "_legacy", "tests")
OUTPUT_CALLS = {"print", "sys.stdout.write", "sys.stderr.write"}


def _unencodable(s):
    out = set()
    for ch in s:
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            out.add(ch)
    return out


def scan_source(source):
    """Return [(lineno, sorted unencodable chars)] for output calls in source."""
    tree = ast.parse(source)
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def scope_of(node):
        best = tree
        for f in funcs:
            if f.lineno <= node.lineno <= f.end_lineno and (
                    best is tree or f.lineno >= best.lineno):
                best = f
        return best

    hits = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or ast.unparse(call.func) not in OUTPUT_CALLS:
            continue
        nodes = list(ast.walk(call))
        names = {n.id for n in nodes if isinstance(n, ast.Name)}
        if names:
            for a in ast.walk(scope_of(call)):
                if not isinstance(a, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    continue
                if a.lineno > call.lineno or a.value is None:
                    continue
                targets = a.targets if isinstance(a, ast.Assign) else [a.target]
                if any(isinstance(x, ast.Name) and x.id in names
                       for t in targets for x in ast.walk(t)):
                    nodes += list(ast.walk(a.value))
        chars = set()
        for n in nodes:
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                chars |= _unencodable(n.value)
        if chars:
            hits.append((call.lineno, sorted(chars)))
    return sorted(hits)


def ava_sources():
    files = [p for p in ROOT.glob("AGENT_*/**/*.py")]
    files += [ROOT / "ava_patched.py", ROOT / "ava.py"]
    out = []
    for p in files:
        if not p.exists() or p.name.startswith("test_"):
            continue
        rel = p.relative_to(ROOT)
        if rel.parts[0].startswith(EXEMPT_TOP_LEVEL) and not rel.parts[0].startswith("AGENT_"):
            continue
        out.append(p)
    return sorted(out)


def test_scanner_flags_a_known_positive():
    src = 'def f():\n    bar = "\u2588" * 3\n    print(bar)\n    print("\u2500" * 70)\n'
    assert scan_source(src) == [(3, ["\u2588"]), (4, ["\u2500"])]


def test_scanner_passes_a_known_negative():
    # Em dash, bullet, accented letters and plain ASCII are all in cp1252.
    src = 'print("a \u2014 b \u2022 c \u00e9")\nprint("-" * 70)\n'
    assert scan_source(src) == []


def test_scanner_ignores_logging_and_non_output_strings():
    src = 'log.info("\u2500")\nx = "\u2713"\nprint("ok")\n'
    assert scan_source(src) == []


def test_scan_actually_covers_the_agents():
    names = {p.name for p in ava_sources()}
    assert len(names) >= 50, len(names)
    assert {"ava_patched.py", "coverage_pipeline.py", "run_iss.py"} <= names


def test_ava_console_output_is_cp1252_encodable():
    problems = []
    for p in ava_sources():
        for lineno, chars in scan_source(p.read_text(encoding="utf-8")):
            shown = " ".join("U+%04X" % ord(c) for c in chars)
            problems.append("%s:%d  %s" % (p.relative_to(ROOT).as_posix(), lineno, shown))
    assert not problems, (
        "print()/write() output a character cp1252 cannot encode "
        "(UnicodeEncodeError on a default Windows console):\n  "
        + "\n  ".join(problems))

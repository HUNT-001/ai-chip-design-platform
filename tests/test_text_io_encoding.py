"""#24 (tranche 1): production code must not depend on the locale's default
text encoding.

``Path.read_text()`` / ``write_text()`` and text-mode ``open()`` with no
``encoding=`` use ``locale.getpreferredencoding()``: UTF-8 on the Linux CI
runners, cp1252 on a default Windows install.  The repo's data files are UTF-8,
so the same call decodes them differently on the two platforms.

Scope of this tranche: tracked production ``.py`` files.  Out of scope, each
for a later tranche or by design: ``tests/`` and ``test_*.py``, ``_legacy*/``,
``corpus*/``, binary-mode opens, and ``subprocess(..., text=True)``.

The scan is an AST walk, not a regex, so a multi-line call or a string that
merely contains ``open(`` is judged on its structure.
"""
import ast
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories that are intentionally not scanned: dead code and vendored sources.
_EXCLUDED_DIRS = ("_legacy", "corpus")

# Intentional exceptions, keyed by (repo-relative path, ast.unparse of the call)
# -> (number of such calls in that file, reason).  Keyed on call text, not line
# number, so unrelated edits do not invalidate it; the count means a new
# identical call in the same file is NOT silently covered.
#
# Empty: every tracked .py file outside the excluded directories, tests
# included, must name its encoding.
ALLOWLIST = {}


def _is_production(rel: str) -> bool:
    # Historical name: the scan now covers tests too (#24 final).
    return not rel.split("/")[0].startswith(_EXCLUDED_DIRS)


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _has_star_kwargs(call: ast.Call) -> bool:
    return any(k.arg is None for k in call.keywords)


def _open_mode(call: ast.Call):
    """Return the mode node of a builtin open() call, or None if absent."""
    if len(call.args) >= 2:
        return call.args[1]
    for k in call.keywords:
        if k.arg == "mode":
            return k.value
    return None


def find_unencoded_text_io(source: str):
    """Yield (lineno, call_text) for each text-mode file call lacking encoding=."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if _has_kw(node, "encoding") or _has_star_kwargs(node):
            continue
        # Path.read_text() / Path.write_text(); encoding is the first
        # positional argument of read_text, the second of write_text.
        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            enc_pos = 0 if func.attr == "read_text" else 1
            if len(node.args) > enc_pos:
                continue
            yield node.lineno, ast.unparse(node)
        # builtin open() in text mode; encoding is the 4th positional.
        elif isinstance(func, ast.Name) and func.id == "open":
            if len(node.args) >= 4:
                continue
            mode = _open_mode(node)
            if mode is None:
                yield node.lineno, ast.unparse(node)
            elif isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                if "b" not in mode.value:
                    yield node.lineno, ast.unparse(node)
            else:
                # Mode computed at runtime: cannot prove it is binary.
                yield node.lineno, ast.unparse(node)


def _tracked_production_files():
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=REPO, capture_output=True,
        text=True, encoding="utf-8", check=True,
    ).stdout.split("\n")
    return [p for p in out if p and _is_production(p)]


def _scan_repo():
    hits = []
    for rel in _tracked_production_files():
        src = (REPO / rel).read_text(encoding="utf-8")
        for lineno, text in find_unencoded_text_io(src):
            hits.append((rel, lineno, text))
    return hits


def apply_allowlist(hits, allowlist):
    """Return (violations, stale).

    violations: hits not covered by the allowlist, as (file, line, text).
    stale: allowlist entries whose count no longer matches the calls found,
    as (file, text, expected, found).
    """
    found = {}
    for rel, lineno, text in hits:
        found.setdefault((rel, text), []).append(lineno)
    violations = []
    for rel, lineno, text in hits:
        if (rel, text) not in allowlist:
            violations.append((rel, lineno, text))
    stale = []
    for (rel, text), (count, _reason) in allowlist.items():
        n = len(found.get((rel, text), []))
        if n != count:
            stale.append((rel, text, count, n))
    return violations, stale


# ---- controls: the scan must be able to fail, and to pass -------------------

def test_control_scan_flags_each_unencoded_form():
    src = (
        "from pathlib import Path\n"
        "a = Path('x').read_text()\n"
        "Path('x').write_text('s')\n"
        "b = open('f')\n"
        "c = open('f', 'w')\n"
        "d = open('f', mode='r')\n"
    )
    assert [n for n, _ in find_unencoded_text_io(src)] == [2, 3, 4, 5, 6]


def test_control_scan_passes_encoded_and_binary_forms():
    src = (
        "from pathlib import Path\n"
        "a = Path('x').read_text(encoding='utf-8')\n"
        "Path('x').write_text('s', encoding='utf-8')\n"
        "b = open('f', encoding='utf-8')\n"
        "c = open('f', 'rb')\n"
        "d = open('f', mode='wb')\n"
        "e = open('f', 'r', -1, 'utf-8')\n"
    )
    assert list(find_unencoded_text_io(src)) == []


def test_scan_covers_a_nontrivial_set_of_files():
    # A scan that walks nothing passes vacuously.
    files = _tracked_production_files()
    assert len(files) >= 100
    assert "ava_patched.py" in files
    assert not any(f.startswith(("_legacy", "corpus")) for f in files)
    assert any(f.startswith("tests/test_") for f in files)


def test_control_unlisted_site_is_a_violation():
    hits = [("a.py", 3, "open(p)")]
    violations, stale = apply_allowlist(hits, {})
    assert violations == [("a.py", 3, "open(p)")] and stale == []


def test_control_listed_site_is_not_a_violation():
    hits = [("a.py", 3, "open(p)")]
    violations, stale = apply_allowlist(hits, {("a.py", "open(p)"): (1, "r")})
    assert violations == [] and stale == []


def test_control_allowlist_entry_matching_no_call_is_stale():
    violations, stale = apply_allowlist([], {("a.py", "open(p)"): (1, "r")})
    assert violations == [] and stale == [("a.py", "open(p)", 1, 0)]


def test_control_extra_identical_call_is_not_silently_covered():
    # One listed, two present: the count no longer matches, so it is flagged.
    hits = [("a.py", 3, "open(p)"), ("a.py", 9, "open(p)")]
    _violations, stale = apply_allowlist(hits, {("a.py", "open(p)"): (1, "r")})
    assert stale == [("a.py", "open(p)", 1, 2)]


# ---- the invariant ----------------------------------------------------------

def test_production_text_io_names_an_encoding():
    violations, _stale = apply_allowlist(_scan_repo(), ALLOWLIST)
    report = "\n".join(f"{rel}:{ln}: {text}" for rel, ln, text in violations)
    assert not violations, (
        f"{len(violations)} text-mode call(s) without encoding=:\n{report}")


def test_allowlist_has_no_stale_entries():
    _violations, stale = apply_allowlist(_scan_repo(), ALLOWLIST)
    report = "\n".join(
        f"{rel}: {text}: listed {exp}, found {n}" for rel, text, exp, n in stale)
    assert not stale, (
        f"{len(stale)} allowlist entr(ies) no longer match:\n{report}")


def test_every_allowlist_entry_has_a_reason_and_positive_count():
    for key, (count, reason) in ALLOWLIST.items():
        assert count >= 1 and reason.strip(), key

"""Guard: temp-file publication must use replace(), never rename().

Every agent here persists its manifest and its reports the same way -- write a
sibling temp file, then move it over the target.  There are two spellings of
that move in Python and they are not interchangeable:

    Path.rename()   -> os.rename()   POSIX: silently replaces the destination.
                                     Windows: FileExistsError if it exists.
    Path.replace()  -> os.replace()  Replaces the destination atomically on
                                     BOTH platforms.

37 call sites used rename().  On Linux they worked, so the suite was green on
every supported interpreter while a third of the agents could not write a
manifest twice on Windows -- not an edge case, just the second call onward.

The behavioural proof of the fix is the Windows leg of the CI matrix.  This
file is the cheap control that runs everywhere, so a reverted call site fails
on all four legs in under a second instead of only on the one that costs the
most to run.  It reads source, not behaviour, on purpose: on POSIX the broken
spelling is behaviourally indistinguishable from the correct one, so there is
nothing for a POSIX behavioural test to observe.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Vendored corpora and retired code carry their own conventions.
EXCLUDED_PREFIXES = ("_legacy/", "corpus/", ".git/", "sim_runs/")

# A name that means "this is the staging file", so `<name>.rename(target)` is
# publication rather than an ordinary user-facing move (e.g. renaming a report
# a user asked to be renamed, which SHOULD raise if the target exists).
TEMP_NAMES = ("tmp", "tmp_path", "tmpfile", "tmp_file", "temp", "temp_path")


def _first_party_sources() -> list[Path]:
    out = []
    for p in REPO.rglob("*.py"):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        # A nested working copy of this repo is not part of it.
        if rel.startswith("ai-chip-design-platform/"):
            continue
        out.append(p)
    return sorted(out)


class TestNoRenameForPublication(unittest.TestCase):
    def test_temp_files_are_published_with_replace(self):
        offenders: list[str] = []

        for path in _first_party_sources():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:          # not ours to police here; CI syntax-checks separately
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not isinstance(fn, ast.Attribute) or fn.attr != "rename":
                    continue
                recv = fn.value
                if isinstance(recv, ast.Name) and recv.id in TEMP_NAMES:
                    rel = path.relative_to(REPO).as_posix()
                    offenders.append(f"{rel}:{node.lineno}  {recv.id}.rename(...)")

        self.assertEqual(
            offenders, [],
            "Temp files must be published with .replace(), not .rename().\n"
            ".rename() raises FileExistsError on Windows when the destination\n"
            "already exists; .replace() overwrites atomically on POSIX and\n"
            "Windows alike. Offending call sites:\n  "
            + "\n  ".join(offenders),
        )

    def test_guard_can_actually_observe(self):
        """The control needs a control: prove the walker finds what it looks for.

        A guard that silently scans nothing passes forever. This repository has
        already shipped one such guard (the CI collection check that scraped
        pytest output, matched nothing, and reported zero files). So: assert the
        sweep reaches a real population, and that the detector fires on a known
        positive.
        """
        self.assertGreater(len(_first_party_sources()), 100,
                           "source sweep found almost nothing -- guard is blind")

        tree = ast.parse("tmp = Path('x')\ntmp.rename(target)\n")
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "rename"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id in TEMP_NAMES]
        self.assertEqual(len(hits), 1, "detector does not fire on a known positive")


if __name__ == "__main__":
    unittest.main()

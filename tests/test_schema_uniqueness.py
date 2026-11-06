"""Ratchet on duplicated first-party JSON schemas (part of #20).

The same schema filename exists in several directories and the copies have
diverged.  AGENT_A's copies are declared canonical in AGENT_A/interfaces.md.
This test pins how many *distinct* documents exist per filename today.  It
fails if a new copy appears or a divergence grows, and also if the pin is
higher than the real count, so the number can only go down.
"""
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# filename -> number of distinct canonicalised documents among first-party copies
PINNED_DISTINCT = {
    "run_manifest.schema.json": 1,
    "commitlog.schema.json": 1,
}

SKIP_PREFIXES = ("_legacy", "corpus")


def count_distinct(docs):
    """Number of distinct JSON documents, ignoring key order and whitespace."""
    return len({json.dumps(d, sort_keys=True) for d in docs})


def scan():
    out = subprocess.check_output(
        ["git", "ls-files", "*.schema.json"], cwd=ROOT, text=True
    )
    groups = defaultdict(list)
    for rel in out.splitlines():
        if rel.startswith(SKIP_PREFIXES):
            continue
        with open(ROOT / rel, encoding="utf-8") as fh:
            groups[Path(rel).name].append(json.load(fh))
    return {name: count_distinct(docs) for name, docs in groups.items()}


def problems(actual, pinned):
    """Why `actual` (filename -> distinct count) does not match `pinned`; [] if it does."""
    found = []
    if set(actual) != set(pinned):
        found.append(
            f"schema filenames changed: actual {sorted(actual)} vs pinned {sorted(pinned)}"
        )
    for name, pin in pinned.items():
        if name not in actual:
            continue
        if actual[name] > pin:
            found.append(f"{name}: {actual[name]} distinct > pinned {pin}")
        elif actual[name] < pin:
            found.append(f"{name}: only {actual[name]} distinct, pin says {pin}; lower the pin")
    return found


def main():
    actual = scan()
    for name in sorted(actual):
        print(f"{name}: {actual[name]} distinct (pinned {PINNED_DISTINCT.get(name, 'none')})")
    found = problems(actual, PINNED_DISTINCT)
    for line in found:
        print(f"FAIL {line}")
    if found:
        return 1
    print(f"Schema uniqueness OK: {len(actual)} filename(s) match their pins")
    return 0


def test_distinct_schema_count_matches_pin():
    assert problems(scan(), PINNED_DISTINCT) == []


def test_problems_reports_a_diverging_copy_a_stale_pin_and_a_new_filename():
    pin = {"a.schema.json": 1}
    assert problems({"a.schema.json": 1}, pin) == []
    assert any("2 distinct > pinned 1" in m for m in problems({"a.schema.json": 2}, pin))
    assert any("lower the pin" in m for m in problems({"a.schema.json": 1}, {"a.schema.json": 2}))
    assert any("filenames changed" in m for m in problems(
        {"a.schema.json": 1, "b.schema.json": 1}, pin))


def test_counter_counts_differing_docs():
    docs = [{"a": 1}, {"a": 2}, {"b": 1}]
    assert count_distinct(docs) == 3


def test_counter_collapses_identical_docs():
    assert count_distinct([{"a": 1, "b": 2}, {"b": 2, "a": 1}, {"a": 1, "b": 2}]) == 1


def test_script_mode_prints_the_distinct_counts_and_exits_zero():
    # CI runs this file as a script; it must say what it counted.
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name, pinned in PINNED_DISTINCT.items():
        assert f"{name}: {pinned} distinct" in proc.stdout, proc.stdout


if __name__ == "__main__":
    sys.exit(main())

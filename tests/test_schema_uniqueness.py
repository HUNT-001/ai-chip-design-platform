"""Ratchet on duplicated first-party JSON schemas (part of #20).

The same schema filename exists in several directories and the copies have
diverged.  AGENT_A's copies are declared canonical in AGENT_A/interfaces.md.
This test pins how many *distinct* documents exist per filename today.  It
fails if a new copy appears or a divergence grows, and also if the pin is
higher than the real count, so the number can only go down.
"""
import json
import subprocess
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


def test_distinct_schema_count_matches_pin():
    actual = scan()
    assert set(actual) == set(PINNED_DISTINCT), (
        f"schema filenames changed: actual {sorted(actual)} vs pinned {sorted(PINNED_DISTINCT)}"
    )
    for name, pinned in PINNED_DISTINCT.items():
        assert actual[name] <= pinned, f"{name}: {actual[name]} distinct > pinned {pinned}"
        assert actual[name] >= pinned, (
            f"{name}: only {actual[name]} distinct, pin says {pinned}; lower the pin"
        )


def test_counter_counts_differing_docs():
    docs = [{"a": 1}, {"a": 2}, {"b": 1}]
    assert count_distinct(docs) == 3


def test_counter_collapses_identical_docs():
    assert count_distinct([{"a": 1, "b": 2}, {"b": 2, "a": 1}, {"a": 1, "b": 2}]) == 1

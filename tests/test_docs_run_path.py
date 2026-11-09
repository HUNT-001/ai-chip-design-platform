"""The run-path diagram is drawn in two places; they must not drift apart."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)


def _first_block(path):
    m = BLOCK.search((ROOT / path).read_text(encoding="utf-8"))
    assert m, f"no mermaid block in {path}"
    return m.group(1)


def test_readme_diagram_matches_docs_diagram():
    assert _first_block("README.md") == _first_block("docs/ava-run-path.md")


def test_diagram_names_all_three_verdicts():
    src = _first_block("docs/ava-run-path.md")
    for needle in ("completed · exit 0", "failed · exit 1", "incomplete · exit 3"):
        assert needle in src

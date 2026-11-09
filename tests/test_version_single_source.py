"""The release version is written in ava_patched.VERSION; nothing else may
carry a different one (the run metadata said AVA-v3.0 through the v4.0.0 work)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ava_patched  # noqa: E402


def test_pyproject_version_is_the_code_version():
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"), re.M)
    assert m and m.group(1) == ava_patched.VERSION


def test_no_hardcoded_version_remains_in_the_orchestrator():
    src = (ROOT / "ava_patched.py").read_text(encoding="utf-8")
    assert not re.findall(r"AVA[ -]v\d+\.\d+", src)

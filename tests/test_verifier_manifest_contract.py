"""A gating AGENT_H verifier must never report success on a manifest it
could not read (#36).

Written from the issue, not from the verifiers: #36 reports that all 50 gating
``run_from_manifest`` entry points return 0 -- "pass" -- when the manifest is
missing or corrupt, so a pipeline step that verified nothing is indistinguishable
from one that verified everything.  The expected answer is a non-zero exit that
means "could not verify", distinct from 1 ("violation found"): exit 3, matching
``EXIT_CONFIG`` in AGENT_C/run_iss.py and AGENT_D/compare_commitlogs.py.  2 is
not available -- every one of these CLIs uses argparse, which exits 2 on a usage
error.

The verifiers are discovered by scanning AGENT_H, never listed by name, so a new
verifier is held to the contract the day it is added.
"""

import importlib
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT_H_DIR = REPO_ROOT / "AGENT_H"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Exit code for a manifest that cannot be read.  From #36's suggested fix.
EXIT_BAD_MANIFEST = 3

#: Documented in #36 as advisory: these always return 0 by design, because
#: nothing gates on them.  Listed by name on purpose -- a verifier must not be
#: able to opt out of the contract by what it says about itself.
ADVISORY = frozenset({
    "coverage_collector",
    "fault_injector",
    "self_evolving_engine",
    "stimulus_generator",
})

#: The issue confirmed 50 affected verifiers at runtime.  The floor is a
#: tripwire: if discovery silently stops finding them, the whole suite below
#: would pass vacuously.
EXPECTED_AT_LEAST = 50


def _gating_verifiers() -> list[str]:
    names = []
    for path in sorted(AGENT_H_DIR.glob("*.py")):
        if path.stem == "__init__" or path.stem in ADVISORY:
            continue
        if "def run_from_manifest" in path.read_text(errors="replace"):
            names.append(path.stem)
    return names


GATING_VERIFIERS = _gating_verifiers()


def _unreadable_manifest(flavour: str, tmp_path: pathlib.Path) -> pathlib.Path:
    if flavour == "missing":
        return tmp_path / "nonexistent" / "manifest.json"
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not json")          # #36's corrupt case, verbatim
    return manifest


def test_discovery_finds_the_gating_verifiers():
    assert len(GATING_VERIFIERS) >= EXPECTED_AT_LEAST, (
        f"discovered only {len(GATING_VERIFIERS)} gating run_from_manifest entry "
        f"points under {AGENT_H_DIR}; #36 confirmed {EXPECTED_AT_LEAST}. Either "
        "discovery is broken, or verifiers were removed and this floor needs "
        "updating deliberately."
    )
    assert ADVISORY.isdisjoint(GATING_VERIFIERS)


@pytest.mark.parametrize("flavour", ["missing", "corrupt"])
@pytest.mark.parametrize("name", GATING_VERIFIERS)
def test_unreadable_manifest_is_never_reported_as_a_pass(name, flavour, tmp_path):
    manifest = _unreadable_manifest(flavour, tmp_path)
    module = importlib.import_module(f"AGENT_H.{name}")

    try:
        rc = module.run_from_manifest(manifest)
    except Exception:
        # Raising is loud: the caller cannot mistake a traceback for a pass.
        # #36 is about the handlers that swallow the error and return 0.
        return

    assert rc != 0, (
        f"AGENT_H/{name}.py: run_from_manifest returned 0 (pass) for a "
        f"{flavour} manifest. Nothing was verified."
    )
    assert rc == EXIT_BAD_MANIFEST, (
        f"AGENT_H/{name}.py: run_from_manifest returned {rc} for a {flavour} "
        f"manifest; expected {EXIT_BAD_MANIFEST} (could not verify), which is "
        "distinct from 1 (violation found)."
    )


@pytest.mark.parametrize("flavour", ["missing", "corrupt"])
def test_cli_exit_code_reaches_the_shell(flavour, tmp_path):
    """The symptom #36 reports is the CLI's exit code, not just the return value."""
    manifest = _unreadable_manifest(flavour, tmp_path)
    proc = subprocess.run(
        [sys.executable, str(AGENT_H_DIR / "cache_verifier.py"),
         "--manifest", str(manifest)],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
    )
    assert proc.returncode == EXIT_BAD_MANIFEST, (
        f"cache_verifier.py exited {proc.returncode} for a {flavour} manifest; "
        f"expected {EXIT_BAD_MANIFEST}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )

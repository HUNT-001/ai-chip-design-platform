"""#33: AGENT_F/manifest_lock.py is a field-contract validator, not a lock,
and its docstring claimed ava_patched.py imports it. It is named for what it
does, and the docstring no longer says something false."""
import importlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_manifest_contract_exposes_contract_and_error():
    mc = importlib.import_module("manifest_contract")
    assert hasattr(mc, "ManifestContract")
    assert hasattr(mc, "ManifestContractError")


def test_old_manifest_lock_module_is_gone():
    assert not (REPO / "AGENT_F" / "manifest_lock.py").exists()


def test_docstring_does_not_claim_ava_patched_imports_it():
    mc = importlib.import_module("manifest_contract")
    assert "Imported by ava_patched" not in (mc.__doc__ or "")

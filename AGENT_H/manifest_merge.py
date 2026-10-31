"""Shared manifest write-back for the AGENT_H verifiers (#32).

A verifier used to load the manifest, run for as long as it takes, and write
its in-memory copy back, discarding whatever another agent had recorded in the
meantime. The window was the whole verifier run, and the lock added for #23
did not help: these writers never took it.

Here a verifier keeps a snapshot of the manifest as it read it, mutates its
own copy as before, and at the end hands both to :func:`commit_manifest`. That
works out which keys the verifier changed and applies only those, re-reading
the manifest under the lock (``atomic_update_manifest`` in AGENT_D), so a key
written by anyone else during the run is left alone.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

_LOCKED_UPDATER_MODULE = "_agent_d_compare_commitlogs"


def _locked_updater():
    """AGENT_D's ``atomic_update_manifest``, loaded by path.

    By path and under a private name because the verifiers run both as
    ``AGENT_H.<name>`` and as bare scripts, and neither has AGENT_D on
    ``sys.path``; putting it there would reintroduce the basename collisions
    of #7. The lock is a file lock on a sidecar, so this copy of the module
    excludes writers using any other copy of it.
    """
    mod = sys.modules.get(_LOCKED_UPDATER_MODULE)
    if mod is None:
        path = Path(__file__).resolve().parent.parent / "AGENT_D" / "compare_commitlogs.py"
        spec = importlib.util.spec_from_file_location(_LOCKED_UPDATER_MODULE, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_LOCKED_UPDATER_MODULE] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(_LOCKED_UPDATER_MODULE, None)
            raise
    return mod.atomic_update_manifest


def snapshot_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """A deep copy to diff against later; take it right after loading."""
    return copy.deepcopy(manifest)


def changed_keys(base: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Dotted-key updates for what the verifier changed, and nothing else.

    A top-level section (``phases``, ``outputs``, ``metrics``) is compared entry
    by entry, so only the verifier's own entries are written -- including when
    the section was absent from the snapshot, because someone else may have
    created it since. Anything that is not a section is written whole. A key
    the verifier removed is not propagated: none of them removes keys, and
    deleting on the strength of a stale snapshot is the mistake this module
    exists to avoid. A section the verifier created empty is not written.
    """
    updates: Dict[str, Any] = {}
    for key, value in manifest.items():
        if "." in key:
            raise ValueError(f"manifest key {key!r} contains '.', which the "
                             f"dotted-key updater cannot address")
        old = base.get(key)
        if value == old:
            continue
        if isinstance(value, dict) and (old is None or isinstance(old, dict)):
            old = old or {}
            for sub, sub_value in value.items():
                if "." in sub:
                    raise ValueError(f"manifest key {key}.{sub!r} contains '.', "
                                     f"which the dotted-key updater cannot address")
                if sub not in old or old[sub] != sub_value:
                    updates[f"{key}.{sub}"] = sub_value
        else:
            updates[key] = value
    return updates


def commit_manifest(manifest_path: Path, base: Dict[str, Any],
                    manifest: Dict[str, Any]) -> None:
    """Write the verifier's changes to the manifest under the lock."""
    updates = changed_keys(base, manifest)
    if updates:
        _locked_updater()(Path(manifest_path), updates)

"""Locked, atomic read-modify-write of the run manifest (#34).

One implementation with one contract, replacing the diverged copies in
AGENT_C and AGENT_D:

* missing manifest                -> FileNotFoundError
* corrupt manifest                -> json.JSONDecodeError
* dotted key through a non-object -> ManifestPathConflict

On any failure the manifest is left byte-identical: every update is applied
to an in-memory copy first, and the file is replaced only if all of them
succeeded.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator


class ManifestPathConflict(ValueError):
    """A dotted key needs an object where the manifest holds something else."""


@contextlib.contextmanager
def _manifest_lock(manifest_path: Path) -> Iterator[None]:
    """Hold an exclusive OS lock on <manifest>.lock for one read-modify-write.

    The lock is on a sidecar, not on the manifest: the manifest is replaced on
    every write, so a lock on it would be on a file nobody else opens.  The
    sidecar is never deleted -- removing it while another process waits on it
    would hand the two of them different locks.
    """
    lock_path = Path(str(manifest_path) + ".lock")
    with open(lock_path, "a+b") as f:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as exc:
                    # LK_LOCK gives up after ~10 s with EDEADLOCK; keep waiting.
                    if exc.errno != errno.EDEADLOCK:
                        raise
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _apply(manifest: Dict[str, Any], updates: Dict[str, Any]) -> None:
    for dotted_key, value in updates.items():
        parts = dotted_key.split(".")
        node = manifest
        for i, part in enumerate(parts[:-1]):
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                prefix = ".".join(parts[: i + 1])
                raise ManifestPathConflict(
                    f"cannot set {dotted_key!r}: {prefix!r} "
                    f"holds {type(child).__name__}, not an object"
                )
            node = child
        node[parts[-1]] = value


def atomic_update_manifest(manifest_path: Path, updates: Dict[str, Any]) -> None:
    """Apply dotted-key *updates* to the manifest, serialised against other writers.

    {"phases.iss.status": "completed"} sets
    manifest["phases"]["iss"]["status"].  The new content is written to a
    uniquely named temp file in the same directory and published with
    os.replace.
    """
    manifest_path = Path(manifest_path)
    with _manifest_lock(manifest_path):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _apply(manifest, updates)
        fd, tmp_name = tempfile.mkstemp(
            dir=manifest_path.parent, prefix=manifest_path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(json.dumps(manifest, indent=2, default=str))
            os.replace(tmp_name, manifest_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

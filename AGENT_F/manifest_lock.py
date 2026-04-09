#!/usr/bin/env python3
"""
manifest_lock.py — AVA Manifest Contract Validator
====================================================
Provides field-level assertion checks for the AVA run manifest (manifest.json).
Imported by ava_patched.py before every manifest read/write to catch contract
violations early — before they propagate to downstream agents.

Design goals
------------
  * Zero-dependency: uses only stdlib (json, pathlib, re, typing).
  * Fast: in-process validation adds < 1 ms per call.
  * Informative: every violation includes field path + constraint description.
  * Non-blocking: validation failures raise ManifestLockError (not SystemExit)
    so the caller can decide whether to abort or warn.

Usage in ava_patched.py
-----------------------
    from manifest_lock import ManifestLock, ManifestLockError

    # Validate before reading:
    lock = ManifestLock("run/manifest.json")
    try:
        lock.assert_readable()
    except ManifestLockError as exc:
        logger.error("Manifest contract violation: %s", exc)
        raise

    # Validate before writing (checks outgoing fields):
    lock.assert_writable(updates={
        "phases.coverage.status": "completed",
        "metrics.coveragepct": 87.5,
    })

    # Full schema validation:
    violations = lock.validate()
    if violations:
        for v in violations:
            logger.warning("Manifest: %s", v)

CLI
---
    python manifest_lock.py run/manifest.json           # validate + print report
    python manifest_lock.py run/manifest.json --strict  # exit 1 on any violation
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


# ── Exceptions ────────────────────────────────────────────────────────────────

class ManifestLockError(Exception):
    """
    Raised when a manifest field fails a contract assertion.

    Attributes
    ----------
    field      : dot-path to the offending field (e.g. 'metrics.coveragepct')
    constraint : human-readable description of the violated constraint
    actual     : the actual value found (or None if field was missing)
    """
    def __init__(self, field: str, constraint: str, actual: Any = None) -> None:
        self.field      = field
        self.constraint = constraint
        self.actual     = actual
        super().__init__(
            f"Manifest contract violation at '{field}': {constraint} "
            f"(got {actual!r})"
        )


# ── Field assertion primitives ────────────────────────────────────────────────

class FieldAssertion:
    """
    A single named assertion on one manifest field.

    Parameters
    ----------
    field       : dot-path (e.g. 'phases.coverage.status')
    required    : if True, field must be present
    types       : allowed Python types (None = any)
    enum        : if set, value must be one of these
    pattern     : regex pattern the string value must match
    minimum     : numeric lower bound (inclusive)
    maximum     : numeric upper bound (inclusive)
    custom      : callable(value) -> Optional[str] (return error message or None)
    description : human-readable constraint description
    """

    def __init__(
        self,
        field:       str,
        required:    bool = False,
        types:       Optional[Tuple[type, ...]] = None,
        enum:        Optional[List[Any]] = None,
        pattern:     Optional[str] = None,
        minimum:     Optional[float] = None,
        maximum:     Optional[float] = None,
        custom:      Optional[Callable[[Any], Optional[str]]] = None,
        description: str = "",
    ) -> None:
        self.field       = field
        self.required    = required
        self.types       = types
        self.enum        = enum
        self.pattern     = re.compile(pattern) if pattern else None
        self.minimum     = minimum
        self.maximum     = maximum
        self.custom      = custom
        self.description = description

    def check(self, data: Dict[str, Any]) -> List[str]:
        """Return a list of violation messages (empty = pass)."""
        value = _get_nested(data, self.field)
        violations: List[str] = []

        if value is None:
            if self.required:
                violations.append(
                    f"[REQUIRED] '{self.field}' is missing. {self.description}"
                )
            return violations   # nothing more to check if absent

        if self.types and not isinstance(value, self.types):
            violations.append(
                f"[TYPE] '{self.field}' must be {self.types}, got {type(value).__name__}. "
                f"Value: {value!r}"
            )

        if self.enum is not None and value not in self.enum:
            violations.append(
                f"[ENUM] '{self.field}' must be one of {self.enum}, got {value!r}"
            )

        if self.pattern is not None and isinstance(value, str):
            if not self.pattern.match(value):
                violations.append(
                    f"[PATTERN] '{self.field}' must match {self.pattern.pattern!r}, got {value!r}"
                )

        if self.minimum is not None and isinstance(value, (int, float)):
            if value < self.minimum:
                violations.append(
                    f"[MIN] '{self.field}' must be >= {self.minimum}, got {value}"
                )

        if self.maximum is not None and isinstance(value, (int, float)):
            if value > self.maximum:
                violations.append(
                    f"[MAX] '{self.field}' must be <= {self.maximum}, got {value}"
                )

        if self.custom is not None:
            msg = self.custom(value)
            if msg:
                violations.append(f"[CUSTOM] '{self.field}': {msg}")

        return violations


# ── Canonical assertion set (matches run_manifest.schema.json) ────────────────

MANIFEST_ASSERTIONS: List[FieldAssertion] = [

    # ── Top-level required fields ──────────────────────────────────────────
    FieldAssertion("rundir",  required=True,  types=(str,),
                   description="Absolute path to run working directory."),
    FieldAssertion("run_id",  required=True,  types=(str,),
                   description="Unique run identifier string."),
    FieldAssertion("isa",     required=True,  types=(str,),
                   pattern=r"^rv(32|64)(i|e)(m?)(a?)(f?)(d?)(c?)$",
                   description="RISC-V ISA string, e.g. 'rv32im'."),

    # ── Phases ─────────────────────────────────────────────────────────────
    FieldAssertion("phases.coverage.status",
                   types=(str,),
                   enum=["pending","running","completed","failed"],
                   description="Coverage phase lifecycle status."),
    FieldAssertion("phases.coverage.duration",
                   types=(int, float), minimum=0.0,
                   description="Wall-clock seconds for coverage phase."),

    # ── Outputs ────────────────────────────────────────────────────────────
    FieldAssertion("outputs.coveragesummary",
                   types=(str,),
                   description="Relative path to coveragesummary.json."),
    FieldAssertion("outputs.coverageraw",
                   types=(str,),
                   description="Relative path to directory containing coverage.dat."),

    # ── Metrics ────────────────────────────────────────────────────────────
    FieldAssertion("metrics.coveragepct",
                   types=(int, float), minimum=0.0, maximum=100.0,
                   description="Weighted functional coverage % [0, 100]."),
    FieldAssertion("metrics.coverage_plateau",
                   types=(bool,),
                   description="True when Mann-Kendall detects stalled coverage."),
    FieldAssertion("metrics.bug_count",
                   types=(int,), minimum=0,
                   description="Number of RTL vs ISS mismatches."),
    FieldAssertion("metrics.industrial_grade",
                   types=(bool,),
                   description="True when line≥95%, branch≥90%, toggle≥85%."),

    # ── Config (optional but type-checked when present) ────────────────────
    FieldAssertion("config.target_coverage",
                   types=(int, float), minimum=0.0, maximum=100.0,
                   description="Target coverage threshold %."),
    FieldAssertion("config.microarch",
                   types=(str,),
                   enum=["in_order","out_of_order","superscalar"],
                   description="Microarchitecture type."),

    # ── Cross-field consistency check: if coverage phase completed, ────────
    # ── coveragesummary must be set ────────────────────────────────────────
    FieldAssertion(
        "phases.coverage.status",
        custom=lambda v: (
            None  # checked separately in ManifestLock.validate()
        ),
    ),
]

# ── Write-path assertions (subset validated before manifest updates) ──────────

WRITE_ASSERTIONS: Dict[str, FieldAssertion] = {
    a.field: a for a in MANIFEST_ASSERTIONS
}


# ── Helper: dot-path navigation ───────────────────────────────────────────────

def _get_nested(data: Dict[str, Any], dotpath: str) -> Any:
    """Return the value at a dot-path like 'phases.coverage.status', or None."""
    parts = dotpath.split(".")
    node: Any = data
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


# ═══════════════════════════════════════════════════════════════════════════════
# ManifestLock
# ═══════════════════════════════════════════════════════════════════════════════

class ManifestLock:
    """
    Field-level contract validator for AVA manifest.json.

    The ManifestLock is stateless with respect to the file — it re-reads
    on every call so it always reflects the current on-disk state.
    """

    def __init__(self, manifest_path: Union[str, Path]) -> None:
        self._path = Path(manifest_path)

    # ── Public API ─────────────────────────────────────────────────────────

    def load(self) -> Dict[str, Any]:
        """Load and JSON-parse the manifest. Raises ManifestLockError on failure."""
        if not self._path.exists():
            raise ManifestLockError(
                "manifest", "File does not exist", str(self._path)
            )
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestLockError("manifest", f"JSON parse error: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestLockError("manifest", "Root must be a JSON object", type(data).__name__)
        return data

    def validate(self) -> List[str]:
        """
        Run all assertions against the current on-disk manifest.

        Returns a (possibly empty) list of human-readable violation strings.
        Does NOT raise — collect and log at the call site.
        """
        try:
            data = self.load()
        except ManifestLockError as exc:
            return [str(exc)]

        violations: List[str] = []

        for assertion in MANIFEST_ASSERTIONS:
            violations.extend(assertion.check(data))

        # Cross-field: if coverage phase completed → coveragesummary must be set
        status   = _get_nested(data, "phases.coverage.status")
        summary  = _get_nested(data, "outputs.coveragesummary")
        if status == "completed" and not summary:
            violations.append(
                "[CROSS] 'outputs.coveragesummary' must be set when "
                "'phases.coverage.status' == 'completed'"
            )

        # Cross-field: coveragepct must match industrial_grade flag
        pct   = _get_nested(data, "metrics.coveragepct")
        grade = _get_nested(data, "metrics.industrial_grade")
        if pct is not None and grade is not None:
            expected_grade = pct >= 90.0   # simplified threshold
            if grade != expected_grade:
                violations.append(
                    f"[CROSS] 'metrics.industrial_grade' ({grade}) "
                    f"inconsistent with 'metrics.coveragepct' ({pct}%). "
                    f"Expected {expected_grade}."
                )

        return violations

    def assert_readable(self) -> None:
        """
        Raise ManifestLockError if required read-path fields are invalid.

        Call this before reading the manifest in any agent.
        Only checks 'rundir' and 'run_id' — the minimum needed to locate artifacts.
        """
        data = self.load()
        for field in ("rundir", "run_id"):
            assertion = next((a for a in MANIFEST_ASSERTIONS if a.field == field), None)
            if assertion:
                violations = assertion.check(data)
                if violations:
                    raise ManifestLockError(field, violations[0])

    def assert_writable(self, updates: Dict[str, Any]) -> None:
        """
        Raise ManifestLockError if any pending update violates a field constraint.

        Parameters
        ----------
        updates : dict of dot-path -> value (same format as update_manifest())

        Call this before calling update_manifest() to catch errors early.
        """
        for dotkey, value in updates.items():
            assertion = WRITE_ASSERTIONS.get(dotkey)
            if assertion is None:
                continue   # unknown field = not locked = allow
            # Build a temporary data dict for the assertion to check
            parts = dotkey.split(".")
            tmp: Dict[str, Any] = {}
            node = tmp
            for part in parts[:-1]:
                node[part] = {}
                node = node[part]
            node[parts[-1]] = value
            violations = assertion.check(tmp)
            if violations:
                raise ManifestLockError(dotkey, violations[0], actual=value)

    def assert_phase_order(self) -> None:
        """
        Assert that no phase is marked 'completed' before its prerequisite.

        Phase order: semantic → testbench → simulation → coverage → red_team
        Raises ManifestLockError if a phase is complete but its predecessor is not.
        """
        PHASE_ORDER = ["semantic","testbench","simulation","coverage","red_team"]
        data = self.load()

        prev_status = "completed"   # virtual predecessor of 'semantic'
        for phase in PHASE_ORDER:
            status = _get_nested(data, f"phases.{phase}.status")
            if status == "completed" and prev_status not in ("completed", None):
                raise ManifestLockError(
                    f"phases.{phase}.status",
                    f"Phase '{phase}' is 'completed' but predecessor status "
                    f"is '{prev_status}' (expected 'completed')",
                    actual=status,
                )
            prev_status = status

    def report(self, strict: bool = False) -> int:
        """
        Print a human-readable validation report.

        Returns 0 if no violations, 1 if violations found.
        If strict=True, raises ManifestLockError on first violation instead.
        """
        violations = self.validate()
        if not violations:
            print(f"  ✔  {self._path} — manifest contract OK")
            return 0

        print(f"  ✘  {self._path} — {len(violations)} violation(s):")
        for v in violations:
            print(f"      • {v}")
            if strict:
                raise ManifestLockError("manifest", v)
        return 1


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _main() -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="AVA Manifest Contract Validator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python manifest_lock.py run/manifest.json
  python manifest_lock.py run/manifest.json --strict
  python manifest_lock.py run/manifest.json --field metrics.coveragepct
""",
    )
    p.add_argument("manifest",  type=Path, help="Path to manifest.json")
    p.add_argument("--strict",  action="store_true",
                   help="Exit 1 on first violation (default: report all)")
    p.add_argument("--field",   metavar="DOTPATH",
                   help="Validate only this dot-path field")
    args = p.parse_args()

    lock = ManifestLock(args.manifest)

    if args.field:
        try:
            data = lock.load()
        except ManifestLockError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        assertion = next(
            (a for a in MANIFEST_ASSERTIONS if a.field == args.field), None
        )
        if assertion is None:
            print(f"No assertion registered for field '{args.field}'")
            return 0
        violations = assertion.check(data)
        if violations:
            for v in violations:
                print(f"FAIL: {v}")
            return 1
        print(f"PASS: '{args.field}' = {_get_nested(data, args.field)!r}")
        return 0

    return lock.report(strict=args.strict)


if __name__ == "__main__":
    sys.exit(_main())

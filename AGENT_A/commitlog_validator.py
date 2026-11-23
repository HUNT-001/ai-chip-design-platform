#!/usr/bin/env python3
"""Validate a commit log (JSONL) against commitlog.schema.json 2.2.0 (#177).

A log is a sequence of JSONL lines.  It may open with one header record,

    {"_header": {"schema_version": "2.2.0", "fields_reported": [...]}}

which declares the fields its producer reports.  The header is a line of the
log, not a sidecar, so the declaration travels with it.

With a header the list is authoritative:

* rule 1: a listed field missing from a record makes the log malformed;
  a field not listed is not reported;
* rule 4: a record that carries a field the list does not name makes the log
  malformed, even when its value is null.  A producer may not claim less
  than it emits.

Rules 2 and 3 (what a comparator does with a field one side or neither side
reports) belong to the comparators, not to this validator.

A log with no header is legacy: it is checked against the record schema only,
and the report says ``fields_reported: "undeclared"``.  See
docs/dut_commit_log.md.

Usage: commitlog_validator.py LOG.jsonl [--json]
Exit:  0 valid, 1 malformed, 3 could not validate (unreadable log, no jsonschema).
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA_PATH = Path(__file__).resolve().with_name("commitlog.schema.json")
SCHEMA_VERSION = "2.2.0"
LEGACY_VERSIONS = ("2.1.0", "2.2.0")
HEADER_KEY = "_header"
UNDECLARED = "undeclared"

EXIT_VALID, EXIT_MALFORMED, EXIT_INCOMPLETE = 0, 1, 3

_cache: Dict[str, Any] = {}


def _schema() -> Dict[str, Any]:
    if "schema" not in _cache:
        _cache["schema"] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _cache["schema"]


def _validators():
    """(record validator, header validator).  jsonschema is required: without it
    nothing is validated, so the ImportError is not caught here."""
    if "validators" not in _cache:
        import jsonschema
        s = _schema()
        header = {"$schema": s["$schema"], "$defs": s["$defs"], "$ref": "#/$defs/log_header"}
        _cache["validators"] = (jsonschema.Draft202012Validator(s),
                                jsonschema.Draft202012Validator(header))
    return _cache["validators"]


def identity_fields() -> List[str]:
    """Fields every record carries whatever its producer can see."""
    return list(_schema()["required"])


def declarable_fields() -> List[str]:
    """Fields a header may list: every record property but the identity ones."""
    return list(_schema()["$defs"]["log_header"]["properties"][HEADER_KEY]
                ["properties"]["fields_reported"]["items"]["enum"])


def _is_header(obj: Any) -> bool:
    return isinstance(obj, dict) and HEADER_KEY in obj


def _where(err) -> str:
    return "/".join(str(p) for p in err.absolute_path) or "(record)"


def validate_records(objs: Sequence[Any], linenos: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """Validate parsed JSONL values; ``objs[0]`` may be the header.

    ``linenos`` gives the file line of each value (1-based); by default value i
    is line i + 1.
    """
    rec_v, hdr_v = _validators()
    if linenos is None:
        linenos = list(range(1, len(objs) + 1))
    errors: List[str] = []
    declared: Optional[List[str]] = None
    header_ok = False
    has_header = bool(objs) and _is_header(objs[0])

    if has_header:
        line = linenos[0]
        herrs = sorted(hdr_v.iter_errors(objs[0]), key=lambda e: list(e.absolute_path))
        for e in herrs:
            errors.append(f"line {line}: header {_where(e)}: {e.message}")
        fr = objs[0][HEADER_KEY].get("fields_reported") if isinstance(objs[0][HEADER_KEY], dict) else None
        if isinstance(fr, list):
            known = set(declarable_fields())
            for name in fr:
                if isinstance(name, str) and name not in known:
                    why = "an identity field, carried by every record" if name in identity_fields() \
                        else "not a field a producer may leave out"
                    errors.append(f"line {line}: header lists {name!r}, which is {why}")
            dups = sorted({n for n in fr if isinstance(n, str) and fr.count(n) > 1})
            for name in dups:
                errors.append(f"line {line}: header lists {name!r} more than once")
        header_ok = not errors
        if header_ok:
            declared = sorted(fr)

    n_records = 0
    for i, obj in enumerate(objs):
        line = linenos[i]
        if i == 0 and has_header:
            continue
        if _is_header(obj):
            errors.append(f"line {line}: a header record must be the first line, and only one "
                          f"is allowed" + (" (this is a second header)" if has_header else ""))
            continue
        n_records += 1
        for e in sorted(rec_v.iter_errors(obj), key=lambda e: list(e.absolute_path)):
            errors.append(f"line {line}: {_where(e)}: {e.message}")
        if not isinstance(obj, dict):
            continue
        if has_header:
            if obj.get("schema_version") != SCHEMA_VERSION:
                errors.append(f"line {line}: schema_version {obj.get('schema_version')!r} under a "
                              f"{SCHEMA_VERSION} header; a declared log is {SCHEMA_VERSION} throughout")
            if declared is not None:
                for f in declared:
                    if f not in obj:
                        errors.append(f"line {line}: '{f}' is listed in fields_reported but missing "
                                      f"from this record (rule 1; use null for 'none here')")
                listed = set(declared)
                for f in declarable_fields():
                    if f in obj and f not in listed:
                        errors.append(f"line {line}: this record carries '{f}', which fields_reported "
                                      f"does not list (rule 4)")

    if n_records == 0:
        errors.append("the log has no records")

    if has_header:
        fields_reported: Any = declared
    else:
        fields_reported = UNDECLARED
    return {"valid": not errors, "errors": errors, "fields_reported": fields_reported,
            "records": n_records, "schema_version": SCHEMA_VERSION}


def validate_lines(lines: Iterable[str]) -> Dict[str, Any]:
    """Validate the text lines of a JSONL log.  Blank lines are skipped; any
    other line that is not a JSON value is a malformed log."""
    objs: List[Any] = []
    linenos: List[int] = []
    errors: List[str] = []
    for n, raw in enumerate(lines, 1):
        text = raw.strip()
        if not text:
            continue
        try:
            objs.append(json.loads(text))
        except json.JSONDecodeError as exc:
            errors.append(f"line {n}: not JSON: {exc.msg}")
            continue
        linenos.append(n)
    rep = validate_records(objs, linenos)
    if errors:
        rep["errors"] = errors + rep["errors"]
        rep["valid"] = False
    return rep


def validate_file(path) -> Dict[str, Any]:
    """Raises OSError / UnicodeDecodeError if the log cannot be read."""
    with open(path, encoding="utf-8") as fh:
        return validate_lines(fh)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--max-errors", type=int, default=50, help="errors to print (0 = all)")
    args = ap.parse_args(argv)
    try:
        rep = validate_file(args.log)
    except ImportError as exc:
        print(f"cannot validate: {exc} (jsonschema is required)", file=sys.stderr)
        return EXIT_INCOMPLETE
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read {args.log}: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        fr = rep["fields_reported"]
        fr_text = (f"{UNDECLARED} (legacy: no header)" if fr == UNDECLARED
                   else "invalid header" if fr is None else ", ".join(fr) or "(none)")
        print(f"{args.log}: {'VALID' if rep['valid'] else 'MALFORMED'}, "
              f"{rep['records']} records, fields_reported: {fr_text}")
        shown = rep["errors"] if args.max_errors == 0 else rep["errors"][:args.max_errors]
        for e in shown:
            print(f"  {e}")
        if len(shown) < len(rep["errors"]):
            print(f"  ... {len(rep['errors']) - len(shown)} more")
    return EXIT_VALID if rep["valid"] else EXIT_MALFORMED


if __name__ == "__main__":
    sys.exit(main())

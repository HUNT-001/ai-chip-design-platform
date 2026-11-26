"""A commit log's fields_reported header, for readers that hold a log in memory (#177).

A log may open with one header record,

    {"_header": {"schema_version": "2.2.0", "fields_reported": [...]}}

(docs/dut_commit_log.md section 4).  The header is not an instruction and is
never counted as one.  Under a header the list is authoritative: a listed field
missing from a record, a record carrying a field the list does not name (even
null), a legacy key spelling, a header that is not first or is repeated, or a
header with no records makes the log malformed.

This module is what the orchestrator (`ava_patched.py`),
`AGENT_C/run_iss.validate_commitlog` and `AGENT_A.semantic_analyzer` share.
It needs only the standard library: it does not run the record schema, and a
headerless (legacy) log goes through untouched, so the strict validator
(`commitlog_validator.py`) is never applied to one here (#184).

AGENT_D applies the same rules in `compare_commitlogs.py` (`_parse_header`,
`_check_declared`) with its own copy of the field lists, so that it runs from
any directory; tests/test_orchestrator_fields_reported.py holds the two to each
other and to the schema.
"""
import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

HEADER_KEY = "_header"
HEADER_SCHEMA_VERSION = "2.2.0"
#: What a verdict records for a side whose log has no header.
LEGACY_FIELDS_REPORTED = "undeclared (legacy)"

# Loaded from the schema rather than copied, so the list cannot drift from it.
_SCHEMA = json.loads(Path(__file__).with_name("commitlog.schema.json").read_text(encoding="utf-8"))
#: The names a header may list: every record property but the identity fields.
DECLARABLE_FIELDS: FrozenSet[str] = frozenset(
    _SCHEMA["$defs"]["log_header"]["properties"][HEADER_KEY]
    ["properties"]["fields_reported"]["items"]["enum"])
#: The fields every record carries, which no header lists.
IDENTITY_FIELDS: FrozenSet[str] = frozenset(_SCHEMA["required"])


def _allows_null(s: Any) -> bool:
    """Whether a property's schema admits null: a null type, or a oneOf/anyOf
    branch (local $refs followed) that does."""
    if not isinstance(s, dict):
        return False
    if "$ref" in s:
        node: Any = _SCHEMA
        for part in s["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return _allows_null(node)
    t = s.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    return any(_allows_null(b) for k in ("oneOf", "anyOf") for b in s.get(k, []))


#: Declarable fields the schema gives no null (regs, csrs, priv, ...).  Under a
#: header null on a listed field is "reported, none" (#192): for mem_writes it
#: is "no store", for trap "no trap".  On one of these it is a malformed log.
NON_NULLABLE_FIELDS: FrozenSet[str] = frozenset(
    f for f in DECLARABLE_FIELDS if not _allows_null(_SCHEMA["properties"][f]))
#: Pre-schema spellings of declarable fields.  A declared log uses the schema's
#: names only, or `rd` would report registers its header says it does not.
LEGACY_KEYS: Dict[str, str] = {
    "rd": "regs", "rd_val": "regs", "csr_writes": "csrs", "privilege": "priv",
    "mem_addr": "mem_writes", "mem_val": "mem_writes", "mem_op": "mem_writes",
    "mem_size": "mem_writes", "trap_cause": "trap", "trap_tval": "trap", "trap_pc": "trap",
}

FieldsReported = Union[List[str], str]


class MalformedLogError(ValueError):
    """A declared log that breaks its own header.

    Nothing in it can be compared or trusted, so a reader reports it as "could
    not verify", never as a pass and never as a finding.  `side`, `line` and
    `field` name where; `reason` is the label a verdict records."""

    def __init__(self, side: str, line: Optional[int], field: str, why: str):
        self.side, self.line, self.field, self.why = side, line, field, why
        where = f"line {line}" if line is not None else "end of log"
        super().__init__(f"[{side}] {where}: malformed log: {why}")

    @property
    def reason(self) -> str:
        where = f"line{self.line}" if self.line is not None else "end"
        return f"malformed_log:{self.side}:{where}:{self.field}"


def is_header(obj: Any) -> bool:
    return isinstance(obj, dict) and HEADER_KEY in obj


def parse_header(obj: Dict[str, Any], side: str = "log", line: Optional[int] = 1) -> List[str]:
    """The sorted fields_reported of a header record, or MalformedLogError.

    The shape is the schema's `$defs.log_header`: exactly
    `{"_header": {"schema_version": "2.2.0", "fields_reported": [...]}}`, the
    list holding distinct declarable names."""
    def bad(why: str, field: str = "header") -> MalformedLogError:
        return MalformedLogError(side, line, field, f"header {why}")

    extra = sorted(k for k in obj if k != HEADER_KEY)
    if extra:
        raise bad(f"record carries {extra} besides {HEADER_KEY!r}")
    h = obj[HEADER_KEY]
    if not isinstance(h, dict):
        raise bad(f"{HEADER_KEY!r} is a {type(h).__name__}, not an object")
    for k in sorted(set(h) - {"schema_version", "fields_reported"}):
        raise bad(f"key {k!r} is not defined by the format", k)
    for k in ("schema_version", "fields_reported"):
        if k not in h:
            raise bad(f"has no {k!r}", k)
    if h["schema_version"] != HEADER_SCHEMA_VERSION:
        raise bad(f"schema_version is {h['schema_version']!r}, not {HEADER_SCHEMA_VERSION!r}",
                  "schema_version")
    fr = h["fields_reported"]
    if not isinstance(fr, list):
        raise bad(f"fields_reported is a {type(fr).__name__}, not a list", "fields_reported")
    for name in fr:
        if not isinstance(name, str) or name not in DECLARABLE_FIELDS:
            why = ("an identity field, carried by every record" if name in IDENTITY_FIELDS
                   else "not a field a producer may leave out")
            raise bad(f"lists {name!r}, which is {why}", str(name))
        if fr.count(name) > 1:
            raise bad(f"lists {name!r} more than once", name)
    return sorted(fr)


def check_record(rec: Any, declared: FrozenSet[str], side: str = "log",
                 line: Optional[int] = None) -> None:
    """Hold one record to its log's header: every listed field present (null is
    a value, "reported, none", where the schema has a null; #192), nothing
    unlisted (even null), no legacy key spellings."""
    if not isinstance(rec, dict):
        return      # not a record at all: each reader reports that as it always has
    for f in sorted(declared):
        if f not in rec:
            raise MalformedLogError(
                side, line, f, f"{f!r} is listed in fields_reported but missing from this "
                f"record (under a header, null is 'none here'; absence is not)")
        if rec[f] is None and f in NON_NULLABLE_FIELDS:
            raise MalformedLogError(
                side, line, f, f"{f!r} is null, which the schema does not allow (under a "
                f"header null is 'reported, none', and {f!r} has no null)")
    for f in sorted(DECLARABLE_FIELDS - declared):
        if f in rec:
            raise MalformedLogError(
                side, line, f, f"this record carries {f!r}, which fields_reported does not list")
    for k in sorted(LEGACY_KEYS):
        if k in rec:
            raise MalformedLogError(
                side, line, k, f"this record carries {k!r}, the legacy spelling of "
                f"{LEGACY_KEYS[k]!r}; a declared log uses the schema's names")


def null_is_none(fields_reported: Any, field: str) -> bool:
    """True when a side's header lists `field`, so a null there is "reported,
    none" (#192), not "not reported".  False for a headerless side, whose null
    keeps meaning not reported."""
    return (isinstance(fields_reported, list) and field in fields_reported
            and field not in NON_NULLABLE_FIELDS)


def split_log(objs: Sequence[Any], side: str = "log",
              linenos: Optional[Sequence[int]] = None) -> Tuple[List[Any], FieldsReported]:
    """(records, fields_reported) for a parsed log, or MalformedLogError.

    `objs[0]` may be the header; it is never returned as a record.  A
    headerless log comes back unchanged with LEGACY_FIELDS_REPORTED, its
    records not checked here.  A header anywhere but first is malformed in
    either kind of log.  `linenos` gives each value's line (default i + 1)."""
    if linenos is None:
        linenos = range(1, len(objs) + 1)
    declared: Optional[FrozenSet[str]] = None
    records: List[Any] = []
    for i, (obj, line) in enumerate(zip(objs, linenos)):
        if is_header(obj):
            if i != 0:
                raise MalformedLogError(
                    side, line, "header", "a header record must be the first line, and only "
                    "one is allowed" + (" (this is a second header)" if declared is not None else ""))
            declared = frozenset(parse_header(obj, side, line))
            continue
        if declared is not None:
            check_record(obj, declared, side, line)
        records.append(obj)
    if declared is None:
        return records, LEGACY_FIELDS_REPORTED
    if not records:
        raise MalformedLogError(side, None, "header", "a header and no records")
    return records, sorted(declared)


def strip_header(objs: Sequence[Any]) -> List[Any]:
    """The records of a log without its leading header, unchecked.  For readers
    that only need the instructions after the log has been judged elsewhere."""
    return list(objs[1:]) if objs and is_header(objs[0]) else list(objs)

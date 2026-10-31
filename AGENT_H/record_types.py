"""Field-type check for commit-log records, shared by the AGENT_H verifiers (#10).

A record whose field has the wrong type -- an int where a string or dict is
expected -- makes a checker raise AttributeError or TypeError. That is a bad
record, not a defect in the checker, and the verifiers used to hide it by
swallowing every exception per record. They now let programming errors
propagate, so a bad record has to be recognised before the checker runs:
the verifier counts it as unchecked and moves on.

Only the fields a verifier is known to trip over are listed in its spec, and
only the field's own type is checked, not what it contains. An absent field or
``None`` is fine: the checkers treat both as "not provided".
"""
from __future__ import annotations

from typing import Any, Dict, Tuple, Type, Union

FieldTypes = Dict[str, Union[Type, Tuple[Type, ...]]]


def is_well_typed(rec: Dict[str, Any], field_types: FieldTypes) -> bool:
    """True if every listed field that is present and not None has its type."""
    for key, types in field_types.items():
        value = rec.get(key)
        if value is not None and not isinstance(value, types):
            return False
    return True

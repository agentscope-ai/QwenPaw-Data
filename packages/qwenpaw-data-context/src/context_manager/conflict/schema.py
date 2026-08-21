"""Factual property schema for C1 conflict detection.

Defines which properties are "factual" per (label, type) pair.
A change to a factual property triggers a C1 ConflictEvent;
a change to a non-factual property is a normal UPDATE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# (label, type) → set of factual property names.
# "*" as type means "all types of this label".
FACTUAL_SCHEMA: dict[tuple[str, str], frozenset[str]] = {
    ("Entity", "metric"): frozenset(
        {"formula", "calculation_logic", "unit"}
    ),
    ("Entity", "dimension"): frozenset(
        {"hierarchy", "granularity"}
    ),
    ("Entity", "term"): frozenset(
        {"definition",}
    ),
    ("Entity", "formula"): frozenset(
        {"expression", "parameters"}
    ),
}

# Properties that are always treated as mutually exclusive (enum-like).
# If old != new after normalization, it's definitely a conflict.
_ENUM_EXCLUSIVE: frozenset[str] = frozenset({"unit"})

_SQL_KW = re.compile(
    r"\b(SELECT|FROM|WHERE|AND|OR|NOT|IN|AS|ON|JOIN|LEFT|RIGHT|INNER|OUTER|"
    r"GROUP|ORDER|BY|HAVING|LIMIT|OFFSET|UNION|ALL|DISTINCT|CASE|WHEN|THEN|"
    r"ELSE|END|NULL|IS|LIKE|BETWEEN|EXISTS|COUNT|SUM|AVG|MIN|MAX|COALESCE|"
    r"CAST|OVER|PARTITION|ROW_NUMBER|RANK|DENSE_RANK|WITH|INSERT|UPDATE|"
    r"DELETE|CREATE|ALTER|DROP|SET|VALUES|INTO)\b",
    re.IGNORECASE,
)


def normalize_value(value: str) -> str:
    """Normalize a property value for deterministic comparison.

    - strip whitespace
    - lowercase SQL keywords (preserve identifiers)
    - remove trailing semicolons
    - collapse internal whitespace
    """
    if not isinstance(value, str):
        value = str(value)
    v = value.strip()
    v = v.rstrip(";").strip()
    v = _SQL_KW.sub(lambda m: m.group(0).lower(), v)
    v = re.sub(r"\s+", " ", v)
    return v


def get_factual_properties(label: str, node_type: str) -> frozenset[str]:
    """Return the set of factual properties for a (label, type) pair."""
    exact = FACTUAL_SCHEMA.get((label, node_type))
    if exact is not None:
        return exact
    wildcard = FACTUAL_SCHEMA.get((label, "*"))
    if wildcard is not None:
        return wildcard
    return frozenset()


@dataclass
class FactualDiff:
    """Result of comparing old and new properties for factual conflicts."""

    changed_props: dict[str, tuple[str, str]]  # prop → (old_val, new_val)
    normalized_equal: bool  # True if all diffs disappear after normalization
    exclusive_conflict: bool  # True if an enum-exclusive prop differs


def factual_diff_check(
    label: str,
    node_type: str,
    old_props: dict,
    new_props: dict,
) -> Optional[FactualDiff]:
    """Compare old and new properties, returning only factual diffs.

    Returns None if no factual properties changed at all.
    """
    factual_keys = get_factual_properties(label, node_type)
    if not factual_keys:
        return None

    changed: dict[str, tuple[str, str]] = {}
    for prop in factual_keys:
        old_val = str(old_props.get(prop) or "")
        new_val = str(new_props.get(prop) or "")
        if old_val == new_val:
            continue
        # Both empty after coercion → no real change
        if not old_val and not new_val:
            continue
        changed[prop] = (old_val, new_val)

    if not changed:
        return None

    all_normalized_equal = all(
        normalize_value(old) == normalize_value(new)
        for old, new in changed.values()
    )

    has_exclusive = any(p in _ENUM_EXCLUSIVE for p in changed)

    return FactualDiff(
        changed_props=changed,
        normalized_equal=all_normalized_equal,
        exclusive_conflict=has_exclusive and not all_normalized_equal,
    )

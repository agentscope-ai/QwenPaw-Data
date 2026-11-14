# -*- coding: utf-8 -*-
"""Business entities recovered from one ``query_graph`` call.

The model writes its own Cypher, so there is no per-entity tool to key on. What
a lookup was *aimed at* is read off the statement (a name equality is a lookup
by name, a bare listing is a dump), and what it *found* off the result rows.
Variables bind to labels through the MATCH patterns, and RETURN items bind
result columns back to those variables, whether the model projected properties
(``m.name AS metric``) or whole nodes (``RETURN m``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from qwenpaw_data.host.core.algo.followup.models import Provenance

EntityKind = Literal["metric", "dimension", "dataset"]

_PATTERN_VAR_RE = re.compile(r"[(\[]\s*(\w+)\s*:\s*(\w+)")
_RETURN_RE = re.compile(
    r"\bRETURN\b(.*?)(?=\bORDER\s+BY\b|\bSKIP\b|\bLIMIT\b|\bUNION\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_PATTERN_PROPS_RE = re.compile(r"\{[^{}]*:")
_AS_RE = re.compile(r"^(?P<expr>.*?)\s+AS\s+(?P<alias>\w+)$", re.IGNORECASE | re.DOTALL)
_PROP_EXPR_RE = re.compile(r"^(\w+)\.(\w+)$")
_VAR_EXPR_RE = re.compile(r"^\w+$")
_IDENT_RE = re.compile(r"[a-zA-Z_]\w*")

_METRIC_LABELS = frozenset({"Metric", "MetricCaliber"})
_NAME_PROPS = ("name", "metric_name", "key")
_KEYED_LABELS = frozenset({"Metric", "Dimension", "Dataset"})


@dataclass
class GraphEntity:
    """One entity a lookup returned, merged across the rows of one result."""

    kind: EntityKind
    name: str
    provenance: Provenance
    analyzed: bool = False
    aliases: tuple[str, ...] = ()
    columns: set[str] = field(default_factory=set)


@dataclass
class GraphFindings:
    """Everything one ``query_graph`` call told the run."""

    entities: list[GraphEntity] = field(default_factory=list)
    metric_dimensions: dict[str, set[str]] = field(default_factory=dict)
    has_golden_query: bool = False


def parse_graph_query(cypher: str, rows: list[Any]) -> GraphFindings:
    """Read the entities, metric-to-dimension bindings and golden SQL of a call."""
    findings = GraphFindings()
    labels = dict(_PATTERN_VAR_RE.findall(cypher))
    items = _return_items(cypher)
    filtered = bool(_WHERE_RE.search(cypher) or _PATTERN_PROPS_RE.search(cypher))
    merged: dict[tuple[EntityKind, str], GraphEntity] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        by_var = _props_by_var(row, items)
        metrics: list[str] = []
        dimensions: list[GraphEntity] = []
        columns: set[str] = set()

        for var, props in by_var.items():
            label = labels.get(var, "")
            if label == "VerifiedQuery" and props.get("verified_sql"):
                findings.has_golden_query = True
            elif label == "DatasetField":
                columns.update(_column_tokens(props.get("name")))
                _add_dataset(merged, props.get("dataset"), filtered)
            elif label == "MAPS_TO_FIELD":
                columns.update(_column_tokens(props.get("expr")))
            elif label == "MetricCaliber":
                _add_dataset(merged, props.get("dataset"), filtered)

            if label in {"Dataset", "Table"}:
                _add_dataset(merged, _name_of(props, label), filtered)
            elif label in _METRIC_LABELS | {"Dimension"}:
                name = _name_of(props, label)
                if not name:
                    continue
                kind: EntityKind = "dimension" if label == "Dimension" else "metric"
                entity = _merge(
                    merged,
                    GraphEntity(
                        kind=kind,
                        name=name,
                        provenance=_provenance(cypher, var, filtered),
                        analyzed=_is_named_lookup(cypher, var),
                        aliases=_strings(props.get("synonyms")),
                    ),
                )
                if kind == "metric":
                    metrics.append(name)
                else:
                    dimensions.append(entity)

        for entity in dimensions:
            entity.columns.update(columns)
            for metric in metrics:
                findings.metric_dimensions.setdefault(metric, set()).add(entity.name)

    findings.entities = list(merged.values())
    return findings


def _return_items(cypher: str) -> list[tuple[str, str, str | None]]:
    """RETURN items as (result column, variable, property or None for a node)."""
    clauses = _RETURN_RE.findall(cypher)
    if not clauses:
        return []
    body = re.sub(r"^\s*DISTINCT\s+", "", clauses[-1], flags=re.IGNORECASE)
    items: list[tuple[str, str, str | None]] = []
    for raw in _split_top_level(body):
        text = raw.strip()
        if not text:
            continue
        alias_match = _AS_RE.match(text)
        expr = alias_match["expr"].strip() if alias_match else text
        column = alias_match["alias"] if alias_match else text
        prop_match = _PROP_EXPR_RE.match(expr)
        if prop_match:
            items.append((column, prop_match[1], prop_match[2]))
        elif _VAR_EXPR_RE.match(expr):
            items.append((column, expr, None))
    return items


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _props_by_var(
    row: dict[str, Any], items: list[tuple[str, str, str | None]]
) -> dict[str, dict[str, Any]]:
    by_var: dict[str, dict[str, Any]] = {}
    for column, var, prop in items:
        if column not in row:
            continue
        value = row[column]
        props = by_var.setdefault(var, {})
        if prop is not None:
            props[prop] = value
        elif isinstance(value, dict):
            props.update(value)
    return by_var


def _name_of(props: dict[str, Any], label: str) -> str:
    # Only these labels' graph keys end in the surface name the collector strips
    # to; a caliber or table key does not, so those fall back to no name at all.
    keys = _NAME_PROPS if label in _KEYED_LABELS else _NAME_PROPS[:2]
    for key in keys:
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _column_tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    tokens = {token.lower() for token in _IDENT_RE.findall(value)}
    tokens.discard("select")
    return tokens


def _is_named_lookup(cypher: str, var: str) -> bool:
    """True when the statement pins this variable's name with = or IN."""
    equality = re.compile(
        rf"\b{re.escape(var)}\.(?:name|metric_name)\s*\)?\s*(?:=(?!~)|IN\b)",
        re.IGNORECASE,
    )
    pattern = re.compile(
        rf"\(\s*{re.escape(var)}\s*:\s*\w+\s*\{{[^{{}}]*\bname\s*:", re.IGNORECASE
    )
    return bool(equality.search(cypher) or pattern.search(cypher))


def _provenance(cypher: str, var: str, filtered: bool) -> Provenance:
    if _is_named_lookup(cypher, var):
        return "targeted"
    # A search scoped to the user's own wording is mildly relevant; a statement
    # with no condition at all lists a whole domain and names what nobody asked for.
    return "metric_bound" if filtered else "domain_dump"


def _add_dataset(
    merged: dict[tuple[EntityKind, str], GraphEntity], name: Any, filtered: bool
) -> None:
    if not isinstance(name, str) or not name.strip():
        return
    provenance: Provenance = "targeted" if filtered else "domain_dump"
    _merge(
        merged,
        GraphEntity(kind="dataset", name=name.strip().lower(), provenance=provenance),
    )


def _merge(
    merged: dict[tuple[EntityKind, str], GraphEntity], entity: GraphEntity
) -> GraphEntity:
    existing = merged.get((entity.kind, entity.name))
    if existing is None:
        merged[(entity.kind, entity.name)] = entity
        return entity
    existing.analyzed = existing.analyzed or entity.analyzed
    existing.aliases = tuple(dict.fromkeys((*existing.aliases, *entity.aliases)))
    if entity.provenance == "targeted":
        existing.provenance = "targeted"
    return existing


__all__ = ["GraphEntity", "GraphFindings", "parse_graph_query"]

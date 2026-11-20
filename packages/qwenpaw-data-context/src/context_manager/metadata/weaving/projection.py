"""Project committed editor facts into the current logical Metadata schema."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ...schema import (
    ANCHORED_TO,
    AVAILABLE_IN,
    CALIBER_ON,
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    MAPS_TO_FIELD,
    DOMAIN,
    HAS_CALIBER,
    HAS_DATASET,
    HAS_DIMENSION,
    HAS_METRIC,
    METRIC,
    METRIC_CALIBER,
    VERIFIED_QUERY,
)
from ...store import EdgeDraft, NodeDraft
from .vocabulary import (
    additivity,
    binding_properties,
    dimension_column,
    dimension_role,
    priority,
)


@dataclass(frozen=True)
class Projection:
    """Canonical facts plus each node's datasource placement, before graph I/O."""

    nodes: dict[str, NodeDraft]
    edges: dict[tuple, EdgeDraft]
    datasources: dict[str, str | None]
    bindings: frozenset[tuple[str, str]] = (
        frozenset()
    )  # Dimension/Dataset row identities, independent of expr.
    datasets_without_datasource: frozenset[int] = frozenset()


def members(value) -> list[str]:
    return [part.strip() for part in (value or "").split("$$$") if part.strip()]


def project(snapshot: dict, schema, owner_id: str) -> Projection:
    """Keep stable Domain IDs and re-key dependents when their business names change."""
    nodes, edges, datasources = {}, {}, {}
    domains = {row["id"]: row for row in snapshot.get("domain", ())}
    datasets = {row["id"]: row for row in snapshot.get("dataset", ())}
    # Legacy rows without a datasource have no graph scope or datasource anchor.
    datasets_without_datasource = frozenset(
        dataset_id
        for dataset_id, row in datasets.items()
        if not (row["datasource_id"] or "").strip()
    )
    dimensions = {row["id"]: row for row in snapshot.get("dimension_dict", ())}
    metrics = {row["id"]: row for row in snapshot.get("metric_dict", ())}
    sources = {row["datasource_id"]: row for row in snapshot.get("datasource", ())}
    refs = {}
    bindings = set()

    def domain_id(row):
        domain = domains[row["domain_id"]]
        return str(domain.get("import_id") or "").strip() or str(domain["id"])

    def node(spec, values, datasource=None, *, anchor=False):
        key = spec.property("key").generate(
            {"owner_id": owner_id, "datasource_id": datasource, **values}
        )
        if anchor and key in nodes:
            return key
        draft = NodeDraft.build(
            schema,
            spec,
            key,
            values,
            transient={"reference_only": True} if anchor else {},
        )
        nodes[key] = draft
        datasources[key] = datasource or None
        return key

    def edge(spec, source_ref, target_ref, *, anchor=False, **values):
        draft = EdgeDraft.build(
            schema,
            spec,
            source_ref,
            target_ref,
            values,
            transient={"reference_only": True} if anchor else {},
        )
        edges[(spec, source_ref, target_ref)] = draft

    for row in domains.values():
        refs[("domain", row["id"])] = node(
            DOMAIN,
            {
                "domain_id": str(row.get("import_id") or "").strip() or str(row["id"]),
                "name": row["domain_name"],
                "description": row.get("description"),
                "datasource_scope": members(row.get("datasource_scope")),
                "source": "semantic_config",
            },
        )
    for row in datasets.values():
        if row["id"] in datasets_without_datasource:
            continue
        key = node(
            DATASET,
            {
                "domain": domain_id(row),
                "name": row["dataset_name"],
                "description": row.get("description"),
                "source": row["source"],
            },
            row["datasource_id"],
        )
        refs[("dataset", row["id"])] = key
        edge(HAS_DATASET, refs[("domain", row["domain_id"])], key)
        source = sources.get(row["datasource_id"], {})
        source_key = node(
            DATA_SOURCE,
            {
                "datasource_name": source.get("datasource_name")
                or row["datasource_id"],
                "datasource_type": source.get("datasource_type") or "",
            },
            row["datasource_id"],
            anchor=True,
        )
        edge(AVAILABLE_IN, key, source_key)
    for row in snapshot.get("dataset_column_meta", ()):
        if row["dataset_id"] in datasets_without_datasource:
            continue
        parent = datasets[row["dataset_id"]]
        key = node(
            DATASET_FIELD,
            {
                "domain": domain_id(parent),
                "dataset": parent["dataset_name"],
                "name": row["field_name"],
                "description": row.get("description"),
                "data_type": row.get("data_type") or "text",
                "is_primary": str(row.get("is_primary") or "N").upper() == "Y",
                "is_nullable": str(row.get("is_nullable") or "Y").upper() == "Y",
            },
            parent["datasource_id"],
        )
        edge(DATASET_HAS_COLUMN_DATASET_FIELD, refs[("dataset", parent["id"])], key)
    for kind, spec, relation, records in (
        ("dimension", DIMENSION, HAS_DIMENSION, dimensions),
        ("metric", METRIC, HAS_METRIC, metrics),
    ):
        for row in records.values():
            values = {
                "domain": domain_id(row),
                "name": row[f"{kind}_name"],
                "description": row.get("description"),
                "synonyms": members(row.get("synonyms")),
                "priority": priority(row.get("priority")),
                "source": "semantic_config",
            }
            if spec is DIMENSION:
                values.update(
                    dimension_role=dimension_role(row.get("dimension_type")),
                    child_dimensions=members(row.get("child_dimensions")),
                )
            key = node(spec, values)
            refs[(kind, row["id"])] = key
            edge(relation, refs[("domain", row["domain_id"])], key)
    for row in snapshot.get("dataset_dimension", ()):
        if row["dataset_id"] in datasets_without_datasource:
            continue
        binding = (
            refs[("dimension", row["dimension_id"])],
            refs[("dataset", row["dataset_id"])],
        )
        bindings.add(binding)
        column = dimension_column(row["expr"])
        if not column:
            continue
        parent = datasets[row["dataset_id"]]
        key = node(
            DATASET_FIELD,
            {
                "domain": domain_id(parent),
                "dataset": parent["dataset_name"],
                "name": column,
            },
            parent["datasource_id"],
            anchor=True,
        )
        edge(
            DATASET_HAS_COLUMN_DATASET_FIELD,
            refs[("dataset", parent["id"])],
            key,
            anchor=True,
        )
        edge(
            MAPS_TO_FIELD,
            refs[("dimension", row["dimension_id"])],
            key,
            **binding_properties(row["expr"], members(row.get("value_example"))),
        )
        identity = (MAPS_TO_FIELD, binding[0], key)
        draft = edges[identity]
        edges[identity] = EdgeDraft.build(
            schema,
            draft.spec,
            draft.source_ref,
            draft.target_ref,
            draft.properties,
            transient={"binding": binding},
        )
    for row in snapshot.get("metric_formula_lib", ()):
        if row["dataset_id"] in datasets_without_datasource:
            continue
        parent = datasets[row["dataset_id"]]
        metric = metrics[row["metric_id"]]
        key = node(
            METRIC_CALIBER,
            {
                "domain": domain_id(metric),
                "metric_name": metric["metric_name"],
                "dataset": parent["dataset_name"],
                "expr": row["expr"],
                "time_window": row.get("time_window") or "",
                "additivity": additivity(row.get("additivity")),
                "is_default": None
                if row.get("is_default") is None
                else bool(row["is_default"]),
                "source": "semantic_config",
            },
            parent["datasource_id"],
        )
        edge(HAS_CALIBER, refs[("metric", metric["id"])], key)
        edge(CALIBER_ON, key, refs[("dataset", parent["id"])])
    for row in snapshot.get("verified_query", ()):
        question = row["question"].strip()
        key = node(
            VERIFIED_QUERY,
            {
                "domain": domain_id(row),
                "question": question,
                "question_hash": hashlib.sha256(question.encode()).hexdigest()[:16],
                "verified_sql": row["verified_sql"],
            },
        )
        edge(ANCHORED_TO, key, refs[("domain", row["domain_id"])])
    return Projection(
        nodes,
        edges,
        datasources,
        bindings=frozenset(bindings),
        datasets_without_datasource=datasets_without_datasource,
    )

"""Adapt develop's settlement reads to canonical Store records.

Datasource selection follows Dataset relationships; it is not graph authorization.
Workspace/owner permissions remain enforced by the supplied Store Scope.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import cached_property

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ...auth import ScopeBinding
from ...db import get_database
from ...schema import (
    CALIBER_ON,
    DATASET,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    DIMENSION_BINDING,
    DOMAIN,
    HAS_CALIBER,
    HAS_DATASET,
    HAS_METRIC,
    MAPS_TO_FIELD,
    METRIC,
    EdgeSpec,
    NodeSpec,
)
from ...schema.relational import datasource
from ...store import (
    EdgeRecord,
    GraphStore,
    NeighborDirection,
    NodeRecord,
    StoreError,
    legacy_reads,
)
from ...utils.vector import cosine_similarity
from .identifiers import logical_dataset_name
from .projection import dataset_source, dataset_summary, dimension_summary
from .queries import _PAGE_SIZE, _nodes
from .ranking import (
    blend_relevance,
    classify,
    rrf_merge,
    soft_text_match,
)
from .relevance import RELEVANCE

log = logging.getLogger(__name__)


class MetadataHttpReader:
    """Read one authorized MG, keeping datasource/dataset selection request-local."""

    def __init__(
        self,
        store: GraphStore,
        scope: ScopeBinding,
        *,
        datasource_id: str = "",
        dataset_name: str = "",
        include_identifiers: bool = False,
    ):
        self.store = store
        self.scope = scope
        self.datasource_id = datasource_id.strip()
        self.dataset_name = dataset_name.strip()
        self.include_identifiers = include_identifiers  # MCP discovery must feed keyed tools; HTTP keeps its display contract.
        self._node_cache: dict[tuple, list[NodeRecord]] = {}
        self._records: dict[tuple[NodeSpec, str], NodeRecord | None] = {}
        self._edge_indexes: dict[
            EdgeSpec, tuple[dict[str, list[EdgeRecord]], dict[str, list[EdgeRecord]]]
        ] = {}
        self._selection_cache: dict[tuple[NodeSpec, bool], set[str]] = {}

    def _all(self, spec: NodeSpec) -> list[NodeRecord]:
        return self._list_nodes(spec)

    def _list_nodes(self, spec: NodeSpec, **filters) -> list[NodeRecord]:
        key = (spec, tuple(sorted(filters.items())))
        if key not in self._node_cache:
            nodes = list(_nodes(self.store, spec, filters, self.scope))
            self._node_cache[key] = nodes
            self._records.update(((spec, node.key), node) for node in nodes)
        return self._node_cache[key]

    def _get_nodes(self, spec: NodeSpec, keys) -> list[NodeRecord]:
        keys = tuple(dict.fromkeys(keys))
        missing = [key for key in keys if (spec, key) not in self._records]
        for offset in range(0, len(missing), _PAGE_SIZE):
            batch = missing[offset : offset + _PAGE_SIZE]
            records = self.store.get_nodes(spec, batch, scope=self.scope)
            self._records.update(
                ((spec, key), node) for key, node in zip(batch, records, strict=True)
            )
        return [node for key in keys if (node := self._records[spec, key]) is not None]

    def _edge_index(self, edge: EdgeSpec, direction=NeighborDirection.OUTGOING):
        """Read each relation once per request; Store scopes both endpoints and edges."""
        if edge not in self._edge_indexes:
            rows = {}
            offset = 0
            while True:
                page = self.store.list_edges(
                    edge, limit=_PAGE_SIZE, offset=offset, scope=self.scope
                )
                for row in page:
                    # Match get_edges' first record for parallel endpoint pairs.
                    rows.setdefault((row.source_key, row.target_key), row)
                if len(page) < _PAGE_SIZE:
                    break
                offset += len(page)
            outgoing, incoming = {}, {}
            for pair in sorted(rows):
                row = rows[pair]
                outgoing.setdefault(row.source_key, []).append(row)
                incoming.setdefault(row.target_key, []).append(row)
            self._edge_indexes[edge] = outgoing, incoming
        return self._edge_indexes[edge][direction is NeighborDirection.INCOMING]

    def _related_edges(self, keys, edge, direction=NeighborDirection.OUTGOING):
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return []
        index = self._edge_index(edge, direction)
        return [row for key in keys for row in index.get(key, ())]

    def _related_keys(self, keys, edge, direction=NeighborDirection.OUTGOING):
        return dict.fromkeys(
            row.target_key
            if direction is NeighborDirection.OUTGOING
            else row.source_key
            for row in self._related_edges(keys, edge, direction)
        )

    def _follow(self, nodes, edge: EdgeSpec, direction=NeighborDirection.OUTGOING):
        target = edge.target if direction is NeighborDirection.OUTGOING else edge.source
        return self._get_nodes(
            target, self._related_keys((node.key for node in nodes), edge, direction)
        )

    @cached_property
    def _source_datasets(self):
        filters = {"datasource_id": self.datasource_id} if self.datasource_id else {}
        return self._list_nodes(DATASET, **filters)

    @cached_property
    def _source_domains(self):
        return set(
            self._related_keys(
                (node.key for node in self._source_datasets),
                HAS_DATASET,
                NeighborDirection.INCOMING,
            )
        )

    @cached_property
    def _dataset_candidates(self):
        return sorted(
            (
                node
                for node in self._source_datasets
                if node.properties["name"] == self.dataset_name
            ),
            key=lambda node: (self.domain_name(node.properties["domain"]), node.key),
        )

    @property
    def _dataset_anchored(self):
        return bool(
            self.datasource_id
            and self.dataset_name
            and len(self._dataset_candidates) == 1
        )

    def dataset_scope_note(self):
        """Retain develop's explicit diagnostic when the anchor cannot narrow."""
        if not self.dataset_name or self._dataset_anchored:
            return None
        if not self.datasource_id:
            state = "requires_datasource"
            message = "dataset_name 需与 datasource_id 一同提供，本次已忽略数据集锚点"
        elif not self._dataset_candidates:
            state = "not_found"
            message = f"数据集 {self.dataset_name!r} 在数据源 {self.datasource_id!r} 内未找到，本次结果按数据源范围返回"
        else:
            state = "ambiguous"
            message = f"数据集名 {self.dataset_name!r} 在数据源 {self.datasource_id!r} 内命中多个域，本次结果按数据源范围返回；请带 domain 收窄"
        note = {"dataset_name": self.dataset_name, "status": state, "message": message}
        if state == "ambiguous":
            names = {
                node.properties["domain_id"]: node.properties["name"]
                for node in self._all(DOMAIN)
            }
            note["candidates"] = [
                {
                    "key": node.key,
                    "name": node.properties["name"],
                    "domain": names.get(
                        node.properties["domain"], node.properties["domain"]
                    ),
                }
                for node in self._dataset_candidates
            ]
        return note

    def _selected_datasets(self, *, retrieval=False):
        if retrieval:
            # Retrieval normalizes qualified names; HTTP anchors use exact names.
            name = logical_dataset_name(self.dataset_name)
            candidates = [
                node
                for node in self._source_datasets
                if node.properties["name"] == name
            ]
            return (
                candidates
                if self.dataset_name and len(candidates) == 1
                else self._source_datasets
            )
        return (
            self._dataset_candidates
            if self._dataset_anchored
            else self._source_datasets
        )

    def _selected_keys(self, spec: NodeSpec, *, retrieval=False):
        """Resolve only the relation paths needed by this result type."""
        cache_key = spec, retrieval
        if cache_key in self._selection_cache:
            return self._selection_cache[cache_key]
        datasets = [node.key for node in self._selected_datasets(retrieval=retrieval)]
        incoming = NeighborDirection.INCOMING
        if spec is DATASET:
            keys = set(datasets)
        elif spec is DIMENSION:
            fields = self._related_keys(datasets, DATASET_HAS_COLUMN_DATASET_FIELD)
            keys = set(self._related_keys(fields, MAPS_TO_FIELD, incoming))
            keys.update(self._related_keys(datasets, DIMENSION_BINDING, incoming))
        elif spec is METRIC:
            calibers = self._related_keys(datasets, CALIBER_ON, incoming)
            keys = set(self._related_keys(calibers, HAS_CALIBER, incoming))
        else:
            raise KeyError(spec)
        self._selection_cache[cache_key] = keys
        return keys

    def contains(self, node: NodeRecord, *, orphan_metric: bool = False) -> bool:
        """Apply develop's relation-based selection, independently of graph authorization."""
        if not self.datasource_id:
            return True
        if node.spec is DOMAIN:
            return node.key in self._source_domains
        if node.key in self._selected_keys(node.spec):
            return True
        # Without a pinned Dataset, an unbound metric has no evidence excluding it.
        return (
            orphan_metric
            and node.spec is METRIC
            and not self._dataset_anchored
            and node.key not in self._edge_index(HAS_CALIBER)
        )

    def metric_nodes(self, domain: str):
        """Datasource gates the domain; only a unique Dataset anchor gates each metric."""
        roots = [
            node
            for node in self._all(DOMAIN)
            if node.properties["domain_id"] == domain and self.contains(node)
        ]
        nodes = [
            node
            for node in self._follow(roots, HAS_METRIC)
            if node.properties["domain"] == domain
            and (not self._dataset_anchored or self.contains(node))
        ]
        return sorted(
            nodes,
            key=lambda node: (
                node.properties.get("priority") != "high",
                node.properties["name"],
                node.key,
            ),
        )

    def list_domains(self):
        nodes = self._all(DOMAIN)
        if self.datasource_id:
            nodes = [node for node in nodes if node.key in self._source_domains]
        return [
            {
                "name": node.properties["name"],
                "display_name": node.properties["name"],
                "description": node.properties.get("description") or "",
                "aliases": [],
                "datasource_id": node.properties.get("datasource_id") or "",
                **(
                    {"domain_id": node.properties["domain_id"]}
                    if self.include_identifiers
                    else {}
                ),
            }
            for node in sorted(
                nodes,
                key=lambda node: (
                    node.properties.get("datasource_id") or "",
                    node.properties["name"],
                ),
            )
        ]

    def domain_name(self, domain: str) -> str:
        return next(
            (
                node.properties["name"]
                for node in self._all(DOMAIN)
                if node.properties["domain_id"] == domain
            ),
            domain,
        )

    def domain(self, value: str | None) -> str:
        """Resolve a supplied domain name or ID against the visible Metadata graph."""
        nodes = sorted(self._all(DOMAIN), key=lambda node: node.properties["name"])
        query = (value or "").strip().lower()
        if not query:
            raise ValueError(
                f"Cannot infer domain. Available domains: {[node.properties['name'] for node in nodes]!r}. Specify domain."
            )
        if not nodes:
            return value.strip()
        matches = [
            node for node in nodes if node.properties["domain_id"].lower() == query
        ]
        if not matches:
            matches = [
                node for node in nodes if node.properties["name"].lower() == query
            ]
        if not matches:
            matches = [
                node
                for node in nodes
                if query in node.properties["name"].lower()
                or node.properties["name"].lower() in query
            ]
        if not matches:
            raise LookupError(f"Domain not found: {value.strip()}")
        if len(matches) > 1:
            raise ValueError(
                f"Cannot uniquely infer domain from '{value}'. Candidates: {[node.properties['name'] for node in matches]}. Specify domain."
            )
        return matches[0].properties["domain_id"]

    def _domain_nodes(self, spec: NodeSpec, domain: str):
        nodes = (
            [
                node
                for node in self._source_datasets
                if node.properties.get("domain") == domain
            ]
            if spec is DATASET
            else self._list_nodes(spec, domain=domain)
        )
        if self.datasource_id:
            keys = self._selected_keys(spec)
            nodes = [node for node in nodes if node.key in keys]
        return sorted(nodes, key=lambda node: (node.properties["name"], node.key))

    def _named_node(self, spec: NodeSpec, domain: str, name: str):
        """Keep cm_resolve's dimension/dataset confidence and ambiguity decisions."""
        nodes = self._domain_nodes(spec, domain)
        query = name.strip().lower()
        scored = []
        if spec is DIMENSION:
            # detect_dimensions uses retrieval scope; its list fallback uses semantic scope.
            detection_nodes = [
                node
                for node in self._list_nodes(DIMENSION, domain=domain)
                if (
                    not self.datasource_id
                    or node.key in self._selected_keys(DIMENSION, retrieval=True)
                )
            ]
            detected = [
                node
                for node in detection_nodes
                if any(
                    text.lower() in query
                    for text in (
                        node.properties["name"],
                        *(node.properties.get("synonyms") or ()),
                    )
                    if text
                )
            ]
            detected.sort(key=lambda node: -len(node.properties["name"]))
            scored = [(node, 0.75) for node in detected[:5]]
            if not scored:
                scored = [
                    (node, 0.55)
                    for node in nodes
                    if query in node.properties["name"].lower()
                ][:8]
        else:
            for node in nodes:
                candidate = node.properties["name"].lower()
                physical = (
                    dataset_source(node)["parents"] or candidate.removeprefix("view_")
                ).lower()
                score = (
                    0.95
                    if query == candidate
                    else 0.9
                    if query == physical
                    else 0.7
                    if query in candidate or query in physical
                    else 0.65
                    if candidate in query
                    else 0.0
                )
                if score:
                    scored.append((node, score))
            scored.sort(key=lambda item: -item[1])
            scored = scored[:8]
        exact = [node for node, _ in scored if node.properties["name"].lower() == query]
        if len(exact) == 1:
            return exact[0]
        if not scored:
            raise LookupError(
                f"{spec.name} not found: {self.domain_name(domain)}::{name}"
            )
        node, score = scored[0]
        second = scored[1][1] if len(scored) > 1 else 0
        if score >= 0.55 and score - second >= 0.05:
            return node
        return {
            "ambiguous": True,
            "ambiguity_candidates": [
                {
                    "entity_type": spec.name,
                    "name": candidate.properties["name"],
                    "domain": self.domain_name(domain),
                    "description": ""
                    if spec is DIMENSION
                    else (candidate.properties.get("description") or "")[:200],
                    "match_confidence": confidence,
                    "disambiguation_hint": "",
                }
                for candidate, confidence in scored
                if confidence >= 0.30
            ][:10],
            "hint": "请用更精确的名称或 domain 缩小范围",
        }

    def _bindings(self, node):
        bindings = []
        for edge in (MAPS_TO_FIELD, DIMENSION_BINDING):
            rows = self._related_edges((node.key,), edge)
            targets = {
                target.key: target
                for target in self._get_nodes(
                    edge.target, (row.target_key for row in rows)
                )
            }
            bindings.extend(
                (targets[row.target_key], row)
                for row in rows
                if row.target_key in targets
            )
        return bindings

    def dimensions(self, domain: str, name: str | None):
        nodes = self._domain_nodes(DIMENSION, domain)
        if not (name or "").strip():
            return [
                {
                    **dimension_summary(node),
                    **({"key": node.key} if self.include_identifiers else {}),
                }
                for node in nodes
            ]
        node = self._named_node(DIMENSION, domain, name)
        if isinstance(node, dict):
            return node
        if node not in nodes:
            raise KeyError(
                f"Dimension not found: {self.domain_name(domain)}::{node.properties['name']}"
            )
        bindings = self._bindings(node)
        values, markers = {}, set()
        for _, edge in bindings:
            for value in edge.properties.get("value_example") or ():
                if value.strip():
                    values.setdefault(value.strip(), None)
            marker = edge.properties.get("total_value_marker")
            if marker:
                markers.add(marker.strip())
        # develop also recognizes these explicit read-side rollup literals.
        markers.update(
            {
                "全部",
                "总计",
                "合计",
                "all",
                "ALL",
                "All",
                "overall",
                "Overall",
                "OVERALL",
                "total",
                "Total",
                "TOTAL",
            }
        )
        parents = [
            other.properties["name"]
            for other in nodes
            if node.properties["name"]
            in (other.properties.get("child_dimensions") or ())
        ]
        target, edge = bindings[0] if bindings else (None, None)
        return {
            **dimension_summary(node),
            "domain": self.domain_name(domain),
            "dataset_name": (
                target.properties.get("dataset") or target.properties["name"]
            )
            if target
            else "",
            "calculate_expr": (edge.properties.get("expr") or "") if edge else "",
            "data_type": "text",
            "parent_dimension": parents[0] if parents else "",
            "hierarchy_level": 0,
            "is_contribution_dimension": True,
            "sample_values": [
                {
                    "value": value,
                    "business_meaning": "",
                    "frequency": max(0.0, 0.35 - i * 0.02),
                    "is_rollup_sentinel": value in markers,
                }
                for i, value in enumerate(list(values)[:10])
            ],
            "sample_values_total": len(values),
            "related_knowledge": [],
        }

    @cached_property
    def _datasource_types(self):
        """Use the same workspace SQL catalog as develop, not possibly stale graph attributes."""
        try:
            with get_database().engine.connect() as connection:
                rows = (
                    connection.execute(
                        select(
                            datasource.c.datasource_id, datasource.c.datasource_type
                        ).where(
                            datasource.c.tenant_id == self.scope.tenant_id,
                            datasource.c.workspace_id == self.scope.workspace_id,
                            datasource.c.is_deleted == 0,
                        )
                    )
                    .mappings()
                    .all()
                )
            return {row["datasource_id"]: row["datasource_type"] for row in rows}
        except SQLAlchemyError:
            # As in develop, unavailable display metadata does not block graph reads.
            log.warning("Datasource types unavailable; omitting display types")
            return {}

    def datasets(self, domain: str, name: str | None):
        root = next(
            (
                node
                for node in self._all(DOMAIN)
                if node.properties["domain_id"] == domain
            ),
            None,
        )
        if root is None or (
            self.datasource_id and root.key not in self._source_domains
        ):
            suffix = f" @{self.datasource_id}" if self.datasource_id else ""
            raise KeyError(f"Domain not found: {self.domain_name(domain)}{suffix}")
        nodes = self._domain_nodes(DATASET, domain)
        types = self._datasource_types
        if not (name or "").strip():
            return [
                dataset_summary(node, self.domain_name(domain), types) for node in nodes
            ]
        node = self._named_node(DATASET, domain, name)
        if isinstance(node, dict):
            return node
        fields = self._follow((node,), DATASET_HAS_COLUMN_DATASET_FIELD)
        if not fields:
            raise KeyError(
                f"Dataset not found: {self.domain_name(domain)}::{node.properties['name']}"
            )
        columns = []
        for field in sorted(
            fields, key=lambda field: (field.properties["name"], field.key)
        ):
            values = {}
            for binding in self._related_edges(
                (field.key,), MAPS_TO_FIELD, NeighborDirection.INCOMING
            ):
                examples = (
                    (binding.properties.get("value_example") or ()) if binding else ()
                )
                if examples and examples[0].strip():
                    values.setdefault(examples[0].strip(), None)
            data_type = field.properties.get("data_type") or "string"
            column_type = (
                "日期"
                if data_type.lower() in {"date", "datetime", "timestamp"}
                else "维度"
                if "dim" in data_type.lower()
                else "度量"
            )
            columns.append(
                {
                    "column_name": field.properties["name"],
                    "column_type": column_type,
                    "data_type": data_type,
                    "description": field.properties.get("description") or "",
                    "granularity_role": "",
                    "topline_value": "",
                    "sample_values": list(values)[:10],
                    "sample_values_total": len(values) if values else None,
                    "composite": False,
                    "composite_desc": "",
                }
            )
        return {
            **dataset_summary(node, self.domain_name(domain), types),
            "physical_tables": dataset_source(node)["physical_tables"],
            "columns": columns,
        }

    def _metric_candidates(self, domain, query, limit, *, embedding=None):
        """Preserve native text ranking; vector relevance uses canonical records."""
        filters = {"domain": domain}
        try:
            if embedding:
                records = self.store.search_vector(
                    embedding,
                    specs=(METRIC,),
                    filters=filters,
                    limit=limit,
                    scope=self.scope,
                )
            else:
                # Node-only search cannot preserve score ties by source_trust,
                # or filter expired rows before top-k without a bounded recall gap.
                rows = legacy_reads.search_fulltext_scores(
                    self.store,
                    METRIC,
                    query,
                    limit=limit,
                    scope=self.scope,
                    metric_domain=domain,
                )
                return [
                    (NodeRecord(METRIC, row["node"]), float(row["score"]))
                    for row in rows
                ]
        except StoreError as exc:
            if exc.code != "index_unavailable":
                raise
            if embedding:
                log.warning("Metric vector search unavailable; retaining text results")
                return []
            log.warning("Metric text search unavailable; using name/alias matching")
            text = query.casefold()
            records = [
                record
                for record in _nodes(self.store, METRIC, filters, self.scope)
                if any(
                    text in str(value).casefold()
                    for value in (
                        record.properties.get("name", ""),
                        *(record.properties.get("synonyms") or ()),
                    )
                )
            ]
            records.sort(
                key=lambda record: -(record.properties.get("source_trust") or 1.0)
            )
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        active = []
        for record in records:
            end = record.properties.get("valid_to")
            end = end.isoformat() if hasattr(end, "isoformat") else end
            if end is None or end > now:
                active.append(
                    (
                        record,
                        cosine_similarity(embedding, record.properties["embedding"])
                        if embedding and record.properties.get("embedding")
                        else 0.0,
                    )
                )
        return active[:limit]

    def search_metrics(
        self,
        domain: str,
        query: str,
        k: int,
        *,
        embedding=None,
        relevance_threshold=None,
    ):
        """Develop fulltext/vector RRF, relevance gate and datasource soft filtering."""
        limit = max(k * 4, 20)
        text_rows = self._metric_candidates(domain, query, limit)
        nodes = {node.key: node for node, _ in text_rows}
        rankings = [[{"key": node.key} for node, _ in text_rows]]
        cosines = {}
        if embedding:
            vector_rows = self._metric_candidates(
                domain, query, limit, embedding=embedding
            )
            if vector_rows:
                rankings.append([{"key": node.key} for node, _ in vector_rows])
            for node, score in vector_rows:
                nodes[node.key] = node
                cosines[node.key] = score
        rows = rrf_merge(rankings) if len(rankings) > 1 else rankings[0]
        if self.datasource_id and rows and self._selected_datasets(retrieval=True):
            # develop preserves unbound metrics and falls back if none survive.
            # This soft query selection never relaxes Workspace/owner permissions.
            with_caliber = self._edge_index(HAS_CALIBER)
            allowed = self._selected_keys(METRIC, retrieval=True)
            filtered = [
                row
                for row in rows
                if row["key"] in allowed or row["key"] not in with_caliber
            ]
            rows = filtered or rows
        rows = rows[:k]
        threshold = (
            relevance_threshold
            if relevance_threshold and relevance_threshold > 0
            else RELEVANCE.threshold
        )
        scored = [
            (
                blend_relevance(
                    text=soft_text_match(
                        query,
                        nodes[row["key"]].properties["name"],
                        list(nodes[row["key"]].properties.get("synonyms") or ()),
                        nodes[row["key"]].properties.get("description") or "",
                    ),
                    vec_cosine_normalized=cosines.get(row["key"], 0.0),
                ),
                nodes[row["key"]],
            )
            for row in rows
        ]
        score, best = max(scored, key=lambda item: item[0], default=(0.0, None))
        status = classify(score, threshold=threshold)
        if status != "relevant":
            message = (
                f"未检索到与「{query}」相关的指标（相关度 {score:.2f}，低于门槛）。"
                if status == "no_match"
                else f"检索到与「{query}」弱相关的指标（相关度 {score:.2f}），但置信度不足，已过滤。"
            )
            return {
                "status": status,
                "message": message,
                "suggestion": "建议：1) 换用更通用的关键词重试；2) 调用 list_metrics 查看当前 domain 的全量指标列表确认是否存在；3) 确认 domain 参数是否正确。",
                "score": round(score, 3),
                "matched_name": best.properties["name"]
                if best and status == "low_confidence"
                else "",
            }
        return [
            {
                "metric_name": node.properties["name"],
                "description": node.properties.get("description") or "",
                "aliases": list(node.properties.get("synonyms") or ()),
                "tags": [],
                # develop's retrieval projection does not expose priority/role.
                "role": "",
                "priority": "normal",
                **({"key": node.key} if self.include_identifiers else {}),
            }
            for score, node in scored
            if score >= threshold
        ]

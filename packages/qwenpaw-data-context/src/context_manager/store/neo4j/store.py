"""Implement canonical graph primitives directly on prepared Cypher."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

from ...auth import GraphScopeAuthority, ScopeBinding
from ...schema import (
    CrudOperation,
    EdgeSpec,
    Glossary,
    GraphSchema,
    NodeSpec,
)
from ...utils.validation import require_nonempty_text, require_trimmed_text
from ..base import GraphStore, NeighborDirection, primitive
from ..models import EdgeRecord, NodeRecord, StoreError
from ..runtime import GraphRuntime
from .catalog import (
    Neo4jIndex,
    Neo4jIndexKind,
    Neo4jSchemaCatalog,
)
from .physical_schema import NEO4J_PHYSICAL_SCHEMA
from .runtime import Neo4jRuntime

_T = TypeVar("_T")


class Neo4jGraphStore(GraphStore):
    """Persist one exact GraphSchema through a CM Auth-checked Neo4j runtime."""

    @property
    def backend(self) -> str:
        return "neo4j"

    @property
    def fulltext_indexes(self) -> Mapping[str, str]:
        """Map public full-text index names to their logical node types."""
        return {
            index.name: index.node.name
            for index in (() if self._catalog is None else self._catalog.indexes)
            if index.kind is Neo4jIndexKind.FULLTEXT and not index.node.internal
        }

    def __init__(
        self,
        runtime: GraphRuntime,
        schema: GraphSchema,
        access: GraphScopeAuthority,
        glossary: Glossary,
        *,
        catalog: Neo4jSchemaCatalog | None = None,
        owns_runtime: bool = True,
    ) -> None:
        """Bind the only runtime, schema, and CM Auth objects used by the Store."""
        self._bind(runtime, schema, access, glossary)
        resolved_catalog = catalog
        if resolved_catalog is None and schema is NEO4J_PHYSICAL_SCHEMA.schema:
            resolved_catalog = NEO4J_PHYSICAL_SCHEMA
        if resolved_catalog is not None and resolved_catalog.schema is not schema:
            raise ValueError("physical catalog must govern the Store schema")
        self._catalog = resolved_catalog
        self._vector_dimensions = {
            node: resolved_catalog.embed_dim
            for node in schema.nodes
            if resolved_catalog is not None and node.embedding_fields
        }
        self._owns_runtime = owns_runtime

    def health(self) -> Mapping[str, object]:
        """Return a narrow liveness result without exposing driver details."""
        rows = self._runtime.read("RETURN 1 AS ok", {}, operation="health")
        return {"ok": bool(rows and rows[0].get("ok") == 1)}

    def get_nodes(
        self,
        spec: NodeSpec,
        keys: Sequence[str],
        *,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> tuple[NodeRecord | None, ...]:
        """Read exact nodes in one query and restore input positions, including misses."""
        self._prepare_read(spec, scope)
        projection = self._property_projection((spec,), fields, alias="n")
        items = self._node_batch_rows(spec, keys)
        if not items:
            return ()
        clauses, parameters, _ = self._scope(spec, scope, alias="n")
        clauses.insert(0, "n.key = row.key")
        rows = self._runtime.read(
            f"UNWIND $rows AS row MATCH (n:{spec.name}) "
            f"WHERE {' AND '.join(clauses)} "
            f"RETURN row.index AS index, {projection} AS properties",
            {**parameters, "rows": items},
            operation="get_nodes",
        )
        records = {row["index"]: NodeRecord(spec, row["properties"]) for row in rows}
        return tuple(records.get(index) for index in range(len(items)))

    @primitive
    def create_nodes(
        self,
        spec: NodeSpec,
        properties: Sequence[Mapping[str, object]],
        *,
        scope: ScopeBinding,
    ) -> tuple[str, ...]:
        """Validate the entire input, then create all nodes in one write query."""
        items = self._node_create_rows(spec, properties, scope)
        for item in items:
            item["core"] = "Core" in item.pop("labels")
            item.pop("key")
        if not items:
            return ()
        rows = self._create_batch(
            f"UNWIND $rows AS row CREATE (n:{spec.name}) SET n = row.properties "
            "FOREACH (_ IN CASE WHEN row.core THEN [1] ELSE [] END | SET n:Core) "
            "RETURN row.index AS index, n.key AS key",
            {"rows": items},
            operation="create_nodes",
        )
        return tuple(row["key"] for row in rows)

    @primitive
    def patch_nodes(
        self,
        spec: NodeSpec,
        patches: Sequence[tuple[str, Mapping[str, object]]],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Patch independent nodes with per-row fields and term label updates."""
        self._prepare_write(spec, scope, CrudOperation.UPDATE)
        items = self._node_batch_rows(
            spec, tuple(key for key, _ in patches), unique=True
        )
        managed = set(self._glossary.managed_properties(spec))
        for item, (_, changes) in zip(items, patches, strict=True):
            changes = self._patch_values(spec, changes)
            item["changes"] = changes
            item["core"] = (
                "Core" in self._tag_labels(spec, changes)
                if managed.intersection(changes)
                else None
            )
        if not items:
            return
        clauses, parameters, _ = self._scope(spec, scope, alias="n")
        clauses.insert(0, "n.key = row.key")
        self._runtime.write(
            f"UNWIND $rows AS row MATCH (n:{spec.name}) WHERE {' AND '.join(clauses)} "
            "SET n += row.changes "
            "FOREACH (_ IN CASE WHEN row.core = true THEN [1] ELSE [] END | SET n:Core) "
            "FOREACH (_ IN CASE WHEN row.core = false THEN [1] ELSE [] END | REMOVE n:Core)",
            {**parameters, "rows": items},
            operation="patch_nodes",
        )

    @primitive
    def delete_nodes(
        self,
        spec: NodeSpec,
        keys: Sequence[str],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Delete independent nodes without implicit DETACH or intermediate commits."""
        self._prepare_write(spec, scope, CrudOperation.DELETE)
        items = self._node_batch_rows(spec, keys, unique=True)
        if not items:
            return
        clauses, parameters, _ = self._scope(spec, scope, alias="n")
        clauses.insert(0, "n.key = row.key")
        self._runtime.write(
            f"UNWIND $rows AS row MATCH (n:{spec.name}) WHERE {' AND '.join(clauses)} "
            "DELETE n",
            {**parameters, "rows": items},
            operation="delete_nodes",
        )

    def get_edges(
        self,
        spec: EdgeSpec,
        endpoints: Sequence[tuple[str, str]],
        *,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> tuple[EdgeRecord | None, ...]:
        """Read exact edges, retaining missing and repeated endpoint pairs."""
        self._prepare_read(spec, scope)
        projection = self._property_projection((spec,), fields, alias="r")
        items = self._edge_batch_rows(spec, endpoints)
        if not items:
            return ()
        query, parameters = self._batch_edge_match(spec, scope)
        rows = self._runtime.read(
            f"{query} RETURN row.index AS index, s.key AS source_key, "
            f"t.key AS target_key, {projection} AS properties",
            {**parameters, "rows": items},
            operation="get_edges",
        )
        records = {
            row["index"]: EdgeRecord(
                spec, row["source_key"], row["target_key"], row["properties"]
            )
            for row in rows
        }
        return tuple(records.get(index) for index in range(len(items)))

    @primitive
    def create_edges(
        self,
        spec: EdgeSpec,
        edges: Sequence[tuple[str, str, Mapping[str, object]]],
        *,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> tuple[EdgeRecord, ...]:
        """Write endpoint-qualified edges, rejecting any missing endpoint before commit."""
        items = self._edge_create_rows(spec, edges, scope)
        projection = self._property_projection((spec,), fields, alias="r")
        if not items:
            return ()
        source_scope, source_parameters, _ = self._scope(spec.source, scope, alias="s")
        target_scope, target_parameters, _ = self._scope(spec.target, scope, alias="t")
        clauses = [
            "s.key = row.source_key",
            "t.key = row.target_key",
            *source_scope,
            *target_scope,
        ]
        rows = self._create_batch(
            f"UNWIND $rows AS row MATCH (s:{spec.source.name}), (t:{spec.target.name}) "
            f"WHERE {' AND '.join(clauses)} CREATE (s)-[r:{spec.relationship}]->(t) "
            "SET r = row.properties RETURN row.index AS index, s.key AS source_key, "
            f"t.key AS target_key, {projection} AS properties",
            {**source_parameters, **target_parameters, "rows": items},
            operation="create_edges",
        )
        return tuple(
            EdgeRecord(spec, row["source_key"], row["target_key"], row["properties"])
            for row in rows
        )

    @primitive
    def patch_edges(
        self,
        spec: EdgeSpec,
        patches: Sequence[tuple[str, str, Mapping[str, object]]],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Patch independent edges with per-row change maps."""
        self._prepare_write(spec, scope, CrudOperation.UPDATE)
        items = self._edge_batch_rows(
            spec,
            tuple((source, target) for source, target, _ in patches),
            unique=True,
        )
        for item, (_, _, changes) in zip(items, patches, strict=True):
            changes = self._patch_values(spec, changes)
            item["changes"] = dict(changes)
        if not items:
            return
        query, parameters = self._batch_edge_match(spec, scope)
        self._runtime.write(
            f"{query} SET r += row.changes",
            {**parameters, "rows": items},
            operation="patch_edges",
        )

    @primitive
    def delete_edges(
        self,
        spec: EdgeSpec,
        endpoints: Sequence[tuple[str, str]],
        *,
        scope: ScopeBinding,
    ) -> None:
        """Delete independent edges in one write query."""
        self._prepare_write(spec, scope, CrudOperation.DELETE)
        items = self._edge_batch_rows(spec, endpoints, unique=True)
        if not items:
            return
        query, parameters = self._batch_edge_match(spec, scope)
        self._runtime.write(
            f"{query} DELETE r",
            {**parameters, "rows": items},
            operation="delete_edges",
        )

    def _property_projection(
        self,
        specs: Sequence[NodeSpec | EdgeSpec],
        fields: Sequence[str] | None,
        *,
        alias: str,
    ) -> str:
        """Select properties in Cypher without changing filtering or ordering."""
        selected = self._projection_fields(specs, fields)
        if selected is None:
            return f"properties({alias})"
        return alias + "{" + ", ".join(f".{name}" for name in selected) + "}"

    def _batch_edge_match(
        self,
        spec: EdgeSpec,
        scope: ScopeBinding,
    ) -> tuple[str, dict[str, object]]:
        """Match batch endpoint keys with the same three-owner Scope as single reads."""
        clauses, parameters = self._all_scope(spec, scope)
        clauses.extend(("s.key = row.source_key", "t.key = row.target_key"))
        return (
            (
                f"UNWIND $rows AS row MATCH (s:{spec.source.name})-[r:{spec.relationship}]->"
                f"(t:{spec.target.name}) WHERE {' AND '.join(clauses)}"
            ),
            parameters,
        )

    def _create_batch(
        self,
        query: str,
        parameters: Mapping[str, object],
        *,
        operation: str,
    ) -> Sequence[Mapping[str, object]]:
        """Check every create result before either an ordinary or enclosing commit."""

        def execute(runtime: GraphRuntime) -> Sequence[Mapping[str, object]]:
            rows = runtime.write(query, parameters, operation=operation)
            expected = len(parameters["rows"])
            if len(rows) != expected or {row["index"] for row in rows} != set(
                range(expected)
            ):
                raise StoreError(
                    "batch create requires one visible result for every input",
                    operation=operation,
                    code="missing_endpoint"
                    if operation == "create_edges"
                    else "constraint_violation",
                )
            return tuple(sorted(rows, key=lambda row: row["index"]))

        if isinstance(self._runtime, Neo4jRuntime):
            return self._runtime.write_work(execute, operation=operation)
        return execute(self._runtime)

    def list_nodes(
        self,
        spec: NodeSpec,
        *,
        filters: Mapping[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Read a bounded, key-ordered node page with equality filters."""
        self._prepare_read(spec, scope)
        projection = self._property_projection((spec,), fields, alias="n")
        limit, offset = self._page(limit, offset)
        filter_values = self._filters(spec, filters)
        filter_clauses = []
        parameters: dict[str, object] = {"limit": limit, "offset": offset}
        for index, (name, value) in enumerate(filter_values.items()):
            parameter = f"filter_{index}"
            filter_clauses.append(
                f"n.{name} IS NULL" if value is None else f"n.{name} = ${parameter}"
            )
            parameters[parameter] = value
        scope_clauses, scope_parameters, _ = self._scope(
            spec,
            scope,
            alias="n",
        )
        clauses = [*filter_clauses, *scope_clauses]
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._runtime.read(
            f"MATCH (n:{spec.name}){where} RETURN {projection} AS properties "
            "ORDER BY n.key SKIP $offset LIMIT $limit",
            {**parameters, **scope_parameters},
            operation="list_nodes",
        )
        return tuple(NodeRecord(spec, row["properties"]) for row in rows)

    def search_fulltext(
        self,
        text: str,
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        filters_by_type: Mapping[str, Mapping[str, object]] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Merge full-text index hits by raw score descending."""
        filters_by_spec = self._search_specs(
            specs, Neo4jIndexKind.FULLTEXT, filters, scope, filters_by_type
        )
        specs = tuple(filters_by_spec)
        projection = self._property_projection(specs, fields, alias="node")
        query_text = require_nonempty_text(text, "full-text query")
        # Treat query punctuation as word boundaries, never Lucene operators.
        # Bare boolean words are literal terms; OR below belongs to us.
        query_text = " OR ".join(
            f'"{term}"' if term in {"AND", "OR", "NOT"} else term
            for term in re.split(r'[\s+\-!(){}\[\]^"~*?:\\/&|]+', query_text)
            if term
        )
        limit, _ = self._page(limit, 0)
        if not query_text:
            return ()
        indexes = tuple(
            self._search_index(item, Neo4jIndexKind.FULLTEXT) for item in specs
        )
        ranked: list[tuple[float, NodeRecord]] = []
        for item, index in zip(specs, indexes, strict=True):
            clauses, parameters = self._search_predicates(
                item, filters_by_spec[item], scope
            )
            predicate = " AND ".join((f"node:{item.name}", *clauses))
            rows = self._runtime.read(
                "CALL db.index.fulltext.queryNodes($index_name, $query_text) "
                "YIELD node, score "
                f"WHERE {predicate} "
                f"RETURN {projection} AS properties, score "
                f"ORDER BY score DESC, {self._node_order(alias='node')} "
                "LIMIT $limit",
                {
                    **parameters,
                    "index_name": index.name,
                    "query_text": query_text,
                    "limit": limit,
                },
                operation="search_fulltext",
            )
            ranked.extend(
                (row["score"], NodeRecord(item, row["properties"])) for row in rows
            )
        ranked.sort(key=lambda hit: (-hit[0], hit[1].spec.name, hit[1].key))
        return tuple(record for _, record in ranked[:limit])

    def search_vector(
        self,
        embedding: Sequence[float],
        *,
        specs: Sequence[NodeSpec] | None = None,
        filters: Mapping[str, object] | None = None,
        filters_by_type: Mapping[str, Mapping[str, object]] | None = None,
        limit: int = 20,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[NodeRecord]:
        """Rank visible vectors before limiting, without global ANN starvation."""
        filters_by_spec = self._search_specs(
            specs, Neo4jIndexKind.VECTOR, filters, scope, filters_by_type
        )
        specs = tuple(filters_by_spec)
        projection = self._property_projection(specs, fields, alias="node")
        if not isinstance(embedding, Sequence) or isinstance(embedding, (str, bytes)):
            raise TypeError("query embedding must be a numeric sequence")
        vector = tuple(embedding)
        if not vector:
            raise ValueError("query embedding must be non-empty")
        limit, _ = self._page(limit, 0)
        for item in specs:
            self._validate_values(item, {"embedding": vector})
        ranked: list[tuple[float, NodeRecord]] = []
        for item in specs:
            clauses, parameters = self._search_predicates(
                item, filters_by_spec[item], scope
            )
            predicate = " AND ".join((*clauses, "node.embedding IS NOT NULL"))
            rows = self._runtime.read(
                f"MATCH (node:{item.name}) "
                f"WHERE {predicate} "
                "WITH node, vector.similarity.cosine(node.embedding, $embedding) AS score "
                f"RETURN {projection} AS properties, score "
                f"ORDER BY score DESC, {self._node_order(alias='node')} "
                "LIMIT $limit",
                {
                    **parameters,
                    "embedding": vector,
                    "limit": limit,
                },
                operation="search_vector",
            )
            ranked.extend(
                (row["score"], NodeRecord(item, row["properties"])) for row in rows
            )
        ranked.sort(key=lambda hit: (-hit[0], hit[1].spec.name, hit[1].key))
        return tuple(record for _, record in ranked[:limit])

    def expand_neighbors(
        self,
        node_spec: NodeSpec,
        key: str,
        *,
        direction: NeighborDirection = NeighborDirection.BOTH,
        limit: int = 100,
        offset: int = 0,
        node_fields: Sequence[str] | None = None,
        edge_fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[tuple[NodeRecord, EdgeRecord]]:
        """Return one key-ordered page of direct neighbor connections."""
        self._prepare_read(node_spec, scope)
        try:
            resolved_direction = NeighborDirection(direction)
        except (TypeError, ValueError) as exc:
            raise ValueError("direction must be both, incoming, or outgoing") from exc
        limit, offset = self._page(limit, offset)
        seed_clauses, seed_parameters = self._key_match(
            node_spec,
            key,
            alias="seed",
        )
        seed_scope, seed_scope_parameters, _ = self._scope(
            node_spec,
            scope,
            alias="seed",
        )
        edge_specs = self._neighbor_specs(node_spec, resolved_direction)
        parameters: dict[str, object] = {
            **seed_parameters,
            **seed_scope_parameters,
            "offset": offset,
            "limit": limit,
        }
        if not edge_specs:
            return ()

        neighbor_specs = tuple(
            dict.fromkeys(
                spec
                for edge in edge_specs
                for spec in (edge.source, edge.target)
                if spec is not node_spec or edge.source is edge.target
            )
        )
        node_projection = self._property_projection(
            neighbor_specs, node_fields, alias="neighbor"
        )
        edge_projection = self._property_projection(
            edge_specs, edge_fields, alias="path_rel"
        )
        relationship_types = "|".join(
            sorted({edge.relationship for edge in edge_specs})
        )
        triple_clauses: list[str] = []
        order_cases: list[str] = []
        for index, edge in enumerate(edge_specs):
            clauses = [
                f"type(path_rel) = '{edge.relationship}'",
                f"startNode(path_rel):{edge.source.name}",
                f"endNode(path_rel):{edge.target.name}",
            ]
            for owner, alias, prefix in (
                (edge.source, "startNode(path_rel)", "source"),
                (edge, "path_rel", "edge"),
                (edge.target, "endNode(path_rel)", "target"),
            ):
                scope_clauses, scope_parameters, _ = self._scope(
                    owner, scope, alias=alias, parameter_prefix=f"{prefix}_{index}"
                )
                clauses.extend(scope_clauses)
                parameters.update(scope_parameters)
            triple = " AND ".join(clauses)
            triple_clauses.append(f"({triple})")
            order_values = [f"'{index:04d}'"]
            order_values.append("coalesce(toString(startNode(path_rel).key), '')")
            order_values.append("coalesce(toString(endNode(path_rel).key), '')")
            order_cases.append(f"WHEN {triple} THEN [{', '.join(order_values)}]")
        pattern = {
            NeighborDirection.BOTH: (
                f"(seed)-[path_rel:{relationship_types}]-(neighbor)"
            ),
            NeighborDirection.OUTGOING: (
                f"(seed)-[path_rel:{relationship_types}]->(neighbor)"
            ),
            NeighborDirection.INCOMING: (
                f"(seed)<-[path_rel:{relationship_types}]-(neighbor)"
            ),
        }[resolved_direction]
        edge_order = "CASE " + " ".join(order_cases) + " END"
        query = (
            f"MATCH (seed:{node_spec.name})\n"
            f"WHERE {' AND '.join((*seed_clauses, *seed_scope))}\n"
            f"MATCH {pattern}\n"
            f"WHERE ({' OR '.join(triple_clauses)})\n"
            f"WITH seed, path_rel, neighbor, {edge_order} AS edge_order\n"
            "ORDER BY edge_order\n"
            "SKIP $offset LIMIT $limit\n"
            "RETURN toInteger(edge_order[0]) AS edge_index, "
            "CASE WHEN startNode(path_rel) = seed "
            "THEN 'outgoing' ELSE 'incoming' END AS direction, "
            f"{node_projection} AS properties, "
            f"{edge_projection} AS edge_properties"
        )
        rows = self._runtime.read(
            query,
            parameters,
            operation="expand_neighbors",
        )
        return self._decode_neighbor_rows(
            rows,
            node_spec=node_spec,
            key=key,
            edge_specs=edge_specs,
        )

    def list_edges(
        self,
        spec: EdgeSpec,
        *,
        filters: Mapping[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> Sequence[EdgeRecord]:
        """Read one bounded edge page using edge-property equality filters."""
        self._prepare_read(spec, scope)
        projection = self._property_projection((spec,), fields, alias="r")
        limit, offset = self._page(limit, offset)
        values = self._filters(spec, filters)
        clauses, parameters = self._all_scope(spec, scope)
        parameters.update({"limit": limit, "offset": offset})
        for index, (name, value) in enumerate(values.items()):
            parameter = f"edge_filter_{index}"
            clauses.append(
                f"r.{name} IS NULL" if value is None else f"r.{name} = ${parameter}"
            )
            parameters[parameter] = value
        rows = self._runtime.read(
            f"MATCH (s:{spec.source.name})-[r:{spec.relationship}]->"
            f"(t:{spec.target.name}) WHERE {' AND '.join(clauses)} "
            "RETURN s.key AS source_key, t.key AS target_key, "
            f"{projection} AS properties "
            f"ORDER BY {self._edge_order()} SKIP $offset LIMIT $limit",
            parameters,
            operation="list_edges",
        )
        return tuple(
            EdgeRecord(
                spec,
                row["source_key"],
                row["target_key"],
                row["properties"],
            )
            for row in rows
        )

    def atomic(self, work: Callable[[GraphStore], _T]) -> _T:
        """Run Store calls against one transaction without re-preparing writes."""
        if not isinstance(self._runtime, Neo4jRuntime):
            raise StoreError("nested atomic work is not supported", operation="atomic")
        return self._runtime.write_work(
            lambda runtime: work(
                Neo4jGraphStore(
                    runtime,
                    self._schema,
                    self._access,
                    self._glossary,
                    catalog=self._catalog,
                    owns_runtime=False,
                )
            ),
            operation="atomic",
        )

    def close(self) -> None:
        """Close an owned ordinary runtime and ignore transaction-bound stores."""
        if self._owns_runtime and isinstance(self._runtime, Neo4jRuntime):
            self._runtime.close()

    def _search_index(
        self,
        spec: NodeSpec,
        kind: Neo4jIndexKind,
    ) -> Neo4jIndex:
        """Resolve one node search primitive through the physical catalog."""
        if self._catalog is None:
            raise ValueError("Store has no physical index catalog")
        matches = tuple(
            index
            for index in self._catalog.indexes
            if index.node is spec and index.kind is kind
        )
        if len(matches) != 1:
            raise ValueError(f"{spec.name} requires exactly one {kind.value} index")
        return matches[0]

    def _search_predicates(
        self,
        spec: NodeSpec,
        filters: Mapping[str, object],
        scope: ScopeBinding,
    ) -> tuple[list[str], dict[str, object]]:
        """Compile business equality filters beside mandatory CM Auth scope."""
        clauses, parameters, _ = self._scope(spec, scope, alias="node")
        for position, (name, value) in enumerate(filters.items()):
            parameter = f"search_filter_{position}"
            clauses.append(
                f"node.{name} IS NULL"
                if value is None
                else f"node.{name} = ${parameter}"
            )
            parameters[parameter] = value
        return clauses, parameters

    def _key_match(
        self,
        spec: NodeSpec,
        key: str,
        *,
        alias: str,
    ) -> tuple[list[str], dict[str, object]]:
        """Compile one exact key predicate after canonical value validation."""
        key = require_trimmed_text(key, f"{spec.name} key")
        self._schema.validate_values(spec, {"key": key})
        parameter = f"{alias}_key"
        return [f"{alias}.key = ${parameter}"], {parameter: key}

    def _scope(
        self,
        spec: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
        *,
        alias: str,
        for_write: bool = False,
        parameter_prefix: str | None = None,
    ) -> tuple[list[str], dict[str, object], dict[str, object]]:
        """Compile CM Auth-owned fields for one schema owner and query alias."""
        supplied = self._scope_inputs(spec, scope)
        clauses = []
        parameters = {}
        create_values = {}
        prefix = parameter_prefix or alias
        for name, value in supplied.items():
            if for_write and value is not None:
                create_values[name] = value
            if name == "owner_id":
                if scope.owner_ids:
                    parameter = f"{prefix}_scope_owner_ids"
                    clauses.append(f"{alias}.owner_id IN ${parameter}")
                    parameters[parameter] = scope.owner_ids
                continue
            if name == "datasource_id" and value is None:
                continue
            parameter = f"{prefix}_scope_{name}"
            clauses.append(f"{alias}.{name} = ${parameter}")
            parameters[parameter] = value
        return clauses, parameters, create_values

    def _all_scope(
        self,
        spec: EdgeSpec,
        scope: ScopeBinding,
    ) -> tuple[list[str], dict[str, object]]:
        """Compile source, edge, and target CM Auth predicates once each."""
        clauses = []
        parameters = {}
        for owner, alias in ((spec.source, "s"), (spec, "r"), (spec.target, "t")):
            item_clauses, item_parameters, _ = self._scope(
                owner,
                scope,
                alias=alias,
            )
            clauses.extend(item_clauses)
            parameters.update(item_parameters)
        return clauses, parameters

    @staticmethod
    def _edge_order() -> str:
        """Return deterministic endpoint key ordering for edge pages."""
        return "s.key, t.key"

    @staticmethod
    def _node_order(*, alias: str) -> str:
        """Return deterministic key ordering after equal search scores."""
        return f"{alias}.key"

    def _decode_neighbor_rows(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        node_spec: NodeSpec,
        key: str,
        edge_specs: tuple[EdgeSpec, ...],
    ) -> tuple[tuple[NodeRecord, EdgeRecord], ...]:
        """Decode direct-neighbor rows without reconstructing a subgraph."""
        records: list[tuple[NodeRecord, EdgeRecord]] = []
        for row in rows:
            edge_index = row.get("edge_index")
            if (
                isinstance(edge_index, bool)
                or not isinstance(edge_index, int)
                or not 0 <= edge_index < len(edge_specs)
            ):
                raise StoreError(
                    "neighbor edge index is invalid",
                    operation="expand_neighbors",
                    code="invalid_backend_result",
                )
            try:
                direction = NeighborDirection(row.get("direction"))
            except (TypeError, ValueError) as exc:
                raise StoreError(
                    "neighbor direction is invalid",
                    operation="expand_neighbors",
                    code="invalid_backend_result",
                ) from exc
            if direction is NeighborDirection.BOTH:
                raise StoreError(
                    "neighbor direction is invalid",
                    operation="expand_neighbors",
                    code="invalid_backend_result",
                )
            edge_spec = edge_specs[edge_index]
            outgoing_matches = (
                direction is NeighborDirection.OUTGOING
                and edge_spec.source is node_spec
            )
            incoming_matches = (
                direction is NeighborDirection.INCOMING
                and edge_spec.target is node_spec
            )
            if not outgoing_matches and not incoming_matches:
                raise StoreError(
                    "neighbor direction does not match canonical edge",
                    operation="expand_neighbors",
                    code="invalid_backend_result",
                )
            neighbor_spec = (
                edge_spec.target
                if direction is NeighborDirection.OUTGOING
                else edge_spec.source
            )
            properties = row.get("properties")
            if not isinstance(properties, Mapping):
                raise StoreError(
                    "neighbor properties are invalid",
                    operation="expand_neighbors",
                    code="invalid_backend_result",
                )
            try:
                node = NodeRecord(neighbor_spec, properties)
                neighbor_key = node.key
            except (KeyError, TypeError, ValueError) as exc:
                raise StoreError(
                    "neighbor key is incomplete",
                    operation="expand_neighbors",
                    code="invalid_backend_result",
                ) from exc
            source_key, target_key = (
                (key, neighbor_key) if outgoing_matches else (neighbor_key, key)
            )
            records.append(
                (
                    node,
                    EdgeRecord(
                        edge_spec, source_key, target_key, row["edge_properties"]
                    ),
                )
            )
        return tuple(records)


__all__ = ["Neo4jGraphStore"]

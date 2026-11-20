"""Implement canonical graph primitives using native HoloAge statements.

Current limitations and deferred features:
- Business key uniqueness is not enforced across requests or concurrent writers.
  Repeated targets within one write batch are still rejected.
- Edge uniqueness and stable edge IDs are not provided. Parallel edges are
  allowed; endpoint-based get selects one, while patch/delete match all.
  Selection and pagination of duplicate identities are not backend-equivalent.
- Native multiple labels are unavailable. Entity Core membership uses the
  temporary physical is_core column, synchronized on create/type patch; this
  does not expose a Cypher :Core label or an n.is_core property.
- Search uses content and plot_embedding physical projections. Direct indexing
  of properties.embedding is not implemented here; embedding updates synchronize
  both copies. Tokenization, ANN recall and index visibility may differ from
  Neo4j, so identical search results or immediate visibility are not guaranteed.
- Historical value migration, Core backfill and automatic projection refresh
  after Glossary mapping changes are deferred.
- Personal schemas are allocated by sync; historical personal rows in the
  workspace graph are not migrated. Reads before the first sync require an
  allocated space.
- TODO(transition:global-row-scope): remove global owner/workspace predicates
  after historical personal rows are migrated out of the workspace graph.

Native queries accept complete AGE SQL. The query Service validates model-authored
SQL against the authorized graph schemas before execution. See the package's docs/graph-store-contract-alignment.md for scope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

from psycopg import sql
from pydantic_core import to_json

from ...auth import GLOBAL_OWNER_ID, GraphScopeAuthority, ScopeBinding
from ...schema import (
    GRAPH_SCHEMA,
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
from .catalog import HoloSchemaCatalog
from .physical_schema import build_physical_schema, ensure_schema
from .runtime import HoloRuntime
from .target import HoloTarget, validate_graph_name

_T = TypeVar("_T")

CAPABILITIES = {
    "backend": "holoage",
    "query": "native-age-sql",
    "fulltext": "native-label-content-async-index",
    "vector": "native-label-hgraph",
    "atomic": "hologres-mixed-dml-beta",
    "crash_recovery": "hologres-wal",
    "write_concurrency": "native-table-locks",
    "business_key_uniqueness": "not-enforced",
    "edge_create": "insert-only-no-concurrent-uniqueness",
    "node_delete": "age-reject-incident-edges",
    "core_labels": "physical-is_core-projection",
    "patch_values": "atomic-agtype-map",
}


class HoloGraphStore(GraphStore):
    """Persist one exact GraphSchema through an authorized native runtime."""

    @property
    def backend(self) -> str:
        return "holoage"

    def __init__(
        self,
        runtime: GraphRuntime[HoloTarget],
        schema: GraphSchema,
        access: GraphScopeAuthority,
        glossary: Glossary,
        *,
        catalog: HoloSchemaCatalog | None = None,
        owns_runtime: bool = True,
    ) -> None:
        self._bind(runtime, schema, access, glossary)
        if type(runtime.target) is not HoloTarget:
            raise TypeError("runtime target must be a HoloTarget")
        resolved_catalog = catalog
        if resolved_catalog is None and schema is GRAPH_SCHEMA:
            resolved_catalog = build_physical_schema()
        if resolved_catalog is not None and resolved_catalog.schema is not schema:
            raise ValueError("physical catalog must govern the Store schema")
        self._catalog = resolved_catalog
        self._vector_dimensions = {
            node: resolved_catalog.embed_dim
            for node in schema.nodes
            if resolved_catalog is not None and node.embedding_fields
        }
        self._owns_runtime = owns_runtime

    def _graph_name(self, owner_id: str) -> str:
        target = self._runtime.target
        if owner_id == GLOBAL_OWNER_ID:
            return target.graph_name
        graph = validate_graph_name(
            f"{target.tenant_id}_{target.workspace_id}_{owner_id}"
        )
        if graph == target.graph_name:
            raise PermissionError("personal and global graph names must differ")
        owner = (
            "cm:user-space:"
            + to_json([target.tenant_id, target.workspace_id, owner_id]).decode()
        )
        # Names remain human-readable; the catalog disambiguates component boundaries.
        rows = self._runtime.read(
            "SELECT obj_description(n.oid, 'pg_namespace') AS owner "
            "FROM hologres.hg_ag_graph g "
            "JOIN pg_namespace n ON n.nspname = g.name "
            "WHERE g.name = %(graph)s",
            {"graph": graph},
            operation="require_user_space",
        )
        # CREATE and its owner comment are separate DDL commits. An unmarked
        # graph is not readable until ownership has been established.
        if not rows or rows[0]["owner"] is None:
            raise StoreError(
                "Personal graph has not been initialized",
                operation="require_user_space",
                code="user_space_uninitialized",
            )
        if rows[0]["owner"] == "initializing:" + owner:
            raise StoreError(
                "Personal graph initialization has not completed",
                operation="require_user_space",
                code="user_space_initializing",
            )
        if rows[0]["owner"] != owner:
            raise PermissionError("personal graph does not belong to this owner")
        return graph

    def _read_graphs(self, scope: ScopeBinding) -> tuple[str, ...]:
        """Resolve authorized owners; all-owner scans use persisted readiness."""
        if scope.owner_ids:
            return tuple(self._graph_name(owner) for owner in scope.owner_ids)
        target = self._runtime.target
        rows = self._runtime.read(
            "SELECT g.name FROM hologres.hg_ag_graph g "
            "JOIN pg_namespace n ON n.nspname = g.name "
            "WHERE left(obj_description(n.oid, 'pg_namespace'), length(%(space)s)) = %(space)s "
            "AND g.name <> %(global_graph)s "
            "ORDER BY g.name",
            {
                "space": "cm:user-space:"
                + to_json([target.tenant_id, target.workspace_id]).decode()[:-1]
                + ",",
                "global_graph": target.graph_name,
            },
            operation="list_user_spaces",
        )
        return (target.graph_name, *(row["name"] for row in rows))

    def initialize_user_space(self, scope: ScopeBinding) -> None:
        super().initialize_user_space(scope)
        if not isinstance(self._runtime, HoloRuntime):
            raise StoreError(
                "user space allocation requires an unbound runtime",
                operation="initialize_user_space",
            )
        if self._catalog is None:
            raise ValueError("user space allocation requires a physical catalog")
        graph = validate_graph_name(
            f"{scope.tenant_id}_{scope.workspace_id}_{scope.write_owner_id}"
        )
        if graph == self._runtime.target.graph_name:
            raise ValueError("personal and global graph names must differ")
        self._runtime.prepare_schema(
            lambda execute: ensure_schema(
                graph,
                self._catalog,
                execute,
                user_space="cm:user-space:"
                + to_json(
                    [scope.tenant_id, scope.workspace_id, scope.write_owner_id]
                ).decode(),
            )
        )

    @property
    def capabilities(self) -> Mapping[str, str]:
        return dict(CAPABILITIES)

    def health(self) -> Mapping[str, object]:
        rows = self._runtime.read("SELECT 1 AS ok", {}, operation="health")
        return {"ok": bool(rows and rows[0].get("ok") == 1)}

    def get_nodes(
        self,
        spec: NodeSpec,
        keys: Sequence[str],
        *,
        fields: Sequence[str] | None = None,
        scope: ScopeBinding,
    ) -> tuple[NodeRecord | None, ...]:
        self._prepare_read(spec, scope)
        items = self._node_batch_rows(spec, keys)
        selected = self._projection_fields((spec,), fields)
        if not items:
            return ()

        label = spec.name.replace("`", "``")
        projection = (
            "properties(n)"
            if selected is None
            else "{" + ", ".join(f"`{name}`: n.`{name}`" for name in selected) + "}"
        )

        branches, bindings = [], {}
        for graph_index, graph_name in enumerate(self._read_graphs(scope)):
            graph = sql.Literal(graph_name).as_string().replace("%", "%%")
            scoped = self._scope_inputs(spec, scope)
            values = {"datasource_id": scoped.get("datasource_id")}
            global_conditions = ""
            if graph_name == self._runtime.target.graph_name:
                scoped["owner_id"] = GLOBAL_OWNER_ID
                for name in ("workspace_id", "owner_id"):
                    if name in scoped:
                        values[name] = scoped[name]
                        global_conditions += f" AND n.{name} = $scope.{name}"
            bindings[f"params_{graph_index}"] = to_json(
                {"keys": [item["key"] for item in items], "scope": values},
                fallback=dict,
            ).decode()
            branches.append(f"""SELECT ag_catalog.agtype_to_json(p) AS properties, node_id, {graph}::text AS graph_name
            FROM ag_catalog.cypher({graph}, $$
                MATCH (n:`{label}`) WHERE n.key IN $keys
                  {global_conditions}
                  AND ($scope.datasource_id IS NULL OR n.datasource_id = $scope.datasource_id)
                RETURN {projection}, id(n)
            $$, %(params_{graph_index})s::ag_catalog.agtype) AS result(p ag_catalog.agtype, node_id ag_catalog.agtype)""")
        rows = self._runtime.read(
            f"SELECT properties FROM ({' UNION ALL '.join(branches)}) hits ORDER BY node_id, graph_name",
            bindings,
            operation="get_nodes",
        )
        records = {}
        for row in rows:
            properties = row["properties"]
            for name in selected or ():
                properties.setdefault(name, None)
            if properties["key"] not in records:
                records[properties["key"]] = NodeRecord(spec, properties)
        return tuple(records.get(item["key"]) for item in items)

    @primitive
    def create_nodes(
        self,
        spec: NodeSpec,
        properties: Sequence[Mapping[str, object]],
        *,
        scope: ScopeBinding,
    ) -> tuple[str, ...]:
        items = self._node_create_rows(spec, properties, scope)
        if not items:
            return ()
        table = (
            sql.Identifier(self._graph_name(scope.write_owner_id), spec.name)
            .as_string()
            .replace("%", "%%")
        )
        columns = ["properties"]
        # TODO(transition:core-label-projection): retire this projection once
        # native labels or replacement Core queries pass acceptance.
        if spec.name == "Entity":
            columns.append("is_core")
        if spec.fulltext_fields:
            columns.append("content")
        # TODO(transition:embedding-index-projection): 预计未来移除；
        # sync both copies until property indexing or a single-copy migration.
        if spec.embedding_fields:
            columns.append("plot_embedding")
        values, parameters = [], {}
        for index, item in enumerate(items):
            properties = item["properties"]
            row = [f"%(properties_{index})s::ag_catalog.agtype"]
            parameters[f"properties_{index}"] = to_json(
                properties, fallback=dict
            ).decode()
            if spec.name == "Entity":
                row.append(f"%(core_{index})s::boolean")
                parameters[f"core_{index}"] = "Core" in item["labels"]
            if spec.fulltext_fields:
                pieces = []
                for name in spec.fulltext_fields:
                    value = properties.get(name)
                    if isinstance(value, str):
                        pieces.append(value)
                    elif value is not None:
                        pieces.extend(value)
                row.append(f"%(content_{index})s::text")
                parameters[f"content_{index}"] = "\n".join(pieces) or None
            if spec.embedding_fields:
                embedding = properties.get("embedding")
                row.append(f"%(embedding_{index})s::float4[]")
                parameters[f"embedding_{index}"] = (
                    None if embedding is None else list(embedding)
                )
            values.append("(" + ", ".join(row) + ")")
        self._runtime.write(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES {', '.join(values)}",
            parameters,
            operation="create_nodes",
        )
        return tuple(item["key"] for item in items)

    @primitive
    def patch_nodes(
        self,
        spec: NodeSpec,
        patches: Sequence[tuple[str, Mapping[str, object]]],
        *,
        scope: ScopeBinding,
    ) -> None:
        self._prepare_write(spec, scope, CrudOperation.UPDATE)
        items = self._node_batch_rows(spec, [key for key, _ in patches], unique=True)
        values, parameters, alternatives = [], {}, []
        merged = "ag_catalog.agtype_concat(n.properties, i.changes)"
        for index, (item, (_, patch)) in enumerate(zip(items, patches, strict=True)):
            changes = self._patch_values(spec, patch)
            labels = self._tag_labels(spec, changes)
            embedding = changes.get("embedding")
            expression = merged
            for name, value in changes.items():
                if value is None:
                    expression = f"ag_catalog.agtype_delete_key({expression}, {sql.Literal(name).as_string()})"
            if expression != merged:
                alternatives.append(f"WHEN {index} THEN {expression}")
            values.append(
                f"({index}, %(key_{index})s::text, %(changes_{index})s::ag_catalog.agtype, %(has_vector_{index})s::boolean, %(vector_{index})s::float4[], %(core_{index})s::boolean)"
            )
            parameters.update(
                {
                    f"key_{index}": item["key"],
                    f"changes_{index}": to_json(changes, fallback=dict).decode(),
                    f"has_vector_{index}": "embedding" in changes,
                    f"vector_{index}": None if embedding is None else list(embedding),
                    f"core_{index}": "Core" in labels if "type" in changes else None,
                }
            )
        if not items:
            return
        if alternatives:
            merged = "CASE i.index " + " ".join(alternatives) + f" ELSE {merged} END"
        assignments = [f"properties = {merged}"]
        # TODO(transition:core-label-projection): keep type and its projection
        # in one write until native labels or replacement queries are accepted.
        if spec.name == "Entity":
            assignments.append("is_core = COALESCE(i.is_core, n.is_core)")
        if spec.fulltext_fields:
            pieces = [
                f"ag_catalog.agtype_object_field_text({merged}, {sql.Literal(name).as_string()})"
                for name in spec.fulltext_fields
            ]
            assignments.append(
                "content = NULLIF(concat_ws(E'\\n', " + ", ".join(pieces) + "), '')"
            )
        # TODO(transition:embedding-index-projection): 预计未来移除；
        # sync both copies until property indexing or a single-copy migration.
        if spec.embedding_fields:
            assignments.append(
                "plot_embedding = CASE WHEN i.has_vector THEN i.vector ELSE n.plot_embedding END"
            )
        table = (
            sql.Identifier(self._graph_name(scope.write_owner_id), spec.name)
            .as_string()
            .replace("%", "%%")
        )
        conditions = [
            "ag_catalog.agtype_object_field_text(n.properties, 'key') = i.key"
        ]

        scoped = self._scope_inputs(spec, scope)
        scope_fields = (
            ("workspace_id", "owner_id", "datasource_id")
            if scope.write_owner_id == GLOBAL_OWNER_ID
            else ("datasource_id",)
        )
        for name in scope_fields:
            value = scoped.get(name)
            if value is not None:
                parameters[f"n_{name}"] = value
                conditions.append(
                    f"ag_catalog.agtype_object_field_text(n.properties, '{name}') = %(n_{name})s"
                )

        self._runtime.write(
            f"""UPDATE {table} n SET {", ".join(assignments)}
                FROM (VALUES {", ".join(values)}) AS i(index, key, changes, has_vector, vector, is_core)
                WHERE {" AND ".join(conditions)}""",
            parameters,
            operation="patch_nodes",
        )

    @primitive
    def delete_nodes(
        self, spec: NodeSpec, keys: Sequence[str], *, scope: ScopeBinding
    ) -> None:
        self._prepare_write(spec, scope, CrudOperation.DELETE)
        items = self._node_batch_rows(spec, keys, unique=True)
        if not items:
            return
        conditions = [f"n.key IN {to_json([item['key'] for item in items]).decode()}"]
        scoped = self._scope_inputs(spec, scope)
        scope_fields = (
            ("workspace_id", "owner_id", "datasource_id")
            if scope.write_owner_id == GLOBAL_OWNER_ID
            else ("datasource_id",)
        )
        for name in scope_fields:
            value = scoped.get(name)
            if value is not None:
                conditions.append(f"n.`{name}` = {to_json(value).decode()}")
        label = spec.name.replace("`", "``")
        query = f"MATCH (n:`{label}`) WHERE {' AND '.join(conditions)} DELETE n"
        # Holo Cypher DML rejects parameters. JSON quotes values for Cypher;
        # a non-colliding SQL delimiter keeps values out of the surrounding SQL.
        delimiter = "$cm$"
        while delimiter in query:
            delimiter = delimiter[:-1] + "_" + "$"
        graph = sql.Literal(self._graph_name(scope.write_owner_id)).as_string()
        self._runtime.write(
            f"SELECT * FROM ag_catalog.cypher({graph}, {delimiter}{query}{delimiter}) AS result(value ag_catalog.agtype)",
            {},
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
        self._prepare_read(spec, scope)
        items = self._edge_batch_rows(spec, endpoints)
        selected = self._projection_fields((spec,), fields)
        if not items:
            return ()
        parameters = {"items": items}

        source, relationship, target = (
            name.replace("`", "``")
            for name in (spec.source.name, spec.relationship, spec.target.name)
        )
        projection = (
            "properties(r)"
            if selected is None
            else "{" + ", ".join(f"`{name}`: r.`{name}`" for name in selected) + "}"
        )

        branches, bindings = [], {}
        for graph_index, graph_name in enumerate(self._read_graphs(scope)):
            graph = sql.Literal(graph_name).as_string().replace("%", "%%")
            global_conditions = ""
            for name, alias, owner in (
                ("source_scope", "s", spec.source),
                ("target_scope", "t", spec.target),
                ("edge_scope", "r", spec),
            ):
                scoped = self._scope_inputs(owner, scope)
                values = {"datasource_id": scoped.get("datasource_id")}
                if graph_name == self._runtime.target.graph_name:
                    scoped["owner_id"] = GLOBAL_OWNER_ID
                    for field in ("workspace_id", "owner_id"):
                        if field in scoped:
                            values[field] = scoped[field]
                            global_conditions += (
                                f" AND {alias}.{field} = ${name}.{field}"
                            )
                parameters[name] = values
            bindings[f"params_{graph_index}"] = to_json(
                parameters, fallback=dict
            ).decode()
            branches.append(f"""SELECT ag_catalog.agtype_to_json(p) AS properties,
                   ag_catalog.agtype_to_json(identity) AS identity, edge_id, {graph}::text AS graph_name
            FROM ag_catalog.cypher({graph}, $$
                UNWIND $items AS item
                MATCH (s:`{source}`)-[r:`{relationship}`]->(t:`{target}`)
                WHERE s.key = item.source_key AND t.key = item.target_key
                  {global_conditions}
                  AND ($source_scope.datasource_id IS NULL OR s.datasource_id = $source_scope.datasource_id)
                  AND ($target_scope.datasource_id IS NULL OR t.datasource_id = $target_scope.datasource_id)
                  AND ($edge_scope.datasource_id IS NULL OR r.datasource_id = $edge_scope.datasource_id)
                RETURN {projection}, {{index: item.index, source_key: s.key, target_key: t.key}}, id(r)
            $$, %(params_{graph_index})s::ag_catalog.agtype)
            AS result(p ag_catalog.agtype, identity ag_catalog.agtype, edge_id ag_catalog.agtype)""")
        rows = self._runtime.read(
            f"SELECT properties, identity FROM ({' UNION ALL '.join(branches)}) hits ORDER BY edge_id, graph_name",
            bindings,
            operation="get_edges",
        )
        records = {}
        for row in rows:
            identity = row["identity"]
            for name in selected or ():
                row["properties"].setdefault(name, None)
            if identity["index"] not in records:
                records[identity["index"]] = EdgeRecord(
                    spec,
                    identity["source_key"],
                    identity["target_key"],
                    row["properties"],
                )
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
        items = self._edge_create_rows(spec, edges, scope)
        selected = self._projection_fields((spec,), fields)
        if not items:
            return ()
        graph = self._graph_name(scope.write_owner_id)
        table, source, target = (
            sql.Identifier(graph, name).as_string().replace("%", "%%")
            for name in (spec.relationship, spec.source.name, spec.target.name)
        )
        parameters, values = {}, []
        for index, item in enumerate(items):
            values.append(
                f"(%(source_{index})s::text, %(target_{index})s::text, %(properties_{index})s::ag_catalog.agtype)"
            )
            parameters.update(
                {
                    f"source_{index}": item["source_key"],
                    f"target_{index}": item["target_key"],
                    f"properties_{index}": to_json(
                        item["properties"], fallback=dict
                    ).decode(),
                }
            )
        source_conditions = [
            "ag_catalog.agtype_object_field_text(s.properties, 'key') = i.source_key"
        ]
        target_conditions = [
            "ag_catalog.agtype_object_field_text(t.properties, 'key') = i.target_key"
        ]
        for alias, owner, conditions in (
            ("s", spec.source, source_conditions),
            ("t", spec.target, target_conditions),
        ):
            scoped = self._scope_inputs(owner, scope)
            scope_fields = (
                ("workspace_id", "owner_id", "datasource_id")
                if scope.write_owner_id == GLOBAL_OWNER_ID
                else ("datasource_id",)
            )
            for name in scope_fields:
                value = scoped.get(name)
                if value is not None:
                    parameters[f"{alias}_{name}"] = value
                    conditions.append(
                        f"ag_catalog.agtype_object_field_text({alias}.properties, '{name}') = %({alias}_{name})s"
                    )
        # Missing or invisible endpoints violate native NOT NULL, rolling back the batch.
        self._runtime.write(
            f"""INSERT INTO {table} (start_id, end_id, properties)
                SELECT s.id, t.id, i.properties
                FROM (VALUES {", ".join(values)}) AS i(source_key, target_key, properties)
                LEFT JOIN {source} s ON {" AND ".join(source_conditions)}
                LEFT JOIN {target} t ON {" AND ".join(target_conditions)}""",
            parameters,
            operation="create_edges",
        )
        return tuple(
            EdgeRecord(
                spec,
                item["source_key"],
                item["target_key"],
                item["properties"]
                if selected is None
                else {name: item["properties"].get(name) for name in selected},
            )
            for item in items
        )

    @primitive
    def patch_edges(
        self,
        spec: EdgeSpec,
        patches: Sequence[tuple[str, str, Mapping[str, object]]],
        *,
        scope: ScopeBinding,
    ) -> None:
        self._prepare_write(spec, scope, CrudOperation.UPDATE)
        items = self._edge_batch_rows(
            spec, [(s, t) for s, t, _ in patches], unique=True
        )
        parameters, values, alternatives = {}, [], []
        merged = "ag_catalog.agtype_concat(r.properties, i.changes)"
        for index, (item, (_, _, patch)) in enumerate(zip(items, patches, strict=True)):
            changes = self._patch_values(spec, patch)
            expression = merged
            for name, value in changes.items():
                if value is None:
                    expression = f"ag_catalog.agtype_delete_key({expression}, {sql.Literal(name).as_string()})"
            if expression != merged:
                alternatives.append(f"WHEN {index} THEN {expression}")
            values.append(
                f"({index}, %(source_{index})s::text, %(target_{index})s::text, %(changes_{index})s::ag_catalog.agtype)"
            )
            parameters.update(
                {
                    f"source_{index}": item["source_key"],
                    f"target_{index}": item["target_key"],
                    f"changes_{index}": to_json(changes, fallback=dict).decode(),
                }
            )
        if not items:
            return
        if alternatives:
            merged = "CASE i.index " + " ".join(alternatives) + f" ELSE {merged} END"

        graph = self._graph_name(scope.write_owner_id)
        table, source, target = (
            sql.Identifier(graph, name).as_string().replace("%", "%%")
            for name in (spec.relationship, spec.source.name, spec.target.name)
        )
        conditions = [
            "r.start_id = s.id",
            "r.end_id = t.id",
            "ag_catalog.agtype_object_field_text(s.properties, 'key') = i.source_key",
            "ag_catalog.agtype_object_field_text(t.properties, 'key') = i.target_key",
        ]

        for alias, owner in (("s", spec.source), ("t", spec.target), ("r", spec)):
            scoped = self._scope_inputs(owner, scope)
            scope_fields = (
                ("workspace_id", "owner_id", "datasource_id")
                if scope.write_owner_id == GLOBAL_OWNER_ID
                else ("datasource_id",)
            )
            for name in scope_fields:
                value = scoped.get(name)
                if value is not None:
                    parameters[f"{alias}_{name}"] = value
                    conditions.append(
                        f"ag_catalog.agtype_object_field_text({alias}.properties, '{name}') = %({alias}_{name})s"
                    )

        self._runtime.write(
            f"""UPDATE {table} r SET properties = {merged}
                FROM {source} s, {target} t, (VALUES {", ".join(values)}) AS i(index, source_key, target_key, changes)
                WHERE {" AND ".join(conditions)}""",
            parameters,
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
        self._prepare_write(spec, scope, CrudOperation.DELETE)
        items = self._edge_batch_rows(spec, endpoints, unique=True)
        if not items:
            return
        parameters, values = {}, []
        for index, item in enumerate(items):
            values.append(f"(%(source_{index})s::text, %(target_{index})s::text)")
            parameters.update(
                {
                    f"source_{index}": item["source_key"],
                    f"target_{index}": item["target_key"],
                }
            )

        graph = self._graph_name(scope.write_owner_id)
        table, source, target = (
            sql.Identifier(graph, name).as_string().replace("%", "%%")
            for name in (spec.relationship, spec.source.name, spec.target.name)
        )
        conditions = [
            "r.start_id = s.id",
            "r.end_id = t.id",
            "ag_catalog.agtype_object_field_text(s.properties, 'key') = i.source_key",
            "ag_catalog.agtype_object_field_text(t.properties, 'key') = i.target_key",
        ]

        for alias, owner in (("s", spec.source), ("t", spec.target), ("r", spec)):
            scoped = self._scope_inputs(owner, scope)
            scope_fields = (
                ("workspace_id", "owner_id", "datasource_id")
                if scope.write_owner_id == GLOBAL_OWNER_ID
                else ("datasource_id",)
            )
            for name in scope_fields:
                value = scoped.get(name)
                if value is not None:
                    parameters[f"{alias}_{name}"] = value
                    conditions.append(
                        f"ag_catalog.agtype_object_field_text({alias}.properties, '{name}') = %({alias}_{name})s"
                    )

        self._runtime.write(
            f"""DELETE FROM {table} r
                USING {source} s, {target} t, (VALUES {", ".join(values)}) AS i(source_key, target_key)
                WHERE {" AND ".join(conditions)}""",
            parameters,
            operation="delete_edges",
        )

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
        self._prepare_read(spec, scope)
        limit, offset = self._page(limit, offset)
        selected = self._projection_fields((spec,), fields)
        filters = self._filters(spec, filters)

        label = spec.name.replace("`", "``")
        projection = (
            "properties(n)"
            if selected is None
            else "{" + ", ".join(f"`{name}`: n.`{name}`" for name in selected) + "}"
        )

        conditions = "".join(
            f" AND n.`{name}` IS NULL"
            if value is None
            else f" AND n.`{name}` = $filters.`{name}`"
            for name, value in filters.items()
        )
        branches, bindings = [], {}
        for graph_index, graph_name in enumerate(self._read_graphs(scope)):
            graph = sql.Literal(graph_name).as_string().replace("%", "%%")
            scoped = self._scope_inputs(spec, scope)
            values = {"datasource_id": scoped.get("datasource_id")}
            global_conditions = ""
            if graph_name == self._runtime.target.graph_name:
                scoped["owner_id"] = GLOBAL_OWNER_ID
                for name in ("workspace_id", "owner_id"):
                    if name in scoped:
                        values[name] = scoped[name]
                        global_conditions += f" AND n.{name} = $scope.{name}"
            bindings[f"params_{graph_index}"] = to_json(
                {"scope": values, "filters": filters}, fallback=dict
            ).decode()
            branches.append(f"""SELECT ag_catalog.agtype_to_json(p) AS properties, node_key, node_id, {graph}::text AS graph_name
            FROM ag_catalog.cypher({graph}, $$
                MATCH (n:`{label}`) WHERE true
                  {global_conditions}
                  AND ($scope.datasource_id IS NULL OR n.datasource_id = $scope.datasource_id)
                {conditions}
                RETURN {projection}, n.key, id(n)
            $$, %(params_{graph_index})s::ag_catalog.agtype) AS result(p ag_catalog.agtype, node_key ag_catalog.agtype, node_id ag_catalog.agtype)""")
        bindings.update(limit=limit, offset=offset)
        rows = self._runtime.read(
            f"SELECT properties FROM ({' UNION ALL '.join(branches)}) hits ORDER BY node_key, node_id, graph_name LIMIT %(limit)s OFFSET %(offset)s",
            bindings,
            operation="list_nodes",
        )
        for row in rows:
            for name in selected or ():
                row["properties"].setdefault(name, None)
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
        filters_by_spec = self._search_specs(
            specs, "fulltext", filters, scope, filters_by_type
        )
        specs = tuple(filters_by_spec)
        text = require_nonempty_text(text, "full-text query")
        limit, _ = self._page(limit, 0)
        selected = self._projection_fields(specs, fields)
        if not specs:
            return ()
        parameters = {"limit": limit, "text": text}
        branches = []
        for graph_name in self._read_graphs(scope):
            for index, item in enumerate(specs):
                table = (
                    sql.Identifier(graph_name, item.name).as_string().replace("%", "%%")
                )
                projection = (
                    "n.properties"
                    if selected is None
                    else "ag_catalog.agtype_build_map("
                    + ", ".join(
                        f"{sql.Literal(name).as_string()}, ag_catalog.agtype_object_field(n.properties, {sql.Literal(name).as_string()})"
                        for name in selected
                    )
                    + ")"
                )
                score = "text_search(n.content, %(text)s)"
                conditions = [f"{score} > 0"]
                scoped = self._scope_inputs(item, scope)
                scope_fields = ("datasource_id",)
                if graph_name == self._runtime.target.graph_name:
                    scope_fields += ("workspace_id", "owner_id")
                    if "owner_id" in scoped:
                        scoped["owner_id"] = GLOBAL_OWNER_ID
                for name in scope_fields:
                    value = scoped.get(name)
                    if value is not None:
                        parameters[f"scope_{index}_{name}"] = value
                        conditions.append(
                            f"ag_catalog.agtype_object_field_text(n.properties, '{name}') = %(scope_{index}_{name})s"
                        )
                for field_index, (name, value) in enumerate(
                    filters_by_spec[item].items()
                ):
                    parameters[f"filter_{index}_{field_index}"] = to_json(
                        value, fallback=dict
                    ).decode()
                    conditions.append(
                        f"ag_catalog.agtype_object_field(n.properties, {sql.Literal(name).as_string()}) IS NULL"
                        if value is None
                        else f"ag_catalog.agtype_object_field(n.properties, {sql.Literal(name).as_string()}) = %(filter_{index}_{field_index})s::ag_catalog.agtype"
                    )
                branches.append(f"""(SELECT {index} AS spec_index, {sql.Literal(item.name).as_string()} AS spec_name, ag_catalog.agtype_to_json({projection}) AS properties,
                                           {score} AS score, ag_catalog.agtype_object_field_text(n.properties, 'key') AS key, n.id, {sql.Literal(graph_name).as_string().replace("%", "%%")}::text AS graph_name
                                    FROM {table} n WHERE {" AND ".join(conditions)}
                                    ORDER BY score DESC, key, n.id LIMIT %(limit)s)""")
        rows = self._runtime.read(
            f"SELECT spec_index, properties FROM ({' UNION ALL '.join(branches)}) hits ORDER BY score DESC, spec_name, key, id, graph_name LIMIT %(limit)s",
            parameters,
            operation="search_fulltext",
        )
        for row in rows:
            for name in selected or ():
                row["properties"].setdefault(name, None)
        return tuple(
            NodeRecord(specs[row["spec_index"]], row["properties"]) for row in rows
        )

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
        filters_by_spec = self._search_specs(
            specs, "vector", filters, scope, filters_by_type
        )
        specs = tuple(filters_by_spec)
        if not isinstance(embedding, Sequence) or isinstance(embedding, (str, bytes)):
            raise TypeError("query embedding must be a numeric sequence")
        embedding = tuple(embedding)
        if not embedding:
            raise ValueError("query embedding must be non-empty")
        for item in specs:
            self._validate_values(item, {"embedding": embedding})
        limit, _ = self._page(limit, 0)
        selected = self._projection_fields(specs, fields)
        if not specs:
            return ()
        parameters = {"limit": limit, "embedding": list(embedding)}
        branches = []
        for graph_name in self._read_graphs(scope):
            for index, item in enumerate(specs):
                table = (
                    sql.Identifier(graph_name, item.name).as_string().replace("%", "%%")
                )
                projection = (
                    "n.properties"
                    if selected is None
                    else "ag_catalog.agtype_build_map("
                    + ", ".join(
                        f"{sql.Literal(name).as_string()}, ag_catalog.agtype_object_field(n.properties, {sql.Literal(name).as_string()})"
                        for name in selected
                    )
                    + ")"
                )
                # TODO(transition:embedding-index-projection): replace this column read
                # after property indexing or a single-copy migration.
                score = (
                    "approx_cosine_distance(n.plot_embedding, %(embedding)s::float4[])"
                )
                conditions = ["n.plot_embedding IS NOT NULL"]
                scoped = self._scope_inputs(item, scope)
                scope_fields = ("datasource_id",)
                if graph_name == self._runtime.target.graph_name:
                    scope_fields += ("workspace_id", "owner_id")
                    if "owner_id" in scoped:
                        scoped["owner_id"] = GLOBAL_OWNER_ID
                for name in scope_fields:
                    value = scoped.get(name)
                    if value is not None:
                        parameters[f"scope_{index}_{name}"] = value
                        conditions.append(
                            f"ag_catalog.agtype_object_field_text(n.properties, '{name}') = %(scope_{index}_{name})s"
                        )
                for field_index, (name, value) in enumerate(
                    filters_by_spec[item].items()
                ):
                    parameters[f"filter_{index}_{field_index}"] = to_json(
                        value, fallback=dict
                    ).decode()
                    conditions.append(
                        f"ag_catalog.agtype_object_field(n.properties, {sql.Literal(name).as_string()}) IS NULL"
                        if value is None
                        else f"ag_catalog.agtype_object_field(n.properties, {sql.Literal(name).as_string()}) = %(filter_{index}_{field_index})s::ag_catalog.agtype"
                    )
                branches.append(f"""(SELECT {index} AS spec_index, {sql.Literal(item.name).as_string()} AS spec_name, ag_catalog.agtype_to_json({projection}) AS properties,
                                           {score} AS score, ag_catalog.agtype_object_field_text(n.properties, 'key') AS key, n.id, {sql.Literal(graph_name).as_string().replace("%", "%%")}::text AS graph_name
                                    FROM {table} n WHERE {" AND ".join(conditions)}
                                    ORDER BY score DESC, key, n.id LIMIT %(limit)s)""")
        rows = self._runtime.read(
            f"SELECT spec_index, properties FROM ({' UNION ALL '.join(branches)}) hits ORDER BY score DESC, spec_name, key, id, graph_name LIMIT %(limit)s",
            parameters,
            operation="search_vector",
        )
        for row in rows:
            for name in selected or ():
                row["properties"].setdefault(name, None)
        return tuple(
            NodeRecord(specs[row["spec_index"]], row["properties"]) for row in rows
        )

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
        self._prepare_read(node_spec, scope)
        direction = NeighborDirection(direction)
        limit, offset = self._page(limit, offset)
        key = require_trimmed_text(key, f"{node_spec.name} key")
        self._schema.validate_values(node_spec, {"key": key})
        specs = self._neighbor_specs(node_spec, direction)
        neighbor_specs = tuple(
            dict.fromkeys(
                owner
                for edge in specs
                for owner in (edge.source, edge.target)
                if owner is not node_spec or edge.source is edge.target
            )
        )
        selected_nodes = self._projection_fields(neighbor_specs, node_fields)
        selected_edges = self._projection_fields(specs, edge_fields)
        if not specs:
            return ()
        branches = []
        parameters = {
            "limit": limit,
            "offset": offset,
        }
        for graph_index, graph_name in enumerate(self._read_graphs(scope)):
            graph = sql.Literal(graph_name).as_string().replace("%", "%%")
            for index, edge in enumerate(specs):
                values = {
                    "key": key,
                    "outgoing": edge.source is node_spec
                    and direction is not NeighborDirection.INCOMING,
                    "incoming": edge.target is node_spec
                    and direction is not NeighborDirection.OUTGOING,
                }
                global_conditions = ""
                for name, alias, owner in (
                    ("source_scope", "s", edge.source),
                    ("target_scope", "t", edge.target),
                    ("edge_scope", "r", edge),
                ):
                    scoped = self._scope_inputs(owner, scope)
                    owner_scope = {"datasource_id": scoped.get("datasource_id")}
                    if graph_name == self._runtime.target.graph_name:
                        scoped["owner_id"] = GLOBAL_OWNER_ID
                        for field in ("workspace_id", "owner_id"):
                            if field in scoped:
                                owner_scope[field] = scoped[field]
                                global_conditions += (
                                    f" AND {alias}.{field} = ${name}.{field}"
                                )
                    values[name] = owner_scope
                source, relationship, target = (
                    name.replace("`", "``")
                    for name in (edge.source.name, edge.relationship, edge.target.name)
                )
                edge_projection = (
                    "properties(r)"
                    if selected_edges is None
                    else "{"
                    + ", ".join(f"`{name}`: r.`{name}`" for name in selected_edges)
                    + "}"
                )
                source_projection = (
                    "properties(s)"
                    if selected_nodes is None
                    else "{"
                    + ", ".join(f"`{name}`: s.`{name}`" for name in selected_nodes)
                    + "}"
                )
                target_projection = (
                    "properties(t)"
                    if selected_nodes is None
                    else "{"
                    + ", ".join(f"`{name}`: t.`{name}`" for name in selected_nodes)
                    + "}"
                )
                cypher = f"""
                    MATCH (s:`{source}`)-[r:`{relationship}`]->(t:`{target}`)
                    WHERE (($outgoing AND s.key = $key) OR ($incoming AND t.key = $key))
                      {global_conditions}
                      AND ($source_scope.datasource_id IS NULL OR s.datasource_id = $source_scope.datasource_id)
                      AND ($target_scope.datasource_id IS NULL OR t.datasource_id = $target_scope.datasource_id)
                      AND ($edge_scope.datasource_id IS NULL OR r.datasource_id = $edge_scope.datasource_id)
                    RETURN {edge_projection},
                           CASE WHEN $outgoing AND s.key = $key THEN {target_projection} ELSE {source_projection} END,
                           {{source_key: s.key, target_key: t.key}},
                           id(r)
                """
                query = "$$" + cypher + "$$"
                parameters[f"params_{graph_index}_{index}"] = to_json(values).decode()
                branches.append(f"""
                    SELECT {index}::integer AS spec_index,
                           ag_catalog.agtype_to_json(p) AS properties,
                           ag_catalog.agtype_to_json(neighbor) AS neighbor_properties,
                           ag_catalog.agtype_to_json(identity) AS identity,
                           edge_id, {graph}::text AS graph_name
                    FROM ag_catalog.cypher({graph}, {query}, %(params_{graph_index}_{index})s::ag_catalog.agtype)
                         AS result(p ag_catalog.agtype, neighbor ag_catalog.agtype,
                                   identity ag_catalog.agtype, edge_id ag_catalog.agtype)
                """)
        rows = self._runtime.read(
            f"""
            SELECT spec_index, identity, properties, neighbor_properties
            FROM ({" UNION ALL ".join(branches)}) neighbors
            ORDER BY spec_index, identity ->> 'source_key', identity ->> 'target_key', edge_id, graph_name
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            parameters,
            operation="expand_neighbors",
        )
        for row in rows:
            for name in selected_nodes or ():
                row["neighbor_properties"].setdefault(name, None)
            for name in selected_edges or ():
                row["properties"].setdefault(name, None)
        return tuple(
            (
                NodeRecord(
                    specs[row["spec_index"]].target
                    if (
                        row["identity"]["source_key"] == key
                        and specs[row["spec_index"]].source is node_spec
                        and direction is not NeighborDirection.INCOMING
                    )
                    else specs[row["spec_index"]].source,
                    row["neighbor_properties"],
                ),
                EdgeRecord(
                    specs[row["spec_index"]],
                    row["identity"]["source_key"],
                    row["identity"]["target_key"],
                    row["properties"],
                ),
            )
            for row in rows
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
        self._prepare_read(spec, scope)
        limit, offset = self._page(limit, offset)
        selected = self._projection_fields((spec,), fields)
        filters = self._filters(spec, filters)
        parameters = {"filters": filters}

        source, relationship, target = (
            name.replace("`", "``")
            for name in (spec.source.name, spec.relationship, spec.target.name)
        )
        projection = (
            "properties(r)"
            if selected is None
            else "{" + ", ".join(f"`{name}`: r.`{name}`" for name in selected) + "}"
        )

        conditions = "".join(
            f" AND r.`{name}` IS NULL"
            if value is None
            else f" AND r.`{name}` = $filters.`{name}`"
            for name, value in filters.items()
        )
        branches, bindings = [], {}
        for graph_index, graph_name in enumerate(self._read_graphs(scope)):
            graph = sql.Literal(graph_name).as_string().replace("%", "%%")
            global_conditions = ""
            for name, alias, owner in (
                ("source_scope", "s", spec.source),
                ("target_scope", "t", spec.target),
                ("edge_scope", "r", spec),
            ):
                scoped = self._scope_inputs(owner, scope)
                values = {"datasource_id": scoped.get("datasource_id")}
                if graph_name == self._runtime.target.graph_name:
                    scoped["owner_id"] = GLOBAL_OWNER_ID
                    for field in ("workspace_id", "owner_id"):
                        if field in scoped:
                            values[field] = scoped[field]
                            global_conditions += (
                                f" AND {alias}.{field} = ${name}.{field}"
                            )
                parameters[name] = values
            bindings[f"params_{graph_index}"] = to_json(
                parameters, fallback=dict
            ).decode()
            branches.append(f"""SELECT ag_catalog.agtype_to_json(p) AS properties,
                   ag_catalog.agtype_to_json(identity) AS identity, source_key, target_key, edge_id, {graph}::text AS graph_name
            FROM ag_catalog.cypher({graph}, $$
                MATCH (s:`{source}`)-[r:`{relationship}`]->(t:`{target}`) WHERE true
                  {global_conditions}
                  AND ($source_scope.datasource_id IS NULL OR s.datasource_id = $source_scope.datasource_id)
                  AND ($target_scope.datasource_id IS NULL OR t.datasource_id = $target_scope.datasource_id)
                  AND ($edge_scope.datasource_id IS NULL OR r.datasource_id = $edge_scope.datasource_id)
                {conditions}
                RETURN {projection}, {{source_key: s.key, target_key: t.key}}, s.key, t.key, id(r)
            $$, %(params_{graph_index})s::ag_catalog.agtype)
            AS result(p ag_catalog.agtype, identity ag_catalog.agtype, source_key ag_catalog.agtype, target_key ag_catalog.agtype, edge_id ag_catalog.agtype)""")
        bindings.update(limit=limit, offset=offset)
        rows = self._runtime.read(
            f"SELECT properties, identity FROM ({' UNION ALL '.join(branches)}) hits ORDER BY source_key, target_key, edge_id, graph_name LIMIT %(limit)s OFFSET %(offset)s",
            bindings,
            operation="list_edges",
        )
        for row in rows:
            for name in selected or ():
                row["properties"].setdefault(name, None)
        return tuple(
            EdgeRecord(
                spec,
                row["identity"]["source_key"],
                row["identity"]["target_key"],
                row["properties"],
            )
            for row in rows
        )

    def atomic(self, work: Callable[[GraphStore], _T]) -> _T:
        if not isinstance(self._runtime, HoloRuntime):
            raise StoreError("nested atomic work is not supported", operation="atomic")
        return self._runtime.write_work(
            lambda runtime: work(
                HoloGraphStore(
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
        if self._owns_runtime and isinstance(self._runtime, HoloRuntime):
            self._runtime.close()


__all__ = ["CAPABILITIES", "HoloGraphStore"]

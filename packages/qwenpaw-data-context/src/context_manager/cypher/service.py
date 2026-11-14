"""Execute native graph reads and project bounded public results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from time import monotonic

from pydantic_core import to_json

from ..auth import AccessMode, ScopeBinding
from ..model import ModelService
from ..schema import DisplayPolicy, NodeSpec
from ..store import GraphPropertyMap, GraphStore
from .admission import GraphQueryError, admit_age_sql, admit_cypher
from .cypher_ast import CypherAstError, analyse_scoped_query, scope_parsed_query

_MAX_RESULT_BYTES = 1_000_000
_DOMAIN_TYPES = frozenset(
    {
        "Domain",
        "Metric",
        "Dimension",
        "Dataset",
        "DatasetField",
        "MetricCaliber",
        "VerifiedQuery",
    }
)
_DATASOURCE_TYPES = frozenset(
    {"DataSource", "Table", "Column", "Dataset", "DatasetField", "MetricCaliber"}
)


class GraphQueryService:
    """Run native reads of the current user graph with an MCP-issued Scope."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._backend = store.backend
        if self._backend not in {"neo4j", "holoage"}:
            raise ValueError("Unsupported graph query dialect")
        self._nodes = tuple(node for node in store.schema.nodes if not node.internal)
        self._fulltext_indexes = dict(store.fulltext_indexes)
        visible = frozenset(
            prop.name
            for spec in (*self._nodes, *store.schema.edges)
            for prop in spec.properties
            if prop.display_policy is not DisplayPolicy.NEVER
        )
        self._hidden = frozenset(
            prop.name
            for spec in (*store.schema.nodes, *store.schema.edges)
            for prop in spec.properties
            if prop.display_policy is DisplayPolicy.NEVER
        )
        # Untyped MATCH variables can span types with different display policies
        # for the same field (e.g. updated_at). Publish only names safe on all types.
        self._properties = visible - self._hidden
        self._node_fields = {
            spec.name: {prop.name for prop in spec.properties} & self._properties
            for spec in self._nodes
        }
        self._edge_fields: dict[str, set[str]] = {}
        for spec in store.schema.edges:
            self._edge_fields.setdefault(spec.relationship, set()).update(
                {prop.name for prop in spec.properties} & self._properties
            )

    def get_graph_schema(self, scope: ScopeBinding) -> dict[str, object]:
        """Describe public fields with declared logical kinds and topology."""
        self._store.access.require(scope, AccessMode.READ)
        self._store._require_target(scope)
        schema = self._store.schema.get_overview()
        schema["backend"] = self._backend
        schema["query_scope"] = {
            "workspace_id": scope.workspace_id,
            "owner_ids": list(scope.owner_ids),
            "zone": scope.zone,
            "datasource_id": scope.datasource_id,
        }
        if self._backend == "holoage":
            schema["graphs"] = [self._store._graph_name(scope.user_id)]
        for entry in schema["nodes"]:
            for name, label in self._fulltext_indexes.items():
                if entry["name"] == label:
                    entry["fulltext_index"] = name
        for entries, declarations in (
            (schema["nodes"], self._nodes),
            (schema["edges"], self._store.schema.edges),
        ):
            for entry, declaration in zip(entries, declarations, strict=True):
                entry["properties"] = [
                    name for name in entry["properties"] if name not in self._hidden
                ]
                entry["property_types"] = {
                    prop.name: prop.kind.value
                    for prop in declaration.properties
                    if prop.name in entry["properties"]
                }
        return schema

    def execute(
        self,
        query: str,
        params: Mapping[str, object],
        *,
        scope: ScopeBinding,
        limit: int = 100,
    ) -> dict[str, object]:
        if not isinstance(query, str) or not query.strip() or len(query) > 50_000:
            raise GraphQueryError("Query must contain 1-50000 characters")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GraphQueryError("limit must be between 1 and 1000")
        started = monotonic()
        if self._backend == "holoage":
            # Catalog reads used by admission have their own execution boundary.
            self._store.access.require(scope, AccessMode.READ)
            self._store._require_target(scope)
            admit_age_sql(
                query,
                graph=self._store._graph_name(scope.user_id),
            )
        else:
            tree = admit_cypher(query, params, fulltext_indexes=self._fulltext_indexes)
            try:
                query, scope_params = scope_parsed_query(
                    query,
                    analyse_scoped_query(tree, allow_fulltext=True),
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    is_admin=False,
                    include_global=False,
                )
            except CypherAstError as exc:
                raise GraphQueryError(str(exc)) from exc
            params = {**params, **scope_params}
        result = self._store.execute_query(
            query, params, scope=scope, max_rows=limit + 1
        )
        return self._result(result, limit=limit, started=started)

    def search_keyword(
        self,
        text: str,
        *,
        scope: ScopeBinding,
        limit: int = 20,
        node_types: Sequence[str] | None = None,
        domain_id: str | None = None,
        datasource_id: str | None = None,
    ) -> dict[str, object]:
        started = monotonic()
        specs, filters = self._search_nodes(
            node_types, "fulltext", domain_id, datasource_id
        )
        return self._search(
            text,
            text=text,
            specs=specs,
            filters=filters,
            scope=scope,
            limit=limit,
            started=started,
        )

    async def search_vector(
        self,
        text: str,
        *,
        models: ModelService,
        run_sync: Callable[..., Awaitable],
        scope: ScopeBinding,
        limit: int = 20,
        node_types: Sequence[str] | None = None,
        domain_id: str | None = None,
        datasource_id: str | None = None,
    ) -> dict[str, object]:
        """Embed once, then rank through the Store on the shared read executor."""
        started = monotonic()
        specs, filters = self._search_nodes(
            node_types, "vector", domain_id, datasource_id
        )
        config = await run_sync(models.resolve_for_use, "embedding")
        async with models.embedder(config) as embedder:
            vector = (await embedder.embed((text,)))[0]
        return await run_sync(
            self._search,
            vector,
            text=text,
            specs=specs,
            filters=filters,
            scope=scope,
            limit=limit,
            started=started,
        )

    def _search_nodes(
        self,
        node_types: Sequence[str] | None,
        kind: str,
        domain_id: str | None,
        datasource_id: str | None,
    ) -> tuple[tuple[NodeSpec, ...], dict[str, dict[str, object]]]:
        searchable = {
            node.name: node
            for node in self._nodes
            if (node.fulltext_fields if kind == "fulltext" else node.embedding_fields)
        }
        if node_types is not None and (invalid := set(node_types) - searchable.keys()):
            raise GraphQueryError(
                f"Unsupported {kind} node_types: {sorted(invalid)}; "
                f"searchable public types: {sorted(searchable)}"
            )
        selected = (
            tuple(dict.fromkeys(node_types))
            if node_types is not None
            else tuple(searchable)
        )
        for parameter, value, supported in (
            ("domain_id", domain_id, _DOMAIN_TYPES),
            ("datasource_id", datasource_id, _DATASOURCE_TYPES),
        ):
            if value is None:
                continue
            if node_types is not None and (invalid := set(selected) - supported):
                raise GraphQueryError(
                    f"{parameter} is unsupported for node_types: {sorted(invalid)}; "
                    f"supported {kind} types: {sorted(supported & searchable.keys())}. "
                    "Filters use direct properties, not relationship traversal."
                )
            selected = tuple(name for name in selected if name in supported)
        specs = tuple(searchable[name] for name in selected)
        filters = {}
        for spec in specs:
            properties = {}
            if domain_id is not None:
                properties["domain_id" if spec.name == "Domain" else "domain"] = (
                    domain_id
                )
            if datasource_id is not None:
                properties["datasource_id"] = datasource_id
            filters[spec.name] = properties
        return specs, filters

    def _search(
        self,
        query: str | Sequence[float],
        *,
        text: str,
        specs: Sequence[NodeSpec],
        filters: Mapping[str, Mapping[str, object]],
        scope: ScopeBinding,
        limit: int,
        started: float,
    ) -> dict[str, object]:
        keyword = isinstance(query, str)
        # Project at the database boundary so vectors never enter the response buffer.
        fields = tuple(sorted(set().union(*(self._node_fields[n.name] for n in specs))))
        search = self._store.search_fulltext if keyword else self._store.search_vector
        records = search(
            query,
            specs=specs,
            filters_by_type=filters,
            scope=scope,
            limit=limit + 1,
            fields=fields,
        )
        lexical_query = text.strip().casefold()
        rows = [
            {
                "node_type": record.spec.name,
                "node": GraphPropertyMap(
                    record.properties, kind="node", types=(record.spec.name,)
                ),
                "retrieval": {
                    "method": "fulltext" if keyword else "vector",
                    "lexical_matches": self._lexical_matches(
                        lexical_query,
                        record.properties,
                        self._node_fields[record.spec.name],
                    ),
                },
            }
            for record in records
        ]
        return self._result(
            rows,
            limit=limit,
            started=started,
            searched_node_types=[spec.name for spec in specs],
        )

    @staticmethod
    def _lexical_matches(
        query: str, properties: Mapping[str, object], public_fields: set[str]
    ) -> list[dict[str, str]]:
        """Report literal evidence, never a backend score explanation or confidence."""
        matches = []
        for field in (
            "name",
            "datasource_name",
            "metric_name",
            "canonical_name",
            "synonyms",
            "aliases",
            "description",
            "comment",
        ):
            if field not in public_fields:
                continue
            value = properties.get(field)
            values = value if isinstance(value, tuple) else (value,)
            relation = "contains" if field in {"description", "comment"} else "exact"
            if any(
                isinstance(item, str)
                and (
                    query in item.strip().casefold()
                    if relation == "contains"
                    else query == item.strip().casefold()
                )
                for item in values
            ):
                matches.append({"field": field, "relation": relation})
        return matches

    def _result(
        self,
        result: Sequence[Mapping[str, object]],
        *,
        limit: int,
        started: float,
        searched_node_types: Sequence[str] | None = None,
    ) -> dict[str, object]:
        rows = []
        columns = list(result[0]) if result else []
        search_metadata = (
            {"searched_node_types": searched_node_types}
            if searched_node_types is not None
            else {}
        )
        size = len(
            to_json(
                {
                    "columns": columns,
                    "rows": [],
                    "row_count": limit,
                    "truncated": True,
                    "elapsed_ms": 9999999.999,
                    **search_metadata,
                }
            )
        )
        truncated = len(result) > limit
        for row in result[:limit]:
            projected = self._project(row)
            size += len(to_json(projected)) + 1
            if size > _MAX_RESULT_BYTES:
                truncated = True
                break
            rows.append(projected)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": max(0.0, (monotonic() - started) * 1000),
            **search_metadata,
        }

    def _project(self, value: object) -> object:
        if isinstance(value, GraphPropertyMap):
            fields = self._node_fields if value.kind == "node" else self._edge_fields
            allowed = set().union(*(fields.get(name, set()) for name in value.types))
            return {
                name: self._project(item)
                for name, item in value.items()
                if name in allowed
            }
        if isinstance(value, Mapping):
            return {
                name: self._project(item)
                for name, item in value.items()
                if name not in self._hidden
            }
        if isinstance(value, (list, tuple)):
            return [self._project(item) for item in value]
        return value

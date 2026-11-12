"""Expose bounded graph reads through request-owned MCP capabilities."""

from collections.abc import Callable
from contextlib import contextmanager
from typing import Annotated

from qwenpaw_data_auth.authorization import AuthorizationContext
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, JsonValue
from pydantic_core import to_json

from ..cypher import GraphQueryError, GraphQueryService
from ..model import LlmError, ModelConfigError
from ..runtime import ContextRuntime, ContextRuntimeUnavailable
from ..store import StoreError

ResolveContext = Callable[[Context, str], tuple[ContextRuntime, AuthorizationContext]]
SearchText = Annotated[str, Field(min_length=1, max_length=8000, pattern=r"\S")]
SearchLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
SearchNodeTypes = Annotated[
    list[str],
    Field(min_length=1, description="Public searchable types; omit for all compatible types. Unknown, internal or filter-incompatible types are rejected; duplicates are ignored."),
]
SearchDomainId = Annotated[
    str,
    Field(min_length=1, max_length=512, pattern=r"^\S(?:[\s\S]*\S)?$", description="Exact Domain.domain_id, not its display name or graph key. Filters direct domain properties before Top K, without relationship inference."),
]
SearchDatasourceId = Annotated[
    str,
    Field(min_length=1, max_length=512, pattern=r"^\S(?:[\s\S]*\S)?$", description="Exact DataSource.datasource_id, not its display name or graph key. Filters direct datasource properties before Top K, without inferring Metric/Dimension ownership."),
]


def register_graph_tools(mcp: FastMCP, resolve_context: ResolveContext) -> None:
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    @contextmanager
    def graph_errors():
        try:
            yield
        except (GraphQueryError, ModelConfigError) as exc:
            raise ToolError(str(exc)) from exc
        except LlmError as exc:
            raise ToolError("Query embedding failed; check the embedding Provider") from exc
        except PermissionError as exc:
            raise ToolError("Graph read is forbidden") from exc
        except ContextRuntimeUnavailable as exc:
            raise ToolError("Context runtime is unavailable") from exc
        except BlockingIOError as exc:
            raise ToolError("Graph query execution is busy") from exc
        except StoreError as exc:
            message = f"Graph query failed ({exc.code or 'graph_store_error'})"
            diagnostic = exc.query_diagnostic
            if diagnostic is not None:
                message += f"\nDatabase code: {diagnostic.code}\nMessage: {diagnostic.message}"
                if diagnostic.position is not None:
                    message += f"\nPosition: {diagnostic.position}"
            raise ToolError(message) from exc

    async def read(ctx: Context, tool_name: str, operation: Callable) -> str:
        with graph_errors():
            runtime, authorization = resolve_context(ctx, tool_name)

            def execute() -> str:
                scope = runtime.issue_graph_read_scope(authorization, owner_id=authorization.user_id)
                return to_json(operation(runtime.graph_store(scope), scope)).decode("utf-8")

            return await runtime.run_graph_read(execute)

    search_description = (
        "Search the authenticated user's graph on Neo4j or Holo. Domain layer: Domain, Metric, Dimension, VerifiedQuery. "
        "Datasource layer: DataSource, Table, Column. Both: Dataset, DatasetField, MetricCaliber. "
        "Entity, Event and UserMemory support neither ID filter. Both IDs select only both-layer types. "
        "Omitted node_types selects compatible types; explicit incompatible types are errors. "
        "Resolve display names to exact domain/datasource IDs first; query text does not select IDs. "
        "Rank across types without quotas. Return {columns,rows,row_count,truncated,elapsed_ms,searched_node_types}; "
        "rows contain node_type, public node properties and retrieval.lexical_matches, never scores or embeddings. "
        "Literal evidence uses trim+casefold: exact names or synonym/alias entries, and description/comment containing the query. "
        "Empty evidence is only a retrieval candidate, not proof of business equivalence or confidence. "
        "Results are capped at limit and 1 MB. Verify definitions and bindings with get_graph_schema/query_graph. "
        "Do not call both search tools unless both retrieval modes are needed. "
    )

    @mcp.tool(annotations=annotations, structured_output=False, description=(
        "Find graph nodes using plain-text keywords, not substrings or Lucene expressions. " + search_description
    ))
    async def search_graph_fulltext(
        text: SearchText, ctx: Context, limit: SearchLimit = 20,
        node_types: SearchNodeTypes | None = None, domain_id: SearchDomainId | None = None,
        datasource_id: SearchDatasourceId | None = None,
    ) -> str:
        return await read(ctx, "search_graph_fulltext", lambda store, scope: GraphQueryService(store).search_keyword(
            text, scope=scope, limit=limit, node_types=node_types, domain_id=domain_id, datasource_id=datasource_id,
        ))

    @mcp.tool(annotations=annotations, structured_output=False, description=(
        "Find semantically related graph nodes using the user's saved embedding model. Table/Column are fulltext-only. "
        "Nodes without vectors cannot match; this does not rebuild embeddings. Provider failures are errors, not empty results. "
        "elapsed_ms includes query embedding. " + search_description
    ))
    async def search_graph_vector(
        text: SearchText, ctx: Context, limit: SearchLimit = 20,
        node_types: SearchNodeTypes | None = None, domain_id: SearchDomainId | None = None,
        datasource_id: SearchDatasourceId | None = None,
    ) -> str:
        with graph_errors():
            runtime, authorization = resolve_context(ctx, "search_graph_vector")

            def prepare():
                scope = runtime.issue_graph_read_scope(authorization, owner_id=authorization.user_id)
                return GraphQueryService(runtime.graph_store(scope)), scope, runtime.models(authorization)

            service, scope, models = await runtime.run_graph_read(prepare)
            result = await service.search_vector(
                text, models=models, run_sync=runtime.run_graph_read, scope=scope, limit=limit,
                node_types=node_types, domain_id=domain_id, datasource_id=datasource_id,
            )
            return to_json(result).decode("utf-8")

    @mcp.tool(annotations=annotations, structured_output=False, description=(
        "Get {version,backend,zones,nodes,edges,query_scope}; Holo also returns exact authorized graphs. "
        "Use trusted query_scope IDs when querying; caller IDs never expand access. Properties and topology are declared, "
        "not live counts/index readiness/nullability. Domain.domain_id and DataSource.datasource_id are business IDs, "
        "not names or graph keys; discover actual IDs with graph reads."
    ))
    async def get_graph_schema(ctx: Context) -> str:
        return await read(ctx, "get_graph_schema", lambda store, scope: GraphQueryService(store).get_graph_schema(scope))

    @mcp.tool(annotations=annotations, structured_output=False, description=(
        "Read get_graph_schema first, then execute one native read-only Neo4j Cypher or complete Holo AGE SQL statement. "
        "Neo4j scope is inserted by the server; Holo must use the authorized schema names. Administrators cannot expand "
        "the current user's scope. Apply confirmed domain/datasource IDs on their direct properties; choosing a SQL "
        "datasource does not scope the graph. For Holo, ORDER BY source expressions, not RETURN aliases; RETURN/AS "
        "columns must match and use ag_catalog.agtype. Outer SQL uses AS column names. Return "
        "{columns,rows,row_count,truncated,elapsed_ms}, capped at limit/1 MB and ten seconds. "
        "Database errors retain codes/messages/positions; correct the failing layer, never treat errors as empty results. "
        "For business table rows use execute_read."
    ))
    async def query_graph(
        cypher: Annotated[str, Field(min_length=1, max_length=50000, description="One read-only native statement for get_graph_schema.backend.")],
        ctx: Context,
        params: Annotated[dict[str, JsonValue] | None, Field(description="Neo4j: $name; scoped_ws_id/scoped_uid reserved. Holo: %(name)s and JSON string cast to ag_catalog.agtype for cypher params.")] = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100,
    ) -> str:
        return await read(ctx, "query_graph", lambda store, scope: GraphQueryService(store).execute(
            cypher, params or {}, scope=scope, limit=limit,
        ))

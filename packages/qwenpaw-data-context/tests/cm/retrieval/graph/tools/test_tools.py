"""Verify native graph query scope, public projection and bounded output."""

import asyncio
import json
from contextlib import aclosing
from unittest.mock import AsyncMock, Mock, create_autospec

import psycopg
import pytest
from context_manager.auth import GraphScopeAuthority
from context_manager.cypher import GraphQueryError, GraphQueryService
from context_manager.cypher.cypher_ast import parse_cypher
from context_manager.mcp.graph import register_graph_tools
from context_manager.model import LlmError, ModelService, TextEmbedder
from context_manager.runtime import ContextRuntime
from context_manager.schema import GRAPH_SCHEMA
from context_manager.store import NodeRecord, StoreError
from context_manager.store.holoage.runtime import HoloRuntime
from context_manager.store.holoage.store import HoloGraphStore
from context_manager.store.holoage.target import HoloTarget
from context_manager.store.neo4j import GraphTarget
from context_manager.store.neo4j.runtime import Neo4jRuntime
from context_manager.store.neo4j.store import Neo4jGraphStore
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from psycopg.pq import DiagnosticField

from cm.infrastructure.access.support import authorization
from cm.infrastructure.storage.schema_support import make_glossary


@pytest.mark.asyncio
async def test_search_tools_return_scoped_public_nodes_and_surface_embedding_failure(
    query,
):
    _, store, scope, _, _ = query
    runtime = create_autospec(ContextRuntime, instance=True)
    runtime.run_graph_read.side_effect = asyncio.to_thread
    runtime.issue_graph_read_scope.return_value = scope
    runtime.graph_store.return_value = store
    models = create_autospec(ModelService, instance=True)
    embedder = AsyncMock(spec=TextEmbedder)
    embedder.embed.return_value = ((0.25, 0.75),)
    models.embedder.side_effect = lambda config: aclosing(embedder)
    runtime.models.return_value = models
    records = (
        NodeRecord(
            GRAPH_SCHEMA.node("Metric"),
            {"key": "m1", "name": "Revenue", "embedding": [1, 0]},
        ),
        NodeRecord(GRAPH_SCHEMA.node("Dataset"), {"key": "d1", "name": "orders"}),
    )
    store.search_fulltext.return_value = records
    store.search_vector.return_value = records
    mcp = FastMCP("search-test")
    register_graph_tools(mcp, lambda ctx, tool_name: (runtime, authorization()))
    for name, search in (
        ("fulltext", store.search_fulltext),
        ("vector", store.search_vector),
    ):
        result = json.loads(
            (
                await mcp.call_tool(
                    f"search_graph_{name}", {"text": "revenue", "limit": 1}
                )
            )[0].text
        )
        assert result["rows"] == [
            {
                "node_type": "Metric",
                "node": {"key": "m1", "name": "Revenue"},
                "retrieval": {
                    "method": name,
                    "lexical_matches": [{"field": "name", "relation": "exact"}],
                },
            }
        ]
        assert result["columns"] == ["node_type", "node", "retrieval"]
        assert result["truncated"] is True
        assert search.call_args.kwargs["scope"] is scope
        assert search.call_args.kwargs["limit"] == 2
        assert "embedding" not in search.call_args.kwargs["fields"]
        assert set(search.call_args.kwargs["specs"]) == {
            node
            for node in GRAPH_SCHEMA.nodes
            if not node.internal
            and (node.fulltext_fields if name == "fulltext" else node.embedding_fields)
        }
        for arguments in ({"text": " "}, {"text": "x", "limit": 101}):
            with pytest.raises(ToolError):
                await mcp.call_tool(f"search_graph_{name}", arguments)
    store.search_vector.assert_called_once()
    assert store.search_vector.call_args.args == ((0.25, 0.75),)
    embedder.embed.assert_awaited_once_with(("revenue",))
    embedder.aclose.assert_awaited_once()
    embedder.embed.side_effect = LlmError("private provider details")
    with pytest.raises(ToolError, match="Query embedding failed"):
        await mcp.call_tool("search_graph_vector", {"text": "revenue"})
    store.search_vector.assert_called_once()

    embedder.embed.side_effect = None
    for name, search in (
        ("fulltext", store.search_fulltext),
        ("vector", store.search_vector),
    ):
        await mcp.call_tool(
            f"search_graph_{name}", {"text": "revenue", "node_types": None}
        )
        all_specs = search.call_args.kwargs["specs"]
        type_names = {node.name for node in all_specs}
        assert type_names >= {"Metric", "Dataset", "DatasetField"}
        if name == "fulltext":
            assert {"Table", "Column"} <= type_names
        else:
            assert not {"Table", "Column"} & type_names
        selected = "Table" if name == "fulltext" else "Dataset"
        search.return_value = (
            NodeRecord(GRAPH_SCHEMA.node(selected), {"key": "s1", "name": "orders"}),
        )
        result = json.loads(
            (
                await mcp.call_tool(
                    f"search_graph_{name}",
                    {"text": "revenue", "node_types": [selected, selected], "limit": 1},
                )
            )[0].text
        )
        assert search.call_args.kwargs["specs"] == (GRAPH_SCHEMA.node(selected),)
        assert result["rows"] == [
            {
                "node_type": selected,
                "node": {"key": "s1", "name": "orders"},
                "retrieval": {"method": name, "lexical_matches": []},
            }
        ]
        assert result["searched_node_types"] == [selected]
        assert result["truncated"] is False
        search.return_value = ()
        for constraints, expected in (
            (
                {"domain_id": "sales"},
                {
                    "Domain",
                    "Metric",
                    "Dimension",
                    "VerifiedQuery",
                    "Dataset",
                    "DatasetField",
                    "MetricCaliber",
                },
            ),
            (
                {"datasource_id": "ds-chat"},
                {"DataSource", "Dataset", "DatasetField", "MetricCaliber"}
                | ({"Table", "Column"} if name == "fulltext" else set()),
            ),
            (
                {"domain_id": "sales", "datasource_id": "ds-chat"},
                {"Dataset", "DatasetField", "MetricCaliber"},
            ),
        ):
            response = json.loads(
                (
                    await mcp.call_tool(
                        f"search_graph_{name}", {"text": "model", **constraints}
                    )
                )[0].text
            )
            assert set(response["searched_node_types"]) == expected
            assert response["rows"] == []
            assert search.call_args.kwargs["scope"] is scope
            assert search.call_args.kwargs["filters_by_type"] == {
                kind: {
                    (
                        "domain" if key == "domain_id" and kind != "Domain" else key
                    ): value
                    for key, value in constraints.items()
                }
                for kind in expected
            }
        search.reset_mock()
        embedder.embed.reset_mock()
        invalid_types = [
            [],
            ["Unknown"],
            [next(n.name for n in GRAPH_SCHEMA.nodes if n.internal)],
            [
                next(
                    n.name
                    for n in GRAPH_SCHEMA.nodes
                    if not n.internal and not n.fulltext_fields
                )
            ],
        ]
        if name == "vector":
            invalid_types.extend([["Table"], ["Column"]])
        for node_types in invalid_types:
            with pytest.raises(ToolError):
                await mcp.call_tool(
                    f"search_graph_{name}",
                    {"text": "revenue", "node_types": node_types},
                )
        for constraints in (
            {"domain_id": "sales", "node_types": ["DataSource"]},
            {"datasource_id": "ds-chat", "node_types": ["Metric", "Dataset"]},
            {"datasource_id": "ds-chat", "node_types": ["VerifiedQuery"]},
            {"domain_id": 123},
            {"domain_id": ""},
            {"domain_id": " "},
            {"datasource_id": " ds-chat"},
            {"datasource_id": "ds-chat\n"},
        ):
            with pytest.raises(ToolError):
                await mcp.call_tool(
                    f"search_graph_{name}", {"text": "model", **constraints}
                )
        search.assert_not_called()
        embedder.embed.assert_not_awaited()


def test_lexical_evidence_does_not_infer_business_equivalence(query):
    service, store, scope, _, _ = query
    store.search_fulltext.return_value = (
        NodeRecord(
            GRAPH_SCHEMA.node("Dimension"),
            {
                "key": "requested",
                "name": "Model",
                "synonyms": ["requested model"],
                "description": "Selected MODEL, including failures",
                "embedding": [1, 0],
            },
        ),
        NodeRecord(
            GRAPH_SCHEMA.node("Dimension"),
            {
                "key": "actual",
                "name": "Actual Model",
                "synonyms": ["served MODEL"],
            },
        ),
    )
    rows = service.search_keyword(" model ", scope=scope)["rows"]
    assert rows[0]["retrieval"]["lexical_matches"] == [
        {"field": "name", "relation": "exact"},
        {"field": "description", "relation": "contains"},
    ]
    assert rows[1]["retrieval"]["lexical_matches"] == []
    rows = service.search_keyword("REQUESTED MODEL", scope=scope)["rows"]
    assert rows[0]["retrieval"]["lexical_matches"] == [
        {"field": "synonyms", "relation": "exact"}
    ]
    assert "embedding" not in rows[0]["node"]


@pytest.mark.parametrize("kind", ["fulltext", "vector"])
def test_typed_search_filters_reach_database_before_top_k(query, kind, monkeypatch):
    _, mock_store, scope, _, _ = query
    holo = mock_store.backend == "holoage"
    target = (
        HoloTarget("tenant-1", "workspace-1", "global_graph")
        if holo
        else GraphTarget("tenant-1", "workspace-1")
    )
    runtime = Mock(target=target)
    runtime.read.return_value = ()
    store = (HoloGraphStore if holo else Neo4jGraphStore)(
        runtime, GRAPH_SCHEMA, mock_store.access, make_glossary(GRAPH_SCHEMA)
    )
    if holo:
        monkeypatch.setattr(store, "_read_graphs", lambda scope: ("alice_graph",))
    domain, dataset = GRAPH_SCHEMA.node("Domain"), GRAPH_SCHEMA.node("Dataset")
    search = getattr(store, f"search_{kind}")
    text = "model" if kind == "fulltext" else (0.25,) * 1024
    # Domain uses domain_id; Dataset uses domain, within one global ranked search.
    filters = {
        "Domain": {"domain_id": "sales"},
        "Dataset": {"domain": "sales", "datasource_id": "ds-chat"},
    }
    search(text, specs=(domain, dataset), filters_by_type=filters, limit=1, scope=scope)
    calls = runtime.read.call_args_list
    assert len(calls) == (1 if holo else 2)
    if holo:
        statement, params = calls[0].args
        branches = statement.split(" UNION ALL ")
        assert '"alice_graph"."Domain"' in branches[0]
        assert '"alice_graph"."Dataset"' in branches[1]
        for index, properties in enumerate(filters.values()):
            for position, (field, value) in enumerate(properties.items()):
                predicate = f"agtype_object_field(n.properties, '{field}') = %(filter_{index}_{position})s"
                assert (
                    branches[index].index(predicate)
                    < branches[index].index("ORDER BY")
                    < branches[index].index("LIMIT")
                )
                assert json.loads(params[f"filter_{index}_{position}"]) == value
    else:
        for call, properties in zip(calls, filters.values(), strict=True):
            statement, params = call.args
            assert params["node_scope_workspace_id"] == scope.workspace_id
            assert params["node_scope_owner_ids"] == scope.owner_ids
            for position, (field, value) in enumerate(properties.items()):
                predicate = f"node.{field} = $search_filter_{position}"
                assert (
                    statement.index(predicate)
                    < statement.index("ORDER BY")
                    < statement.index("LIMIT")
                )
                assert params[f"search_filter_{position}"] == value
    runtime.read.reset_mock()
    for overrides in (
        {"filters_by_type": {"Metric": {"domain": "sales"}}},
        {"filters_by_type": {"Dataset": {"unknown": "x"}}},
        {
            "filters": {"domain": "other"},
            "filters_by_type": {"Dataset": {"domain": "sales"}},
        },
    ):
        with pytest.raises(ValueError):
            search(text, specs=(dataset,), scope=scope, **overrides)
    runtime.read.assert_not_called()


def _query(backend):
    kind = HoloGraphStore if backend == "holoage" else Neo4jGraphStore
    store = create_autospec(kind, instance=True)
    store.schema = GRAPH_SCHEMA
    store.backend = backend
    store.access = GraphScopeAuthority()
    if backend == "holoage":
        store._graph_name.return_value = "alice_graph"
    scope = store.access.issue_read_scope(authorization(), owner_id="user-1")
    statement = (
        "SELECT ag_catalog.agtype_to_json(n) AS node FROM "
        "ag_catalog.cypher('alice_graph', $$MATCH (n:Metric) RETURN properties(n)$$) "
        "AS (n ag_catalog.agtype)"
        if backend == "holoage"
        else "MATCH (n:Metric {workspace_id:$ws, owner_id:$owner}) RETURN n AS node"
    )
    params = {} if backend == "holoage" else {"ws": "workspace-1", "owner": "user-1"}
    return GraphQueryService(store), store, scope, statement, params


@pytest.fixture(params=["neo4j", "holoage"])
def query(request):
    return _query(request.param)


def test_results_drop_hidden_properties_and_report_truncation(query):
    service, store, scope, statement, params = query
    store.execute_query.return_value = (
        {"node": {"name": "GMV", "embedding": [0.1, 0.2], "updated_at": "internal"}},
        {"node": {"name": "second"}},
    )
    result = service.execute(statement, params, scope=scope, limit=1)
    executed, bound = store.execute_query.call_args.args
    assert store.execute_query.call_args.kwargs == {"scope": scope, "max_rows": 2}
    if store.backend == "holoage":
        assert (executed, bound) == (statement, params)
    else:
        assert "n.owner_id = $scoped_uid" in executed
        assert bound == {
            **params,
            "scoped_ws_id": "workspace-1",
            "scoped_uid": "user-1",
        }
    assert result["rows"] == [{"node": {"name": "GMV"}}]
    assert result["truncated"] is True
    assert result["columns"] == ["node"]
    schema = service.get_graph_schema(scope)
    assert (
        "updated_at"
        not in next(n for n in schema["nodes"] if n["name"] == "Metric")["properties"]
    )
    assert schema["query_scope"]["owner_ids"] == ["user-1"]
    if store.backend == "holoage":
        assert schema["graphs"] == ["alice_graph"]


def test_cross_user_graph_is_rejected_before_execution():
    service, store, scope, statement, params = _query("holoage")
    statement = statement.replace("alice_graph", "bob_graph")
    with pytest.raises(GraphQueryError, match="authorized|unauthorized"):
        service.execute(statement, params, scope=scope)
    store.execute_query.assert_not_called()


@pytest.mark.parametrize("role", ["member", "ws_admin"])
def test_native_driver_details_do_not_escape_the_mcp_boundary(query, role):
    _, store, scope, statement, params = query
    runtime = create_autospec(ContextRuntime, instance=True)
    runtime.issue_graph_read_scope.return_value = scope
    runtime.graph_store.return_value = store
    runtime.run_graph_read.side_effect = asyncio.to_thread
    store.execute_query.side_effect = StoreError(
        "password=secret", operation="execute_query", code="invalid_query"
    )
    mcp = FastMCP("query-test")
    context = authorization(role=role)
    register_graph_tools(mcp, lambda ctx, tool_name: (runtime, context))
    with pytest.raises(ToolError) as error:
        asyncio.run(
            mcp.call_tool("query_graph", {"cypher": statement, "params": params})
        )
    assert "secret" not in str(error.value)
    assert str(error.value) == (
        "Error executing tool query_graph: Graph query failed (invalid_query)"
    )
    runtime.issue_graph_read_scope.assert_called_once_with(context, owner_id="user-1")


@pytest.mark.parametrize(
    "holo_type, neo4j_code, primary, code, retryable",
    [
        (
            psycopg.errors.UndefinedTable,
            "Neo.ClientError.Statement.SyntaxError",
            'Undefined variable "m" (line 1, column 8)',
            "invalid_query",
            False,
        ),
        (
            psycopg.errors.InsufficientPrivilege,
            "Neo.ClientError.Security.Forbidden",
            "Permission denied for the requested graph",
            "permission_denied",
            False,
        ),
        (
            psycopg.errors.SerializationFailure,
            "Neo.TransientError.Transaction.DeadlockDetected",
            "Transaction could not complete due to a concurrent update",
            "unavailable",
            True,
        ),
        (
            psycopg.errors.FeatureNotSupported,
            "Neo.DatabaseError.Statement.ExecutionFailed",
            "The query uses an unsupported feature",
            "backend_error",
            False,
        ),
    ],
)
def test_native_database_diagnostics_reach_mcp_for_all_error_categories(
    query, holo_type, neo4j_code, primary, code, retryable
):
    _, store, scope, statement, params = query
    if store.backend == "holoage":
        info = {
            DiagnosticField.MESSAGE_PRIMARY: primary.encode(),
            DiagnosticField.MESSAGE_DETAIL: b"parameters: password=secret",
            DiagnosticField.CONTEXT: b"connection password=secret",
            DiagnosticField.INTERNAL_QUERY: b"SELECT 'secret'",
        }
        if code == "invalid_query":
            info[DiagnosticField.STATEMENT_POSITION] = b"8"
        native = holo_type("password=secret", info=info)
        failure = HoloRuntime._error(native, "execute_query")
        native_code = native.sqlstate
    else:
        native = Neo4jError._hydrate_gql(
            neo4j_code=neo4j_code,
            message=primary,
            gql_status="50N42",
            description="driver details: password=secret",
            diagnostic_record={"connection": "password=secret"},
        )
        failure = Neo4jRuntime._error(native, "execute_query")
        native_code = neo4j_code
    assert (failure.code, failure.retryable) == (code, retryable)
    native.__cause__ = RuntimeError("password=secret")
    failure.__cause__ = native
    store.execute_query.side_effect = failure
    runtime = create_autospec(ContextRuntime, instance=True)
    runtime.issue_graph_read_scope.return_value = scope
    runtime.graph_store.return_value = store
    runtime.run_graph_read.side_effect = asyncio.to_thread
    mcp = FastMCP("query-test")
    register_graph_tools(mcp, lambda ctx, tool_name: (runtime, authorization()))
    with pytest.raises(ToolError) as error:
        asyncio.run(
            mcp.call_tool("query_graph", {"cypher": statement, "params": params})
        )
    expected = (
        "Error executing tool query_graph: "
        f"Graph query failed ({code})\nDatabase code: {native_code}\nMessage: {primary}"
    )
    if store.backend == "holoage" and code == "invalid_query":
        expected += "\nPosition: 8"
    assert str(error.value) == expected
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("backend", ["holoage", "neo4j"])
def test_connection_and_internal_errors_keep_driver_details_private(backend):
    if backend == "holoage":
        runtime = HoloRuntime
        connection_error = psycopg.OperationalError("password=secret")
        internal_error = psycopg.errors.UndefinedTable(
            "password=secret",
            info={DiagnosticField.MESSAGE_PRIMARY: b"password=secret"},
        )
    else:
        runtime = Neo4jRuntime
        connection_error = ServiceUnavailable("password=secret")
        internal_error = Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError", message="password=secret"
        )
    failure = runtime._error(connection_error, "execute_query")
    assert failure.code == "unavailable"
    assert failure.query_diagnostic is None
    failure = runtime._error(internal_error, "get_nodes")
    assert failure.code == "invalid_query"
    assert failure.query_diagnostic is None


def test_holo_query_cancellation_keeps_native_reason_and_classification():
    primary = "canceling statement due to statement timeout"
    native = psycopg.errors.QueryCanceled(
        primary, info={DiagnosticField.MESSAGE_PRIMARY: primary.encode()}
    )
    failure = HoloRuntime._error(native, "execute_query")
    assert (failure.code, failure.retryable) == ("unavailable", False)
    assert failure.query_diagnostic.code == "57014"
    assert failure.query_diagnostic.message == primary


def test_node_selected_by_aggregate_can_continue_a_scoped_match():
    service, store, scope, _, params = _query("neo4j")
    statement = (
        "MATCH (m:Metric {workspace_id:$ws,owner_id:$owner}) "
        "WITH head(collect(m)) AS selected "
        "MATCH (selected:Metric {workspace_id:$ws,owner_id:$owner}) "
        "RETURN selected.name AS name"
    )
    store.execute_query.return_value = ({"name": "GMV"},)
    assert service.execute(statement, params, scope=scope)["rows"] == [{"name": "GMV"}]
    executed, bound = store.execute_query.call_args.args
    assert "selected.owner_id = $scoped_uid" in executed
    assert bound["scoped_uid"] == "user-1"


@pytest.mark.parametrize("role", ["member", "ws_admin", "tenant_admin"])
def test_neo4j_scopes_nodes_and_edges_even_for_admins_and_forged_params(role):
    service, store, _, _, _ = _query("neo4j")
    scope = store.access.issue_read_scope(authorization(role=role), owner_id="user-1")
    statement = (
        "MATCH (m) WHERE m.owner_id = 'other-user' OR true "
        "WITH m AS source OPTIONAL MATCH (source)-[r:HAS]->(c) RETURN c"
    )
    store.execute_query.return_value = ()
    service.execute(
        statement, {"scoped_uid": "other-user", "scoped_ws_id": "other"}, scope=scope
    )
    executed, bound = store.execute_query.call_args.args
    for variable in ("m", "source", "r", "c"):
        assert f"{variable}.workspace_id = $scoped_ws_id" in executed
        assert f"{variable}.owner_id = $scoped_uid" in executed
    assert "AND (m.owner_id = 'other-user' OR true)" in executed
    assert "'__global__'" not in executed
    assert bound == {"scoped_uid": "user-1", "scoped_ws_id": "workspace-1"}


@pytest.mark.parametrize(
    "statement, variables",
    [
        ("UNWIND [1, 2] AS x RETURN x", ()),
        ("UNWIND [1, 2] AS x MATCH (n) WHERE n.rank = x RETURN n", ("n",)),
        (
            "MATCH (n) WITH collect(n) AS ns UNWIND ns AS m MATCH (m) RETURN m",
            ("n", "m"),
        ),
        ("MATCH (n) RETURN n AS item UNION MATCH (m) RETURN m AS item", ("n", "m")),
        (
            "MATCH (n) RETURN n AS item\nUNION ALL\nMATCH (m) RETURN m AS item",
            ("n", "m"),
        ),
    ],
)
def test_unwind_and_union_keep_all_matches_in_the_personal_graph(statement, variables):
    service, store, scope, _, _ = _query("neo4j")
    store.execute_query.return_value = ()
    service.execute(statement, {}, scope=scope)
    executed, bound = store.execute_query.call_args.args
    parse_cypher(executed)
    for variable in variables:
        assert f"{variable}.workspace_id = $scoped_ws_id" in executed
        assert f"{variable}.owner_id = $scoped_uid" in executed
    assert "'__global__'" not in executed
    assert bound == (
        {"scoped_ws_id": "workspace-1", "scoped_uid": "user-1"} if variables else {}
    )


@pytest.mark.parametrize("where", ["", "WHERE n.owner_id='other-user' OR true "])
def test_fulltext_and_vector_query_scopes_the_current_user_at_yield(where):
    _, store, scope, _, params = _query("neo4j")
    store.fulltext_indexes = {"cm_metric_fulltext": "Metric"}
    service = GraphQueryService(store)
    statement = (
        "CALL db.index.fulltext.queryNodes($index,$text) YIELD node AS n,score "
        f"{where}"
        "OPTIONAL MATCH (n)-[r:HAS]->(c) "
        "RETURN n.name AS name,vector.similarity.cosine(n.embedding,$vector) AS similarity"
    )
    params.update(index="cm_metric_fulltext", text="revenue", vector=[1.0])
    store.execute_query.return_value = ({"name": "revenue", "similarity": 1.0},)
    assert (
        service.execute(statement, params, scope=scope)["rows"][0]["name"] == "revenue"
    )
    executed, bound = store.execute_query.call_args.args
    parse_cypher(executed)
    for variable in ("n", "r", "c"):
        assert f"{variable}.owner_id = $scoped_uid" in executed
        assert f"{variable}.workspace_id = $scoped_ws_id" in executed
    assert bound["scoped_uid"] == "user-1"
    assert "'__global__'" not in executed
    if where:
        assert "AND (n.owner_id='other-user' OR true)" in executed
    for invalid in (
        statement.replace("$index,$text", "$index,$text,{limit:1}"),
        statement.replace("$index", "'unknown_index'"),
        statement.replace("YIELD node AS n,score", "YIELD score"),
    ):
        with pytest.raises(GraphQueryError):
            service.execute(invalid, params, scope=scope)


@pytest.mark.parametrize(
    "body",
    [
        "MATCH (m) OPTIONAL MATCH (m)-[:HAS]->(c) RETURN c",
        "MATCH (m:UnknownLabel) RETURN m",
    ],
)
def test_holo_cypher_does_not_require_schema_labels(body):
    service, store, scope, _, _ = _query("holoage")
    statement = (
        "SELECT * FROM ag_catalog.cypher('alice_graph', $$"
        f"{body}$$) AS (n ag_catalog.agtype)"
    )
    store.execute_query.return_value = ()
    service.execute(statement, {}, scope=scope)
    store.execute_query.assert_called_once_with(
        statement, {}, scope=scope, max_rows=101
    )


@pytest.mark.parametrize("label", ["Metric", "CustomLabel"])
def test_sql_json_aggregation_and_case_are_admitted_in_the_personal_graph(label):
    service, store, scope, _, _ = _query("holoage")
    statement = (
        "SELECT json_agg(CASE WHEN array_length(ARRAY[1,2],1)>0 "
        "THEN ag_catalog.agtype_object_field_text(properties, 'name') ELSE '' END) AS names "
        f'FROM "alice_graph"."{label}"'
    )
    store.execute_query.return_value = ({"names": ["GMV"]},)
    assert service.execute(statement, {}, scope=scope)["rows"] == [{"names": ["GMV"]}]
    store.execute_query.assert_called_once_with(
        statement, {}, scope=scope, max_rows=101
    )
    store.execute_query.reset_mock()
    with pytest.raises(GraphQueryError):
        service.execute(statement.replace("alice_graph", "bob_graph"), {}, scope=scope)
    store.execute_query.assert_not_called()


def test_anonymous_relationships_keep_personal_scope_before_execution():
    service, store, scope, _, _ = _query("neo4j")
    statement = "MATCH (n:Metric)-[:HAS]->(c:MetricCaliber) RETURN c.expr"
    store.execute_query.return_value = ({"c.expr": "sum(amount)"},)
    assert service.execute(statement, {}, scope=scope)["rows"] == [
        {"c.expr": "sum(amount)"}
    ]
    executed, bound = store.execute_query.call_args.args
    assert "[__scoped_rel_0:HAS]" in executed
    for variable in ("n", "c", "__scoped_rel_0"):
        assert f"{variable}.workspace_id = $scoped_ws_id" in executed
        assert f"{variable}.owner_id = $scoped_uid" in executed
    assert bound == {"scoped_ws_id": "workspace-1", "scoped_uid": "user-1"}
    assert "'__global__'" not in executed
    parse_cypher(executed)


def test_writes_and_patterns_without_scope_injection_support_are_rejected():
    service, store, scope, _, params = _query("neo4j")
    for statement in (
        (
            "MATCH (n:Metric {workspace_id:$ws,owner_id:$owner}) "
            "RETURN [(a:Metric)-->(b:Metric) | b.name]"
        ),
        "MATCH (n:Metric {workspace_id:$ws,owner_id:$owner}) DELETE n",
        "UNWIND [1] AS x CREATE (n:Metric) RETURN n",
        "MATCH (n) RETURN n AS item UNION MATCH (m) DELETE m RETURN m AS item",
    ):
        with pytest.raises(GraphQueryError):
            service.execute(statement, params, scope=scope)
    store.execute_query.assert_not_called()


def test_sql_subqueries_and_functions_cannot_escape_authorized_schemas():
    service, store, scope, _, _ = _query("holoage")
    for statement in (
        'SELECT * FROM "bob_graph"."Metric"',
        'WITH x AS (SELECT * FROM "bob_graph"."Metric") SELECT * FROM x',
        "SELECT * FROM pg_authid WHERE EXISTS (WITH pg_authid AS (SELECT 1) SELECT * FROM pg_authid)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT CASE WHEN true THEN pg_read_file('/etc/passwd') ELSE '' END",
        "SELECT json_agg(pg_read_file('/etc/passwd'))",
        "SELECT * FROM ag_catalog.cypher('alice_graph', $$MATCH (n:Metric) DELETE n$$) AS (n ag_catalog.agtype)",
        "SELECT 1; SELECT 2",
    ):
        with pytest.raises(GraphQueryError):
            service.execute(statement, {}, scope=scope)
    store.execute_query.assert_not_called()

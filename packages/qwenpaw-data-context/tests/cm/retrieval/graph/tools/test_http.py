"""Protect tool authentication and SQL write authorization."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cm.retrieval.mcp_support import build_app, call_tool

EXPECTED = {
    "get_graph_schema",
    "query_graph",
    "search_graph_fulltext",
    "search_graph_vector",
    "execute_read",
    "execute_create",
    "execute_modify",
}


@pytest.mark.parametrize("tool", sorted(EXPECTED))
@pytest.mark.parametrize("token", [None, "invalid", "other-workspace"])
def test_authentication_precedes_resource_selection(tool, token):
    app, _, runtime, _ = build_app()
    with (
        TestClient(app) as client,
        patch.object(runtime, "graph_store", wraps=runtime.graph_store) as store,
    ):
        response = call_tool(
            client,
            tool,
            {
                "cypher": "RETURN 1 AS ok",
                "datasource_id": "warehouse",
                "sql": "SELECT 1",
                "text": "sales revenue",
            },
            token=token,
        )
        assert response.status_code == (403 if token == "other-workspace" else 401)
        store.assert_not_called()


@pytest.mark.parametrize(
    "tool,sql",
    [
        ("execute_create", "CREATE TABLE t(x INT)"),
        ("execute_modify", "INSERT INTO t VALUES (1)"),
    ],
)
def test_members_cannot_execute_sql_writes(tool, sql):
    app, _, runtime, _ = build_app()
    with (
        TestClient(app) as client,
        patch.object(runtime, "sql_query", wraps=runtime.sql_query) as service,
    ):
        response = call_tool(client, tool, {"datasource_id": "warehouse", "sql": sql})
        assert response.json()["result"]["isError"] is True
        assert "administration" in response.text
        service.assert_not_called()

"""Verify settlement HTTP contracts against the current Metadata topology."""

import asyncio
import json

import pytest
from context_manager.schema import (
    CALIBER_ON,
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    HAS_CALIBER,
    HAS_DATASET,
    MAPS_TO_FIELD,
    METRIC,
    METRIC_CALIBER,
)
from context_manager.store import StoreError, legacy_reads
from qwenpaw_data_auth.database import get_db
from fastapi.testclient import TestClient

from cm.retrieval.metadata_support import (
    SCOPE,
    node,
    read,
    unavailable_query_embedding,  # noqa: F401 - shared pytest fixtures
)
from cm.retrieval.metadata_support import (
    graph as graph,  # noqa: PLC0414 - pytest fixture
)
from cm.retrieval.metadata_support import (
    imported_graph as imported_graph,  # noqa: PLC0414 - pytest fixture
)

ROUTES = [
    ("domains", {}),
    ("search-metrics", {"domain": "Sales", "query": "Revenue"}),
    ("dimensions", {"domain": "Sales", "name": "Country"}),
    ("datasets", {"domain": "Sales", "name": "Orders"}),
]


@pytest.mark.parametrize(
    "failure", ["index_unavailable", "invalid_query", "unavailable"]
)
def test_metric_search_only_recovers_from_missing_index(graph, monkeypatch, failure):  # noqa: F811 - pytest fixture
    app, _, _ = graph

    def fail(*args, **kwargs):
        raise StoreError(failure, operation="search_fulltext", code=failure)

    monkeypatch.setattr(legacy_reads, "search_fulltext_scores", fail)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/cm/search-metrics",
            params={"domain": "Sales", "query": "Revenue"},
            headers={"Authorization": "Bearer user-1"},
        )
    if failure == "index_unavailable":
        assert response.status_code == 200
        assert response.json()[0]["metric_name"] == "Revenue"
    else:
        assert response.status_code == 500
        assert response.json()["detail"] == "Metadata store query failed"


def test_http_reads_actual_importer_output_without_model_calls(imported_graph):  # noqa: F811 - pytest fixture
    app, caller = imported_graph
    calls = len(caller.embed_calls), len(caller.complete_calls)
    with TestClient(app) as client:
        domain = read(client, "domains")[0]["name"]
        assert (
            read(client, "search-metrics", domain=domain, query="Revenue")[0][
                "metric_name"
            ]
            == "Revenue"
        )
        examples = read(client, "dimensions", domain=domain, name="Country")[
            "sample_values"
        ]
        assert {item["value"] for item in examples} == {"全部", "CN"}
        column = read(client, "datasets", domain=domain, name="Orders")["columns"][0]
        assert (
            column["column_name"] == "country"
            and column["description"] == "Order country"
        )
    assert (len(caller.embed_calls), len(caller.complete_calls)) == calls


def test_real_host_settlement_client_calls_legacy_http(graph, monkeypatch):  # noqa: F811 - pytest fixture
    host = pytest.importorskip("qwenpaw_data.host.core.algo.settlement.cm_utils.client")
    app, _, _ = graph
    async_client = host.httpx.AsyncClient

    def client_factory(**kwargs):
        return async_client(transport=host.httpx.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(host.httpx, "AsyncClient", client_factory)
    client = host.SettlementCmClient(
        access_token="user-1", datasource_id="warehouse", base_url="http://testserver"
    )

    async def scenario():
        assert await client.list_domain_names() == ["Sales"]
        for tool, arguments, field, value in (
            ("search_metrics", {"query": "Revenue", "k": 3}, "metric_name", "Revenue"),
            ("get_dimension", {"name": "Country"}, "dimension_name", "Country"),
            ("get_dataset", {"name": "Orders"}, "dataset_name", "Orders"),
        ):
            result = await client.call(tool, {"domain": "Sales", **arguments})
            assert result["status"] == "ok", result
            payload = json.loads(result["result"])
            if isinstance(payload, list):
                payload = payload[0]
            assert payload[field] == value
            if tool == "get_dataset":
                assert payload["columns"][0]["column_name"] == "country"
                assert payload["columns"][0]["description"] == "Order country"

    with TestClient(app):
        asyncio.run(scenario())


def add_inventory_source(
    store, *, datasource="inventory", domain="sales-id", root="domain-key"
):
    node(
        store,
        DATASET,
        "inventory-dataset",
        name="Inventory",
        domain=domain,
        datasource_id=datasource,
    )
    node(
        store,
        DATASET_FIELD,
        "inventory-field",
        name="warehouse",
        dataset="Inventory",
        domain=domain,
    )
    node(store, DIMENSION, "inventory-dimension", name="Warehouse", domain=domain)
    node(store, METRIC, "inventory-metric", name="Revenue Inventory", domain=domain)
    node(store, METRIC_CALIBER, "inventory-caliber", domain=domain)
    node(store, METRIC_CALIBER, "orders-caliber")
    for edge, source, target in (
        (HAS_DATASET, root, "inventory-dataset"),
        (DATASET_HAS_COLUMN_DATASET_FIELD, "inventory-dataset", "inventory-field"),
        (MAPS_TO_FIELD, "inventory-dimension", "inventory-field"),
        (HAS_CALIBER, "inventory-metric", "inventory-caliber"),
        (CALIBER_ON, "inventory-caliber", "inventory-dataset"),
        (HAS_CALIBER, "metric-key", "orders-caliber"),
        (CALIBER_ON, "orders-caliber", "dataset-key"),
    ):
        store.edges[edge, source, target] = dict(SCOPE)


@pytest.mark.parametrize(
    "path,extra,field",
    [
        ("datasets", {}, "dataset_name"),
        ("dimensions", {}, "dimension_name"),
        ("search-metrics", {"query": "Revenue"}, "metric_name"),
    ],
)
def test_dataset_anchor_narrows_related_objects_and_explicit_name_wins(
    graph, path, extra, field  # noqa: F811 - pytest fixture
):
    app, store, _ = graph
    add_inventory_source(store, datasource="warehouse")
    expected = {
        "datasets": "Orders",
        "dimensions": "Country",
        "search-metrics": "Revenue",
    }
    with TestClient(app) as client:
        result = read(client, path, domain="Sales", dataset_name="Orders", **extra)
        assert [row[field] for row in result] == [expected[path]]
        if path == "datasets":
            result = read(
                client, path, domain="Sales", name="Inventory", dataset_name="Orders"
            )
            assert result["dataset_name"] == "Inventory"


def test_dataset_type_uses_workspace_catalog_and_never_stale_graph_connection_facts(
    graph,  # noqa: F811 - pytest fixture
):
    app, store, _ = graph
    store.nodes[DATA_SOURCE, "source-key"]["datasource_type"] = "mysql"
    with get_db(app.state.auth_config.control_plane_dsn) as connection:
        connection.execute(
            "INSERT INTO datasource VALUES (?, ?, ?, ?, ?)",
            ("tenant-2", "workspace-1", "warehouse", "mysql", 0),
        )
        connection.execute(
            "INSERT INTO datasource VALUES (?, ?, ?, ?, ?)",
            ("tenant-1", "workspace-2", "warehouse", "mysql", 0),
        )
        connection.commit()
    with TestClient(app) as client:
        assert (
            read(client, "datasets", domain="Sales")[0]["datasource_type"]
            == "postgresql"
        )
        with get_db(app.state.auth_config.control_plane_dsn) as connection:
            connection.execute(
                "UPDATE datasource SET is_deleted = 1 WHERE tenant_id = ? AND workspace_id = ?",
                ("tenant-1", "workspace-1"),
            )
            connection.commit()
        assert (
            read(client, "datasets", domain="Sales", name="Orders")["datasource_type"]
            == ""
        )

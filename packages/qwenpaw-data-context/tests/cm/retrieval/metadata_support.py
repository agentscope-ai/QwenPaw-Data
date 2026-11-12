"""Shared scoped metadata graph and mounted transport fixtures."""

from dataclasses import replace

import pytest
from context_manager.runtime import ContextRuntime
from context_manager.schema import (
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    DOMAIN,
    HAS_DATASET,
    MAPS_TO_FIELD,
    METRIC,
)
from context_manager.store import legacy_reads
from qwenpaw_data_auth.database import get_db

from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import mock_model_service
from cm.retrieval.mcp_support import build_app

PREFIX = "/api/v1/cm"
HEADERS = {"Authorization": "Bearer user-1"}
SCOPE = {"workspace_id": "workspace-1", "owner_id": "__global__", "zone": "metadata"}


def node(store, spec, key, **properties):
    store.nodes[spec, key] = {
        **SCOPE,
        "key": key,
        "name": key,
        "domain": "sales-id",
        "embedding": [0.1],
        "embedding_hash": "private-hash",
        **properties,
    }


@pytest.fixture(autouse=True)
def unavailable_query_embedding(monkeypatch, tmp_path):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("QWENPAW_DATA_LOCAL_CACHE", str(tmp_path))
    monkeypatch.setenv("DATAAGENT_SESSIONS_DB", str(tmp_path / "sessions.db"))
    manager = mock_model_service(error=RuntimeError("test provider unavailable"))
    monkeypatch.setattr(ContextRuntime, "models", lambda *args: manager)
    monkeypatch.setattr(legacy_reads, "search_fulltext_scores", metric_index_rows)


def metric_index_rows(store, spec, query, *, limit, scope, metric_domain=None):
    """Supply index rows; real backend acceptance covers native ranking."""
    records = store.search_fulltext(
        query,
        specs=(spec,),
        filters={"domain": metric_domain},
        limit=limit,
        scope=scope,
    )
    return tuple({"node": dict(node.properties), "score": 1.0} for node in records)


@pytest.fixture
def graph(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'catalog.db'}"
    app, store, runtime, _ = build_app(model_config_dsn=dsn)
    app.state.auth_config = replace(app.state.auth_config, control_plane_dsn=dsn)
    with get_db(dsn) as connection:
        connection.execute(
            "CREATE TABLE datasource (tenant_id TEXT, workspace_id TEXT, datasource_id TEXT, "
            "datasource_type TEXT, is_deleted INTEGER)"
        )
        connection.execute(
            "INSERT INTO datasource VALUES (?, ?, ?, ?, ?)",
            ("tenant-1", "workspace-1", "warehouse", "postgresql", 0),
        )
        connection.commit()
    node(store, DOMAIN, "domain-key", domain_id="sales-id", name="Sales")
    node(
        store,
        DATA_SOURCE,
        "source-key",
        datasource_id="warehouse",
        datasource_type="postgresql",
        config="secret-password",
    )
    node(
        store,
        METRIC,
        "metric-key",
        name="Revenue",
        synonyms=["Sales amount"],
        description="Booked revenue",
        priority="high",
    )
    node(
        store,
        DIMENSION,
        "dimension-key",
        name="Country",
        synonyms=["Region"],
        dimension_role="categorical",
    )
    node(
        store,
        DATASET,
        "dataset-key",
        name="Orders",
        description="Order facts",
        datasource_id="warehouse",
        source="public.orders",
    )
    node(
        store,
        DATASET_FIELD,
        "field-key",
        dataset="Orders",
        name="country",
        data_type="TEXT",
        description="Order country",
    )
    store.edges[DATASET_HAS_COLUMN_DATASET_FIELD, "dataset-key", "field-key"] = dict(
        SCOPE
    )
    store.edges[HAS_DATASET, "domain-key", "dataset-key"] = dict(SCOPE)
    store.edges[MAPS_TO_FIELD, "dimension-key", "field-key"] = {
        **SCOPE,
        "expr": "country",
        "value_example": ["CN", "ALL"],
        "total_value_marker": "ALL",
    }
    return app, store, runtime


def read(client, path, **params):
    response = client.get(
        f"{PREFIX}/{path}",
        params={"datasource_id": "warehouse", **params},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.headers["Deprecation"] == "true"
    return response.json()


@pytest.fixture
def imported_graph():
    import asyncio

    from context_manager.auth import WriteOwner
    from context_manager.metadata import MetadataImportService, SemanticImportRequest

    from cm.infrastructure.access.support import authorization
    from cm.infrastructure.models.support import FakeCaller, StaticModelService
    from cm.metadata.support import UnstackedImports

    app, store, _, _ = build_app()
    caller = FakeCaller()
    scope = store.access.issue_write_scope(
        authorization(role="tenant_admin"),
        owner=WriteOwner.WORKSPACE,
        datasource_id="warehouse",
        zone="metadata",
    )
    request = SemanticImportRequest.model_validate(
        {
            "datasource_id": "warehouse",
            "task_id": "transport-readback",
            "datasource": {
                "datasource_name": "Warehouse",
                "datasource_type": "postgresql",
            },
            "semantic": {
                "domains": [
                    {
                        "domain_id": "sales-id",
                        "name": "Sales",
                        "datasets": [
                            {
                                "name": "Orders",
                                "source": "SELECT * FROM orders",
                                "dataset_fields": [
                                    {
                                        "name": "country",
                                        "data_type": "text",
                                        "description": "Order country",
                                    }
                                ],
                            }
                        ],
                        "dimensions": [
                            {
                                "name": "Country",
                                "child_dimensions": ["City"],
                                "dimension_bindings": [
                                    {
                                        "dataset": "Orders",
                                        "expr": "country",
                                        "value_example": ["全部", "CN"],
                                    },
                                ],
                            },
                            {"name": "City"},
                        ],
                        "metrics": [
                            {
                                "name": "Revenue",
                                "description": "Total revenue",
                                "priority": "HIGH",
                                "metric_calibers": [
                                    {"dataset": "Orders", "expr": "sum(amount)"}
                                ],
                            }
                        ],
                    }
                ]
            },
        }
    )
    asyncio.run(
        MetadataImportService(
            store,
            StaticModelService(caller),
            executor=executor,
            recovery=UnstackedImports(store),
        ).import_snapshot(request, scope)
    )
    return app, caller

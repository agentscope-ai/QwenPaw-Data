"""Verify canonical Metadata snapshots and scoped graph import."""

from __future__ import annotations

import pytest
from context_manager.auth import ScopeBinding, WriteOwner
from context_manager.metadata import SemanticImportRequest
from context_manager.schema import GRAPH_SCHEMA, Glossary
from context_manager.sql import RdbConnector

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.models.support import FakeCaller, StaticModelService
from cm.infrastructure.storage.schema_support import make_glossary
from cm.metadata.support import MemoryGraphStore


def _glossary() -> Glossary:
    """Seed an isolated SQL-backed Glossary for runtime tests."""
    return make_glossary(GRAPH_SCHEMA)


def _metadata_runtime(store, connector, glossary):
    """Bind each Metadata importer to the authenticated user's test models."""
    return bound_context_runtime(
        store,
        connector,
        glossary,
        model_service_factory=lambda user_id: StaticModelService(
            FakeCaller(), user_id=user_id
        ),
    )


def _admin_authorization():
    """Return the trusted admin Auth result used for Metadata writes."""
    return authorization(user_id="admin-1", role="tenant_admin")


def _metadata_scope(runtime) -> ScopeBinding:
    """Issue one datasource-bound Workspace Metadata write capability."""
    return runtime.issue_graph_write_scope(
        _admin_authorization(),
        owner=WriteOwner.WORKSPACE,
        datasource_id="warehouse",
        zone="metadata",
    )


@pytest.mark.asyncio
async def test_current_snapshot_projects_the_complete_metadata_topology() -> None:
    """The explicit bridge can populate every independently declared MG label."""
    store = MemoryGraphStore()
    runtime = _metadata_runtime(
        store,
        RdbConnector(),
        _glossary(),
    )
    scope = _metadata_scope(runtime)
    request = SemanticImportRequest.model_validate(
        {
            "datasource_id": "warehouse",
            "datasource": {
                "datasource_name": "Warehouse",
                "datasource_type": "postgresql",
                "description": "Primary warehouse",
            },
            "tables": [
                {
                    "name": "public.orders",
                    "columns": [
                        {
                            "name": "country",
                            "data_type": "text",
                            "is_nullable": False,
                            "sample_values": ["CN", "US"],
                        }
                    ],
                }
            ],
            "semantic": {
                "domains": [
                    {
                        "name": "Sales",
                        "description": "Commercial semantics",
                        "datasets": [
                            {
                                "name": "orders",
                                "source": "public.orders",
                                "dataset_fields": [
                                    {
                                        "name": "country",
                                        "data_type": "text",
                                        "sample_values": ["CN", "US"],
                                    }
                                ],
                            }
                        ],
                        "dimensions": [
                            {
                                "name": "Country",
                                "dimension_type": "categorical",
                                "dimension_bindings": [
                                    {
                                        "dataset": "orders",
                                        "expr": "country",
                                        "value_example": ["CN", "US"],
                                    }
                                ],
                            }
                        ],
                        "metrics": [
                            {
                                "name": "OrderCount",
                                "metric_calibers": [
                                    {
                                        "dataset": "orders",
                                        "expr": "COUNT(*)",
                                        "time_window": "1d",
                                    }
                                ],
                            }
                        ],
                        "verified_queries": [
                            {
                                "question": "How many orders?",
                                "verified_sql": "SELECT COUNT(*) FROM public.orders",
                                "source_channel": "human",
                            }
                        ],
                    }
                ]
            },
        }
    )

    result = await runtime.metadata_importer(scope).import_snapshot(request, scope)

    assert result.stats["nodes"]["created"] == len(store.nodes)
    assert {spec.name for spec, _ in store.nodes} == {
        "DataSource",
        "Domain",
        "Dataset",
        "DatasetField",
        "Dimension",
        "Metric",
        "MetricCaliber",
        "VerifiedQuery",
        "Table",
        "Column",
    }
    assert {
        ("DataSource", "HAS", "Table"),
        ("Table", "HAS", "Column"),
        ("Domain", "HAS", "Dataset"),
        ("Domain", "HAS", "Dimension"),
        ("Domain", "HAS", "Metric"),
        ("Dataset", "HAS", "DatasetField"),
        ("Metric", "HAS", "MetricCaliber"),
        ("Dimension", "MAPS_TO_FIELD", "DatasetField"),
        ("Dataset", "AVAILABLE_IN", "DataSource"),
        ("MetricCaliber", "CALIBER_ON", "Dataset"),
        ("VerifiedQuery", "ANCHORED_TO", "Domain"),
    } <= {spec.triple for spec, _, _ in store.edges}
    runtime.close()


@pytest.mark.asyncio
async def test_import_maps_physical_joins_and_table_details() -> None:
    """Physical joins and table details cross the bridge into graph state."""
    store = MemoryGraphStore()
    runtime = _metadata_runtime(
        store,
        RdbConnector(),
        _glossary(),
    )
    scope = _metadata_scope(runtime)
    request = SemanticImportRequest.model_validate(
        {
            "datasource_id": "warehouse",
            "datasource": {
                "datasource_name": "Warehouse",
                "datasource_type": "postgresql",
            },
            "tables": [
                {
                    "name": "public.orders",
                    "comment": "Order facts",
                    "partition_keys": ["dt"],
                    "columns": [{"name": "order_id", "data_type": "bigint"}],
                },
                {
                    "name": "public.users",
                    "columns": [{"name": "user_id", "data_type": "bigint"}],
                },
            ],
            "physical_joins": [
                {
                    "left_table": "public.orders",
                    "left_columns": ["user_id"],
                    "right_table": "public.users",
                    "right_columns": ["user_id"],
                    "join_type": "left",
                }
            ],
            "semantic": {
                "domains": [
                    {"name": "Sales", "datasource_scope": ["warehouse"]},
                ]
            },
        }
    )

    result = await runtime.metadata_importer(scope).import_snapshot(request, scope)

    assert result.stats["edges"]["created"] == len(store.edges)
    read = store.access.issue_read_scope(
        _admin_authorization(),
        datasource_id="warehouse",
        zone="metadata",
    )
    table = store.get_nodes(
        GRAPH_SCHEMA.node("Table"),
        ("tbl:__global__:warehouse:public.orders",),
        scope=read,
    )[0]
    assert table is not None
    assert table.properties["comment"] == "Order facts"
    assert table.properties["partition_keys"] == ("dt",)
    join = store.get_edges(
        GRAPH_SCHEMA.edge(("Table", "JOINS", "Table")),
        (
            (
                "tbl:__global__:warehouse:public.orders",
                "tbl:__global__:warehouse:public.users",
            ),
        ),
        scope=read,
    )[0]
    assert join is not None
    assert join.properties["left_columns"] == ("user_id",)
    assert join.properties["join_type"] == "left"
    assert join.properties["source"] == "weave_p4"
    assert (
        join.properties["key"]
        == "pjn:warehouse:public.orders:user_id:public.users:user_id"
    )
    domain = store.get_nodes(
        GRAPH_SCHEMA.node("Domain"), ("dom:__global__:Sales",), scope=read
    )[0]
    assert domain is not None
    assert domain.properties["datasource_scope"] == ("warehouse",)
    await runtime.metadata_importer(scope).import_snapshot(
        request.model_copy(update={"drop_semantic_first": True, "physical_joins": []}),
        scope,
    )
    assert not store.list_edges(
        GRAPH_SCHEMA.edge(("Table", "JOINS", "Table")), scope=read
    )
    assert len(store.list_nodes(GRAPH_SCHEMA.node("Table"), scope=read)) == 2
    runtime.close()

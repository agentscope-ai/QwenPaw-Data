"""Settlement-card behavior through the native service and canonical graph."""

import json
from unittest.mock import AsyncMock

import pytest
from context_manager.auth import WriteOwner
from context_manager.metadata import MetadataSyncService
from context_manager.metadata.feedback import FeedbackError, MetadataFeedbackService
from context_manager.metadata.feedback.contracts import FeedbackCard
from context_manager.model import LlmCaller, LlmError, TextEmbedder
from context_manager.schema import (
    DATA_SOURCE,
    DATASET,
    DOMAIN,
    METRIC,
    METRIC_CALIBER,
)
from pydantic import TypeAdapter

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import mock_model_service
from cm.metadata.support import MemoryGraphStore


@pytest.fixture
def feedback_graph(database_engine):
    store = MemoryGraphStore()
    # Persistence uses canonical primitives; candidate ranking is injected here.
    store.search_vector = lambda *args, **kwargs: ()
    shared = store.access.issue_write_scope(
        authorization("admin", role="ws_admin"),
        owner=WriteOwner.WORKSPACE,
        zone="metadata",
    )
    datasource = store.access.narrow_write_scope(shared, datasource_id="warehouse")
    store.create_nodes(
        DOMAIN,
        (
            {
                "domain_id": "sales-id",
                "name": "Sales",
                "datasource_scope": ["warehouse"],
            },
        ),
        scope=shared,
    )
    store.create_nodes(
        DATA_SOURCE,
        ({"datasource_name": "Warehouse", "datasource_type": "postgresql"},),
        scope=datasource,
    )
    store.create_nodes(
        DATASET,
        (
            {
                "domain": "sales-id",
                "name": "orders",
                "source": "SELECT * FROM orders",
                "description": "Orders",
            },
        ),
        scope=datasource,
    )
    own = store.access.issue_write_scope(
        authorization("alice"), owner=WriteOwner.USER, zone="metadata"
    )
    MetadataSyncService(store, database_engine=database_engine).sync(
        store.access.issue_write_scope(authorization("alice"), owner=WriteOwner.USER)
    )
    embedder = AsyncMock(spec=TextEmbedder)
    embedder.embed.side_effect = lambda texts: tuple((0.1, 0.2) for _ in texts)
    llm = AsyncMock(spec=LlmCaller)
    llm.complete.return_value = '{"verdict":"CLEAR"}'
    models = mock_model_service(llm=llm, embedder=embedder)
    return store, own, shared, models


def card(kind="metric_caliber", **overrides):
    fields = {
        "metric_caliber": {
            "name": "Revenue",
            "description": "Revenue without refunds",
            "dataset": "orders",
            "formula": "sum(amount)",
            "unit": "CNY",
        },
        "dimension_def": {
            "name": "Region",
            "dataset_name": "orders",
            "maps_to_column": "region",
            "values": ["East", "West"],
        },
        "column_meaning": {
            "dataset_name": "orders",
            "column": "amount",
            "description": "Amount in CNY",
        },
        "dataset_usage": {"datasets": ["orders"], "description": "Use for daily sales"},
    }
    return TypeAdapter(FeedbackCard).validate_python(
        {
            "type": kind,
            "client_card_id": "card-1",
            "domain": "Sales",
            "datasource_id": "warehouse",
            "extra": {"session_id": "chat-1"},
            **fields[kind],
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_confirm_rescreens_alias_and_merges_without_process_cache(feedback_graph):
    store, own, _, models = feedback_graph
    original = store.get_nodes(
        METRIC,
        store.create_nodes(
            METRIC,
            (
                {
                    "domain": "sales-id",
                    "name": "Turnover",
                    "description": "Booked sales",
                },
            ),
            scope=own,
        ),
        scope=own,
    )[0]
    models.llm.return_value.__aenter__.return_value.complete.return_value = json.dumps(
        {"verdict": "ADDITIVE", "matched_key": original.key, "confidence": 0.95}
    )
    # Candidate retrieval models the database's fulltext selection; writeback uses real primitives.
    store.search_fulltext = lambda *a, **kw: (original,)
    ack = await MetadataFeedbackService(store, models, executor=executor).ingest(
        card(), own, mode="confirm"
    )
    assert ack.keys == [original.key]
    metric = store.get_nodes(METRIC, (original.key,), scope=own)[0]
    assert metric.properties["name"] == "Turnover"
    assert metric.properties["description"] == "Booked sales\nRevenue without refunds"
    assert (
        store.list_nodes(METRIC_CALIBER, scope=own)[0].properties["metric_name"]
        == "Turnover"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["CLEAR", "ADDITIVE"])
async def test_changed_fact_during_screening_is_not_silently_overwritten(
    feedback_graph,
    verdict,
):
    store, own, _, models = feedback_graph
    first = await MetadataFeedbackService(store, models, executor=executor).ingest(
        card(), own, mode="confirm"
    )
    ds_scope = store.access.narrow_write_scope(own, datasource_id="warehouse")

    async def concurrent_edit(*args, **kwargs):
        store.patch_nodes(
            METRIC,
            ((first.keys[0], {"description": "Another confirmed edit"}),),
            scope=ds_scope,
        )
        return json.dumps(
            {"verdict": verdict, "matched_key": first.keys[0], "confidence": 0.95}
        )

    models.llm.return_value.__aenter__.return_value.complete.side_effect = (
        concurrent_edit
    )
    with pytest.raises(FeedbackError) as err:
        await MetadataFeedbackService(store, models, executor=executor).ingest(
            card(description="Stale feedback"), own, mode="confirm"
        )
    assert err.value.code == "STALE_FEEDBACK"
    assert (
        store.get_nodes(METRIC, tuple(first.keys), scope=own)[0].properties[
            "description"
        ]
        == "Another confirmed edit"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        LlmError("unavailable"),
        "not JSON",
        '{"verdict":"ADDITIVE","matched_key":"unknown","confidence":0.95}',
    ],
)
async def test_expected_screening_failures_keep_explicit_feedback(
    feedback_graph, reply
):
    store, own, _, models = feedback_graph
    service = MetadataFeedbackService(store, models, executor=executor)
    await service.ingest(card(), own, mode="confirm")
    llm = models.llm.return_value.__aenter__.return_value
    if isinstance(reply, LlmError):
        llm.complete.side_effect = reply
    else:
        llm.complete.return_value = reply
    ack = await service.ingest(
        card(description="Confirmed meaning"), own, mode="confirm"
    )
    assert ack.screen.verdict == "SKIPPED"
    assert any(w.code == "SCREEN_SKIPPED" for w in ack.warnings)
    assert (
        store.get_nodes(METRIC, tuple(ack.keys), scope=own)[0].properties["description"]
        == "Confirmed meaning"
    )

"""Publish reviewed profiling facts through current weaving and scoped graph writes."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from context_manager.auth import GLOBAL_OWNER_ID, WriteOwner
from context_manager.schema import (
    CALIBER_ON,
    DATASET,
    DATASET_FIELD,
    DIMENSION,
    DOMAIN,
    HAS_CALIBER,
    METRIC,
    METRIC_CALIBER,
)
from profiling.db import init_probe_db
from profiling.models.probe import STAGES
from profiling.services import orchestrator
from profiling.stages import def_by_stage
from profiling.stub_provider import StubProvider
from profiling.tables import probe_stage
from semantic_config.engine import get_engine, get_session_factory
from semantic_config.tables import (
    dataset,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
)
from sqlalchemy import select

from cm.infrastructure.access.support import authorization
from cm.metadata.test_weaving import (
    editor as editor,  # noqa: PLC0414 - pytest fixture
)
from cm.metadata.test_weaving import (
    editor_database as editor_database,  # noqa: PLC0414 - pytest fixture
)
from cm.metadata.test_weaving import (
    graph_nodes,
)


@pytest_asyncio.fixture
async def profiling(editor, monkeypatch):  # noqa: F811 - imported pytest fixture
    await init_probe_db()
    monkeypatch.setattr(orchestrator, "load_provider", StubProvider)
    # Keep cross-stage fixture references independent of database row order.
    monkeypatch.setattr(
        "profiling.stub_provider._first_domain_name",
        lambda ctx, fallback: "STUB_DOMAIN",
    )
    source = await editor.request(
        "POST",
        "/datasource",
        json={
            "datasource_name": "用户行为仓库",
            "datasource_type": "postgresql",
            "config": {
                "host": "127.0.0.1",
                "port": 1,
                "database": "events",
                "user": "test",
                "password": "synthetic",
            },
        },
    )
    editor.datasource_id = source["datasource_id"]
    editor.probe_path = f"/probe/{editor.datasource_id}"
    await editor.request("GET", editor.probe_path + "/status")
    yield editor
    await asyncio.gather(*tuple(orchestrator._REGISTRY.values()))


async def review_stage(profiling, stage, *, complete=True):
    """Use the same start, review and completion APIs as the workbench."""
    definition = def_by_stage(stage)
    path = f"{profiling.probe_path}/{definition.path}"
    await profiling.request("POST", path + "/start")
    await asyncio.gather(*tuple(orchestrator._REGISTRY.values()))
    task = await profiling.request("GET", path + "/task")
    assert task["status"] == "SUCCESS", task
    rows = (await profiling.request("GET", path + "/results"))["records"]
    assert rows
    if definition.has_rows:
        await profiling.request(
            "POST", path + "/row/confirm", json={"row_ids": [row["id"] for row in rows]}
        )
    if complete:
        return await profiling.request("POST", path + "/complete")


async def review_until_last(profiling):
    for stage in STAGES[:-1]:
        result = await review_stage(profiling, stage)
        assert "graph_sync" not in result
    await review_stage(profiling, STAGES[-1], complete=False)
    assert not graph_nodes(profiling, METRIC)


@pytest.mark.asyncio
@pytest.mark.parametrize("editor", [None, "neo4j"], indirect=True)
@pytest.mark.parametrize("admin", ["admin", "second-admin"])
async def test_last_completion_publishes_committed_formal_facts_to_authoritative_mg(
    profiling, monkeypatch, admin
):
    workspace_id = profiling.scope.workspace_id
    profiling.identity["value"] = authorization(
        admin, role="ws_admin", workspace_id=workspace_id
    )
    private_scope = profiling.runtime.issue_graph_write_scope(
        authorization("user-2", workspace_id=workspace_id),
        owner=WriteOwner.USER,
        zone="metadata",
    )
    private = profiling.store.get_nodes(
        DOMAIN,
        profiling.store.create_nodes(
            DOMAIN,
            ({"domain_id": "private", "name": "私人业务域"},),
            scope=private_scope,
        ),
        scope=private_scope,
    )[0]
    await review_until_last(profiling)
    publish = type(profiling.publisher).import_snapshot
    observed = []

    async def publish_after_commit(service, request, scope, **kwargs):
        # Neither the request's write transaction nor the snapshot reader remains open.
        assert get_engine().sync_engine.pool.checkedout() == 0
        async with get_session_factory()() as db:
            formal = (await db.execute(select(metric_dict))).mappings().one()
            statuses = (await db.execute(select(probe_stage.c.status))).scalars().all()
        assert formal["metric_name"] == "日活用户数"
        assert formal["created_by"] == admin
        assert set(statuses) == {"COMPLETED"}
        assert scope.user_id == admin and scope.write_owner_id == GLOBAL_OWNER_ID
        assert scope.datasource_id is None
        assert (
            request.datasource_id == profiling.datasource_id
            and request.drop_semantic_first is True
        )
        observed.append(request.task_id)
        return await publish(service, request, scope, **kwargs)

    monkeypatch.setattr(
        type(profiling.publisher), "import_snapshot", publish_after_commit
    )
    result = await profiling.request(
        "POST", profiling.probe_path + "/metric-manage/complete"
    )
    assert result["graph_sync"] == {
        "synced": True,
        "task_id": observed[0],
        "status": "SUCCESS",
        "error_msg": None,
    }
    for table, spec, name in (
        (domain, DOMAIN, "STUB_DOMAIN"),
        (dataset, DATASET, "STUB_DATASET"),
        (dimension_dict, DIMENSION, "地区"),
        (metric_dict, METRIC, "日活用户数"),
    ):
        assert len(await profiling.rows(table)) == 1
        assert [node.properties["name"] for node in graph_nodes(profiling, spec)] == [
            name
        ]
    assert graph_nodes(profiling, DATASET_FIELD)[0].properties["name"] == "country_code"
    assert (
        len(await profiling.rows(metric_caliber))
        == len(graph_nodes(profiling, METRIC_CALIBER))
        == 1
    )
    for edge in (HAS_CALIBER, CALIBER_ON):
        assert len(profiling.store.list_edges(edge, scope=profiling.scope)) == 1
    assert (
        profiling.store.get_nodes(DOMAIN, (private.key,), scope=private_scope)[0]
        == private
    )
    assert profiling.caller.embed_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["embedding", "graph_write"])
async def test_graph_failure_keeps_formal_facts_and_completion_but_rolls_back_mg(
    profiling, monkeypatch, failure
):
    await review_until_last(profiling)
    before = tuple(
        {key: deepcopy(value) for key, value in records.items()}
        for records in (profiling.store.nodes, profiling.store.edges)
    )
    if failure == "embedding":
        monkeypatch.setattr(
            profiling.caller,
            "embed",
            AsyncMock(side_effect=RuntimeError("embedding unavailable")),
        )
    else:
        create = profiling.store.create_nodes

        def fail_after_write(spec, properties, *, scope):
            result = create(spec, properties, scope=scope)
            if spec is METRIC:
                raise RuntimeError("graph write unavailable")
            return result

        monkeypatch.setattr(profiling.store, "create_nodes", fail_after_write)
    result = await profiling.request(
        "POST", profiling.probe_path + "/metric-manage/complete", expected=503
    )
    assert "配置已保存" in result["detail"]
    assert "图同步失败" in result["detail"]
    assert (
        len(await profiling.rows(metric_dict))
        == len(await profiling.rows(metric_caliber))
        == 1
    )
    assert (await profiling.request("GET", profiling.probe_path + "/status"))[
        "overall_status"
    ] == "COMPLETED"
    assert (profiling.store.nodes, profiling.store.edges) == before

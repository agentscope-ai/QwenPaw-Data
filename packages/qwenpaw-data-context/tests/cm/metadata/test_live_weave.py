"""Opt-in Excel/editor → asynchronous weave → real embeddings/Neo4j journeys."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3

import pytest
import pytest_asyncio
from context_manager.auth import WriteOwner
from context_manager.schema import (
    COLUMN,
    DATA_SOURCE,
    METRIC,
    TABLE,
    VERIFIED_QUERY,
    Zone,
)
from openpyxl import load_workbook
from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.engine import reset_engine
from semantic_config.services import weave_task_service
from semantic_config.services.autoexplore import trigger

from cm.acceptance.test_graph_roundtrip import assert_embeddings
from cm.acceptance.test_graph_roundtrip import (
    live_graph as live_graph,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.access.support import authorization

EDITOR = "/api/semantic-config"


@pytest_asyncio.fixture
async def editor(live_graph, monkeypatch):  # noqa: F811 - imported pytest fixture
    live = live_graph
    # Reuse only this test's temporary Host database, never the configured business DB.
    monkeypatch.setattr(get_settings(), "db_url", f"sqlite:///{live.database_path}")
    with sqlite3.connect(live.database_path) as conn:
        conn.execute("ALTER TABLE datasource RENAME TO history_datasource_fixture")
    await init_db()
    with sqlite3.connect(live.database_path) as conn:
        conn.execute(
            "UPDATE role_bindings SET role = 'ws_admin' WHERE user_id = ?",
            (live.context().user_id,),
        )
    context = authorization(
        live.context().user_id, live.tenant, live.workspace, role="ws_admin"
    )
    scope = live.runtime.issue_graph_write_scope(
        context, owner=WriteOwner.WORKSPACE, zone="metadata"
    )
    live.weaving = live.runtime.metadata_weaving(scope)
    try:
        yield live
    finally:
        # These registries are used only to drain test-owned background work at shutdown.
        await asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS))
        await asyncio.gather(*tuple(trigger._ACTIVE_TASKS.values()))
        await reset_engine()


async def create_datasource(live, name):
    response = await live.request(
        "POST",
        f"{EDITOR}/datasource",
        json={
            "datasource_name": name,
            "datasource_type": "postgresql",
            "description": "合成数据源，仅导入表结构，不连接数仓。",
            "config": {
                "host": "127.0.0.1",
                "port": 1,
                "database": "retail",
                "user": "synthetic-reader",
                "password": "synthetic-only",
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["datasource_id"]


async def upload_workbook(live, datasource_id, domain_name):
    template = await live.request("GET", f"{EDITOR}/import/excel/template")
    assert template.status_code == 200, template.text
    workbook = load_workbook(io.BytesIO(template.content))
    rows = {
        "biz_domain": [{"domain_name": domain_name, "description": "订单和退款分析"}],
        "table": [{"table_name": "public.orders", "table_comment": "订单明细"}],
        "table_column": [
            {
                "table_name": "public.orders",
                "column_name": "region",
                "data_type": "text",
                "column_comment": "订单地区",
            },
            {
                "table_name": "public.orders",
                "column_name": "paid_amount",
                "data_type": "numeric",
                "column_comment": "实付金额，单位人民币元",
            },
        ],
        "dataset": [
            {
                "domain_name": domain_name,
                "dataset_name": "orders",
                "description": "已支付订单明细",
                "source": "public.orders",
            }
        ],
        "dataset_column": [
            {
                "domain_name": domain_name,
                "dataset_name": "orders",
                "field_name": "region",
                "description": "订单地区",
                "data_type": "text",
            }
        ],
        "dimension": [
            {
                "domain_name": domain_name,
                "dimension_name": "地区",
                "description": "订单所属地区",
                "synonyms": "区域",
                "dimension_type": "categorical",
            }
        ],
        "dataset_dimension": [
            {
                "domain_name": domain_name,
                "dimension_name": "地区",
                "dataset_name": "orders",
                "expr": "region",
                "value_example": "华东$$$华南",
            }
        ],
        "metric": [
            {
                "domain_name": domain_name,
                "metric_name": "GMV",
                "description": "GMV 是订单实付金额之和，不包括退款订单。",
                "synonyms": "成交额",
            }
        ],
        "metric_formula": [
            {
                "domain_name": domain_name,
                "metric_name": "GMV",
                "dataset_name": "orders",
                "expr": "SUM(paid_amount)",
                "time_window": "1d",
            }
        ],
        "verified_query": [
            {
                "domain_name": domain_name,
                "question": "各地区的实付成交额是多少？",
                "verified_sql": "SELECT region, SUM(paid_amount) FROM public.orders GROUP BY region",
            }
        ],
    }
    for name, records in rows.items():
        sheet = workbook[name]
        columns = [cell.value for cell in sheet[1]]
        for record in records:
            sheet.append([record.get(column) for column in columns])
    content = io.BytesIO()
    workbook.save(content)
    response = await live.request(
        "POST",
        f"{EDITOR}/import/excel",
        data={"datasource_id": datasource_id},
        files={
            "file": (
                "retail.xlsx",
                content.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True, response.text
    assert response.json()["errors"] == []
    assert sum(response.json()["summary"].values()) == 11
    assert response.json()["weave_task_id"]
    await asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS))
    automatic = await live.request(
        "GET", f"{EDITOR}/weave-task/{response.json()['weave_task_id']}"
    )
    assert automatic.json()["status"] == "SUCCESS", automatic.text


async def weave(live, datasource_id, mode, *, status="SUCCESS"):
    response = await live.request(
        "POST",
        f"{EDITOR}/weave-task/submit",
        json={
            "datasource_id": datasource_id,
            "task_name": f"真实联调 {mode}",
            "weave_mode": mode,
        },
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    assert response.json()["status"] == "RUNNING", response.text
    async with asyncio.timeout(120):
        while True:
            response = await live.request("GET", f"{EDITOR}/weave-task/{task_id}")
            assert response.status_code == 200, response.text
            if response.json()["status"] != "RUNNING":
                break
            await asyncio.sleep(0.1)
    assert response.json()["status"] == status, response.text
    assert response.json()["weave_mode"] == mode
    with sqlite3.connect(live.database_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM weave_task WHERE task_id = ?", (task_id,)
        ).fetchone()
    assert row["tenant_id"] == live.tenant
    assert row["workspace_id"] == live.workspace
    assert row["created_by"] == live.context().user_id
    assert row["datasource_id"] == datasource_id
    payload = json.loads(row["export_payload"])
    assert payload["datasource_id"] == datasource_id
    assert payload["drop_semantic_first"] is (mode == "FULL")
    assert "config" not in payload["datasource"]
    assert "synthetic-only" not in row["export_payload"]
    return response.json()


def datasource_nodes(live, datasource_id=None):
    """Read the authoritative MG written by weave, not the caller's mirror."""
    scope = live.read_scope(owner_id="__global__", zone="metadata")
    graph = live.runtime.explorer(scope).global_graph(
        scope,
        datasource_id=datasource_id or "",
        zone_mode="metadata",
        domain_roots_only=False,
        max_nodes=50_000,
        max_edges=1,
    )
    return tuple(
        live.store.get_nodes(
            live.store.schema.node(node["group"]), (node["id"],), scope=scope
        )[0]
        for node in graph["nodes"]
    )


def metadata_edges(live):
    scope = live.read_scope(owner_id="__global__", zone="metadata")
    return tuple(
        edge
        for spec in live.store.schema.edges
        if spec.source.zone is Zone.METADATA
        for edge in live.store.list_edges(spec, scope=scope, limit=1000)
    )


@pytest.mark.asyncio
async def test_excel_weave_preserves_incremental_full_and_datasource_boundaries(editor):
    live = editor
    datasource_id = await create_datasource(live, "星舟零售数仓")
    await upload_workbook(live, datasource_id, "零售经营")
    assert datasource_nodes(live), (
        "Excel must submit and complete its automatic FULL weave"
    )
    denied = await live.request(
        "POST",
        f"{EDITOR}/weave-task/submit",
        user="live-bob",
        json={
            "datasource_id": datasource_id,
            "task_name": "普通用户不得编织",
            "weave_mode": "FULL",
        },
    )
    assert denied.status_code in {400, 403}, denied.text
    with sqlite3.connect(live.database_path) as conn:
        assert conn.execute("SELECT count(*) FROM weave_task").fetchone()[0] == 1
    await weave(live, datasource_id, "FULL")
    initial = datasource_nodes(live, datasource_id)
    assert len(initial) == 11
    assert len({node.spec for node in initial}) == 10
    assert {node.properties["owner_id"] for node in initial} == {"__global__"}
    assert_embeddings(initial)
    physical_keys = {
        node.key for node in initial if node.spec in {DATA_SOURCE, TABLE, COLUMN}
    }
    metric = next(node for node in initial if node.spec is METRIC)
    verified_query = next(node for node in initial if node.spec is VERIFIED_QUERY)

    other_datasource = await create_datasource(live, "对照数仓")
    await upload_workbook(live, other_datasource, "对照经营")
    await weave(live, other_datasource, "FULL")
    other_before = {
        node.key: node.properties for node in datasource_nodes(live, other_datasource)
    }
    assert set(other_before).isdisjoint(node.key for node in initial)

    response = await live.request(
        "GET", f"{EDITOR}/metric-lib", params={"datasource_id": datasource_id}
    )
    assert response.status_code == 200, response.text
    metric_id = response.json()["records"][0]["metric_id"]
    description = "GMV 按人民币计价，扣除退款和取消订单后计算实付成交额。"
    updated = await live.request(
        "PUT", f"{EDITOR}/metric-lib/{metric_id}", json={"description": description}
    )
    assert updated.status_code == 200, updated.text
    queries = await live.request("GET", f"{EDITOR}/verified-query")
    assert queries.status_code == 200, queries.text
    query = next(
        row for row in queries.json()["records"] if row["domain_name"] == "零售经营"
    )
    removed = await live.request("DELETE", f"{EDITOR}/verified-query/{query['id']}")
    assert removed.status_code == 200, removed.text

    await weave(live, datasource_id, "INCREMENTAL")
    incremental = datasource_nodes(live, datasource_id)
    assert {node.key for node in incremental} == {node.key for node in initial} - {
        verified_query.key
    }
    changed_metric = next(node for node in incremental if node.spec is METRIC)
    assert changed_metric.key == metric.key
    assert changed_metric.properties["description"] == description
    assert (
        changed_metric.properties["embedding_hash"]
        != metric.properties["embedding_hash"]
    )
    assert_embeddings(incremental)

    await weave(live, datasource_id, "FULL")
    rebuilt = datasource_nodes(live, datasource_id)
    assert {node.key for node in rebuilt} == {node.key for node in initial} - {
        verified_query.key
    }
    assert {
        node.key for node in rebuilt if node.spec in {DATA_SOURCE, TABLE, COLUMN}
    } == physical_keys
    other_after = {
        node.key: node.properties for node in datasource_nodes(live, other_datasource)
    }
    assert other_after.keys() == other_before.keys()
    for key, properties in other_before.items():
        current = other_after[key]
        if properties.get("datasource_id") is None:
            # Workspace dictionaries are republished; only their write timestamp may change.
            assert current["updated_at"] >= properties["updated_at"]
            current = {**current, "updated_at": properties["updated_at"]}
        assert current == properties
    assert_embeddings(rebuilt)
    visible_keys = {node.key for node in datasource_nodes(live)}
    assert all(
        edge.source_key in visible_keys and edge.target_key in visible_keys
        for edge in metadata_edges(live)
    )
    # Weave publishes authority; each user explicitly pulls an independent mirror.
    private_keys = []
    for user in ("live-alice", "live-bob"):
        own_scope = live.read_scope(user, owner_id=user, zone="metadata")
        assert live.store.list_nodes(METRIC, scope=own_scope) == ()
        pulled = await live.request("POST", "/api/v1/mg/userspace/sync", user=user)
        assert pulled.status_code == 200, pulled.text
        assert pulled.json()["data"]["conflict_count"] == 0
        mirrors = tuple(
            node
            for spec in live.store.schema.nodes
            if spec.zone is Zone.METADATA and not spec.internal
            for node in live.store.list_nodes(spec, scope=own_scope, limit=1000)
        )
        assert len(mirrors) == len(visible_keys)
        assert {node.properties["owner_id"] for node in mirrors} == {user}
        assert all(
            "embedding" not in node.properties and "config" not in node.properties
            for node in mirrors
        )
        private_keys.append({node.key for node in mirrors})
        replay = await live.request("POST", "/api/v1/mg/userspace/sync", user=user)
        assert replay.status_code == 200, replay.text
        assert all(
            replay.json()["data"][name] == 0
            for name in ("created", "overwritten", "deleted", "conflict_count")
        )
    assert private_keys[0].isdisjoint(private_keys[1] | visible_keys)
    assert private_keys[1].isdisjoint(visible_keys)
    print(
        f"MG HTTP readback: initial={len(initial)}, rebuilt={len(rebuilt)}, Excel/FULL/INCREMENTAL/datasource isolation=ok"
    )

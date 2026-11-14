"""Opt-in real Auth/editor HTTP → Embedding → Neo4j → user mirror acceptance."""

import asyncio
import io
import sqlite3
from types import SimpleNamespace

import pytest
from context_manager.auth import WriteOwner
from context_manager.schema import (
    AVAILABLE_IN,
    DATASET,
    DATASET_FIELD,
    HAS_CALIBER,
    MAPS_TO_FIELD,
    METRIC,
    METRIC_CALIBER,
)
from openpyxl import load_workbook
from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.engine import reset_engine
from semantic_config.services import weave_task_service

from cm.acceptance.test_graph_roundtrip import assert_embeddings
from cm.acceptance.test_graph_roundtrip import (
    live_graph as live_graph,  # noqa: PLC0414 - pytest fixture
)
from cm.infrastructure.access.support import authorization
from cm.metadata.test_weaving import seed


@pytest.mark.asyncio
async def test_real_editor_crud_automatic_full_and_personal_mirrors(
    live_graph, monkeypatch  # noqa: F811 - imported pytest fixture
):
    live = live_graph
    monkeypatch.setattr(get_settings(), "db_url", f"sqlite:///{live.database_path}")
    # The shared chat fixture only needs a minimal datasource lookup table.
    # Keep it separately and let this editor journey create the real SQL schema.
    with sqlite3.connect(live.database_path) as connection:
        connection.execute(
            "ALTER TABLE datasource RENAME TO history_datasource_fixture"
        )
    await init_db()
    with sqlite3.connect(live.database_path) as connection:
        connection.execute(
            "UPDATE role_bindings SET role='ws_admin' WHERE user_id=?", ("live-alice",)
        )
    context = authorization("live-alice", live.tenant, live.workspace, role="ws_admin")
    scope = live.runtime.issue_graph_write_scope(
        context, owner=WriteOwner.WORKSPACE, zone="metadata"
    )

    async def no_discovery(**kwargs):
        return None

    monkeypatch.setattr(weave_task_service, "_fire_autoexplore_hook", no_discovery)

    async def request(method, path, **kwargs):
        response = await live.request(method, "/api/semantic-config" + path, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def nodes(spec):
        return live.store.list_nodes(spec, scope=scope, limit=1000)

    async def finished(task_id):
        await asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS))
        task = await request("GET", "/weave-task/" + task_id)
        assert task["status"] == "SUCCESS", task

    try:
        data = await seed(SimpleNamespace(request=request))
        original = nodes(METRIC)[0]
        assert_embeddings((original,))
        assert nodes(METRIC_CALIBER)[0].properties["expr"] == "SUM(amount)"
        assert original.properties["priority"] == "high"
        assert len(nodes(DATASET_FIELD)) == 2
        assert len(live.store.list_edges(MAPS_TO_FIELD, scope=scope)) == 1
        assert len(live.store.list_edges(AVAILABLE_IN, scope=scope)) == 1
        canvas = await live.request(
            "POST",
            "/api/v1/admin/explorer/global-graph",
            json={
                "datasource_id": data["datasource"],
                "zone_mode": "metadata",
                "domain_roots_only": False,
            },
        )
        assert canvas.status_code == 200, canvas.text
        graph = canvas.json()["data"]
        assert (len(graph["nodes"]), len(graph["edges"])) == (9, 10)
        domain = next(node for node in graph["nodes"] if node["group"] == "Domain")
        assert domain["label"] == "零售"
        assert (
            sum(
                node["group"] == "Metric"
                and node["props"].get("domain") == domain["label"]
                for node in graph["nodes"]
            )
            == 1
        )
        await request(
            "PUT",
            f"/metric-lib/{data['metric']['metric_id']}",
            json={"description": "按人民币计算实付金额，扣除退款"},
        )
        updated = nodes(METRIC)[0]
        assert updated.key == original.key
        assert (
            updated.properties["embedding_hash"]
            != original.properties["embedding_hash"]
        )
        for mode in ("FULL", "INCREMENTAL"):
            task = await request(
                "POST",
                "/weave-task/submit",
                json={
                    "datasource_id": data["datasource"],
                    "task_name": mode,
                    "weave_mode": mode,
                },
            )
            await finished(task["task_id"])
            assert nodes(METRIC)[0].key == original.key

        for user in ("live-alice", "live-bob"):
            response = await live.request(
                "POST", "/api/v1/mg/userspace/sync", user=user
            )
            assert response.status_code == 200, response.text
            personal = live.read_scope(user, owner_id=user, zone="metadata")
            metric = live.store.list_nodes(METRIC, scope=personal)[0]
            caliber = live.store.list_nodes(METRIC_CALIBER, scope=personal)[0]
            relation = live.store.list_edges(HAS_CALIBER, scope=personal)[0]
            assert (relation.source_key, relation.target_key) == (
                metric.key,
                caliber.key,
            )
        await request(
            "PUT",
            f"/dataset-meta/{data['dataset']['dataset_id']}",
            json={"dataset_name": "paid_orders"},
        )
        assert [node.properties["name"] for node in nodes(DATASET)] == ["paid_orders"]
        assert nodes(METRIC_CALIBER)[0].properties["dataset"] == "paid_orders"
        bob = live.read_scope("live-bob", owner_id="live-bob", zone="metadata")
        assert (
            live.store.list_nodes(DATASET, scope=bob)[0].properties["name"] == "orders"
        )
        pulled = await live.request(
            "POST", "/api/v1/mg/userspace/sync", user="live-bob"
        )
        assert pulled.status_code == 200, pulled.text
        assert (
            live.store.list_nodes(DATASET, scope=bob)[0].properties["name"]
            == "paid_orders"
        )
        rejected = await live.request(
            "PUT",
            f"/api/semantic-config/metric-lib/{data['metric']['metric_id']}",
            user="live-bob",
            json={"description": "不应写入"},
        )
        assert rejected.status_code == 403

        # Upload the frontend's real workbook contract and observe its automatic FULL task.
        template = await live.request(
            "GET",
            "/api/semantic-config/import/excel/template",
            params={"object": "metric"},
        )
        assert template.status_code == 200
        book = load_workbook(io.BytesIO(template.content))
        sheet = book["metric"]
        headers = [cell.value for cell in sheet[1]]
        row = {
            "domain_name": "零售",
            "metric_name": "订单数",
            "description": "已付款订单的数量",
            "priority": "HIGH",
        }
        sheet.append([row.get(header) for header in headers])
        output = io.BytesIO()
        book.save(output)
        uploaded = await request(
            "POST",
            "/import/excel",
            data={"datasource_id": data["datasource"]},
            files={
                "file": (
                    "synthetic.xlsx",
                    output.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert uploaded["success"], uploaded
        assert uploaded["weave_task_id"]
        await finished(uploaded["weave_task_id"])
        with sqlite3.connect(live.database_path) as connection:
            logs = connection.execute(
                "SELECT status FROM sync_log WHERE workspace_id=?", (live.workspace,)
            ).fetchall()
            assert logs and {row[0] for row in logs} == {"success"}
        await request(
            "DELETE", f"/metric-formula-lib/metric/{data['metric']['metric_id']}"
        )
        assert not nodes(METRIC_CALIBER)
    finally:
        await asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS))
        await reset_engine()

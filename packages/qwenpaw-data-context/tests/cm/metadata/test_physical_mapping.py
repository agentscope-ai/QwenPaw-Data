"""Physical mapping starts from the editor catalog and publishes authoritative MG."""

import asyncio
import json
import sqlite3

import pytest
from context_manager.auth import WriteOwner
from context_manager.metadata.physical import PhysicalMappingService
from context_manager.schema import (
    COLUMN,
    DATA_SOURCE,
    HAS_TABLE,
    TABLE,
    TABLE_HAS_COLUMN_COLUMN,
)
from context_manager.settings import clear_settings
from context_manager.sql import RdbConnector
from semantic_config.db import now_iso
from semantic_config.engine import get_session_factory
from semantic_config.services import weave_task_service
from semantic_config.services.autoexplore import trigger
from semantic_config.tables import explore_state, physical_table, table_column
from sqlalchemy import insert

from cm.infrastructure.access.support import authorization
from cm.metadata.test_weaving import (
    editor as editor,  # noqa: PLC0414 -- pytest fixture export
)
from cm.metadata.test_weaving import (
    editor_database as editor_database,  # noqa: PLC0414 -- pytest fixture export
)


@pytest.fixture
def source_file(tmp_path):
    filename = tmp_path / "orders.db"
    with sqlite3.connect(filename) as db:
        db.executescript("""
            CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT NOT NULL, amount NUMERIC);
            INSERT INTO orders VALUES (1, '华东', 12.5), (2, '华南', 20), (3, '华东', 30);
            CREATE VIEW order_regions AS SELECT region FROM orders;
            CREATE TABLE tmp_orders (id INTEGER);
            CREATE TABLE orders_20261004 (id INTEGER);
        """)
    return filename


async def create_source(editor, filename):  # noqa: F811 - imported pytest fixture
    value = await editor.request(
        "POST",
        "/datasource",
        json={
            "datasource_name": "合成订单源",
            "datasource_type": "sqlite",
            "config": {"path": str(filename)},
        },
    )
    return value["datasource_id"]


async def finish_explore():
    await asyncio.gather(*tuple(trigger._ACTIVE_TASKS.values()))


async def add_semantics(editor, datasource_id):  # noqa: F811 - imported pytest fixture
    domain = await editor.request(
        "POST", "/biz-domain", json={"domain_name": "零售", "description": "订单分析"}
    )
    await editor.request(
        "POST",
        "/dataset-meta",
        json={
            "datasource_id": datasource_id,
            "domain_id": domain["domain_id"],
            "dataset_name": "订单",
            "source": "main.orders",
            "description": "合成订单数据",
        },
    )


async def weave(editor, datasource_id):  # noqa: F811 - imported pytest fixture
    task = await editor.request(
        "POST",
        "/weave-task/submit",
        json={
            "task_name": "订单结构",
            "datasource_id": datasource_id,
            "weave_mode": "FULL",
        },
    )
    await asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS))
    await finish_explore()
    result = await editor.request("GET", "/weave-task/" + task["task_id"])
    assert result["status"] == "SUCCESS", result


def physical_nodes(editor, spec):  # noqa: F811 - imported pytest fixture
    return editor.store.list_nodes(spec, scope=editor.scope, limit=1000)


@pytest.fixture
def physical_writer(editor, monkeypatch, request):  # noqa: F811 - imported pytest fixture
    monkeypatch.setenv("CM_GRAPH_WRITE_BATCH_MAX_ROWS", str(request.param))
    clear_settings()
    return request.param, PhysicalMappingService(
        editor.store, RdbConnector(), run_sync=asyncio.to_thread
    )


@pytest.mark.parametrize(
    "editor", ["physical_mapping", "neo4j_physical_mapping"], indirect=True
)
async def test_first_weave_discovers_real_tables_columns_samples_and_edges(
    editor, source_file  # noqa: F811 - imported pytest fixture
):
    dsid = await create_source(editor, source_file)
    # A connection in SQL is enough to start; there need not be any MG projection.
    assert not physical_nodes(editor, DATA_SOURCE)
    await add_semantics(editor, dsid)
    await weave(editor, dsid)
    status = await editor.request("GET", f"/datasources/{dsid}/explore-scope")
    assert status["last_status"] == "ok", status
    assert json.loads(status["last_counts"]) == {
        "tables_new": 2,
        "tables_refreshed": 0,
        "columns": 4,
        "sampled_cols": 3,
        "skipped_manual": 0,
        "truncated": False,
    }
    tables = physical_nodes(editor, TABLE)
    columns = physical_nodes(editor, COLUMN)
    assert {node.properties["name"] for node in tables} == {
        "main.orders",
        "main.order_regions",
    }
    region = next(
        node
        for node in columns
        if node.properties["table"] == "main.orders"
        and node.properties["name"] == "region"
    )
    assert set(region.properties["sample_values"]) == {"华东", "华南"}
    assert region.properties["is_nullable"] is False
    assert all(
        node.properties["owner_id"] == "__global__" for node in (*tables, *columns)
    )
    assert len(editor.store.list_edges(HAS_TABLE, scope=editor.scope)) == 2
    assert (
        len(editor.store.list_edges(TABLE_HAS_COLUMN_COLUMN, scope=editor.scope)) == 4
    )
    # Physical mapping does not insert editor-maintained rows.
    assert not await editor.rows(physical_table)
    assert not await editor.rows(table_column)

    keys = {node.key for node in (*tables, *columns)}
    await weave(editor, dsid)
    again = await editor.request("GET", f"/datasources/{dsid}/explore-scope")
    assert again["last_run_at"] == status["last_run_at"]
    assert {
        node.key
        for node in (*physical_nodes(editor, TABLE), *physical_nodes(editor, COLUMN))
    } == keys
    assert len(editor.store.list_edges(HAS_TABLE, scope=editor.scope)) == 2
    assert (
        len(editor.store.list_edges(TABLE_HAS_COLUMN_COLUMN, scope=editor.scope)) == 4
    )

    # Physical mapping updates only the authoritative graph. Pulling into a private graph
    # remains an explicit, user-scoped MetadataSync operation.
    for user in ("alice", "bob"):
        scope = editor.runtime.issue_graph_write_scope(
            authorization(user, workspace_id=editor.scope.workspace_id),
            owner=WriteOwner.USER,
        )
        assert not editor.store.list_nodes(TABLE, scope=scope)
        sync = editor.runtime.metadata_sync(scope)
        sync.sync(scope)
        assert len(editor.store.list_nodes(TABLE, scope=scope)) == 2
        assert all(
            node.properties["owner_id"] == user
            for node in editor.store.list_nodes(COLUMN, scope=scope)
        )


async def test_manual_refresh_skips_unchanged_fingerprints_and_publishes_new_columns(
    editor, source_file  # noqa: F811 - imported pytest fixture
):
    dsid = await create_source(editor, source_file)
    for _ in range(2):
        result = await editor.request("POST", f"/datasources/{dsid}/explore")
        assert result["status"] == "accepted"
        await finish_explore()
    status = await editor.request("GET", f"/datasources/{dsid}/explore-scope")
    assert json.loads(status["last_counts"])["columns"] == 0
    previous = {
        node.key: node.properties["updated_at"]
        for node in physical_nodes(editor, COLUMN)
    }
    with sqlite3.connect(source_file) as db:
        db.execute("ALTER TABLE orders ADD COLUMN status TEXT")
        db.execute("UPDATE orders SET status = '已支付'")
    await editor.request("POST", f"/datasources/{dsid}/explore")
    await finish_explore()
    status = await editor.request("GET", f"/datasources/{dsid}/explore-scope")
    assert json.loads(status["last_counts"])["tables_refreshed"] == 1
    columns = physical_nodes(editor, COLUMN)
    added = next(node for node in columns if node.properties["name"] == "status")
    assert added.properties["sample_values"] == ("已支付",)
    assert all(
        node.properties["updated_at"] == previous[node.key]
        for node in columns
        if node.key in previous
    )
    assert not editor.caller.complete_calls
    assert not editor.caller.embed_calls


async def test_manual_columns_and_table_comments_survive_partial_mapping(
    editor, source_file  # noqa: F811 - imported pytest fixture
):
    dsid = await create_source(editor, source_file)
    scope = editor.store.access.narrow_write_scope(editor.scope, datasource_id=dsid)
    table = editor.store.get_nodes(
        TABLE,
        editor.store.create_nodes(
            TABLE,
            (
                {
                    "name": "main.orders",
                    "comment": "人工表说明",
                    "partition_keys": ("day",),
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )[0]
    column = editor.store.get_nodes(
        COLUMN,
        editor.store.create_nodes(
            COLUMN,
            (
                {
                    "table": "main.orders",
                    "name": "region",
                    "data_type": "varchar",
                    "comment": "人工地区说明",
                    "sample_values": ("人工样例",),
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )[0]
    async with get_session_factory()() as db:
        values = {
            "tenant_id": "tenant-1",
            "workspace_id": editor.scope.workspace_id,
            "created_by": "admin",
            "updated_by": "admin",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        await db.execute(
            insert(physical_table).values(
                **values, id=1, datasource_id=dsid, table_name="main.orders"
            )
        )
        await db.execute(
            insert(table_column).values(
                **values,
                datasource_id=dsid,
                table_id=1,
                column_name="region",
                data_type="varchar",
            )
        )
        await db.commit()
    await editor.request("POST", f"/datasources/{dsid}/explore")
    await finish_explore()
    assert (
        editor.store.get_nodes(TABLE, (table.key,), scope=scope)[0].properties
        == table.properties
    )
    assert (
        editor.store.get_nodes(COLUMN, (column.key,), scope=scope)[0].properties
        == column.properties
    )
    assert {node.properties["name"] for node in physical_nodes(editor, COLUMN)} == {
        "region",
        "id",
        "amount",
    }
    states = await editor.rows(explore_state)
    assert all(row["status"] == "ok" for row in states)
    async with get_session_factory()() as db:
        for name, dtype in (("id", "integer"), ("amount", "numeric")):
            await db.execute(
                insert(table_column).values(
                    **values,
                    datasource_id=dsid,
                    table_id=1,
                    column_name=name,
                    data_type=dtype,
                )
            )
        await db.commit()
    await editor.request("POST", f"/datasources/{dsid}/explore")
    await finish_explore()
    states = await editor.rows(explore_state)
    assert (
        next(row for row in states if row["table_name"] == "main.orders")["status"]
        == "skipped_manual"
    )


@pytest.mark.parametrize("physical_writer", [3, 100], indirect=True)
async def test_physical_failure_rolls_back_current_batch_and_retry_completes(
    editor, physical_writer, source_file  # noqa: F811 - imported pytest fixture
):
    limit, writer = physical_writer
    dsid = await create_source(editor, source_file)
    rows = [
        {
            "datasource_id": dsid,
            "table_name": "main.orders",
            "column_name": name,
            "data_type": "integer",
        }
        for name in ("id", "amount", "quantity")
    ]
    editor.store.fail_edge_write = True
    with pytest.raises(RuntimeError):
        writer.write_rows("table_column", rows, dsid, editor.scope)
    if limit == 3:
        assert len(physical_nodes(editor, DATA_SOURCE)) == 1
        assert len(physical_nodes(editor, TABLE)) == 1
        assert {node.properties["name"] for node in physical_nodes(editor, COLUMN)} == {
            "id"
        }
    else:
        assert not physical_nodes(editor, DATA_SOURCE)
        assert not physical_nodes(editor, TABLE)
        assert not physical_nodes(editor, COLUMN)
    assert not editor.store.list_edges(HAS_TABLE, scope=editor.scope)
    assert not editor.store.list_edges(TABLE_HAS_COLUMN_COLUMN, scope=editor.scope)

    editor.store.fail_edge_write = False
    writer.write_rows("table_column", rows, dsid, editor.scope)
    columns = physical_nodes(editor, COLUMN)
    assert {node.properties["name"] for node in columns} == {"id", "amount", "quantity"}
    assert len(editor.store.list_edges(HAS_TABLE, scope=editor.scope)) == 1
    assert {
        edge.target_key
        for edge in editor.store.list_edges(TABLE_HAS_COLUMN_COLUMN, scope=editor.scope)
    } == {node.key for node in columns}

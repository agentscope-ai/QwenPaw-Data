"""Editor HTTP commits, graph publication, task feedback and user mirrors agree."""

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from context_manager.api.app import create_api_app
from context_manager.auth import WriteOwner, bind_authorization
from context_manager.db import initialize_database
from context_manager.metadata.weaving.integration import mount_editor
from context_manager.schema import (
    AVAILABLE_IN,
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    DOMAIN,
    MAPS_TO_FIELD,
    METRIC,
    METRIC_CALIBER,
    VERIFIED_QUERY,
)
from context_manager.schema.relational import sync_change, sync_log
from context_manager.settings.db import DbSettings
from context_manager.sql import RdbConnector
from qwenpaw_data_auth import Principal
from semantic_config import tables
from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.engine import get_session_factory, reset_engine
from semantic_config.principal import current_principal
from semantic_config.services import weave_assembler, weave_task_service
from semantic_config.services.autoexplore import trigger
from sqlalchemy import create_engine, insert, inspect, select, text, update
from sqlalchemy.engine import make_url

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.models.support import FakeCaller, StaticModelService
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.support import MemoryGraphStore


@pytest_asyncio.fixture
async def editor_database(tmp_path):
    url = os.environ.get("CM_WEAVING_TEST_PG_URL")
    if not url:
        yield f"sqlite:///{tmp_path / 'editor.db'}"
        return
    name = "weaving_" + uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(url).set(database=name).render_as_string(hide_password=False)
    finally:
        from context_manager.db import close_database

        close_database()
        await reset_engine()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


@pytest_asyncio.fixture
async def editor(tmp_path, monkeypatch, request, editor_database):
    mode = getattr(request, "param", "")
    native = mode in {"neo4j", "neo4j_physical_mapping"}
    if native and os.environ.get("CM_RUN_LIVE_GRAPH_TESTS") != "1":
        pytest.skip(
            "set CM_RUN_LIVE_GRAPH_TESTS=1 for editor-to-Explorer native queries"
        )
    monkeypatch.setattr(get_settings(), "db_url", editor_database)
    await init_db()
    database = initialize_database(DbSettings(url=get_settings().db_url))
    database.initialize()
    store = MemoryGraphStore()
    workspace_id = "weaving-explorer-" + uuid4().hex if native else "workspace-1"
    glossary = make_glossary(
        store.schema, engine=database.engine, workspace_id=workspace_id
    )
    if native:
        from context_manager.auth import GraphScopeAuthority
        from context_manager.store.neo4j import (
            GraphTarget,
            Neo4jGraphStore,
            Neo4jRuntime,
        )
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            os.environ["CM_LIVE_NEO4J_URI"],
            auth=("neo4j", os.environ["CM_LIVE_NEO4J_PASSWORD"]),
        )
        store = Neo4jGraphStore(
            Neo4jRuntime(driver, GraphTarget("tenant-1", workspace_id)),
            store.schema,
            GraphScopeAuthority(),
            glossary,
        )
    if native and database.engine.dialect.name == "postgresql":
        # Shared Neo4j has globally unique graph keys. Give this isolated editor
        # database a distinct numeric ID range rather than touching existing graph data.
        first_id = 1_000_000 + int(uuid4().hex[:6], 16) * 100
        with database.engine.begin() as connection:
            existing = set(inspect(connection).get_table_names())
            for table in tables.metadata.sorted_tables:
                if (
                    table.name in existing
                    and "id" in table.c
                    and table.c.id.primary_key
                ):
                    sequence = connection.execute(
                        text("SELECT pg_get_serial_sequence(:name, 'id')"),
                        {"name": table.name},
                    ).scalar()
                    if sequence is not None:
                        connection.execute(
                            text("SELECT setval(:sequence, :first_id, false)"),
                            {"sequence": sequence, "first_id": first_id},
                        )
    caller = FakeCaller(dimensions=1024 if native else 3)
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        glossary,
        database_engine=database.engine,
        model_service_factory=lambda user: StaticModelService(
            caller, user_id=user, workspace_id=workspace_id
        ),
        workspace_id=workspace_id,
        model_config_dsn=editor_database,
    )
    identity = {
        "value": authorization("admin", role="ws_admin", workspace_id=workspace_id)
    }
    scope = runtime.issue_graph_write_scope(
        identity["value"], owner=WriteOwner.WORKSPACE, zone="metadata"
    )
    publisher = runtime.metadata_weaving(scope)
    app = create_api_app()
    app.state.context_runtime = runtime
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=identity["value"].user_id,
        tenant_id=identity["value"].tenant_id,
        workspace_ids=list(identity["value"].workspace_ids),
        active_workspace_id=identity["value"].active_workspace_id,
        role=identity["value"].active_workspace_role,
    )
    mount_editor(app)

    # Physical mapping belongs to capability 11, not this weaving regression.
    async def no_physical_mapping(**kwargs):
        return None

    if mode not in {"physical_mapping", "neo4j_physical_mapping"}:
        monkeypatch.setattr(
            weave_task_service, "_fire_autoexplore_hook", no_physical_mapping
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:

        async def request(method, path, *, expected=200, **kwargs):
            with bind_authorization(identity["value"]):
                response = await client.request(
                    method, "/api/semantic-config" + path, **kwargs
                )
            assert response.status_code == expected, response.text
            return response.json()

        async def rows(table):
            async with get_session_factory()() as db:
                return (
                    (await db.execute(select(table).order_by(table.c.id)))
                    .mappings()
                    .all()
                )

        yield SimpleNamespace(
            request=request,
            runtime=runtime,
            scope=scope,
            publisher=publisher,
            store=store,
            identity=identity,
            caller=caller,
            rows=rows,
            app=app,
        )
    await asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS))
    await asyncio.gather(*tuple(trigger._ACTIVE_TASKS.values()))
    if native:
        with driver.session() as session:
            session.run(
                "MATCH (n {workspace_id: $workspace}) DETACH DELETE n",
                workspace=workspace_id,
            ).consume()
    await runtime.aclose()
    await reset_engine()


async def seed(editor):
    def post(path, **body):
        return editor.request("POST", path, json=body)
    source = await post(
        "/datasource",
        datasource_name="订单仓库",
        datasource_type="postgresql",
        config={
            "host": "127.0.0.1",
            "port": 1,
            "database": "orders",
            "user": "test",
            "password": "synthetic",
        },
    )
    dsid = source["datasource_id"]
    domain = await post("/biz-domain", domain_name="零售", description="订单和退款分析")
    dom = domain["domain_id"]
    dataset = await post(
        "/dataset-meta",
        datasource_id=dsid,
        domain_id=dom,
        dataset_name="orders",
        source="public.orders",
        description="已支付订单",
    )
    did = dataset["dataset_id"]
    field = await post(
        "/dataset-column-meta",
        dataset_id=did,
        field_name="amount",
        description="实付金额",
        data_type="numeric",
    )
    dimension = await post(
        "/dimension",
        domain_id=dom,
        dimension_name="地区",
        description="订单地区",
        dimension_type="categorical",
    )
    metric = await post(
        "/metric-lib",
        domain_id=dom,
        metric_name="GMV",
        description="实付成交额",
        priority="HIGH",
    )
    binding = await post(
        "/dataset-dimension",
        dataset_id=did,
        dimension_id=dimension["dimension_id"],
        expr="region",
        value_example="华东$$$华南",
    )
    caliber = await post(
        "/metric-formula-lib",
        dataset_id=did,
        metric_id=metric["metric_id"],
        expr="SUM(amount)",
        time_window="day",
        is_default=True,
    )
    query = await post(
        "/verified-query",
        domain_id=dom,
        question="昨天成交额多少？",
        verified_sql="SELECT SUM(amount) FROM orders",
    )
    return {
        "datasource": dsid,
        "domain": domain,
        "dataset": dataset,
        "field": field,
        "dimension": dimension,
        "metric": metric,
        "binding": binding,
        "caliber": caliber,
        "query": query,
    }


def graph_nodes(editor, spec):
    return editor.store.list_nodes(spec, scope=editor.scope, limit=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("datasource_id", [None, " "])
async def test_unscoped_legacy_dataset_does_not_block_domain_publication(
    editor, datasource_id
):
    data = await seed(editor)
    dataset_id = data["dataset"]["dataset_id"]
    async with get_session_factory()() as db:
        await db.execute(
            update(tables.dataset)
            .where(tables.dataset.c.id == dataset_id)
            .values(datasource_id=datasource_id)
        )
        await db.commit()

    saved = await editor.request(
        "PUT",
        f"/dataset-meta/{dataset_id}",
        json={"description": "Updated description"},
    )
    assert saved["description"] == "Updated description"
    assert saved["datasource_id"] == datasource_id
    [graph_dataset] = graph_nodes(editor, DATASET)
    assert graph_dataset.properties["description"] == data["dataset"]["description"]
    dataset_log = (await editor.rows(sync_log))[-1]
    assert dataset_log["entity"] == "dataset-meta"
    assert dataset_log["applied"] == 0
    assert json.loads(dataset_log["skipped"]) == [
        {"dataset_id": dataset_id, "reason": "missing_datasource_id"}
    ]

    domain = await editor.request(
        "POST", "/biz-domain", json={"domain_name": "Support", "description": "Tickets"}
    )
    await editor.request(
        "PUT", f"/biz-domain/{domain['domain_id']}", json={"domain_name": "Operations"}
    )
    assert {row.properties["name"] for row in graph_nodes(editor, DOMAIN)} == {
        "零售",
        "Operations",
    }


@pytest.mark.asyncio
async def test_eight_editor_entities_publish_after_commit_and_record_results(editor):
    data = await seed(editor)
    for spec in (
        DOMAIN,
        DATA_SOURCE,
        DATASET,
        DATASET_FIELD,
        DIMENSION,
        METRIC,
        METRIC_CALIBER,
        VERIFIED_QUERY,
    ):
        rows = graph_nodes(editor, spec)
        assert len(rows) == (2 if spec is DATASET_FIELD else 1), spec.name
        assert rows[0].properties["owner_id"] == "__global__"
        if spec.embedding_fields:
            assert rows[0].properties["embedding_hash"]
    bindings = editor.store.list_edges(MAPS_TO_FIELD, scope=editor.scope)
    assert bindings[0].properties["value_example"] == ("华东", "华南")
    assert len(editor.store.nodes) == 9
    assert len(editor.store.edges) == 10
    assert graph_nodes(editor, DOMAIN)[0].properties["name"] == "零售"
    dataset = graph_nodes(editor, DATASET)[0]
    assert dataset.properties["source"] == "public.orders"
    assert dataset.properties["datasource_id"] == data["datasource"]
    assert "source_datasource" not in dataset.properties
    assert (
        editor.store.list_edges(AVAILABLE_IN, scope=editor.scope)[0].source_key
        == dataset.key
    )
    metric = graph_nodes(editor, METRIC)[0]
    assert metric.properties["priority"] == "high"
    caliber = graph_nodes(editor, METRIC_CALIBER)[0]
    assert caliber.properties["expr"] == "SUM(amount)"
    assert caliber.properties["metric_name"] == metric.properties["name"]
    dimension = graph_nodes(editor, DIMENSION)[0]
    assert dimension.properties["dimension_role"] == "categorical"
    logs = await editor.rows(sync_log)
    assert len(logs) == 8
    assert {row["status"] for row in logs} == {"success"}, logs
    assert all(row["created_by"] == "admin" for row in logs)
    assert {row["result"] for row in await editor.rows(sync_change)} == {"applied"}


@pytest.mark.asyncio
@pytest.mark.parametrize("editor", ["", "neo4j"], indirect=True)
@pytest.mark.parametrize(
    "entity,id_field,field,value,spec",
    [
        ("domain", "domain_id", "domain_name", "订单分析", DOMAIN),
        ("dataset", "dataset_id", "dataset_name", "paid_orders", DATASET),
        ("field", "id", "field_name", "paid_amount", DATASET_FIELD),
        ("dimension", "dimension_id", "dimension_name", "销售区域", DIMENSION),
        ("metric", "metric_id", "metric_name", "成交额", METRIC),
        ("caliber", "id", "time_window", "month", METRIC_CALIBER),
        ("query", "id", "question", "上个月成交额多少？", VERIFIED_QUERY),
    ],
)
async def test_rename_preserves_anchors_and_rekeys_dependents(
    editor, entity, id_field, field, value, spec
):
    data = await seed(editor)
    old_keys = {record.key for record in graph_nodes(editor, spec)}
    if entity == "field":
        old_keys = {
            record.key
            for record in graph_nodes(editor, spec)
            if record.properties["name"] == "amount"
        }
    paths = {
        "domain": "biz-domain",
        "dataset": "dataset-meta",
        "field": "dataset-column-meta",
        "dimension": "dimension",
        "metric": "metric-lib",
        "caliber": "metric-formula-lib",
        "query": "verified-query",
    }
    await editor.request(
        "PUT", f"/{paths[entity]}/{data[entity][id_field]}", json={field: value}
    )
    assert (await editor.rows(sync_log))[-1]["status"] == "success"
    new_keys = {record.key for record in graph_nodes(editor, spec)}
    if entity == "field":
        new_keys = {
            record.key
            for record in graph_nodes(editor, spec)
            if record.properties["name"] == value
        }
    assert old_keys == new_keys if entity == "domain" else old_keys.isdisjoint(new_keys)
    dataset = graph_nodes(editor, DATASET)[0]
    fields = {record.key for record in graph_nodes(editor, DATASET_FIELD)}
    assert len(fields) == 2
    assert {
        record.properties["dataset"] for record in graph_nodes(editor, DATASET_FIELD)
    } == {dataset.properties["name"]}
    assert {
        edge.target_key
        for edge in editor.store.list_edges(
            DATASET_HAS_COLUMN_DATASET_FIELD, scope=editor.scope
        )
        if edge.source_key == dataset.key
    } == fields
    assert len(graph_nodes(editor, METRIC_CALIBER)) == 1


@pytest.mark.asyncio
async def test_shared_weaving_keeps_datasource_facts_and_workspace_isolation(editor):
    data = await seed(editor)
    other_source = await editor.request(
        "POST",
        "/datasource",
        json={
            "datasource_name": "另一个仓库",
            "datasource_type": "sqlite",
            "config": {"path": ":memory:"},
        },
    )
    domain = await editor.request(
        "POST", "/biz-domain", json={"domain_name": "售后", "description": "退款业务"}
    )
    dataset = await editor.request(
        "POST",
        "/dataset-meta",
        json={
            "datasource_id": other_source["datasource_id"],
            "domain_id": domain["domain_id"],
            "dataset_name": "refunds",
            "source": "refunds",
        },
    )
    metric = await editor.request(
        "POST",
        "/metric-lib",
        json={
            "domain_id": domain["domain_id"],
            "metric_name": "退款额",
        },
    )
    await editor.request(
        "POST",
        "/metric-formula-lib",
        json={
            "metric_id": metric["metric_id"],
            "dataset_id": dataset["dataset_id"],
            "expr": "SUM(amount)",
            "time_window": "day",
        },
    )
    foreign = {
        node.key: node.properties
        for spec in (DATASET, METRIC_CALIBER)
        for node in graph_nodes(editor, spec)
        if node.properties.get("datasource_id") == other_source["datasource_id"]
    }
    async with get_session_factory()() as db:
        for tenant, workspace, deleted in (
            (editor.scope.tenant_id, "other-workspace", 0),
            ("other-tenant", editor.scope.workspace_id, 0),
            (editor.scope.tenant_id, editor.scope.workspace_id, 1),
        ):
            audit = {
                "tenant_id": tenant,
                "workspace_id": workspace,
                "is_deleted": deleted,
            }
            domain_id = (
                await db.execute(
                    insert(tables.domain)
                    .values(
                        domain_name="excluded",
                        **audit,
                    )
                    .returning(tables.domain.c.id)
                )
            ).scalar_one()
            await db.execute(
                insert(tables.metric_dict).values(
                    domain_id=domain_id,
                    metric_name="excluded",
                    **audit,
                )
            )
            # A foreign child must stay excluded even if it references a visible domain.
            await db.execute(
                insert(tables.dimension_dict).values(
                    domain_id=data["domain"]["domain_id"],
                    dimension_name="excluded",
                    **audit,
                )
            )
            await db.execute(
                insert(tables.verified_query).values(
                    domain_id=data["domain"]["domain_id"],
                    question="excluded",
                    verified_sql="SELECT 1",
                    **audit,
                )
            )
        await db.commit()
        payload = await weave_assembler.assemble(
            db,
            data["datasource"],
            "scope-check",
            editor.scope.tenant_id,
            editor.scope.workspace_id,
        )
    domains = {item["name"]: item for item in payload["semantic"]["domains"]}
    assert set(domains) == {"零售", "售后"}
    assert [item["name"] for item in domains["零售"]["datasets"]] == ["orders"]
    assert [item["name"] for item in domains["零售"]["dimensions"]] == ["地区"]
    assert [item["question"] for item in domains["零售"]["verified_queries"]] == [
        "昨天成交额多少？"
    ]
    assert [item["name"] for item in domains["售后"]["metrics"]] == ["退款额"]
    assert not domains["售后"].get("datasets")
    assert not domains["售后"]["metrics"][0].get("metric_calibers")

    task = await editor.request(
        "POST",
        "/weave-task/submit",
        json={
            "task_name": "FULL",
            "datasource_id": data["datasource"],
            "weave_mode": "FULL",
        },
    )
    await asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS))
    result = await editor.request("GET", "/weave-task/" + task["task_id"])
    assert result["status"] == "SUCCESS", result
    assert foreign == {
        node.key: node.properties
        for spec in (DATASET, METRIC_CALIBER)
        for node in graph_nodes(editor, spec)
        if node.properties.get("datasource_id") == other_source["datasource_id"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("editor", ["", "neo4j"], indirect=True)
async def test_bulk_deletes_remove_only_requested_bindings_and_calibers(editor):
    data = await seed(editor)
    editor.store.create_nodes(
        DATASET_FIELD,
        (
            {
                "domain": str(data["domain"]["domain_id"]),
                "dataset": "orders",
                "name": "legacy_orphan",
            },
        ),
        scope=editor.store.access.narrow_write_scope(
            editor.scope, datasource_id=data["datasource"]
        ),
    )
    await editor.request(
        "DELETE", f"/metric-formula-lib/metric/{data['metric']['metric_id']}"
    )
    assert not graph_nodes(editor, METRIC_CALIBER)
    assert len(graph_nodes(editor, METRIC)) == 1
    await editor.request(
        "DELETE", f"/dataset-dimension/dataset/{data['dataset']['dataset_id']}"
    )
    assert not editor.store.list_edges(MAPS_TO_FIELD, scope=editor.scope)
    assert len(graph_nodes(editor, DIMENSION)) == 1
    assert len(graph_nodes(editor, DATASET)) == 1
    for entity, path, identity in (
        ("query", "verified-query", "id"),
        ("field", "dataset-column-meta", "id"),
        ("metric", "metric-lib", "metric_id"),
        ("dimension", "dimension", "dimension_id"),
        ("dataset", "dataset-meta", "dataset_id"),
        ("domain", "biz-domain", "domain_id"),
    ):
        await editor.request("DELETE", f"/{path}/{data[entity][identity]}")
    assert len(graph_nodes(editor, DATA_SOURCE)) == 1
    assert not any(
        graph_nodes(editor, spec)
        for spec in (
            DOMAIN,
            DATASET,
            DATASET_FIELD,
            DIMENSION,
            METRIC,
            METRIC_CALIBER,
            VERIFIED_QUERY,
        )
    )
    assert {row["status"] for row in await editor.rows(sync_log)} == {"success"}


@pytest.mark.asyncio
@pytest.mark.parametrize("editor", ["", "neo4j"], indirect=True)
async def test_binding_cleanup_keeps_fields_used_by_other_bindings_or_explicit_definitions(
    editor,
):
    data = await seed(editor)
    dimension = await editor.request(
        "POST",
        "/dimension",
        json={
            "domain_id": data["domain"]["domain_id"],
            "dimension_name": "配送区域",
        },
    )
    other = await editor.request(
        "POST",
        "/dataset-dimension",
        json={
            "dataset_id": data["dataset"]["dataset_id"],
            "dimension_id": dimension["dimension_id"],
            "expr": "region",
        },
    )
    await editor.request(
        "PUT", f"/dataset-dimension/{data['binding']['id']}", json={"expr": "amount"}
    )
    assert {node.properties["name"] for node in graph_nodes(editor, DATASET_FIELD)} == {
        "amount",
        "region",
    }
    assert {
        edge.properties["expr"]
        for edge in editor.store.list_edges(MAPS_TO_FIELD, scope=editor.scope)
    } == {"amount", "region"}
    await editor.request("DELETE", f"/dataset-dimension/{other['id']}")
    await editor.request("DELETE", f"/dataset-dimension/{data['binding']['id']}")
    assert [node.properties["name"] for node in graph_nodes(editor, DATASET_FIELD)] == [
        "amount"
    ]
    assert not editor.store.list_edges(MAPS_TO_FIELD, scope=editor.scope)

"""SC 路由层 v2.1 契约冒烟测试（卡片 mgw1-routers）。

用 `semantic_config.main.create_app()` 真实装配 + httpx ASGITransport，
覆盖各实体 CRUD 与「旧契约消失」断言（查询参数删减、旧前缀下线、旧字段不再出现）。
测试风格沿用 test_semantic_config_commit_ordering.py。
"""

from __future__ import annotations

import httpx
import pytest

from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.engine import get_session_factory
from semantic_config.main import create_app
from semantic_config.principal import LOCAL_DEV_PRINCIPAL

from _sc_dbutil import seed_physical_table

SC = "/api/semantic-config"


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_AUTH_SECRET", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_API_ALLOWED_HOSTS", "sc")
    settings = get_settings()

    monkeypatch.setattr(
        settings, "db_url", "sqlite+aiosqlite:///" + str(tmp_path / "sc.db")
    )
    await init_db()
    return create_app()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sc") as c:
        yield c


# ---------- helpers：父对象经真实依赖链创建，不 mock ----------


async def _create_datasource(
    client: httpx.AsyncClient, name: str = "holo业务库"
) -> dict:
    resp = await client.post(
        f"{SC}/datasource",
        json={
            "datasource_name": name,
            "datasource_type": "postgresql",
            "config": {
                "host": "holo.example.com",
                "port": 8099,
                "database": "busi",
                "user": "u",
                "password": "p",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_domain(
    client: httpx.AsyncClient,
    name: str = "电商域",
    description: str = "电商核心业务域",
) -> dict:
    """业务域是工作空间级对象，建域不带数据源（SPEC 00 §5.1）。"""
    resp = await client.post(
        f"{SC}/biz-domain",
        json={"domain_name": name, "description": description},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_dataset(
    client: httpx.AsyncClient,
    datasource_id: str,
    domain_id: int,
    name: str = "dwd_order",
    source: str = "proj.dwd_order",
) -> dict:
    resp = await client.post(
        f"{SC}/dataset-meta",
        json={
            "datasource_id": datasource_id,
            "domain_id": domain_id,
            "dataset_name": name,
            "description": "订单明细宽表",
            "source": source,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _query_param_names(app, path: str, method: str = "get") -> set[str]:
    """端点在 openapi 中声明的全部查询参数名。"""
    op = app.openapi()["paths"][path][method]
    return {p["name"] for p in op.get("parameters", []) if p.get("in") == "query"}


async def _create_table_column(
    client: httpx.AsyncClient,
    datasource_id: str,
    table_name: str = "proj.dwd_order",
    column_name: str = "order_id",
    **extra,
) -> dict:
    resp = await client.post(
        f"{SC}/table-column",
        json={
            "datasource_id": datasource_id,
            "table_name": table_name,
            "column_name": column_name,
            "data_type": "bigint",
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_field(
    client: httpx.AsyncClient,
    dataset_id: int,
    field_name: str = "订单金额",
    **extra,
) -> dict:
    payload = {
        "dataset_id": dataset_id,
        "field_name": field_name,
        "description": "字段",
    }
    payload.update(extra)
    resp = await client.post(f"{SC}/dataset-column-meta", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_dimension(
    client: httpx.AsyncClient, domain_id: int, name: str = "地区", **extra
) -> dict:
    """维度字典挂业务域，不带数据源（SPEC 00 §4.1）。"""
    payload = {
        "domain_id": domain_id,
        "dimension_name": name,
        **extra,
    }
    resp = await client.post(f"{SC}/dimension", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_binding(
    client: httpx.AsyncClient,
    dataset_id: int,
    dimension_id: int,
    expr: str = "area_code",
    **extra,
) -> dict:
    resp = await client.post(
        f"{SC}/dataset-dimension",
        json={
            "dataset_id": dataset_id,
            "dimension_id": dimension_id,
            "expr": expr,
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_metric(
    client: httpx.AsyncClient, domain_id: int, name: str = "GMV", **extra
) -> dict:
    """指标字典挂业务域，不带数据源（SPEC 00 §4.2）。"""
    resp = await client.post(
        f"{SC}/metric-lib",
        json={"domain_id": domain_id, "metric_name": name, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_caliber(
    client: httpx.AsyncClient,
    metric_id: int,
    dataset_id: int,
    time_window: str = "1d",
    expr: str = "SUM(pay_amt)",
    **extra,
) -> dict:
    resp = await client.post(
        f"{SC}/metric-formula-lib",
        json={
            "metric_id": metric_id,
            "dataset_id": dataset_id,
            "expr": expr,
            "time_window": time_window,
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_verified_query(
    client: httpx.AsyncClient,
    question: str = "昨天的 GMV 是多少",
    verified_sql: str = "SELECT SUM(pay_amt) FROM proj.dwd_order WHERE dt = '${yyyymmdd-1}'",
    **extra,
) -> dict:
    resp = await client.post(
        f"{SC}/verified-query",
        json={"question": question, "verified_sql": verified_sql, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 注册面：openapi 契约 ----------


class TestV21ListSignatures:
    async def test_biz_domain_list_drops_datasource_id_param(self, app):
        """业务域已与数据源解耦，list 只按名称过滤（SPEC 00 §11.2）。"""
        params = _query_param_names(app, f"{SC}/biz-domain")
        assert "datasource_id" not in params
        assert "domain_name" in params

    async def test_dataset_meta_list_drops_dataset_type_param(self, app):
        params = _query_param_names(app, f"{SC}/dataset-meta")
        assert "dataset_type" not in params
        assert {"datasource_id", "domain_id", "dataset_name"} <= params
        assert "source_datasource" not in params

    async def test_dimension_list_keeps_datasource_id_param(self, app):
        params = _query_param_names(app, f"{SC}/dimension")
        assert "datasource_id" in params
        assert {"domain_id", "dimension_name"} <= params

    async def test_dataset_dimension_list_keeps_dataset_and_name_filters(self, app):
        params = _query_param_names(app, f"{SC}/dataset-dimension")
        assert {"dataset_id", "dimension_name"} <= params

    async def test_metric_lib_list_keeps_datasource_id_param(self, app):
        params = _query_param_names(app, f"{SC}/metric-lib")
        assert "datasource_id" in params
        assert {"domain_id", "metric_name"} <= params

    async def test_metric_caliber_list_filters(self, app):
        params = _query_param_names(app, f"{SC}/metric-formula-lib")
        assert {"metric_id", "dataset_id"} <= params


class TestV21RouteRegistration:
    async def test_dataset_field_legacy_prefix_registered(self, app):
        paths = app.openapi()["paths"]
        assert f"{SC}/dataset-field" in paths

    async def test_metric_caliber_legacy_prefix_registered(self, app):
        paths = app.openapi()["paths"]
        assert f"{SC}/metric-caliber" in paths

    async def test_verified_query_prefix_registered(self, app):
        paths = app.openapi()["paths"]
        assert f"{SC}/verified-query" in paths
        assert f"{SC}/verified-query/{{vq_id}}" in paths
        params = _query_param_names(app, f"{SC}/verified-query")
        assert {"domain_id", "question"} <= params


class TestBizDomainRouterV21:
    async def test_crud_flow(self, client):
        created = await _create_domain(client)
        domain_id = created["domain_id"]
        assert created["domain_name"] == "电商域"

        got = await client.get(f"{SC}/biz-domain/{domain_id}")
        assert got.status_code == 200, got.text
        assert got.json()["description"] == "电商核心业务域"

        renamed = await client.put(
            f"{SC}/biz-domain/{domain_id}", json={"description": "电商改名"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["description"] == "电商改名"

        deleted = await client.delete(f"{SC}/biz-domain/{domain_id}")
        assert deleted.status_code == 200, deleted.text
        gone = await client.get(f"{SC}/biz-domain/{domain_id}")
        assert gone.status_code == 404

    async def test_create_requires_description(self, client):
        resp = await client.post(f"{SC}/biz-domain", json={"domain_name": "空描述域"})
        assert resp.status_code == 400
        assert "description" in resp.json()["message"]

    async def test_legacy_payload_fields_are_ignored(self, client):
        """旧载荷里的 datasource_id / aliases 都已下线：接收不报错、响应不回显。"""
        ds = await _create_datasource(client)
        resp = await client.post(
            f"{SC}/biz-domain",
            json={
                "datasource_id": ds["datasource_id"],
                "domain_name": "旧载荷域",
                "description": "legacy",
                "aliases": "旧别名A$$$旧别名B",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "datasource_id" not in body
        assert "datasource_name" not in body
        assert "aliases" not in body
        # 派生列取代旧外键：尚无数据集时为空串
        assert body["datasource_scope"] == ""

    async def test_list_filters_by_domain_name(self, client):
        await _create_domain(client, name="甲域", description="A")
        await _create_domain(client, name="乙域", description="B")
        page = await client.get(f"{SC}/biz-domain", params={"domain_name": "甲域"})
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["total"] == 1
        assert body["records"][0]["domain_name"] == "甲域"

    async def test_list_not_filtered_by_datasource_id(self, client):
        """业务域是工作空间级对象：带上旧的 datasource_id 参数也不再收窄结果（SPEC 00 §15.2 ①）。"""
        ds = await _create_datasource(client)
        await _create_domain(client, name="甲域", description="A")
        await _create_domain(client, name="乙域", description="B")
        resp = await client.get(
            f"{SC}/biz-domain", params={"datasource_id": ds["datasource_id"]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 2


class TestDatasetMetaRouterV21:
    async def test_create_requires_source(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        resp = await client.post(
            f"{SC}/dataset-meta",
            json={
                "datasource_id": ds["datasource_id"],
                "domain_id": dom["domain_id"],
                "dataset_name": "无源表",
            },
        )
        assert resp.status_code == 400
        assert "source" in resp.json()["message"]

    async def test_crud_flow_with_source_column(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        created = await _create_dataset(
            client,
            ds["datasource_id"],
            dom["domain_id"],
            source="WITH t AS (SELECT 1) SELECT * FROM t",
        )
        assert created["source"].startswith("WITH")
        assert created["datasource_id"] == ds["datasource_id"]
        assert "source_datasource" not in created
        assert "dataset_type" not in created
        assert "sql_content" not in created
        assert "parents" not in created

        dataset_id = created["dataset_id"]
        got = await client.get(f"{SC}/dataset-meta/{dataset_id}")
        assert got.status_code == 200, got.text

        upd = await client.put(
            f"{SC}/dataset-meta/{dataset_id}", json={"description": "新描述"}
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["description"] == "新描述"

        deleted = await client.delete(f"{SC}/dataset-meta/{dataset_id}")
        assert deleted.status_code == 200, deleted.text

    async def test_list_keeps_ds_domain_name_filters(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        await _create_dataset(
            client, ds["datasource_id"], dom["domain_id"], name="orders"
        )
        await _create_dataset(
            client, ds["datasource_id"], dom["domain_id"], name="users"
        )
        page = await client.get(f"{SC}/dataset-meta", params={"dataset_name": "orders"})
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 1


# ---------- R2：table_column（原 dataset_column_meta 更名重构） ----------


class TestTableColumnRouterV21:
    @pytest.fixture
    async def column_datasource(self, client):
        datasource = await _create_datasource(client)
        principal = LOCAL_DEV_PRINCIPAL
        async with get_session_factory()() as db:
            for table_name in ("proj.dwd_order", "proj.dim_user"):
                await seed_physical_table(
                    db,
                    datasource["datasource_id"],
                    table_name,
                    principal.tenant_id,
                    principal.active_workspace_id,
                    created_by=principal.user_id,
                )
            await db.commit()
        return datasource

    async def test_create_column_comment_optional(self, client, column_datasource):
        """v2.1：key=(datasource_id, table_name, column_name)；column_comment 不必填。"""
        ds = column_datasource
        created = await _create_table_column(client, ds["datasource_id"])
        assert created["table_name"] == "proj.dwd_order"
        assert created["column_name"] == "order_id"
        assert created["column_comment"] is None
        assert "dataset_id" not in created
        assert "domain_id" not in created

    async def test_create_duplicate_key_rejected(self, client, column_datasource):
        ds = column_datasource
        await _create_table_column(client, ds["datasource_id"])
        dup = await client.post(
            f"{SC}/table-column",
            json={
                "datasource_id": ds["datasource_id"],
                "table_name": "proj.dwd_order",
                "column_name": "order_id",
            },
        )
        assert dup.status_code == 400
        assert "已存在" in dup.json()["message"]

    async def test_create_requires_table_name(self, client, column_datasource):
        ds = column_datasource
        resp = await client.post(
            f"{SC}/table-column",
            json={"datasource_id": ds["datasource_id"], "column_name": "c1"},
        )
        assert resp.status_code == 400
        assert "table_name" in resp.json()["message"]

    async def test_list_by_table(self, client, column_datasource):
        ds = column_datasource
        await _create_table_column(client, ds["datasource_id"], column_name="order_id")
        await _create_table_column(client, ds["datasource_id"], column_name="pay_amt")
        await _create_table_column(
            client,
            ds["datasource_id"],
            table_name="proj.dim_user",
            column_name="user_id",
        )
        rows = await client.get(
            f"{SC}/table-column/table",
            params={
                "datasource_id": ds["datasource_id"],
                "table_name": "proj.dwd_order",
            },
        )
        assert rows.status_code == 200, rows.text
        assert {r["column_name"] for r in rows.json()} == {"order_id", "pay_amt"}

    async def test_list_by_table_requires_both_params(self, client):
        resp = await client.get(
            f"{SC}/table-column/table", params={"table_name": "proj.t"}
        )
        assert resp.status_code in (400, 422)

    async def test_list_page_filters(self, client, column_datasource):
        ds = column_datasource
        await _create_table_column(client, ds["datasource_id"], column_name="c1")
        await _create_table_column(
            client, ds["datasource_id"], table_name="proj.dim_user", column_name="c2"
        )
        page = await client.get(
            f"{SC}/table-column",
            params={
                "datasource_id": ds["datasource_id"],
                "table_name": "proj.dwd_order",
            },
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 1

    async def test_update_and_delete(self, client, column_datasource):
        ds = column_datasource
        created = await _create_table_column(client, ds["datasource_id"])
        col_id = created["id"]

        upd = await client.put(
            f"{SC}/table-column/{col_id}",
            json={"column_comment": "订单号", "sample_values": "1$$$2"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["column_comment"] == "订单号"
        assert upd.json()["sample_values"] == "1$$$2"

        deleted = await client.delete(f"{SC}/table-column/{col_id}")
        assert deleted.status_code == 200, deleted.text
        gone = await client.get(f"{SC}/table-column/{col_id}")
        assert gone.status_code == 404

    async def test_legacy_prefix_returns_404(self, client):
        resp = await client.get(f"{SC}/table-column-meta")
        assert resp.status_code in (403, 404)


# ---------- R3：dataset_field（L3 新建） ----------


class TestDatasetFieldRouterV21:
    async def test_create_field(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        created = await _create_field(
            client,
            dsm["dataset_id"],
            description="订单金额（元）",
            data_type="decimal",
            is_primary="N",
            is_nullable="Y",
        )
        assert created["dataset_id"] == dsm["dataset_id"]
        assert created["dataset_name"] == dsm["dataset_name"]
        assert created["data_type"] == "decimal"
        assert created["is_primary"] == "N"
        assert created["is_nullable"] == "Y"

    async def test_create_requires_field_name(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        resp = await client.post(
            f"{SC}/dataset-column-meta", json={"dataset_id": dsm["dataset_id"]}
        )
        assert resp.status_code == 400
        assert "field_name" in resp.json()["message"]

    async def test_create_duplicate_field_rejected(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        await _create_field(client, dsm["dataset_id"], field_name="金额")
        dup = await client.post(
            f"{SC}/dataset-column-meta",
            json={
                "dataset_id": dsm["dataset_id"],
                "field_name": "金额",
                "description": "字段",
            },
        )
        assert dup.status_code == 400
        assert "已存在" in dup.json()["message"]

    async def test_create_rejects_missing_parent_dataset(self, client):
        resp = await client.post(
            f"{SC}/dataset-column-meta",
            json={"dataset_id": 99999, "field_name": "f", "description": "字段"},
        )
        assert resp.status_code == 400

    async def test_list_by_dataset(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        await _create_field(client, dsm["dataset_id"], field_name="f1")
        await _create_field(client, dsm["dataset_id"], field_name="f2")
        rows = await client.get(f"{SC}/dataset-column-meta/dataset/{dsm['dataset_id']}")
        assert rows.status_code == 200, rows.text
        assert {r["field_name"] for r in rows.json()} == {"f1", "f2"}

    async def test_list_page_filter_and_update_delete(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        created = await _create_field(client, dsm["dataset_id"], field_name="待改")
        field_id = created["id"]

        page = await client.get(
            f"{SC}/dataset-column-meta", params={"dataset_id": dsm["dataset_id"]}
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 1

        upd = await client.put(
            f"{SC}/dataset-column-meta/{field_id}", json={"field_name": "已改"}
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["field_name"] == "已改"

        deleted = await client.delete(f"{SC}/dataset-column-meta/{field_id}")
        assert deleted.status_code == 200, deleted.text
        gone = await client.get(f"{SC}/dataset-column-meta/{field_id}")
        assert gone.status_code == 404


# ---------- R4：dimension / dataset_dimension ----------


class TestDimensionRouterV21:
    async def test_create_dimension_with_v21_fields(self, client):
        dom = await _create_domain(client)
        created = await _create_dimension(
            client,
            dom["domain_id"],
            description="地域维度",
            dimension_type="categorical",
            child_dimensions="省份$$$城市",
            priority="HIGH",
        )
        assert created["dimension_type"] == "categorical"
        assert created["child_dimensions"] == "省份$$$城市"
        assert created["priority"] == "HIGH"
        assert "datasource_id" not in created
        assert "enums" not in created

    async def test_list_by_domain_filters(self, client):
        dom = await _create_domain(client)
        await _create_dimension(client, dom["domain_id"], name="地区")
        await _create_dimension(client, dom["domain_id"], name="渠道")
        page = await client.get(
            f"{SC}/dimension",
            params={"domain_id": dom["domain_id"], "dimension_name": "地区"},
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 1

    async def test_update_and_delete(self, client):
        dom = await _create_domain(client)
        created = await _create_dimension(client, dom["domain_id"])
        dim_id = created["dimension_id"]
        upd = await client.put(f"{SC}/dimension/{dim_id}", json={"priority": "HIGH"})
        assert upd.status_code == 200, upd.text
        assert upd.json()["priority"] == "HIGH"
        deleted = await client.delete(f"{SC}/dimension/{dim_id}")
        assert deleted.status_code == 200, deleted.text


class TestDatasetDimensionRouterV21:
    async def test_create_binding_with_expr(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        dim = await _create_dimension(client, dom["domain_id"])
        created = await _create_binding(
            client,
            dsm["dataset_id"],
            dim["dimension_id"],
            expr="area_code",
            value_example="华东$$$华南",
        )
        assert created["expr"] == "area_code"
        assert created["value_example"] == "华东$$$华南"
        assert "calculate_expr" not in created

    async def test_create_binding_requires_expr(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        dim = await _create_dimension(client, dom["domain_id"])
        resp = await client.post(
            f"{SC}/dataset-dimension",
            json={"dataset_id": dsm["dataset_id"], "dimension_id": dim["dimension_id"]},
        )
        assert resp.status_code == 400
        assert "expr" in resp.json()["message"]

    async def test_list_by_dataset_and_dimension_name(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        dim = await _create_dimension(client, dom["domain_id"], name="地区")
        await _create_binding(client, dsm["dataset_id"], dim["dimension_id"])
        page = await client.get(
            f"{SC}/dataset-dimension",
            params={"dataset_id": dsm["dataset_id"], "dimension_name": "地区"},
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 1

    async def test_list_by_dataset_and_update_delete(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        dim = await _create_dimension(client, dom["domain_id"])
        created = await _create_binding(client, dsm["dataset_id"], dim["dimension_id"])
        dd_id = created["id"]

        rows = await client.get(f"{SC}/dataset-dimension/dataset/{dsm['dataset_id']}")
        assert rows.status_code == 200, rows.text
        assert len(rows.json()) == 1

        upd = await client.put(
            f"{SC}/dataset-dimension/{dd_id}", json={"expr": "region_code"}
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["expr"] == "region_code"

        deleted = await client.delete(f"{SC}/dataset-dimension/{dd_id}")
        assert deleted.status_code == 200, deleted.text


# ---------- R5：metric_lib / metric_caliber ----------


class TestMetricLibRouterV21:
    async def test_create_metric_with_priority(self, client):
        dom = await _create_domain(client)
        created = await _create_metric(
            client, dom["domain_id"], priority="HIGH", synonyms="成交额$$$交易额"
        )
        assert created["priority"] == "HIGH"
        assert created["synonyms"] == "成交额$$$交易额"
        assert "is_polaris" not in created
        assert "unit" not in created
        assert "tags" not in created
        assert "datasource_id" not in created

    async def test_list_by_domain_filters(self, client):
        dom = await _create_domain(client)
        await _create_metric(client, dom["domain_id"], name="GMV")
        await _create_metric(client, dom["domain_id"], name="DAU")
        page = await client.get(
            f"{SC}/metric-lib",
            params={"domain_id": dom["domain_id"], "metric_name": "GMV"},
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 1

    async def test_update_and_delete(self, client):
        dom = await _create_domain(client)
        created = await _create_metric(client, dom["domain_id"])
        metric_id = created["metric_id"]
        upd = await client.put(
            f"{SC}/metric-lib/{metric_id}", json={"priority": "HIGH"}
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["priority"] == "HIGH"
        deleted = await client.delete(f"{SC}/metric-lib/{metric_id}")
        assert deleted.status_code == 200, deleted.text


class TestMetricCaliberRouterV21:
    async def _parents(self, client):
        ds = await _create_datasource(client)
        dom = await _create_domain(client)
        dsm = await _create_dataset(client, ds["datasource_id"], dom["domain_id"])
        met = await _create_metric(client, dom["domain_id"])
        return dsm, met

    async def test_create_caliber_with_v21_fields(self, client):
        dsm, met = await self._parents(client)
        created = await _create_caliber(
            client,
            met["metric_id"],
            dsm["dataset_id"],
            time_window="7d",
            additivity="additive",
            is_default=1,
        )
        assert created["time_window"] == "7d"
        assert created["additivity"] == "additive"
        assert created["is_default"] == 1
        assert created["expr"] == "SUM(pay_amt)"
        assert created["domain_id"] == met["domain_id"]
        assert "formula" not in created
        assert "date_range" not in created

    async def test_create_caliber_requires_expr_and_time_window(self, client):
        dsm, met = await self._parents(client)
        missing_expr = await client.post(
            f"{SC}/metric-formula-lib",
            json={
                "metric_id": met["metric_id"],
                "dataset_id": dsm["dataset_id"],
                "time_window": "1d",
            },
        )
        assert missing_expr.status_code == 400
        assert "expr" in missing_expr.json()["message"]

        missing_tw = await client.post(
            f"{SC}/metric-formula-lib",
            json={
                "metric_id": met["metric_id"],
                "dataset_id": dsm["dataset_id"],
                "expr": "SUM(1)",
            },
        )
        assert missing_tw.status_code == 400
        assert "time_window" in missing_tw.json()["message"]

    async def test_duplicate_key_rejected_but_different_time_window_ok(self, client):
        dsm, met = await self._parents(client)
        await _create_caliber(
            client, met["metric_id"], dsm["dataset_id"], time_window="1d"
        )
        dup = await client.post(
            f"{SC}/metric-formula-lib",
            json={
                "metric_id": met["metric_id"],
                "dataset_id": dsm["dataset_id"],
                "expr": "SUM(pay_amt)",
                "time_window": "1d",
            },
        )
        assert dup.status_code == 400
        assert "已存在" in dup.json()["message"]
        # 不同 time_window 是唯一键的另一段，允许
        other = await _create_caliber(
            client, met["metric_id"], dsm["dataset_id"], time_window="mtd"
        )
        assert other["time_window"] == "mtd"

    async def test_update_time_window_and_is_default(self, client):
        dsm, met = await self._parents(client)
        created = await _create_caliber(
            client, met["metric_id"], dsm["dataset_id"], time_window="1d"
        )
        cid = created["id"]
        upd = await client.put(
            f"{SC}/metric-formula-lib/{cid}",
            json={"time_window": "30d", "is_default": 1},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["time_window"] == "30d"
        assert upd.json()["is_default"] == 1

    async def test_list_page_by_metric_and_delete_by_metric(self, client):
        """指标维度列表走 GET "" 的 metric_id 过滤（D-R2：service 无 list_by_metric 封装）；
        批量删除走 DELETE /metric/{metric_id}（service.delete_by_metric）。"""
        dsm, met = await self._parents(client)
        await _create_caliber(
            client, met["metric_id"], dsm["dataset_id"], time_window="1d"
        )
        await _create_caliber(
            client, met["metric_id"], dsm["dataset_id"], time_window="7d"
        )

        page = await client.get(
            f"{SC}/metric-formula-lib", params={"metric_id": met["metric_id"]}
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 2

        deleted = await client.delete(
            f"{SC}/metric-formula-lib/metric/{met['metric_id']}"
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] == 2

    async def test_single_delete_and_get(self, client):
        dsm, met = await self._parents(client)
        created = await _create_caliber(client, met["metric_id"], dsm["dataset_id"])
        cid = created["id"]
        got = await client.get(f"{SC}/metric-formula-lib/{cid}")
        assert got.status_code == 200, got.text
        deleted = await client.delete(f"{SC}/metric-formula-lib/{cid}")
        assert deleted.status_code == 200, deleted.text
        gone = await client.get(f"{SC}/metric-formula-lib/{cid}")
        assert gone.status_code == 404

    async def test_legacy_prefix_returns_404(self, client):
        resp = await client.get(f"{SC}/metric-formula")
        assert resp.status_code in (403, 404)


# ---------- R6：verified_query（L8 新建） ----------


class TestVerifiedQueryRouterV21:
    async def test_create_verified_query(self, client):
        dom = await _create_domain(client)
        created = await _create_verified_query(client, domain_id=dom["domain_id"])
        assert created["question"] == "昨天的 GMV 是多少"
        assert created["verified_sql"].startswith("SELECT")
        assert created["domain_id"] == dom["domain_id"]
        assert created["domain_name"] == dom["domain_name"]

    async def test_create_without_domain_rejected(self, client):
        """domain_id NOT NULL：无域归属的问答不落库，400 而非 500。"""
        resp = await client.post(
            f"{SC}/verified-query",
            json={"question": "通用问题", "verified_sql": "SELECT 1"},
        )
        assert resp.status_code == 400
        assert "domain_id" in resp.json()["message"]

    async def test_create_requires_question_and_verified_sql(self, client):
        missing_sql = await client.post(
            f"{SC}/verified-query", json={"question": "只有问题"}
        )
        assert missing_sql.status_code == 400
        assert "verified_sql" in missing_sql.json()["message"]

        missing_q = await client.post(
            f"{SC}/verified-query", json={"verified_sql": "SELECT 1"}
        )
        assert missing_q.status_code == 400
        assert "question" in missing_q.json()["message"]

    async def test_create_rejects_missing_parent_domain(self, client):
        resp = await client.post(
            f"{SC}/verified-query",
            json={"domain_id": 99999, "question": "q", "verified_sql": "SELECT 1"},
        )
        assert resp.status_code == 400

    async def test_duplicate_question_in_domain_rejected(self, client):
        dom = await _create_domain(client)
        await _create_verified_query(
            client, domain_id=dom["domain_id"], question="重复问题"
        )
        dup = await client.post(
            f"{SC}/verified-query",
            json={
                "domain_id": dom["domain_id"],
                "question": "重复问题",
                "verified_sql": "SELECT 2",
            },
        )
        assert dup.status_code == 400
        assert "已存在" in dup.json()["message"]

    async def test_list_filters_by_domain_and_question(self, client):
        dom = await _create_domain(client)
        other = await _create_domain(client, name="供应链域")
        await _create_verified_query(
            client, domain_id=dom["domain_id"], question="甲问题"
        )
        await _create_verified_query(
            client, domain_id=dom["domain_id"], question="乙问题"
        )
        await _create_verified_query(
            client, domain_id=other["domain_id"], question="他域问题"
        )

        page = await client.get(
            f"{SC}/verified-query", params={"domain_id": dom["domain_id"]}
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 2

        filtered = await client.get(
            f"{SC}/verified-query", params={"question": "甲问题"}
        )
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["total"] == 1

    async def test_update_and_delete(self, client):
        dom = await _create_domain(client)
        created = await _create_verified_query(client, domain_id=dom["domain_id"])
        vq_id = created["id"]

        got = await client.get(f"{SC}/verified-query/{vq_id}")
        assert got.status_code == 200, got.text

        upd = await client.put(
            f"{SC}/verified-query/{vq_id}",
            json={"verified_sql": "SELECT 42"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["verified_sql"] == "SELECT 42"

        deleted = await client.delete(f"{SC}/verified-query/{vq_id}")
        assert deleted.status_code == 200, deleted.text
        gone = await client.get(f"{SC}/verified-query/{vq_id}")
        assert gone.status_code == 404

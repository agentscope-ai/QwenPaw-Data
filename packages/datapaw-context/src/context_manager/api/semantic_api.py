"""Semantic Layer REST API (read-only Phase 1)."""
from __future__ import annotations

import logging
from typing import Any, Union

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .semantic_models import (
    ColumnMeta,
    Dataset,
    DatasetSchema,
    Dimension,
    DimensionHierarchy,
    Domain,
    MetricDetail,
    MetricDimensionBinding,
    MetricNorthStarSummary,
    MetricSummary,
    SearchMetricsMiss,
)
from ..contracts.import_models import ImportStatus, SemanticImportRequest, SemanticImportResult
from .cm_resolve import resolve_read_datasource
from . import semantic_store as store

log = logging.getLogger("api.semantic_api")

router = APIRouter(prefix="/api/v1/semantic", tags=["semantic"])


def _driver(request: Request):
    return request.app.state.driver


def _require_domain(domain: str | None) -> str:
    if not (domain or "").strip():
        raise HTTPException(status_code=400, detail="domain is required")
    return domain.strip()


def _key_error(exc: KeyError) -> HTTPException:
    msg = exc.args[0] if exc.args else str(exc)
    return HTTPException(status_code=404, detail=str(msg))


def _read_ds_id(domain: str, datasource_id: str | None) -> str:
    """read 接口过滤用的 datasource_id：只认请求显式值，缺失即不过滤。"""
    return resolve_read_datasource(domain, req_datasource_id=datasource_id or "")


# ---------------------------------------------------------------------- #
# §2.1 Domains
# ---------------------------------------------------------------------- #

@router.get("/domains", response_model=list[Domain])
def list_domains(
    request: Request,
    datasource_id: str | None = Query(None, description="数据源 id（可选，不传则返回全部）"),
) -> list[Domain]:
    try:
        ds = (datasource_id or "").strip()
        return store.list_domain_records(_driver(request), datasource_id=ds)
    except Exception as exc:
        log.exception("list_domains: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------- #
# §2.2–2.5 Metrics (static paths before {metric_name})
# ---------------------------------------------------------------------- #

@router.get("/metrics", response_model=list[MetricSummary])
def list_metrics(
    request: Request,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[MetricSummary]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.list_metrics(_driver(request), domain, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("list_metrics: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/metrics/search")
def search_metrics(
    request: Request,
    query: str = Query(..., description="自然语言检索"),
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> Union[list[MetricSummary], SearchMetricsMiss]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    if not (query or "").strip():
        raise HTTPException(status_code=400, detail="query is required")
    try:
        result = store.search_metrics(_driver(request), domain, query.strip(), datasource_id=ds_id)
        if isinstance(result, dict):
            return SearchMetricsMiss(**result)
        return result
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("search_metrics: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/metrics/north-star", response_model=list[MetricNorthStarSummary])
def north_star_metrics(
    request: Request,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[MetricNorthStarSummary]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.list_north_star_metrics(_driver(request), domain, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("north_star_metrics: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/metrics/for-dimension", response_model=list[MetricSummary])
def metrics_for_dimension(
    request: Request,
    domain: str = Query(..., description="业务域名称"),
    dimension: str = Query(..., description="维度名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[MetricSummary]:
    """反向查询：给定维度，返回所有可通过该维度分析的指标。"""
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_metrics_for_dimension(_driver(request), domain, dimension, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("metrics_for_dimension: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/metrics/{metric_name}", response_model=MetricDetail)
def get_metric(
    request: Request,
    metric_name: str,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> MetricDetail:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_metric_detail(_driver(request), domain, metric_name, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("get_metric: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------- #
# §2.6–2.10 Dimensions
# ---------------------------------------------------------------------- #

@router.get("/dimensions", response_model=list[str])
def list_dimensions(
    request: Request,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[str]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.list_dimension_names(_driver(request), domain, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("list_dimensions: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/dimensions/for-metric", response_model=list[MetricDimensionBinding])
def dimensions_for_metric(
    request: Request,
    domain: str = Query(..., description="业务域名称"),
    metric: str = Query(..., description="指标名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[MetricDimensionBinding]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_dimensions_for_metric(_driver(request), domain, metric, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("dimensions_for_metric: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/dimensions/{dim_name}", response_model=Dimension)
def get_dimension(
    request: Request,
    dim_name: str,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> Dimension:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_dimension(_driver(request), domain, dim_name, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("get_dimension: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/dimensions/{dim_name}/hierarchy", response_model=DimensionHierarchy)
def dimension_hierarchy(
    request: Request,
    dim_name: str,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> DimensionHierarchy:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_dimension_hierarchy(_driver(request), domain, dim_name, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("dimension_hierarchy: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/dimensions/{dim_name}/values", response_model=list[str])
def dimension_values(
    request: Request,
    dim_name: str,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[str]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_dimension_values(_driver(request), domain, dim_name, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("dimension_values: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------- #
# §2.12–2.14 Datasets
# ---------------------------------------------------------------------- #

@router.get("/datasets", response_model=list[Dataset])
def list_datasets(
    request: Request,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[Dataset]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.list_datasets(_driver(request), domain, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("list_datasets: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/datasets/{name}/columns", response_model=list[ColumnMeta])
def dataset_columns(
    request: Request,
    name: str,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> list[ColumnMeta]:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_dataset_columns(_driver(request), domain, name, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("dataset_columns: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/datasets/{name}/schema", response_model=DatasetSchema)
def dataset_schema(
    request: Request,
    name: str,
    domain: str = Query(..., description="业务域名称"),
    datasource_id: str | None = Query(None, description="数据源 id（可选，默认按 domain 解析）"),
) -> DatasetSchema:
    domain = _require_domain(domain)
    ds_id = _read_ds_id(domain, datasource_id)
    try:
        return store.get_dataset_schema(_driver(request), domain, name, datasource_id=ds_id)
    except KeyError as exc:
        raise _key_error(exc) from exc
    except Exception as exc:
        log.exception("dataset_schema: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------- #
# Semantic Import 写入接口
# ---------------------------------------------------------------------- #

@router.post("/import", response_model=SemanticImportResult)
async def semantic_import(request: SemanticImportRequest, req: Request):
    """纯语义层导入：不涉及物理连接 / 密码，只写语义节点。

    配置管理前端专用。物理数据源须提前注册。
    传入 ``task_id`` + ``callback_url`` 时，完成后 POST 回调通知调用方。

    图构建逻辑抽到 :mod:`context_manager.graph.semantic_import_service`，
    本端点只负责解析 driver / 回调 / HTTPException 转换；进程内调用方
    可直接用 ``run_semantic_import_sync`` / ``run_semantic_import_async``。
    """
    import asyncio
    import uuid

    from ..graph.semantic_import_service import run_semantic_import_async
    from .auth import internal_callback_auth_headers
    from ..net_guard import (
        CallbackUrlError,
        ensure_safe_callback_url,
        post_safe_callback,
    )

    _log = logging.getLogger(__name__)

    async def _fire_semantic_callback(callback_url: str, tid: str, status: str, error_msg: str | None) -> None:
        """POST {task_id, status, error_msg} to the caller's callback URL."""
        payload = {"task_id": tid, "status": status, "error_msg": error_msg}
        try:
            result = await post_safe_callback(
                callback_url,
                payload=payload,
                headers=internal_callback_auth_headers(callback_url),
            )
            _log.info(
                "semantic callback %s → %s (task=%s)",
                callback_url,
                result.status_code,
                tid,
            )
        except Exception as exc:
            _log.warning(
                "semantic callback to %s failed: %s (task=%s)",
                callback_url,
                type(exc).__name__,
                tid,
            )

    driver = _driver(req)
    # 优先用调用方传入的 task_id，否则自动生成
    task_id = (request.task_id or f"sem_{uuid.uuid4().hex[:12]}").strip()
    callback_url = (request.callback_url or "").strip() or None
    if callback_url:
        # SSRF 防护：拒绝指向回环/内网/metadata 的回调目标（可用环境变量显式放行）
        try:
            callback_url = ensure_safe_callback_url(callback_url)
        except CallbackUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _callback(status: str, error_msg: str | None = None) -> None:
        if callback_url:
            asyncio.create_task(_fire_semantic_callback(callback_url, task_id, status, error_msg))

    # 调用进程内业务逻辑（不走 HTTP 回调，回调由本端点触发）
    result = await run_semantic_import_async(
        driver,
        request,
        task_id=task_id,
        governor=req.app.state.blocking_io,
    )

    # 按 result.status 触发 HTTP 回调通知调用方
    if result.status == ImportStatus.success:
        _callback("SUCCESS")
    else:
        err_msg = "; ".join(e.message for e in result.errors) if result.errors else None
        _callback("FAILED", err_msg)
    return result


@router.delete("/admin/domain/{name}")
def delete_domain(
    name: str,
    request: Request,
    datasource_id: str | None = Query(None, description="数据源 id（不传则删 legacy/无 ds scope 的同名 domain）"),
) -> dict[str, str]:
    """删除指定域及其所有子节点（Metric/Dimension/Dataset/Formula 等）。"""
    driver = _driver(request)
    ds = (datasource_id or "").strip()

    def _do_delete(tx):
        # 1. 删叶子：DimensionValue (HAS_VALUE)、Formula (HAS_FORMULA)、Caliber (HAS_CALIBER)
        tx.run(
            """
            MATCH (d:Domain {name: $name})
            WHERE ($ds = '' AND (d.datasource_id IS NULL OR d.datasource_id = '')
                   OR d.datasource_id = $ds)
            MATCH (d)-[:HAS_METRIC|HAS_DIMENSION]->(parent)
                  -[:HAS_VALUE|HAS_FORMULA|HAS_CALIBER]->(leaf)
            DETACH DELETE leaf
            """, name=name, ds=ds,
        )
        # 2. 删子节点：Metric / Dimension / Dataset（通过 HAS_* 挂在 Domain 下）
        tx.run(
            """
            MATCH (d:Domain {name: $name})
            WHERE ($ds = '' AND (d.datasource_id IS NULL OR d.datasource_id = '')
                   OR d.datasource_id = $ds)
            MATCH (d)-[:HAS_METRIC|HAS_DIMENSION|HAS_DATASET]->(child)
            DETACH DELETE child
            """, name=name, ds=ds,
        )
        # 3. 删 Domain 本身（含 DataSource → HAS_DOMAIN 边）
        result = tx.run(
            """
            MATCH (d:Domain {name: $name})
            WHERE ($ds = '' AND (d.datasource_id IS NULL OR d.datasource_id = '')
                   OR d.datasource_id = $ds)
            DETACH DELETE d RETURN count(d) AS n
            """,
            name=name, ds=ds,
        )
        return result.single()["n"]

    with driver.session() as s:
        count = s.execute_write(_do_delete)

    if count == 0:
        ds_hint = f" @{datasource_id}" if datasource_id else ""
        raise HTTPException(status_code=404, detail=f"domain '{name}'{ds_hint} not found")
    return {"deleted": name, "datasource_id": datasource_id or ""}

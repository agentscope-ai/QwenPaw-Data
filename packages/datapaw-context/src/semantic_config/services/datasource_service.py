from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.datasource import (
    ConnectionTestResponse,
    DatasourceCreate,
    DatasourceMetadataResponse,
    DatasourceResponse,
    DatasourceUpdate,
)
from semantic_config.models.datasource_config import validate_config
from semantic_config.pagination import Page, offset
from semantic_config.repositories import datasource_repo as repo
from semantic_config.services import connection_tester

log = logging.getLogger("semantic_config.datasource_service")

_ALLOWED_TYPES = {"mysql", "postgresql", "odps"}
_SENSITIVE_CONFIG_KEYS = frozenset({"password", "access_key_secret", "sts_token"})


def _safe_config(raw_config: str | None) -> dict[str, Any] | None:
    config = json.loads(raw_config) if raw_config else None
    if not isinstance(config, dict):
        return None
    return {
        key: value
        for key, value in config.items()
        if key not in _SENSITIVE_CONFIG_KEYS
    }


def _to_response(row: aiosqlite.Row) -> DatasourceResponse:
    raw_config = row["config"]
    return DatasourceResponse(
        datasource_id=row["datasource_id"],
        datasource_name=row["datasource_name"],
        datasource_type=row["datasource_type"],
        config=_safe_config(raw_config),
    )


def _to_metadata_response(row: aiosqlite.Row) -> DatasourceMetadataResponse:
    return DatasourceMetadataResponse(
        datasource_id=row["datasource_id"],
        datasource_name=row["datasource_name"],
        datasource_type=row["datasource_type"],
    )


async def create(db: aiosqlite.Connection, payload: DatasourceCreate) -> DatasourceResponse:
    if not payload.datasource_name:
        raise BadRequestError("datasource_name 不能为空")
    if not payload.datasource_type:
        raise BadRequestError("datasource_type 不能为空")

    datasource_type = payload.datasource_type
    if datasource_type.lower() not in _ALLOWED_TYPES:
        raise BadRequestError(f"数据源类型不支持: {datasource_type}")

    normalized = validate_config(datasource_type, payload.config)

    # datasource_id 由后端自动生成：type-uuid（全局唯一，前端不传）
    datasource_id = f"{datasource_type.lower()}-{uuid.uuid4().hex[:8]}"

    config_json = json.dumps(normalized, ensure_ascii=False)
    new_id = await repo.insert(
        db, datasource_id, payload.datasource_name, datasource_type, config_json
    )
    row = await repo.find_by_id(db, new_id)
    return _to_response(row)


async def update(
    db: aiosqlite.Connection, datasource_id: str, payload: DatasourceUpdate
) -> DatasourceResponse:
    row = await repo.find_by_datasource_id(db, datasource_id)
    if row is None:
        raise NotFoundError(f"数据源不存在: datasource_id={datasource_id}")

    if payload.datasource_type is not None and payload.datasource_type.lower() not in _ALLOWED_TYPES:
        raise BadRequestError(f"数据源类型不支持: {payload.datasource_type}")

    # 有效类型：以传入 type 优先，否则沿用原类型（config 校验按有效类型走）
    effective_type = payload.datasource_type or row["datasource_type"]

    update_config = payload.config is not None
    config_json: str | None = None
    if update_config:
        candidate = dict(payload.config or {})
        if effective_type == row["datasource_type"]:
            existing = json.loads(row["config"]) if row["config"] else {}
            for key in _SENSITIVE_CONFIG_KEYS:
                if candidate.get(key) in (None, "") and existing.get(key) not in (
                    None,
                    "",
                ):
                    candidate[key] = existing[key]
        normalized = validate_config(effective_type, candidate)
        config_json = json.dumps(normalized, ensure_ascii=False)
    elif payload.datasource_type is not None:
        # 改了类型但没给新 config：确保原有 config 仍与新类型兼容，否则要求补 config
        existing = json.loads(row["config"]) if row["config"] else None
        validate_config(effective_type, existing)

    await repo.update(
        db,
        row["id"],
        name=payload.datasource_name,
        dtype=payload.datasource_type,
        config_json=config_json,
        update_config=update_config,
    )
    return _to_response(await repo.find_by_id(db, row["id"]))


async def get_one(db: aiosqlite.Connection, datasource_id: str) -> DatasourceResponse:
    row = await repo.find_by_datasource_id(db, datasource_id)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    page: int,
    size: int,
) -> Page[DatasourceResponse]:
    total = await repo.count(db, datasource_id, name, dtype)
    rows = await repo.find_page(db, datasource_id, name, dtype, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def list_metadata_page(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    page: int,
    size: int,
) -> Page[DatasourceMetadataResponse]:
    total = await repo.count(db, datasource_id, name, dtype)
    rows = await repo.find_page(db, datasource_id, name, dtype, size, offset(page, size))
    return Page(
        records=[_to_metadata_response(row) for row in rows],
        total=total,
        page=page,
        size=size,
    )


async def delete(db: aiosqlite.Connection, datasource_id: str, *, driver: Any = None) -> dict:
    row = await repo.find_by_datasource_id(db, datasource_id)
    if row is None:
        raise NotFoundError(f"数据源不存在: datasource_id={datasource_id}")
    domains = await repo.count_domains(db, datasource_id)
    if domains > 0:
        raise BadRequestError(f"数据源被引用，无法删除：业务域 {domains} 个")
    await repo.soft_delete(db, row["id"])

    # 同步清空图数据库中该数据源对应的语义层与物理层节点。图清理失败不回滚 SQLite
    # 软删除——图是 SQLite 元数据的下游投影，偶发不一致可后续重导修复。
    if driver is not None and datasource_id:
        try:
            from context_manager.graph.semantic_import_service import (
                drop_semantic_for_datasource,
                drop_physical_for_datasource,
            )

            dropped_sem = drop_semantic_for_datasource(driver, datasource_id)
            log.info(
                "delete datasource %s: dropped %d semantic domain roots from graph",
                datasource_id, dropped_sem,
            )
            dropped_phy = drop_physical_for_datasource(driver, datasource_id)
            log.info(
                "delete datasource %s: dropped %d physical nodes from graph",
                datasource_id, dropped_phy,
            )
        except Exception as exc:
            log.warning(
                "delete datasource %s: graph cleanup failed (non-fatal): %s",
                datasource_id, exc,
            )
    return {"message": "deleted", "datasource_id": datasource_id}


async def test_connection(datasource_type: str, config: dict) -> ConnectionTestResponse:
    """存盘前测试连接：直接用传入的类型与 config。"""
    return await connection_tester.test_connection(datasource_type, config)


async def test_connection_by_id(db: aiosqlite.Connection, datasource_id: str) -> ConnectionTestResponse:
    """测试已保存数据源的连接。"""
    row = await repo.find_by_datasource_id(db, datasource_id)
    if row is None:
        raise NotFoundError(f"数据源不存在: datasource_id={datasource_id}")
    config = json.loads(row["config"]) if row["config"] else None
    if config is None:
        raise BadRequestError(f"数据源 datasource_id={datasource_id} 未配置连接信息（config 为空）")
    return await connection_tester.test_connection(row["datasource_type"], config)

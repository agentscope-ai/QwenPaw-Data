from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.datasource import (
    ConnectionTestResponse,
    DatasourceCreate,
    DatasourceResponse,
    DatasourceUpdate,
)
from semantic_config.models.datasource_config import resolve_type, validate_config
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo as repo
from semantic_config.services import connection_tester

log = logging.getLogger("semantic_config.datasource_service")

#: Write-only config keys: never echoed in API responses, kept on blank same-type edits.
_SENSITIVE_CONFIG_KEYS = frozenset(
    {"password", "access_key_secret", "sts_token", "credentials_json"}
)


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
    keys = row.keys()
    config = _safe_config(raw_config)
    return DatasourceResponse(
        id=row["id"],
        datasource_id=row["datasource_id"],
        datasource_name=row["datasource_name"],
        datasource_type=row["datasource_type"],
        config=config,
        description=row["description"] if "description" in keys else None,
    )


async def create(
    db: aiosqlite.Connection, payload: DatasourceCreate, principal: Principal
) -> DatasourceResponse:
    """新建数据源（幂等）：同 tenant/workspace 下同名 + 同类型已存在则直接返回该行，不新建。

    幂等键 = (datasource_name, 归一后的 datasource_type)；命中即返回已存在数据源，
    连接配置不做校验/更新（“已存在即复用”，改配置请走 update）。
    """
    require_admin(principal)
    if not payload.datasource_name:
        raise BadRequestError("datasource_name 不能为空")
    if not payload.datasource_type:
        raise BadRequestError("datasource_type 不能为空")

    # The type vocabulary is centralized in resolve_type (incl. legacy alias folding)
    datasource_type = resolve_type(payload.datasource_type).value

    # 幂等短路：同名同类型已存在则直接返回（config 不校验、不覆盖）。
    existing = await repo.find_by_name_and_type(
        db,
        payload.datasource_name,
        datasource_type,
        principal.tenant_id,
        principal.active_workspace_id,
    )
    if existing is not None:
        return _to_response(existing)

    normalized = validate_config(datasource_type, payload.config)

    # datasource_id is generated server-side: type-uuid (globally unique, never client-sent)
    datasource_id = f"{datasource_type}-{uuid.uuid4().hex[:8]}"

    config_json = json.dumps(normalized, ensure_ascii=False)
    new_id = await repo.insert(
        db,
        datasource_id,
        payload.datasource_name,
        datasource_type,
        config_json,
        description=payload.description,
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        created_by=principal.user_id,
    )
    row = await repo.find_by_id(
        db, new_id, principal.tenant_id, principal.active_workspace_id
    )
    return _to_response(row)


async def update(
    db: aiosqlite.Connection,
    datasource_id: str,
    payload: DatasourceUpdate,
    principal: Principal,
) -> DatasourceResponse:
    require_admin(principal)
    row = await repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundError(f"数据源不存在: datasource_id={datasource_id}")

    # Effective type: an incoming type wins (normalized), else the stored one; config validates against it
    effective_type = (
        resolve_type(payload.datasource_type).value
        if payload.datasource_type is not None
        else resolve_type(row["datasource_type"]).value
    )

    update_config = payload.config is not None
    config_json: str | None = None
    if update_config:
        candidate = dict(payload.config or {})
        try:
            same_type = resolve_type(row["datasource_type"]).value == effective_type
        except BadRequestError:  # unknown stored type: no secrets to carry over
            same_type = False
        if same_type:
            # Responses omit secrets, so a blank secret keeps the stored value.
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
        # Type changed without a new config: the existing config must stay compatible, else 400
        existing = json.loads(row["config"]) if row["config"] else None
        validate_config(effective_type, existing)

    await repo.update(
        db,
        row["id"],
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        name=payload.datasource_name,
        dtype=effective_type if payload.datasource_type is not None else None,
        config_json=config_json,
        update_config=update_config,
        description=payload.description,
        update_description=payload.description is not None,
        updated_by=principal.user_id,
    )
    return _to_response(
        await repo.find_by_id(
            db, row["id"], principal.tenant_id, principal.active_workspace_id
        )
    )


async def get_one(
    db: aiosqlite.Connection, datasource_id: str, principal: Principal
) -> DatasourceResponse:
    row = await repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
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
    principal: Principal,
) -> Page[DatasourceResponse]:
    total = await repo.count(
        db,
        datasource_id,
        name,
        dtype,
        principal.tenant_id,
        principal.active_workspace_id,
    )
    rows = await repo.find_page(
        db,
        datasource_id,
        name,
        dtype,
        size,
        offset(page, size),
        principal.tenant_id,
        principal.active_workspace_id,
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def delete(
    db: aiosqlite.Connection,
    datasource_id: str,
    principal: Principal,
    *,
    driver: Any = None,
) -> dict:
    require_admin(principal)
    row = await repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundError(f"数据源不存在: datasource_id={datasource_id}")
    # 业务域已与数据源解耦，删除只看数据集这一条引用边（SPEC 00 §6）。
    datasets = await repo.count_datasets(
        db, row["datasource_id"], principal.tenant_id, principal.active_workspace_id
    )
    if datasets:
        raise BadRequestError(f"数据源被引用，无法删除：数据集 {datasets} 个")
    await repo.soft_delete(
        db,
        row["id"],
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        updated_by=principal.user_id,
    )

    # mg-088（§4.3 适配点）：级联软删 explore_scope/explore_state（与
    # datasource_repo 删除链同风格；失败不回滚 ds 软删，漂移可重探）。
    try:
        from semantic_config.repositories import explore_repo

        cascaded = await explore_repo.soft_delete_for_datasource(
            db,
            principal.tenant_id,
            principal.active_workspace_id,
            datasource_id,
            updated_by=principal.user_id,
        )
        if cascaded:
            log.info(
                "delete datasource %s: cascaded %d explore rows",
                datasource_id,
                cascaded,
            )
    except Exception as exc:  # noqa: BLE001 — 级联失败不阻断 ds 删除
        log.warning(
            "delete datasource %s: explore cascade failed (non-fatal): %s",
            datasource_id,
            exc,
        )

    # TODO: use a datasource-scoped deletion service once CM exposes one, including
    # ownership handling for semantic nodes shared by several datasources.
    log.warning(
        "delete datasource %s: CM graph cleanup is not yet connected", datasource_id
    )
    return {"message": "deleted", "datasource_id": datasource_id}


async def test_connection(datasource_type: str, config: dict) -> ConnectionTestResponse:
    """Test a connection before persisting, using the given type and config."""
    return await connection_tester.test_connection(datasource_type, config)


async def test_connection_by_id(
    db: aiosqlite.Connection, datasource_id: str, principal: Principal
) -> ConnectionTestResponse:
    """Test the connection of an already-persisted datasource."""
    row = await repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundError(f"数据源不存在: datasource_id={datasource_id}")
    config = json.loads(row["config"]) if row["config"] else None
    if config is None:
        raise BadRequestError(
            f"数据源 datasource_id={datasource_id} 未配置连接信息（config 为空）"
        )
    return await connection_tester.test_connection(row["datasource_type"], config)

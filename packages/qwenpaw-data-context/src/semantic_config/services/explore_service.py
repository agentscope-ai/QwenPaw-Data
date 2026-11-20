"""mg-088 自动探查端点服务面（§2.2）：scope 视图 / 修改校验 / 手动增量触发。

命名锁 explore_scope（spec §1.4）；v1 不暴露 MCP/host（裁决 Q8）。
"""

from __future__ import annotations

import logging

from qwenpaw_data_auth import Principal
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.errors import BadRequestError, NotFoundError
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo, explore_repo
from semantic_config.services.autoexplore import trigger

log = logging.getLogger("semantic_config.explore_service")

_SAMPLE_MODES = {"on", "off", "auto"}


async def _require_datasource(
    db: AsyncSession, datasource_id: str, principal: Principal
):
    ds = await datasource_repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        raise NotFoundError(f"数据源不存在: datasource_id={datasource_id}")
    return ds


async def get_scope_view(
    db: AsyncSession, datasource_id: str, principal: Principal
) -> dict:
    """GET：返回 scope 行（首次读自动生成默认行，§1.2）。"""
    await _require_datasource(db, datasource_id, principal)
    await explore_repo.ensure_scope(
        db, principal.tenant_id, principal.active_workspace_id, datasource_id
    )
    row = await explore_repo.get_scope(
        db, principal.tenant_id, principal.active_workspace_id, datasource_id
    )
    return dict(row)


async def update_scope_view(
    db: AsyncSession,
    datasource_id: str,
    principal: Principal,
    *,
    schemas: str | None = None,
    include_patterns: str | None = None,
    exclude_patterns: str | None = None,
    sample_mode: str | None = None,
    sample_limit: int | None = None,
    table_limit: int | None = None,
    enabled: int | None = None,
) -> dict:
    """PUT：修改 scope（origin→manual；修改不自动重探，重探走手动端点 §2.2）。"""
    require_admin(principal)
    await _require_datasource(db, datasource_id, principal)
    # 首写同首读：无行先生成默认行再改（§1.2）
    await explore_repo.ensure_scope(
        db, principal.tenant_id, principal.active_workspace_id, datasource_id
    )
    if (
        sample_mode is not None
        and str(sample_mode).strip().lower() not in _SAMPLE_MODES
    ):
        raise BadRequestError(f"sample_mode 仅支持 on/off/auto，收到: {sample_mode}")
    for name, v in (("sample_limit", sample_limit), ("table_limit", table_limit)):
        if v is not None and int(v) <= 0:
            raise BadRequestError(f"{name} 必须为正整数")
    row = await explore_repo.update_scope(
        db,
        principal.tenant_id,
        principal.active_workspace_id,
        datasource_id,
        schemas=schemas,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        sample_mode=sample_mode,
        sample_limit=sample_limit,
        table_limit=table_limit,
        enabled=enabled,
        updated_by=principal.user_id,
    )
    return dict(row)


async def manual_trigger(
    db: AsyncSession, datasource_id: str, principal: Principal, *, graph_access
) -> dict:
    """POST：手动增量触发（force_refresh=True 防 TTL 缓存读旧）。

    返回 ``{status: accepted, run_id}`` / ``{status: already_running}``（单飞）
    / ``{status: disabled}``（scope enabled=0）。
    """
    require_admin(principal)
    await _require_datasource(db, datasource_id, principal)
    tenant_id = principal.tenant_id
    workspace_id = principal.active_workspace_id
    scope = await explore_repo.ensure_scope(db, tenant_id, workspace_id, datasource_id)
    if not scope["enabled"]:
        return {"status": "disabled"}
    await db.commit()
    return await trigger.schedule_explore(
        datasource_id=datasource_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        graph_access=graph_access,
        force_refresh=True,
    )

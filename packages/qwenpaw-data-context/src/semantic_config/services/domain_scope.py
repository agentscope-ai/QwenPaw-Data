"""``domain.datasource_scope`` 的派生与消费（SPEC 00 §4、§15.3）。

业务域不再直连数据源：一个域涉及哪些数据源，完全由该域下的数据集派生。
``datasource_scope`` 是这份派生结果的缓存列（``$$$`` 拼接的数据源编码集合），
由数据集写操作在同一事务内重算，业务域自身的 create/update 不接收该字段。

两类消费方都在本模块：

- 写侧 :func:`recompute_domain_scope` / :func:`recompute_domain_scopes`
  ——数据集 CRUD 与 Excel 导入调用。
- 读侧 :func:`scope_contains` / :func:`domain_ids_for_datasource`
  ——Excel 单源导出、编织装配、建模工作台的单数据源视图调用。
"""

from __future__ import annotations

from sqlalchemy import func, literal, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import dataset, domain

#: 多值列的统一分隔符（datasource_scope、child_dimensions、value_example 等共用）。
SCOPE_SEP = "$$$"


def scope_contains(datasource_id: str):
    """ "该域的 datasource_scope 含 DS" 的 SQL 条件（按分隔符精确比对）。

    scope 是 ``a$$$b$$$c`` 形式的拼接串，裸 ``LIKE '%ds%'`` 会让 ``mysql-3f2a``
    误命中 ``mysql-3f2a1b9c``。故两侧补分隔符后再整段匹配，等价于按 ``$$$``
    切分后的精确相等。``autoescape`` 让编码里的 ``%``/``_`` 不被当通配符。
    """
    padded = (
        literal(SCOPE_SEP)
        + func.coalesce(domain.c.datasource_scope, "")
        + literal(SCOPE_SEP)
    )
    return padded.contains(f"{SCOPE_SEP}{datasource_id}{SCOPE_SEP}", autoescape=True)


def domain_ids_for_datasource(datasource_id: str, tenant_id: str, workspace_id: str):
    """入围域集合 D 的子查询：至少有一个数据集落在 DS 上的业务域 id。

    供维度字典 / 指标字典的派生 ``datasource_id`` 过滤（SPEC 00 §15.3）与
    Excel 单源导出的业务域 sheet（§12.3 ①）复用。
    """
    return (
        select(domain.c.id)
        .where(
            domain.c.tenant_id == tenant_id,
            domain.c.workspace_id == workspace_id,
            domain.c.is_deleted == 0,
            scope_contains(datasource_id),
        )
        .scalar_subquery()
    )


async def _derive_scope(
    db: AsyncSession, domain_id: int, tenant_id: str, workspace_id: str
) -> str:
    """按该域下未软删数据集的 datasource_id 去重拼接（字典序，便于 diff）。"""
    res = await db.execute(
        select(dataset.c.datasource_id)
        .where(
            dataset.c.domain_id == domain_id,
            dataset.c.tenant_id == tenant_id,
            dataset.c.workspace_id == workspace_id,
            dataset.c.is_deleted == 0,
        )
        .distinct()
    )
    codes = sorted({c for c in res.scalars().all() if c})
    return SCOPE_SEP.join(codes)


async def recompute_domain_scope(
    db: AsyncSession,
    domain_id: int | None,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str | None = None,
) -> str | None:
    """重算并回写单个业务域的 ``datasource_scope``，返回新值。

    与触发它的数据集写操作共处同一事务（``get_db`` 每请求一个事务），保证
    scope 与 dataset 强一致。``domain_id`` 为空（数据集未绑域）时空转。
    空集写入空串而非 NULL，读侧统一按空串处理。
    """
    if domain_id is None:
        return None
    scope = await _derive_scope(db, domain_id, tenant_id, workspace_id)
    values: dict = {"datasource_scope": scope}
    # 只有 scope 变化时才动审计字段，避免重算把 updated_by 刷成无关操作人。
    if updated_by is not None:
        values["updated_by"] = updated_by
        values["updated_at"] = now_iso()
    await db.execute(
        sa_update(domain)
        .where(
            domain.c.id == domain_id,
            domain.c.tenant_id == tenant_id,
            domain.c.workspace_id == workspace_id,
            domain.c.is_deleted == 0,
        )
        .values(**values)
    )
    return scope


async def recompute_domain_scopes(
    db: AsyncSession,
    domain_ids,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str | None = None,
) -> None:
    """批量重算（Excel 导入结束时对涉及的所有域统一调用，避免逐行重复）。"""
    for domain_id in sorted({d for d in domain_ids if d is not None}):
        await recompute_domain_scope(
            db,
            domain_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            updated_by=updated_by,
        )

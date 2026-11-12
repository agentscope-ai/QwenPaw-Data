"""身份适配器：薄封装 qwenpaw-data-auth，供 semantic_config 各层取当前请求身份。

- 合并部署 + 开启鉴权：返回 CM auth_middleware 注入到 principal_ctx 的真实身份。
- 本地/开源非鉴权（未设 QWENPAW_DATA_AUTH_SECRET）或独立部署（无 context_manager）：
  使用显式本地开发身份（default/default, tenant_admin）。

见 SPEC：dev_docs/spec/02-语义配置权限(多租户)-SPEC.md §5.3 / §5.5。
"""

from __future__ import annotations

from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError


LOCAL_DEV_PRINCIPAL = Principal(
    user_id="default",
    tenant_id="default",
    workspace_ids=["default"],
    active_workspace_id="default",
    role="tenant_admin",
)


async def current_principal() -> Principal:
    """取当前请求身份；仅无鉴权/独立开发模式使用本地身份。"""
    try:
        from context_manager.api.auth_middleware import auth_enabled, get_principal
    except ImportError:
        # Standalone semantic_config process has no context_manager package.
        return LOCAL_DEV_PRINCIPAL
    if not auth_enabled():
        return LOCAL_DEV_PRINCIPAL
    # Authentication is enabled: missing/invalid request identity must never
    # downgrade to the default tenant_admin.
    principal = get_principal()
    if (
        principal.role == "anonymous"
        or not principal.user_id
        or not principal.tenant_id
        or not principal.active_workspace_id
    ):
        raise PermissionError(
            "Authenticated semantic access requires a tenant workspace"
        )
    return principal


def require_admin(p: Principal) -> None:
    """写操作防御性门禁；路由层（auth 侧）已强制 datasource:write，此处为独立部署兜底。"""
    if not p.is_admin:
        raise BadRequestError("无权限：仅管理员可修改语义配置")

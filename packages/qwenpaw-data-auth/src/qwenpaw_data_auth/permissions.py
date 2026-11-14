"""
Static permission matrix — a pure function that maps (role, resource) → bool.

Both the Python backend and the TypeScript frontend maintain identical copies
of this matrix.  Any change here MUST be mirrored in
``packages/qwenpaw-data-context/frontend/src/auth/permissions.ts``.
"""

from __future__ import annotations

PERMISSION_MATRIX: dict[str, dict[str, bool]] = {
    "tenant_admin": {
        "mg:read": True,
        "mg:write": True,
        "mg_copy:self": True,
        "mg_userspace:sync": True,
        "mg_userspace:append": True,
        "tg_kg:self": True,
        "tg_kg:others_read": True,
        "workspace:manage": True,
        "user:manage": True,
        "datasource:read": True,
        "datasource:write": True,
        "skill:publish": True,
        "skill:install": True,
        "snapshot:create": True,
        "snapshot:rollback_mg": True,
        "snapshot:rollback_self": True,
        "raw_cypher": True,
        "semantic_weaving": True,
        "excel_import": True,
        "model_config": True,
        "ops:logs": True,
    },
    "ws_admin": {
        "mg:read": True,
        "mg:write": True,
        "mg_copy:self": True,
        "mg_userspace:sync": True,
        "mg_userspace:append": True,
        "tg_kg:self": True,
        "tg_kg:others_read": True,
        "workspace:manage": True,
        "user:manage": True,
        "datasource:read": True,
        "datasource:write": True,
        "skill:publish": True,
        "skill:install": True,
        "snapshot:create": True,
        "snapshot:rollback_mg": True,
        "snapshot:rollback_self": True,
        "raw_cypher": True,
        "semantic_weaving": True,
        "excel_import": True,
        "model_config": True,
        "ops:logs": False,
    },
    "member": {
        "mg:read": True,
        "mg:write": False,
        "mg_copy:self": True,
        "mg_userspace:sync": True,
        "mg_userspace:append": True,
        "tg_kg:self": True,
        "tg_kg:others_read": False,
        "workspace:manage": False,
        "user:manage": False,
        "datasource:read": True,
        "datasource:write": False,
        "skill:publish": False,
        "skill:install": True,
        "snapshot:create": True,
        "snapshot:rollback_mg": False,
        "snapshot:rollback_self": True,
        "raw_cypher": False,
        "semantic_weaving": False,
        "excel_import": False,
        "model_config": True,
        "ops:logs": False,
    },
}


def check_permission(role: str, resource: str) -> bool:
    return PERMISSION_MATRIX.get(role, {}).get(resource, False)

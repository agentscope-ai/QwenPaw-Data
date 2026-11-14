"""Read shared Auth user records and workspace role bindings."""

from sqlalchemy import and_, or_, select

from ..db import get_database
from ..schema.relational import role_bindings, tenants, users, workspaces


class OwnerRepository:
    """Read current user facts through the shared database connection pool."""

    def get_username(self, tenant_id: str, user_id: str) -> str | None:
        with get_database().engine.connect() as connection:
            return connection.execute(
                select(users.c.username).where(
                    users.c.tenant_id == tenant_id,
                    users.c.id == user_id,
                )
            ).scalar_one_or_none()

    def list_workspace_roles(
        self,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
    ) -> list[dict]:
        """Read bindings for an active user, tenant and workspace with instance state."""
        statement = (
            select(
                role_bindings.c.scope_type,
                role_bindings.c.role,
                workspaces.c.instance_status,
            )
            .select_from(
                role_bindings.join(users, users.c.id == role_bindings.c.user_id)
                .join(tenants, tenants.c.id == users.c.tenant_id)
                .join(workspaces, workspaces.c.tenant_id == tenants.c.id)
            )
            .where(
                users.c.id == user_id,
                users.c.tenant_id == tenant_id,
                users.c.status == "active",
                tenants.c.status == "active",
                workspaces.c.id == workspace_id,
                workspaces.c.status == "active",
                or_(
                    and_(
                        role_bindings.c.scope_type == "tenant",
                        role_bindings.c.scope_id == tenant_id,
                    ),
                    and_(
                        role_bindings.c.scope_type == "workspace",
                        role_bindings.c.scope_id == workspace_id,
                    ),
                ),
            )
        )
        with get_database().engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

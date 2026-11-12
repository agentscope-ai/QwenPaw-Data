"""Resolve authenticated Workspaces through one runtime pool."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import text

from ..db import get_database
from ..settings import Settings, get_settings
from ..store.runtime import ExecutionTarget
from ..utils.validation import require_trimmed_text
from .connection import (
    GraphConnectionConfig,
    HoloConnectionConfig,
    Neo4jConnectionConfig,
)
from .errors import ContextRuntimeUnavailable
from .workspace import (
    WorkspaceRuntime,
    WorkspaceRuntimePool,
)

#: Endpoint scheme marking a Workspace routed to the shared Holo AGE instance.
_HOLO_ENDPOINT_SCHEME = "holo-age://"

#: Control-plane marker naming the Holo AGE backend as the graph instance.
_HOLO_INSTANCE_VERSION = "holo_age"


class WorkspaceResolver:
    """Resolve an authorized Workspace into its pooled runtime."""

    def __init__(
        self,
        pool: WorkspaceRuntimePool,
        *,
        connection: GraphConnectionConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._pool = pool
        self._connection = connection
        self._settings = settings if settings is not None else get_settings()

    @property
    def fixed_target(self) -> ExecutionTarget | None:
        """Expose an explicitly bound target for fixed-Workspace authentication."""
        return self._connection.target if self._connection is not None else None

    def resolve(self, tenant_id: str, workspace_id: str) -> WorkspaceRuntime:
        """Validate the target, then obtain its existing or newly built runtime."""
        if self._connection is not None:
            self.require_workspace(tenant_id, workspace_id)
            connection = self._connection
        else:
            tenant_id = require_trimmed_text(tenant_id, "tenant_id")
            workspace_id = require_trimmed_text(workspace_id, "workspace_id")
            connection = self._load_connection(tenant_id, workspace_id)
        return self._pool.resolve(connection)

    def require_workspace(self, tenant_id: str, workspace_id: str) -> None:
        """Validate Workspace identity and status without opening its graph."""
        tenant_id = require_trimmed_text(tenant_id, "tenant_id")
        workspace_id = require_trimmed_text(workspace_id, "workspace_id")
        if self._connection is not None:
            if tenant_id != self._connection.tenant_id:
                raise PermissionError(
                    "authenticated tenant does not match this Context runtime"
                )
            if workspace_id != self._connection.workspace_id:
                raise PermissionError(
                    "active workspace does not match this Context runtime"
                )
        else:
            self._load_workspace(tenant_id, workspace_id)

    def close(self) -> None:
        """Release the pooled synchronous Workspace resources."""
        self._pool.close()

    async def stop_services(self) -> None:
        """Stop Workspace services while their shared resources remain available."""
        await self._pool.stop_services()

    async def aclose(self) -> None:
        """Release the pooled Workspace resources and model clients."""
        await self._pool.aclose()

    def health(self) -> bool:
        """Check the fixed runtime or the shared Workspace registry."""
        if self._connection is not None:
            self.resolve(self._connection.tenant_id, self._connection.workspace_id)
            return True
        try:
            with get_database().engine.connect() as connection:
                row = connection.execute(
                    text("SELECT COUNT(*) AS workspace_count FROM workspaces")
                ).fetchone()
        except Exception:  # noqa: BLE001 - health must collapse backend failures.
            return False
        return row is not None

    def _load_workspace(
        self, tenant_id: str, workspace_id: str
    ) -> Mapping[str, object]:
        """Read an active Workspace belonging to the authenticated tenant."""
        try:
            with get_database().engine.connect() as connection:
                row = (
                    connection.execute(
                        text("""
                    SELECT
                        t.neo4j_database,
                        t.status AS tenant_status,
                        w.status AS workspace_status,
                        w.instance_status,
                        w.instance_version,
                        w.endpoint_ref,
                        w.secret_ref
                    FROM workspaces AS w
                    JOIN tenants AS t ON t.id = w.tenant_id
                    WHERE w.id = :workspace_id AND w.tenant_id = :tenant_id
                    """),
                        {"workspace_id": workspace_id, "tenant_id": tenant_id},
                    )
                    .mappings()
                    .first()
                )
        except Exception as exc:
            raise ContextRuntimeUnavailable(
                "workspace runtime registry is unavailable"
            ) from exc
        if row is None:
            raise PermissionError(
                "authenticated Workspace is not registered for this tenant"
            )
        if row["tenant_status"] != "active" or row["workspace_status"] != "active":
            raise PermissionError("authenticated Workspace is not active")
        return row

    def _load_connection(
        self, tenant_id: str, workspace_id: str
    ) -> GraphConnectionConfig:
        """Read the registered graph route and resolve its credential references."""
        row = self._load_workspace(tenant_id, workspace_id)
        if row["instance_status"] not in {"ready", "active", "running"}:
            raise ContextRuntimeUnavailable("workspace graph instance is not ready")

        endpoint = self._dispatch_endpoint(str(row["endpoint_ref"] or ""))
        graph_name = None
        if endpoint.startswith(_HOLO_ENDPOINT_SCHEME):
            from ..store.holoage.target import provisionable_graph_name

            try:
                graph_name = provisionable_graph_name(
                    workspace_id,
                    graph_name=endpoint.removeprefix(_HOLO_ENDPOINT_SCHEME),
                    reserved_graph_names=(self._settings.graph.graph_name,),
                )
            except (TypeError, ValueError) as exc:
                raise ContextRuntimeUnavailable(
                    "workspace graph name is invalid"
                ) from exc
        elif str(row["instance_version"] or "") == _HOLO_INSTANCE_VERSION:
            raise ContextRuntimeUnavailable("workspace graph name is not registered")
        if graph_name is not None:
            with get_database().engine.connect() as connection:
                others = (
                    connection.execute(
                        text("SELECT id, endpoint_ref FROM workspaces WHERE id <> :id"),
                        {"id": workspace_id},
                    )
                    .mappings()
                    .all()
                )
            for other in others:
                reference = str(other["endpoint_ref"] or "")
                if not reference:
                    continue
                try:
                    endpoint = self._dispatch_endpoint(reference)
                except ContextRuntimeUnavailable:
                    continue
                if (
                    endpoint.startswith(_HOLO_ENDPOINT_SCHEME)
                    and endpoint.removeprefix(_HOLO_ENDPOINT_SCHEME).strip()
                    == graph_name
                ):
                    raise ContextRuntimeUnavailable(
                        "workspace graph is registered to multiple workspaces"
                    )
            return HoloConnectionConfig(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                graph_name=graph_name,
            )

        endpoint = self._resolve_endpoint(str(row["endpoint_ref"] or ""))
        secret = self._resolve_secret(str(row["secret_ref"] or ""))
        user = secret.get("user")
        password = secret.get("password")
        database = secret.get("database") or row["neo4j_database"]
        if not isinstance(user, str) or not user.strip():
            raise ContextRuntimeUnavailable("workspace graph user is unavailable")
        if not isinstance(password, str) or not password:
            raise ContextRuntimeUnavailable("workspace graph password is unavailable")
        if database is not None and not isinstance(database, str):
            raise ContextRuntimeUnavailable("workspace graph database is invalid")
        return Neo4jConnectionConfig(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            neo4j_uri=endpoint,
            neo4j_user=user.strip(),
            neo4j_password=password,
            neo4j_database=database,
        )

    def _dispatch_endpoint(self, reference: str) -> str:
        """Resolve an endpoint reference just enough to route its Workspace."""
        value = self._resolve_environment_reference(reference)
        return reference if value is None else value

    def _resolve_endpoint(self, reference: str) -> str:
        """Resolve a literal or environment-backed graph endpoint reference."""
        value = self._resolve_environment_reference(reference)
        if value is None:
            value = reference
        try:
            return require_trimmed_text(value, "workspace endpoint_ref")
        except (TypeError, ValueError) as exc:
            raise ContextRuntimeUnavailable(
                "workspace graph endpoint is unavailable"
            ) from exc

    def _resolve_secret(self, reference: str) -> Mapping[str, object]:
        """Read one JSON credential document without exposing its contents."""
        raw = self._resolve_environment_reference(reference)
        try:
            if raw is None:
                if not reference.startswith("file://"):
                    raise ContextRuntimeUnavailable(
                        "workspace graph secret reference is unsupported"
                    )
                raw = Path(reference.removeprefix("file://")).read_text(
                    encoding="utf-8"
                )
            document = json.loads(raw)
        except ContextRuntimeUnavailable:
            raise
        except (OSError, json.JSONDecodeError):
            raise ContextRuntimeUnavailable(
                "workspace graph secret is unavailable"
            ) from None
        if not isinstance(document, Mapping):
            raise ContextRuntimeUnavailable("workspace graph secret is invalid")
        return document

    def _resolve_environment_reference(self, reference: str) -> str | None:
        """Resolve the finite env:// and env: reference forms."""
        name: str | None = None
        if reference.startswith("env://"):
            name = reference.removeprefix("env://")
        elif reference.startswith("env:"):
            name = reference.removeprefix("env:")
        if name is None:
            return None
        try:
            variable = require_trimmed_text(name, "environment reference")
        except (TypeError, ValueError) as exc:
            raise ContextRuntimeUnavailable(
                "workspace environment reference is invalid"
            ) from exc
        value = self._settings.environment_value(variable)
        if not isinstance(value, str) or not value:
            raise ContextRuntimeUnavailable(
                "workspace environment reference is unavailable"
            )
        return value


__all__ = [
    "WorkspaceResolver",
]

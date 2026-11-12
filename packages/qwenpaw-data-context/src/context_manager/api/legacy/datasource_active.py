"""Persist the temporary datasource selection shared by CM UI and Host."""

from datetime import UTC, datetime

from sqlalchemy import text
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...db import get_database
from ..dependencies import AuthorizationDependency

# TODO(cm-post-merge): move selection state and its UI/Host consumers to the
# application layer after branch integration, then remove these routes and
# retire active_datasource_selection after migrating any retained preferences.
# Keep graph queries and SQL execution bound to explicit datasource arguments.


class ActiveDatasourceRequest(BaseModel):
    """Accept the datasource identifier used by the existing UI."""

    datasource_id: str = ""


router = APIRouter(tags=["legacy-datasource-active"])


@router.get("/api/datasources/active")
@router.get("/api/v1/cm/datasources/active")
def get_active_datasource(
    request: Request,
    response: Response,
    context: AuthorizationDependency,
) -> dict[str, str]:
    """Read this user's selection if its workspace datasource still exists."""
    response.headers["Deprecation"] = "true"
    with get_database().engine.connect() as connection:
        row = (
            connection.execute(
                text("""
            SELECT s.datasource_id
            FROM active_datasource_selection s
            JOIN datasource d ON d.datasource_id = s.datasource_id
                AND d.tenant_id = s.tenant_id AND d.workspace_id = s.workspace_id
                AND d.is_deleted = 0
            WHERE s.tenant_id = :tenant_id AND s.workspace_id = :workspace_id AND s.user_id = :user_id
            """),
                {
                    "tenant_id": context.tenant_id,
                    "workspace_id": context.active_workspace_id,
                    "user_id": context.user_id,
                },
            )
            .mappings()
            .first()
        )
    return {"datasource_id": row["datasource_id"] if row else ""}


@router.put("/api/datasources/active", response_model=None)
@router.put("/api/v1/cm/datasources/active", response_model=None)
def set_active_datasource(
    payload: ActiveDatasourceRequest,
    request: Request,
    response: Response,
    context: AuthorizationDependency,
) -> dict[str, str | bool] | JSONResponse:
    """Validate and save this user's workspace-scoped selection."""
    response.headers["Deprecation"] = "true"
    code = payload.datasource_id.strip()
    scope = {
        "tenant_id": context.tenant_id,
        "workspace_id": context.active_workspace_id,
        "user_id": context.user_id,
    }
    with get_database().engine.begin() as connection:
        row = (
            connection.execute(
                text("""
            SELECT datasource_id FROM datasource
            WHERE (datasource_id = :code OR CAST(id AS TEXT) = :code)
                AND tenant_id = :tenant_id AND workspace_id = :workspace_id AND is_deleted = 0
            """),
                {"code": code, **scope},
            )
            .mappings()
            .first()
            if code
            else None
        )
        if not row or not row["datasource_id"]:
            return JSONResponse(
                status_code=400,
                headers={"Deprecation": "true"},
                content={
                    "success": False,
                    "code": "INVALID_DATASOURCE",
                    "message": (
                        f"datasource_id {code!r} 在当前 tenant/workspace 中未找到或已删除"
                        if code
                        else "datasource_id 不能为空"
                    ),
                },
            )
        selected = row["datasource_id"]
        updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        connection.execute(
            text("""
            INSERT INTO active_datasource_selection
                (tenant_id, workspace_id, user_id, datasource_id, updated_at)
            VALUES (:tenant_id, :workspace_id, :user_id, :datasource_id, :updated_at)
            ON CONFLICT (tenant_id, workspace_id, user_id) DO UPDATE SET
                datasource_id = excluded.datasource_id,
                updated_at = excluded.updated_at
            """),
            {**scope, "datasource_id": selected, "updated_at": updated_at},
        )
    return {"success": True, "datasource_id": selected, "message": "switched"}

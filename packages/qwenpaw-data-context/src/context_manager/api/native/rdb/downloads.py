"""Serve saved SQL CSV results through the Host download contract."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ....sql.artifact import sql_result_path
from ...dependencies import AuthorizationDependency, RuntimeDependency

router = APIRouter(prefix="/api/v1/cm/downloads", tags=["context-manager"])


@router.get("/{result_id}.csv", response_class=FileResponse)
def download_sql_result(
    result_id: str,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> FileResponse:
    """Download a saved result in the current authenticated workspace."""
    try:
        scope = runtime.issue_graph_read_scope(context, owner_id=context.user_id)
        path = sql_result_path(result_id, scope=scope)
    except PermissionError as exc:
        raise HTTPException(403, "SQL result access is forbidden") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SQL result not found") from exc
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="SQL result is unavailable"
        ) from exc
    return FileResponse(
        path,
        media_type="text/csv",
        filename=f"{result_id}.csv",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

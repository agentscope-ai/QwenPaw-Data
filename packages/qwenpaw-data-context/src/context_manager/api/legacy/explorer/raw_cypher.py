"""Adapt the existing frontend Raw Cypher endpoint to compatibility."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ....compatibility.explorer.raw_cypher import RawQueryViolation
from ...dependencies import AuthorizationDependency, RuntimeDependency
from .graph_center import failure, success

router = APIRouter(prefix="/api/v1/admin", tags=["cypher"])


class RawCypherRequest(BaseModel):
    cypher: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(200, ge=1, le=1000)


@router.post("/cypher/raw")
async def raw_cypher(
    body: RawCypherRequest,
    request: Request,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def execute():
        return runtime.raw_cypher(context).execute(
            **body.model_dump(),
            context=context,
            token=request.headers.get("X-QwenPaw-Data-Breakglass-Token", ""),
            control_plane_dsn=request.app.state.auth_config.control_plane_dsn,
            actor_ip=request.client.host if request.client else "",
        )

    try:
        data = await runtime.run_background(execute)
    except BlockingIOError as exc:
        raise HTTPException(
            503, failure("EXECUTION_BUSY", "Graph write execution is busy")
        ) from exc
    except RawQueryViolation as exc:
        raise HTTPException(exc.status_code, failure(exc.code, str(exc))) from exc
    return success(data)

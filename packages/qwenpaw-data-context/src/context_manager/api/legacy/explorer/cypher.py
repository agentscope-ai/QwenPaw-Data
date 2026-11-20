"""Expose develop's scoped, read-only Explorer Cypher contract."""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ....compatibility.explorer.cypher import CypherWriteBlocked
from ....cypher.cypher_ast import CypherAstError
from ...dependencies import AuthorizationDependency, RuntimeDependency
from .graph_center import failure, success


class CypherRequest(BaseModel):
    cypher: str
    params: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(200, ge=1, le=1000)
    response_format: Literal["auto", "graph", "table"] = "auto"
    datasource_id: str = ""
    scope: Literal["auto", "global_mg"] = "auto"


router = APIRouter(prefix="/api/v1/admin", tags=["cypher"])


@router.post("/cypher")
async def execute_cypher(
    body: CypherRequest, runtime: RuntimeDependency, context: AuthorizationDependency
):
    def execute():
        return runtime.explorer_cypher(context).execute(
            **body.model_dump(),
            user_id=context.user_id,
            is_admin=context.active_workspace_role in {"ws_admin", "tenant_admin"},
        )

    try:
        data = await runtime.run_graph_read(execute)
    except BlockingIOError as exc:
        raise HTTPException(503, str(exc)) from exc
    except CypherWriteBlocked as exc:
        raise HTTPException(403, failure("WRITE_BLOCKED", str(exc))) from exc
    except CypherAstError as exc:
        raise HTTPException(400, failure("SCOPE_ERROR", str(exc))) from exc
    return success(data)

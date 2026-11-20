"""Expose User Graph profile, memory management and manual-ingest HTTP APIs."""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from qwenpaw_data_auth.authorization import AuthorizationContext
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, field_validator

from ....auth import GraphScopeError, WriteOwner
from ....store import StoreError
from ....user.crud import UserGraphError
from ...dependencies import (
    AuthorizationDependency,
    RuntimeDependency,
)
from .ingest import ManualUserIngestResponse, trigger_user_ingest
from .requests import (
    MemoryCreate,
    MemoryPatch,
    MemoryReference,
    MemoryStatus,
    MemoryType,
    ProfilePatch,
)

_T = TypeVar("_T")


class UserProfile(BaseModel):
    display_name: str | None = None
    description: str | None = None
    locale: str | None = None
    timezone: str | None = None


class UserView(BaseModel):
    workspace_id: str
    user_id: str
    profile: UserProfile | None
    status: str
    created_at: datetime | None
    updated_at: datetime | None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def timestamp_text(cls, value: object) -> object:
        # Store adapters can return datetime-like values as well as native datetimes.
        return value.isoformat() if hasattr(value, "isoformat") else value


class GlobalMemoryView(BaseModel):
    id: str
    type: Literal["principle", "preference"]
    description: str


class GlobalMemoriesResponse(BaseModel):
    workspace_id: str
    user_id: str
    profile: dict[str, str] | None
    type_descriptions: dict[str, str]
    principles: list[GlobalMemoryView]
    preferences: list[GlobalMemoryView]


class ReferenceView(MemoryReference):
    name: str


class MemoryView(BaseModel):
    id: str
    workspace_id: str
    owner_id: str
    type: MemoryType
    description: str
    source_type: str
    source_ref: str
    source_refs: list[str]
    triggers: list[str]
    status: MemoryStatus
    created_at: datetime | None
    updated_at: datetime | None
    references: list[ReferenceView]
    action: Literal["created", "merged", "updated", "revoked"] | None = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def timestamp_text(cls, value: object) -> object:
        return value.isoformat() if hasattr(value, "isoformat") else value


class PaginationMeta(BaseModel):
    total: int
    page: int
    page_size: int
    has_more: bool


class UserGraphEnvelope(BaseModel, Generic[_T]):
    ok: bool = True
    data: _T
    error: dict[str, object] | None = None
    meta: PaginationMeta | None = None


def review_context(
    context: AuthorizationDependency,
    workspace_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> AuthorizationContext:
    """Validate the legacy Workspace selector; Authority decides review permission."""
    if workspace_id is not None and workspace_id.strip() != context.active_workspace_id:
        raise HTTPException(
            403, "workspace_id must match the authenticated active workspace"
        )
    return context


ReviewContextDependency = Annotated[AuthorizationContext, Depends(review_context)]
Keyword = Annotated[str | None, Query(max_length=256)]
UserId = Annotated[
    str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
MemoryId = Annotated[
    str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]


def _pagination(page: int, page_size: int) -> tuple[int, int]:
    return max(1, page or 1), max(1, min(page_size or 20, 100))


_user_graph_router = APIRouter(
    prefix="/api/v1/ug",
    tags=["user-graph"],
)


@_user_graph_router.get(
    "/me", response_model=UserGraphEnvelope[UserView | list[UserView]]
)
async def get_me(context: AuthorizationDependency, runtime: RuntimeDependency):
    def read():
        scope = runtime.issue_graph_read_scope(
            context, owner_id=context.user_id, zone="user"
        )
        return {"data": runtime.user_crud(scope).get_user(context.user_id, scope) or []}

    try:
        return await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc


@_user_graph_router.get("/me/global-memories", response_model=GlobalMemoriesResponse)
async def get_my_global_memories(
    context: AuthorizationDependency,
    runtime: RuntimeDependency,
):
    """Read the caller's active long-term context for Host prompt assembly."""

    def read():
        scope = runtime.issue_graph_read_scope(
            context, owner_id=context.user_id, zone="user"
        )
        return runtime.user_crud(scope).get_global_memories(scope)

    try:
        return await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc


@_user_graph_router.patch("/me", response_model=UserGraphEnvelope[UserView])
async def patch_me(
    body: ProfilePatch, context: AuthorizationDependency, runtime: RuntimeDependency
):
    def write():
        try:
            scope = runtime.issue_graph_write_scope(
                context, owner=WriteOwner.USER, zone="user"
            )
            return {
                "data": runtime.user_crud(scope).patch_profile(
                    body.model_dump(exclude_unset=True), scope
                )
            }
        except (UserGraphError, StoreError, PermissionError) as exc:
            raise _http_error(exc) from exc

    try:
        return await runtime.run_background(write)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph write execution is busy") from exc


@_user_graph_router.get(
    "/me/memories", response_model=UserGraphEnvelope[list[MemoryView]]
)
async def list_my_memories(
    context: AuthorizationDependency,
    runtime: RuntimeDependency,
    page: int = 1,
    page_size: int = 20,
    type: MemoryType | None = None,
    status: MemoryStatus | None = None,
    q: Keyword = None,
):
    page, page_size = _pagination(page, page_size)

    def read():
        scope = runtime.issue_graph_read_scope(context, owner_id=context.user_id)
        return runtime.user_crud(scope).list_memories(
            context.user_id,
            scope,
            page=page,
            page_size=page_size,
            memory_type=type,
            status=status,
            q=q,
        )

    try:
        data, total = await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc
    return {
        "data": data,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        },
    }


@_user_graph_router.post(
    "/me/memories",
    response_model=UserGraphEnvelope[MemoryView],
    responses={201: {"model": UserGraphEnvelope[MemoryView]}},
)
async def create_memory(
    body: MemoryCreate,
    response: Response,
    context: AuthorizationDependency,
    runtime: RuntimeDependency,
):
    try:
        scope = runtime.issue_graph_read_scope(context, owner_id=context.user_id)
        write_scope = runtime.issue_graph_write_scope(
            context, owner=WriteOwner.USER, zone="user"
        )
        service = runtime.user_crud(write_scope)
        data = await runtime.run_background(
            service.create_memory,
            body.model_dump(exclude={"references"}),
            scope,
            write_scope,
            references=[(ref.label, ref.key) for ref in body.references],
        )
    except (
        UserGraphError,
        StoreError,
        PermissionError,
        BlockingIOError,
    ) as exc:
        if isinstance(exc, UserGraphError) and exc.code == "invalid_memory_shape":
            raise RequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body",),
                        "msg": str(exc),
                        "input": body.model_dump(),
                    }
                ]
            ) from exc
        raise _http_error(exc) from exc
    response.status_code = 201 if data["action"] == "created" else 200
    return {"data": data}


@_user_graph_router.get(
    "/me/memories/{memory_id}", response_model=UserGraphEnvelope[MemoryView]
)
async def get_my_memory(
    memory_id: MemoryId,
    context: AuthorizationDependency,
    runtime: RuntimeDependency,
):
    def read():
        scope = runtime.issue_graph_read_scope(context, owner_id=context.user_id)
        return {
            "data": runtime.user_crud(scope).get_memory(
                context.user_id, memory_id, scope
            )
        }

    try:
        return await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc


@_user_graph_router.patch(
    "/me/memories/{memory_id}", response_model=UserGraphEnvelope[MemoryView]
)
async def patch_memory(
    memory_id: MemoryId,
    body: MemoryPatch,
    context: AuthorizationDependency,
    runtime: RuntimeDependency,
):
    try:
        scope = runtime.issue_graph_read_scope(context, owner_id=context.user_id)
        write_scope = runtime.issue_graph_write_scope(
            context, owner=WriteOwner.USER, zone="user"
        )
        service = runtime.user_crud(write_scope)
        return {
            "data": await runtime.run_background(
                service.patch_memory,
                memory_id,
                body.model_dump(exclude_unset=True, exclude={"references"}),
                scope,
                write_scope,
                references=(
                    [(ref.label, ref.key) for ref in body.references]
                    if "references" in body.model_fields_set
                    else None
                ),
            )
        }
    except (
        UserGraphError,
        StoreError,
        PermissionError,
        BlockingIOError,
    ) as exc:
        raise _http_error(exc) from exc


@_user_graph_router.delete(
    "/me/memories/{memory_id}", response_model=UserGraphEnvelope[MemoryView]
)
async def revoke_memory(
    memory_id: MemoryId,
    context: AuthorizationDependency,
    runtime: RuntimeDependency,
):
    def write():
        try:
            scope = runtime.issue_graph_read_scope(context, owner_id=context.user_id)
            write_scope = runtime.issue_graph_write_scope(
                context, owner=WriteOwner.USER, zone="user"
            )
            return {
                "data": runtime.user_crud(scope).revoke_memory(
                    memory_id, scope, write_scope
                )
            }
        except (UserGraphError, StoreError, PermissionError) as exc:
            raise _http_error(exc) from exc

    try:
        return await runtime.run_background(write)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph write execution is busy") from exc


@_user_graph_router.get(
    "/users",
    response_model=UserGraphEnvelope[list[UserView]],
)
async def list_users(
    context: ReviewContextDependency,
    runtime: RuntimeDependency,
    page: int = 1,
    page_size: int = 20,
):
    page, page_size = _pagination(page, page_size)

    def read():
        scope = runtime.issue_graph_read_scope(context, zone="user", review=True)
        return runtime.user_crud(scope).list_users(
            scope, page=page, page_size=page_size
        )

    try:
        data, total = await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc
    return {
        "data": data,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        },
    }


@_user_graph_router.get(
    "/users/{user_id}",
    response_model=UserGraphEnvelope[UserView | list[UserView]],
)
async def get_user(
    user_id: UserId, context: ReviewContextDependency, runtime: RuntimeDependency
):
    def read():
        scope = runtime.issue_graph_read_scope(
            context, owner_id=user_id, zone="user", review=True
        )
        return {"data": runtime.user_crud(scope).get_user(user_id, scope) or []}

    try:
        return await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc


@_user_graph_router.get(
    "/users/{user_id}/memories",
    response_model=UserGraphEnvelope[list[MemoryView]],
)
async def list_memories(
    user_id: UserId,
    context: ReviewContextDependency,
    runtime: RuntimeDependency,
    page: int = 1,
    page_size: int = 20,
    type: MemoryType | None = None,
    status: MemoryStatus | None = None,
    q: Keyword = None,
):
    page, page_size = _pagination(page, page_size)

    def read():
        scope = runtime.issue_graph_read_scope(context, owner_id=user_id, review=True)
        return runtime.user_crud(scope).list_memories(
            user_id,
            scope,
            page=page,
            page_size=page_size,
            memory_type=type,
            status=status,
            q=q,
        )

    try:
        data, total = await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc
    return {
        "data": data,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        },
    }


@_user_graph_router.get(
    "/users/{user_id}/memories/{memory_id}",
    response_model=UserGraphEnvelope[MemoryView],
)
async def get_memory(
    user_id: UserId,
    memory_id: MemoryId,
    context: ReviewContextDependency,
    runtime: RuntimeDependency,
):
    def read():
        scope = runtime.issue_graph_read_scope(context, owner_id=user_id, review=True)
        return {"data": runtime.user_crud(scope).get_memory(user_id, memory_id, scope)}

    try:
        return await runtime.run_graph_read(read)
    except (UserGraphError, StoreError, PermissionError, BlockingIOError) as exc:
        raise _http_error(exc) from exc


def _http_error(
    exc: UserGraphError | StoreError | PermissionError | BlockingIOError,
) -> HTTPException:
    """Translate known business failures without exposing backend or provider details."""
    if isinstance(exc, UserGraphError):
        response_status = {
            "not_found": 404,
            "conflict": 409,
            "invalid_memory": 422,
            "invalid_memory_shape": 422,
            "invalid_reference": 422,
        }[exc.code]
        return HTTPException(response_status, str(exc))
    if isinstance(exc, BlockingIOError):
        return HTTPException(503, "User Graph query execution is busy")
    if isinstance(exc, StoreError):
        return HTTPException(503 if exc.retryable else 500, "User Graph storage failed")
    if isinstance(exc, GraphScopeError):
        return HTTPException(403, str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(403, "User Graph access is forbidden")
    raise TypeError(f"unexpected User Graph error: {type(exc).__name__}")


router = APIRouter()
router.include_router(_user_graph_router)
# TODO(cm-ug-walkthrough): remove this route once
# the User Graph extraction walkthrough no longer needs an HTTP trigger.
router.add_api_route(
    "/api/cm/user/ingest",
    trigger_user_ingest,
    methods=["POST"],
    tags=["context-manager-user"],
    response_model=ManualUserIngestResponse,
    response_model_exclude_none=True,
)

__all__ = ["router"]

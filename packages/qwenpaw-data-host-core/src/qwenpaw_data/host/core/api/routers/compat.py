# -*- coding: utf-8 -*-
"""App 0.3 Host REST routes kept on the current services and runtime.

The pinned App bridge still calls these 0.3 paths and envelopes; native
routes keep their own shapes. Every handler resolves the session through
``access_session`` first, so unknown sessions and other users' sessions are
indistinguishable (404).
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.mappers import chat_to_schema
from qwenpaw_data.host.core.api.models.requests import CreateChatRequest
from qwenpaw_data.host.core.api.routers.console import admit_chat, launch_chat
from qwenpaw_data.host.core.api.routers.datasources import get_context_manager_client
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.integrations.cm import ContextManagerClient
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.query_log import Step, log_info
from qwenpaw_data.host.core.repositories import ChatRepository
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.settings.mcp import McpSettings
from qwenpaw_data.host.core.utils.mcp_headers import parse_mcp_headers
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(tags=["compat"])

# Only features this Host implements and tests. Request-scoped model bindings
# (#81) are accepted but not applied yet, so they are reported as absent.
CONSOLE_CAPABILITIES: dict[str, Any] = {
    "console_protocol": 1,
    "scoped_sessions": True,
    "runtime_model_bindings": False,
    "dataset_context": True,
}

# How often a stop re-reads a turn that another worker is unwinding.
_STOP_POLL_SECONDS = 0.2


def _raise(exc: Exception) -> None:
    http = map_domain_error(exc)
    if http:
        raise http from exc
    raise exc


def _artifact_not_found() -> None:
    raise_api("NOT_FOUND", "artifact not found", status=404)


def _artifact_root(session: Session) -> Path:
    return UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=session.agent_id,
        session_id=session.id,
    ).artifact_dir


def contained_artifact(root: Path, path: str) -> Path | None:
    """0.3 containment: plain relative segments resolving to a file in ``root``.

    Escape characters and dot segments are rejected before any filesystem
    access; the resolved target (symlinks followed) must stay below ``root``.
    """
    if (
        not path
        or path.startswith(("/", "~"))
        or "\\" in path
        or "\x00" in path
        or any(segment in {"", ".", ".."} for segment in path.split("/"))
    ):
        return None
    root = root.resolve()
    normalized = os.path.normpath(os.path.join(str(root), path))
    if not normalized.startswith(str(root) + os.sep):
        return None
    candidate = Path(normalized).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


@router.post("/sessions/{session_id}/chats")
async def create_chat(
    session_id: str,
    body: CreateChatRequest,
    request: Request,
    background: BackgroundTasks,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
    cm: ContextManagerClient = Depends(get_context_manager_client),
) -> dict[str, Any]:
    """0.3 chat creation: the native console admission for the path session."""
    try:
        mcp_headers = parse_mcp_headers(request.headers.get(McpSettings.HEADERS_HEADER))
        chat, access_token = await admit_chat(
            db,
            identity,
            session_id=session_id,
            body=body,
            cm=cm,
        )
    except Exception as exc:
        _raise(exc)
    response = {"chat": chat_to_schema(chat)}
    launch_chat(
        background,
        chat,
        identity=identity,
        access_token=access_token,
        mcp_headers=mcp_headers,
        model_binding_request=body.runtime_models,
    )
    return response


@router.get("/sessions/{session_id}/chats")
async def list_chats(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id)
        chats = await ChatRepository(db, identity).list_for_session(session_id)
    except Exception as exc:
        _raise(exc)
    return {"items": [chat_to_schema(chat) for chat in chats]}


async def _stop_stored(
    db: AsyncSession, repo: ChatRepository, chat: Chat
) -> Chat:
    """Cancel a turn this process does not run: an orphan or another worker's."""
    deadline = time.monotonic() + get_settings().stream.heartbeat_ttl_seconds
    chat = await repo.stop(chat)
    await db.commit()
    while chat.is_active() and time.monotonic() < deadline:
        await asyncio.sleep(_STOP_POLL_SECONDS)
        chat = await repo.get(chat.id, populate_existing=True)
        await db.commit()
    return chat


@router.post("/sessions/{session_id}/chats/{chat_id}/stop")
async def stop_chat(
    session_id: str,
    chat_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """0.3 stop: answers once the turn has unwound (the App waits up to 180s)."""
    try:
        await access_session(db, identity, session_id, write=True)
        repo = ChatRepository(db, identity)
        chat = await repo.get(chat_id, session_id=session_id)
        log_info(
            request_id=chat_id,
            step=Step.CHAT_STOP,
            message="Chat stop requested",
            context={"session_id": session_id},
        )
        # End the read transaction: the unwinding turn writes its terminal state.
        await db.commit()
        runtime = get_runtime_registry().get(chat.id)
        if runtime is None or not await runtime.cancel():
            await _stop_stored(db, repo, chat)
        chat = await repo.get(chat_id, session_id=session_id, populate_existing=True)
    except Exception as exc:
        _raise(exc)
    return {"chat": chat_to_schema(chat)}


@router.get("/sessions/{session_id}/artifacts/file")
async def download_artifact(
    session_id: str,
    path: str = Query("", description="Path relative to the session artifacts"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """0.3 raw artifact bytes; every lookup or containment failure is a 404."""
    try:
        session = await access_session(db, identity, session_id)
    except Exception as exc:
        if map_domain_error(exc) is None:
            raise
        _artifact_not_found()
    file_path = contained_artifact(_artifact_root(session), path)
    if file_path is None:
        _artifact_not_found()
    return FileResponse(
        file_path,
        filename=file_path.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/console/capabilities")
async def console_capabilities(
    _identity: Identity = Depends(get_identity),
) -> dict[str, Any]:
    return dict(CONSOLE_CAPABILITIES)

# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.auth_middleware import auth_enabled
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.settings.mcp import McpSettings
from qwenpaw_data.host.core.utils.mcp_headers import parse_mcp_headers
from qwenpaw_data.host.core.api.mappers import chat_to_schema
from qwenpaw_data.host.core.api.models.common import AttachmentRefSchema
from qwenpaw_data.host.core.api.models.requests import (
    ChatAdmissionFields,
    ConsoleChatRequest,
)
from qwenpaw_data.host.core.api.routers.datasources import (
    get_context_manager_client,
    require_known_datasets,
)
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.preference import UserRuntimeConfig
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.integrations.cm import ContextManagerClient
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import (
    AttachmentRepository,
    ChatRepository,
    PreferencesRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.query_log import Step, log_info
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.auth import (
    generate_temporary_token,
    resolve_context_access_token,
)
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(prefix="/console", tags=["console"])


def _session_paths(session: Session) -> UserAgentPaths:
    return UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=session.agent_id,
        session_id=session.id,
    )


def _raise(exc: Exception) -> None:
    http = map_domain_error(exc)
    if http:
        raise http from exc
    raise


@router.post("/upload")
async def upload_attachment(
    session_id: str = Form(...),
    file: UploadFile = File(...),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        if not file.filename:
            raise ValueError("filename is required")
        session = await access_session(db, identity, session_id, write=True)
        repo = AttachmentRepository(db, identity)
        filename = Path(file.filename).name
        if await repo.find_by_filename(session.id, filename):
            raise ValueError(f"duplicate filename: {filename}")
        attachment = Attachment.receive(
            session_id=session.id,
            identity=identity,
            filename=filename,
            data=await file.read(),
            dest_dir=_session_paths(session).uploads_dir,
        )
        await repo.add(attachment)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"attachment": AttachmentRefSchema.model_validate(attachment.to_ref())}


@router.delete("/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    attachment_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        repo = AttachmentRepository(db, identity)
        attachment = await repo.get(attachment_id)
        session = await access_session(db, identity, attachment.session_id, write=True)
        chats = await ChatRepository(db, identity).list_for_session(session.id)
        if any(chat.references_attachment(attachment.id) for chat in chats):
            raise RuntimeError("CONFLICT: attachment is used by a chat")
        attachment.remove_file(_session_paths(session).workspace)
        await repo.delete(attachment)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return Response(status_code=204)


@router.post("/chat")
async def console_chat(
    body: ConsoleChatRequest,
    request: Request,
    background: BackgroundTasks,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
    cm: ContextManagerClient = Depends(get_context_manager_client),
) -> dict[str, Any]:
    try:
        mcp_headers = parse_mcp_headers(request.headers.get(McpSettings.HEADERS_HEADER))
        chat, access_token, runtime_config, context_model_header = await admit_chat(
            db,
            identity,
            session_id=body.session_id,
            body=body,
            cm=cm,
            attachment_ids=body.attachment_ids,
            artifact_comments=[
                item.model_dump(mode="json") for item in body.artifact_comments
            ],
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    response = {"chat": chat_to_schema(chat)}
    launch_chat(
        background,
        chat,
        identity=identity,
        access_token=access_token,
        mcp_headers=mcp_headers,
        runtime_config=runtime_config,
        context_model_header=context_model_header,
    )
    return response


async def admit_chat(
    db: AsyncSession,
    identity: Identity,
    *,
    session_id: str,
    body: ChatAdmissionFields,
    cm: ContextManagerClient,
    attachment_ids: list[str] | None = None,
    artifact_comments: list[dict[str, Any]] | None = None,
) -> tuple[Chat, str | None, UserRuntimeConfig, str | None]:
    """Admit one chat turn for every chat-creation route (native and 0.3 compat).

    Write access to the session, a resolvable model configuration and a
    dataset focus known to the session datasource are checked before the
    chat row is committed.
    """
    session_repo = SessionRepository(db, identity)
    session = await access_session(db, identity, session_id, write=True)
    prefs = await PreferencesRepository(db, identity).load()
    binding = body.runtime_models
    runtime_config = binding.runtime_config(prefs) if binding else prefs.resolve()
    context_model_header = (
        binding.resolved_context_header(runtime_config) if binding else None
    )
    if context_model_header and (
        auth_enabled() or not resolve_context_access_token(None)
    ):
        raise ValueError(
            "DataBridge model binding requires local mode and a configured "
            "Context service credential"
        )
    dataset_names = await require_known_datasets(
        cm,
        datasource_id=(body.datasource_id or "").strip()
        or getattr(session, "datasource_id", None),
        dataset_names=body.dataset_names,
    )
    attachments = await AttachmentRepository(db, identity).require_for_session(
        session.id,
        attachment_ids or [],
        workspace=_session_paths(session).workspace,
    )
    chat = session.open_chat(
        text=body.text,
        datasource_id=body.datasource_id,
        dataset_names=dataset_names,
        has_active_chat=await session_repo.has_active_chat(session.id),
        artifact_comments=artifact_comments or [],
        attachments=[item.to_ref() for item in attachments],
    )
    await session_repo.save(session)
    await ChatRepository(db, identity).add(
        chat, inherit_plan=get_settings().runtime.inherit_plan
    )
    await db.commit()
    access_token = await asyncio.to_thread(generate_temporary_token, identity)
    return chat, access_token, runtime_config, context_model_header


def launch_chat(
    background: BackgroundTasks,
    chat: Chat,
    *,
    identity: Identity,
    access_token: str | None,
    mcp_headers: dict[str, dict[str, str]],
    runtime_config: UserRuntimeConfig,
    context_model_header: str | None,
) -> None:
    """Run an admitted turn after the response (native and 0.3 compat routes).

    The runtime is registered when background execution begins so an in-process
    stop can await its unwind (``ChatRuntime.cancel``). Registration before the
    background task starts would leave a stale entry if the task never runs.

    Model settings are an admission-time snapshot, held only by this task.
    Credentials never become part of the persisted Chat or preferences.
    """
    runtime = ChatRuntime(get_session_factory(), chat.id)
    registry = get_runtime_registry()

    async def run(
        *,
        identity: Identity,
        access_token: str | None,
        mcp_headers: dict[str, dict[str, str]],
    ) -> None:
        registry.register(chat.id, runtime)
        try:
            await runtime.run(
                identity=identity,
                access_token=access_token,
                mcp_headers=mcp_headers,
                runtime_config=runtime_config,
                context_model_header=context_model_header,
            )
        finally:
            registry.unregister(chat.id)

    background.add_task(
        run,
        identity=identity,
        access_token=access_token,
        mcp_headers=mcp_headers,
    )


@router.post("/chat/stop")
async def stop_chat(
    session_id: str = Query(...),
    chat_id: str = Query(...),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        log_info(
            request_id=chat_id,
            step=Step.CHAT_STOP,
            message="Chat stop requested",
            context={"session_id": session_id},
        )
        repo = ChatRepository(db, identity)
        chat = await repo.get(chat_id, session_id=session_id)
        chat = await repo.stop(chat)
        await db.commit()
        return {"chat": chat_to_schema(chat)}
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

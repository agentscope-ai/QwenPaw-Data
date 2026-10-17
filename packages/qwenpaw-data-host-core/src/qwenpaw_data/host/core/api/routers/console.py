# -*- coding: utf-8 -*-
"""Console-shaped compat routes: uploads and attachment-aware chat."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response

from qwenpaw_data.host.core.api.deps import (
    ServiceState,
    get_identity,
    get_state,
    owned_session,
)
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.mappers import chat_to_schema
from qwenpaw_data.host.core.api.models.common import AttachmentRefSchema
from qwenpaw_data.host.core.api.models.requests import ConsoleChatRequest
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.stream.output_stream import OutputStream

router = APIRouter(prefix="/console", tags=["console"])


def _workspace(state: ServiceState, session_id: str) -> Path:
    return Path(state.hosts.get(session_id=session_id).paths.workspace)


def _uploads_dir(state: ServiceState, session_id: str) -> Path:
    return _workspace(state, session_id) / "uploads" / session_id


def _raise(exc: Exception) -> None:
    http = map_domain_error(exc)
    if http:
        raise http from exc
    raise exc


@router.post("/upload")
async def upload_attachment(
    session_id: str = Form(...),
    file: UploadFile = File(...),
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        if not file.filename:
            raise ValueError("filename is required")
        session = await owned_session(state, identity, session_id)
        filename = Path(file.filename).name
        if await state.attachments.find_by_filename(
            session.identity.user_id, session.id, filename
        ):
            raise ValueError(f"duplicate filename: {filename}")
        attachment = await asyncio.to_thread(
            Attachment.receive,
            session_id=session.id,
            identity=identity,
            filename=filename,
            data=await file.read(),
            dest_dir=_uploads_dir(state, session.id),
        )
        await state.attachments.add(attachment)
    except Exception as exc:
        _raise(exc)
    return {"attachment": AttachmentRefSchema.model_validate(attachment.to_ref())}


@router.delete("/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    attachment_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> Response:
    try:
        attachment = await state.attachments.get(identity.user_id, attachment_id)
        session = await owned_session(state, identity, attachment.session_id)
        chats = await state.chats.list_for_session(session.id)
        if any(chat.references_attachment(attachment.id) for chat in chats):
            raise RuntimeError("CONFLICT: attachment is used by a chat")
        await asyncio.to_thread(attachment.remove_file, _workspace(state, session.id))
        await state.attachments.delete(identity.user_id, attachment.id)
    except Exception as exc:
        _raise(exc)
    return Response(status_code=204)


@router.post("/chat")
async def console_chat(
    body: ConsoleChatRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        session = await owned_session(state, identity, body.session_id)
        attachments = await state.attachments.require_for_session(
            session.identity.user_id,
            session.id,
            body.attachment_ids,
            workspace=_workspace(state, session.id),
        )
        runtime_config = None
        context_model_header = None
        if body.runtime_models is not None:
            local = (await state.prefs.load(identity.user_id)).runtime_config()
            runtime_config = body.runtime_models.runtime_config(local)
            context_model_header = body.runtime_models.resolved_context_header(
                runtime_config
            )
        chat = session.open_chat(
            text=body.text,
            datasource_id=body.datasource_id,
            dataset_names=body.dataset_names,
            has_active_chat=await state.sessions.has_active_chat(session.id),
            artifact_comments=[
                item.model_dump(mode="json") for item in body.artifact_comments
            ],
            attachments=[item.to_ref() for item in attachments],
        )
        await state.sessions.save(session)
        await state.chats.add(chat)
    except Exception as exc:
        _raise(exc)
    runtime = ChatRuntime(
        chats=state.chats,
        events=state.events,
        hosts=state.hosts,
        prefs=state.prefs,
        sessions=state.sessions,
        settlement=state.settlement,
    )
    kwargs = {"identity": identity}
    if runtime_config is not None:
        kwargs.update(
            runtime_config=runtime_config, context_model_header=context_model_header
        )
    state.track(asyncio.create_task(runtime.run(chat.id, **kwargs)))
    return {"chat": chat_to_schema(chat)}


@router.post("/chat/stop")
async def stop_chat(
    session_id: str = Query(...),
    chat_id: str = Query(...),
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        chat = await state.chats.get(chat_id, session_id=session_id)
        runtime = get_runtime_registry().get(chat_id)
        if runtime is not None:
            await runtime.cancel()
            chat = await state.chats.get(chat_id, session_id=session_id)
        elif chat.status == "running":
            await OutputStream(
                state.events,
                session_id=session_id,
                chat_id=chat_id,
                identity=chat.identity,
            ).response_cancelled()
            chat.cancel()
            await state.chats.reload_event_watermark(chat)
            await state.chats.save(chat)
    except Exception as exc:
        _raise(exc)
    return {"chat": chat_to_schema(chat)}


@router.get("/capabilities")
async def console_capabilities(identity: Identity = Depends(get_identity)):
    return {
        "console_protocol": 1,
        "scoped_sessions": True,
        "runtime_model_bindings": True,
        "dataset_context": True,
    }

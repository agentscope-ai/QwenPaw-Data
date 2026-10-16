# -*- coding: utf-8 -*-
"""Independent, idempotent runs for the PawApp backend adapter."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.models.submissions import (
    SubmissionCommandRequest,
    SubmissionCommandSchema,
    SubmissionLookupSchema,
    SubmissionRunSchema,
    SubmitRunRequest,
)
from qwenpaw_data.host.core.api.routers.events import chat_events
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.clarification import (
    ClarificationConflict,
    ClarificationNotFound,
)
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.stream.output_stream import OutputStream

if TYPE_CHECKING:
    from qwenpaw_data.host.core.store.submissions import (
        SQLSubmissionStore,
        SubmissionRecord,
    )

router = APIRouter(tags=["submissions"])


def _store(state: ServiceState) -> SQLSubmissionStore:
    if state.submissions is None:
        raise_api(
            "VALIDATION",
            "Durable submissions require the SQL store",
            status=501,
            details={"reason": "durable_submissions_unsupported"},
        )
    return state.submissions


async def _lookup_response(
    store: SQLSubmissionStore,
    record: SubmissionRecord,
) -> SubmissionLookupSchema:
    return SubmissionLookupSchema(
        submission_id=record.submission_id,
        state="accepted",
        run=SubmissionRunSchema(
            session_id=record.session_id,
            run_id=record.run_id,
            **await store.run_snapshot(record),
        ),
    )


@router.get("/capabilities/submissions")
async def submission_capabilities(
    state: ServiceState = Depends(get_state),
) -> dict:
    supported = state.submissions is not None
    return {
        "protocol_version": 1 if supported else None,
        "durable_submissions": supported,
        "event_replay": supported,
        "durable_commands": supported,
        "scoped_host_capabilities": supported,
        "artifact_handoff": supported,
        "analysis_experience": supported,
        "session_submissions": supported,
        "optional_session_datasource": supported,
        "dataset_context": supported,
        "runtime_model_bindings": supported,
        "scoped_sessions": True,
    }


@router.get("/capabilities/analysis")
async def analysis_capabilities(
    local_only: bool = False,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict:
    """Configuration readiness only, never credentials or a provider probe."""
    ready = state.analysis_model_ready
    if local_only:
        configured = False
        if state.prefs is not None:
            configured = (
                await state.prefs.load(identity.user_id)
            ).runtime_config().default is not None
    else:
        configured = bool(ready and await ready())
    return {
        "readiness_version": 1,
        "model_configured": configured,
    }


@router.get("/sessions/{session_id}/submission-readiness")
async def session_submission_readiness(
    session_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict:
    try:
        session = await state.sessions.get(session_id)
        if session.identity.user_id != identity.user_id:
            raise LookupError("session not found")
        return {"ready": not await state.sessions.has_active_chat(session_id)}
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise


async def _accept_and_start(
    body: SubmitRunRequest,
    identity: Identity,
    state: ServiceState,
) -> SubmissionRecord:
    # A replay returns the original mapping even after files/session were deleted.
    existing = await _store(state).lookup(identity.user_id, body.submission_id)
    if existing is not None:
        _store(state)._check_digest(existing, body.request_digest())
        return existing
    runtime_config = None
    context_model_header = None
    if body.runtime_models is not None:
        from ...domain.preference import UserRuntimeConfig

        local = UserRuntimeConfig()
        if state.prefs is not None:
            local = (await state.prefs.load(identity.user_id)).runtime_config()
        runtime_config = body.runtime_models.runtime_config(local)
        context_model_header = body.runtime_models.resolved_context_header(
            runtime_config
        )
    attachments = []
    if body.session_id is not None:
        session = await state.sessions.get(body.session_id)
        if session.identity.user_id != identity.user_id:
            raise LookupError("session not found")
        host = state.hosts.get(session_id=session.id)
        attachments = await state.attachments.require_for_session(
            identity.user_id,
            session.id,
            body.attachment_ids,
            workspace=host.paths.workspace,
        )
    elif body.attachment_ids or body.artifact_comments:
        raise ValueError("session context requires an existing session")
    record, created = await _store(state).submit(
        identity=identity,
        submission_id=body.submission_id,
        request_digest=body.request_digest(),
        text=body.text,
        datasource_id=body.datasource_id,
        dataset_names=body.dataset_names,
        agent_id=body.agent_id,
        session_id=body.session_id,
        attachments=[item.to_ref() for item in attachments],
        artifact_comments=[
            item.model_dump(mode="json") for item in body.artifact_comments
        ],
    )
    if created:
        runtime = ChatRuntime(
            chats=state.chats,
            events=state.events,
            hosts=state.hosts,
            prefs=state.prefs,
            sessions=state.sessions,
            settlement=state.settlement,
        )
        run_kwargs = {"identity": identity}
        if runtime_config is not None:
            run_kwargs["runtime_config"] = runtime_config
            run_kwargs["context_model_header"] = context_model_header
        if body.capability_bridge is not None:
            run_kwargs["capability_bridge"] = body.capability_bridge.model_dump(
                mode="json",
            )
        state.track(
            asyncio.create_task(runtime.run(record.run_id, **run_kwargs)),
        )
    return record


@router.post("/submissions", response_model=SubmissionLookupSchema, status_code=202)
async def submit_run(
    body: SubmitRunRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> SubmissionLookupSchema:
    store = _store(state)
    # A disconnected HTTP caller must not cancel acceptance between commit and
    # scheduling. A process crash in that window is reconciled at startup.
    task = asyncio.create_task(_accept_and_start(body, identity, state))
    state.track(task)
    try:
        record = await asyncio.shield(task)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return await _lookup_response(store, record)


@router.get("/submissions/{submission_id}", response_model=SubmissionLookupSchema)
async def lookup_submission(
    submission_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> SubmissionLookupSchema:
    store = _store(state)
    record = await store.lookup(identity.user_id, submission_id)
    if record is None:
        return SubmissionLookupSchema(submission_id=submission_id, state="not_found")
    return await _lookup_response(store, record)


def _command_response(record) -> SubmissionCommandSchema:
    return SubmissionCommandSchema(
        submission_id=record.submission_id,
        command_id=record.command_id,
        kind=record.kind,
        state=record.state,
        reason=record.reason,
    )


async def _cancel_submission(record, state: ServiceState) -> str | None:
    chat = await state.chats.get(record.run_id, session_id=record.session_id)
    runtime = get_runtime_registry().get(record.run_id)
    if runtime is not None:
        await runtime.cancel()
        return None
    if chat.status == "running":
        # The receipt binds cancellation to this exact run. If its in-memory
        # runtime disappeared, persist the same authoritative terminal frame.
        await OutputStream(
            state.events,
            session_id=record.session_id,
            chat_id=record.run_id,
            identity=chat.identity,
        ).response_cancelled()
        chat.cancel()
        await state.chats.reload_event_watermark(chat)
        await state.chats.save(chat)
        return None
    return "already_terminal"


@router.post(
    "/submissions/{submission_id}/commands",
    response_model=SubmissionCommandSchema,
)
async def execute_submission_command(
    submission_id: str,
    body: SubmissionCommandRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> SubmissionCommandSchema:
    store = _store(state)
    submission = await store.lookup(identity.user_id, submission_id)
    if submission is None:
        raise_api("NOT_FOUND", "Submission not found", status=404)
    try:
        command, created = await store.prepare_command(
            record=submission,
            command_id=body.command_id,
            kind=body.kind,
            request_digest=body.request_digest(),
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    # Only the durable insert winner may apply the command. Concurrent and
    # post-response retries observe the same receipt without another effect.
    if not created:
        return _command_response(command)
    try:
        reason = None
        if body.kind == "answer":
            runtime = get_runtime_registry().get(submission.run_id)
            if runtime is None:
                raise ClarificationNotFound()
            runtime.answer(
                clarification_id=body.request_id or "",
                result={
                    "status": "answered",
                    "answers": [
                        answer.model_dump(exclude_none=True)
                        for answer in body.answers or []
                    ],
                },
            )
        else:
            reason = await _cancel_submission(submission, state)
        command = await store.finish_command(
            command,
            state="accepted",
            reason=reason,
        )
    except (ClarificationConflict, ClarificationNotFound):
        command = await store.finish_command(
            command,
            state="rejected",
            reason="stale_request",
        )
    return _command_response(command)


@router.get(
    "/submissions/{submission_id}/commands/{command_id}",
    response_model=SubmissionCommandSchema,
)
async def lookup_submission_command(
    submission_id: str,
    command_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> SubmissionCommandSchema:
    store = _store(state)
    submission = await store.lookup(identity.user_id, submission_id)
    if submission is None:
        raise_api("NOT_FOUND", "Submission not found", status=404)
    command = await store.lookup_command(
        identity.user_id,
        submission_id,
        command_id,
    )
    if command is None:
        return SubmissionCommandSchema(
            submission_id=submission_id,
            command_id=command_id,
            state="not_found",
        )
    return _command_response(command)


@router.get("/submissions/{submission_id}/events")
async def submission_events(
    request: Request,
    submission_id: str,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
    after_sequence_number: int = Query(-1, ge=-1),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    store = _store(state)
    record = await store.lookup(identity.user_id, submission_id)
    if record is None:
        raise_api("NOT_FOUND", "Submission not found", status=404)
    snapshot = await store.run_snapshot(record)
    if snapshot["status"] == "unknown":
        raise_api(
            "NOT_FOUND",
            "Accepted submission's run is unavailable",
            status=410,
            details={"reason": "run_missing"},
        )
    return await chat_events(
        request,
        record.session_id,
        record.run_id,
        identity,
        state,
        after_sequence_number,
        last_event_id,
    )

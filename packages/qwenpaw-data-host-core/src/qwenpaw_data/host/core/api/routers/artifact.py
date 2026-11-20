# -*- coding: utf-8 -*-
from __future__ import annotations

import mimetypes
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.models.artifact import (
    ArtifactSchema,
    SessionSegmentArtifactSchema,
    ShareFileRequest,
    ShareFileResponse,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.pinned_file_response import PinnedFileResponse
from qwenpaw_data.host.core.utils.workspace_file import open_resolved_workspace_file
from qwenpaw_data.host.core.integrations.oss import get_oss_store
from qwenpaw_data.host.core.tracing.records import artifact_chat_id, tracer_record_behavior
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.query_log import Step, log_info
from qwenpaw_data.host.core.repositories import ArtifactRepository, SegmentRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.host.core.utils.time import utcnow

router = APIRouter(prefix="/sessions/{session_id}", tags=["artifacts"])


@router.get("/artifacts")
async def list_session_artifacts(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id)
        artifacts = await ArtifactRepository(db, identity).list_for_session(session_id)
        segment_artifacts = await SegmentRepository(
            db, identity
        ).list_artifacts_for_session(session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {
        "artifacts": [ArtifactSchema.model_validate(item) for item in artifacts],
        "segment_artifacts": [
            SessionSegmentArtifactSchema.model_validate(item)
            for item in segment_artifacts
        ],
    }


# Apply isolation to the report response itself, including new-window previews.
# Never allow same-origin access to the console from generated HTML.
_HTML_CSP = (
    "sandbox allow-scripts; default-src 'none'; "
    "script-src 'unsafe-inline' https:; style-src 'unsafe-inline' https:; "
    "img-src https: data: blob:; font-src https: data:; "
    "connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)


def resolve_session_file(artifact_root: Path, relative_path: str) -> Path:
    if not relative_path.strip():
        raise_api("VALIDATION", "path is required", status=400)
    if "\x00" in relative_path:
        raise_api("VALIDATION", "path contains an invalid character", status=400)
    requested = Path(relative_path)
    if requested.anchor:
        raise_api(
            "VALIDATION", "path must be relative to the session workspace", status=400
        )

    artifact_root = artifact_root.resolve()
    # Check lexical containment before following links, then verify the target.
    # The caller still pins the file descriptor before reading.
    normalized = os.path.normpath(os.path.join(artifact_root, requested))
    if ".." not in requested.parts and normalized.startswith(os.path.join(artifact_root, "")):
        try:
            candidate = Path(normalized).resolve()
        except (RuntimeError, ValueError):
            raise_api("VALIDATION", "path cannot be resolved", status=400)
        except OSError:
            raise_api("NOT_FOUND", "workspace file not found", status=404)
        if candidate.is_relative_to(artifact_root):
            if not candidate.is_file():
                raise_api("NOT_FOUND", "workspace file not found", status=404)
            return candidate
    raise_api("VALIDATION", "path escapes the session artifact directory", status=400)


@router.get("/files/access", response_class=FileResponse)
async def access_workspace_file(
    session_id: str,
    path: str = Query(..., min_length=1),
    purpose: Literal["preview", "download"] = Query("preview"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> FileResponse:
    try:
        session = await access_session(db, identity, session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise

    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=session.agent_id,
        session_id=session.id,
    )
    file_path = resolve_session_file(paths.artifact_dir, path)
    chat_id = await artifact_chat_id(db, identity, session_id, path)
    await tracer_record_behavior(
        db,
        identity,
        session_id=session_id,
        chat_id=chat_id,
        kind="artifact_access",
        target_type="file",
        target_id=path,
        payload={"purpose": purpose},
        score_name="artifact_access",
        value=purpose,
    )
    media_type, _ = mimetypes.guess_type(file_path.name)
    relative_path = file_path.relative_to(paths.artifact_dir.resolve()).as_posix()
    headers = {}
    if media_type == "text/html":
        headers = {
            "Content-Security-Policy": _HTML_CSP,
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    try:
        file = open_resolved_workspace_file(paths.artifact_dir, file_path)
    except OSError:
        raise_api("NOT_FOUND", "workspace file not found", status=404)
    try:
        response = PinnedFileResponse(
            file, path=str(file_path), filename=file_path.name,
            media_type=media_type or "application/octet-stream", headers=headers,
            content_disposition_type="inline" if purpose == "preview" else "attachment",
        )
        log_info(
            request_id=session.id,
            step=Step.ARTIFACT_PREVIEW if purpose == "preview" else Step.ARTIFACT_DOWNLOAD,
            message="Artifact previewed" if purpose == "preview" else "Artifact downloaded",
            context={
                "user_id": identity.user_id,
                "tenant_id": identity.tenant_id,
                "workspace_id": identity.workspace_id,
                "session_id": session.id,
                "owner_user_id": session.identity.user_id,
                "path": relative_path,
                "name": file_path.name,
                "purpose": purpose,
                "bytes": response.stat_result.st_size,
                "media_type": media_type or "application/octet-stream",
            },
        )
        return response
    except BaseException:
        file.close()
        raise


@router.post("/files/share")
async def get_oss_download_url(
    session_id: str,
    body: ShareFileRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> ShareFileResponse:
    try:
        session = await access_session(db, identity, session_id, write=True)
        settings = get_settings()
        settings.oss.require_configured()
        paths = UserAgentPaths.from_identity(
            home=settings.paths.home,
            identity=session.identity,
            agent_id=session.agent_id,
            session_id=session.id,
        )
        file_path = resolve_session_file(paths.artifact_dir, body.path)
        try:
            with open_resolved_workspace_file(paths.artifact_dir, file_path) as file:
                file_bytes = file.read(settings.oss.max_bytes + 1)
        except OSError:
            raise_api("NOT_FOUND", "workspace file not found", status=404)
        if len(file_bytes) > settings.oss.max_bytes:
            raise ValueError(f"file exceeds {settings.oss.max_bytes} bytes")
        relative = file_path.relative_to(paths.artifact_dir.resolve()).as_posix()
        store_path = f"{settings.oss.prefix.rstrip('/')}/{session.id}/{relative}"
        store = get_oss_store()
        store.upload_file(store_path, file_bytes)
        url = store.create_signed_download_url(
            store_path,
            expires_in=settings.oss.share_ttl_seconds,
            download_filename=file_path.name,
        )
        chat_id = await artifact_chat_id(db, identity, session_id, relative)
        await tracer_record_behavior(
            db,
            identity,
            session_id=session_id,
            chat_id=chat_id,
            kind="share",
            target_type="file",
            target_id=relative,
            payload={"name": file_path.name},
            score_name="share",
            value="file",
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return ShareFileResponse(
        url=url,
        expires_at=utcnow() + timedelta(seconds=settings.oss.share_ttl_seconds),
        name=file_path.name,
    )

# -*- coding: utf-8 -*-
"""Session workspace file access and HMAC-signed share links.

Share links substitute for the enterprise object-store URLs: the token
encodes ``session_id|path|expiry`` signed with ``QWENPAW_DATA_SHARE_SECRET``
(else the API token), so the resolver route works without bearer auth —
the signature is the credential.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import mimetypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from qwenpaw_data.host.core.api.auth import get_configured_api_token
from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error, raise_api
from qwenpaw_data.host.core.api.models.artifact import (
    ShareFileRequest,
    ShareFileResponse,
)
from qwenpaw_data.host.core.domain.identity import Identity

router = APIRouter(prefix="/sessions/{session_id}/files", tags=["files"])
shared_router = APIRouter(prefix="/files", tags=["files"])

SHARE_SECRET_ENV = "QWENPAW_DATA_SHARE_SECRET"
SHARE_TTL_ENV = "QWENPAW_DATA_SHARE_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 3600


def _artifact_dir(state: ServiceState, session_id: str) -> Path:
    return Path(state.hosts.get(session_id=session_id).paths.artifact_dir)


def resolve_session_file(artifact_root: Path, relative_path: str) -> Path:
    """Containment-checked lookup under the session artifact directory."""
    if (
        not relative_path
        or relative_path.startswith(("/", "~"))
        or "\\" in relative_path
        or "\x00" in relative_path
        or any(
            segment in {"", ".", ".."} for segment in relative_path.split("/")
        )
    ):
        raise_api("NOT_FOUND", "workspace file not found", status=404)
    root = artifact_root.resolve()
    # Normpath+prefix barrier; only the guarded branch may return, with a
    # symlink-safe resolve containment check on top.
    normalized = os.path.normpath(os.path.join(str(root), relative_path))
    if normalized.startswith(str(root) + os.sep):
        candidate = Path(normalized).resolve()
        if candidate != root and root in candidate.parents and candidate.is_file():
            return candidate
    raise_api("NOT_FOUND", "workspace file not found", status=404)


def _share_secret() -> str:
    secret = (os.environ.get(SHARE_SECRET_ENV) or "").strip()
    if secret:
        return secret
    return get_configured_api_token()


def _share_ttl() -> int:
    raw = (os.environ.get(SHARE_TTL_ENV) or "").strip()
    try:
        return max(60, int(raw)) if raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _sign(session_id: str, path: str, expires: int, secret: str) -> str:
    message = f"{session_id}|{path}|{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _encode_token(session_id: str, path: str, expires: int) -> str:
    raw = f"{session_id}|{path}|{expires}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_token(token: str) -> tuple[str, str, int]:
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        session_id, path, expires = raw.rsplit("|", 2)
        return session_id, path, int(expires)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise LookupError("share link is invalid") from exc


@router.get("/access", response_class=FileResponse)
async def access_workspace_file(
    session_id: str,
    path: str = Query(..., min_length=1),
    purpose: Literal["preview", "download"] = Query("preview"),
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> FileResponse:
    _ = identity
    try:
        await state.sessions.get(session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    file_path = resolve_session_file(_artifact_dir(state, session_id), path)
    media_type, _guess = mimetypes.guess_type(file_path.name)
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type or "application/octet-stream",
        content_disposition_type=(
            "inline" if purpose == "preview" else "attachment"
        ),
    )


@router.post("/share")
async def share_workspace_file(
    session_id: str,
    body: ShareFileRequest,
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    _ = identity
    try:
        await state.sessions.get(session_id)
        secret = _share_secret()
        if not secret:
            raise ValueError(
                "sharing requires QWENPAW_DATA_SHARE_SECRET or an API token"
            )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    file_path = resolve_session_file(_artifact_dir(state, session_id), body.path)
    expires = int(time.time()) + _share_ttl()
    token = _encode_token(session_id, body.path, expires)
    signature = _sign(session_id, body.path, expires, secret)
    return ShareFileResponse(
        url=f"/api/v1/files/shared/{token}?sig={signature}",
        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc),
        name=file_path.name,
    ).model_dump(mode="json")


@shared_router.get("/shared/{token}", response_class=FileResponse)
async def resolve_shared_file(
    token: str,
    sig: str = Query(..., min_length=1),
    state: ServiceState = Depends(get_state),
) -> FileResponse:
    try:
        session_id, path, expires = _decode_token(token)
    except LookupError:
        raise_api("NOT_FOUND", "share link is invalid", status=404)
    secret = _share_secret()
    if not secret or not hmac.compare_digest(
        sig, _sign(session_id, path, expires, secret)
    ):
        raise_api("NOT_FOUND", "share link is invalid", status=404)
    if time.time() > expires:
        raise_api("NOT_FOUND", "share link has expired", status=404)
    try:
        await state.sessions.get(session_id)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    file_path = resolve_session_file(_artifact_dir(state, session_id), path)
    media_type, _guess = mimetypes.guess_type(file_path.name)
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type or "application/octet-stream",
        content_disposition_type="attachment",
    )

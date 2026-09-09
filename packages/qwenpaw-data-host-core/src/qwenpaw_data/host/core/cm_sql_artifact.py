# -*- coding: utf-8 -*-
"""Copy CM execute_sql CSV into the current session artifact directory."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx

from qwenpaw_data.host.core.cm_client import (
    API_TOKEN_ENV,
    CLIENT_API_TOKEN_ENV,
    _trust_env_for_url,
    resolve_cm_base_url,
)
from qwenpaw_data.host.core.mcp_cm import is_cm_mcp_tool_name

logger = logging.getLogger(__name__)

_DOWNLOAD_PATH_RE = re.compile(r"/api/v1/cm/downloads/([A-Za-z0-9_-]+)\.csv$")


class SqlArtifactError(RuntimeError):
    """Raised when Host cannot materialize an execute_sql CSV."""

    def __init__(self, message: str, *, phase: str = "unknown") -> None:
        super().__init__(message)
        self.phase = phase


@dataclass(frozen=True, slots=True)
class SqlArtifactLogContext:
    """Identifiers correlating a materialization attempt. Never holds secrets."""

    session_id: str | None = None
    chat_id: str | None = None
    backend: str | None = None
    workspace_id: str | None = None
    sandbox_id: str | None = None


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def is_execute_sql_tool(tool_name: str, prefixes: Iterable[str]) -> bool:
    return (
        bool(tool_name)
        and is_cm_mcp_tool_name(tool_name, prefixes)
        and tool_name.rsplit("__", 1)[-1] == "execute_sql"
    )


def _resolve_token(access_token: str | None) -> str:
    if access_token is not None:
        return access_token.strip()
    return (
        (os.environ.get(CLIENT_API_TOKEN_ENV) or "").strip()
        or (os.environ.get(API_TOKEN_ENV) or "").strip()
    )


def _download_id(download_url: str) -> str:
    match = _DOWNLOAD_PATH_RE.fullmatch(urlparse(download_url).path)
    if match is None:
        raise SqlArtifactError(
            f"execute_sql download_url is not a CM CSV download: {download_url}",
            phase="parse",
        )
    return match.group(1)


def _safe_artifact_destination(
    host_artifact_dir: Path | str,
    relative_path: Path,
) -> Path:
    """Create and validate a destination beneath the host artifact root."""
    try:
        root = Path(host_artifact_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        parent = root
        for part in relative_path.parent.parts:
            candidate = parent / part
            candidate.mkdir(exist_ok=True)
            parent = candidate.resolve(strict=True)
            if not parent.is_dir() or not parent.is_relative_to(root):
                raise SqlArtifactError(
                    "unsafe execute_sql artifact destination",
                    phase="write",
                )

        destination = parent / relative_path.name
        if destination.is_symlink():
            raise SqlArtifactError(
                "unsafe execute_sql artifact destination",
                phase="write",
            )
        if not destination.resolve().is_relative_to(root):
            raise SqlArtifactError(
                "unsafe execute_sql artifact destination",
                phase="write",
            )
        return destination
    except SqlArtifactError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SqlArtifactError(
            "could not safely create execute_sql artifact destination",
            phase="write",
        ) from exc


async def materialize_execute_sql_result(
    result_text: str,
    *,
    artifact_dir: Path | str,
    model_artifact_dir: Path | str | None = None,
    access_token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    log_context: SqlArtifactLogContext | None = None,
) -> str:
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return result_text
    download_url = payload.get("download_url") if isinstance(payload, dict) else None
    if not isinstance(download_url, str) or not download_url.strip():
        return result_text

    started = time.monotonic()
    logger.info(
        "execute_sql artifact: phase=%s context=%s", "parse", log_context
    )
    token = _resolve_token(access_token)
    download_id = _download_id(download_url.strip())
    relative_path = Path("data") / "raw" / f"{download_id}.csv"
    dest = _safe_artifact_destination(artifact_dir, relative_path)
    url = f"{resolve_cm_base_url()}/api/v1/cm/downloads/{download_id}.csv"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        timeout=60.0,
        transport=transport,
        trust_env=_trust_env_for_url(url),
    ) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise SqlArtifactError(
                f"execute_sql CSV fetch failed: {exc}",
                phase="fetch",
            ) from exc
    if response.status_code >= 400:
        raise SqlArtifactError(
            f"execute_sql CSV fetch returned HTTP {response.status_code}",
            phase="fetch",
        )
    logger.info(
        "execute_sql artifact: phase=%s bytes=%d duration_ms=%d context=%s",
        "fetch",
        len(response.content),
        _elapsed_ms(started),
        log_context,
    )
    try:
        dest.write_bytes(response.content)
    except OSError as exc:
        raise SqlArtifactError(
            f"execute_sql CSV write failed: {exc}",
            phase="write",
        ) from exc
    logger.info(
        "execute_sql artifact: phase=%s duration_ms=%d context=%s",
        "write",
        _elapsed_ms(started),
        log_context,
    )
    model_dest = (
        dest.resolve()
        if model_artifact_dir is None
        else Path(model_artifact_dir) / relative_path
    )
    payload["file_path"] = str(model_dest)
    del payload["download_url"]
    return json.dumps(payload, ensure_ascii=False)

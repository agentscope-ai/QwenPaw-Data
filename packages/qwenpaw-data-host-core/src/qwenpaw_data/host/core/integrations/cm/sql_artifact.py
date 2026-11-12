# -*- coding: utf-8 -*-
"""Copy CM execute_sql CSV into the current session artifact directory."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePath, PurePosixPath
from urllib.parse import urlparse

import httpx

from qwenpaw_data.host.core.api.auth_middleware import auth_enabled
from qwenpaw_data.host.core.integrations.cm.client import (
    _trust_env_for_url,
    resolve_cm_base_url,
)
from qwenpaw_data.host.core.settings.cm import CmSettings
from qwenpaw_data.host.core.utils.workspace import model_visible_path

_DOWNLOAD_PATH_RE = re.compile(r"/api/v1/cm/downloads/([A-Za-z0-9_-]+)\.csv$")


class SqlArtifactError(RuntimeError):
    """Raised when Host cannot materialize an execute_sql CSV."""


def is_execute_sql_tool(tool_name: str) -> bool:
    return tool_name == f"mcp__{CmSettings.MCP_NAME}__execute_read"


def _download_id(download_url: str) -> str:
    match = _DOWNLOAD_PATH_RE.fullmatch(urlparse(download_url).path)
    if match is None:
        raise SqlArtifactError(
            f"execute_sql download_url is not a CM CSV download: {download_url}"
        )
    return match.group(1)


def _safe_artifact_destination(artifact_dir: Path, relative: PurePosixPath) -> Path:
    """Create ``artifact_dir/relative``'s parents; refuse symlinked escapes."""
    try:
        root = artifact_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        parent = artifact_dir
        for part in relative.parent.parts:
            parent = parent / part
            parent.mkdir(exist_ok=True)
            if not parent.resolve(strict=True).is_relative_to(root):
                raise SqlArtifactError("unsafe execute_sql artifact destination")
        destination = parent / relative.name
        if destination.is_symlink() or not destination.resolve().is_relative_to(root):
            raise SqlArtifactError("unsafe execute_sql artifact destination")
        return destination
    except SqlArtifactError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SqlArtifactError(
            "could not create execute_sql artifact destination"
        ) from exc


async def materialize_execute_sql_result(
    result_text: str,
    *,
    artifact_dir: Path | str,
    access_token: str | None,
    transport: httpx.BaseTransport | None = None,
    model_artifact_dir: PurePath | str | None = None,
) -> str:
    """Download the CSV into ``artifact_dir/data/raw`` and point the payload at it.

    ``file_path`` is what the model sees: ``model_artifact_dir`` (the session
    artifact dir in the executor's namespace) joined with the session-relative
    path. Without it the unresolved host path under ``artifact_dir`` is
    returned for the caller to re-root; it is never symlink-resolved.
    """
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return result_text
    download_url = payload.get("download_url") if isinstance(payload, dict) else None
    if not isinstance(download_url, str) or not download_url.strip():
        return result_text

    token = (access_token or "").strip()
    if auth_enabled() and not token:
        raise SqlArtifactError(
            "access_token is required to materialize execute_sql CSV when auth is enabled"
        )

    download_id = _download_id(download_url.strip())
    relative = PurePosixPath("data", "raw", f"{download_id}.csv")
    dest = _safe_artifact_destination(Path(artifact_dir), relative)
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
            raise SqlArtifactError(f"execute_sql CSV fetch failed: {exc}") from exc
    if response.status_code >= 400:
        raise SqlArtifactError(
            f"execute_sql CSV fetch returned HTTP {response.status_code}"
        )
    try:
        dest.write_bytes(response.content)
    except OSError as exc:
        raise SqlArtifactError("execute_sql CSV write failed") from exc
    if model_artifact_dir is None:
        payload["file_path"] = str(dest)
    else:
        payload["file_path"] = str(
            model_visible_path(model_artifact_dir).joinpath(*relative.parts)
        )
    del payload["download_url"]
    return json.dumps(payload, ensure_ascii=False)

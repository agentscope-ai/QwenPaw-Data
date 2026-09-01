# -*- coding: utf-8 -*-
"""Copy CM execute_sql CSV into the current session artifact directory."""
from __future__ import annotations

import json
import os
import re
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

_DOWNLOAD_PATH_RE = re.compile(r"/api/v1/cm/downloads/([A-Za-z0-9_-]+)\.csv$")


class SqlArtifactError(RuntimeError):
    """Raised when Host cannot materialize an execute_sql CSV."""


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
            f"execute_sql download_url is not a CM CSV download: {download_url}"
        )
    return match.group(1)


async def materialize_execute_sql_result(
    result_text: str,
    *,
    artifact_dir: Path | str,
    access_token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return result_text
    download_url = payload.get("download_url") if isinstance(payload, dict) else None
    if not isinstance(download_url, str) or not download_url.strip():
        return result_text

    token = _resolve_token(access_token)
    download_id = _download_id(download_url.strip())
    dest = Path(artifact_dir) / "data" / "raw" / f"{download_id}.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
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
    dest.write_bytes(response.content)
    payload["file_path"] = str(dest.resolve())
    del payload["download_url"]
    return json.dumps(payload, ensure_ascii=False)

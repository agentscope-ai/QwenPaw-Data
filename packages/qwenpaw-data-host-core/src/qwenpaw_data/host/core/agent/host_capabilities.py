# -*- coding: utf-8 -*-
"""Task-scoped tools and skills imported from the QwenPaw Host."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import frontmatter
import httpx
from agentscope.message import TextBlock, ToolResultState
from agentscope.skill import Skill
from agentscope.tool import FunctionTool, ToolChunk

_MAX_SKILL_BYTES = 2 * 1024 * 1024
_MAX_SKILL_FILES = 1024
_VALID_STATES = {item.value: item for item in ToolResultState}
_MATERIALIZE_LOCK = threading.Lock()


class HostCapabilityError(RuntimeError):
    """A stable protocol or authorization failure from the Host bridge."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class HostCapabilityClient:
    """Consume the narrow, task-bound capability protocol exposed by Host."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        storage_workspace_dir: str | Path,
        runtime_workspace_dir: str | Path,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config.get("protocol_version") != 1:
            raise HostCapabilityError("unsupported_host_capability_protocol")
        endpoint = config.get("endpoint")
        token = config.get("token")
        if not isinstance(endpoint, str) or not isinstance(token, str):
            raise HostCapabilityError("invalid_host_capability_bridge")
        try:
            url = httpx.URL(endpoint)
        except (TypeError, ValueError, httpx.InvalidURL):
            raise HostCapabilityError("invalid_host_capability_bridge") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or not token
        ):
            raise HostCapabilityError("invalid_host_capability_bridge")
        self._storage_root = Path(storage_workspace_dir) / ".pawapp-capabilities"
        self._runtime_root = Path(runtime_workspace_dir) / ".pawapp-capabilities"
        self._client = httpx.AsyncClient(
            base_url=str(url).rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=httpx.Timeout(60.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._catalog: list[dict[str, Any]] | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def catalog(self) -> list[dict[str, Any]]:
        if self._catalog is not None:
            return list(self._catalog)
        payload = await self._request("GET", "catalog")
        if payload.get("protocol_version") != 1:
            raise HostCapabilityError("unsupported_host_capability_protocol")
        entries = payload.get("capabilities")
        if not isinstance(entries, list):
            raise HostCapabilityError("invalid_host_capability_response")
        parsed = [self._descriptor(item) for item in entries]
        ids = [item["capability_id"] for item in parsed]
        tool_names = [item["wire_name"] for item in parsed if item["kind"] == "tool"]
        skill_names = [item["wire_name"] for item in parsed if item["kind"] == "skill"]
        if (
            len(ids) != len(set(ids))
            or len(tool_names) != len(set(tool_names))
            or len(skill_names) != len(set(skill_names))
        ):
            raise HostCapabilityError("invalid_host_capability_response")
        self._catalog = parsed
        return list(parsed)

    async def build_tools(self) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        for descriptor in await self.catalog():
            if descriptor["kind"] != "tool":
                continue
            tools.append(
                FunctionTool(
                    self._remote_tool(descriptor),
                    name=descriptor["wire_name"],
                    description=descriptor["description"],
                    input_schema=descriptor["input_schema"],
                    is_read_only=descriptor["is_read_only"],
                ),
            )
        return tools

    async def load_skills(self) -> list[Skill]:
        skills: list[Skill] = []
        for descriptor in await self.catalog():
            if descriptor["kind"] != "skill":
                continue
            if not descriptor["available"]:
                raise HostCapabilityError(
                    descriptor["blocked_reason"] or "skill_dependency_unavailable",
                )
            capability_id = quote(descriptor["capability_id"], safe="")
            payload = await self._request("GET", f"skills/{capability_id}")
            if payload.get("protocol_version") != 1:
                raise HostCapabilityError(
                    "unsupported_host_capability_protocol",
                )
            files = payload.get("files")
            returned_descriptor = self._descriptor(payload.get("descriptor"))
            if returned_descriptor != descriptor:
                raise HostCapabilityError("invalid_host_capability_response")
            if not isinstance(files, list):
                raise HostCapabilityError("invalid_host_capability_response")
            directory = await self._materialize(descriptor, files)
            try:
                skill_text = (directory / "SKILL.md").read_text(encoding="utf-8")
                content = frontmatter.loads(skill_text)
            except (OSError, UnicodeDecodeError, ValueError):
                raise HostCapabilityError("invalid_host_skill") from None
            if not content.get("name") or not content.get("description"):
                raise HostCapabilityError("invalid_host_skill")
            runtime_dir = self._runtime_root / descriptor["digest"]
            skills.append(
                Skill(
                    name=descriptor["wire_name"],
                    description=descriptor["description"],
                    dir=str(runtime_dir),
                    markdown=content.content,
                    updated_at=(directory / "SKILL.md").stat().st_mtime,
                ),
            )
        return skills

    def _remote_tool(self, descriptor: dict[str, Any]):
        async def invoke(**kwargs: Any) -> ToolChunk:
            capability_id = quote(descriptor["capability_id"], safe="")
            try:
                payload = await self._request(
                    "POST",
                    f"tools/{capability_id}/invoke",
                    json={"params": kwargs},
                )
            except HostCapabilityError as exc:
                denied = exc.code in {
                    "capability_not_found",
                    "invalid_capability_scope",
                    "invalid_capability_token",
                    "tool_permission_denied",
                }
                return ToolChunk(
                    content=[TextBlock(text=f"HostCapabilityError: {exc.code}")],
                    state=(ToolResultState.DENIED if denied else ToolResultState.ERROR),
                )
            if (
                payload.get("protocol_version") != 1
                or payload.get("state") != "success"
            ):
                return ToolChunk(
                    content=[
                        TextBlock(
                            text="HostCapabilityError: invalid_host_capability_response"
                        ),
                    ],
                    state=ToolResultState.ERROR,
                )
            return self._as_tool_chunk(payload.get("output"))

        return invoke

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TransportError:
            raise HostCapabilityError("host_capability_unavailable") from None
        try:
            payload = response.json()
        except ValueError:
            raise HostCapabilityError("invalid_host_capability_response") from None
        if not isinstance(payload, dict):
            raise HostCapabilityError("invalid_host_capability_response")
        if not response.is_success:
            detail = payload.get("detail")
            code = detail if isinstance(detail, str) else "host_capability_rejected"
            raise HostCapabilityError(code)
        return payload

    @staticmethod
    def _descriptor(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise HostCapabilityError("invalid_host_capability_response")
        required_strings = ("capability_id", "name", "wire_name", "scope", "digest")
        if any(
            not isinstance(value.get(key), str) or not value[key]
            for key in required_strings
        ):
            raise HostCapabilityError("invalid_host_capability_response")
        kind = value.get("kind")
        scope = value.get("scope")
        if kind not in {"tool", "skill"}:
            raise HostCapabilityError("invalid_host_capability_response")
        if scope not in {"host_public", "app_private"}:
            raise HostCapabilityError("invalid_host_capability_response")
        expected_segment = "tool" if kind == "tool" else "skill"
        if not value["capability_id"].startswith(
            (f"host/{expected_segment}/", f"app/{expected_segment}/"),
        ):
            raise HostCapabilityError("invalid_host_capability_response")
        description = value.get("description", "")
        available = value.get("available", True)
        blocked_reason = value.get("blocked_reason")
        is_read_only = value.get("is_read_only", False)
        if (
            not isinstance(description, str)
            or not isinstance(available, bool)
            or not isinstance(is_read_only, bool)
            or (blocked_reason is not None and not isinstance(blocked_reason, str))
        ):
            raise HostCapabilityError("invalid_host_capability_response")
        schema = value.get("input_schema")
        if kind == "tool" and not isinstance(schema, dict):
            raise HostCapabilityError("invalid_host_capability_response")
        digest = value["digest"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise HostCapabilityError("invalid_host_capability_response")
        return {
            "capability_id": value["capability_id"],
            "name": value["name"],
            "wire_name": value["wire_name"],
            "kind": kind,
            "scope": scope,
            "description": description,
            "input_schema": schema,
            "digest": digest,
            "available": available,
            "blocked_reason": blocked_reason,
            "is_read_only": is_read_only,
        }

    async def _materialize(
        self,
        descriptor: dict[str, Any],
        files: list[Any],
    ) -> Path:
        return await asyncio.to_thread(
            self._materialize_sync,
            descriptor,
            files,
        )

    def _materialize_sync(
        self,
        descriptor: dict[str, Any],
        files: list[Any],
    ) -> Path:
        with _MATERIALIZE_LOCK:
            if len(files) > _MAX_SKILL_FILES:
                raise HostCapabilityError("host_skill_too_large")
            if self._storage_root.is_symlink():
                raise HostCapabilityError("invalid_host_skill_path")
            self._storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self._storage_root / descriptor["digest"]
            temp = Path(tempfile.mkdtemp(prefix=".skill-", dir=self._storage_root))
            total = 0
            seen: set[PurePosixPath] = set()
            try:
                for entry in files:
                    if not isinstance(entry, dict):
                        raise HostCapabilityError("invalid_host_skill")
                    raw_path = entry.get("path")
                    encoding = entry.get("encoding")
                    raw_content = entry.get("content")
                    if not isinstance(raw_path, str) or not isinstance(
                        raw_content,
                        str,
                    ):
                        raise HostCapabilityError("invalid_host_skill")
                    relative = PurePosixPath(raw_path)
                    if (
                        not raw_path
                        or "\\" in raw_path
                        or relative.is_absolute()
                        or relative in seen
                        or any(part in {"", ".", ".."} for part in relative.parts)
                    ):
                        raise HostCapabilityError("invalid_host_skill_path")
                    seen.add(relative)
                    if encoding == "utf-8":
                        data = raw_content.encode("utf-8")
                    elif encoding == "base64":
                        try:
                            data = base64.b64decode(raw_content, validate=True)
                        except (ValueError, binascii.Error):
                            raise HostCapabilityError("invalid_host_skill") from None
                    else:
                        raise HostCapabilityError("invalid_host_skill")
                    total += len(data)
                    if total > _MAX_SKILL_BYTES:
                        raise HostCapabilityError("host_skill_too_large")
                    destination = temp.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(
                        destination,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                if not (temp / "SKILL.md").is_file():
                    raise HostCapabilityError("invalid_host_skill")
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target)
                os.replace(temp, target)
                return target
            except HostCapabilityError:
                shutil.rmtree(temp, ignore_errors=True)
                raise
            except OSError:
                shutil.rmtree(temp, ignore_errors=True)
                raise HostCapabilityError(
                    "host_skill_materialization_failed",
                ) from None

    @staticmethod
    def _as_tool_chunk(output: Any) -> ToolChunk:
        state = ToolResultState.SUCCESS
        blocks: list[str] = []
        chunks = output if isinstance(output, list) else [output]
        for chunk in chunks:
            if isinstance(chunk, dict) and isinstance(chunk.get("content"), list):
                raw_state = chunk.get("state")
                if isinstance(raw_state, str) and raw_state in _VALID_STATES:
                    state = _VALID_STATES[raw_state]
                for block in chunk["content"]:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        blocks.append(block["text"])
                    else:
                        blocks.append(json.dumps(block, ensure_ascii=False))
            elif isinstance(chunk, str):
                blocks.append(chunk)
            else:
                blocks.append(json.dumps(chunk, ensure_ascii=False))
        return ToolChunk(
            content=[TextBlock(text="\n".join(blocks))],
            state=state,
        )


__all__ = ["HostCapabilityClient", "HostCapabilityError"]

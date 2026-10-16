from __future__ import annotations

import json

import httpx
import pytest
from agentscope.message import ToolResultState

from qwenpaw_data.host.core.agent.host_capabilities import (
    HostCapabilityClient,
    HostCapabilityError,
)


def descriptor(
    capability_id: str,
    *,
    kind: str,
    wire_name: str,
    digest: str,
    available: bool = True,
    blocked_reason: str | None = None,
) -> dict:
    return {
        "capability_id": capability_id,
        "name": capability_id.rsplit("/", 1)[-1],
        "wire_name": wire_name,
        "kind": kind,
        "scope": "app_private",
        "description": f"{wire_name} description",
        "input_schema": (
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            }
            if kind == "tool"
            else None
        ),
        "tool_refs": [],
        "is_read_only": kind == "tool",
        "digest": digest,
        "available": available,
        "blocked_reason": blocked_reason,
    }


def client(tmp_path, handler) -> HostCapabilityClient:
    return HostCapabilityClient(
        {
            "protocol_version": 1,
            "endpoint": "http://host.test/api/pawapp-capabilities",
            "token": "scoped-token",
        },
        storage_workspace_dir=tmp_path,
        runtime_workspace_dir="/workspace",
        transport=httpx.MockTransport(handler),
    )


async def test_remote_tool_and_skill_are_loaded_with_scoped_auth(tmp_path) -> None:
    calls: list[tuple[str, str]] = []
    tool = descriptor(
        "app/tool/echo",
        kind="tool",
        wire_name="app__echo",
        digest="a" * 64,
    )
    skill = descriptor(
        "app/skill/guidance",
        kind="skill",
        wire_name="guidance",
        digest="b" * 64,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer scoped-token"
        calls.append((request.method, request.url.raw_path.decode()))
        if request.url.path.endswith("/catalog"):
            return httpx.Response(
                200,
                json={"protocol_version": 1, "capabilities": [tool, skill]},
            )
        if "/tools/" in request.url.path:
            assert json.loads(request.content) == {"params": {"text": "hello"}}
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "state": "success",
                    "output": {
                        "content": [{"type": "text", "text": "echo: hello"}],
                        "state": "success",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "descriptor": skill,
                "files": [
                    {
                        "path": "SKILL.md",
                        "encoding": "utf-8",
                        "content": (
                            "---\nname: guidance\n"
                            "description: Use the echo tool\n---\n"
                            "Call app__echo.\n"
                        ),
                    },
                    {
                        "path": "references/example.txt",
                        "encoding": "utf-8",
                        "content": "example",
                    },
                ],
            },
        )

    bridge = client(tmp_path, handler)
    try:
        tools = await bridge.build_tools()
        skills = await bridge.load_skills()
        result = await tools[0](text="hello")
    finally:
        await bridge.aclose()

    assert tools[0].name == "app__echo"
    assert tools[0].is_read_only is True
    assert result.state is ToolResultState.SUCCESS
    assert result.content[0].text == "echo: hello"
    assert skills[0].name == "guidance"
    assert skills[0].dir == f"/workspace/.pawapp-capabilities/{'b' * 64}"
    materialized = tmp_path / ".pawapp-capabilities" / ("b" * 64)
    assert (materialized / "references/example.txt").read_text() == "example"
    assert sum(path.endswith("/catalog") for _, path in calls) == 1
    assert any("app%2Ftool%2Fecho" in path for _, path in calls)


async def test_remote_denial_becomes_denied_tool_result(tmp_path) -> None:
    tool = descriptor(
        "host/tool/bash",
        kind="tool",
        wire_name="bash",
        digest="c" * 64,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/catalog"):
            return httpx.Response(
                200,
                json={"protocol_version": 1, "capabilities": [tool]},
            )
        return httpx.Response(403, json={"detail": "tool_permission_denied"})

    bridge = client(tmp_path, handler)
    try:
        remote = (await bridge.build_tools())[0]
        result = await remote(text="unsafe")
    finally:
        await bridge.aclose()

    assert result.state is ToolResultState.DENIED
    assert result.content[0].text == "HostCapabilityError: tool_permission_denied"


async def test_skill_materialization_rejects_path_traversal(tmp_path) -> None:
    skill = descriptor(
        "host/skill/guidance",
        kind="skill",
        wire_name="guidance",
        digest="d" * 64,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/catalog"):
            return httpx.Response(
                200,
                json={"protocol_version": 1, "capabilities": [skill]},
            )
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "descriptor": skill,
                "files": [
                    {
                        "path": "../outside.txt",
                        "encoding": "utf-8",
                        "content": "escape",
                    },
                ],
            },
        )

    bridge = client(tmp_path, handler)
    try:
        with pytest.raises(HostCapabilityError, match="invalid_host_skill_path"):
            await bridge.load_skills()
    finally:
        await bridge.aclose()
    assert not (tmp_path / "outside.txt").exists()


async def test_unavailable_skill_dependency_fails_before_loading(tmp_path) -> None:
    skill = descriptor(
        "host/skill/guidance",
        kind="skill",
        wire_name="guidance",
        digest="e" * 64,
        available=False,
        blocked_reason="skill_dependency_unavailable",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/catalog")
        return httpx.Response(
            200,
            json={"protocol_version": 1, "capabilities": [skill]},
        )

    bridge = client(tmp_path, handler)
    try:
        with pytest.raises(
            HostCapabilityError,
            match="skill_dependency_unavailable",
        ):
            await bridge.load_skills()
    finally:
        await bridge.aclose()

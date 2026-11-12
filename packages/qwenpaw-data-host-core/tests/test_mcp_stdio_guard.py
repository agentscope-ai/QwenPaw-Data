# -*- coding: utf-8 -*-
"""STDIO MCP must never execute in the host process.

A user-registered stdio MCP is a configured command; connecting it spawns
that command wherever ``connect()`` runs. ``McpRepository.list_tools`` runs
in the host process, so before this guard any logged-in user could achieve
remote code execution on the tenant pod with a plain MCP registration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agentscope.mcp import MCPClient
from fastapi import HTTPException

from qwenpaw_data.host.core.api.models.mcp import McpWriteRequest
from qwenpaw_data.host.core.api.routers.mcp import _from_write
from qwenpaw_data.host.core.domain.mcp import Mcp, McpNotFoundError
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.utils.mcp import write_mcp_manifest


def _stdio(name: str, *, source: str = "custom") -> Mcp:
    return Mcp(
        client=MCPClient(
            name=name,
            is_stateful=True,
            mcp_config={"type": "stdio_mcp", "command": "/bin/sh", "args": []},
        ),
        enabled=True,
        source=source,
        description="",
    )


def _stdio_record(source: str) -> dict:
    return {
        "is_stateful": True,
        "mcp_config": {"type": "stdio_mcp", "command": "/bin/sh", "args": []},
        "enable_tools": None,
        "disable_tools": None,
        "execution_timeout": None,
        "enabled": True,
        "source": source,
        "description": "",
    }


def _deny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_MCP_ALLOW_CUSTOM_STDIO", raising=False)
    clear_settings()


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_MCP_ALLOW_CUSTOM_STDIO", "1")
    clear_settings()


def test_stdio_rejected_on_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deny(monkeypatch)
    repo = McpRepository(tmp_path / "mcp.json")
    with pytest.raises(ValueError, match="STDIO MCP is not allowed"):
        repo.create(_stdio("probe"))
    assert "probe" not in {mcp.name for mcp in repo.list()}


def test_stdio_dropped_from_untrusted_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The manifest sits in the user-writable workspace, so both the record
    # and its claimed source are attacker-controlled; neither may matter.
    _deny(monkeypatch)
    write_mcp_manifest(
        tmp_path / "mcp.json",
        {
            "probe": _stdio_record("custom"),
            "cm-lookalike": _stdio_record("builtin"),
        },
    )
    repo = McpRepository(tmp_path / "mcp.json")
    names = {mcp.name for mcp in repo.list()}
    assert "probe" not in names
    assert "cm-lookalike" not in names
    with pytest.raises(McpNotFoundError):
        repo.require("probe")


def test_stdio_allowed_when_opted_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow(monkeypatch)
    repo = McpRepository(tmp_path / "mcp.json")
    repo.create(_stdio("probe"))
    assert "probe" in {mcp.name for mcp in repo.list()}


def test_from_write_rejects_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deny(monkeypatch)
    body = McpWriteRequest(
        name="probe",
        is_stateful=True,
        enabled=False,
        mcp_config={"type": "stdio_mcp", "command": "/bin/sh", "args": []},
    )
    for source in ("custom", "builtin"):
        with pytest.raises(HTTPException) as exc_info:
            _from_write(body, source=source)
        assert exc_info.value.status_code == 400


async def test_list_tools_never_connects_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deny(monkeypatch)
    repo = McpRepository(tmp_path / "mcp.json")
    # Bypass the manifest filter to exercise the connect-time guard itself.
    monkeypatch.setattr(repo, "require", lambda name: _stdio(name))
    with pytest.raises(ValueError, match="STDIO MCP must not connect"):
        await repo.list_tools("probe")

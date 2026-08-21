# -*- coding: utf-8 -*-
"""Workspace 工具的安全行为测试：超时子进程回收与文件路径 containment。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from qwenpaw_data.host.core.tools.workspace import (
    WorkspaceBash,
    WorkspaceEdit,
    WorkspaceGlob,
    WorkspaceGrep,
    WorkspaceRead,
    WorkspaceWrite,
)


async def _collect_bash(tool: WorkspaceBash, **kwargs):
    chunks = []
    async for chunk in tool(**kwargs):
        chunks.append(chunk)
    return chunks


@pytest.mark.skipif(
    os.name == "nt", reason="native Windows workspace uses AgentScope PowerShell"
)
async def test_bash_timeout_terminates_process_group(tmp_path: Path) -> None:
    """超时后整个进程组应被终止回收，不留后台残余。"""
    tool = WorkspaceBash(tmp_path)
    pid_file = tmp_path / "child.pid"
    # 后台再派生一个孙进程并写下 PID，验证整组回收
    command = f'echo $$ > "{pid_file}"; sleep 30'

    chunks = await _collect_bash(
        tool,
        command=command,
        timeout=1000,
    )
    assert chunks[-1].state == "error"
    assert "timed out" in chunks[-1].content[0].text

    # 等待一小段时间让 TERM/KILL 生效
    await asyncio.sleep(0.5)
    child_pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # 进程应已不存在


@pytest.mark.skipif(
    os.name == "nt", reason="native Windows workspace uses AgentScope PowerShell"
)
async def test_bash_normal_command_still_works(tmp_path: Path) -> None:
    tool = WorkspaceBash(tmp_path)
    chunks = await _collect_bash(tool, command="echo hello", timeout=10000)
    assert chunks[-1].state == "running"
    assert "hello" in chunks[-1].content[0].text


@pytest.mark.skipif(
    os.name == "nt", reason="native Windows workspace uses AgentScope PowerShell"
)
async def test_bash_cancellation_terminates_process_group(
    tmp_path: Path,
) -> None:
    """调用方取消工具时也必须回收 shell 及其子进程。"""
    tool = WorkspaceBash(tmp_path)
    pid_file = tmp_path / "cancelled.pid"
    task = asyncio.create_task(
        _collect_bash(
            tool,
            command=f'echo $$ > "{pid_file}"; sleep 30',
            timeout=30000,
        ),
    )

    async with asyncio.timeout(5):
        while not pid_file.exists():
            await asyncio.sleep(0.05)

    child_pid = int(pid_file.read_text().strip())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


async def test_write_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workdir = tmp_path / "ws"
    workdir.mkdir()
    outside = tmp_path / "outside.txt"
    tool = WorkspaceWrite(workdir)

    result = await tool(file_path=str(outside), content="nope")
    assert result.state == "error"
    assert "Access denied" in result.content[0].text
    assert not outside.exists()


async def test_write_rejects_dotdot_traversal(tmp_path: Path) -> None:
    workdir = tmp_path / "ws"
    workdir.mkdir()
    tool = WorkspaceWrite(workdir)

    sneaky = str(workdir / ".." / "escape.txt")
    result = await tool(file_path=sneaky, content="nope")
    assert result.state == "error"
    assert not (tmp_path / "escape.txt").exists()


async def test_write_and_read_inside_workspace(tmp_path: Path) -> None:
    workdir = tmp_path / "ws"
    workdir.mkdir()
    target = workdir / "note.txt"

    write_tool = WorkspaceWrite(workdir)
    result = await write_tool(file_path=str(target), content="hello ws")
    assert result.state != "error"
    assert target.read_text() == "hello ws"

    read_tool = WorkspaceRead(workdir)
    result = await read_tool(file_path=str(target))
    assert result.state != "error"
    assert "hello ws" in result.content[0].text


async def test_read_allows_extra_roots(tmp_path: Path) -> None:
    """技能目录等额外只读根应可被 Read 访问。"""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_md = skills / "SKILL.md"
    skill_md.write_text("# demo skill")

    tool = WorkspaceRead(workdir, extra_roots=[str(skills)])
    result = await tool(file_path=str(skill_md))
    assert result.state != "error"
    assert "demo skill" in result.content[0].text

    # 额外根之外仍拒绝
    other = tmp_path / "secret.txt"
    other.write_text("secret")
    result = await tool(file_path=str(other))
    assert result.state == "error"


async def test_edit_rejects_outside_workspace(tmp_path: Path) -> None:
    workdir = tmp_path / "ws"
    workdir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original")

    tool = WorkspaceEdit(workdir)
    result = await tool(
        file_path=str(outside), old_string="original", new_string="hacked"
    )
    assert result.state == "error"
    assert outside.read_text() == "original"


async def test_read_relative_path_resolved_against_workspace(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "ws"
    workdir.mkdir()
    (workdir / "rel.txt").write_text("relative ok")

    tool = WorkspaceRead(workdir)
    result = await tool(file_path="rel.txt")
    assert result.state != "error"
    assert "relative ok" in result.content[0].text


@pytest.mark.parametrize("tool_cls", [WorkspaceGlob, WorkspaceGrep])
async def test_search_tools_reject_explicit_path_outside_workspace(
    tmp_path: Path,
    tool_cls,
) -> None:
    workdir = tmp_path / "ws"
    workdir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")

    tool = tool_cls(workdir)
    result = await tool(pattern="secret", path=str(outside))

    assert result.state == "error"
    assert "Access denied" in result.content[0].text


@pytest.mark.parametrize("tool_cls", [WorkspaceGlob, WorkspaceGrep])
async def test_search_tools_reject_symlink_escape(
    tmp_path: Path,
    tool_cls,
) -> None:
    workdir = tmp_path / "ws"
    workdir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = workdir / "escape"
    try:
        escape.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    tool = tool_cls(workdir)
    result = await tool(pattern="*", path=str(escape))

    assert result.state == "error"
    assert "Access denied" in result.content[0].text

"""Current Docker boundary: cleanup cannot be skipped by stop requests."""

import asyncio
import pathlib
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import Bash, ToolChunk

from qwenpaw_data.host.core.workspace.docker_workspace import ManagedDockerBash, _SUPERVISOR


def test_supervisor_publishes_pid_only_after_complete_write(tmp_path, monkeypatch):
    record = tmp_path / "exec.pid"
    opened, release = threading.Event(), threading.Event()
    def delayed(path, text, **kwargs):
        with path.open("w", encoding=kwargs["encoding"]) as stream:
            opened.set()
            assert release.wait(3)
            stream.write(text)

    monkeypatch.setattr(pathlib.Path, "write_text", delayed)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=42, wait=lambda: 0))
    monkeypatch.setattr("sys.argv", ["supervisor", str(record), "command"])
    def run():
        with pytest.raises(SystemExit) as result:
            exec(_SUPERVISOR, {})
        assert result.value.code == 0

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert opened.wait(3)
        assert not record.exists()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert record.read_text() == "42"


@pytest.mark.parametrize("transport_error", [False, True])
async def test_failed_cleanup_retires_workspace(transport_error):
    delegate = Bash()

    async def broken(**kwargs):
        if transport_error:
            raise OSError("Docker disconnected")
        yield ToolChunk(content=[TextBlock(text="failed")], state=ToolResultState.ERROR)

    delegate.call = broken
    close = AsyncMock()
    tool = ManagedDockerBash(delegate, cleanup_failure_handler=close)
    await tool._cleanup("/tmp/test-record", interrupted=True)
    close.assert_awaited_once()


async def test_repeated_cancellation_waits_for_cleanup_and_preserves_permissions():
    started, finish = asyncio.Event(), asyncio.Event()
    delegate = Bash()
    delegate.check_permissions = AsyncMock(return_value="original decision")

    async def delayed(**kwargs):
        started.set()
        await finish.wait()
        yield ToolChunk(content=[TextBlock(text="")])

    delegate.call = delayed
    close = AsyncMock()
    tool = ManagedDockerBash(delegate, cleanup_failure_handler=close)
    task = asyncio.create_task(tool._cleanup("/tmp/test-record", interrupted=True))
    await started.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    close.assert_not_awaited()
    original = {"command": "ls"}
    assert await tool.check_permissions(original, "context") == "original decision"
    delegate.check_permissions.assert_awaited_once_with(original, "context")

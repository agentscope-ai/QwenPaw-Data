from __future__ import annotations

import json

from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import BackendBase, ExecResult, Read, Toolkit, Write


class MemoryBackend(BackendBase):
    def __init__(self) -> None:
        self.files = {
            "/workspace/empty.txt": b"",
            "/workspace/unread.txt": b"",
        }
        self.mtimes = {path: 1.0 for path in self.files}

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    async def read_file(self, path: str) -> bytes:
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data
        self.mtimes[path] = self.mtimes.get(path, 0.0) + 1.0

    async def file_exists(self, path: str) -> bool:
        return path in self.files

    async def is_dir(self, path: str) -> bool:
        return False

    async def stat_mtime(self, path: str) -> float | None:
        return self.mtimes.get(path)


async def call_tool(toolkit, state, name, arguments):
    tool_call = ToolCallBlock(
        id=f"call-{name.lower()}",
        name=name,
        input=json.dumps(arguments),
    )
    return [item async for item in toolkit.call_tool(tool_call, state)][-1]


async def test_existing_empty_backend_file_can_be_written_after_read() -> None:
    backend = MemoryBackend()
    toolkit = Toolkit(tools=[Read(backend=backend), Write(backend=backend)])
    state = AgentState()

    read_response = await call_tool(
        toolkit,
        state,
        "Read",
        {"file_path": "/workspace/empty.txt"},
    )
    write_response = await call_tool(
        toolkit,
        state,
        "Write",
        {
            "file_path": "/workspace/empty.txt",
            "content": "updated\n",
        },
    )

    assert read_response.state == ToolResultState.SUCCESS
    assert write_response.state == ToolResultState.SUCCESS
    assert backend.files["/workspace/empty.txt"] == b"updated\n"


async def test_existing_backend_file_still_requires_read_before_write() -> None:
    backend = MemoryBackend()
    toolkit = Toolkit(tools=[Read(backend=backend), Write(backend=backend)])

    response = await call_tool(
        toolkit,
        AgentState(),
        "Write",
        {
            "file_path": "/workspace/unread.txt",
            "content": "updated\n",
        },
    )

    assert response.state == ToolResultState.ERROR
    assert "has not been read yet" in response.content[0].text
    assert backend.files["/workspace/unread.txt"] == b""

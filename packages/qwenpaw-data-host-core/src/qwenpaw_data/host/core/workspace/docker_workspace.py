"""Docker execution safeguards owned by the workspace, shared by all builders."""

from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import Bash, ToolChunk
from agentscope.workspace import DockerWorkspace

logger = logging.getLogger(__name__)

# Keep the process-group record until the host acknowledges completion. The
# shell may exit before descendants finish, so its PID alone is not liveness.
_SUPERVISOR = """
import pathlib, subprocess, sys
process = subprocess.Popen(sys.argv[2:], start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(str(process.pid), encoding='ascii')
raise SystemExit(process.wait())
""".strip()

_REAPER = """
import os, pathlib, signal, sys, time
record = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 0.75
while not record.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
# Unknown startup state must fail closed: an exec may still be starting.
if not record.exists():
    raise SystemExit(2)
pgid = int(record.read_text(encoding='ascii'))
def alive():
    for path in pathlib.Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in ('Z', 'X'):
                return True
        except FileNotFoundError:
            pass
    return False
for sig, wait in ((signal.SIGTERM, float(sys.argv[2])), (signal.SIGKILL, 1.0)):
    if not alive():
        break
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + wait
    while alive() and time.monotonic() < deadline:
        time.sleep(0.02)
if alive():
    raise SystemExit(3)
record.unlink(missing_ok=True)
""".strip()


class ManagedDockerBash(Bash):
    """Reap interrupted shell process groups; retire the container on doubt.

    AgentScope's Docker backend stops waiting on timeout/cancellation without
    terminating the exec. This adapter preserves the Bash schema and delegates
    permission checks on the *original* command, not the supervisor script.
    It is lifecycle cleanup, not a security boundary against adversarial code
    that deliberately escapes its process group.
    """

    def __init__(
        self,
        delegate: Bash,
        *,
        cleanup_failure_handler: Callable[[], Awaitable[None]],
        grace_seconds: float = 3.0,
    ) -> None:
        super().__init__()
        self._delegate = delegate
        self._cleanup_failure_handler = cleanup_failure_handler
        self._grace_seconds = grace_seconds

    async def check_permissions(self, tool_input, context):
        return await self._delegate.check_permissions(tool_input, context)

    async def check_read_only(self, tool_input):
        return await self._delegate.check_read_only(tool_input)

    async def call(
        self,
        command: str,
        description: str = "",
        timeout: int = 120000,
    ) -> AsyncGenerator[ToolChunk, None]:
        record = f"/tmp/qwenpaw-data-exec-{uuid.uuid4().hex}.pid"
        supervised = shlex.join(
            ["python", "-c", _SUPERVISOR, record, "/bin/sh", "-c", command],
        )
        finished = False
        try:
            async for chunk in self._delegate.call(
                command=supervised,
                description=description,
                timeout=timeout,
            ):
                if chunk.state == ToolResultState.ERROR and any(
                    "Command timed out after" in getattr(block, "text", "")
                    for block in chunk.content
                ):
                    finished = True
                    await self._cleanup(record, interrupted=True)
                    yield ToolChunk(
                        content=[
                            TextBlock(
                                text=(
                                    f"Command timed out after {min(timeout, 600000)}ms: {command}"
                                )
                            )
                        ],
                        state=ToolResultState.ERROR,
                        is_last=True,
                    )
                    return
                # Do not expose internal supervisor code in command errors.
                if chunk.state == ToolResultState.ERROR:
                    chunk = chunk.model_copy(
                        update={
                            "content": [
                                block.model_copy(
                                    update={
                                        "text": block.text.replace(supervised, command)
                                    }
                                )
                                if isinstance(block, TextBlock)
                                else block
                                for block in chunk.content
                            ]
                        }
                    )
                elif chunk.is_last and not any(
                    not isinstance(block, TextBlock) or block.text
                    for block in chunk.content
                ):
                    chunk = chunk.model_copy(
                        update={
                            "content": [
                                TextBlock(
                                    text="Command completed successfully with no stdout or stderr."
                                )
                            ]
                        }
                    )
                yield chunk
            finished = True
            await self._cleanup(record, interrupted=False)
        finally:
            # Includes cancellation, stream closure and transport exceptions.
            if not finished:
                await self._cleanup(record, interrupted=True)

    async def _cleanup(self, record: str, *, interrupted: bool) -> None:
        async def run() -> None:
            try:
                script = (
                    _REAPER
                    if interrupted
                    else (
                        "import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)"
                    )
                )
                chunks = [
                    chunk
                    async for chunk in self._delegate.call(
                        command=shlex.join(
                            [
                                "python",
                                "-c",
                                script,
                                record,
                                str(self._grace_seconds),
                            ]
                        ),
                        description="Clean up Docker command execution",
                        timeout=int((self._grace_seconds + 2) * 1000),
                    )
                ]
                if not chunks or any(c.state == ToolResultState.ERROR for c in chunks):
                    raise RuntimeError(
                        "Docker execution cleanup could not be confirmed"
                    )
            except Exception:
                logger.exception("Docker cleanup failed; retiring workspace")
                await self._cleanup_failure_handler()

        cleanup = asyncio.create_task(run())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Repeated stop requests must not interrupt fail-closed cleanup.
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()


class QwenPawDataDockerWorkspace(DockerWorkspace):
    """Expose protected execution through the standard workspace tool contract."""

    async def list_tools(self):
        return [
            ManagedDockerBash(tool, cleanup_failure_handler=self.close)
            if isinstance(tool, Bash)
            else tool
            for tool in await super().list_tools()
        ]

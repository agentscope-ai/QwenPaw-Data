"""Real-Docker regression test for interrupted command cleanup."""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid

import pytest

from datapaw.host.core.utils.workspace import ManagedDockerBash


pytestmark = pytest.mark.skipif(
    os.getenv("DATAPAW_DOCKER_E2E") != "1",
    reason="set DATAPAW_DOCKER_E2E=1 to run tests against a Docker daemon",
)


async def test_timeout_and_cancellation_remove_processes_from_container() -> None:
    import aiodocker
    from agentscope.message import ToolResultState
    from agentscope.tool import Bash
    from agentscope.workspace import DockerBackend

    client = aiodocker.Docker()
    name = f"datapaw-cleanup-test-{uuid.uuid4().hex[:10]}"
    container = await client.containers.create(
        config={
            "Image": "python:3.11-slim",
            "Cmd": ["sleep", "infinity"],
            "WorkingDir": "/tmp",
        },
        name=name,
    )
    try:
        await container.start()
        delegate = DockerBackend(container, "/tmp")
        bash = ManagedDockerBash(
            Bash(cwd="/tmp", backend=delegate),
            grace_seconds=0.2,
        )
        marker = f"datapaw-timeout-{uuid.uuid4().hex}"

        chunks = await _collect_chunks(
            bash.call(
                command=shlex.join(
                    ["python", "-c", "import time; time.sleep(60)", marker],
                ),
                timeout=200,
            ),
        )

        assert chunks[-1].state == ToolResultState.ERROR
        assert "Command timed out after 200ms" in chunks[-1].content[0].text
        await _assert_process_absent(delegate, marker)

        cancel_marker = f"datapaw-cancel-{uuid.uuid4().hex}"
        command = asyncio.create_task(
            _collect_chunks(
                bash.call(
                    command=shlex.join(
                        [
                            "python",
                            "-c",
                            "import time; time.sleep(60)",
                            cancel_marker,
                        ],
                    ),
                ),
            ),
        )
        await asyncio.sleep(0.2)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        await _assert_process_absent(delegate, cancel_marker)
    finally:
        try:
            await container.stop()
        except Exception:
            pass
        await container.delete(force=True)
        await client.close()


async def _collect_chunks(chunks):
    return [chunk async for chunk in chunks]


async def _assert_process_absent(delegate, marker: str) -> None:
    probe = await delegate.exec_shell(
        [
            "python",
            "-c",
            (
                "import os,pathlib,sys; needle=sys.argv[1].encode(); "
                "found=[]; me=os.getpid(); "
                "[(found.append(p.name) if p.name.isdigit() and "
                "int(p.name)!=me and needle in (p/'cmdline').read_bytes() "
                "else None) for p in pathlib.Path('/proc').iterdir() "
                "if (p/'cmdline').is_file()]; print(len(found))"
            ),
            marker,
        ],
        timeout=5,
    )
    assert probe.ok(), probe.stderr.decode(errors="replace")
    assert probe.stdout.strip() == b"0"

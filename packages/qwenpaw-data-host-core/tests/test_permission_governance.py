"""Permission policy regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from agentscope.event import (
    ConfirmResult,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionMode

from qwenpaw_data.host.core.permission import (
    build_permission_context,
    deny_confirmation,
    resolve_permission_mode,
)
from qwenpaw_data.host.core.agent.dag_execution import DAGExecution
from qwenpaw_data.host.core.orchestration import RuntimeStateManager


def test_library_permission_defaults_are_workspace_aware() -> None:
    assert resolve_permission_mode("docker") is PermissionMode.BYPASS
    assert resolve_permission_mode("docker", "auto") is PermissionMode.BYPASS
    assert resolve_permission_mode("local") is PermissionMode.DONT_ASK
    assert resolve_permission_mode("local", "auto") is PermissionMode.DONT_ASK


def test_explicit_permission_mode_is_preserved() -> None:
    assert (
        resolve_permission_mode("local", "accept_edits") is PermissionMode.ACCEPT_EDITS
    )


def test_permission_context_scopes_accept_edits_to_workspace(tmp_path) -> None:
    context = build_permission_context(
        mode=PermissionMode.ACCEPT_EDITS,
        workdir=tmp_path,
    )

    assert context.mode is PermissionMode.ACCEPT_EDITS
    assert list(context.working_directories) == [str(tmp_path)]
    assert context.working_directories[str(tmp_path)].source == "session"


def test_unattended_confirmation_is_denied() -> None:
    tool_call = ToolCallBlock(
        id="call-1",
        name="Bash",
        input='{"command":"curl example.com"}',
    )
    event = RequireUserConfirmEvent(reply_id="reply-1", tool_calls=[tool_call])

    result = deny_confirmation(event)

    assert result.reply_id == "reply-1"
    assert len(result.confirm_results) == 1
    assert result.confirm_results[0].confirmed is False
    assert result.confirm_results[0].tool_call is tool_call


@pytest.mark.parametrize("handler_mode", ["approve", "missing", "mismatch", "error"])
async def test_agent_resumes_after_confirmation(handler_mode) -> None:
    tool_call = ToolCallBlock(
        id="call-1",
        name="Bash",
        input='{"command":"pwd"}',
    )
    required = RequireUserConfirmEvent(
        reply_id="reply-1",
        tool_calls=[tool_call],
    )
    received = []

    async def fake_reply_stream(inputs=None):
        received.append(inputs)
        if len(received) == 1:
            yield required
        else:
            yield inputs

    async def confirm(event):
        if handler_mode == "error":
            raise RuntimeError("handler failed")
        return UserConfirmResultEvent(
            reply_id="another-reply" if handler_mode == "mismatch" else event.reply_id,
            confirm_results=[
                ConfirmResult(confirmed=True, tool_call=event.tool_calls[0]),
            ],
        )

    execution = DAGExecution(
        RuntimeStateManager(),
        mode="plan",
        session_id="test",
        workspace_dir=Path("/workspace"),
        artifact_dir=Path("/workspace/artifacts/test"),
        session_trace_writer=None,
        confirmation_handler=None if handler_mode == "missing" else confirm,
    )
    events = [
        event async for event in execution.reply_stream(fake_reply_stream, "start")
    ]

    assert events[0] is required
    assert isinstance(received[1], UserConfirmResultEvent)
    assert received[1].reply_id == "reply-1"
    assert received[1].confirm_results[0].confirmed is (handler_mode == "approve")

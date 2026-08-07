"""Permission policy regression tests."""

from __future__ import annotations

from agentscope.agent import Agent
from agentscope.event import (
    ConfirmResult,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionMode

from datapaw.host.core.permission import (
    build_permission_context,
    deny_confirmation,
    resolve_permission_mode,
)
from datapaw.host.core.agent.datapaw_agent import DataPawAgent


def test_library_permission_defaults_are_workspace_aware() -> None:
    assert resolve_permission_mode("docker") is PermissionMode.BYPASS
    assert resolve_permission_mode("docker", "auto") is PermissionMode.BYPASS
    assert resolve_permission_mode("local") is PermissionMode.DONT_ASK
    assert resolve_permission_mode("local", "auto") is PermissionMode.DONT_ASK


def test_explicit_permission_mode_is_preserved() -> None:
    assert (
        resolve_permission_mode("local", "accept_edits")
        is PermissionMode.ACCEPT_EDITS
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


async def test_agent_resumes_after_confirmation(monkeypatch) -> None:
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

    async def fake_reply_stream(self, inputs=None):
        received.append(inputs)
        if len(received) == 1:
            yield required
        else:
            yield inputs

    async def confirm(event):
        return UserConfirmResultEvent(
            reply_id=event.reply_id,
            confirm_results=[
                ConfirmResult(confirmed=True, tool_call=event.tool_calls[0]),
            ],
        )

    monkeypatch.setattr(Agent, "reply_stream", fake_reply_stream)
    agent = object.__new__(DataPawAgent)
    agent._confirmation_handler = confirm

    events = [event async for event in agent._reply_with_confirmations("start")]

    assert events[0] is required
    assert isinstance(received[1], UserConfirmResultEvent)
    assert received[1].confirm_results[0].confirmed is True

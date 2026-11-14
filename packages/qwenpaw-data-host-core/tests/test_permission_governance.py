"""Permission policy regression tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.event import (
    RequireUserConfirmEvent,
)
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.permission import PermissionMode

from qwenpaw_data.host.core.permission import (
    build_permission_context,
    deny_confirmation,
    resolve_permission_mode,
)
from qwenpaw_data.host.core.agent.middleware.tool_confirmation import (
    ToolConfirmationMiddleware,
)


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


@pytest.mark.parametrize("answer", ["approved", "denied", "timeout", "invalid"])
async def test_service_only_executes_explicitly_approved_tools(answer) -> None:
    tool_call = ToolCallBlock(
        id="call-1",
        name="Bash",
        input='{"command":"pwd"}',
    )
    middleware = ToolConfirmationMiddleware(
        run_context=SimpleNamespace(
            session_factory=None,
            chat_id="chat-1",
            user_runtime_config=SimpleNamespace(tool_confirmation_level="all"),
        ),
    )
    middleware._gate = SimpleNamespace(
        decide=lambda _: "ask", wait=AsyncMock(return_value=answer)
    )
    executed = []

    async def execute(**kwargs):
        executed.append(kwargs["tool_call"].id)
        yield "executed"

    async def collect():
        return [
            item
            async for item in middleware.on_acting(
                None, {"tool_call": tool_call}, execute
            )
        ]

    if answer == "invalid":
        with pytest.raises(ValueError, match="invalid tool confirmation"):
            await collect()
    else:
        events = await collect()
        if answer == "approved":
            assert events == ["executed"]
        else:
            assert len(events) == 1
            assert events[0].state == ToolResultState.DENIED
    assert executed == (["call-1"] if answer == "approved" else [])
    middleware._gate.wait.assert_awaited_once_with("call-1")


@pytest.mark.parametrize(
    "error", [RuntimeError("gate failed"), asyncio.CancelledError()]
)
async def test_service_confirmation_failure_never_executes_tool(error) -> None:
    middleware = ToolConfirmationMiddleware(
        run_context=SimpleNamespace(
            session_factory=None,
            chat_id="chat-1",
            user_runtime_config=SimpleNamespace(tool_confirmation_level="all"),
        ),
    )
    middleware._gate = SimpleNamespace(
        decide=lambda _: "ask", wait=AsyncMock(side_effect=error)
    )
    executed = []

    async def execute(**kwargs):
        executed.append(kwargs)
        yield "must not execute"

    with pytest.raises(type(error)):
        async for _ in middleware.on_acting(
            None, {"tool_call": SimpleNamespace(id="call-1", name="Bash")}, execute
        ):
            pass
    assert executed == []

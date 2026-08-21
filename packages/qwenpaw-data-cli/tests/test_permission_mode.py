"""CLI permission mode and confirmation regression tests."""

from __future__ import annotations

import argparse

from agentscope.event import RequireUserConfirmEvent
from agentscope.message import ToolCallBlock

from qwenpaw_data.cli.util import (
    build_cli_confirmation_handler,
    resolve_permission_mode,
)


def test_cli_permission_mode_defaults(monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_PERMISSION_MODE", raising=False)
    args = argparse.Namespace(permission_mode=None)

    assert resolve_permission_mode(args, "docker", interactive=True) == "bypass"
    assert (
        resolve_permission_mode(args, "local", interactive=True)
        == "accept_edits"
    )
    assert resolve_permission_mode(args, "local", interactive=False) == "dont_ask"


def test_cli_permission_mode_explicit_precedence(monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_PERMISSION_MODE", "explore")
    args = argparse.Namespace(permission_mode="bypass")

    assert resolve_permission_mode(args, "local", interactive=True) == "bypass"
    assert (
        resolve_permission_mode(argparse.Namespace(), "local", interactive=True)
        == "explore"
    )


async def test_cli_confirmation_prompts_once(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    event = RequireUserConfirmEvent(
        reply_id="reply-1",
        tool_calls=[
            ToolCallBlock(
                id="call-1",
                name="Bash",
                input='{"command":"pwd"}',
            ),
        ],
    )

    result = await build_cli_confirmation_handler(interactive=True)(event)

    assert result.confirm_results[0].confirmed is True


async def test_cli_confirmation_denies_without_tty(monkeypatch) -> None:
    def unexpected_input(prompt):
        raise AssertionError("input must not be called without a TTY")

    monkeypatch.setattr("builtins.input", unexpected_input)
    event = RequireUserConfirmEvent(
        reply_id="reply-1",
        tool_calls=[
            ToolCallBlock(
                id="call-1",
                name="Bash",
                input='{"command":"pwd"}',
            ),
        ],
    )

    result = await build_cli_confirmation_handler(interactive=False)(event)

    assert result.confirm_results[0].confirmed is False

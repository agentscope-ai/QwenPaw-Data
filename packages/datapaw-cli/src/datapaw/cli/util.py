"""Shared helpers for DataPaw CLI commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

_WORKSPACE_TYPES = ("local", "docker")
_PERMISSION_MODES = (
    "auto",
    "default",
    "accept_edits",
    "explore",
    "bypass",
    "dont_ask",
)


def add_prompt_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("prompt", nargs="*", help="Prompt text")
    parser.add_argument("--file", "-f", type=Path, help="Read prompt text from a file")


def print_json(payload: Any) -> None:
    """Print an API payload with the CLI's canonical JSON formatting."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object from a file, rejecting non-object documents."""
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def parse_json_object(text: str, *, flag: str) -> dict[str, Any]:
    """Parse an inline JSON object argument."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{flag} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{flag} must be a JSON object")
    return payload


def confirm_deletion(subject: str, *, assume_yes: bool) -> bool:
    """Terminal confirmation for destructive commands.

    Returns True when the deletion may proceed. Non-TTY callers must pass
    --yes explicitly so unattended scripts never block or delete by accident.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError(f"refusing to delete {subject} without --yes in a non-interactive session")
    try:
        answer = input(f"Delete {subject}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def add_stream_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream intermediate agent output to stdout (default: enabled)",
    )


def add_datasource_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--datasource-id",
        help="Datasource id to inject into DataBridge MCP metadata",
    )


def add_workspace_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        choices=_WORKSPACE_TYPES,
        default=None,
        help=(
            "Workspace backend for tool execution: docker (default, container "
            "boundary) or local (unsandboxed host execution). Overrides "
            "DATAPAW_WORKSPACE."
        ),
    )


def add_permission_mode_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--permission-mode",
        choices=_PERMISSION_MODES,
        default=None,
        help=(
            "AgentScope tool permission policy. auto (default) uses bypass "
            "inside Docker, accept_edits for an interactive local terminal, "
            "and dont_ask for unattended local execution. Overrides "
            "DATAPAW_PERMISSION_MODE."
        ),
    )


def resolve_workspace_type(args: argparse.Namespace | None = None) -> str:
    """CLI 参数 > DATAPAW_WORKSPACE 环境变量 > docker；非法值直接报错。"""
    from_args = getattr(args, "workspace", None) if args is not None else None
    value = (from_args or os.getenv("DATAPAW_WORKSPACE", "") or "docker").strip().lower()
    if value not in _WORKSPACE_TYPES:
        raise ValueError(
            f"invalid workspace type {value!r} "
            f"(expected one of {', '.join(_WORKSPACE_TYPES)})"
        )
    return value


def resolve_permission_mode(
    args: argparse.Namespace | None,
    workspace_type: str,
    *,
    interactive: bool | None = None,
) -> str:
    """Resolve CLI permission mode with a fail-closed local default."""
    from_args = getattr(args, "permission_mode", None) if args is not None else None
    value = (
        from_args
        or os.getenv("DATAPAW_PERMISSION_MODE", "")
        or "auto"
    ).strip().lower()
    if value not in _PERMISSION_MODES:
        raise ValueError(
            f"invalid permission mode {value!r} "
            f"(expected one of {', '.join(_PERMISSION_MODES)})",
        )
    if value != "auto":
        return value
    if workspace_type == "docker":
        return "bypass"
    if interactive is None:
        interactive = sys.stdin.isatty()
    return "accept_edits" if interactive else "dont_ask"


def build_cli_confirmation_handler(*, interactive: bool | None = None) -> Any:
    """Create a terminal confirmation callback; non-TTY callers deny."""
    from agentscope.event import ConfirmResult, UserConfirmResultEvent

    if interactive is None:
        interactive = sys.stdin.isatty()

    async def confirm(event: Any) -> Any:
        results = []
        for tool_call in event.tool_calls:
            approved = False
            if interactive:
                raw_input = str(tool_call.input)
                if len(raw_input) > 500:
                    raw_input = raw_input[:500] + "..."
                prompt = (
                    "\nDataPaw permission request\n"
                    f"  tool: {tool_call.name}\n"
                    f"  input: {raw_input}\n"
                    "Allow this call? [y/N] "
                )
                try:
                    answer = await asyncio.to_thread(input, prompt)
                except EOFError:
                    answer = ""
                approved = answer.strip().lower() in {"y", "yes"}
            results.append(
                ConfirmResult(confirmed=approved, tool_call=tool_call),
            )
        return UserConfirmResultEvent(
            reply_id=event.reply_id,
            confirm_results=results,
        )

    return confirm


def request_context_from_args(args: argparse.Namespace) -> dict[str, Any]:
    datasource_id = getattr(args, "datasource_id", None)
    if not datasource_id:
        return {}
    return {"datasource_id": datasource_id}


def create_datapaw(
    request_context: dict[str, Any] | None = None,
    workspace_type: str | None = None,
    permission_mode: str | None = None,
    confirmation_handler: Any = None,
) -> Any:
    from datapaw.host.core import DataPawHost, resolve_datapaw_home

    return DataPawHost(
        home=resolve_datapaw_home(),
        workspace_type=workspace_type or resolve_workspace_type(),
        request_context=request_context,
        permission_mode=permission_mode,
        confirmation_handler=confirmation_handler,
    )


def read_prompt(args: argparse.Namespace) -> str:
    if args.file and args.prompt:
        raise ValueError("pass either prompt text or --file, not both")

    text = (
        args.file.expanduser().read_text(encoding="utf-8")
        if args.file
        else " ".join(args.prompt)
    )
    text = text.strip()
    if not text:
        raise ValueError("prompt is required")
    return text


async def print_event_stream(events: Any) -> None:
    from agentscope.event import (
        ReplyEndEvent,
        TextBlockDeltaEvent,
        ToolCallDeltaEvent,
        ToolCallEndEvent,
        ToolCallStartEvent,
        ToolResultEndEvent,
        ToolResultStartEvent,
        ToolResultTextDeltaEvent,
    )
    from agentscope.message import ToolResultState

    _bad_states = {
        ToolResultState.ERROR,
        ToolResultState.DENIED,
        ToolResultState.INTERRUPTED,
    }

    async for event in events:
        if isinstance(event, TextBlockDeltaEvent):
            print(event.delta, end="", flush=True)
        elif isinstance(event, ToolCallStartEvent):
            print(f"\n[tool call] {event.tool_call_name} ", end="", flush=True)
        elif isinstance(event, ToolCallDeltaEvent):
            print(event.delta, end="", flush=True)
        elif isinstance(event, ToolCallEndEvent):
            print("", flush=True)
        elif isinstance(event, ToolResultStartEvent):
            print("[tool result] ", end="", flush=True)
        elif isinstance(event, ToolResultTextDeltaEvent):
            print(event.delta, end="", flush=True)
        elif isinstance(event, ToolResultEndEvent):
            state = getattr(event, "state", None)
            if state in _bad_states:
                state_text = getattr(state, "value", state)
                print(f"\n[tool {state_text}] tool call failed (state={state_text})", flush=True)
            else:
                print("", flush=True)
        elif isinstance(event, ReplyEndEvent):
            print("", flush=True)


def print_msg(msg: Any) -> None:
    _print_failed_tool_results(msg)
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        text = content.strip()
        if text:
            print(text)
        return

    parts: list[str] = []
    for item in content or []:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content")
            if text:
                parts.append(str(text))
        else:
            text = getattr(item, "text", None) or getattr(item, "content", None)
            if text:
                parts.append(str(text))

    text = "\n".join(parts).strip()
    if text:
        print(text)


def _print_failed_tool_results(msg: Any) -> None:
    """Surface ERROR/DENIED/INTERRUPTED tool results on stderr.

    In non-stream mode only the final message is printed, so failed tool
    results (e.g. MCP errors, which never raise) would otherwise be invisible.
    """
    import sys

    get_blocks = getattr(msg, "get_content_blocks", None)
    if not callable(get_blocks):
        return
    try:
        tool_results = get_blocks("tool_result")
    except Exception:
        return

    bad_states = {"error", "denied", "interrupted"}
    for block in tool_results:
        state = str(getattr(getattr(block, "state", None), "value", getattr(block, "state", "")) or "")
        if state not in bad_states:
            continue
        name = getattr(block, "name", "") or "?"
        texts: list[str] = []
        for item in getattr(block, "output", None) or []:
            text = getattr(item, "text", None)
            if text:
                texts.append(str(text))
        detail = " ".join(texts).strip()
        print(f"[tool {state}] {name}: {detail}", file=sys.stderr)


def print_execution_summary(msg: Any) -> None:
    metadata = getattr(msg, "metadata", {}) or {}
    graph_id = metadata.get("graph_id")
    nodes = metadata.get("nodes") or []
    artifacts = metadata.get("artifacts") or []
    if not graph_id and not nodes and not artifacts:
        return

    print("\nExecution summary:")
    if graph_id:
        print(f"graph_id: {graph_id}")
    if "completed" in metadata:
        print(f"completed: {metadata['completed']}")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id", "")
        name = node.get("name", "")
        state = node.get("state", "")
        print(f"- {state}: {name} ({node_id})")
    if artifacts:
        print("artifacts:")
        for item in artifacts:
            if isinstance(item, dict):
                print(f"- {item.get('path') or item.get('name') or item}")
            else:
                print(f"- {item}")

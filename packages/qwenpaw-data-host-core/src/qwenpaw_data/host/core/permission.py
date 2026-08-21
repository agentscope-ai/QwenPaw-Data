"""Workspace-aware AgentScope permission policy for QwenPaw Data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.event import (
    ConfirmResult,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionContext,
    PermissionMode,
)

ConfirmationHandler = Callable[
    [RequireUserConfirmEvent],
    Awaitable[UserConfirmResultEvent],
]


def resolve_permission_mode(
    workspace_type: str,
    requested: PermissionMode | str | None = None,
) -> PermissionMode:
    """Resolve a safe library-level permission mode.

    Docker execution is already bounded by a per-session container, so the
    AgentScope permission layer may run in ``BYPASS`` mode there.  Local
    execution has host-user privileges and therefore fails closed unless the
    caller explicitly selects another mode and supplies confirmation handling.
    """
    if requested is None or str(requested).strip().lower() == "auto":
        return (
            PermissionMode.BYPASS
            if workspace_type == "docker"
            else PermissionMode.DONT_ASK
        )
    if isinstance(requested, PermissionMode):
        return requested
    try:
        return PermissionMode(str(requested).strip().lower())
    except ValueError as exc:
        choices = ", ".join(mode.value for mode in PermissionMode)
        raise ValueError(
            f"invalid permission mode {requested!r} (expected one of {choices})",
        ) from exc


def build_permission_context(
    *,
    mode: PermissionMode,
    workdir: Any,
) -> PermissionContext:
    """Create a permission context scoped to the selected workspace root."""
    path = str(workdir)
    return PermissionContext(
        mode=mode,
        working_directories={
            path: AdditionalWorkingDirectory(path=path, source="session"),
        },
    )


def deny_confirmation(event: RequireUserConfirmEvent) -> UserConfirmResultEvent:
    """Build a fail-closed response for a pending permission request."""
    return UserConfirmResultEvent(
        reply_id=event.reply_id,
        confirm_results=[
            ConfirmResult(confirmed=False, tool_call=tool_call)
            for tool_call in event.tool_calls
        ],
    )

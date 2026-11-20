"""Read ODPS execution facts from Host events, preserving develop semantics."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..algo.reconciliation import evidence_time

_SQL_TOOLS = frozenset({"execute_read", "execute_modify", "execute_create"})


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _metadata_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _sql_tool_name(value: Any) -> str:
    name = _string(value)
    for prefix in ("mcp__context-manager__", "context-manager__"):
        if name.startswith(prefix):
            name = name.removeprefix(prefix)
            break
    return name if name in _SQL_TOOLS else ""


def _tool_blocks(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    return [
        block["data"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "data"
        and isinstance(block.get("data"), dict)
    ]


def _execution_project(data: Mapping[str, Any], arguments: Mapping[str, Any]) -> str:
    if data.get("state") != "success":
        return ""
    output = _metadata_object(data.get("output"))
    if output.get("error"):
        return ""
    if "exec_status" in output or "odps_execution_project" in output:
        return (
            _string(output.get("odps_execution_project"))
            if output.get("exec_status") in ("success", "empty", "slow")
            else ""
        )
    # Reconstructed execute_read raises on failure and uses the supplied project
    # unchanged (no project fallback). Its successful result is a row preview.
    if (
        _sql_tool_name(data.get("name")) == "execute_read"
        and isinstance(output.get("columns"), list)
        and isinstance(output.get("rows"), list)
    ):
        return _string(arguments.get("odps_execution_project"))
    return ""


def _execution_datasource(arguments: Mapping[str, Any], chat_datasource_id: str) -> str:
    # Match CM resolution: explicit argument, Host's chat header, then metadata.
    metadata = _metadata_object(arguments.get("metadata"))
    return (
        _string(arguments.get("datasource_id"))
        or chat_datasource_id
        or _string(metadata.get("datasource_id"))
        or _string(metadata.get("ds_id"))
    )


def odps_project_observations(
    events: Sequence[Mapping[str, Any]],
    *,
    chat_id: str,
    chat_datasource_id: str,
    datasource_types: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Keep the latest successful effective project per scoped ODPS datasource."""
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = _metadata_object(event.get("payload"))
        if payload.get("status", "completed") != "completed":
            continue
        for data in _tool_blocks(payload):
            name = _sql_tool_name(data.get("name"))
            if not name:
                continue
            call_id = _string(data.get("call_id")) or _string(payload.get("source_id"))
            if payload.get("type") == "plugin_call":
                if call_id:
                    calls[call_id, name] = _metadata_object(data.get("arguments"))
                continue
            if payload.get("type") != "plugin_call_output":
                continue
            arguments = calls.get((call_id, name), {})
            project = _execution_project(data, arguments)
            datasource_id = _execution_datasource(arguments, chat_datasource_id)
            if not project or datasource_types.get(datasource_id) != "odps":
                continue
            observation = {
                "datasource_id": datasource_id,
                "project": project,
                "source_ref": f"host-chat:{chat_id}:event:{event['sequence_number']}",
                "evidence_at": evidence_time(event["created_at"]),
                "sequence_number": event["sequence_number"],
            }
            previous = latest.get(datasource_id)
            if previous is None or (
                observation["evidence_at"],
                observation["sequence_number"],
            ) > (previous["evidence_at"], previous["sequence_number"]):
                latest[datasource_id] = observation
    return [latest[key] for key in sorted(latest)]

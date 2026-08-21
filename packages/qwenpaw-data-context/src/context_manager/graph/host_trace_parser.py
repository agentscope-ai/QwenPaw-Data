"""Parse a QwenPaw Data host session snapshot into an idempotent TG write model."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any


_GRAPH_ID_RE = re.compile(r"\bGraph ID:\s*([A-Za-z0-9_-]+)")
_SESSION_ID_RE = re.compile(r"(?m)^- Session ID:\s*(.+?)\s*$")
_USER_ID_RE = re.compile(r"(?m)^- User ID:\s*(.+?)\s*$")
_STATE_MAP = {
    "todo": "pending",
    "pending": "pending",
    "in_progress": "running",
    "running": "running",
    "done": "success",
    "success": "success",
    "completed": "success",
    "failed": "failed",
    "error": "failed",
    "abandoned": "failed",
    "cancelled": "failed",
}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(
                    str(
                        item.get("text")
                        or item.get("thinking")
                        or item.get("output")
                        or ""
                    )
                )
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("thinking") or _dump(value))
    return str(value or "")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_input_intent(tool_name: str, value: Any) -> str:
    """Prefer natural-language fields from the source tool input."""
    payload = _json_object(value)
    for key in (
        "task",
        "goal",
        "description",
        "query",
        "command",
        "sql",
        "file_path",
        "name",
    ):
        text = _text(payload.get(key)).strip()
        if text:
            return text
    return tool_name


def _extract_datasource_id(payload: dict[str, Any]) -> str:
    """Pull the datasource identifier from the host trace payload metadata.

    Accept ``datasource_id`` (canonical), then ``ds_id`` (older hosts, often
    the numeric PK), and normalize to the canonical id so TG nodes are stamped
    consistently with MG nodes and match the frontend's graph filter.
    """
    def _pick(d: dict[str, Any]) -> Any:
        return (
            d.get("datasource_id")
            or d.get("ds_id")
        )

    raw_metadata = payload.get("metadata")
    value: Any = None
    if isinstance(raw_metadata, dict):
        value = _pick(raw_metadata)
    elif isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            value = _pick(parsed)
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from ..api.datasource_active_api import resolve_datasource_id

        return resolve_datasource_id(raw) or raw
    except Exception:
        return raw


def _agent_context_value(
    payload: dict[str, Any],
    agent: dict[str, Any],
    field: str,
    pattern: re.Pattern[str],
) -> str:
    direct = payload.get(field) or agent.get(field)
    if direct:
        return str(direct).strip()
    match = pattern.search(str(agent.get("_sys_prompt") or ""))
    return match.group(1).strip() if match else ""


def _state(value: Any, default: str = "pending") -> str:
    return _STATE_MAP.get(str(value or "").strip().lower(), default)


def _result_status(block: dict[str, Any]) -> tuple[str, str]:
    """Return normalized status and an optional error message."""
    explicit = block.get("state") or block.get("exec_status") or block.get("status")
    output = block.get("output")
    parsed = output
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (TypeError, ValueError):
            parsed = output
    if isinstance(parsed, dict):
        explicit = (
            parsed.get("state")
            or parsed.get("exec_status")
            or parsed.get("status")
            or explicit
        )
        error = str(parsed.get("error") or "")
        if error:
            return "failed", error
    normalized = str(explicit or "").strip().lower()
    if normalized in {"error", "failed", "failure"}:
        return "failed", _text(output)[:2000]
    if normalized in {"success", "ok", "done", "completed", "slow"}:
        return "success", ""
    raw_text = _text(output)
    lowered = raw_text.lower()
    if (
        "command failed" in lowered
        or lowered.startswith("error:")
        or '"exec_status":"error"' in lowered.replace(" ", "")
    ):
        return "failed", raw_text[:2000]
    return "success", ""


def _graph_id(result: dict[str, Any] | None, result_message: dict[str, Any] | None) -> str:
    metadata = (result_message or {}).get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("graph_id"):
        return str(metadata["graph_id"])
    match = _GRAPH_ID_RE.search(_text((result or {}).get("output")))
    return match.group(1) if match else ""


def _message_records(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    agent = payload.get("agent")
    if not isinstance(agent, dict):
        raise ValueError("host trace must contain an agent object")
    memory = agent.get("memory")
    raw_content = memory.get("content") if isinstance(memory, dict) else None
    if not isinstance(raw_content, list) or not raw_content:
        raise ValueError("host trace agent.memory.content must be a non-empty list")

    messages: list[dict[str, Any]] = []
    for index, item in enumerate(raw_content):
        raw_message = item[0] if isinstance(item, list) and item else item
        if not isinstance(raw_message, dict):
            raise ValueError(f"memory item {index} does not contain a message object")
        message_id = str(raw_message.get("id") or "").strip()
        if not message_id:
            raise ValueError(f"memory message {index} is missing id")
        messages.append(
            {
                "index": index,
                "id": message_id,
                "name": str(raw_message.get("name") or ""),
                "role": str(raw_message.get("role") or ""),
                "timestamp": str(raw_message.get("timestamp") or ""),
                "metadata": raw_message.get("metadata")
                if isinstance(raw_message.get("metadata"), dict)
                else {},
                "content": raw_message.get("content"),
                "raw": raw_message,
            }
        )
    return agent, messages


def parse_host_trace(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse one complete host session JSON document.

    The returned dictionaries contain only Neo4j-compatible scalar values.
    No graph writes happen in this function.
    """
    if not isinstance(payload, dict):
        raise ValueError("host trace body must be a JSON object")
    agent, messages = _message_records(payload)
    first_user = next((m for m in messages if m["role"] == "user"), messages[0])
    session_anchor = (
        _agent_context_value(payload, agent, "session_id", _SESSION_ID_RE)
        or first_user["id"]
    )
    user_id = (
        _agent_context_value(payload, agent, "user_id", _USER_ID_RE)
        or "unknown"
    )
    session_key = f"sess:{session_anchor}"
    agent_name = str(agent.get("name") or "QwenPaw Data")
    datasource_id = _extract_datasource_id(payload)

    model: dict[str, Any] = {
        "session": {
            "key": session_key,
            "session_id": session_anchor,
            "user_id": user_id,
            "agent_name": agent_name,
            "datasource_id": datasource_id,
            "metadata_json": _dump(
                {
                    "source": "qwenpaw_data_host",
                    "agent_name": agent_name,
                    "compressed_summary": (
                        (agent.get("memory") or {}).get("_compressed_summary", "")
                        if isinstance(agent.get("memory"), dict)
                        else ""
                    ),
                }
            ),
            "message_count": len(messages),
            "trace_hash": _hash(
                _dump([message["raw"] for message in messages]), 64
            ),
        },
        "tasks": [],
        "plans": [],
        "tool_calls": [],
        "spawns": [],
    }

    calls: list[dict[str, Any]] = []
    results_by_id: dict[
        str, list[tuple[dict[str, Any], dict[str, Any], int]]
    ] = defaultdict(list)
    for message in messages:
        for block_index, block in enumerate(_blocks(message["raw"])):
            block_type = str(block.get("type") or "")
            if block_type == "tool_use":
                call_id = str(block.get("id") or "").strip()
                if not call_id:
                    raise ValueError(
                        f"tool_use in message {message['id']}[{block_index}] is missing id"
                    )
                calls.append(
                    {
                        "id": call_id,
                        "name": str(block.get("name") or ""),
                        "input": block.get("input") or {},
                        "raw": block,
                        "message": message,
                        "block_index": block_index,
                    }
                )
            elif block_type == "tool_result":
                result_id = str(block.get("id") or "").strip()
                if not result_id:
                    raise ValueError(
                        f"tool_result in message {message['id']}[{block_index}] is missing id"
                    )
                results_by_id[result_id].append((block, message, block_index))

    call_ids = {call["id"] for call in calls}
    orphan_results = sorted(set(results_by_id) - call_ids)
    if orphan_results:
        raise ValueError(f"orphan tool_result ids: {', '.join(orphan_results[:5])}")

    create_calls = [call for call in calls if call["name"] == "create_plan"]
    if not create_calls:
        # Router class 1a/1b (simple query) turns answer directly without a
        # DAG. Capture them as one implicit linear Task instead of rejecting.
        if not calls:
            raise ValueError(
                "host trace has neither create_plan nor tool calls"
            )
        _build_planless_model(
            model,
            calls=calls,
            results_by_id=results_by_id,
            agent_name=agent_name,
            session_key=session_key,
            session_anchor=session_anchor,
            first_user=first_user,
        )
        _validate_model(model)
        return model

    graphs: dict[str, dict[str, Any]] = {}
    call_graph: dict[str, str] = {}
    for call in create_calls:
        result_tuples = results_by_id.get(call["id"]) or []
        result_tuple = result_tuples[0] if result_tuples else None
        result = result_tuple[0] if result_tuple else None
        result_message = result_tuple[1] if result_tuple else None
        graph_id = _graph_id(result, result_message)
        if not graph_id:
            raise ValueError(f"cannot determine graph_id for create_plan {call['id']}")
        plan_input = call["input"] if isinstance(call["input"], dict) else {}
        nodes = plan_input.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(f"create_plan {call['id']} contains no nodes")
        task_key = f"task:qwenpaw-data:{graph_id}"
        graph = {
            "graph_id": graph_id,
            "task_key": task_key,
            "input": plan_input,
            "plans": {},
            "active_node": "",
            "bootstrap_key": f"step:{graph_id}:bootstrap",
        }
        graphs[graph_id] = graph
        call_graph[call["id"]] = graph_id
        anchor_message = next(
            (
                message
                for message in reversed(messages[: call["message"]["index"] + 1])
                if message["role"] == "user"
            ),
            first_user,
        )
        model["tasks"].append(
            {
                "key": task_key,
                "goal": str(
                    plan_input.get("description")
                    or plan_input.get("name")
                    or _text(first_user["content"])
                )[:8000],
                "status": "running",
                "owner_agent": agent_name,
                "task_signature": _hash(
                    str(plan_input.get("description") or plan_input.get("name") or graph_id)
                ),
                "created_at": call["message"]["timestamp"],
                "task_kind": "main",
                "parent_task_key": "",
                "parent_tool_call_key": "",
                "graph_id": graph_id,
                "session_key": session_key,
                "source_message_id": anchor_message["id"],
                "source_message_role": anchor_message["role"],
                "source_message_timestamp": anchor_message["timestamp"],
                "user_input": _text(anchor_message["content"])[:8000],
                "expected_outcome": str(plan_input.get("expected_outcome") or "")[:8000],
            }
        )
        bootstrap_reasoning = [
            block
            for block in _blocks(call["message"]["raw"])
            if block.get("type") in {"thinking", "text"}
        ]
        bootstrap_intent = " ".join(
            _text(block).strip() for block in bootstrap_reasoning
        ).strip()
        if not bootstrap_intent:
            bootstrap_intent = str(
                plan_input.get("description")
                or plan_input.get("name")
                or "create_plan"
            )
        bootstrap = {
            "key": graph["bootstrap_key"],
            "task_key": task_key,
            "step_idx": -1,
            "source_node_id": "bootstrap",
            "intent": bootstrap_intent[:8000],
            "status": "success",
            "tool_hint": "",
            "source_entry_index": -1,
            "deps_json": "[]",
            "source_message_id": call["message"]["id"],
            "source_message_role": call["message"]["role"],
            "source_message_timestamp": call["message"]["timestamp"],
            "reasoning_json": _dump(bootstrap_reasoning),
        }
        graph["plans"]["bootstrap"] = bootstrap
        model["plans"].append(bootstrap)
        for node_index, raw_node in enumerate(nodes):
            if not isinstance(raw_node, dict):
                raise ValueError(
                    f"create_plan {call['id']} node {node_index} is not an object"
                )
            node_id = str(raw_node.get("node_id") or "").strip()
            if not node_id:
                raise ValueError(f"create_plan {call['id']} node {node_index} has no node_id")
            plan = {
                "key": f"step:{graph_id}:{node_id}",
                "task_key": task_key,
                "step_idx": node_index,
                "source_node_id": node_id,
                "intent": str(
                    raw_node.get("description") or raw_node.get("name") or node_id
                )[:8000],
                "status": _state(raw_node.get("state")),
                "tool_hint": "",
                "source_entry_index": node_index,
                "deps_json": _dump(
                    [str(dep) for dep in raw_node.get("deps") or []]
                ),
                "source_message_id": "",
                "source_message_role": "",
                "source_message_timestamp": "",
                "reasoning_json": "[]",
            }
            graph["plans"][node_id] = plan
            model["plans"].append(plan)
        for raw_node in nodes:
            node_id = str(raw_node.get("node_id") or "")
            for dep in raw_node.get("deps") or []:
                dep_id = str(dep)
                if dep_id not in graph["plans"]:
                    raise ValueError(f"unknown dependency {dep_id} for plan {node_id}")

    default_graph_id = next(iter(graphs))
    active_graph_id = default_graph_id
    task_by_key = {task["key"]: task for task in model["tasks"]}

    for call_index, call in enumerate(calls):
        message = call["message"]
        metadata = message["metadata"]
        graph_id = str(metadata.get("graph_id") or call_graph.get(call["id"]) or active_graph_id)
        if graph_id not in graphs:
            graph_id = default_graph_id
        graph = graphs[graph_id]
        active_graph_id = graph_id
        call_input = call["input"] if isinstance(call["input"], dict) else {}
        node_id = str(metadata.get("node_id") or call_input.get("node_id") or "")
        if call["name"] == "create_plan":
            plan_key = graph["bootstrap_key"]
        elif node_id and node_id in graph["plans"]:
            graph["active_node"] = node_id
            plan_key = graph["plans"][node_id]["key"]
        elif graph["active_node"]:
            plan_key = graph["plans"][graph["active_node"]]["key"]
        else:
            plan_key = graph["bootstrap_key"]

        result_tuples = results_by_id.get(call["id"]) or []
        status, error = (
            _result_status(result_tuples[-1][0])
            if result_tuples
            else ("running", "")
        )
        tool_key = f"tc:{graph_id}:{call['id']}"
        tool_row = {
            "key": tool_key,
            "plan_key": plan_key,
            "tool_name": call["name"],
            "args_json": _dump(call_input),
            "status": status,
            "error": error,
            "agent_name": agent_name,
            "source_message_id": message["id"],
            "source_message_role": message["role"],
            "source_message_timestamp": message["timestamp"],
            "source_entry_index": call["block_index"],
            "parent_tool_call_key": "",
            "synthetic": False,
            "observations": [],
        }
        model["tool_calls"].append(tool_row)
        target_plan = next(
            plan for plan in model["plans"] if plan["key"] == plan_key
        )
        if not target_plan["source_message_id"]:
            target_plan["source_message_id"] = message["id"]
            target_plan["source_message_role"] = message["role"]
            target_plan["source_message_timestamp"] = message["timestamp"]
            target_plan["reasoning_json"] = _dump(
                [
                    block
                    for block in _blocks(message["raw"])
                    if block.get("type") in {"thinking", "text"}
                ]
            )
        for result_ordinal, result_tuple in enumerate(result_tuples):
            result, result_message, result_index = result_tuple
            obs_key = (
                f"obs:{graph_id}:{call['id']}"
                if len(result_tuples) == 1
                else f"obs:{graph_id}:{call['id']}:{result_ordinal}"
            )
            result_status, result_error = _result_status(result)
            observation = {
                "key": obs_key,
                "tool_call_key": tool_key,
                "summary": _text(result.get("output"))[:8000],
                "output_json": _dump(result.get("output")),
                "status": result_status,
                "error": result_error,
                "source_message_id": result_message["id"],
                "source_message_role": result_message["role"],
                "source_message_timestamp": result_message["timestamp"],
                "source_entry_index": result_index,
            }
            tool_row["observations"].append(observation)

            subtrace = (
                result_message["metadata"].get("sub_agent_trace")
                if call["name"] == "spawn_subagent"
                else None
            )
            if (
                call["name"] == "spawn_subagent"
                and not isinstance(subtrace, dict)
            ):
                subtrace = result_message["metadata"].get("subagent_trace")
            if isinstance(subtrace, dict):
                _parse_subagent_trace(
                    subtrace,
                    model=model,
                    graph_id=graph_id,
                    parent_task_key=graph["task_key"],
                    parent_tool=tool_row,
                    source_message=result_message,
                    depth=1,
                    visited=set(),
                )

        # Replay host task/plan state after recording the control call itself.
        if node_id and node_id in graph["plans"]:
            if call["name"] == "update_subtask_state":
                graph["plans"][node_id]["status"] = _state(call_input.get("state"))
            elif call["name"] == "finish_subtask":
                graph["plans"][node_id]["status"] = "success" if status == "success" else "failed"
        if call["name"] == "finish_plan":
            task_by_key[graph["task_key"]]["status"] = _state(
                call_input.get("state"), status
            )

    for graph in graphs.values():
        task = task_by_key[graph["task_key"]]
        if task["status"] not in {"success", "failed"}:
            statuses = [
                plan["status"]
                for node_id, plan in graph["plans"].items()
                if node_id != "bootstrap"
            ]
            if statuses and all(status == "success" for status in statuses):
                task["status"] = "success"
            elif any(status == "failed" for status in statuses):
                task["status"] = "failed"
            else:
                task["status"] = "running"

    _validate_model(model)
    return model


def _build_planless_model(
    model: dict[str, Any],
    *,
    calls: list[dict[str, Any]],
    results_by_id: dict[
        str, list[tuple[dict[str, Any], dict[str, Any], int]]
    ],
    agent_name: str,
    session_key: str,
    session_anchor: str,
    first_user: dict[str, Any],
) -> None:
    """Build a single implicit Task for sessions without a create_plan call.

    Simple query turns (router class 1a/1b) answer directly without a DAG.
    We still record them in the Trace Graph as one linear Task whose Steps
    mirror the tool-call sequence, so every data task gets a trace.
    """
    graph_id = f"implicit:{session_anchor}"
    task_key = f"task:qwenpaw-data:{graph_id}"
    goal = (_text(first_user["content"]) or agent_name)[:8000]
    model["tasks"].append(
        {
            "key": task_key,
            "goal": goal,
            "status": "running",
            "owner_agent": agent_name,
            "task_signature": _hash(f"{graph_id}|{goal}"),
            "created_at": first_user["timestamp"],
            "task_kind": "main",
            "parent_task_key": "",
            "parent_tool_call_key": "",
            "graph_id": graph_id,
            "session_key": session_key,
            "source_message_id": first_user["id"],
            "source_message_role": first_user["role"],
            "source_message_timestamp": first_user["timestamp"],
            "user_input": _text(first_user["content"])[:8000],
            "expected_outcome": "",
        }
    )
    task = model["tasks"][-1]

    statuses: list[str] = []
    for step_idx, call in enumerate(calls):
        message = call["message"]
        call_input = call["input"] if isinstance(call["input"], dict) else {}
        result_tuples = results_by_id.get(call["id"]) or []
        status, error = (
            _result_status(result_tuples[-1][0])
            if result_tuples
            else ("running", "")
        )
        statuses.append(status)
        reasoning = [
            block
            for block in _blocks(message["raw"])
            if block.get("type") in {"thinking", "text"}
        ]
        intent = " ".join(
            _text(block).strip() for block in reasoning
        ).strip()
        if not intent:
            intent = _tool_input_intent(call["name"], call_input)
        plan_key = f"step:{graph_id}:{step_idx}"
        model["plans"].append(
            {
                "key": plan_key,
                "task_key": task_key,
                "step_idx": step_idx,
                "source_node_id": f"call:{step_idx}",
                "intent": intent[:8000],
                "status": status,
                "tool_hint": call["name"],
                "source_entry_index": call["block_index"],
                "deps_json": "[]",
                "source_message_id": message["id"],
                "source_message_role": message["role"],
                "source_message_timestamp": message["timestamp"],
                "reasoning_json": _dump(reasoning),
            }
        )
        tool_key = f"tc:{graph_id}:{call['id']}"
        tool_row = {
            "key": tool_key,
            "plan_key": plan_key,
            "tool_name": call["name"],
            "args_json": _dump(call_input),
            "status": status,
            "error": error,
            "agent_name": agent_name,
            "source_message_id": message["id"],
            "source_message_role": message["role"],
            "source_message_timestamp": message["timestamp"],
            "source_entry_index": call["block_index"],
            "parent_tool_call_key": "",
            "synthetic": False,
            "observations": [],
        }
        model["tool_calls"].append(tool_row)
        for result_ordinal, result_tuple in enumerate(result_tuples):
            result, result_message, result_index = result_tuple
            obs_key = (
                f"obs:{graph_id}:{call['id']}"
                if len(result_tuples) == 1
                else f"obs:{graph_id}:{call['id']}:{result_ordinal}"
            )
            result_status, result_error = _result_status(result)
            tool_row["observations"].append(
                {
                    "key": obs_key,
                    "tool_call_key": tool_key,
                    "summary": _text(result.get("output"))[:8000],
                    "output_json": _dump(result.get("output")),
                    "status": result_status,
                    "error": result_error,
                    "source_message_id": result_message["id"],
                    "source_message_role": result_message["role"],
                    "source_message_timestamp": result_message["timestamp"],
                    "source_entry_index": result_index,
                }
            )
            subtrace = None
            if call["name"] == "spawn_subagent":
                subtrace = result_message["metadata"].get("sub_agent_trace")
                if not isinstance(subtrace, dict):
                    subtrace = result_message["metadata"].get(
                        "subagent_trace"
                    )
            if isinstance(subtrace, dict):
                _parse_subagent_trace(
                    subtrace,
                    model=model,
                    graph_id=graph_id,
                    parent_task_key=task_key,
                    parent_tool=tool_row,
                    source_message=result_message,
                    depth=1,
                    visited=set(),
                )

    task["status"] = (
        "failed"
        if statuses and all(status == "failed" for status in statuses)
        else "success"
    )


def _parse_subagent_trace(
    trace: dict[str, Any],
    *,
    model: dict[str, Any],
    graph_id: str,
    parent_task_key: str,
    parent_tool: dict[str, Any],
    source_message: dict[str, Any],
    depth: int,
    visited: set[str],
) -> None:
    if depth > 8:
        raise ValueError("subagent trace nesting exceeds maximum depth 8")
    entries = trace.get("entries")
    if not isinstance(entries, list):
        raise ValueError("subagent_trace.entries must be a list")
    agent_name = str(trace.get("agent_name") or "subagent")
    trace_identity = f"{graph_id}|{parent_tool['key']}|{agent_name}"
    child_suffix = _hash(trace_identity, 20)
    if child_suffix in visited:
        raise ValueError(f"cyclic subagent trace detected: {agent_name}")
    visited.add(child_suffix)
    child_task_key = f"task:subagent:{child_suffix}"
    child_status = parent_tool["status"]
    parent_args = _json_object(parent_tool.get("args_json"))
    parent_step = next(
        (
            step
            for step in model["plans"]
            if step["key"] == parent_tool.get("plan_key")
        ),
        {},
    )
    child_goal = _text(
        trace.get("goal")
        or trace.get("task")
        or parent_args.get("task")
        or parent_args.get("goal")
        or parent_args.get("description")
        or parent_step.get("intent")
        or agent_name
    ).strip()
    model["tasks"].append(
        {
            "key": child_task_key,
            "goal": child_goal[:8000],
            "status": child_status,
            "owner_agent": agent_name,
            "task_signature": _hash(trace_identity),
            "created_at": source_message["timestamp"],
            "task_kind": "subagent",
            "parent_task_key": parent_task_key,
            "parent_tool_call_key": parent_tool["key"],
            "graph_id": graph_id,
            "session_key": model["session"]["key"],
            "source_message_id": source_message["id"],
            "source_message_role": source_message["role"],
            "source_message_timestamp": source_message["timestamp"],
            "expected_outcome": str(
                parent_args.get("expected_outcome") or ""
            )[:8000],
        }
    )
    model["spawns"].append(
        {"tool_call_key": parent_tool["key"], "task_key": child_task_key}
    )

    pending_thinking: list[str] = []
    pending_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pending_by_id: dict[str, dict[str, Any]] = {}
    last_tool_by_name: dict[str, dict[str, Any]] = {}
    child_plans: list[dict[str, Any]] = []

    def create_child_call(
        name: str,
        input_value: Any,
        entry_index: int,
        *,
        synthetic: bool,
    ) -> dict[str, Any]:
        plan_index = len(child_plans)
        plan_key = f"step:{child_suffix}:{plan_index}"
        reasoning_entries = list(pending_thinking)
        intent = (
            "\n".join(pending_thinking).strip()
            or _tool_input_intent(name, input_value)
        )
        pending_thinking.clear()
        plan = {
            "key": plan_key,
            "task_key": child_task_key,
            "step_idx": plan_index,
            "source_node_id": f"entry:{entry_index}",
            "intent": intent[:8000],
            "status": "running",
            "tool_hint": name,
            "source_entry_index": entry_index,
            "deps_json": "[]",
            "source_message_id": source_message["id"],
            "source_message_role": source_message["role"],
            "source_message_timestamp": source_message["timestamp"],
            "reasoning_json": _dump(reasoning_entries),
        }
        model["plans"].append(plan)
        child_plans.append(plan)
        tool_key = f"tc:{child_suffix}:{entry_index}"
        tool = {
            "key": tool_key,
            "plan_key": plan_key,
            "tool_name": name,
            "args_json": _dump(input_value if input_value is not None else {}),
            "status": "running",
            "error": "",
            "agent_name": agent_name,
            "source_message_id": source_message["id"],
            "source_message_role": source_message["role"],
            "source_message_timestamp": source_message["timestamp"],
            "source_entry_index": entry_index,
            "parent_tool_call_key": parent_tool["key"],
            "synthetic": synthetic,
            "observations": [],
        }
        model["tool_calls"].append(tool)
        return tool

    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                f"subagent {agent_name} entry {entry_index} is not an object"
            )
        entry_type = str(entry.get("type") or "")
        if entry_type == "thinking":
            pending_thinking.append(str(entry.get("text") or entry.get("thinking") or ""))
            continue
        if entry_type == "tool_call":
            name = str(entry.get("name") or "").strip()
            if not name:
                raise ValueError(
                    f"subagent {agent_name} tool_call {entry_index} has no name"
                )
            tool = create_child_call(
                name, entry.get("input"), entry_index, synthetic=False
            )
            pending_by_name[name].append(tool)
            last_tool_by_name[name] = tool
            source_call_id = str(
                entry.get("id") or entry.get("tool_call_id") or ""
            ).strip()
            if source_call_id:
                pending_by_id[source_call_id] = tool
            continue
        if entry_type != "tool_result":
            continue

        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(
                f"subagent {agent_name} tool_result {entry_index} has no name"
            )
        source_call_id = str(
            entry.get("id") or entry.get("tool_call_id") or ""
        ).strip()
        tool = pending_by_id.pop(source_call_id, None) if source_call_id else None
        pending = pending_by_name.get(name) or []
        if tool is not None:
            if tool in pending:
                pending.remove(tool)
        elif pending:
            tool = pending.pop(0)
        elif name in last_tool_by_name:
            tool = last_tool_by_name[name]
        else:
            # Some host versions emit batched result entries without the
            # corresponding call entries. Preserve the step with a synthetic
            # ToolCall instead of silently dropping it.
            tool = create_child_call(name, {}, entry_index, synthetic=True)
            last_tool_by_name[name] = tool
        status, error = _result_status(entry)
        tool["status"] = status
        tool["error"] = error
        plan = next(plan for plan in child_plans if plan["key"] == tool["plan_key"])
        plan["status"] = status
        obs_key = f"obs:{child_suffix}:{entry_index}"
        tool["observations"].append(
            {
                "key": obs_key,
                "tool_call_key": tool["key"],
                "summary": _text(entry.get("output"))[:8000],
                "output_json": _dump(entry.get("output")),
                "status": status,
                "error": error,
                "source_message_id": source_message["id"],
                "source_message_role": source_message["role"],
                "source_message_timestamp": source_message["timestamp"],
                "source_entry_index": entry_index,
            }
        )

        nested = entry.get("sub_agent_trace") or entry.get("subagent_trace")
        if isinstance(nested, dict):
            _parse_subagent_trace(
                nested,
                model=model,
                graph_id=graph_id,
                parent_task_key=child_task_key,
                parent_tool=tool,
                source_message=source_message,
                depth=depth + 1,
                visited=visited,
            )

    visited.remove(child_suffix)


def _validate_model(model: dict[str, Any]) -> None:
    task_keys = {row["key"] for row in model["tasks"]}
    plan_keys = {row["key"] for row in model["plans"]}
    tool_keys = {row["key"] for row in model["tool_calls"]}
    if len(task_keys) != len(model["tasks"]):
        raise ValueError("duplicate Task key in parsed host trace")
    if len(plan_keys) != len(model["plans"]):
        raise ValueError("duplicate Step key in parsed host trace")
    if len(tool_keys) != len(model["tool_calls"]):
        raise ValueError("duplicate ToolCall key in parsed host trace")
    observation_keys: set[str] = set()
    for plan in model["plans"]:
        if plan["task_key"] not in task_keys:
            raise ValueError(f"orphan Step: {plan['key']}")
    for tool in model["tool_calls"]:
        if tool["plan_key"] not in plan_keys:
            raise ValueError(f"orphan ToolCall: {tool['key']}")
        for observation in tool.get("observations") or []:
            if observation["tool_call_key"] != tool["key"]:
                raise ValueError(f"orphan Observation: {observation['key']}")
            if observation["key"] in observation_keys:
                raise ValueError(
                    f"duplicate Observation key: {observation['key']}"
                )
            observation_keys.add(observation["key"])
    for edge in model["spawns"]:
        if edge["tool_call_key"] not in tool_keys or edge["task_key"] not in task_keys:
            raise ValueError(f"invalid ToolCall SPAWNS edge: {edge}")

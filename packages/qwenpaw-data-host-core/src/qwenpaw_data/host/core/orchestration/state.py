# -*- coding: utf-8 -*-
"""RuntimeStateManager —— QwenPaw Data 运行时状态管理器。

操作自持的 ``_nodes: List[TaskNode]`` 扁平列表，
通过 ``TaskNode.graph_id`` 分组管理 DAG。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
)

from agentscope.tool import ToolChunk
from agentscope.message import TextBlock
from pydantic import ValidationError

from .artifact import ArtifactItem
from .dag_store import DAGStore
from .events import TaskEventType
from .hint import DefaultGraphToHint
from .task_graph import (
    FileRef,
    GraphRegistry,
    NodeOutput,
    PlanNodeChange,
    TaskNode,
    apply_plan_changes,
    get_graph_meta,
    graph_nodes,
    make_node,
)
from ..utils.ids import create_graph_id

FilesInput = Optional[
    List[FileRef] | List[Dict[str, str]] | Dict[str, str] | str
]

logger = logging.getLogger(__name__)


def _text(msg: str) -> ToolChunk:
    """构造纯文本 ToolChunk。"""
    return ToolChunk(content=[TextBlock(type="text", text=msg)])


def _trace_payload(msg: Any) -> Any:
    if hasattr(msg, "to_dict"):
        return msg.to_dict()
    if hasattr(msg, "model_dump"):
        try:
            return msg.model_dump(mode="json")
        except TypeError:
            return msg.model_dump()
    return msg


def _error(msg: str) -> ToolChunk:
    """构造错误 ToolChunk。"""
    return ToolChunk(
        content=[TextBlock(type="text", text=msg)],
        state="error",
        is_last=True,
    )


def _normalize_deps_to_node_ids(nodes: List[TaskNode]) -> str | None:
    """Normalize deps that uniquely reference node names to node ids."""
    ids = {node.id for node in nodes}
    names: dict[str, list[str]] = {}
    for node in nodes:
        names.setdefault(node.name, []).append(node.id)

    for node in nodes:
        normalized: list[str] = []
        for dep in node.deps:
            if dep in ids:
                normalized.append(dep)
                continue

            matched_ids = names.get(dep, [])
            if len(matched_ids) == 1:
                normalized.append(matched_ids[0])
                continue
            if len(matched_ids) > 1:
                return (
                    f"Node '{node.id}' has ambiguous dep {dep!r}; "
                    "deps must reference node_id when node names are duplicated."
                )
            return (
                f"Node '{node.id}' has unknown dep {dep!r}; "
                "deps must reference an existing node_id."
            )
        if node.id in normalized:
            return f"Node '{node.id}' cannot depend on itself."
        node.deps = normalized

    return None


_STATE_COMPAT: dict[str, str] = {
    "pending": "todo",
    "completed": "done",
    "stale": "todo",
}


def _migrate_task_dict(d: dict) -> dict:
    """将旧 agentscope.state.Task 格式转为 TaskNode 格式。"""
    result = dict(d)
    if "subject" in result and "name" not in result:
        result["name"] = result.pop("subject")
    if "blocked_by" in result and "deps" not in result:
        result["deps"] = result.pop("blocked_by")
    meta = result.pop("metadata", {})
    for key in ("graph_id", "expected_outcome", "started_at", "output"):
        if key in meta and key not in result:
            result[key] = meta[key]
    for key in ("blocks", "owner"):
        result.pop(key, None)
    if "state" in result:
        result["state"] = _STATE_COMPAT.get(result["state"], result["state"])
    return result


class RuntimeStateManager:
    """QwenPaw Data 运行时状态管理器（自持 _nodes: List[TaskNode]）。"""

    description: str = (
        "QwenPaw Data task graph management tools. Use these to create a DAG "
        "of analysis tasks (create_plan), track node execution "
        "(update_subtask), revise nodes (revise_current_plan), and "
        "archive the current graph (finish_plan). Ready-to-execute node "
        "hints are injected automatically each reasoning round — follow them."
    )

    def __init__(
        self,
        graph_to_hint: Optional[Callable] = None,
        path_resolver: Callable[[str], Path] | None = None,
    ) -> None:
        self._nodes: List[TaskNode] = []
        self._current_graph_id: str | None = None
        self._trigger_msg_id: str = ""
        self.artifacts: List[ArtifactItem] = []
        self._pending_edits: list[dict] = []
        self._traces: Dict[str, list] = {}
        self._graph_registry: GraphRegistry = {}
        self._path_resolver = path_resolver
        self._graph_to_hint = graph_to_hint or DefaultGraphToHint()

        # DAGStore 持久化
        self._dag_store: Optional[DAGStore] = None
        self._dag_session_id: str = ""

    # ==================================================================
    # Graph node accessors
    # ==================================================================

    def _graph_nodes(self) -> List[TaskNode]:
        if not self._current_graph_id:
            return []
        return graph_nodes(self._nodes, self._current_graph_id)

    @property
    def current_graph_id(self) -> str | None:
        return self._current_graph_id

    def get_current_in_progress_node(self) -> TaskNode | None:
        """Return the single in-progress node for the active graph."""
        for node in self._graph_nodes():
            if node.state == "in_progress":
                return node
        return None

    def get_upstream_outputs(self, node_id: str) -> Dict[str, str]:
        """Return upstream done-node summaries keyed by node id."""
        node_map = {node.id: node for node in self._graph_nodes()}
        node = node_map.get(node_id)
        if node is None:
            return {}

        outputs: Dict[str, str] = {}
        for dep_id in node.deps:
            dep = node_map.get(dep_id)
            if dep is None or dep.state != "done" or dep.output is None:
                continue
            outputs[dep_id] = dep.output.summary
        return outputs

    def configure_dag_store(
        self,
        dag_store: DAGStore,
        *,
        session_id: str,
    ) -> None:
        """Attach the per-session DAG backing store."""
        self._dag_store = dag_store
        self._dag_session_id = session_id

    # ==================================================================
    # trigger_msg_id
    # ==================================================================

    def set_trigger_msg_id(self, msg_id: str) -> None:
        self._trigger_msg_id = msg_id

    # ==================================================================
    # Hook 机制
    # ==================================================================

    async def _notify_graph_change(self, event_type: str) -> None:
        if self._dag_store is not None and self._dag_session_id:
            try:
                await self._dag_store.write(
                    self._dag_session_id,
                    self.state_dict(),
                )
            except Exception:
                logger.warning(
                    "RuntimeStateManager: DAGStore.write failed; continuing",
                    exc_info=True,
                )


    # ==================================================================
    # 前端编辑支持
    # ==================================================================

    def pop_pending_edits(self) -> list[dict]:
        edits = self._pending_edits[:]
        self._pending_edits = []
        return edits

    # ==================================================================
    # 活跃图生命周期管理
    # ==================================================================

    async def _archive_current_graph(self, reason: str = "") -> None:
        """归档当前活跃图。

        nodes 留在 _nodes 列表中。
        "归档"只是清除 _current_graph_id，标记未完成节点为 completed。
        """
        if not self._current_graph_id:
            return

        gn = self._graph_nodes()
        for n in gn:
            if n.state != "done":
                n.state = "done"

        await self._notify_graph_change(TaskEventType.GRAPH_ARCHIVED)
        self._current_graph_id = None

    async def load_graph_from_nodes(
        self,
        graph_id: str,
        new_nodes: List[TaskNode],
    ) -> None:
        """将 nodes 注册为当前活跃图。"""
        await self._archive_current_graph(
            reason=f"Replaced by loaded graph.",
        )
        self._nodes.extend(new_nodes)
        self._current_graph_id = graph_id
        await self._notify_graph_change(TaskEventType.GRAPH_CREATED)

    # ==================================================================
    # 提示生成
    # ==================================================================

    def get_current_hint(self) -> str | None:
        """生成 DAG 状态提示。"""
        return self._graph_to_hint(
            self._nodes, self._current_graph_id, self._graph_registry,
        )

    # ==================================================================
    # Trace 收集
    # ==================================================================

    def append_to_trace(self, msg: Any) -> None:
        """追加消息到当前执行节点的 trace。"""
        if not self._current_graph_id:
            return
        gn = self._graph_nodes()
        for n in gn:
            if n.state == "in_progress":
                trace_entry = _trace_payload(msg)
                self._traces.setdefault(n.id, []).append(trace_entry)
                return

    def trace_context(self) -> dict[str, str | None]:
        """Return current DAG trace context for session-level trace entries."""
        node_id: str | None = None
        if self._current_graph_id:
            for n in self._graph_nodes():
                if n.state == "in_progress":
                    node_id = n.id
                    break
        return {
            "graph_id": self._current_graph_id,
            "node_id": node_id,
        }

    # ==================================================================
    # Artifact 管理
    # ==================================================================

    def set_path_resolver(
        self,
        resolver: Callable[[str], Path] | None,
    ) -> None:
        self._path_resolver = resolver

    def resolve_path(self, path: str) -> Optional[Path]:
        if self._path_resolver is None:
            return None
        try:
            return self._path_resolver(path)
        except Exception:
            logger.warning(
                "RuntimeStateManager: failed to resolve artifact path %r",
                path, exc_info=True,
            )
            return None

    def _stat_size_bytes(self, rel_path: str) -> int:
        if self._path_resolver is None:
            return 0
        try:
            path = self._path_resolver(rel_path)
            return path.stat().st_size
        except Exception:
            logger.warning(
                "RuntimeStateManager: failed to stat artifact path %r",
                rel_path, exc_info=True,
            )
            return 0

    def _record_files(
        self,
        *,
        graph_id: str,
        node_id: str,
        files: Optional[List[FileRef]],
    ) -> int:
        if not files:
            return 0
        count = 0
        for file_ref in files:
            self.artifacts.append(
                ArtifactItem(
                    graph_id=graph_id,
                    node_id=node_id,
                    name=file_ref.name,
                    path=file_ref.path,
                    mime_type=file_ref.mime_type,
                    size_bytes=self._stat_size_bytes(file_ref.path),
                ),
            )
            count += 1
        return count

    def _normalize_files(self, files: FilesInput) -> List[FileRef]:
        if not files:
            return []
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "files must be a JSON array/object when provided as a string.",
                ) from exc
        if isinstance(files, dict):
            files = [files]
        if not isinstance(files, list):
            raise ValueError(
                "files must be a list of FileRef objects, a single FileRef "
                "object, or a JSON string.",
            )
        return [
            f if isinstance(f, FileRef) else FileRef.model_validate(f)
            for f in files
        ]

    # ==================================================================
    # Agent Tools
    # ==================================================================

    async def create_plan(
        self,
        name: str,
        description: str,
        expected_outcome: str,
        nodes: List[Dict[str, Any] | TaskNode],
    ) -> ToolChunk:
        """Create a new analysis task graph (DAG).

        Args:
            name: Short, descriptive graph name (<= 10 words).
            description: Constraints, target, and measurable outcome.
            expected_outcome: Specific, concrete final deliverable.
            nodes: List of node dicts. Each must have name, description,
                expected_outcome, and optionally node_id and deps.
        """
        graph_id = create_graph_id()
        created_nodes: List[TaskNode] = []
        for n in nodes:
            if isinstance(n, TaskNode):
                n.graph_id = graph_id
                created_nodes.append(n)
            else:
                node = make_node(
                    graph_id=graph_id,
                    node_id=n.get("node_id"),
                    name=n.get("name", ""),
                    description=n.get("description", ""),
                    expected_outcome=n.get("expected_outcome", ""),
                    deps=n.get("deps", []),
                    state="todo",
                )
                created_nodes.append(node)

        deps_error = _normalize_deps_to_node_ids(created_nodes)
        if deps_error is not None:
            return _error(deps_error)

        replaced_msg = ""
        if self._current_graph_id:
            old_meta = get_graph_meta(self._graph_registry, self._current_graph_id)
            old_name = old_meta.get("name", "")
            await self._archive_current_graph(
                reason=f"Replaced by a new task graph '{name}'.",
            )
            replaced_msg = f"The previous graph '{old_name}' was archived. "

        self._current_graph_id = graph_id
        self._graph_registry[graph_id] = {
            "id": graph_id,
            "name": name,
            "description": description,
            "expected_outcome": expected_outcome,
            "anchor_message_id": self._trigger_msg_id,
        }
        self._nodes.extend(created_nodes)

        await self._notify_graph_change(TaskEventType.GRAPH_CREATED)

        return _text(
            f"{replaced_msg}Task graph '{name}' created with "
            f"{len(created_nodes)} node(s). Graph ID: {graph_id}",
        )

    async def update_subtask(
        self,
        node_id: str,
        state: Literal[
            "todo", "in_progress", "done",
            "failed", "abandoned",
        ],
        *,
        reasoning: Optional[str] = None,
        summary: Optional[str] = None,
        files: FilesInput = None,
    ) -> ToolChunk:
        """Update the status of a DAG node.

        Args:
            node_id: ID of the node to update.
            state: Target state (todo/in_progress/done/failed/abandoned).
            reasoning: How the work was done (required when state='done').
            summary: What the result is (required when state='done').
            files: Optional list of FileRef objects (used when state='done').
        """
        if not self._current_graph_id:
            return _text("No active task graph. Call create_plan first.")

        gn = self._graph_nodes()
        node_map = {n.id: n for n in gn}
        node = node_map.get(node_id)
        if node is None:
            return _text(f"Node '{node_id}' not found.")

        if state == "done":
            if not reasoning or not summary:
                return _text(
                    "state='done' requires both 'reasoning' and 'summary'.",
                )
            try:
                file_refs = self._normalize_files(files)
            except (ValueError, ValidationError) as exc:
                return _text(
                    "Invalid files argument. Use files as a structured array: "
                    '[{"name": "result.csv", "path": "...", '
                    '"mime_type": "text/csv"}]. '
                    f"Details: {exc}",
                )

            node.state = "done"
            node.output = NodeOutput(
                reasoning=reasoning,
                summary=summary,
                files=file_refs,
            )

            file_count = self._record_files(
                graph_id=self._current_graph_id,
                node_id=node_id,
                files=file_refs,
            )

            await self._notify_graph_change(TaskEventType.GRAPH_UPDATED)
            return _text(
                f"Node '{node_id}' marked as done. "
                f"Recorded {file_count} file(s).",
            )

        if state == "in_progress" and node.state != "in_progress":
            existing_in_progress = [
                n.id for n in gn
                if n.state == "in_progress" and n.id != node_id
            ]
            if existing_in_progress:
                return _text(
                    f"已有节点 {existing_in_progress} 正在执行。"
                    f"请先完成当前节点再开始执行节点 '{node_id}'。"
                )

        if state == "in_progress":
            self._traces.pop(node_id, None)
            node.state = "in_progress"
            node.started_at = datetime.now().isoformat()
        elif state == "todo":
            node.state = "todo"
            node.started_at = None
        else:
            node.state = state

        await self._notify_graph_change(TaskEventType.GRAPH_UPDATED)
        return _text(f"Node '{node_id}' marked as '{state}'.")

    async def revise_current_plan(
        self,
        changes: List[PlanNodeChange | Dict[str, Any]],
    ) -> ToolChunk:
        """Apply one or more node mutations to the active graph.

        Args:
            changes: List of add / revise / delete operations. The batch is
                applied atomically; if any change is invalid, nothing is
                modified.
        """
        if not self._current_graph_id:
            return _text("No active task graph.")

        try:
            result = apply_plan_changes(
                self._nodes,
                self._current_graph_id,
                changes,
            )
        except ValueError as exc:
            return _text(str(exc))

        await self._notify_graph_change(TaskEventType.GRAPH_UPDATED)

        parts = [f"Applied {len(changes)} change(s)."]
        if result.added:
            parts.append(f"Added: {result.added}.")
        if result.revised:
            parts.append(f"Revised: {result.revised}.")
        if result.deleted:
            parts.append(f"Deleted: {result.deleted}.")
        if result.downstream_reset:
            parts.append(f"Downstream reset to todo: {result.downstream_reset}.")
        return _text(" ".join(parts))

    async def finish_plan(
        self,
        state: Literal["done", "abandoned"],
        outcome: str,
    ) -> ToolChunk:
        """Finish the active graph with an outcome.

        Args:
            state: 'done' or 'abandoned'.
            outcome: Final report summary.
        """
        if not self._current_graph_id:
            return _text("No active task graph to finish.")

        gn = self._graph_nodes()
        for n in gn:
            if n.state not in ("done", "failed", "abandoned"):
                n.state = state

        await self._notify_graph_change(TaskEventType.GRAPH_FINISHED)
        self._current_graph_id = None
        return _text("Task graph finished.")

    # ==================================================================
    # 序列化
    # ==================================================================

    def state_dict(self) -> dict:
        """序列化为完整快照（供 DAGStore 持久化）。"""
        nodes_data = [n.model_dump(mode="json") for n in self._nodes]
        return {
            "current_graph_id": self._current_graph_id,
            "graph_registry": dict(self._graph_registry),
            "artifacts": [item.model_dump(mode="json") for item in self.artifacts],
            "_pending_edits": self._pending_edits,
            "traces": self._traces,
            "nodes": nodes_data,
        }

    def load_state_dict(self, data: dict, strict: bool = True) -> None:
        """从快照恢复状态（含 nodes）。"""
        self._current_graph_id = data.get("current_graph_id")
        self._graph_registry = data.get("graph_registry") or {}
        artifacts_raw = data.get("artifacts", [])
        self.artifacts = [
            ArtifactItem.model_validate(item) for item in (artifacts_raw or [])
        ]
        self._pending_edits = data.get("_pending_edits", [])
        traces = data.get("traces", {})
        self._traces = traces if isinstance(traces, dict) else {}

        nodes_raw = data.get("nodes")
        if nodes_raw is None:
            # Legacy format: migrate from agentscope.state.Task dicts
            tasks_raw = data.get("tasks", [])
            nodes_raw = [
                _migrate_task_dict(d) for d in tasks_raw if isinstance(d, dict)
            ]

        restored: List[TaskNode] = []
        for d in nodes_raw:
            if isinstance(d, dict):
                try:
                    restored.append(TaskNode.model_validate(_migrate_task_dict(d)))
                except Exception:
                    logger.warning(
                        "RuntimeStateManager: skip malformed node in snapshot",
                        exc_info=True,
                    )
        self._nodes = restored

    def restore_state(self, state: Dict[str, Any]) -> None:
        """兼容旧接口。"""
        self.load_state_dict(state, strict=False)

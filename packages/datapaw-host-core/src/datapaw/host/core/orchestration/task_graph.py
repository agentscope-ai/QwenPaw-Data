# -*- coding: utf-8 -*-
"""DataPaw DAG 数据模型 + TaskNode helper 函数。

DAG 节点用独立的 ``TaskNode(BaseModel)`` 表示，
状态使用五态（``todo`` / ``in_progress`` / ``done`` / ``failed`` / ``abandoned``）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from ..utils.ids import create_graph_id, create_node_id


# ---------------------------------------------------------------------------
# FileRef
# ---------------------------------------------------------------------------


class FileRef(BaseModel):
    """节点产出的文件引用（图表/Excel/PDF 等）。"""

    name: str = Field(description="文件名，如 ``dau_trend.png``")
    path: str = Field(
        description="相对当前 session artifacts 根的文件路径",
    )
    mime_type: str = Field(description="MIME 类型，如 ``image/png``")


# ---------------------------------------------------------------------------
# TaskNode — 独立 DAG 节点模型
# ---------------------------------------------------------------------------

NodeStatus = Literal[
    "todo",
    "in_progress",
    "done",
    "failed",
    "abandoned",
]

PlanChangeAction = Literal["add", "revise", "delete"]


class NodeOutput(BaseModel):
    """已完成节点的类型化输出。"""

    reasoning: str = ""
    summary: str = ""
    files: List[FileRef] = Field(default_factory=list)


class TaskNode(BaseModel):
    """DAG 执行单元 — 独立于 agentscope.state.Task。"""

    id: str = Field(default_factory=create_node_id)
    graph_id: str
    name: str
    description: str = ""
    expected_outcome: str = ""
    state: NodeStatus = "todo"
    deps: List[str] = Field(default_factory=list)
    started_at: Optional[str] = None
    output: Optional[NodeOutput] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


GraphMeta = Dict[str, Any]
GraphRegistry = Dict[str, GraphMeta]


# ---------------------------------------------------------------------------
# TaskNode 创建 helper
# ---------------------------------------------------------------------------


def make_node(
    *,
    graph_id: str,
    node_id: str | None = None,
    name: str,
    description: str,
    expected_outcome: str,
    deps: List[str] | None = None,
    state: NodeStatus = "todo",
) -> TaskNode:
    """创建一个 DataPaw DAG 节点。"""
    return TaskNode(
        id=node_id or create_node_id(),
        graph_id=graph_id,
        name=name,
        description=description,
        expected_outcome=expected_outcome,
        deps=list(deps or []),
        state=state,
    )


# ---------------------------------------------------------------------------
# Graph 过滤 / 元数据 helper
# ---------------------------------------------------------------------------


def graph_nodes(nodes: List[TaskNode], graph_id: str) -> List[TaskNode]:
    """过滤出指定 graph 的所有节点。"""
    return [n for n in nodes if n.graph_id == graph_id]


def get_graph_meta(registry: GraphRegistry, graph_id: str) -> GraphMeta:
    """从 registry 获取 graph-level 元数据。"""
    return registry.get(graph_id, {"id": graph_id})


def set_graph_meta(
    registry: GraphRegistry,
    graph_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    expected_outcome: str | None = None,
    anchor_message_id: str | None = None,
) -> None:
    """更新 registry 中的 graph-level 元数据。"""
    meta = registry.setdefault(graph_id, {"id": graph_id})
    if name is not None:
        meta["name"] = name
    if description is not None:
        meta["description"] = description
    if expected_outcome is not None:
        meta["expected_outcome"] = expected_outcome
    if anchor_message_id is not None:
        meta["anchor_message_id"] = anchor_message_id


def list_graph_ids(nodes: List[TaskNode]) -> List[str]:
    """列举所有不同的 graph_id（按首次出现顺序）。"""
    seen: set[str] = set()
    result: List[str] = []
    for n in nodes:
        gid = n.graph_id
        if gid and gid not in seen:
            seen.add(gid)
            result.append(gid)
    return result


_TERMINAL_STATES: frozenset[str] = frozenset({"done", "failed", "abandoned"})


def is_graph_done(nodes: List[TaskNode], graph_id: str) -> bool:
    """graph 中所有节点均为终态（done / failed / abandoned）。"""
    gn = graph_nodes(nodes, graph_id)
    if not gn:
        return False
    return all(n.state in _TERMINAL_STATES for n in gn)


# ---------------------------------------------------------------------------
# DAG 调度 helper
# ---------------------------------------------------------------------------


def get_ready_nodes(nodes: List[TaskNode], graph_id: str) -> List[TaskNode]:
    """返回就绪节点（todo 且依赖全部 done）。"""
    gn = graph_nodes(nodes, graph_id)
    node_map = {n.id: n for n in gn}
    ready: List[TaskNode] = []
    for n in gn:
        if n.state != "todo":
            continue
        deps_satisfied = all(
            node_map.get(dep_id) is not None
            and node_map[dep_id].state == "done"
            for dep_id in n.deps
        )
        if deps_satisfied:
            ready.append(n)
    return ready


# ---------------------------------------------------------------------------
# DAG 变更 / 传播 helper
# ---------------------------------------------------------------------------


def _clone_node(node: TaskNode) -> TaskNode:
    return TaskNode.model_validate(node.model_dump(mode="json"))


def _node_payload(node: Any) -> Dict[str, Any]:
    if isinstance(node, TaskNode):
        return node.model_dump(mode="json")
    if isinstance(node, dict):
        return dict(node)
    raise ValueError("node must be a mapping.")


def _make_change_node(
    *,
    graph_id: str,
    node_id: str,
    action: PlanChangeAction,
    node: Any,
) -> TaskNode:
    payload = _node_payload(node)
    if action == "add" and "node_id" in payload and payload["node_id"] != node_id:
        raise ValueError(
            f"add node_id mismatch: change.node_id={node_id!r} "
            f"vs node.node_id={payload['node_id']!r}.",
        )

    missing = [
        field
        for field in ("name", "description", "expected_outcome", "deps")
        if field not in payload
    ]
    if missing:
        raise ValueError(
            f"action='{action}' requires node field(s): {missing}. "
            "Include name, description, expected_outcome, and deps.",
        )

    return make_node(
        graph_id=graph_id,
        node_id=node_id,
        name=payload["name"],
        description=payload["description"],
        expected_outcome=payload["expected_outcome"],
        deps=list(payload.get("deps") or []),
        state="todo",
    )


def _normalize_deps_to_node_ids_in_map(nodes_by_id: Dict[str, TaskNode]) -> None:
    """Normalize deps that uniquely reference node names to node ids."""
    ids = set(nodes_by_id.keys())
    names: dict[str, list[str]] = {}
    for node in nodes_by_id.values():
        names.setdefault(node.name, []).append(node.id)

    for node in nodes_by_id.values():
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
                raise ValueError(
                    f"Invalid dependency: node '{node.id}' has ambiguous "
                    f"dep {dep!r}; deps must reference node_id when node "
                    "names are duplicated.",
                )
            raise ValueError(
                f"Invalid dependency: node '{node.id}' references unknown "
                f"deps {[dep]}. Every dep must be an existing node_id in "
                "the graph after this batch is applied.",
            )
        node.deps = normalized


def _validate_graph_topology(nodes_by_id: Dict[str, TaskNode]) -> None:
    """校验批量变更后的 deps 引用和有向无环拓扑。"""
    if not nodes_by_id:
        return

    node_ids = set(nodes_by_id.keys())
    for node_id, node in nodes_by_id.items():
        if node_id in node.deps:
            raise ValueError(
                f"Invalid dependency: node '{node_id}' cannot list itself "
                "in deps.",
            )
        unknown_deps = set(node.deps) - node_ids
        if unknown_deps:
            raise ValueError(
                f"Invalid dependency: node '{node_id}' references unknown "
                f"deps {sorted(unknown_deps)}. Every dep must be an "
                "existing node_id in the graph after this batch is applied.",
            )

    in_degree: Dict[str, int] = {node_id: 0 for node_id in node_ids}
    adjacency: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
    for node_id, node in nodes_by_id.items():
        for dep in node.deps:
            adjacency[dep].append(node_id)
            in_degree[node_id] += 1

    queue = [node_id for node_id, degree in in_degree.items() if degree == 0]
    visited_count = 0
    while queue:
        current = queue.pop(0)
        visited_count += 1
        for downstream in adjacency.get(current, []):
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                queue.append(downstream)

    if visited_count != len(nodes_by_id):
        cyclic_nodes = sorted(
            node_id for node_id, degree in in_degree.items() if degree > 0
        )
        raise ValueError(
            "Invalid topology: task graph would contain a cycle after "
            "applying these changes. Check deps for circular references. "
            f"Nodes involved: {cyclic_nodes}.",
        )


def find_downstream_node_ids(
    nodes: List[TaskNode],
    graph_id: str,
    node_id: str,
) -> List[str]:
    """返回当前 graph 中某节点的所有下游节点 ID（深度优先）。"""
    gn = graph_nodes(nodes, graph_id)
    downstream: List[str] = []
    visited: set[str] = set()

    def visit(current: str) -> None:
        for node in gn:
            if current in node.deps and node.id not in visited:
                visited.add(node.id)
                downstream.append(node.id)
                visit(node.id)

    visit(node_id)
    return downstream


def reset_node_to_todo(node: TaskNode) -> bool:
    """将节点重置为 todo；返回状态/运行态是否发生变化。"""
    changed = (
        node.state != "todo"
        or node.started_at is not None
        or node.output is not None
    )
    node.state = "todo"
    node.started_at = None
    node.output = None
    return changed


def mark_downstream_todo(
    nodes: List[TaskNode],
    graph_id: str,
    node_id: str,
) -> List[str]:
    """将下游非 abandoned 节点重置为 todo；返回实际重置的节点 ID。"""
    node_map = {node.id: node for node in graph_nodes(nodes, graph_id)}
    reset_ids: List[str] = []
    for downstream_id in find_downstream_node_ids(nodes, graph_id, node_id):
        node = node_map[downstream_id]
        if node.state == "abandoned":
            continue
        if reset_node_to_todo(node):
            reset_ids.append(downstream_id)
    return reset_ids


# ---------------------------------------------------------------------------
# 渲染 helper
# ---------------------------------------------------------------------------


def node_to_markdown(node: TaskNode, detailed: bool = False) -> str:
    """将单个 TaskNode 渲染为 Markdown 行。"""
    status_map = {
        "todo": "- [ ] ",
        "in_progress": "- [ ] [WIP]",
        "done": "- [x] ",
        "failed": "- [!] ",
        "abandoned": "- [-] ",
    }
    prefix = status_map.get(node.state, "- [ ] ")
    header = f"{prefix}{node.name} (`{node.id}`)"

    if not detailed:
        return header

    lines = [
        header,
        f"\t- State: {node.state}",
        f"\t- Deps: {node.deps or '[]'}",
        f"\t- Description: {node.description}",
        f"\t- Expected Outcome: {node.expected_outcome}",
    ]
    if node.started_at:
        lines.append(f"\t- Started At: {node.started_at}")
    if node.state == "done" and node.output:
        lines.append(f"\t- Reasoning: {node.output.reasoning}")
        lines.append(f"\t- Summary: {node.output.summary}")
        if node.output.files:
            files_desc = ", ".join(f.name for f in node.output.files)
            lines.append(f"\t- Files: {files_desc}")
    return "\n".join(lines)


def graph_to_markdown(
    nodes: List[TaskNode],
    graph_id: str,
    registry: GraphRegistry | None = None,
) -> str:
    """将 graph 渲染为 Markdown。"""
    gn = graph_nodes(nodes, graph_id)
    meta = get_graph_meta(registry or {}, graph_id)

    header = [
        f"# {meta.get('name', '(unnamed)')}",
        f"**ID**: `{graph_id}`",
        f"**Anchor Message ID**: `{meta.get('anchor_message_id') or '(none)'}`",
        f"**Description**: {meta.get('description', '')}",
        f"**Expected Outcome**: {meta.get('expected_outcome', '')}",
        "## Nodes",
    ]
    node_lines = [node_to_markdown(n) for n in gn]
    return "\n".join(header + node_lines)


# ---------------------------------------------------------------------------
# SOP 最小契约常量
# ---------------------------------------------------------------------------

_SOP_GRAPH_FIELDS: tuple = ("name", "description", "expected_outcome")
_SOP_NODE_FIELDS: tuple = (
    "node_id", "name", "description",
    "expected_outcome", "deps",
)
_SOP_GRAPH_ALLOWED: frozenset = frozenset(set(_SOP_GRAPH_FIELDS) | {"nodes"})
_SOP_GRAPH_FORBIDDEN: frozenset = frozenset({
    "id", "anchor_message_id", "created_at", "finished_at",
    "state",
})
_SOP_NODE_ALLOWED: frozenset = frozenset(_SOP_NODE_FIELDS)
_SOP_NODE_FORBIDDEN: frozenset = frozenset({
    "state", "output", "error", "started_at", "finished_at",
    "trace",
})

_DAG_TOP_RUNTIME_IGNORED: frozenset = frozenset({
    "id", "anchor_message_id", "created_at", "finished_at",
    "state",
})
_DAG_NODE_RUNTIME_IGNORED: frozenset = frozenset({
    "created_at", "output", "error", "started_at", "finished_at",
    "trace",
})
_DAG_NODE_ALLOWED: frozenset = frozenset(set(_SOP_NODE_FIELDS) | {"state"})


def _normalize_and_validate_nodes(
    cleaned_nodes: List[Dict[str, Any]],
    *,
    allowed_fields: frozenset,
    label: str,
) -> List[Dict[str, Any]]:
    """节点去重、字段白名单校验、deps 引用校验、Kahn 环检测。"""
    processed: List[Dict[str, Any]] = []
    for idx, node in enumerate(cleaned_nodes):
        unknown = set(node.keys()) - allowed_fields
        if unknown:
            raise ValueError(
                f"Node at index {idx} contains unknown field(s): "
                f"{sorted(unknown)}. "
                f"Allowed: {sorted(allowed_fields)}.",
            )

        nid = node.get("node_id") or f"node_{idx:03d}"
        if any(existing["node_id"] == nid for existing in processed):
            raise ValueError(f"Duplicate node_id in {label}: {nid!r}.")
        n = dict(node)
        n["node_id"] = nid
        n.setdefault("deps", [])
        processed.append(n)

    node_ids = {n["node_id"] for n in processed}
    for n in processed:
        unknown_deps = set(n["deps"]) - node_ids
        if unknown_deps:
            raise ValueError(
                f"Node '{n['node_id']}' has unknown deps: {sorted(unknown_deps)}.",
            )

    # Kahn 算法检测环
    in_degree: Dict[str, int] = {n["node_id"]: 0 for n in processed}
    adjacency: Dict[str, List[str]] = {n["node_id"]: [] for n in processed}
    for n in processed:
        for dep in n["deps"]:
            adjacency[dep].append(n["node_id"])
            in_degree[n["node_id"]] += 1

    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    visited_count = 0
    while queue:
        cur = queue.pop(0)
        visited_count += 1
        for downstream in adjacency.get(cur, []):
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                queue.append(downstream)

    if visited_count != len(processed):
        raise ValueError(
            f"{label} contains a cycle. "
            "Check 'deps' fields for circular references.",
        )

    return processed


def _validate_sop_dict(data: dict) -> List[Dict[str, Any]]:
    """校验 SOP dict，返回处理后的节点列表。"""
    if not isinstance(data, dict):
        raise ValueError("SOP must be a mapping.")

    forbidden_top = set(data.keys()) & _SOP_GRAPH_FORBIDDEN
    if forbidden_top:
        raise ValueError(
            f"SOP contains forbidden runtime field(s) at graph level: "
            f"{sorted(forbidden_top)}. "
            f"Remove them — these are auto-generated by the system.",
        )

    unknown_top = set(data.keys()) - _SOP_GRAPH_ALLOWED
    if unknown_top:
        raise ValueError(
            f"SOP contains unknown field(s) at graph level: "
            f"{sorted(unknown_top)}. "
            f"Allowed: {sorted(_SOP_GRAPH_ALLOWED)}.",
        )

    nodes_raw = data.get("nodes", [])
    if not isinstance(nodes_raw, list):
        raise ValueError("SOP 'nodes' must be a list.")

    cleaned: List[Dict[str, Any]] = []
    for idx, node in enumerate(nodes_raw):
        if not isinstance(node, dict):
            raise ValueError(f"Node at index {idx} is not a mapping: {node!r}")
        forbidden_node = set(node.keys()) & _SOP_NODE_FORBIDDEN
        if forbidden_node:
            raise ValueError(
                f"Node at index {idx} contains forbidden runtime field(s): "
                f"{sorted(forbidden_node)}. Remove them.",
            )
        cleaned.append(node)

    return _normalize_and_validate_nodes(
        cleaned, allowed_fields=_SOP_NODE_ALLOWED, label="SOP",
    )


def _validate_dag_dict(data: dict) -> List[Dict[str, Any]]:
    """校验 DAG patch dict，返回处理后的节点列表。"""
    if not isinstance(data, dict):
        raise ValueError("DAG must be a mapping.")

    clean_data = {
        k: v for k, v in data.items() if k not in _DAG_TOP_RUNTIME_IGNORED
    }
    unknown_top = set(clean_data.keys()) - _SOP_GRAPH_ALLOWED
    if unknown_top:
        raise ValueError(
            f"DAG contains unknown field(s) at graph level: "
            f"{sorted(unknown_top)}. Allowed: {sorted(_SOP_GRAPH_ALLOWED)}.",
        )

    nodes_raw = clean_data.get("nodes", [])
    if not isinstance(nodes_raw, list):
        raise ValueError("DAG 'nodes' must be a list.")

    cleaned: List[Dict[str, Any]] = []
    for idx, node in enumerate(nodes_raw):
        if not isinstance(node, dict):
            raise ValueError(f"Node at index {idx} is not a mapping: {node!r}")
        clean_node = {
            k: v for k, v in node.items() if k not in _DAG_NODE_RUNTIME_IGNORED
        }
        if "state" in clean_node and clean_node["state"] != "todo":
            if set(node.keys()) & _DAG_NODE_RUNTIME_IGNORED:
                clean_node.pop("state", None)
            else:
                raise ValueError(
                    f"Node at index {idx} has invalid state "
                    f"{clean_node['state']!r}; only 'todo' is allowed.",
                )
        cleaned.append(clean_node)

    return _normalize_and_validate_nodes(
        cleaned, allowed_fields=_DAG_NODE_ALLOWED, label="DAG",
    )


# ---------------------------------------------------------------------------
# SOP / SOPNode — 纯结构 Pydantic 类型（无运行态）
# ---------------------------------------------------------------------------


class SOPNode(BaseModel):
    """SOP 节点 — 纯结构，无任何运行态。"""

    node_id: Optional[str] = None
    name: str
    description: str
    expected_outcome: str
    deps: List[str] = Field(default_factory=list)


class SOP(BaseModel):
    """SOP — 任务结构，无运行态字段。"""

    name: str
    description: str
    expected_outcome: str
    nodes: List[SOPNode] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "SOP":
        """严格校验：拒绝运行态字段、未知字段、未知 deps、有环。"""
        _validate_sop_dict(data)
        nodes_raw = data.get("nodes", [])
        sop_nodes: List[SOPNode] = []
        for idx, n in enumerate(nodes_raw):
            nid = n.get("node_id") or f"node_{idx:03d}"
            sop_nodes.append(
                SOPNode(
                    node_id=nid,
                    name=n["name"],
                    description=n["description"],
                    expected_outcome=n["expected_outcome"],
                    deps=n.get("deps", []),
                )
            )
        return cls(
            name=data["name"],
            description=data["description"],
            expected_outcome=data["expected_outcome"],
            nodes=sop_nodes,
        )

    @classmethod
    def from_yaml(cls, yaml_text: str) -> "SOP":
        """从 YAML 字符串解析 SOP。"""
        raw: Any = yaml.safe_load(yaml_text)
        if not isinstance(raw, dict):
            raise ValueError("SOP YAML root must be a mapping.")
        return cls.from_dict(raw)

    def to_dict(self) -> dict:
        """导出符合 SOP 最小契约的字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "expected_outcome": self.expected_outcome,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "name": n.name,
                    "description": n.description,
                    "expected_outcome": n.expected_outcome,
                    "deps": list(n.deps),
                }
                for n in self.nodes
            ],
        }

    def to_yaml(self) -> str:
        """导出 SOP YAML。"""
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# DAG / DAGNode — 用户上传 DAG patch schema
# ---------------------------------------------------------------------------


class DAGNode(SOPNode):
    """DAG patch 节点 — SOP 字段 + 用户可覆写的 state。"""

    state: Optional[Literal["todo"]] = None


class DAG(BaseModel):
    """DAG patch — 结构字段 + 节点级 state，忽略只读运行态字段。"""

    name: str
    description: str
    expected_outcome: str
    nodes: List[DAGNode] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "DAG":
        """解析 DAG patch dict，支持 GET /dag 后直接 round-trip。"""
        processed_nodes = _validate_dag_dict(data)
        clean_data = {
            k: v for k, v in data.items() if k not in _DAG_TOP_RUNTIME_IGNORED
        }
        dag_nodes: List[DAGNode] = []
        for n in processed_nodes:
            kwargs = {
                "node_id": n["node_id"],
                "name": n["name"],
                "description": n["description"],
                "expected_outcome": n["expected_outcome"],
                "deps": n.get("deps", []),
            }
            if "state" in n:
                kwargs["state"] = n["state"]
            dag_nodes.append(DAGNode(**kwargs))
        return cls(
            name=clean_data["name"],
            description=clean_data["description"],
            expected_outcome=clean_data["expected_outcome"],
            nodes=dag_nodes,
        )

    @classmethod
    def from_yaml(cls, yaml_text: str) -> "DAG":
        """从 YAML 字符串解析 DAG patch。"""
        raw: Any = yaml.safe_load(yaml_text)
        if not isinstance(raw, dict):
            raise ValueError("DAG YAML root must be a mapping.")
        return cls.from_dict(raw)

    def to_dict(self) -> dict:
        """导出 DAG patch 字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "expected_outcome": self.expected_outcome,
            "nodes": [
                {
                    **{
                        "node_id": n.node_id,
                        "name": n.name,
                        "description": n.description,
                        "expected_outcome": n.expected_outcome,
                        "deps": list(n.deps),
                    },
                    **({"state": n.state} if "state" in n.model_fields_set else {}),
                }
                for n in self.nodes
            ],
        }


class PlanNodeChange(BaseModel):
    """``revise_current_plan`` 的单条节点变更。"""

    node_id: str = Field(description="Target node_id.")
    action: PlanChangeAction = Field(description="'add' / 'revise' / 'delete'.")
    node: Optional[Any] = Field(
        default=None,
        description="Required for 'add' and 'revise'.",
    )


class ApplyPlanChangesResult(BaseModel):
    """批量任务图变更结果。"""

    added: List[str] = Field(default_factory=list)
    revised: List[str] = Field(default_factory=list)
    deleted: List[str] = Field(default_factory=list)
    downstream_reset: List[str] = Field(default_factory=list)


class DAGDiff(BaseModel):
    """DAG merge 差异摘要。"""

    added: List[str] = Field(default_factory=list)
    removed: List[str] = Field(default_factory=list)
    modified: List[str] = Field(default_factory=list)
    state_overridden: List[str] = Field(default_factory=list)
    downstream_reset: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SOP / DAG 桥接 helper
# ---------------------------------------------------------------------------


def sop_to_nodes(sop: SOP | Dict[str, Any] | str) -> tuple[str, List[TaskNode], GraphMeta]:
    """从 SOP 创建 TaskNode 列表，返回 (graph_id, nodes, graph_meta)。

    入参兼容 SOP / dict / YAML str。
    """
    if isinstance(sop, SOP):
        raw_data = sop.to_dict()
    elif isinstance(sop, str):
        raw: Any = yaml.safe_load(sop)
        if not isinstance(raw, dict):
            raise ValueError("SOP YAML root must be a mapping.")
        raw_data = raw
    else:
        raw_data = sop

    processed_nodes = _validate_sop_dict(raw_data)

    graph_id = create_graph_id()
    graph_meta: GraphMeta = {
        "id": graph_id,
        "name": raw_data.get("name", ""),
        "description": raw_data.get("description", ""),
        "expected_outcome": raw_data.get("expected_outcome", ""),
    }

    nodes: List[TaskNode] = []
    for n in processed_nodes:
        node = make_node(
            graph_id=graph_id,
            node_id=n["node_id"],
            name=n["name"],
            description=n["description"],
            expected_outcome=n["expected_outcome"],
            deps=n.get("deps", []),
            state="todo",
        )
        nodes.append(node)
    return graph_id, nodes, graph_meta


def nodes_to_sop_yaml(
    nodes: List[TaskNode],
    graph_id: str,
    registry: GraphRegistry | None = None,
) -> str:
    """导出 SOP YAML（纯结构，不含运行态）。"""
    sop = nodes_to_sop(nodes, graph_id, registry)
    return sop.to_yaml()


def nodes_to_sop(
    nodes: List[TaskNode],
    graph_id: str,
    registry: GraphRegistry | None = None,
) -> SOP:
    """从 TaskNode 列表构建 SOP 对象。"""
    gn = graph_nodes(nodes, graph_id)
    meta = get_graph_meta(registry or {}, graph_id)
    sop_nodes = [
        SOPNode(
            node_id=n.id,
            name=n.name,
            description=n.description,
            expected_outcome=n.expected_outcome,
            deps=list(n.deps),
        )
        for n in gn
    ]
    return SOP(
        name=meta.get("name", ""),
        description=meta.get("description", ""),
        expected_outcome=meta.get("expected_outcome", ""),
        nodes=sop_nodes,
    )


def nodes_to_dag_dict(
    nodes: List[TaskNode],
    graph_id: str,
    traces: Dict[str, list] | None = None,
    registry: GraphRegistry | None = None,
) -> dict:
    """将 TaskNode 列表转换为 DAG dict（结构 + 运行态），供 GET /dag 序列化。"""
    gn = graph_nodes(nodes, graph_id)
    meta = get_graph_meta(registry or {}, graph_id)

    nodes_list = []
    for n in gn:
        node_data: Dict[str, Any] = {
            "node_id": n.id,
            "name": n.name,
            "description": n.description,
            "expected_outcome": n.expected_outcome,
            "deps": list(n.deps),
            "state": n.state,
        }
        if n.started_at:
            node_data["started_at"] = n.started_at
        if n.output:
            node_data["output"] = n.output.model_dump(mode="json")
        if traces and n.id in traces:
            node_data["trace"] = traces[n.id]
        nodes_list.append(node_data)

    return {
        "id": graph_id,
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "expected_outcome": meta.get("expected_outcome", ""),
        "anchor_message_id": meta.get("anchor_message_id", ""),
        "nodes": nodes_list,
    }


def nodes_to_dag_yaml(
    nodes: List[TaskNode],
    graph_id: str,
    traces: Dict[str, list] | None = None,
    registry: GraphRegistry | None = None,
) -> str:
    """导出 DAG YAML。"""
    return yaml.safe_dump(
        nodes_to_dag_dict(nodes, graph_id, traces, registry),
        allow_unicode=True,
        sort_keys=False,
    )


def _normalize_plan_changes(
    changes: List[PlanNodeChange | Dict[str, Any]],
) -> List[PlanNodeChange]:
    normalized: List[PlanNodeChange] = []
    for change in changes:
        if isinstance(change, PlanNodeChange):
            normalized.append(change)
        else:
            normalized.append(PlanNodeChange.model_validate(change))
    return normalized


def _simulate_plan_changes(
    nodes: List[TaskNode],
    graph_id: str,
    changes: List[PlanNodeChange],
) -> Dict[str, TaskNode]:
    simulated: Dict[str, TaskNode] = {
        node.id: _clone_node(node) for node in graph_nodes(nodes, graph_id)
    }

    for change in changes:
        if change.action != "delete":
            continue
        node_id = change.node_id
        simulated.pop(node_id, None)
        for other in simulated.values():
            if node_id in other.deps:
                other.deps = [dep for dep in other.deps if dep != node_id]

    for change in changes:
        if change.action != "add":
            continue
        assert change.node is not None
        simulated[change.node_id] = _make_change_node(
            graph_id=graph_id,
            node_id=change.node_id,
            action=change.action,
            node=change.node,
        )

    for change in changes:
        if change.action != "revise":
            continue
        assert change.node is not None
        simulated[change.node_id] = _make_change_node(
            graph_id=graph_id,
            node_id=change.node_id,
            action=change.action,
            node=change.node,
        )

    _normalize_deps_to_node_ids_in_map(simulated)
    _validate_graph_topology(simulated)
    return simulated


def apply_plan_changes(
    nodes: List[TaskNode],
    graph_id: str,
    changes: List[PlanNodeChange | Dict[str, Any]],
) -> ApplyPlanChangesResult:
    """原子应用 ``revise_current_plan`` 的批量节点变更。"""
    normalized = _normalize_plan_changes(changes)
    if not normalized:
        raise ValueError("changes must not be empty.")

    seen_ids: set[str] = set()
    for change in normalized:
        if change.node_id in seen_ids:
            raise ValueError(f"Duplicate node_id in changes: {change.node_id!r}.")
        seen_ids.add(change.node_id)
        if change.action in ("add", "revise") and change.node is None:
            raise ValueError(
                f"action='{change.action}' requires a 'node' argument.",
            )

    deletes = [change for change in normalized if change.action == "delete"]
    adds = [change for change in normalized if change.action == "add"]
    revises = [change for change in normalized if change.action == "revise"]

    existing_ids = {node.id for node in graph_nodes(nodes, graph_id)}
    simulated_ids = set(existing_ids)
    for change in deletes:
        if change.node_id not in simulated_ids:
            raise ValueError(f"Node '{change.node_id}' not found.")
        simulated_ids.discard(change.node_id)

    for change in revises:
        if change.node_id not in simulated_ids:
            raise ValueError(f"Node '{change.node_id}' not found.")

    for change in adds:
        if change.node_id in simulated_ids:
            raise ValueError(
                f"Cannot add: node '{change.node_id}' already exists.",
            )
        simulated_ids.add(change.node_id)

    simulated = _simulate_plan_changes(nodes, graph_id, normalized)
    result = ApplyPlanChangesResult()

    for change in deletes:
        for node in list(nodes):
            if node.graph_id == graph_id and node.id == change.node_id:
                nodes.remove(node)
                break
        for other in graph_nodes(nodes, graph_id):
            if change.node_id in other.deps:
                other.deps = [
                    dep for dep in other.deps if dep != change.node_id
                ]
        result.deleted.append(change.node_id)

    for change in adds:
        nodes.append(_clone_node(simulated[change.node_id]))
        result.added.append(change.node_id)

    revised_ids: List[str] = []
    for change in revises:
        replacement = _clone_node(simulated[change.node_id])
        for idx, node in enumerate(nodes):
            if node.graph_id == graph_id and node.id == change.node_id:
                nodes[idx] = replacement
                break
        result.revised.append(change.node_id)
        revised_ids.append(change.node_id)

    reset_seen: set[str] = set()
    for node_id in revised_ids:
        for reset_id in mark_downstream_todo(nodes, graph_id, node_id):
            if reset_id not in reset_seen:
                reset_seen.add(reset_id)
                result.downstream_reset.append(reset_id)

    return result


def apply_dag_patch(
    nodes: List[TaskNode],
    graph_id: str,
    dag: DAG | Dict[str, Any] | str,
    registry: GraphRegistry | None = None,
) -> DAGDiff:
    """将 DAG patch 应用到 nodes 列表中指定 graph 的节点。

    返回 DAGDiff 描述变更摘要。
    """
    if isinstance(dag, DAG):
        patch = dag
    elif isinstance(dag, str):
        try:
            raw: Any = yaml.safe_load(dag)
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse DAG YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("DAG YAML root must be a mapping.")
        patch = DAG.from_dict(raw)
    else:
        patch = DAG.from_dict(dag)

    gn = graph_nodes(nodes, graph_id)
    existing_map = {n.id: n for n in gn}
    existing_ids = set(existing_map.keys())
    patch_ids = {n.node_id for n in patch.nodes if n.node_id is not None}
    reg = registry if registry is not None else {}

    diff = DAGDiff()
    source_ids: set[str] = set()

    set_graph_meta(
        reg, graph_id,
        name=patch.name,
        description=patch.description,
        expected_outcome=patch.expected_outcome,
    )

    # Remove nodes not in patch
    for removed_id in sorted(existing_ids - patch_ids):
        n = existing_map[removed_id]
        nodes.remove(n)
        for other in graph_nodes(nodes, graph_id):
            if removed_id in other.deps:
                other.deps = [d for d in other.deps if d != removed_id]
        diff.removed.append(removed_id)

    for patch_node in patch.nodes:
        assert patch_node.node_id is not None
        nid = patch_node.node_id
        explicit_state = "state" in patch_node.model_fields_set

        if nid not in existing_map:
            new_node = make_node(
                graph_id=graph_id,
                node_id=nid,
                name=patch_node.name,
                description=patch_node.description,
                expected_outcome=patch_node.expected_outcome,
                deps=list(patch_node.deps),
                state=patch_node.state if explicit_state else "todo",
            )
            nodes.append(new_node)
            diff.added.append(nid)
            if explicit_state:
                diff.state_overridden.append(nid)
            continue

        # Existing node — merge
        node = existing_map[nid]
        changed = False

        if node.description != patch_node.description:
            node.description = patch_node.description
            changed = True
        if node.expected_outcome != patch_node.expected_outcome:
            node.expected_outcome = patch_node.expected_outcome
            changed = True
        new_deps = list(patch_node.deps)
        if node.deps != new_deps:
            node.deps = new_deps
            changed = True

        node.name = patch_node.name

        if changed:
            reset_node_to_todo(node)
            diff.modified.append(nid)
            source_ids.add(nid)

        if explicit_state and node.state != patch_node.state:
            node.state = patch_node.state
            if node.state == "todo":
                node.started_at = None
                node.output = None
            diff.state_overridden.append(nid)
            source_ids.add(nid)

    reset_seen: set[str] = set()
    for source_id in sorted(source_ids):
        for reset_id in mark_downstream_todo(nodes, graph_id, source_id):
            if reset_id not in reset_seen:
                reset_seen.add(reset_id)
                diff.downstream_reset.append(reset_id)

    return diff

# -*- coding: utf-8 -*-
"""DefaultGraphToHint —— 基于 TaskNode 列表的 DAG 状态生成 LLM 提示。

提示注入时机：每轮 ``_reasoning`` 开始前，由
``RuntimeStateManager.get_current_hint()`` 调用。
"""
from __future__ import annotations

from typing import List, Optional

from .task_graph import (
    GraphRegistry,
    TaskNode,
    get_ready_nodes,
    graph_nodes,
    graph_to_markdown,
    is_graph_done,
)


class DefaultGraphToHint:
    """基于 TaskNode 列表和 graph_id 生成提示字符串（None 表示无提示）。"""

    hint_prefix: str = "<system-hint>"
    hint_suffix: str = "</system-hint>"

    no_graph: str = (
        "The user has not initiated any analysis task graph yet. "
        "If the user's request is complex or requires multiple analytical "
        "steps (data fetching, volatility analysis, attribution, report), "
        "you SHOULD call `create_plan` to lay out a DAG first. "
        "For simple questions you can answer directly without creating a "
        "graph."
    )

    at_beginning: str = (
        "The current task graph:\n"
        "```\n{graph}\n```\n"
        "All nodes are in `todo` state. Your options:\n"
        "- Execute the graph serially: start only one ready node, complete "
        "it, then choose the next node in a later step.\n"
        "- Pick one ready node (no unfinished dependencies) and call "
        "`update_subtask(node_id, 'in_progress')` to start executing.\n"
        "- If the graph no longer fits the user's intent, call "
        "`revise_current_plan` to adjust."
    )

    ready_nodes_hint: str = (
        "The current task graph:\n"
        "```\n{graph}\n```\n"
        "Ready nodes (deps satisfied, status todo): "
        "{ready_ids}\n"
        "- Even if multiple ready nodes are listed, execute exactly ONE "
        "node at a time.\n"
        "- Pick one ready node and call `update_subtask(node_id, "
        "'in_progress')` before doing actual work.\n"
        "- Do NOT start another ready node until the current node has been "
        "marked done.\n"
        "- After the work, call `update_subtask(node_id, 'done', "
        "reasoning=..., summary=..., files=...)`. Include any generated "
        "files with name / path / mime_type; the backend will fill file "
        "sizes automatically."
    )

    in_progress_hint: str = (
        "The current task graph:\n"
        "```\n{graph}\n```\n"
        "Node `{node_id}` (name: '{node_name}') is currently `in_progress`. "
        "This MAY be the result of a prior interruption.\n"
        "- **Check the conversation history carefully**: if the tool call "
        "for this node has already produced a complete result, call "
        "`update_subtask({node_id}, 'done', ...)` immediately to record it.\n"
        "- If the tool result is incomplete or absent, re-execute the node.\n"
        "- If the user has asked for a change to this node's parameters, "
        "call `revise_current_plan(changes=[{{node_id: '{node_id}', "
        "action: 'revise', node: {{..., deps: [direct_upstream_ids]}}}}])` "
        "which resets it and all downstream nodes to todo."
    )

    all_done: str = (
        "The current task graph:\n"
        "```\n{graph}\n```\n"
        "All nodes are done. Summarize the analysis to the user, "
        "then call `finish_plan('done', outcome=<final report summary>)` "
        "to archive the graph."
    )

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def __call__(
        self,
        nodes: List[TaskNode],
        graph_id: str | None,
        registry: GraphRegistry | None = None,
    ) -> Optional[str]:
        """生成提示消息。

        Args:
            nodes: 完整的节点列表。
            graph_id: 当前活跃图的 ID，None 表示无活跃图。
            registry: Graph-level 元数据注册表。

        分类逻辑（按优先级）：
        1. 图为空 → ``no_graph``
        2. 有 ``in_progress`` 节点 → ``in_progress_hint``（中断恢复场景）
        3. 所有节点完成 → ``all_done``
        4. 全部为 pending（新图）→ ``at_beginning``
        5. 其它（执行中）→ ``ready_nodes_hint``
        """
        if graph_id is None:
            return self._wrap(self.no_graph)

        gn = graph_nodes(nodes, graph_id)
        if not gn:
            return self._wrap(self.no_graph)

        graph_md = graph_to_markdown(nodes, graph_id, registry)

        in_progress = [n for n in gn if n.state == "in_progress"]
        if in_progress:
            node = in_progress[0]
            return self._wrap(
                self.in_progress_hint.format(
                    graph=graph_md,
                    node_id=node.id,
                    node_name=node.name,
                ),
            )

        if is_graph_done(nodes, graph_id):
            return self._wrap(self.all_done.format(graph=graph_md))

        done_count = sum(1 for n in gn if n.state == "done")
        if done_count == 0:
            return self._wrap(self.at_beginning.format(graph=graph_md))

        ready = get_ready_nodes(nodes, graph_id)
        ready_ids = [n.id for n in ready] or ["(none)"]
        return self._wrap(
            self.ready_nodes_hint.format(
                graph=graph_md,
                ready_ids=ready_ids,
            ),
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _wrap(self, body: str) -> str:
        return f"{self.hint_prefix}{body}{self.hint_suffix}"

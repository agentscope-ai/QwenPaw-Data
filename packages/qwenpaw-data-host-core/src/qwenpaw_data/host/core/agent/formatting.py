# -*- coding: utf-8 -*-
from __future__ import annotations


def format_pending_edits(edits: list[dict]) -> str:
    """Render external task graph edits into a compact LLM-readable summary."""
    lines: list[str] = []
    for edit in edits:
        etype = edit.get("type")
        if etype in ("sop_replaced", "sop_loaded"):
            name = edit.get("name", "未命名")
            node_count = edit.get("node_count", "?")
            replaced = edit.get("replaced_graph_id")
            node_summary = edit.get("node_summary") or []
            summary_lines = "\n".join(
                f"  - `{x['id']}`: {x['name']} deps={x.get('deps') or []}"
                for x in node_summary
                if isinstance(x, dict)
            )
            head = f"已加载 SOP 模板「{name}」（{node_count} 个节点）"
            if replaced:
                head += f"，已替换旧图 {replaced}"
            body = (
                f"{head}。这是用户提供的执行计划，请按 ready 节点的 deps "
                "顺序逐步执行；如无修改诉求，不要再调用 create_plan。"
            )
            if summary_lines:
                body = body + "\n" + summary_lines
            lines.append(body)
        elif etype == "dag_merged":
            name = edit.get("name", "未命名")
            lines.append(
                f"用户修订了任务图「{name}」：\n"
                f"- 新增节点：{edit.get('added') or []}\n"
                f"- 修改节点：{edit.get('modified') or []}（结构变更节点已重置为 pending）\n"
                f"- 删除节点：{edit.get('removed') or []}\n"
                f"- 用户显式改变状态：{edit.get('state_overridden') or []}\n"
                "- 已 completed 节点保留进度，请勿重新执行。"
            )
        elif etype == "node_edited":
            node_id = edit.get("node_id", "?")
            changes = edit.get("changes", {})
            lines.append(f"用户在任务面板修改了节点 `{node_id}`：{changes}")
        elif etype == "graph_replaced":
            lines.append("当前活跃图被前端替换。请检查新的 current_plan 并按其执行。")
        else:
            lines.append(f"未知外部变更：{edit}")
    return "\n".join(lines) if lines else "(no pending edits)"

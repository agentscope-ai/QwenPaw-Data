# -*- coding: utf-8 -*-
"""Stage-only AgentScope tools for the skill evolution fork."""

from __future__ import annotations

from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, Toolkit, ToolResponse
from agentscope.tool._response import ToolResultState

from qwenpaw_data.host.core.algo.skill_evolution.diffutil import fuzzy_find_and_replace
from qwenpaw_data.host.core.algo.skill_evolution.models import ProposedPatch
from qwenpaw_data.host.core.algo.skill_evolution.skills_io import SkillCatalog
from qwenpaw_data.host.core.algo.skill_evolution.stage_buffer import StageBuffer
from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.utils.skill import is_skill_document, normalize_rel_path


def build_evolution_toolkit(
    *,
    catalog: SkillCatalog,
    buffer: StageBuffer,
) -> Toolkit:
    """Build a whitelist Toolkit that only stages mutations into ``buffer``."""
    handler = _EvolutionTools(catalog=catalog, buffer=buffer)
    return Toolkit(
        tools=[
            FunctionTool(handler.skills_list),
            FunctionTool(handler.skill_view),
            FunctionTool(handler.skill_propose_patch),
            FunctionTool(handler.skill_propose_write_file),
            FunctionTool(handler.skill_evolution_done),
        ]
    )


class _EvolutionTools:
    def __init__(self, *, catalog: SkillCatalog, buffer: StageBuffer) -> None:
        self.catalog = catalog
        self.buffer = buffer

    async def skills_list(self) -> ToolResponse:
        """列出 agent 工作区中已有 skill 的名称与描述。禁止创建/删除 skill。"""
        infos = sorted(self.catalog.infos, key=lambda item: item.name)
        if not infos:
            return _ok("（无已有 skill）")
        lines: list[str] = []
        for info in infos:
            desc = (info.description or "").strip()
            if desc:
                lines.append(f"- {info.name}: {desc}")
            else:
                lines.append(f"- {info.name}")
        return _ok("\n".join(lines))

    async def skill_view(self, name: str, file_path: str = "SKILL.md") -> ToolResponse:
        """读取已有 skill 文件内容。修改前必须先 view 目标文件。

        Args:
            name: 已有 skill 名。
            file_path: 相对路径，默认 SKILL.md；也可为任意捆绑资源。
        """
        try:
            skill_name = require_safe_name((name or "").strip())
        except Exception:
            return _err(f"invalid skill name: {name!r}")
        if skill_name not in self.catalog.names:
            return _err(f"unknown skill (cannot create): {skill_name}")
        try:
            rel = normalize_rel_path(file_path or "SKILL.md")
        except ValueError as exc:
            return _err(str(exc))
        content, err = self._working_text(skill_name, rel)
        if err:
            return _err(err)
        self.buffer.mark_viewed(skill_name, rel)
        return _ok(f"### {skill_name}/{rel}\n```\n{content}\n```")

    async def skill_propose_patch(
        self,
        name: str,
        file_path: str,
        old_string: str,
        new_string: str,
        reason: str,
    ) -> ToolResponse:
        """对已 view 过的文件提出替换补丁（仅暂存，不写盘）。支持模糊匹配。

        Args:
            name: 已有 skill 名。
            file_path: 相对路径（SKILL.md 或任意捆绑资源）。
            old_string: 要替换的原文片段。
            new_string: 替换后的文本。
            reason: 为何改这一处：必须引用对话中的具体证据（用户原话/纠正点）。
        """
        try:
            skill_name = require_safe_name((name or "").strip())
        except Exception:
            return _err(f"invalid skill name: {name!r}")
        if skill_name not in self.catalog.names:
            return _err(f"unknown skill (cannot create): {skill_name}")
        try:
            rel = normalize_rel_path(file_path)
        except ValueError as exc:
            return _err(str(exc))
        if not self.buffer.was_viewed(skill_name, rel):
            return _err(
                f"must skill_view({skill_name!r}, {rel!r}) before propose_patch"
            )
        content, read_err = self._working_text(skill_name, rel)
        if read_err:
            return _err(read_err)
        new_content, resolved_old, resolved_new, strategy, error = (
            fuzzy_find_and_replace(content, old_string, new_string)
        )
        if error:
            return _err(error)
        if resolved_old == resolved_new:
            return _err("patch is a no-op")
        err = self.buffer.stage(
            ProposedPatch(
                skill_name=skill_name,
                relative_path=rel,
                old_string=resolved_old,
                new_string=resolved_new,
                create_file=False,
                reason=(reason or "").strip(),
            ),
            after=new_content,
        )
        if err:
            return _err(err)
        return _ok(
            f"staged patch for {skill_name}/{rel} via {strategy} "
            f"({len(resolved_old)}→{len(resolved_new)} chars)"
        )

    async def skill_propose_write_file(
        self,
        name: str,
        file_path: str,
        content: str,
        reason: str,
    ) -> ToolResponse:
        """在已有 skill 下新增/覆盖捆绑资源（仅暂存）。不得覆盖根 SKILL.md。

        Args:
            name: 已有 skill 名。
            file_path: 捆绑资源相对路径。
            content: 新文件全文。
            reason: 为何新增/覆盖该文件：必须引用对话中的具体证据。
        """
        try:
            skill_name = require_safe_name((name or "").strip())
        except Exception:
            return _err(f"invalid skill name: {name!r}")
        if skill_name not in self.catalog.names:
            return _err(f"unknown skill (cannot create): {skill_name}")
        try:
            rel = normalize_rel_path(file_path)
        except ValueError as exc:
            return _err(str(exc))
        if is_skill_document(rel):
            return _err(
                "skill_propose_write_file cannot overwrite SKILL.md; use skill_propose_patch"
            )
        if not self.buffer.was_viewed(
            skill_name, "SKILL.md"
        ) and not self.buffer.was_viewed(skill_name, rel):
            return _err(f"must skill_view({skill_name!r}) before propose_write_file")
        if not isinstance(content, str):
            return _err("content must be a string")
        previous = self.buffer.current_text(skill_name, rel)
        snap = self.catalog.get(skill_name, rel)
        if previous is not None:
            create_file = False
            old_string = previous
        elif snap is not None:
            create_file = False
            old_string = snap.content
        else:
            create_file = True
            old_string = None
        err = self.buffer.stage(
            ProposedPatch(
                skill_name=skill_name,
                relative_path=rel,
                old_string=old_string,
                new_string=content,
                create_file=create_file,
                reason=(reason or "").strip(),
            ),
            after=content,
        )
        if err:
            return _err(err)
        self.buffer.mark_viewed(skill_name, rel)
        action = "create" if create_file else "overwrite"
        return _ok(f"staged {action} for {skill_name}/{rel}")

    def _working_text(self, skill_name: str, rel: str) -> tuple[str | None, str | None]:
        """Return staged-or-catalog text, or ``(None, error)``."""
        staged = self.buffer.current_text(skill_name, rel)
        if staged is not None:
            return staged, None
        snap = self.catalog.get(skill_name, rel)
        if snap is None:
            return None, f"file not found: {skill_name}/{rel}"
        return snap.content, None

    async def skill_evolution_done(self, summary: str) -> ToolResponse:
        """结束审阅并提交中文摘要。若无需修改，summary 写「无需修改」。

        Args:
            summary: 给用户看的中文变更摘要（改了什么）。
        """
        self.buffer.set_done(summary)
        return _ok("done; staging complete")


def _ok(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _err(text: str) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=text)],
        state=ToolResultState.ERROR,
    )


__all__ = ["build_evolution_toolkit"]

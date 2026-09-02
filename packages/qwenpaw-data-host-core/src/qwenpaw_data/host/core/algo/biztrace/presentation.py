# -*- coding: utf-8 -*-
"""Presentation cards for BizEvents.

A tool call and its result are separate events, so they get separate cards: the
call card carries the caption, what the tool does and its input description; the
result card reuses that caption and describes the output, reading the call card
as the context the output answers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.formatting import (
    compact,
    first_str,
    join_sections,
    output_text,
    truncate,
)
from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLM, StructuredLLMError
from qwenpaw_data.host.core.algo.biztrace.models import BizEvent, Presentation
from qwenpaw_data.host.core.algo.biztrace.prompts import (
    DEFAULT_PROMPT_LANG,
    PresentationTask,
    PromptLang,
    build_presentation_summary_prompt,
    build_tool_output_prompt,
    build_tool_running_prompt,
    get_presentation_system_prompt,
    get_thinking_presentation_system_prompt,
    get_tool_output_presentation_system_prompt,
    get_tool_presentation_system_prompt,
)
from qwenpaw_data.host.core.algo.biztrace.tools import (
    NOTE_ONLY_RESULT_TOOLS,
    PLAN_SUMMARY_TOOLS,
    SKILL_VIEWER,
    SPAWN_SUBAGENT,
    canonical_tool_name,
)
from qwenpaw_data.host.core.utils.skill import discover_builtin_skills

logger = logging.getLogger(__name__)

DESCRIPTION_CHAR_CAP = 200
SUMMARY_SOURCE_CAP = 4_000
THINKING_CHAR_CAP = 2_000
THINKING_SOURCE_CAP = 130_000
TOOL_PAYLOAD_CAP = 2_000
HINT_BODY_CAP = 4_000

_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/([^/]+)/SKILL\.md$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_DESCRIPTION_RE = re.compile(
    r"^description:\s*(.*?)(?=^\w[\w-]*:|\Z)", re.MULTILINE | re.DOTALL
)

_FILE_PATH_KEYS = ("file_path", "path", "filename", "target_file")

CAPTIONS: dict[str, dict[str, str]] = {
    "zh": {
        "user": "用户输入",
        "thinking": "模型思考",
        "text": "模型回复",
        "hint": "用户引导",
        "read_file": "读取文件",
        "read_skill": "读取「{name}」技能",
        "PlanCreate": "创建计划",
        "PlanUpdate": "修改计划",
        "TaskStateUpdate": "更新子任务状态",
        "spawn_subagent": "发起「{name}」Sub-Agent",
        "fallback": "调用 {name}",
        "skill_body": "## 技能描述\n\n{description}",
        "path_body": "## 文件路径\n\n{path}",
        "input_heading": "## Input",
        "output_heading": "## Output",
        "source_heading": "## 来源",
        "TaskStateUpdate_body": "将子任务「{node}」的状态更新为 {state}。",
        "TaskStateUpdate_body_plain": "更新计划中子任务的执行状态。",
        "subagent_unknown": "子",
        "done_note": "调用完成。",
        "failed_note": "调用失败。",
        "unclosed_note": "工具未正常结束。",
        "interrupted_note": "调用被用户打断。",
        "denied_note": "调用被拒绝执行。",
    },
    "en": {
        "user": "User input",
        "thinking": "Model thinking",
        "text": "Model reply",
        "hint": "User guidance",
        "read_file": "Read file",
        "read_skill": 'Load the "{name}" skill',
        "PlanCreate": "Create plan",
        "PlanUpdate": "Revise plan",
        "TaskStateUpdate": "Update sub-task state",
        "spawn_subagent": 'Spawn the "{name}" sub-agent',
        "fallback": "Call {name}",
        "skill_body": "## Skill description\n\n{description}",
        "path_body": "## File path\n\n{path}",
        "input_heading": "## Input",
        "output_heading": "## Output",
        "source_heading": "## Source",
        "TaskStateUpdate_body": 'Sets sub-task "{node}" to {state}.',
        "TaskStateUpdate_body_plain": "Updates a plan sub-task's execution state.",
        "subagent_unknown": "sub",
        "done_note": "The call completed.",
        "failed_note": "The call failed.",
        "unclosed_note": "The tool never finished.",
        "interrupted_note": "The call was interrupted by the user.",
        "denied_note": "The call was denied.",
    },
}

_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"body": {"type": "string"}},
    "required": ["body"],
}

_TOOL_RUNNING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "caption": {"type": "string"},
        "purpose": {"type": "string"},
        "input_summary": {"type": "string"},
    },
    "required": ["caption", "purpose", "input_summary"],
}

_TOOL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"output_summary": {"type": "string"}},
    "required": ["output_summary"],
}

Linker = Callable[[str], str]


class PresentationBuilder:
    """Build the card for each BizEvent; never returns None.

    An in-process cache collapses repeated identical requests, which matters
    for sub-agent chunk streams that re-emit the same reasoning segment.
    """

    def __init__(
        self,
        *,
        llm: StructuredLLM | None = None,
        lang: PromptLang = DEFAULT_PROMPT_LANG,
        linker: Linker | None = None,
    ) -> None:
        self.llm = llm
        self.lang = lang
        self.words = CAPTIONS["en" if lang == "en" else "zh"]
        self.linker = linker
        self.cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.llm_calls = 0
        self.llm_failures = 0
        self.cache_hits = 0

    async def build(
        self,
        event: BizEvent,
        *,
        call_card: Presentation | None = None,
        note: str | None = None,
        hint: Any = None,
    ) -> Presentation:
        """Return the card for ``event``.

        Args:
            event: The event being emitted.
            call_card: The call card, when emitting a tool result event.
            note: Appended verbatim to the body, used for reconciliation and
                for non-error terminal states the frontend must still see.
            hint: Raw HintBlock payload, for ``kind == "hint"`` only.
        """

        card = await self._build(event, call_card, hint)
        if note:
            card = card.model_copy(update={"body": f"{card.body}\n\n{note}".strip()})
        return self._link(card)

    async def _build(
        self,
        event: BizEvent,
        call_card: Presentation | None,
        hint: Any,
    ) -> Presentation:
        if event.kind == "user":
            return Presentation(
                card_type="user",
                caption=self.words["user"],
                body=event.content or "",
            )
        if event.kind == "assistant_thinking":
            return await self._thinking_card(event.content)
        if event.kind == "assistant_text":
            return self._text_card(event.content)
        if event.kind == "hint":
            return self._hint_card(event, hint)
        if event.kind == "tool_result":
            return await self._tool_result_card(event, call_card)
        return await self._tool_call_card(event)

    def _link(self, card: Presentation) -> Presentation:
        """Inject entity links into the body; captions stay untouched."""
        if self.linker is None or not card.body:
            return card
        try:
            linked = self.linker(card.body)
        except Exception:
            logger.exception("Entity linking failed; emitting the raw body")
            return card
        if linked == card.body:
            return card
        return card.model_copy(update={"body": linked})

    # -- text cards -------------------------------------------------------- #

    def _text_card(self, content: str | None) -> Presentation:
        """Reply cards keep the model text as-is; no LLM summary."""
        return Presentation(
            card_type="text",
            caption=self.words["text"],
            body=content or "",
        )

    async def _thinking_card(self, content: str | None) -> Presentation:
        source = content or ""
        body = await self._summarize(
            "thinking",
            source,
            source_cap=THINKING_SOURCE_CAP,
            system=get_thinking_presentation_system_prompt(self.lang),
        ) or truncate(source, THINKING_CHAR_CAP)
        return Presentation(
            card_type="thinking",
            caption=self.words["thinking"],
            body=body,
        )

    async def _summarize(
        self,
        task: PresentationTask,
        content: str,
        *,
        source_cap: int = SUMMARY_SOURCE_CAP,
        system: str | None = None,
    ) -> str | None:
        source = content.strip()
        if not source or self.llm is None:
            return None
        clipped = source[:source_cap]
        data = await self._call(
            ("summary", task, clipped),
            system=system or get_presentation_system_prompt(self.lang),
            user=build_presentation_summary_prompt(
                task=task, content=clipped, lang=self.lang
            ),
            schema_name="presentation_body",
            schema=_SUMMARY_SCHEMA,
        )
        body = str(data.get("body") or "").strip() if data else ""
        return body or None

    def _hint_card(self, event: BizEvent, hint: Any) -> Presentation:
        """Fill the guidance card by rule; hints never reach the LLM."""
        body = truncate(self._hint_body(event, hint), HINT_BODY_CAP)
        if event.source:
            body = join_sections(
                body, self.words["source_heading"], event.source.strip()
            )
        return Presentation(
            card_type="hint", caption=self.words["hint"], body=body
        )

    def _hint_body(self, event: BizEvent, hint: Any) -> str:
        if isinstance(hint, str):
            return hint
        if isinstance(hint, list):
            return "\n\n".join(
                part for part in (_hint_block(block) for block in hint) if part
            )
        return event.content or ""

    # -- tool cards -------------------------------------------------------- #

    async def _tool_call_card(self, event: BizEvent) -> Presentation:
        tool = canonical_tool_name(event.tool_name) or "unknown_tool"
        if tool == "read_file":
            return self._read_file_card(event)
        if tool == SKILL_VIEWER:
            return self._skill_viewer_card(event)
        if tool == "TaskStateUpdate":
            return self._subtask_card(event)
        if tool in PLAN_SUMMARY_TOOLS:
            return await self._plan_card(event, tool)
        if tool == SPAWN_SUBAGENT:
            return await self._subagent_card(event)
        return await self._fallback_call_card(event, tool)

    def _read_file_card(self, event: BizEvent) -> Presentation:
        path = first_str(event.input, _FILE_PATH_KEYS)
        skill = _skill_name(path)
        if skill:
            # The description lives in the file body, so it lands on the result.
            return self._skill_call_card(skill)
        return Presentation(
            card_type="tool",
            caption=self.words["read_file"],
            body=self.words["path_body"].format(path=path or "-"),
        )

    def _skill_viewer_card(self, event: BizEvent) -> Presentation:
        """Same card shape as reading ``skills/<name>/SKILL.md`` via read_file."""
        name = first_str(event.input, ("skill", "name")) or "-"
        return self._skill_call_card(name)

    def _skill_call_card(self, name: str) -> Presentation:
        return Presentation(
            card_type="tool",
            caption=self.words["read_skill"].format(name=name),
            body="",
        )

    def _subtask_card(self, event: BizEvent) -> Presentation:
        info = event.orchestration
        if info is None or not (info.node_name or info.node_id):
            body = self.words["TaskStateUpdate_body_plain"]
        else:
            body = self.words["TaskStateUpdate_body"].format(
                node=info.node_name or info.node_id,
                state=info.subtask_state or "-",
            )
        return Presentation(
            card_type="tool", caption=self.words["TaskStateUpdate"], body=body
        )

    async def _plan_card(self, event: BizEvent, tool: str) -> Presentation:
        task: PresentationTask = "PlanCreate" if tool == "PlanCreate" else "PlanUpdate"
        payload = compact(event.input)
        body = await self._summarize(task, payload) or truncate(
            payload, DESCRIPTION_CHAR_CAP
        )
        return Presentation(card_type="tool", caption=self.words[tool], body=body)

    async def _subagent_card(self, event: BizEvent) -> Presentation:
        role = first_str(event.input, ("role", "name", "agent_name"))
        payload = compact(event.input)
        body = await self._summarize("spawn_subagent", payload) or truncate(
            payload, DESCRIPTION_CHAR_CAP
        )
        return Presentation(
            card_type="tool",
            caption=self.words["spawn_subagent"].format(
                name=role or self.words["subagent_unknown"]
            ),
            body=body,
        )

    async def _fallback_call_card(
        self, event: BizEvent, tool: str
    ) -> Presentation:
        payload = compact(event.input)[:TOOL_PAYLOAD_CAP]
        data = await self._call(
            ("tool_running", tool, payload),
            system=get_tool_presentation_system_prompt(self.lang),
            user=build_tool_running_prompt(
                tool_name=tool, tool_input=payload, lang=self.lang
            ),
            schema_name="tool_presentation",
            schema=_TOOL_RUNNING_SCHEMA,
        )
        if data:
            caption = str(data.get("caption") or "").strip()
            purpose = str(data.get("purpose") or "").strip()
            input_summary = str(data.get("input_summary") or "").strip()
        else:
            caption = purpose = input_summary = ""
        return Presentation(
            card_type="tool",
            caption=caption or self.words["fallback"].format(name=tool),
            body=join_sections(
                truncate(purpose, DESCRIPTION_CHAR_CAP),
                self.words["input_heading"],
                truncate(input_summary or payload, DESCRIPTION_CHAR_CAP),
            ),
        )

    async def _tool_result_card(
        self, event: BizEvent, call_card: Presentation | None
    ) -> Presentation:
        """Describe one result, using its call card as the context it answers."""
        tool = canonical_tool_name(event.tool_name) or "unknown_tool"
        caption = (
            call_card.caption
            if call_card is not None
            else self.words["fallback"].format(name=tool)
        )
        failed = event.status == "error"
        if tool in ("read_file", SKILL_VIEWER):
            body = self._skill_result_body(event, failed)
        elif tool in NOTE_ONLY_RESULT_TOOLS:
            body = self.words["failed_note" if failed else "done_note"]
        else:
            body = await self._output_summary(event, tool, caption, call_card, failed)
        return Presentation(card_type="tool", caption=caption, body=body)

    def _skill_result_body(self, event: BizEvent, failed: bool) -> str:
        """Build the skill card body from frontmatter or a local skill lookup.

        ``read_file`` of ``SKILL.md`` still carries YAML frontmatter in the
        tool output. AgentScope's ``Skill`` viewer returns body-only markdown
        (``Skill.markdown``), so its description is recovered from the shipped
        skill files by the name in ``event.input``.
        """
        description = _skill_description(output_text(event.output))
        if description is None:
            name = first_str(event.input, ("skill", "name"))
            description = _lookup_skill_description(name) if name else None
        if description is None:
            return self.words["failed_note" if failed else "done_note"]
        return self.words["skill_body"].format(description=description)

    async def _output_summary(
        self,
        event: BizEvent,
        tool: str,
        caption: str,
        call_card: Presentation | None,
        failed: bool,
    ) -> str:
        payload = output_text(event.output)[:TOOL_PAYLOAD_CAP]
        context = call_card.body if call_card is not None else ""
        data = await self._call(
            ("tool_output", tool, context, payload, failed),
            system=get_tool_output_presentation_system_prompt(self.lang),
            user=build_tool_output_prompt(
                tool_name=tool,
                purpose=caption,
                call_context=context,
                tool_output=payload,
                failed=failed,
                lang=self.lang,
            ),
            schema_name="tool_output_presentation",
            schema=_TOOL_OUTPUT_SCHEMA,
        )
        summary = str(data.get("output_summary") or "").strip() if data else ""
        summary = _clean_output_summary(summary) or payload
        if not summary:
            return self.words["failed_note" if failed else "done_note"]
        return join_sections(
            self.words["output_heading"], truncate(summary, DESCRIPTION_CHAR_CAP)
        )

    # -- LLM plumbing ------------------------------------------------------ #

    async def _call(
        self,
        cache_key: tuple[Any, ...],
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any] | None:
        cached = self.cache.get(cache_key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if self.llm is None:
            return None
        self.llm_calls += 1
        try:
            data = await self.llm.complete(
                system=system,
                user=user,
                schema_name=schema_name,
                schema=schema,
            )
        except StructuredLLMError as exc:
            self.llm_failures += 1
            logger.warning(
                "presentation %s fell back to template: %s", schema_name, exc
            )
            return None
        self.cache[cache_key] = data
        return data


def _hint_block(block: Any) -> str:
    """Render one hint block; binary payloads become a link, never base64."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return str(block)
    if block.get("type") == "text":
        text = block.get("text")
        return text if isinstance(text, str) else ""
    source = block.get("source")
    source = source if isinstance(source, dict) else {}
    media_type = str(source.get("media_type") or block.get("media_type") or "data")
    url = source.get("url")
    if isinstance(url, str) and url:
        return f"[{media_type}]({url})"
    name = block.get("name")
    label = f"{media_type} · {name}" if name else media_type
    return f"`[{label}]`"


# Models sometimes dump the call-card schema back into output_summary.
_ECHOED_CARD_FIELD_RE = re.compile(
    r"^(卡片小标题|目的|输入摘要|输出摘要|caption|purpose|input_summary|"
    r"output_summary)\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)


def _clean_output_summary(summary: str) -> str:
    """Drop echoed call-card fields if the model stuffed them into the summary."""
    text = summary.strip()
    if not text or "：" not in text and ":" not in text:
        return text

    preferred: str | None = None
    kept: list[str] = []
    saw_label = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _ECHOED_CARD_FIELD_RE.match(stripped)
        if match is None:
            kept.append(stripped)
            continue
        saw_label = True
        label, value = match.group(1), match.group(2).strip()
        if label.lower() in {"输出摘要", "output_summary"} and value:
            preferred = value
    if preferred:
        return preferred
    if saw_label:
        return "\n".join(kept).strip()
    return text


def _skill_name(path: str | None) -> str | None:
    """Return the skill name when a read targets ``skills/<name>/SKILL.md``."""
    if not path:
        return None
    match = _SKILL_PATH_RE.search(path.replace("\\", "/"))
    return match.group(1) if match else None


def _skill_description(text: str) -> str | None:
    """Pull ``description`` out of a SKILL.md YAML frontmatter block."""
    front = _FRONTMATTER_RE.match(text)
    if not front:
        return None
    match = _DESCRIPTION_RE.search(front.group(1))
    if not match:
        return None
    description = " ".join(match.group(1).split())
    return description or None


_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_skill_description_by_name: dict[str, str] | None = None


def _skill_description_index() -> dict[str, str]:
    """Map skill folder / frontmatter names to their SKILL.md descriptions."""
    global _skill_description_by_name
    if _skill_description_by_name is not None:
        return _skill_description_by_name
    index: dict[str, str] = {}
    for skill in discover_builtin_skills():
        description = skill.description.strip() or None
        if description is None:
            continue
        # Keep one-line card text consistent with previous frontmatter parsing.
        index[skill.name] = description
        index[skill.src_dir.name] = description
    _skill_description_by_name = index
    return index


def _lookup_skill_description(name: str) -> str | None:
    """Resolve a skill description when the tool result has no frontmatter."""
    return _skill_description_index().get(name)


__all__ = [
    "CAPTIONS",
    "DESCRIPTION_CHAR_CAP",
    "THINKING_CHAR_CAP",
    "Linker",
    "PresentationBuilder",
]

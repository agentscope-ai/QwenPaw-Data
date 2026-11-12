# -*- coding: utf-8 -*-
"""Presentation cards for BizEvents.

A tool call and its result are separate events, so they get separate cards: the
call card carries the caption, what the tool does and its input description; the
result card reuses that caption and describes the output, reading the call card
as the context the output answers.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.facts import classify_sql_error
from qwenpaw_data.host.core.algo.utils.formatting import (
    FILE_PATH_KEYS,
    compact,
    file_basename,
    first_str,
    is_business_file,
    join_sections,
    output_text,
    skill_name,
    truncate,
)
from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLM, StructuredLLMError
from qwenpaw_data.tracing.core.names import Step
from qwenpaw_data.host.core.algo.biztrace.models import BizEvent, Presentation
from qwenpaw_data.host.core.algo.biztrace.prompts import (
    DEFAULT_PROMPT_LANG,
    PresentationTask,
    PromptLang,
    build_family_call_prompt,
    build_family_result_prompt,
    build_presentation_summary_prompt,
    build_tool_output_prompt,
    build_tool_running_prompt,
    get_file_write_call_system_prompt,
    get_file_write_result_system_prompt,
    get_presentation_system_prompt,
    get_semantic_call_system_prompt,
    get_semantic_result_system_prompt,
    get_shell_call_system_prompt,
    get_shell_result_system_prompt,
    get_sql_call_system_prompt,
    get_sql_result_system_prompt,
    get_thinking_presentation_system_prompt,
    get_tool_output_presentation_system_prompt,
    get_tool_presentation_system_prompt,
)
from qwenpaw_data.host.core.algo.biztrace.tools import (
    GRAPH_SCHEMA_TOOL,
    NOTE_ONLY_RESULT_TOOLS,
    PLAN_SUMMARY_TOOLS,
    SKILL_VIEWER,
    SPAWN_SUBAGENT,
    canonical_tool_name,
    tool_family,
)
from qwenpaw_data.host.core.utils.skill import discover_builtin_skills

logger = logging.getLogger(__name__)

DESCRIPTION_CHAR_CAP = 200
FAMILY_BODY_CAP = 800
SQL_RESULT_CAP = 800
FILE_RESULT_CAP = 800
SHELL_RESULT_CAP = 600
SEMANTIC_RESULT_CAP = 400
LISTING_RESULT_CAP = 400
SUMMARY_SOURCE_CAP = 4_000
THINKING_CHAR_CAP = 2_000
THINKING_SOURCE_CAP = 130_000
TOOL_PAYLOAD_CAP = 2_000
HINT_BODY_CAP = 4_000
SQL_SOURCE_CAP = 8_000

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_DESCRIPTION_RE = re.compile(
    r"^description:\s*(.*?)(?=^\w[\w-]*:|\Z)", re.MULTILINE | re.DOTALL
)
_SQL_FAILURE_KINDS = frozenset(
    {"no_table", "no_permission", "no_column", "blocked", "other"}
)

CAPTIONS: dict[str, dict[str, str]] = {
    "zh": {
        "user": "用户输入",
        "thinking": "模型思考",
        "text": "模型回复",
        "hint": "用户引导",
        "read_file": "读取文件",
        "write_file": "写入文件",
        "edit_file": "修改文件",
        "ask_user_question": "澄清提问",
        "ask_user_question_titled": "澄清提问：{title}",
        "listing": "定位文件",
        "read_skill": "读取「{name}」技能",
        "PlanCreate": "创建计划",
        "PlanUpdate": "修改计划",
        "TaskStateUpdate": "更新子任务状态",
        "spawn_subagent": "发起「{name}」Sub-Agent",
        "fallback": "调用 {name}",
        "graph_schema": "读取业务图谱结构",
        "graph_schema_body": "查看业务图谱的节点类型、属性与关系，为后续检索确定可用的业务对象。",
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
        "write_file": "Write file",
        "edit_file": "Edit file",
        "ask_user_question": "Clarifying question",
        "ask_user_question_titled": "Clarifying question: {title}",
        "listing": "Locate files",
        "read_skill": 'Load the "{name}" skill',
        "PlanCreate": "Create plan",
        "PlanUpdate": "Revise plan",
        "TaskStateUpdate": "Update sub-task state",
        "spawn_subagent": 'Spawn the "{name}" sub-agent',
        "fallback": "Call {name}",
        "graph_schema": "Read the business graph schema",
        "graph_schema_body": (
            "Reads the business graph's node types, properties and relationships "
            "to decide which business objects can be searched."
        ),
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

_SQL_CALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "caption": {"type": "string"},
        "purpose": {"type": "string"},
    },
    "required": ["caption", "purpose"],
}

_SQL_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "output_summary": {"type": "string"},
        "failure_kind": {"type": ["string", "null"]},
    },
    "required": ["output_summary"],
}

Linker = Callable[[str], str]


class PresentationBuilder:
    """Build the card for each BizEvent.

    An in-process cache collapses repeated identical requests, which matters
    for sub-agent chunk streams that re-emit the same reasoning segment.
    """

    def __init__(
        self,
        *,
        llm: StructuredLLM | None = None,
        lang: PromptLang = DEFAULT_PROMPT_LANG,
        linker: Linker | None = None,
        artifact_linker: Linker | None = None,
    ) -> None:
        self.llm = llm
        self.lang = lang
        self.words = CAPTIONS["en" if lang == "en" else "zh"]
        self.linker = linker
        self.artifact_linker = artifact_linker
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
        call_input: Any = None,
    ) -> Presentation | None:
        """Return the card for ``event``, or ``None`` when it should stay hidden.

        Args:
            event: The event being emitted.
            call_card: The call card, when emitting a tool result event.
            note: Appended verbatim to the body, used for reconciliation and
                for non-error terminal states the frontend must still see.
            hint: Raw HintBlock payload, for ``kind == "hint"`` only.
            call_input: Structured arguments of the call this result closes.
        """

        card = await self._build(event, call_card, hint, call_input)
        if card is None:
            return None
        if note:
            card = card.model_copy(update={"body": f"{card.body}\n\n{note}".strip()})
        if card.card_type == "text":
            card = self._artifact_link(card)
        return self._link(card)

    async def _build(
        self,
        event: BizEvent,
        call_card: Presentation | None,
        hint: Any,
        call_input: Any,
    ) -> Presentation | None:
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
            return await self._tool_result_card(event, call_card, call_input=call_input)
        return await self._tool_call_card(event)

    def _artifact_link(self, card: Presentation) -> Presentation:
        """Inject artifact panel links into a reply body; captions stay untouched."""
        if self.artifact_linker is None or not card.body:
            return card
        try:
            linked = self.artifact_linker(card.body)
        except Exception:
            logger.exception("Artifact linking failed; emitting the raw body")
            return card
        if linked == card.body:
            return card
        return card.model_copy(update={"body": linked})

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
            step=Step.PRESENTATION_BODY,
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
        return Presentation(card_type="hint", caption=self.words["hint"], body=body)

    def _hint_body(self, event: BizEvent, hint: Any) -> str:
        if isinstance(hint, str):
            return hint
        if isinstance(hint, list):
            return "\n\n".join(
                part for part in (_hint_block(block) for block in hint) if part
            )
        return event.content or ""

    # -- tool cards -------------------------------------------------------- #

    async def _tool_call_card(self, event: BizEvent) -> Presentation | None:
        tool = canonical_tool_name(event.tool_name) or "unknown_tool"
        family = tool_family(tool)
        if family == "ask":
            return self._ask_call_card(event)
        if family == "listing":
            return self._listing_call_card(event)
        if family == "file_read":
            return self._read_file_card(event)
        if family == "sql":
            return await self._sql_call_card(event, tool)
        if tool == GRAPH_SCHEMA_TOOL:
            return self._graph_schema_card()
        if family == "semantic":
            return await self._semantic_call_card(event, tool)
        if family == "file_write":
            return await self._file_write_call_card(event, tool)
        if family == "shell":
            return await self._shell_call_card(event, tool)
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
        path = first_str(event.input, FILE_PATH_KEYS)
        skill = skill_name(path)
        if skill:
            # The description lives in the file body, so it lands on the result.
            return self._skill_call_card(skill)
        return Presentation(
            card_type="tool",
            caption=self.words["read_file"],
            body=self.words["path_body"].format(path=path or "-"),
        )

    async def _sql_call_card(self, event: BizEvent, tool: str) -> Presentation:
        sql = _sql_from(event.input)
        payload = sql[:SQL_SOURCE_CAP] or compact(event.input)[:TOOL_PAYLOAD_CAP]
        return await self._prose_call_card(
            tool,
            payload,
            cache_key=("sql_call", payload),
            system=get_sql_call_system_prompt(self.lang),
            step=Step.SQL_CALL,
            schema=_SQL_CALL_SCHEMA,
            input_body=_sql_fence(sql) or truncate(payload, FAMILY_BODY_CAP),
        )

    def _graph_schema_card(self) -> Presentation:
        """The schema read takes no input and returns structure, not findings."""
        return Presentation(
            card_type="tool",
            caption=self.words["graph_schema"],
            body=self.words["graph_schema_body"],
        )

    async def _semantic_call_card(self, event: BizEvent, tool: str) -> Presentation:
        payload = _graph_query_payload(event.input) or compact(event.input)
        payload = payload[:TOOL_PAYLOAD_CAP]
        return await self._prose_call_card(
            tool,
            payload,
            cache_key=("semantic_call", tool, payload),
            system=get_semantic_call_system_prompt(self.lang),
            step=Step.SEMANTIC_CALL,
        )

    async def _file_write_call_card(self, event: BizEvent, tool: str) -> Presentation:
        path = first_str(event.input, FILE_PATH_KEYS)
        name = file_basename(path) or (path or "-")
        payload = compact(event.input)[:TOOL_PAYLOAD_CAP]
        return await self._prose_call_card(
            tool,
            payload,
            cache_key=("file_write_call", tool, name, payload),
            system=get_file_write_call_system_prompt(self.lang),
            step=Step.FILE_WRITE_CALL,
            filename=name,
            default_caption=self.words.get(
                tool, self.words["fallback"].format(name=tool)
            ),
        )

    async def _shell_call_card(self, event: BizEvent, tool: str) -> Presentation:
        payload = compact(event.input)[:TOOL_PAYLOAD_CAP]
        return await self._prose_call_card(
            tool,
            payload,
            cache_key=("shell_call", payload),
            system=get_shell_call_system_prompt(self.lang),
            step=Step.SHELL_CALL,
        )

    async def _prose_call_card(
        self,
        tool: str,
        payload: str,
        *,
        cache_key: tuple[Any, ...],
        system: str,
        step: Step,
        schema: dict[str, Any] | None = None,
        filename: str | None = None,
        input_body: str | None = None,
        default_caption: str | None = None,
    ) -> Presentation:
        """Build a call card as caption + natural-language purpose + Input."""
        data = await self._call(
            cache_key,
            system=system,
            user=build_family_call_prompt(
                tool_name=tool,
                tool_input=payload,
                filename=filename,
                lang=self.lang,
            ),
            step=step,
            schema=schema or _TOOL_RUNNING_SCHEMA,
        )
        caption = default_caption or self.words["fallback"].format(name=tool)
        purpose = ""
        input_summary = ""
        if data:
            caption = str(data.get("caption") or "").strip() or caption
            purpose = str(data.get("purpose") or "").strip()
            input_summary = str(data.get("input_summary") or "").strip()
        return Presentation(
            card_type="tool",
            caption=caption,
            body=join_sections(
                truncate(purpose, DESCRIPTION_CHAR_CAP),
                self.words["input_heading"],
                input_body or truncate(input_summary or payload, DESCRIPTION_CHAR_CAP),
            ),
        )

    def _ask_call_card(self, event: BizEvent) -> Presentation:
        return Presentation(
            card_type="tool",
            caption=self._ask_caption(event.input),
            body=_render_ask(event.input, None),
        )

    def blank_ask_call(self, tool_input: Any) -> Presentation:
        """Keep the ask caption after the result arrives; empty the body.

        The frontend concatenates call + result. Blanking the call first
        leaves only the result (questions + answers) on the merged card.
        """
        return Presentation(
            card_type="tool",
            caption=self._ask_caption(tool_input),
            body="",
        )

    def _ask_caption(self, tool_input: Any) -> str:
        payload = tool_input if isinstance(tool_input, dict) else {}
        title = str(payload.get("title") or "").strip()
        if not title:
            return self.words["ask_user_question"]
        return self.words["ask_user_question_titled"].format(title=title)

    def _listing_call_card(self, event: BizEvent) -> Presentation:
        payload = event.input if isinstance(event.input, dict) else {}
        target = first_str(
            payload, ("pattern", "glob", "path", "query", "target")
        ) or compact(event.input)
        return Presentation(
            card_type="tool",
            caption=self.words["listing"],
            body=truncate(file_basename(target) or target, DESCRIPTION_CHAR_CAP),
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

    async def _fallback_call_card(self, event: BizEvent, tool: str) -> Presentation:
        payload = compact(event.input)[:TOOL_PAYLOAD_CAP]
        data = await self._call(
            ("tool_running", tool, payload),
            system=get_tool_presentation_system_prompt(self.lang),
            user=build_tool_running_prompt(
                tool_name=tool, tool_input=payload, lang=self.lang
            ),
            step=Step.TOOL_PRESENTATION,
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
        self,
        event: BizEvent,
        call_card: Presentation | None,
        *,
        call_input: Any = None,
    ) -> Presentation:
        """Describe one result, using its call card as the context it answers."""
        tool = canonical_tool_name(event.tool_name) or "unknown_tool"
        family = tool_family(tool)
        caption = (
            call_card.caption
            if call_card is not None
            else self.words["fallback"].format(name=tool)
        )
        failed = event.status == "error"
        context = call_card.body if call_card is not None else ""
        if family == "ask":
            caption = self._ask_caption(call_input)
            body = _render_ask(call_input, event.output)
        elif tool == GRAPH_SCHEMA_TOOL:
            body = self.words["failed_note" if failed else "done_note"]
        elif family == "listing":
            body = self._listing_result_body(event)
        elif family == "sql":
            body = await self._sql_result_body(
                event, tool, _summary_before_input(context), failed
            )
        elif family == "semantic":
            body = await self._family_output(
                event,
                tool,
                context,
                failed,
                system=get_semantic_result_system_prompt(self.lang),
                step=Step.SEMANTIC_RESULT,
                cap=SEMANTIC_RESULT_CAP,
            )
        elif family == "file_write":
            body = await self._family_output(
                event,
                tool,
                context,
                failed,
                system=get_file_write_result_system_prompt(self.lang),
                step=Step.FILE_WRITE_RESULT,
                cap=FILE_RESULT_CAP,
            )
        elif family == "shell":
            body = await self._family_output(
                event,
                tool,
                context,
                failed,
                system=get_shell_result_system_prompt(self.lang),
                step=Step.SHELL_RESULT,
                cap=SHELL_RESULT_CAP,
            )
        elif family == "file_read":
            path = first_str(call_input, FILE_PATH_KEYS)
            if skill_name(path) or tool == SKILL_VIEWER or _skill_caption(call_card):
                body = self._skill_result_body(event, failed)
            else:
                body = self.words["failed_note" if failed else "done_note"]
        elif tool == SKILL_VIEWER:
            body = self._skill_result_body(event, failed)
        elif tool in NOTE_ONLY_RESULT_TOOLS:
            body = self.words["failed_note" if failed else "done_note"]
        else:
            body = await self._output_summary(event, tool, caption, call_card, failed)
        return Presentation(card_type="tool", caption=caption, body=body)

    def _listing_result_body(self, event: BizEvent) -> str:
        text = output_text(event.output)
        names = [
            file_basename(line.strip())
            for line in text.splitlines()
            if is_business_file(line.strip())
        ]
        unique = list(dict.fromkeys(name for name in names if name))
        if unique:
            return truncate("、".join(unique[:8]), LISTING_RESULT_CAP)
        return self.words["done_note" if event.status != "error" else "failed_note"]

    async def _sql_result_body(
        self, event: BizEvent, tool: str, context: str, failed: bool
    ) -> str:
        payload = output_text(event.output)[:SQL_SOURCE_CAP]
        error_text = _sql_error_text(event.output)
        inferred = classify_sql_error(error_text) if (failed or error_text) else None
        data = await self._call(
            ("sql_result", tool, context, payload, failed),
            system=get_sql_result_system_prompt(self.lang),
            user=build_family_result_prompt(
                tool_name=tool,
                call_context=context,
                tool_output=payload,
                failed=failed or bool(error_text),
                lang=self.lang,
            ),
            step=Step.SQL_RESULT,
            schema=_SQL_RESULT_SCHEMA,
        )
        summary = str(data.get("output_summary") or "").strip() if data else ""
        kind = str(data.get("failure_kind") or "").strip() if data else ""
        if kind not in _SQL_FAILURE_KINDS:
            kind = inferred if inferred and inferred != "tool_unavailable" else kind
            if kind not in _SQL_FAILURE_KINDS:
                kind = inferred or ""
        summary = _clean_output_summary(summary) or payload
        if kind and (failed or error_text):
            label = _FAILURE_LABEL.get(self.lang, _FAILURE_LABEL["zh"]).get(kind, kind)
            summary = join_sections(label, summary) if summary else label
        if not summary:
            return self.words["failed_note" if failed else "done_note"]
        return join_sections(
            self.words["output_heading"], truncate(summary, SQL_RESULT_CAP)
        )

    async def _family_output(
        self,
        event: BizEvent,
        tool: str,
        context: str,
        failed: bool,
        *,
        system: str,
        step: Step,
        cap: int,
    ) -> str:
        payload = output_text(event.output)[:TOOL_PAYLOAD_CAP]
        data = await self._call(
            (step, tool, context, payload, failed),
            system=system,
            user=build_family_result_prompt(
                tool_name=tool,
                call_context=context,
                tool_output=payload,
                failed=failed,
                lang=self.lang,
            ),
            step=step,
            schema=_TOOL_OUTPUT_SCHEMA,
        )
        summary = str(data.get("output_summary") or "").strip() if data else ""
        summary = _clean_output_summary(summary) or payload
        if not summary:
            return self.words["failed_note" if failed else "done_note"]
        return join_sections(self.words["output_heading"], truncate(summary, cap))

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
            step=Step.TOOL_OUTPUT_PRESENTATION,
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
        step: Step,
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
                step=step,
                schema=schema,
            )
        except StructuredLLMError as exc:
            self.llm_failures += 1
            logger.warning("presentation %s fell back to template: %s", step, exc)
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


_FAILURE_LABEL = {
    "zh": {
        "no_table": "无表",
        "no_permission": "无项目权限",
        "no_column": "字段不存在",
        "blocked": "被拦截",
        "other": "调用失败",
        "tool_unavailable": "查询工具不可用",
    },
    "en": {
        "no_table": "table not found",
        "no_permission": "no project permission",
        "no_column": "column missing",
        "blocked": "blocked",
        "other": "call failed",
        "tool_unavailable": "query tool unavailable",
    },
}


def _sql_from(payload: Any) -> str:
    if isinstance(payload, dict):
        sql = payload.get("sql")
        return sql.strip() if isinstance(sql, str) else ""
    return payload.strip() if isinstance(payload, str) else ""


def _graph_query_payload(payload: Any) -> str:
    """Render a ``query_graph`` call as its Cypher plus parameters, else ''."""
    if not isinstance(payload, dict):
        return ""
    cypher = payload.get("cypher")
    if not isinstance(cypher, str) or not cypher.strip():
        return ""
    params = payload.get("params")
    suffix = f"\nparams: {compact(params)}" if params else ""
    return f"cypher: {cypher.strip()}{suffix}"


def _sql_fence(sql: str) -> str:
    if not sql:
        return ""
    return f"```sql\n{sql}\n```"


def _summary_before_input(body: str) -> str:
    """Keep the call-card summary; drop the fenced SQL Input section."""
    index = body.find("## Input")
    return body[:index].strip() if index != -1 else body


def _sql_error_text(output: Any) -> str:
    if isinstance(output, dict):
        error = output.get("error")
        return error.strip() if isinstance(error, str) else ""
    if isinstance(output, str):
        stripped = output.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return ""
            if isinstance(parsed, dict):
                error = parsed.get("error")
                return error.strip() if isinstance(error, str) else ""
    return ""


def _render_ask(tool_input: Any, tool_output: Any) -> str:
    """Render questions and answers; title lives on the card caption."""
    payload = tool_input if isinstance(tool_input, dict) else {}
    answers = _ask_answers(tool_output)
    items: list[str] = []
    for index, item in enumerate(_ask_questions(payload), start=1):
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        lines = [f"{index}. {question}"]
        answer = answers.get(question)
        if answer:
            lines.append(f"*{answer}*")
        elif tool_output is None:
            for option in item.get("options") or []:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()
                if label:
                    lines.append(f"- {label}")
        items.append("\n".join(lines))
    return "\n".join(items).strip()


def _ask_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("questions")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    question = str(payload.get("question") or "").strip()
    return (
        [{"question": question, "options": payload.get("options") or []}]
        if question
        else []
    )


def _ask_answers(output: Any) -> dict[str, str]:
    payload = output
    if isinstance(output, str):
        stripped = output.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = {}
    if not isinstance(payload, dict):
        return {}
    mapped: dict[str, str] = {}
    for item in payload.get("answers") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        custom = item.get("custom_text")
        selected = item.get("selected_options") or []
        if isinstance(custom, str) and custom.strip():
            mapped[question] = custom.strip()
        elif isinstance(selected, list) and selected:
            mapped[question] = "、".join(str(part) for part in selected if part)
    return mapped


def _skill_caption(card: Presentation | None) -> bool:
    if card is None:
        return False
    caption = card.caption.lower()
    return "技能" in card.caption or "skill" in caption


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

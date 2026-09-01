# -*- coding: utf-8 -*-
"""Incremental signal collection: one Chat's raw events in, one snapshot out.

Collecting throughout the run is what makes the recommendation affordable: by
the time the Chat ends there is nothing left to look up, only one model call to
make. Parsing SQL and tool results runs on a thread, so a large result can
never delay the reply.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from qwenpaw_data.host.core.algo.followup.models import Provenance, SignalSnapshot
from qwenpaw_data.host.core.algo.followup.relevance import (
    EntityEvidence,
    is_time_dimension,
    rank_entities,
)
from qwenpaw_data.host.core.algo.followup.settings import (
    ANSWER_LIMIT,
    COLLECTOR_QUEUE_SIZE,
    MAX_DIMENSIONS,
    MAX_METRICS,
    MIN_RELEVANCE,
    USER_INPUT_LIMIT,
)
from qwenpaw_data.host.core.algo.followup.skills import SKILL_INDEX

logger = logging.getLogger(__name__)

# File tools arrive as AgentScope class names; MCP tools as
# ``mcp__context-manager__<fn>``. After stripping the MCP prefix, everything
# below is keyed on the registered name the agent actually calls.
_CANONICAL_TOOL_NAMES: dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "multiedit": "edit_file",
    "skill": "Skill",
}
_PATH_KEYS = ("file_path", "path", "target_file", "output_path", "filename")
_FILE_WRITING_TOOLS = frozenset({"write_file"})
_PLAN_TOOLS = frozenset({"PlanCreate", "PlanUpdate"})
_TASK_STATE_TOOL = "TaskStateUpdate"
_SKILL_TOOL = "Skill"
_DIMENSION_DETAIL_TOOLS = frozenset(
    {"get_dimension", "get_dimension_values", "get_dimension_hierarchy"}
)
_METRIC_LISTING_TOOLS = frozenset(
    {"list_metrics", "get_priority_metrics", "get_north_star_metrics"}
)
_METRIC_RESULT_TOOLS = frozenset(
    {
        "get_metric",
        "list_metrics",
        "get_priority_metrics",
        "get_north_star_metrics",
        "list_dimensions_of_metric",
        "list_metrics_of_dimension",
    }
)
_DIMENSION_RESULT_TOOLS = frozenset(
    {"list_dimensions", "get_dimension", "get_dimension_hierarchy"}
)
_DATASET_RESULT_TOOLS = frozenset({"get_dataset", "list_datasets"})

_TOOL_ERROR_PREFIX = "Error executing tool"

_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/(?:.*/)?([^/]+)/SKILL\.md$")
# CM graph keys (met:/dim:/ds:…:SurfaceName). Follow-up only keeps the surface.
_GRAPH_KEY_RE = re.compile(
    r"^(?:met|dim|ds):(?:[^:\s]+:)+([^\s:\\,;]+)$", re.IGNORECASE
)
_SQL_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w.]*)", re.IGNORECASE)
# Names introduced by "WITH x AS (" are query-local aliases, not datasets.
_SQL_CTE_RE = re.compile(r"([a-zA-Z_]\w*)\s+AS\s*\(", re.IGNORECASE)
_SQL_IDENT_RE = re.compile(r"[a-zA-Z_][\w]*")
_SQL_GROUPBY_RE = re.compile(
    r"GROUP\s+BY(.*?)(?:ORDER\s+BY|LIMIT|HAVING|\)|$)", re.IGNORECASE | re.S
)
_SQL_WHERE_RE = re.compile(
    r"WHERE(.*?)(?:GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|\)|$)", re.IGNORECASE | re.S
)
_CTX_METRIC_RE = re.compile(r"met:[^:\s]+:[^:\s]+:([^\s\\,;]+)")
# One metric's whole block, so its drillable dimensions stay bound to it rather
# than pooling into one undifferentiated set.
_CTX_BLOCK_RE = re.compile(
    r"指标:\s*met:[^:\s]+:[^:\s]+:([^\s\\]+)((?:(?!指标:).)*)", re.S
)
_CTX_DIMENSIONS_RE = re.compile(r"可下钻维度:\s*([^\n\\]+)")
_NAME_SPLIT_RE = re.compile(r"[、,，;；/]\s*")

_ARTIFACT_KINDS: dict[str, str] = {
    ".html": "看板/报告页面",
    ".md": "文档",
    ".csv": "数据文件",
    ".json": "数据文件",
}
_ARTIFACT_FALLBACK = "其他产出"


@dataclass(slots=True)
class _PendingTool:
    """A tool call being streamed: its arguments and result arrive in pieces."""

    tool_name: str
    parts: list[str] = field(default_factory=list)
    output_parts: list[str] = field(default_factory=list)


class SignalCollector:
    """Fold a Chat's raw event flow into the signals a recommendation needs.

    Args:
        previous_followups: Questions already recommended earlier in this
            Session, so the ranking can avoid repeating them.
        max_metrics: Cap on metrics entering the prompt.
        max_dimensions: Cap on dimensions entering the prompt.
        min_relevance: Score an entity must reach to enter the prompt.
    """

    def __init__(
        self,
        *,
        previous_followups: tuple[str, ...] = (),
        max_metrics: int = MAX_METRICS,
        max_dimensions: int = MAX_DIMENSIONS,
        min_relevance: float = MIN_RELEVANCE,
    ) -> None:
        self._previous_followups = previous_followups
        self._max_metrics = max_metrics
        self._max_dimensions = max_dimensions
        self._min_relevance = min_relevance
        self._entries: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=COLLECTOR_QUEUE_SIZE
        )
        self._consumer: asyncio.Task[None] | None = None
        self._closed = False

        self._user_input = ""
        self._final_answer = ""
        self._texts: dict[tuple[str, str], list[str]] = {}
        self._tools: dict[str, _PendingTool] = {}
        self._node_names: dict[str, str] = {}
        self._node_states: dict[str, str] = {}
        self._skills: dict[str, None] = {}
        self._metrics: dict[str, EntityEvidence] = {}
        self._dimensions: dict[str, EntityEvidence] = {}
        self._datasets: dict[str, EntityEvidence] = {}
        self._metric_dimensions: dict[str, set[str]] = {}
        self._groupby_tokens: set[str] = set()
        self._where_tokens: set[str] = set()
        self._artifact_counts: dict[str, int] = {}
        self._artifact_paths: set[str] = set()
        self._position = 0
        self._intent_coverage = ""
        self._intent_gaps: tuple[str, ...] = ()
        self._intent_next_step = ""
        self._has_golden_query = False

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Spin up the consumer so submitting never has to do the work."""
        if self._consumer is None:
            self._consumer = asyncio.create_task(self._consume_loop())

    def submit(self, entry: dict[str, Any]) -> None:
        """Take one raw entry. Synchronous, non-blocking, never raises."""
        if self._closed:
            return
        try:
            self._entries.put_nowait(entry)
        except asyncio.QueueFull:
            logger.warning("Follow-up signal queue is full; dropped one entry")
        except Exception:
            logger.exception("Failed to enqueue a raw entry for follow-up signals")

    async def freeze(self) -> SignalSnapshot:
        """Drain the queue, stop the consumer, and derive the snapshot."""
        if not self._closed:
            self._closed = True
            self._entries.put_nowait(None)
            if self._consumer is not None:
                await self._consumer
                self._consumer = None
        return await asyncio.to_thread(self._build_snapshot)

    async def _consume_loop(self) -> None:
        while True:
            entry = await self._entries.get()
            if entry is None:
                return
            self._position += 1
            try:
                await self._feed(entry)
            except Exception:
                logger.exception("Follow-up signal collection failed on one entry")

    # -- ingestion --------------------------------------------------------- #

    async def _feed(self, entry: dict[str, Any]) -> None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        if entry.get("kind") == "user_input":
            self._on_user_message(payload)
            return

        event_type = str(payload.get("type") or "")
        if event_type == "TEXT_BLOCK_START":
            self._texts[_block_key(payload)] = []
        elif event_type == "TEXT_BLOCK_DELTA":
            parts = self._texts.setdefault(_block_key(payload), [])
            parts.append(str(payload.get("delta") or ""))
        elif event_type == "TEXT_BLOCK_END":
            self._close_text(_block_key(payload))
        elif event_type == "TOOL_CALL_START":
            self._on_tool_call_start(payload)
        elif event_type == "TOOL_CALL_DELTA":
            self._on_tool_call_delta(payload)
        elif event_type == "TOOL_CALL_END":
            await self._on_tool_call_end(payload)
        elif event_type == "TOOL_RESULT_TEXT_DELTA":
            self._on_tool_result_delta(payload)
        elif event_type == "TOOL_RESULT_END":
            await self._on_tool_result_end(payload)

    def _on_user_message(self, payload: dict[str, Any]) -> None:
        blocks = [
            block
            for block in payload.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(str(block.get("text") or "") for block in blocks).strip()
        if text:
            self._user_input = text

    def _close_text(self, key: tuple[str, str]) -> None:
        parts = self._texts.pop(key, None)
        if parts is None:
            return
        text = "".join(parts).strip()
        if text:
            self._final_answer = text

    def _on_tool_call_start(self, payload: dict[str, Any]) -> None:
        call_id = str(payload.get("tool_call_id") or "")
        if not call_id:
            return
        # TOOL_CALL_END carries no name, so it is remembered from the start.
        self._tools[call_id] = _PendingTool(
            tool_name=_canonical(payload.get("tool_call_name"))
        )

    def _on_tool_call_delta(self, payload: dict[str, Any]) -> None:
        pending = self._tools.get(str(payload.get("tool_call_id") or ""))
        if pending is not None:
            pending.parts.append(str(payload.get("delta") or ""))

    async def _on_tool_call_end(self, payload: dict[str, Any]) -> None:
        """Arguments are complete here, so the call can be read."""
        pending = self._tools.get(str(payload.get("tool_call_id") or ""))
        if pending is None:
            return
        parsed = _parse_json("".join(pending.parts))
        arguments = parsed if isinstance(parsed, dict) else {}
        name = pending.tool_name
        if name == _SKILL_TOOL:
            # Skill(skill="<name>") — the primary load path.
            self._record_skill_name(str(arguments.get("skill") or ""))
        elif name == "read_file":
            # Read of skills/<name>/SKILL.md — also counts as a skill use.
            self._record_skill_path(_first_str(arguments, _PATH_KEYS))
        elif name in _FILE_WRITING_TOOLS:
            self._record_artifact(_first_str(arguments, _PATH_KEYS))
        elif name == "execute_sql":
            await asyncio.to_thread(self._parse_sql, str(arguments.get("sql") or ""))
        elif name == "get_metric":
            self._touch(
                self._metrics,
                str(arguments.get("name") or ""),
                "targeted",
                analyzed=True,
            )
        elif name in _DIMENSION_DETAIL_TOOLS:
            self._touch(
                self._dimensions,
                str(arguments.get("name") or ""),
                "targeted",
                analyzed=True,
            )
        elif name == "get_dataset":
            self._touch(
                self._datasets, str(arguments.get("name") or "").lower(), "targeted"
            )
        elif name in _PLAN_TOOLS:
            self._register_plan_nodes(arguments)
        elif name == _TASK_STATE_TOOL:
            self._record_node_state(arguments)

    def _on_tool_result_delta(self, payload: dict[str, Any]) -> None:
        metadata = payload.get("metadata") or {}
        if metadata.get("subagent_event"):
            # Sub-agent chunks are a transcript of their own run, not this
            # agent's tool traffic; their entities arrive on the parent call.
            return
        pending = self._tools.get(str(payload.get("tool_call_id") or ""))
        if pending is not None:
            pending.output_parts.append(str(payload.get("delta") or ""))

    async def _on_tool_result_end(self, payload: dict[str, Any]) -> None:
        """A closed call is the only place entities can be trusted."""
        pending = self._tools.pop(str(payload.get("tool_call_id") or ""), None)
        state = str(payload.get("state") or "success").lower()
        if pending is None or state != "success":
            return
        text = "".join(pending.output_parts).strip()
        if not text or text.startswith(_TOOL_ERROR_PREFIX):
            return
        name = pending.tool_name
        # A domain-wide listing names entities nobody asked for, so it is graded
        # apart from a lookup that was aimed at one entity by name.
        if name == "search_context":
            await asyncio.to_thread(self._parse_search_context, text)
        elif name in _METRIC_RESULT_TOOLS:
            listing = "domain_dump" if name in _METRIC_LISTING_TOOLS else "metric_bound"
            await asyncio.to_thread(self._parse_metric_result, text, listing)
        elif name in _DIMENSION_RESULT_TOOLS:
            listing = "domain_dump" if name == "list_dimensions" else "targeted"
            await asyncio.to_thread(self._parse_dimension_result, text, listing)
        elif name in _DATASET_RESULT_TOOLS:
            await asyncio.to_thread(self._parse_dataset_result, text)

    # -- plan, skills and artifacts ---------------------------------------- #

    def _register_plan_nodes(self, arguments: dict[str, Any]) -> None:
        """Remember task id → subject so later TaskStateUpdate can be named.

        Matches ``PlanCreate`` (``tasks``) and ``PlanUpdate`` (``changes``).
        """

        for task in arguments.get("tasks") or []:
            if isinstance(task, dict):
                self._name_node(task.get("id"), task.get("subject"))
        for change in arguments.get("changes") or []:
            if not isinstance(change, dict):
                continue
            task = change.get("task")
            if isinstance(task, dict):
                self._name_node(change.get("task_id"), task.get("subject"))

    def _name_node(self, node_id: Any, name: Any) -> None:
        if isinstance(node_id, str) and node_id and isinstance(name, str) and name:
            self._node_names[node_id] = name

    def _record_node_state(self, arguments: dict[str, Any]) -> None:
        task_id = str(arguments.get("task_id") or "")
        state = str(arguments.get("state") or "")
        if task_id and state:
            self._node_states[task_id] = state

    def _record_skill_name(self, skill: str) -> None:
        """Record a skill loaded by name (``Skill`` tool)."""
        skill = skill.strip()
        if skill and skill in SKILL_INDEX:
            self._skills.setdefault(skill)

    def _record_skill_path(self, path: str) -> None:
        """Record a skill loaded by reading its ``SKILL.md`` (``Read`` tool)."""
        match = _SKILL_PATH_RE.search(path.replace("\\", "/"))
        if match is not None:
            self._record_skill_name(match.group(1))

    def _record_artifact(self, path: str) -> None:
        if not path or path in self._artifact_paths:
            return
        self._artifact_paths.add(path)
        suffix = path[path.rfind(".") :].lower() if "." in path else ""
        kind = _ARTIFACT_KINDS.get(suffix, _ARTIFACT_FALLBACK)
        self._artifact_counts[kind] = self._artifact_counts.get(kind, 0) + 1

    # -- entity parsing ---------------------------------------------------- #

    def _touch(
        self,
        store: dict[str, EntityEvidence],
        name: str,
        provenance: Provenance,
        *,
        analyzed: bool = False,
        **attributes: Any,
    ) -> None:
        """Record one entity's evidence, O(1) and never downgrading it."""
        name = _surface_name(name)
        if not name:
            return
        evidence = store.get(name)
        if evidence is None:
            evidence = EntityEvidence(name=name)
            store[name] = evidence
        else:
            evidence.touches += 1
        evidence.provenance.add(provenance)
        evidence.analyzed = evidence.analyzed or analyzed
        evidence.last_pos = self._position
        for key, value in attributes.items():
            if not value:
                continue
            if key == "aliases" and isinstance(value, tuple):
                value = tuple(
                    alias
                    for alias in (_surface_name(item) for item in value)
                    if alias and alias != name
                )
                if not value:
                    continue
            elif key == "parent" and isinstance(value, str):
                value = _surface_name(value)
                if not value:
                    continue
            setattr(evidence, key, value)

    def _parse_sql(self, sql: str) -> None:
        """Which clause a column appears in is what tells filtering from
        decomposition, so the two are tokenized separately."""

        aliases = {name.lower() for name in _SQL_CTE_RE.findall(sql)}
        for table in _SQL_TABLE_RE.findall(sql):
            short = table.split(".")[-1].lower()
            if (
                short
                and short not in aliases
                and not table.lower().startswith("information_schema")
            ):
                self._touch(self._datasets, short, "sql_groupby", analyzed=True)
        for clause_re, sink in (
            (_SQL_GROUPBY_RE, self._groupby_tokens),
            (_SQL_WHERE_RE, self._where_tokens),
        ):
            for clause in clause_re.findall(sql):
                sink.update(token.lower() for token in _SQL_IDENT_RE.findall(clause))

    def _parse_search_context(self, text: str) -> None:
        """Semantic lookups name far more entities than the run ends up using."""
        data = _parse_json(text)
        # The hints are where a lookup names what it matched. Reading them
        # decoded keeps the envelope's own punctuation out of the names.
        body = text
        if isinstance(data, dict):
            self._record_intent_feedback(data)
            hints = [str(data.get(key) or "") for key in ("schema_prompt", "path_hint")]
            body = "\n".join(hint for hint in hints if hint) or text
        for metric, block in _CTX_BLOCK_RE.findall(body):
            metric = _surface_name(metric)
            self._touch(self._metrics, metric, "metric_bound")
            bound = self._metric_dimensions.setdefault(metric, set())
            for group in _CTX_DIMENSIONS_RE.findall(block):
                for raw in _NAME_SPLIT_RE.split(group.strip()):
                    name = _surface_name(raw)
                    if not name:
                        continue
                    bound.add(name)
                    self._touch(
                        self._dimensions,
                        name,
                        "metric_bound",
                        is_time=is_time_dimension(name),
                    )
        for metric in _CTX_METRIC_RE.findall(body):
            self._touch(self._metrics, _surface_name(metric), "metric_bound")

    def _record_intent_feedback(self, data: dict[str, Any]) -> None:
        feedback = data.get("intent_feedback")
        if isinstance(feedback, dict):
            coverage = str(feedback.get("coverage") or "").strip()
            if coverage:
                self._intent_coverage = coverage
            gaps = feedback.get("gaps")
            if isinstance(gaps, list):
                self._intent_gaps = tuple(
                    str(gap).strip() for gap in gaps if str(gap).strip()
                )
            next_steps = feedback.get("next_steps")
            if isinstance(next_steps, list) and next_steps:
                self._intent_next_step = str(next_steps[0] or "").strip()
        golden = data.get("golden_query")
        if isinstance(golden, dict) and golden.get("verified_sql"):
            self._has_golden_query = True

    def _parse_metric_result(self, text: str, listing: Provenance) -> None:
        data = _parse_json(text)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("metric_name") or item.get("name")
                if isinstance(name, str):
                    self._touch(
                        self._metrics,
                        name,
                        listing,
                        aliases=_str_tuple(item.get("aliases")),
                    )
            return
        if not isinstance(data, dict):
            return
        matched = data.get("matched_name")
        if isinstance(matched, str):
            self._touch(self._metrics, matched, listing)
        for candidate in data.get("ambiguity_candidates") or []:
            if not isinstance(candidate, dict):
                continue
            store = (
                self._metrics
                if candidate.get("entity_type") == "Metric"
                else self._dimensions
            )
            self._touch(store, str(candidate.get("name") or ""), listing)
        name = data.get("name") or data.get("metric_name")
        if not data.get("ambiguous") and isinstance(name, str):
            self._touch(
                self._metrics,
                name,
                "targeted",
                analyzed=True,
                aliases=_str_tuple(data.get("aliases")),
            )

    def _parse_dimension_result(self, text: str, listing: Provenance) -> None:
        for item in _as_items(_parse_json(text)):
            name = _surface_name(
                str(item.get("dimension_name") or item.get("name") or "")
            )
            if not name:
                continue
            aliases = _str_tuple(item.get("aliases"))
            # Both the aliases and the expression's identifiers are candidate
            # physical columns, which is how SQL traffic is matched back here.
            columns = {alias.lower() for alias in aliases}
            expression = str(item.get("calculate_expr") or "")
            columns.update(
                token.lower() for token in _SQL_IDENT_RE.findall(expression)
            )
            columns.discard("select")
            parent = _surface_name(str(item.get("parent_dimension") or ""))
            self._touch(
                self._dimensions,
                name,
                listing,
                analyzed=listing == "targeted",
                aliases=aliases,
                columns=columns,
                parent=parent,
                is_time=is_time_dimension(name, str(item.get("dimension_type") or "")),
            )
            if parent:
                self._touch(
                    self._dimensions,
                    parent,
                    listing,
                    is_time=is_time_dimension(parent),
                )

    def _parse_dataset_result(self, text: str) -> None:
        for item in _as_items(_parse_json(text)):
            name = item.get("dataset_name") or item.get("name")
            if isinstance(name, str) and name:
                self._touch(self._datasets, name.lower(), "targeted")

    # -- freezing ---------------------------------------------------------- #

    def _apply_sql_evidence(self) -> None:
        """Promote dimensions whose columns show up in a GROUP BY or WHERE.

        A dimension in the GROUP BY was genuinely decomposed by, so it counts as
        analyzed; one appearing only in the WHERE was merely filtered on and is
        still worth offering as a breakdown.
        """

        for evidence in self._dimensions.values():
            probes = evidence.columns or {evidence.name.lower()}
            if probes & self._groupby_tokens:
                evidence.provenance.add("sql_groupby")
                evidence.analyzed = True
            elif probes & self._where_tokens:
                evidence.provenance.add("sql_where")

    def _build_snapshot(self) -> SignalSnapshot:
        for key in list(self._texts):
            # A reply cut short never closed its block, and its text is still
            # the best answer summary available.
            self._close_text(key)
        self._apply_sql_evidence()
        ranked = rank_entities(
            self._metrics,
            self._dimensions,
            self._datasets,
            self._user_input,
            self._metric_dimensions,
            max_metrics=self._max_metrics,
            max_dimensions=self._max_dimensions,
            min_relevance=self._min_relevance,
        )
        return SignalSnapshot(
            user_input=self._user_input[:USER_INPUT_LIMIT],
            final_answer_summary=self._final_answer[:ANSWER_LIMIT],
            completed_nodes=tuple(
                f"{self._node_names.get(node_id, node_id)}: {state}"
                for node_id, state in self._node_states.items()
            ),
            skills_used=tuple(self._skills),
            anchor_metric=ranked.anchor,
            metrics=ranked.metrics,
            dimensions=ranked.dimensions,
            datasets=ranked.datasets,
            unused_dimensions=ranked.unused_dimensions,
            business_entities=ranked.business_entities,
            entity_aliases=ranked.aliases,
            artifacts_summary=self._artifacts_summary(),
            previous_followups=self._previous_followups,
            intent_coverage=self._intent_coverage,
            intent_gaps=self._intent_gaps,
            intent_next_step=self._intent_next_step,
            has_golden_query=self._has_golden_query,
        )

    def _artifacts_summary(self) -> str:
        if not self._artifact_counts:
            return "无产出物"
        produced = "、".join(
            f"{count} 个{kind}" for kind, count in self._artifact_counts.items()
        )
        return f"已产出 {produced}"


def _canonical(tool_name: Any) -> str:
    """Normalize a registered tool name to the name the PRD uses."""
    if not isinstance(tool_name, str) or not tool_name:
        return ""
    raw = tool_name.rsplit("__", 1)[-1].strip()
    return _CANONICAL_TOOL_NAMES.get(raw.lower(), raw)


def _block_key(payload: dict[str, Any]) -> tuple[str, str]:
    return str(payload.get("reply_id") or ""), str(payload.get("block_id") or "")


def _clean_name(name: str) -> str:
    """Strip the punctuation a name picks up when it is read out of raw text."""
    return name.strip().strip('\\"\'`,;。，、[](){}')


def _surface_name(name: str) -> str:
    """Human-facing entity name: drop CM graph-key prefixes when present.

    ``met:postgresql-…:QwenChat:DAU`` and ``met:QwenChat:DAU`` both become
    ``DAU``. Plain display names pass through unchanged.
    """
    cleaned = _clean_name(name)
    if not cleaned:
        return ""
    match = _GRAPH_KEY_RE.fullmatch(cleaned)
    return match.group(1) if match is not None else cleaned


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _as_items(data: Any) -> list[dict[str, Any]]:
    """Read a result that may be one object or a list of them."""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


__all__ = ["SignalCollector"]

"""Host tracing helpers: span metadata, tool/skill stamps, request-context reads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from qwenpaw_data.tracing.core.names import Step
from opentelemetry.sdk.trace import ReadableSpan, Span
from opentelemetry.trace import Status, StatusCode

# Tool result states that mean the call failed.
FAILED_TOOL_STATES = frozenset({"error", "interrupted", "denied"})

_PENDING_TOOLS = "agentscope.agent.external_execution_pending_tools"

# Span name -> (structured output key, metadata key). The full JSON stays on
# the observation output; these keys are the ones the error dashboard
# filters. ``continues`` is stored as ``true``/``false``; a null
# ``failure_kind`` is a success and is left unset.
_SPAN_FIELDS = {
    Step.SEGMENT_KIND: ("kind", "segment_kind"),
    Step.CONTINUITY_DECISION: ("continues", "continues"),
    Step.SQL_RESULT: ("failure_kind", "failure_kind"),
}


def span_metadata(span_name: str, content: Any) -> dict[str, str]:
    """Metadata key/values dashboards can group on for one structured call."""
    spec = _SPAN_FIELDS.get(span_name)
    if spec is None or not isinstance(content, dict):
        return {}
    source_key, metadata_key = spec
    value = content.get(source_key)
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, str) and value:
        text = value
    else:
        return {}
    return {metadata_key: text}


def attach_biztrace_metadata(gen: Any, span_name: str, response: Any) -> None:
    content = getattr(response, "content", None)
    for key, value in span_metadata(span_name, content).items():
        gen.metadata(key, value)


def skill_name_from_arguments(raw: object) -> str | None:
    """Read the skill name from a Skill tool call's arguments."""
    payload: object = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("skill")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def datasource_and_datasets(
    request_context: Mapping[str, Any],
) -> tuple[str | None, list | None]:
    """Typed reads of the request-context fields trace metadata uses."""
    datasource_id = request_context.get("datasource_id")
    dataset_names = request_context.get("dataset_names")
    return (
        datasource_id if isinstance(datasource_id, str) else None,
        dataset_names if isinstance(dataset_names, list) else None,
    )


def stamp_skill_name(span: Span) -> None:
    attributes = span.attributes
    if attributes.get("gen_ai.tool.name") != "Skill":
        return
    skill_name = skill_name_from_arguments(attributes.get("gen_ai.tool.call.arguments"))
    if skill_name:
        span.set_attribute("skill.name", skill_name)


def clear_clarification_pause(span: ReadableSpan) -> None:
    """A reply closed to wait for ask_user_question is not a failure.

    TracingMiddleware records the ``aclose()`` ``GeneratorExit`` as ERROR.
    The chat stays running. Drop that status before export. A real exception
    on the same span, or a user stop without a pending tool, stays ERROR.
    """
    if span.status.status_code != StatusCode.ERROR:
        return
    attributes = span.attributes
    if not _pending_external_tools(attributes.get(_PENDING_TOOLS)):
        return
    events = list(span.events)
    exceptions = [event for event in events if event.name == "exception"]
    if not exceptions or any(not _generator_exit(event) for event in exceptions):
        return
    span._events = [event for event in events if not _generator_exit(event)]  # type: ignore[attr-defined]
    span._status = Status(StatusCode.OK)  # type: ignore[attr-defined]


def restore_tool_error_status(span: ReadableSpan) -> None:
    """Re-mark a failed tool span as ERROR before export.

    ``stamp_tool_state`` sets ERROR while the tool span is open, but
    AgentScope's TracingMiddleware closes every clean stream with
    ``set_status(OK)``, and the OTEL SDK lets OK overwrite ERROR — never the
    reverse. The ``langfuse.observation.level`` attribute survives that
    overwrite; the span status does not. Restore it here so consumers that
    read plain OTEL status see the tool failure too.
    """
    if span.status.status_code != StatusCode.OK:
        return
    attributes = span.attributes
    state = attributes.get("langfuse.observation.metadata.tool_state")
    if state not in FAILED_TOOL_STATES:
        return
    span._status = Status(StatusCode.ERROR, str(state))  # type: ignore[attr-defined]


def _pending_external_tools(value: object) -> bool:
    # AgentScope writes the attribute as a JSON string, only when non-empty.
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and text not in {"[]", "null"}


def _generator_exit(event: object) -> bool:
    # Callers pass only events named "exception".
    attributes = getattr(event, "attributes", None) or {}
    return attributes.get("exception.type") == "GeneratorExit"


__all__ = [
    "FAILED_TOOL_STATES",
    "attach_biztrace_metadata",
    "clear_clarification_pause",
    "datasource_and_datasets",
    "restore_tool_error_status",
    "skill_name_from_arguments",
    "span_metadata",
    "stamp_skill_name",
]

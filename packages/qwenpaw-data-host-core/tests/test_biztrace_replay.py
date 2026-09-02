# -*- coding: utf-8 -*-
"""End-to-end replay of a recorded AgentScope run through the pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qwenpaw_data.host.core.algo.biztrace.models import BizTrace
from qwenpaw_data.host.core.algo.biztrace.pipeline import BizTracePipeline
from qwenpaw_data.host.core.algo.biztrace.presentation import PresentationBuilder
from qwenpaw_data.host.core.algo.biztrace.settings import BizTraceSettings
from qwenpaw_data.host.core.algo.biztrace.store import BizTraceStore, build_store_paths

TRACE_EVENTS = (
    Path(__file__).resolve().parents[3] / "assets" / "trace_events.jsonl"
)


def _entries() -> list[dict[str, Any]]:
    if not TRACE_EVENTS.is_file():
        pytest.skip(f"recorded trace not available at {TRACE_EVENTS}")
    return [
        {"kind": "agent_event", "payload": json.loads(line)}
        for line in TRACE_EVENTS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _pipeline(tmp_path: Path, *, published: list[dict[str, Any]]) -> BizTracePipeline:
    async def biz_event_callback(event: dict[str, Any]) -> None:
        published.append(event)

    settings = BizTraceSettings(trace2segment_enabled=False, biz_link_enabled=False)
    return BizTracePipeline(
        session_id="ses-1",
        store=BizTraceStore(
            build_store_paths(session_id="ses-1", log_dir=str(tmp_path))
        ),
        presenter=PresentationBuilder(),
        settings=settings,
        biz_event_callback=biz_event_callback,
    )


async def test_recorded_run_converts_and_persists(tmp_path: Path) -> None:
    published: list[dict[str, Any]] = []
    pipeline = _pipeline(tmp_path, published=published)
    pipeline.start()

    for entry in _entries():
        pipeline.on_trace_event(entry)
    await pipeline.aclose()

    assert [event["presentation"]["card_type"] for event in published] == [
        "tool",
        "tool",
        "tool",
        "tool",
        "text",
    ]
    assert [event["seq"] for event in published] == [1, 2, 3, 4, 5]
    assert all(event["status"] == "done" for event in published)
    # The Write call and its result share the block the frontend replaces.
    assert published[0]["block_id"] == published[1]["block_id"]
    assert published[0]["block_id"] != published[2]["block_id"]
    assert published[0]["presentation"]["caption"] != ""


async def test_the_persisted_rows_rebuild_the_same_trace(tmp_path: Path) -> None:
    published: list[dict[str, Any]] = []
    pipeline = _pipeline(tmp_path, published=published)
    pipeline.start()

    for entry in _entries():
        pipeline.on_trace_event(entry)
    await pipeline.aclose()

    rows = await pipeline.store.read_events()
    trace = BizTrace.from_rows(rows, session_id="ses-1")

    assert [event.event_id for event in trace.events] == [
        event["event_id"] for event in published
    ]
    assert [event.kind for event in trace.events] == [
        "tool_use",
        "tool_result",
        "tool_use",
        "tool_result",
        "assistant_text",
    ]
    write_call = trace.events[0]
    assert write_call.tool_name == "Write"
    assert write_call.input["file_path"].endswith("hello.txt")
    assert "written successfully" in trace.events[1].output

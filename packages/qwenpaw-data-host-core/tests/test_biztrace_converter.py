# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.converter import BizTraceConverter
from qwenpaw_data.host.core.algo.biztrace.models import BizEvent
from qwenpaw_data.host.core.algo.biztrace.presentation import PresentationBuilder

REPLY_ID = "reply-1"


class Collector:
    """Records every row and every post-emit event, in arrival order."""

    def __init__(self) -> None:
        self.rows: list[BizEvent] = []
        self.events: list[BizEvent] = []

    async def on_row(self, event: BizEvent) -> None:
        self.rows.append(event)

    async def on_event(self, event: BizEvent) -> None:
        self.events.append(event)


def _agent_event(**payload: Any) -> dict[str, Any]:
    payload.setdefault("reply_id", REPLY_ID)
    return {"kind": "agent_event", "payload": payload}


def _text_block(block_id: str, text: str) -> list[dict[str, Any]]:
    return [
        _agent_event(type="TEXT_BLOCK_START", block_id=block_id),
        _agent_event(type="TEXT_BLOCK_DELTA", block_id=block_id, delta=text),
        _agent_event(type="TEXT_BLOCK_END", block_id=block_id),
    ]


def _tool_pair(
    call_id: str,
    name: str,
    arguments: str,
    *,
    output: str = "ok",
    state: str = "success",
    close: bool = True,
) -> list[dict[str, Any]]:
    entries = [
        _agent_event(
            type="TOOL_CALL_START", tool_call_id=call_id, tool_call_name=name
        ),
        _agent_event(type="TOOL_CALL_DELTA", tool_call_id=call_id, delta=arguments),
        # TOOL_CALL_END carries no tool name; it must come from the start event.
        _agent_event(type="TOOL_CALL_END", tool_call_id=call_id),
    ]
    if not close:
        return entries
    return entries + [
        _agent_event(type="TOOL_RESULT_START", tool_call_id=call_id),
        _agent_event(
            type="TOOL_RESULT_TEXT_DELTA", tool_call_id=call_id, delta=output
        ),
        # TOOL_RESULT_END carries no output; only the deltas do.
        _agent_event(type="TOOL_RESULT_END", tool_call_id=call_id, state=state),
    ]


async def _run(entries: list[dict[str, Any]]) -> tuple[Collector, BizTraceConverter]:
    collector = Collector()
    converter = BizTraceConverter(
        presenter=PresentationBuilder(),
        on_row=collector.on_row,
        on_event=collector.on_event,
    )
    converter.start()
    for entry in entries:
        converter.enqueue(entry)
    await converter.aclose()
    return collector, converter


async def test_events_map_one_to_one_with_stable_ids() -> None:
    entries = [
        {
            "kind": "user_input",
            "payload": {
                "id": "msg-1",
                "content": [{"type": "text", "id": "blk-u", "text": "分析日活"}],
            },
        },
        _agent_event(type="THINKING_BLOCK_START", block_id="blk-t"),
        _agent_event(type="THINKING_BLOCK_DELTA", block_id="blk-t", delta="先取数"),
        _agent_event(type="THINKING_BLOCK_END", block_id="blk-t"),
        *_tool_pair("call-1", "Bash", '{"command": "ls"}'),
        *_text_block("blk-a", "结论如下"),
        # Neither of these produces a card.
        _agent_event(type="MODEL_CALL_START"),
        _agent_event(type="REPLY_END"),
    ]

    collector, converter = await _run(entries)

    assert [(event.kind, event.event_id) for event in collector.rows] == [
        ("user", "blk-u"),
        ("assistant_thinking", "blk-t"),
        ("tool_use", "call-1"),
        ("tool_result", "call-1:result"),
        ("assistant_text", "blk-a"),
    ]
    assert [event.seq for event in collector.rows] == [1, 2, 3, 4, 5]
    assert converter.stats.events == 5
    assert converter.stats.tool_calls == 1


async def test_user_event_id_falls_back_to_the_message() -> None:
    collector, _ = await _run(
        [
            {
                "kind": "user_input",
                "payload": {"id": "msg-9", "content": [{"type": "text", "text": "hi"}]},
            }
        ]
    )

    row = collector.rows[0]
    assert row.event_id == "msg-9:0"
    assert row.parent_msg_id == "msg-9"
    assert row.block_id == "msg-9:0"


async def test_tool_pair_shares_a_block_id_and_keeps_its_arguments() -> None:
    collector, _ = await _run(
        _tool_pair("call-1", "Write", '{"file_path": "a.md", "content": "x"}')
    )

    call, result = collector.rows
    assert call.block_id == result.block_id == "call-1"
    assert call.parent_msg_id == REPLY_ID
    assert call.tool_name == "Write"
    assert call.input == {"file_path": "a.md", "content": "x"}
    assert result.tool_name == "Write"
    assert result.output == "ok"
    assert call.status == result.status == "done"


async def test_failed_states_map_to_error_and_explain_themselves() -> None:
    collector, converter = await _run(
        [
            *_tool_pair("call-1", "Bash", "{}", state="error"),
            *_tool_pair("call-2", "Bash", "{}", state="interrupted"),
        ]
    )

    error_result = collector.rows[1]
    interrupted_result = collector.rows[3]
    assert error_result.status == "error"
    assert interrupted_result.status == "error"
    assert interrupted_result.presentation is not None
    assert "打断" in interrupted_result.presentation.body
    assert converter.stats.error_tools == 2


async def test_unclosed_call_is_rewritten_once_and_not_forwarded_again() -> None:
    collector, converter = await _run(
        _tool_pair("call-1", "Bash", '{"command": "sleep"}', close=False)
    )

    first, rewrite = collector.rows
    assert first.status == "done"
    assert rewrite.status == "error"
    assert rewrite.event_id == first.event_id
    # The rewrite lands in the slot the first row took, and segmentation has
    # already consumed that event.
    assert rewrite.seq == first.seq == 1
    assert [event.event_id for event in collector.events] == ["call-1"]
    assert converter.stats.unclosed_tools == 1
    assert converter.stats.events == 1


async def test_hint_becomes_a_rule_based_card() -> None:
    collector, _ = await _run(
        [
            _agent_event(
                type="HINT_BLOCK",
                block_id="blk-h",
                source="interaction-strategy",
                hint=[{"type": "text", "text": "先确认口径"}],
            )
        ]
    )

    row = collector.rows[0]
    assert row.kind == "assistant_text"
    assert row.event_id == "blk-h"
    assert row.content == "先确认口径"
    assert row.presentation is not None
    assert row.presentation.card_type == "text"
    assert row.presentation.caption == "模型回复"
    assert "先确认口径" in row.presentation.body
    assert "interaction-strategy" not in row.presentation.body


async def test_steer_hint_stays_user_guidance() -> None:
    collector, _ = await _run(
        [
            _agent_event(
                type="HINT_BLOCK",
                block_id="blk-s",
                source="steer",
                hint=[{"type": "text", "text": "请改用周口径"}],
            )
        ]
    )

    row = collector.rows[0]
    assert row.kind == "hint"
    assert row.source == "steer"
    assert row.content == "请改用周口径"
    assert row.presentation is not None
    assert row.presentation.card_type == "hint"
    assert row.presentation.caption == "用户引导"
    assert "请改用周口径" in row.presentation.body


async def test_non_steer_system_reminder_becomes_assistant_text() -> None:
    collector, _ = await _run(
        [
            _agent_event(
                type="HINT_BLOCK",
                block_id="blk-r",
                hint=[
                    {
                        "type": "text",
                        "text": (
                            "<system-reminder>\n"
                            "Remember to cite sources.\n"
                            "</system-reminder>"
                        ),
                    }
                ],
            )
        ]
    )

    row = collector.rows[0]
    assert row.kind == "assistant_text"
    assert row.content == "Remember to cite sources."
    assert "<system-reminder>" not in (row.content or "")
    assert row.presentation is not None
    assert row.presentation.card_type == "text"
    assert row.presentation.caption == "模型回复"
    assert "Remember to cite sources." in row.presentation.body
    assert "<system-reminder>" not in row.presentation.body


async def test_rows_keep_arrival_order_when_cards_finish_out_of_order() -> None:
    class SlowFirstPresenter(PresentationBuilder):
        """Delays the first card so a later one would win a race."""

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def build(self, event: BizEvent, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.05)
            return await super().build(event, **kwargs)

    collector = Collector()
    converter = BizTraceConverter(
        presenter=SlowFirstPresenter(), on_row=collector.on_row
    )
    converter.start()
    for entry in [*_text_block("blk-a", "one"), *_text_block("blk-b", "two")]:
        converter.enqueue(entry)
    await converter.aclose()

    assert [event.event_id for event in collector.rows] == ["blk-a", "blk-b"]
    assert [event.seq for event in collector.rows] == [1, 2]


async def test_enqueue_after_close_is_ignored() -> None:
    collector, converter = await _run(_text_block("blk-a", "done"))

    converter.enqueue(_agent_event(type="TEXT_BLOCK_START", block_id="blk-b"))

    assert [event.event_id for event in collector.rows] == ["blk-a"]


async def test_host_plan_tools_become_orchestration_events() -> None:
    """Force-segment tools must match PlanCreate / PlanUpdate / TaskStateUpdate."""
    collector, converter = await _run(
        [
            *_tool_pair(
                "c1",
                "PlanCreate",
                '{"tasks":[{"id":"n1","subject":"取数","description":"拉明细",'
                '"blocked_by":[]}]}',
            ),
            *_tool_pair(
                "c2",
                "TaskStateUpdate",
                '{"task_id":"n1","state":"in_progress"}',
            ),
            *_tool_pair(
                "c3",
                "PlanUpdate",
                '{"changes":[{"task_id":"n2","action":"add","task":'
                '{"subject":"出图","description":"画趋势","blocked_by":["n1"]}}]}',
            ),
        ]
    )

    create, update_state, revise = [
        row for row in collector.rows if row.kind == "tool_use"
    ]
    assert create.orchestration is not None
    assert create.orchestration.category == "PlanCreate"
    assert converter._plan_nodes["n1"] == "取数"

    assert update_state.orchestration is not None
    assert update_state.orchestration.category == "TaskStateUpdate"
    assert update_state.orchestration.subtask_state == "in_progress"
    assert update_state.orchestration.node_id == "n1"
    assert update_state.orchestration.node_name == "取数"

    assert revise.orchestration is not None
    assert revise.orchestration.category == "PlanUpdate"
    assert converter._plan_nodes["n2"] == "出图"
    assert converter.stats.orchestration_events == 3

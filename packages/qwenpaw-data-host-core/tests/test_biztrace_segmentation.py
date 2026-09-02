# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.models import BizEvent, OrchestrationInfo, Segment
from qwenpaw_data.host.core.algo.biztrace.segmentation import (
    ChainBuilder,
    ContinuityJudge,
    SegmentAssembler,
    SegmentExtractor,
)
from qwenpaw_data.host.core.algo.biztrace.workspace_index import ArtifactVerifier

class FakeJudgeLLM:
    """Answers continuity questions from a queue of scripted decisions."""

    def __init__(self, decisions: list[bool]) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    async def complete(self, **kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        self.calls += 1
        continues = self.decisions.pop(0) if self.decisions else True
        return {"continues": continues, "reason": "scripted"}


class FakeExtractLLM:
    """Returns one canned summary for every window."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {
            "title": "取数并核对",
            "input": "分析日活",
            "behavior": "读取明细并比较两周数据。",
            "conclusion": "周末效应导致下降。",
            "artifact": None,
        }
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(str(kwargs.get("user") or ""))
        return self.payload


class FakeRegistry:
    """Stands in for coverage-scoped agent-view files from artifact_delta."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = files or {}
        self.calls = 0
        self.ranges: list[tuple[int, int]] = []

    def __call__(self, start_seq: int, end_seq: int) -> dict[str, str]:
        self.calls += 1
        self.ranges.append((start_seq, end_seq))
        return dict(self.files)


class Collector:
    def __init__(self) -> None:
        self.rows: list[Segment] = []
        self.done: list[Segment] = []
        self.logs: list[dict[str, Any]] = []

    async def on_row(self, segment: Segment) -> None:
        self.rows.append(segment)

    async def on_segment(self, segment: Segment) -> None:
        self.done.append(segment)

    async def on_judge_log(self, entry: dict[str, Any]) -> None:
        self.logs.append(entry)


_SEQ = iter(range(1, 10_000))


def _event(kind: str, **fields: Any) -> BizEvent:
    fields.setdefault("event_id", f"e{next(_SEQ)}")
    fields.setdefault("seq", next(_SEQ))
    fields.setdefault("parent_msg_id", "reply-1")
    fields.setdefault("started_at", 1.0)
    fields.setdefault("ended_at", 2.0)
    return BizEvent(kind=kind, **fields)  # type: ignore[arg-type]


def _text(content: str, msg_id: str) -> BizEvent:
    return _event("assistant_text", content=content, parent_msg_id=msg_id)


def _tool_use(name: str, payload: Any, *, call_id: str, **fields: Any) -> BizEvent:
    return _event(
        "tool_use",
        event_id=call_id,
        block_id=call_id,
        tool_name=name,
        input=payload,
        **fields,
    )


def _tool_result(name: str, output: Any, *, call_id: str, **fields: Any) -> BizEvent:
    return _event(
        "tool_result",
        event_id=f"{call_id}:result",
        block_id=call_id,
        tool_name=name,
        output=output,
        **fields,
    )


def _assembler(
    *,
    judge: FakeJudgeLLM | None = None,
    extract: FakeExtractLLM | None = None,
    max_span: int = 100,
    artifact_files: FakeRegistry | None = None,
    verifier: ArtifactVerifier | None = None,
    linker: Callable[[str], str] | None = None,
) -> tuple[SegmentAssembler, Collector]:
    collector = Collector()
    assembler = SegmentAssembler(
        session_id="ses-1",
        judge=ContinuityJudge(llm=judge),  # type: ignore[arg-type]
        extractor=SegmentExtractor(
            llm=extract if extract is not None else FakeExtractLLM(),  # type: ignore[arg-type]
            verifier=verifier,
            linker=linker,
        ),
        on_row=collector.on_row,
        on_segment=collector.on_segment,
        on_judge_log=collector.on_judge_log,
        max_span=max_span,
        artifact_files=artifact_files,
    )
    return assembler, collector


# -- chain building --------------------------------------------------------- #


def test_one_message_folds_into_one_node() -> None:
    builder = ChainBuilder()
    events = [
        _event("assistant_thinking", content="先取数"),
        _tool_use("Bash", {"command": "ls"}, call_id="c1"),
        _tool_result("Bash", "ok", call_id="c1"),
        _event("assistant_text", content="完成"),
    ]

    closed = [node for event in events for node in builder.push(event)]
    closed.extend(builder.flush())

    assert len(closed) == 1
    node = closed[0]
    assert node.kind == "normal"
    assert [block["type"] for block in node.content] == [
        "thinking",
        "tool_use",
        "tool_result",
        "text",
    ]
    # The two halves of the tool pair are joined by the same call key.
    assert node.content[1]["call"] == node.content[2]["call"]


def test_a_new_message_starts_a_new_node() -> None:
    builder = ChainBuilder()
    builder.push(_text("第一轮", "r1"))
    closed = builder.push(_text("第二轮", "r2"))

    assert [node.parent_msg_id for node in closed] == ["r1"]
    assert [node.parent_msg_id for node in builder.flush()] == ["r2"]


def test_hint_and_orchestration_split_a_message_open() -> None:
    builder = ChainBuilder()
    builder.push(_event("assistant_text", content="前半"))
    hint_nodes = builder.push(_event("hint", content="换个口径"))
    held = builder.push(
        _tool_use(
            "TaskStateUpdate",
            {"task_id": "n1", "state": "in_progress"},
            call_id="c9",
            orchestration=OrchestrationInfo(
                category="TaskStateUpdate",
                subtask_state="in_progress",
                node_id="n1",
                node_name="取数",
            ),
        )
    )
    subtask_nodes = builder.push(
        _tool_result("TaskStateUpdate", "ok", call_id="c9")
    )

    assert [node.kind for node in hint_nodes] == ["normal", "hint"]
    assert hint_nodes[1].covered is False
    assert held == []
    assert [node.kind for node in subtask_nodes] == ["orchestration"]
    assert subtask_nodes[0].orchestration is not None
    # Covered for FE fold under a segment; excluded from summary separately.
    assert subtask_nodes[0].covered is True
    assert subtask_nodes[0].event_ids == ["c9", "c9:result"]


def test_a_question_is_uncovered_until_its_answer_arrives() -> None:
    builder = ChainBuilder()
    builder.push(_event("assistant_text", content="需要确认"))
    nodes = builder.push(
        _tool_use("ask_user_question", {"question": "看哪个口径?"}, call_id="q1")
    )
    ask = nodes[1]
    assert [node.kind for node in nodes] == ["normal", "ask"]
    assert ask.covered is False

    trailing = builder.push(
        _tool_result("ask_user_question", "看自然日", call_id="q1")
    )

    assert trailing == []
    assert ask.covered is True
    assert len(ask.content) == 2


def test_pending_task_state_does_not_force_an_orchestration_node() -> None:
    """Host TaskStateUpdate(pending) is bookkeeping; it stays in the open bubble."""
    builder = ChainBuilder()
    builder.push(_event("assistant_text", content="准备开干"))
    closed = builder.push(
        _tool_use(
            "TaskStateUpdate",
            {"task_id": "n1", "state": "pending"},
            call_id="c1",
            orchestration=OrchestrationInfo(
                category="TaskStateUpdate",
                subtask_state="pending",
                node_id="n1",
            ),
        )
    )

    assert closed == []
    flushed = builder.flush()
    assert [node.kind for node in flushed] == ["normal"]


# -- windowing -------------------------------------------------------------- #


async def test_the_opening_user_message_becomes_the_query_not_a_segment() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))

    await assembler.feed(_event("user", content="分析日活下降"))
    await assembler.aclose()

    assert collector.rows == []
    assert assembler.extractor.query == "分析日活下降"


async def test_a_natural_boundary_closes_one_segment_and_opens_the_next() -> None:
    judge = FakeJudgeLLM([False])
    assembler, collector = _assembler(judge=judge)

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("第一步", "r1"))
    await assembler.feed(_text("第二步", "r2"))
    await assembler.aclose()

    assert judge.calls == 1
    assert [segment.boundary_reason for segment in collector.done] == [
        "natural",
        "session_end",
    ]
    assert [segment.status for segment in collector.done] == ["done", "done"]
    assert collector.done[0].title == "取数并核对"
    assert any(entry["type"] == "continuity" for entry in collector.logs)


async def test_a_hint_clears_the_draft_without_emitting_a_segment() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("半成品", "r1"))
    await assembler.feed(_event("hint", content="口径换成自然日"))
    await assembler.feed(_text("重来", "r2"))
    await assembler.aclose()

    assert assembler.stats.cleared_by_hint == 1
    assert [segment.boundary_reason for segment in collector.done] == ["session_end"]
    # The discarded draft is not part of what the surviving segment covers.
    assert len(collector.done[0].coverage.event_ids) == 1


async def test_a_question_delimits_and_its_answer_joins_the_next_segment() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("先跑一版", "r1"))
    await assembler.feed(
        _tool_use("ask_user_question", {"question": "口径?"}, call_id="q1")
    )
    await assembler.feed(_tool_result("ask_user_question", "自然日", call_id="q1"))
    await assembler.feed(_text("按自然日重跑", "r2"))
    await assembler.aclose()

    reasons = [segment.boundary_reason for segment in collector.done]
    assert reasons == ["ask_user_question", "session_end"]
    assert "q1" not in collector.done[0].coverage.event_ids
    covered_ids = collector.done[1].coverage.event_ids
    assert "q1" in covered_ids and "q1:result" in covered_ids


async def test_an_unanswered_question_does_not_enter_coverage() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("先跑一版", "r1"))
    await assembler.feed(
        _tool_use("ask_user_question", {"question": "口径?"}, call_id="q1")
    )
    await assembler.aclose()

    assert [segment.boundary_reason for segment in collector.done] == [
        "ask_user_question"
    ]
    assert "q1" not in collector.done[0].coverage.event_ids


async def test_a_user_message_forces_a_boundary_and_replaces_the_query() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("做完了", "r1"))
    await assembler.feed(_event("user", content="再看月活"))
    await assembler.feed(_text("再做一遍", "r2"))
    await assembler.aclose()

    assert [segment.boundary_reason for segment in collector.done] == [
        "user_message",
        "session_end",
    ]
    assert assembler.extractor.query == "再看月活"


async def test_a_sub_task_scopes_the_segments_it_opens() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))
    scope = OrchestrationInfo(
        category="TaskStateUpdate",
        subtask_state="in_progress",
        node_id="n1",
        node_name="取数",
    )

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(
        _tool_use("TaskStateUpdate", {}, call_id="c1", orchestration=scope)
    )
    await assembler.feed(
        _tool_result("TaskStateUpdate", "ok", call_id="c1")
    )
    await assembler.feed(_text("取数完成", "r1"))
    await assembler.feed(
        _tool_use(
            "TaskStateUpdate",
            {},
            call_id="c2",
            orchestration=scope.model_copy(update={"subtask_state": "completed"}),
        )
    )
    await assembler.feed(
        _tool_result("TaskStateUpdate", "ok", call_id="c2")
    )
    await assembler.aclose()

    # The closing TaskStateUpdate ends the scope, so it produces the only
    # segment and nothing is left to delimit at session end. Bookkeeping calls
    # and results are covered for FE fold, but are not the only content.
    assert len(collector.done) == 1
    assert collector.done[0].boundary_reason == "TaskStateUpdate"
    assert collector.done[0].subtask is not None
    assert collector.done[0].subtask.node_name == "取数"
    covered = set(collector.done[0].coverage.event_ids)
    assert {"c1", "c1:result", "c2", "c2:result"} <= covered
    assert len(covered) == 5  # in_progress(+result) + text + completed(+result)


async def test_plan_update_forces_a_boundary() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))
    revision = OrchestrationInfo(category="PlanUpdate")

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("先按旧计划做", "r1"))
    await assembler.feed(
        _tool_use("PlanUpdate", {"changes": []}, call_id="p1", orchestration=revision)
    )
    await assembler.feed(_text("按新计划重来", "r2"))
    await assembler.aclose()

    assert [segment.boundary_reason for segment in collector.done] == [
        "PlanUpdate",
        "session_end",
    ]


async def test_max_span_cuts_a_window_that_never_ends() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([True] * 10), max_span=3)

    await assembler.feed(_event("user", content="分析日活"))
    for index in range(4):
        await assembler.feed(
            _text(f"第{index}步", f"r{index}")
        )
    await assembler.aclose()

    assert collector.done[0].boundary_reason == "max_span"
    assert collector.done[0].forced_complete is True


async def test_each_segment_is_written_twice_and_published_once() -> None:
    assembler, collector = _assembler(judge=FakeJudgeLLM([]))

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("做完了", "r1"))
    await assembler.aclose()

    assert [segment.status for segment in collector.rows] == ["extracting", "done"]
    assert len(collector.done) == 1
    assert collector.done[0].segment_id == collector.rows[0].segment_id


async def test_a_failed_extraction_is_recorded_but_not_published() -> None:
    class FailingLLM:
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            _ = kwargs
            return {"title": "", "input": None, "behavior": "", "conclusion": ""}

    assembler, collector = _assembler(
        judge=FakeJudgeLLM([]),
        extract=FailingLLM(),  # type: ignore[arg-type]
    )

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("做完了", "r1"))
    await assembler.aclose()

    assert [segment.status for segment in collector.rows] == ["extracting", "failed"]
    assert collector.done == []
    assert assembler.stats.failed == 1


async def test_artifacts_survive_only_when_the_workspace_lists_them() -> None:
    registry = FakeRegistry({"report.md": "analysis/report.md"})
    verifier = ArtifactVerifier(
        lambda: [
            {
                "rel_path": "analysis/report.md",
                "size_bytes": 10,
                "mime_type": "text/markdown",
                "modified_at": "2026-08-02T00:00:00+00:00",
            }
        ]
    )
    extract = FakeExtractLLM(
        {
            "title": "产出报告",
            "input": None,
            "behavior": "写出分析报告。",
            "conclusion": "报告已保存。",
            "artifact": [
                {"name": "report.md", "description": "分析报告"},
                {"name": "ghost.csv", "description": "模型臆想的文件"},
            ],
        }
    )
    assembler, collector = _assembler(
        judge=FakeJudgeLLM([]),
        extract=extract,
        artifact_files=registry,
        verifier=verifier,
    )

    await assembler.feed(_event("user", content="出个报告"))
    # The script writes the file, so no tool argument ever names it.
    await assembler.feed(
        _tool_use("Bash", {"command": "python build_report.py"}, call_id="c1")
    )
    await assembler.feed(_tool_result("Bash", "done", call_id="c1"))
    await assembler.aclose()

    artifacts = collector.done[0].artifact
    assert artifacts is not None
    assert [item.name for item in artifacts] == ["report.md"]
    assert artifacts[0].relative_path == "analysis/report.md"
    assert verifier.unverified == 1


async def test_a_segment_that_registered_no_file_has_no_artifact() -> None:
    verifier = ArtifactVerifier(lambda: [])
    extract = FakeExtractLLM(
        {
            "title": "产出报告",
            "input": None,
            "behavior": "尝试写报告。",
            "conclusion": "写入失败。",
            "artifact": [{"name": "report.md", "description": "分析报告"}],
        }
    )
    assembler, collector = _assembler(
        judge=FakeJudgeLLM([]),
        extract=extract,
        artifact_files=FakeRegistry(),
        verifier=verifier,
    )

    await assembler.feed(_event("user", content="出个报告"))
    await assembler.feed(
        _tool_use("Bash", {"command": "python build_report.py"}, call_id="c1")
    )
    await assembler.feed(
        _tool_result("Bash", "denied", call_id="c1", status="error")
    )
    await assembler.aclose()

    assert collector.done[0].artifact is None


async def test_the_prompt_offers_the_files_the_segment_produced() -> None:
    extract = FakeExtractLLM()
    assembler, _ = _assembler(
        judge=FakeJudgeLLM([]),
        extract=extract,
        artifact_files=FakeRegistry({"report.md": "analysis/report.md"}),
        verifier=ArtifactVerifier(lambda: []),
    )

    await assembler.feed(_event("user", content="出个报告"))
    await assembler.feed(
        _tool_use("Bash", {"command": "python build_report.py"}, call_id="c1")
    )
    await assembler.feed(_tool_result("Bash", "done", call_id="c1"))
    await assembler.aclose()

    assert "候选工作区文件" in extract.prompts[0]
    assert "`report.md`" in extract.prompts[0]


async def test_extract_asks_for_coverage_scoped_agent_files() -> None:
    registry = FakeRegistry({"report.md": "analysis/report.md"})
    assembler, _ = _assembler(
        judge=FakeJudgeLLM([]),
        extract=FakeExtractLLM(),
        artifact_files=registry,
        verifier=ArtifactVerifier(lambda: [{"rel_path": "analysis/report.md"}]),
    )

    await assembler.feed(_event("user", content="出个报告"))
    await assembler.feed(
        _tool_use("Bash", {"command": "python build_report.py"}, call_id="c1")
    )
    await assembler.feed(_tool_result("Bash", "done", call_id="c1"))
    await assembler.aclose()

    assert registry.calls >= 1
    assert registry.ranges
    start, end = registry.ranges[0]
    assert start <= end


async def test_a_behavior_returned_as_steps_becomes_markdown() -> None:
    extract = FakeExtractLLM(
        {
            "title": "取数并核对",
            "input": None,
            "behavior": ["拉取明细", "- 对比两周", "核对口径"],
            "conclusion": "口径一致。",
            "artifact": None,
        }
    )
    assembler, collector = _assembler(judge=FakeJudgeLLM([]), extract=extract)

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("做完了", "r1"))
    await assembler.aclose()

    assert collector.done[0].behavior == "1. 拉取明细\n- 对比两周\n3. 核对口径"


async def test_the_segments_own_artifact_is_kept_out_of_its_input() -> None:
    registry = FakeRegistry({"report.md": "analysis/report.md"})
    verifier = ArtifactVerifier(lambda: [{"rel_path": "analysis/report.md"}])
    extract = FakeExtractLLM(
        {
            "title": "产出报告",
            "input": "- 六月明细 `june.csv`\n- 分析报告 `report.md`",
            "behavior": "汇总明细并写出报告。",
            "conclusion": "报告已保存。",
            "artifact": [
                {
                    "name": "report.md",
                    "description": "分析报告",
                    "kind": "report",
                    "role": "final",
                }
            ],
        }
    )
    assembler, collector = _assembler(
        judge=FakeJudgeLLM([]),
        extract=extract,
        artifact_files=registry,
        verifier=verifier,
    )

    await assembler.feed(_event("user", content="出个报告"))
    await assembler.feed(
        _tool_use("Bash", {"command": "python build_report.py"}, call_id="c1")
    )
    await assembler.feed(_tool_result("Bash", "done", call_id="c1"))
    await assembler.aclose()

    segment = collector.done[0]
    assert segment.input == "- 六月明细 `june.csv`"
    assert segment.artifact is not None
    assert (segment.artifact[0].kind, segment.artifact[0].role) == ("report", "final")


async def test_window_candidates_are_stripped_from_input_even_without_artifact() -> (
    None
):
    """In-window produced files are never inputs, even if omitted from artifact."""
    registry = FakeRegistry(
        {"report.md": "analysis/report.md", "scratch.csv": "analysis/scratch.csv"}
    )
    extract = FakeExtractLLM(
        {
            "title": "产出报告",
            "input": "- 六月明细 `june.csv`\n- 中间表 `scratch.csv`\n- 报告 `report.md`",
            "behavior": "汇总明细并写出报告。",
            "conclusion": "报告已保存。",
            "artifact": None,
        }
    )
    assembler, collector = _assembler(
        judge=FakeJudgeLLM([]),
        extract=extract,
        artifact_files=registry,
    )

    await assembler.feed(_event("user", content="出个报告"))
    await assembler.feed(_text("做完了", "r1"))
    await assembler.aclose()

    assert collector.done[0].input == "- 六月明细 `june.csv`"
    assert collector.done[0].artifact is None


async def test_entity_links_reach_the_prose_fields_only() -> None:
    def linker(text: str) -> str:
        return text.replace("日活", "[日活](http://cm/metric?metric_id=m1)")

    extract = FakeExtractLLM(
        {
            "title": "核对日活",
            "input": "日活明细",
            "behavior": "比较两周日活。",
            "conclusion": "日活下降。",
            "artifact": None,
        }
    )
    assembler, collector = _assembler(
        judge=FakeJudgeLLM([]), extract=extract, linker=linker
    )

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("做完了", "r1"))
    await assembler.aclose()

    segment = collector.done[0]
    link = "[日活](http://cm/metric?metric_id=m1)"
    assert segment.input == f"{link}明细"
    assert segment.behavior == f"比较两周{link}。"
    assert segment.conclusion == f"{link}下降。"
    assert segment.title == "核对日活"


async def test_a_broken_linker_leaves_the_fields_alone() -> None:
    def linker(text: str) -> str:
        raise RuntimeError("vocabulary exploded")

    assembler, collector = _assembler(judge=FakeJudgeLLM([]), linker=linker)

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("做完了", "r1"))
    await assembler.aclose()

    assert collector.done[0].behavior == "读取明细并比较两周数据。"


async def test_a_judge_without_a_model_keeps_accumulating() -> None:
    assembler, collector = _assembler(judge=None)

    await assembler.feed(_event("user", content="分析日活"))
    await assembler.feed(_text("一", "r1"))
    await assembler.feed(_text("二", "r2"))
    await assembler.aclose()

    assert assembler.judge.degraded == 1
    assert [segment.boundary_reason for segment in collector.done] == ["session_end"]
    assert len(collector.done[0].coverage.event_ids) == 2

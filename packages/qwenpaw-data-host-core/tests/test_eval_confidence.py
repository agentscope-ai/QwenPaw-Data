# -*- coding: utf-8 -*-
"""The confidence scorer: evidence windows, the verdict round-trip, and every
failure path that must leave a segment unbadged rather than unpublished."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.models import (
    Artifact,
    Coverage,
    FrontendSegment,
    Segment,
    SubtaskScope,
)
from qwenpaw_data.host.core.algo.evaluation.evidence import (
    RECORD_TRUNCATION_MARK,
    build_evidence,
)
from qwenpaw_data.host.core.algo.evaluation.grader import SegmentGrader
from qwenpaw_data.host.core.algo.evaluation.models import ConfidenceResult
from qwenpaw_data.host.core.algo.evaluation.prompt import build_confidence_user_prompt
from qwenpaw_data.host.core.algo.evaluation.scorer import ConfidenceScorer
from qwenpaw_data.host.core.algo.evaluation import scorer as evaluation_scorer
from qwenpaw_data.host.core.algo.evaluation import source as evaluation_source
from qwenpaw_data.host.core.api.models.stream_objects import parse_stream_object
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext, ModelUsageSource

_USAGE = ModelUsageContext(
    source=ModelUsageSource.EVALUATION,
    identity=Identity(user_id="u1", tenant_id="t1", workspace_id="w1"),
    session_id="ses-1",
    chat_id="chat-1",
)


def _window_blocks() -> list[dict[str, Any]]:
    """The in-span chain blocks a delimited window hands the grader."""
    return [
        {
            "type": "tool_use",
            "name": "run_sql",
            "seq": 3,
            "role": "assistant",
            "input": {"sql": "SELECT 1"},
        },
        {
            "type": "tool_result",
            "name": "run_sql",
            "seq": 4,
            "role": "assistant",
            "output": {"rows": 1},
            "status": "done",
        },
    ]


def _segment(**overrides: Any) -> Segment:
    values: dict[str, Any] = dict(
        segment_id="seg-1",
        session_id="ses-1",
        status="done",
        title="查询本月日活",
        input="看下本月日活",
        behavior="执行日活查询",
        conclusion="本月日活 1200 万",
        artifact=[
            Artifact(
                name="daau.sql",
                description="日活查询脚本",
                relative_path="sql/daau.sql",
                kind="query_script",
            )
        ],
        coverage=Coverage(start_seq=3, end_seq=8),
        subtask=SubtaskScope(node_id="n1", node_name="统计日活"),
        boundary_reason="natural",
    )
    values.update(overrides)
    return Segment(**values)


def _evidence(**overrides: Any) -> Any:
    segment = _segment(**overrides)
    return build_evidence(
        _window_blocks(),
        user_inputs=[segment.input],
    )


class FakeModel:
    """Record the structured call, replay payloads (last entry repeats)."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        error: Exception | None = None,
        payloads: list[dict[str, Any]] | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.payloads = payloads
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []

    async def generate_structured_output(
        self, *, messages: list[Any], structured_model: dict[str, Any]
    ) -> Any:
        self.calls.append({"messages": messages, "structured_model": structured_model})
        if self.error is not None:
            raise self.error
        if self.payloads is not None:
            assert self.payloads
            entry = self.payloads[min(self._cursor, len(self.payloads) - 1)]
            self._cursor += 1
            return SimpleNamespace(content=entry)
        assert self.payload is not None
        return SimpleNamespace(content=self.payload)


def _scorer(monkeypatch: Any, model: Any) -> ConfidenceScorer:
    monkeypatch.setattr(evaluation_scorer, "_build_model", lambda config, usage: model)
    return ConfidenceScorer(SimpleNamespace(), usage=_USAGE)


_VERDICT = {
    "level": "high",
    "summary": "口径已确认，数值与执行记录一致。",
}


def test_evidence_records_users_and_the_window() -> None:
    record = _evidence()

    assert "【用户需求】" in record
    assert "用户：看下本月日活" in record
    assert "[3] tool_use run_sql" in record
    assert "[4] tool_result run_sql" in record
    assert "[1] 用户" not in record
    assert "好的" not in record
    assert "口径不对，重算" not in record
    assert "SELECT 2" not in record


def test_a_wide_execute_read_result_keeps_its_totals_and_file_path() -> None:
    """The preview rows come first in CM's JSON; a head-only cut used to eat the
    totals the reply is graded against."""
    rows = [{"user_id": f"u{i}", "note": "x" * 400} for i in range(20)]
    output = json.dumps(
        {
            "columns": ["user_id", "note"],
            "rows": rows,
            "row_count": 20,
            "total_row_count": 4750,
            "truncated": True,
            "elapsed_ms": 12,
            "file_path": "/ws/artifacts/data/raw/abc123.csv",
        }
    )
    record = build_evidence(
        [
            {
                "type": "tool_use",
                "name": "mcp__context-manager__execute_read",
                "seq": 3,
                "role": "assistant",
                "input": {"datasource_id": "ds_demo", "sql": "SELECT 1"},
            },
            {
                "type": "tool_result",
                "name": "mcp__context-manager__execute_read",
                "seq": 4,
                "role": "assistant",
                "output": output,
                "status": "done",
            },
        ]
    )

    assert "datasource_id=ds_demo sql=SELECT 1" in record
    result_line = next(line for line in record.splitlines() if "[4]" in line)
    assert "total_row_count=4750 row_count=20 truncated=true" in result_line
    assert "file_path=/ws/artifacts/data/raw/abc123.csv" in result_line
    assert result_line.index("total_row_count") < result_line.index("rows(preview)")


def test_a_query_graph_call_renders_its_cypher_and_params() -> None:
    record = build_evidence(
        [
            {
                "type": "tool_use",
                "name": "mcp__context-manager__query_graph",
                "seq": 3,
                "role": "assistant",
                "input": {
                    "cypher": "MATCH (m:Metric) WHERE m.name = $name RETURN m.name AS metric",
                    "params": {"name": "DAU"},
                },
            },
            {
                "type": "tool_result",
                "name": "mcp__context-manager__query_graph",
                "seq": 4,
                "role": "assistant",
                "output": json.dumps(
                    {
                        "columns": ["metric"],
                        "rows": [{"metric": "DAU"}],
                        "row_count": 1,
                        "truncated": False,
                    }
                ),
                "status": "done",
            },
        ]
    )

    assert "cypher=MATCH (m:Metric)" in record
    assert 'params={"name":"DAU"}' in record
    assert "row_count=1 truncated=false" in record


def test_unparsable_or_failed_cm_results_fall_back_to_plain_text() -> None:
    record = build_evidence(
        [
            {
                "type": "tool_result",
                "name": "mcp__context-manager__execute_read",
                "seq": 4,
                "role": "assistant",
                "output": "Error executing tool execute_read: datasource_id is required",
                "status": "error",
            }
        ]
    )

    assert (
        "tool_result mcp__context-manager__execute_read: "
        "Error executing tool execute_read: datasource_id is required [error]"
    ) in record


def test_evidence_keeps_one_line_per_event() -> None:
    """Multi-line replies must fold, or the `- [seq]` record format breaks."""
    record = build_evidence(
        [
            {"type": "thinking", "seq": 1, "role": "assistant", "text": "取数\n核对"},
            {"type": "text", "seq": 2, "role": "assistant", "text": "结论：\n- 上升"},
        ],
        priors=[(1, "上一段：\n口径已确认")],
    )

    assert "- [1] 思考：取数 核对" in record
    assert "- [2] 回复：结论： - 上升" in record
    assert "(段1) 上一段： 口径已确认" in record
    assert len([line for line in record.splitlines() if line[:3] == "- ["]) == 2


def test_record_truncation_keeps_head_and_tail() -> None:
    blocks: list[dict[str, Any]] = []
    for seq in range(1, 61):
        blocks.append(
            {
                "type": "tool_use",
                "name": "run_sql",
                "seq": seq,
                "role": "assistant",
                "input": {"sql": f"SELECT marker_head_{seq} FROM t{seq}"},
            }
        )
        blocks.append(
            {
                "type": "tool_result",
                "name": "run_sql",
                "seq": seq,
                "role": "assistant",
                "output": {"rows": f"marker_tail_{seq}:" + "x" * 1_500},
                "status": "done",
            }
        )

    record = build_evidence(blocks)

    assert RECORD_TRUNCATION_MARK in record
    assert "SELECT marker_head_1" in record
    assert "marker_tail_60" in record
    assert "SELECT marker_head_30" not in record
    assert len(record) < 62_000


def test_record_lists_prior_conclusions_before_the_window() -> None:
    record = build_evidence(
        _window_blocks(),
        user_inputs=["看下本月日活"],
        priors=[
            (1, "已完成诊断解析，生成包含 <b>10</b> 项任务的修复计划"),
            (2, "已确认全量执行，切分点锁定为 0710"),
        ],
    )

    assert "【前段结论】" in record
    assert "(段1) 已完成诊断解析，生成包含 10 项任务的修复计划" in record
    assert "(段2) 已确认全量执行，切分点锁定为 0710" in record
    assert "<b>" not in record
    assert (
        record.index("【用户需求】")
        < record.index("【前段结论】")
        < record.index("【本段记录】")
    )


def test_record_pairs_the_user_asks_with_the_plan_context() -> None:
    record = build_evidence(
        _window_blocks(),
        plan=[
            {
                "id": "t1",
                "subject": "数据查询",
                "description": "拉取 8 月日活与留存明细",
                "state": "completed",
            },
            {
                "id": "t2",
                "subject": "数据分析",
                "description": "按渠道拆分留存并对比上月",
                "state": "in_progress",
            },
            {
                "id": "t3",
                "subject": "报告产出",
                "description": "汇总结论并生成报告",
                "state": "pending",
            },
        ],
        subtask="t2",
        completed=["t1"],
        user_inputs=["帮我分析 8 月留存"],
    )

    assert "【用户需求】" in record
    assert "【计划上下文】" in record
    assert "用户：帮我分析 8 月留存" in record
    assert "- 已完成：数据查询" in record
    assert "- 正在执行：数据分析 — 按渠道拆分留存并对比上月" in record
    assert "拉取 8 月日活与留存明细" not in record
    assert "报告产出" not in record
    assert (
        record.index("【用户需求】")
        < record.index("【计划上下文】")
        < record.index("[3] tool_use run_sql")
    )


def test_done_tasks_replay_the_ledger_not_the_snapshot() -> None:
    record = build_evidence(
        _window_blocks(),
        plan=[
            {
                "id": "t1",
                "subject": "数据查询",
                "description": "拉取明细",
                "state": "completed",
            },
            {
                "id": "t2",
                "subject": "名单拆分",
                "description": "拆两张名单",
                "state": "completed",
            },
        ],
        completed=["t1"],
    )

    assert "【计划上下文】" in record
    assert "- 已完成：数据查询" in record
    assert "名单拆分" not in record
    assert "正在执行" not in record


def test_no_plan_requirement_renders_the_input_chain() -> None:
    record = build_evidence(
        _window_blocks(),
        user_inputs=["帮我看看上周各渠道的留存为什么掉了", "重点看 iOS 端", "继续"],
    )

    assert "【用户需求】" in record
    assert "用户：帮我看看上周各渠道的留存为什么掉了" in record
    assert "用户：重点看 iOS 端" in record
    assert "用户：继续" in record
    assert record.index("帮我看看") < record.index("重点看") < record.index("继续")


def test_an_empty_task_line_leaves_only_the_user_asks() -> None:
    record = build_evidence(
        _window_blocks(),
        plan=[
            {
                "id": "t1",
                "subject": "界定窗口",
                "description": "界定冲高与回落窗口",
                "state": "pending",
            },
        ],
        subtask=None,
        completed=[],
        user_inputs=["需要看一下8月ARR冲高以后，回落比较明显的客户和对应的渠道和销售"],
    )

    assert "【用户需求】" in record
    assert (
        "用户：需要看一下8月ARR冲高以后，回落比较明显的客户和对应的渠道和销售" in record
    )
    assert "【计划上下文】" not in record
    assert "已完成：" not in record
    assert "正在执行" not in record


class _RecordingScorer:
    def __init__(self) -> None:
        self.evidence: Any = None

    async def score(self, evidence: Any) -> None:
        self.evidence = evidence


def _grader(user_input: str) -> tuple[SegmentGrader, _RecordingScorer]:
    grader = SegmentGrader(None, usage=_USAGE)

    async def fake_inputs() -> list[str]:
        return [user_input]

    grader._inputs = fake_inputs  # type: ignore[method-assign]
    scorer = _RecordingScorer()
    grader._scorer = scorer  # type: ignore[assignment]
    return grader, scorer


async def test_grader_bridges_ready_materials() -> None:
    grader, scorer = _grader("查一下留存")
    await grader.grade(
        blocks=_window_blocks(),
        priors=[(1, "前段已完成")],
    )

    record = scorer.evidence
    assert "[3] tool_use run_sql" in record
    assert "[4] tool_result run_sql" in record
    assert "【前段结论】" in record
    assert "(段1) 前段已完成" in record
    assert "用户：查一下留存" in record
    assert "查 8 月留存" not in record
    assert "下一段的插话" not in record


async def test_grader_leads_with_the_typed_user_input() -> None:
    grader, scorer = _grader("帮我分析 8 月留存")
    await grader.grade(blocks=_window_blocks())

    record = scorer.evidence
    assert "【用户需求】" in record
    assert "用户：帮我分析 8 月留存" in record
    assert record.index("【用户需求】") < record.index("[3] tool_use run_sql")


async def test_grader_uses_handoff_plan_and_priors() -> None:
    grader, scorer = _grader("本轮输入")
    await grader.grade(
        blocks=[
            {
                "type": "tool_use",
                "name": "run_sql",
                "seq": 13,
                "role": "assistant",
                "input": {"sql": "SELECT 1"},
            }
        ],
        plan=[
            {
                "id": "t1",
                "subject": "数据查询",
                "description": "拉取明细",
                "state": "completed",
            },
            {
                "id": "t2",
                "subject": "数据分析",
                "description": "按渠道拆分",
                "state": "in_progress",
            },
        ],
        subtask="t2",
        completed=["t1"],
        priors=[(3, "t2 时期段")],
    )

    record = scorer.evidence
    assert "- 已完成：数据查询" in record
    assert "- 正在执行：数据分析 — 按渠道拆分" in record
    assert "[13] tool_use run_sql" in record
    assert "(段3) t2 时期段" in record
    assert "t1 时期段" not in record
    assert "过渡段" not in record


async def test_grader_lists_done_tasks_from_the_handoff() -> None:
    grader, scorer = _grader("本轮输入")
    await grader.grade(
        blocks=[
            {
                "type": "tool_use",
                "name": "run_sql",
                "seq": 5,
                "role": "assistant",
                "input": {"sql": "SELECT 1"},
            }
        ],
        plan=[
            {"id": "t1", "subject": "数据查询", "state": "completed"},
            {"id": "t2", "subject": "名单拆分", "state": "completed"},
        ],
        completed=["t1"],
    )

    record = scorer.evidence
    assert "- 已完成：数据查询" in record
    assert "名单拆分" not in record
    assert "正在执行" not in record
    assert "[5] tool_use run_sql" in record


def _score_kwargs() -> dict[str, Any]:
    return {
        "blocks": _window_blocks(),
        "plan": [],
        "subtask": None,
        "completed": [],
        "priors": [],
    }


async def test_scoring_hands_the_grader_the_blocks() -> None:
    from qwenpaw_data.host.core.algo.biztrace.scoring import SegmentScoring

    class _RecordingGrader:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def grade(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    grader = _RecordingGrader()
    scoring = SegmentScoring(grader)  # type: ignore[arg-type]

    verdict = await scoring.score(
        _segment(coverage=Coverage(start_seq=3, end_seq=4)),
        **_score_kwargs(),
    )

    assert grader.calls[0]["blocks"] == _window_blocks()
    assert verdict is None


def test_user_prompt_lays_out_the_record() -> None:
    record = _evidence()

    prompt = build_confidence_user_prompt(record)

    assert "会话记录" in prompt
    assert "看下本月日活" in prompt
    assert "SELECT 1" in prompt
    assert "产物清单" not in prompt


async def test_score_returns_the_verdict(monkeypatch: Any) -> None:
    model = FakeModel(payload=_VERDICT)
    scorer = _scorer(monkeypatch, model)

    result = await scorer.score(_evidence())

    assert result is not None
    assert result.level == "high"
    assert result.summary == "口径已确认，数值与执行记录一致。"
    assert model.calls[0]["structured_model"] is not None


async def test_score_without_a_model_stays_none() -> None:
    scorer = ConfidenceScorer(None, usage=_USAGE)

    assert await scorer.score(_evidence()) is None


async def test_score_degrades_when_the_call_fails(monkeypatch: Any) -> None:
    scorer = _scorer(monkeypatch, FakeModel(error=TimeoutError("budget spent")))

    result = await scorer.score(_evidence())

    assert result is None
    assert scorer.failures == 1


async def test_score_discards_a_malformed_payload(monkeypatch: Any) -> None:
    payload = {
        "level": "impossible",
        "summary": "理由",
    }
    scorer = _scorer(monkeypatch, FakeModel(payload=payload))

    assert await scorer.score(_evidence()) is None


async def test_score_retries_a_corrupt_payload_once(monkeypatch: Any) -> None:
    corrupt = {
        "level": "<parameter=level>\nlow",
        "summary": "损坏",
    }
    model = FakeModel(payloads=[corrupt, _VERDICT])
    scorer = _scorer(monkeypatch, model)

    result = await scorer.score(_evidence())

    assert result is not None
    assert result.level == "high"
    assert len(model.calls) == 2
    assert scorer.failures == 0


def test_confidence_survives_the_wire_contract() -> None:
    verdict = ConfidenceResult(
        level="medium",
        summary="口径已确认，数值可追溯。",
    )

    projection = FrontendSegment.of(_segment(confidence=verdict))
    assert projection is not None
    payload = {
        "object": "segment",
        "session_id": "ses-1",
        "chat_id": "chat-1",
        "sequence_number": 1,
        "created_at": "2026-11-04T08:00:00+00:00",
        "segment": projection.model_dump(mode="json", exclude_none=True),
    }

    obj = parse_stream_object(payload)

    assert obj.segment.confidence is not None
    assert obj.segment.confidence.level == "medium"
    assert obj.segment.confidence.summary == "口径已确认，数值可追溯。"


async def test_fetch_user_inputs_reads_the_chat_and_its_predecessors(
    monkeypatch: Any,
) -> None:
    chat = SimpleNamespace(
        user_input="继续",
        session_id="ses-1",
        sequence=5,
    )
    session_chats = [
        SimpleNamespace(sequence=1, user_input="最早的问题"),
        SimpleNamespace(sequence=2, user_input="分析留存"),
        SimpleNamespace(sequence=3, user_input=""),
        SimpleNamespace(sequence=4, user_input="重点看 iOS 端"),
        chat,
    ]

    class _FakeChatRepo:
        def __init__(self, db: Any, identity: Any) -> None:
            pass

        async def get(self, chat_id: str) -> Any:
            return chat

        async def list_for_session(self, session_id: str) -> list[Any]:
            return session_chats

    monkeypatch.setattr(evaluation_source, "ChatRepository", _FakeChatRepo)

    class _NullSession:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: Any) -> None:
            return None

    inputs = await evaluation_source.fetch_user_inputs(
        _NullSession, identity=None, chat_id="c9"
    )

    assert inputs == [
        "分析留存",
        "重点看 iOS 端",
        "继续",
    ]


async def test_init_db_backfills_the_confidence_column_on_an_existing_database() -> (
    None
):
    from sqlalchemy import inspect, text

    from qwenpaw_data.host.core.db import engine as engine_module

    await engine_module.reset_engine()
    try:
        await engine_module.init_db()
        db_engine = engine_module.get_engine()
        async with db_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE segments DROP COLUMN confidence_json"))

        await engine_module.init_db()

        async with db_engine.connect() as conn:
            names = await conn.run_sync(
                lambda sync_conn: [
                    column["name"]
                    for column in inspect(sync_conn).get_columns("segments")
                ]
            )
        assert "confidence_json" in names
        await engine_module.init_db()
    finally:
        await engine_module.reset_engine()


def test_importing_biztrace_models_does_not_load_sqlalchemy() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import qwenpaw_data.host.core.algo.biztrace.models; "
            "import sys; "
            "raise SystemExit(0 if 'sqlalchemy' not in sys.modules else 1)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


async def test_flush_budget_publishes_a_done_segment_when_scoring_is_slow(
    monkeypatch: Any,
) -> None:
    """A finished extraction must publish even when the grade overruns close."""
    from qwenpaw_data.host.core.algo.biztrace.models import BizEvent
    from qwenpaw_data.host.core.algo.biztrace.scoring import SegmentScoring
    from qwenpaw_data.host.core.algo.biztrace import segmentation as segmentation_mod
    from qwenpaw_data.host.core.algo.biztrace.segmentation import (
        ContinuityJudge,
        SegmentAssembler,
        SegmentExtractor,
    )

    class FakeExtractLLM:
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "title": "取数并核对",
                "input": "分析日活",
                "behavior": "读取明细并比较两周数据。",
                "conclusion": "周末效应导致下降。",
                "artifact": None,
            }

    class SlowGrader:
        async def grade(self, **kwargs: Any) -> Any:
            await asyncio.sleep(30)
            return None

    monkeypatch.setattr(segmentation_mod, "SCORE_JOIN_BUDGET_SECONDS", 10.0)

    rows: list[Segment] = []
    done: list[Segment] = []

    async def on_row(segment: Segment) -> None:
        rows.append(segment)

    async def on_segment(segment: Segment) -> None:
        done.append(segment)

    seq = iter(range(1, 100))

    def event(kind: str, **fields: Any) -> BizEvent:
        fields.setdefault("event_id", f"e{next(seq)}")
        fields.setdefault("seq", next(seq))
        fields.setdefault("parent_msg_id", "reply-1")
        fields.setdefault("started_at", 1.0)
        fields.setdefault("ended_at", 2.0)
        return BizEvent(kind=kind, **fields)  # type: ignore[arg-type]

    assembler = SegmentAssembler(
        session_id="ses-1",
        judge=ContinuityJudge(llm=None),
        extractor=SegmentExtractor(llm=FakeExtractLLM()),  # type: ignore[arg-type]
        on_row=on_row,
        scoring=SegmentScoring(SlowGrader()),  # type: ignore[arg-type]
        on_segment=on_segment,
    )
    await assembler.feed(event("user", content="分析日活"))
    await assembler.feed(event("assistant_text", content="先取数", parent_msg_id="r1"))
    await assembler.feed(event("assistant_text", content="再核对", parent_msg_id="r2"))
    await assembler.aclose(budget=0.3)

    assert any(row.status == "done" and row.title == "取数并核对" for row in rows)
    assert [(s.segment_id, s.status) for s in done] == [("seg_0001", "done")]
    assert not any(row.status == "failed" for row in rows)


async def test_scoring_tasks_do_not_leak_when_on_segment_is_none() -> None:
    from qwenpaw_data.host.core.algo.biztrace.models import BizEvent
    from qwenpaw_data.host.core.algo.biztrace.scoring import SegmentScoring
    from qwenpaw_data.host.core.algo.biztrace.segmentation import (
        ContinuityJudge,
        SegmentAssembler,
        SegmentExtractor,
    )

    class FakeExtractLLM:
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "title": "取数并核对",
                "input": "分析日活",
                "behavior": "读取明细并比较两周数据。",
                "conclusion": "周末效应导致下降。",
                "artifact": None,
            }

    class InstantGrader:
        async def grade(self, **kwargs: Any) -> Any:
            return None

    scoring = SegmentScoring(InstantGrader())  # type: ignore[arg-type]
    seq = iter(range(1, 100))

    def event(kind: str, **fields: Any) -> BizEvent:
        fields.setdefault("event_id", f"e{next(seq)}")
        fields.setdefault("seq", next(seq))
        fields.setdefault("parent_msg_id", "reply-1")
        fields.setdefault("started_at", 1.0)
        fields.setdefault("ended_at", 2.0)
        return BizEvent(kind=kind, **fields)  # type: ignore[arg-type]

    assembler = SegmentAssembler(
        session_id="ses-1",
        judge=ContinuityJudge(llm=None),
        extractor=SegmentExtractor(llm=FakeExtractLLM()),  # type: ignore[arg-type]
        on_row=lambda segment: asyncio.sleep(0),
        scoring=scoring,
        on_segment=None,
    )
    await assembler.feed(event("user", content="分析日活"))
    await assembler.feed(event("assistant_text", content="先取数", parent_msg_id="r1"))
    await assembler.aclose(budget=2.0)

    assert scoring._tasks == {}


async def test_ambiguous_plan_ownership_reaches_the_grader_as_no_subtask() -> None:
    """Two tasks in progress means no single owner, whatever scope last saw."""
    from qwenpaw_data.host.core.algo.biztrace.models import BizEvent, OrchestrationInfo
    from qwenpaw_data.host.core.algo.biztrace.scoring import SegmentScoring
    from qwenpaw_data.host.core.algo.biztrace.segmentation import (
        ContinuityJudge,
        SegmentAssembler,
        SegmentExtractor,
    )

    graded: list[dict[str, Any]] = []

    class CaptureGrader:
        async def grade(self, **kwargs: Any) -> Any:
            graded.append(kwargs)
            return None

    class FakeExtractLLM:
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "title": "取数",
                "input": "分析日活",
                "behavior": "读取明细。",
                "conclusion": "已完成。",
                "artifact": None,
            }

    counter = iter(range(1, 200))

    def event(kind: str, **fields: Any) -> BizEvent:
        fields.setdefault("event_id", f"e{next(counter)}")
        fields.setdefault("seq", next(counter))
        fields.setdefault("parent_msg_id", "reply-1")
        return BizEvent(kind=kind, **fields)  # type: ignore[arg-type]

    def plan_call(
        category: str, payload: dict[str, Any], **info: Any
    ) -> list[BizEvent]:
        call_id = f"call-{next(counter)}"
        return [
            event(
                "tool_use",
                event_id=call_id,
                block_id=call_id,
                tool_name=category,
                input=payload,
                orchestration=OrchestrationInfo(
                    category=category,  # type: ignore[arg-type]
                    node_id=payload.get("task_id"),
                    **info,
                ),
            ),
            event("tool_result", block_id=call_id, tool_name=category, status="done"),
        ]

    assembler = SegmentAssembler(
        session_id="ses-1",
        judge=ContinuityJudge(llm=None),
        extractor=SegmentExtractor(llm=FakeExtractLLM()),  # type: ignore[arg-type]
        on_row=lambda segment: asyncio.sleep(0),
        scoring=SegmentScoring(CaptureGrader()),  # type: ignore[arg-type]
    )

    tasks = [
        {"id": "t1", "subject": "取数", "state": "pending"},
        {"id": "t2", "subject": "核对", "state": "pending"},
    ]
    stream = [
        event("user", content="分析日活"),
        *plan_call("PlanCreate", {"tasks": tasks}),
        *plan_call(
            "TaskStateUpdate",
            {"task_id": "t1", "state": "in_progress"},
            subtask_state="in_progress",
        ),
        event("assistant_text", content="先取数", parent_msg_id="r1"),
        *plan_call(
            "TaskStateUpdate",
            {"task_id": "t2", "state": "in_progress"},
            subtask_state="in_progress",
        ),
        event("assistant_text", content="再核对", parent_msg_id="r2"),
    ]
    for item in stream:
        await assembler.feed(item)
    await assembler.aclose(budget=5.0)

    assert graded, "the grader never ran"
    last = graded[-1]
    states = {task["id"]: task["state"] for task in last["plan"]}
    assert states == {"t1": "in_progress", "t2": "in_progress"}
    assert last["subtask"] is None

# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from agentscope.event import ExternalExecutionResultEvent, ReplyEndEvent
from agentscope.message import UserMsg

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.agent.middleware import followup as followup_module
from qwenpaw_data.host.core.agent.middleware.followup import FollowUpMiddleware
from qwenpaw_data.host.core.db.engine import (
    get_session_factory,
    init_db,
    reset_engine,
    TEST_TENANT_ID,
    TEST_WORKSPACE_ID,
)
from qwenpaw_data.host.core.db.tables import ChatRow, SessionRow
from qwenpaw_data.host.core.domain.preference import ActiveModel, UserRuntimeConfig
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import clear_settings, get_settings
from qwenpaw_data.host.core.stream.hub import reset_hub


class _FakeEvent:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


def _reply_end() -> ReplyEndEvent:
    return ReplyEndEvent(session_id="ses_1", reply_id="rpl_1")


class _RecordingRecommend:
    """替身 Recommend：只记录 start/append/join，并返回可配置的 questions。"""

    last: _RecordingRecommend | None = None
    questions: list[str] = ["拆到一级类目看贡献？"]
    instances: list[_RecordingRecommend] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, Any]] = []
        type(self).last = self
        type(self).instances.append(self)

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def append(self, entry: Any, *, last: bool = False) -> None:
        self.calls.append(("append", (entry, last)))

    async def join(self) -> list[str]:
        self.calls.append(("join", None))
        return list(self.questions)

    def eof_count(self) -> int:
        return sum(1 for name, args in self.calls if name == "append" and args[1])


class _FakeStream:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity


class _FakeEnvelope:
    def __init__(self, session_factory: Any = None) -> None:
        self.sent: list[list[str]] = []
        self._session_factory = session_factory
        self.stream = _FakeStream(_identity())

    async def send_followup(self, questions: list[str]) -> None:
        self.sent.append(list(questions))


def _make_run_context(
    *,
    session_id: str = "ses_1",
    chat_id: str = "chat_1",
    workspace_dir: str = "/tmp/ws",
) -> RunContext:
    from qwenpaw_data.host.core.paths import UserAgentPaths

    runtime = get_settings().runtime
    model = ActiveModel(
        provider_id="openai",
        model_id="m1",
        api_key="sk-test",
        base_url="https://example.invalid/v1",
        chat_model="test-model",
        name="test-model",
    )
    return RunContext(
        session_id=session_id,
        chat_id=chat_id,
        workspace=object(),
        paths=UserAgentPaths(
            home=Path(workspace_dir),
            tenant_id="t1",
            workspace_id="w1",
            user_id="u1",
            agent_id="a1",
            session_id=session_id,
        ),
        user_runtime_config=UserRuntimeConfig(
            default=model,
            light=None,
            react_max_iters=10,
            llm_retry_enabled=False,
            llm_max_retries=0,
            context_trigger_ratio=runtime.context_trigger_ratio,
            context_reserve_ratio=runtime.context_reserve_ratio,
            tool_result_limit=runtime.tool_result_limit,
            tool_timeout_enabled=runtime.tool_timeout_enabled,
            tool_timeout_seconds=runtime.tool_timeout_seconds,
        ),
        request_context={"datasource_id": "ds_1"},
        identity=Identity(user_id="u1", tenant_id="t1", workspace_id="w1"),
        session_factory=get_session_factory(),
    )


def _identity() -> Identity:
    return Identity(
        user_id="u1",
        tenant_id=TEST_TENANT_ID,
        workspace_id=TEST_WORKSPACE_ID,
    )


async def _seed_followup_db() -> None:
    """同一 session 两轮 chat + 另一 session 一轮，写入可去重的 mock followup。"""
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    identity = _identity()
    async with factory() as db:
        await db.flush()
        db.add(
            SessionRow(
                id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                title="main",
            )
        )
        db.add(
            SessionRow(
                id="ses_other",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                title="other",
            )
        )
        await db.flush()
        db.add(
            ChatRow(
                id="chat_old",
                session_id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                sequence=1,
            )
        )
        db.add(
            ChatRow(
                id="chat_mid",
                session_id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                sequence=2,
            )
        )
        db.add(
            ChatRow(
                id="chat_cur",
                session_id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                sequence=3,
            )
        )
        db.add(
            ChatRow(
                id="chat_other_ses",
                session_id="ses_other",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                sequence=1,
            )
        )
        await db.flush()
        repo = FollowUpRepository(db, identity)
        await repo.upsert(
            chat_id="chat_old",
            questions=["DAU环比和同比表现如何？", "按端类型拆解一下DAU"],
        )
        await repo.upsert(
            chat_id="chat_mid",
            questions=["把本次分析整理成一份报告"],
        )
        await repo.upsert(
            chat_id="chat_cur",
            questions=["当前轮不该被加载"],
        )
        await repo.upsert(
            chat_id="chat_other_ses",
            questions=["别的 session 不该被加载"],
        )
        await db.commit()


def _make_middleware(
    monkeypatch,
) -> tuple[FollowUpMiddleware, _FakeEnvelope]:
    _RecordingRecommend.last = None
    _RecordingRecommend.instances = []
    _RecordingRecommend.questions = ["拆到一级类目看贡献？"]
    monkeypatch.setattr(followup_module, "FollowUpRecommend", _RecordingRecommend)
    env = _FakeEnvelope()

    async def _empty_previous(self: FollowUpMiddleware) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(FollowUpMiddleware, "_load_previous_followups", _empty_previous)
    mw = FollowUpMiddleware(
        run_context=_make_run_context(),
        envelope=env,
    )
    return mw, env


def _handler(events: list[Any]):
    async def next_handler() -> AsyncGenerator:
        for ev in events:
            yield ev

    return next_handler


async def _drive(
    mw: FollowUpMiddleware,
    events: list[Any],
    *,
    inputs: Any | None = None,
) -> tuple[list[Any], _RecordingRecommend]:
    seen = []
    async for ev in mw.on_reply(
        agent=None,
        input_kwargs={"inputs": inputs or UserMsg(name="user", content="hi")},
        next_handler=_handler(events),
    ):
        seen.append(ev)
    assert _RecordingRecommend.last is not None
    return seen, _RecordingRecommend.last


@pytest.mark.asyncio
async def test_normal_reply_calls_start_appends_eof_join_and_send(monkeypatch) -> None:
    mw, env = _make_middleware(monkeypatch)
    deltas = [
        _FakeEvent({"type": "TEXT_BLOCK_DELTA", "delta": str(i)}) for i in range(3)
    ]
    events = [*deltas, _reply_end()]

    seen, unit = await _drive(mw, events)

    assert seen == events
    assert unit.calls[0] == ("start", None)
    entry, last = unit.calls[1][1]
    assert entry["kind"] == "user_input" and last is False
    for i, (name, (entry, last)) in enumerate(unit.calls[2:5]):
        assert name == "append" and not last
        assert entry == {
            "kind": "agent_event",
            "payload": {"type": "TEXT_BLOCK_DELTA", "delta": str(i)},
        }
    name, (entry, last) = unit.calls[5]
    assert name == "append" and entry["kind"] == "agent_event" and not last
    assert unit.calls[6] == ("append", (None, True))
    assert unit.calls[7] == ("join", None)
    assert len(unit.calls) == 8
    assert env.sent == [["拆到一级类目看贡献？"]]
    assert unit.init_kwargs["deliver"] == env.send_followup


@pytest.mark.asyncio
async def test_host_followup_settings_are_passed_to_recommend(
    monkeypatch,
) -> None:
    """配置归 Host：算法侧只收参数，不自行读环境变量。"""
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_TIMEOUT_SEC", "1.5")
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_MAX_QUESTIONS", "2")
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_MAX_METRICS", "4")
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_MAX_DIMENSIONS", "5")
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_MIN_RELEVANCE", "0.4")
    clear_settings()
    try:
        mw, _env = _make_middleware(monkeypatch)
        _, unit = await _drive(mw, [_reply_end()])
    finally:
        clear_settings()

    assert "enabled" not in unit.init_kwargs
    assert unit.init_kwargs["timeout_sec"] == 1.5
    assert unit.init_kwargs["max_questions"] == 2
    assert unit.init_kwargs["max_metrics"] == 4
    assert unit.init_kwargs["max_dimensions"] == 5
    assert unit.init_kwargs["min_relevance"] == 0.4


@pytest.mark.asyncio
async def test_disabled_followup_never_starts_recommend(monkeypatch) -> None:
    """总开关关闭时 Host 不创建 Recommend，事件仍原样透传。"""
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_ENABLED", "false")
    clear_settings()
    try:
        mw, env = _make_middleware(monkeypatch)
        events = [
            _FakeEvent({"type": "TEXT_BLOCK_DELTA", "delta": "hi"}),
            _reply_end(),
        ]
        seen = []
        async for ev in mw.on_reply(
            agent=None,
            input_kwargs={"inputs": UserMsg(name="user", content="hi")},
            next_handler=_handler(events),
        ):
            seen.append(ev)
    finally:
        clear_settings()

    assert seen == events
    assert _RecordingRecommend.last is None
    assert _RecordingRecommend.instances == []
    assert env.sent == []
    assert mw._unit is None


@pytest.mark.asyncio
async def test_empty_questions_skips_send(monkeypatch) -> None:
    mw, env = _make_middleware(monkeypatch)
    _RecordingRecommend.questions = []

    await _drive(mw, [_reply_end()])

    assert env.sent == []
    assert _RecordingRecommend.last is not None
    assert ("join", None) in _RecordingRecommend.last.calls


@pytest.mark.asyncio
async def test_pause_then_resume_sends_exactly_one_eof(monkeypatch) -> None:
    """ask_user_question 暂停被 aclose 不发 EOF，恢复流结束才收口。"""
    mw, env = _make_middleware(monkeypatch)

    gen = mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="hi")},
        next_handler=_handler(
            [_FakeEvent({"type": "TOOL_CALL_START", "tool": "ask_user_question"})]
        ),
    )
    async for _ in gen:
        break  # 模拟 executor 收到 RequireExternalExecutionEvent 后中断
    await gen.aclose()

    unit = _RecordingRecommend.last
    assert unit is not None
    assert unit.eof_count() == 0
    assert ("join", None) not in unit.calls
    assert env.sent == []

    resume_events = [
        _FakeEvent({"type": "TOOL_RESULT_END", "status": "answered"}),
        _reply_end(),
    ]
    seen, unit2 = await _drive(
        mw,
        resume_events,
        inputs=ExternalExecutionResultEvent(reply_id="rpl_1", execution_results=[]),
    )

    assert seen == resume_events
    assert unit2 is unit
    assert len(_RecordingRecommend.instances) == 1
    assert unit.eof_count() == 1
    assert unit.calls[-2] == ("append", (None, True))
    assert unit.calls[-1] == ("join", None)
    assert env.sent == [["拆到一级类目看贡献？"]]


@pytest.mark.asyncio
async def test_non_msg_inputs_are_not_fed_as_user_input(monkeypatch) -> None:
    mw, env = _make_middleware(monkeypatch)

    _, unit = await _drive(
        mw,
        [_reply_end()],
        inputs=ExternalExecutionResultEvent(reply_id="rpl_1", execution_results=[]),
    )

    kinds = [
        args[0]["kind"]
        for name, args in unit.calls
        if name == "append" and args[0] is not None
    ]
    assert "user_input" not in kinds
    assert env.sent == [["拆到一级类目看贡献？"]]


@pytest.mark.asyncio
async def test_stream_error_leaves_the_end_marker_to_the_executor(monkeypatch) -> None:
    """异常中断的流不在 on_reply 发结束标记，由 AgentExecutor.stop 补发，且不等 join。"""
    mw, env = _make_middleware(monkeypatch)

    async def broken_handler() -> AsyncGenerator:
        yield _FakeEvent({"type": "TEXT_BLOCK_START"})
        raise RuntimeError("agent exploded")

    with pytest.raises(RuntimeError, match="agent exploded"):
        async for _ in mw.on_reply(
            agent=None,
            input_kwargs={"inputs": UserMsg(name="user", content="hi")},
            next_handler=lambda: broken_handler(),
        ):
            pass

    unit = _RecordingRecommend.last
    assert unit is not None
    assert unit.eof_count() == 0
    assert ("join", None) not in unit.calls
    assert env.sent == []

    await mw.end_stream()

    assert unit.calls[-1] == ("append", (None, True))
    assert ("join", None) not in unit.calls
    assert env.sent == []


@pytest.mark.asyncio
async def test_load_previous_followups_returns_prior_session_questions(
    tmp_path: Path,
) -> None:
    await _seed_followup_db()
    mw = FollowUpMiddleware(
        run_context=_make_run_context(session_id="ses_1", chat_id="chat_cur"),
        envelope=_FakeEnvelope(get_session_factory()),
    )

    got = await mw._load_previous_followups()

    assert got == (
        "DAU环比和同比表现如何？",
        "按端类型拆解一下DAU",
        "把本次分析整理成一份报告",
    )
    assert "当前轮不该被加载" not in got
    assert "别的 session 不该被加载" not in got


@pytest.mark.asyncio
async def test_load_previous_followups_empty_when_no_history(tmp_path: Path) -> None:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    async with factory() as db:
        await db.flush()
        db.add(
            SessionRow(
                id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                title="t",
            )
        )
        await db.flush()
        db.add(
            ChatRow(
                id="chat_cur",
                session_id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                sequence=1,
            )
        )
        await db.commit()

    mw = FollowUpMiddleware(
        run_context=_make_run_context(session_id="ses_1", chat_id="chat_cur"),
        envelope=_FakeEnvelope(factory),
    )

    assert await mw._load_previous_followups() == ()


@pytest.mark.asyncio
async def test_load_previous_followups_returns_empty_on_db_error() -> None:
    mw = FollowUpMiddleware(
        run_context=_make_run_context(session_id="ses_1", chat_id="chat_cur"),
        envelope=_FakeEnvelope(session_factory=None),
    )

    assert await mw._load_previous_followups() == ()


@pytest.mark.asyncio
async def test_on_reply_passes_loaded_previous_followups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_followup_db()
    _RecordingRecommend.last = None
    _RecordingRecommend.instances = []
    monkeypatch.setattr(followup_module, "FollowUpRecommend", _RecordingRecommend)

    mw = FollowUpMiddleware(
        run_context=_make_run_context(session_id="ses_1", chat_id="chat_cur"),
        envelope=_FakeEnvelope(get_session_factory()),
    )
    await _drive(mw, [_reply_end()])

    unit = _RecordingRecommend.last
    assert unit is not None
    assert unit.init_kwargs["previous_followups"] == (
        "DAU环比和同比表现如何？",
        "按端类型拆解一下DAU",
        "把本次分析整理成一份报告",
    )

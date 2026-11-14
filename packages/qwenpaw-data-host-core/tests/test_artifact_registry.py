# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from agentscope.event import (
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from agentscope.message import ToolResultState, UserMsg

from qwenpaw_data.host.core.agent.middleware.trace_forward import TraceForwardMiddleware
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.preference import ActiveModel, UserRuntimeConfig
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import (
    ArtifactRepository,
    ChatRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.repositories.snapshot import SnapshotRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.stream.output_stream import OutputStream

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def _paths(home: Path, *, session_id: str = "ses_1") -> UserAgentPaths:
    return UserAgentPaths.from_identity(
        home=home,
        identity=TEST_IDENTITY,
        agent_id="default",
        session_id=session_id,
    )


class RecordingStream:
    """Collects the OutputStream calls made against it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.session_id = "ses_1"
        self.chat_id = "chat_1"
        self.identity = TEST_IDENTITY

    def __getattr__(self, name: str):
        async def record(*args: Any, **kwargs: Any) -> Any:
            payload = args[0] if args else kwargs
            self.calls.append((name, payload))
            if name == "artifact_registered":
                return MagicMock(artifact=MagicMock(**payload))
            return None

        return record


class RecordingTransformer:
    def __init__(self, **kwargs: Any) -> None:
        self.chat_id = kwargs["run_context"].chat_id
        self.entries: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def append(self, entry: Any, *, last: bool = False) -> None:
        if entry is not None:
            self.entries.append(entry)

    async def join(self) -> None:
        return None


def _run_context(paths: UserAgentPaths) -> RunContext:
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
        session_id="ses_1",
        chat_id="chat_1",
        workspace=object(),
        paths=paths,
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
        identity=TEST_IDENTITY,
        session_factory=get_session_factory(),
    )


def _bridge(
    paths: UserAgentPaths,
    stream: RecordingStream,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TraceForwardMiddleware, RecordingTransformer]:
    from qwenpaw_data.host.core.agent.middleware import trace_forward as tf

    monkeypatch.setattr(tf, "BizTraceTransformer", RecordingTransformer)
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]
    # Envelope.stream is replaced so register hits the recording stub.
    envelope.stream = stream  # type: ignore[assignment]
    mw = TraceForwardMiddleware(
        run_context=_run_context(paths),
        envelope=envelope,
    )
    return mw, mw._transformer


async def _drive_tool_success(
    mw: TraceForwardMiddleware,
    *,
    state: ToolResultState = ToolResultState.SUCCESS,
) -> None:
    events = [
        ToolCallStartEvent(
            reply_id="reply-1", tool_call_id="call-1", tool_call_name="Bash"
        ),
        ToolCallDeltaEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            delta='{"command":"python analyze.py"}',
        ),
        ToolCallEndEvent(reply_id="reply-1", tool_call_id="call-1"),
        ToolResultStartEvent(
            reply_id="reply-1", tool_call_id="call-1", tool_call_name="Bash"
        ),
        ToolResultEndEvent(reply_id="reply-1", tool_call_id="call-1", state=state),
    ]

    async def next_handler():
        for event in events:
            yield event

    async for _ in mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="hi")},
        next_handler=next_handler,
    ):
        pass


async def test_bridge_registers_files_no_tool_call_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    artifact_dir = paths.artifact_dir
    (artifact_dir / "charts").mkdir(parents=True)
    stream = RecordingStream()
    spans: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "qwenpaw_data.host.core.agent.middleware.trace_forward.tracer_span",
        lambda step, **kwargs: (
            spans.append(kwargs.get("metadata") or {}) or nullcontext()
        ),
    )
    mw, tr = _bridge(paths, stream, monkeypatch)

    target = artifact_dir / "charts" / "dau.csv"
    target.write_text("day,dau\n2026-07-30,42\n", encoding="utf-8")
    await _drive_tool_success(mw)

    registered = [
        payload for name, payload in stream.calls if name == "artifact_registered"
    ]
    assert len(registered) == 1
    assert registered[0]["name"] == "dau.csv"
    assert registered[0]["path"] == "charts/dau.csv"
    assert [span["artifact_path"] for span in spans] == [
        str((artifact_dir / "charts" / "dau.csv").resolve())
    ]
    deltas = [e for e in tr.entries if e.get("kind") == "artifact_delta"]
    assert deltas == [
        {"kind": "artifact_delta", "payload": {"files": {"dau.csv": "charts/dau.csv"}}}
    ]


async def test_bridge_leaves_preexisting_files_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    artifact_dir = paths.artifact_dir
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "previous.md").write_text("old\n", encoding="utf-8")
    stream = RecordingStream()
    mw, _ = _bridge(paths, stream, monkeypatch)
    await _drive_tool_success(mw)
    registered = [
        payload for name, payload in stream.calls if name == "artifact_registered"
    ]
    assert registered == []


async def test_bridge_registers_rewritten_file_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    artifact_dir = paths.artifact_dir
    artifact_dir.mkdir(parents=True)
    stream = RecordingStream()
    mw, _ = _bridge(paths, stream, monkeypatch)
    target = artifact_dir / "report.md"

    target.write_text("# draft\n", encoding="utf-8")
    await _drive_tool_success(mw)
    target.write_text("# final version\n", encoding="utf-8")
    await _drive_tool_success(mw)

    registered = [
        payload for name, payload in stream.calls if name == "artifact_registered"
    ]
    assert [fields["path"] for fields in registered] == ["report.md", "report.md"]


@pytest.mark.parametrize(
    ("state", "registers"),
    [
        (ToolResultState.SUCCESS, True),
        (ToolResultState.ERROR, False),
        (ToolResultState.INTERRUPTED, False),
        (ToolResultState.DENIED, False),
        (ToolResultState.RUNNING, False),
    ],
)
async def test_only_successful_tool_call_registers_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: ToolResultState,
    registers: bool,
) -> None:
    paths = _paths(tmp_path)
    artifact_dir = paths.artifact_dir
    artifact_dir.mkdir(parents=True)
    stream = RecordingStream()
    mw, _ = _bridge(paths, stream, monkeypatch)
    (artifact_dir / "dau.csv").write_text("day,dau\n", encoding="utf-8")
    await _drive_tool_success(mw, state=state)
    registered = [
        payload for name, payload in stream.calls if name == "artifact_registered"
    ]
    assert bool(registered) is registers


async def test_rewriting_a_path_updates_the_same_row_per_chat(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as db:
            session = Session.create(identity=TEST_IDENTITY, title="artifacts")
            session_repo = SessionRepository(db, TEST_IDENTITY)
            await session_repo.add(session)
            chat_repo = ChatRepository(db, TEST_IDENTITY)
            chats = []
            for text in ("第一轮", "第二轮"):
                chat = session.open_chat(
                    text=text, datasource_id="ds_demo", has_active_chat=False
                )
                chat.mark_status("completed")
                await session_repo.save(session)
                await chat_repo.add(chat)
                chats.append(chat)

            repo = ArtifactRepository(db, TEST_IDENTITY)

            def fields(chat_id: str, name: str = "dau.csv") -> dict:
                return {
                    "session_id": session.id,
                    "chat_id": chat_id,
                    "name": name,
                    "path": "analysis/dau.csv",
                }

            first = await repo.register(**fields(chats[0].id))
            second = await repo.register(**fields(chats[0].id, name="dau-v2.csv"))
            other_chat = await repo.register(**fields(chats[1].id))
            await db.commit()

            assert second["id"] == first["id"]
            assert second["name"] == "dau-v2.csv"
            assert other_chat["id"] != first["id"]

            listed = await repo.list_for_chats([chats[0].id, chats[1].id])
            assert len(listed[chats[0].id]) == 1
            assert len(listed[chats[1].id]) == 1
    finally:
        await reset_engine()


async def test_segment_does_not_flag_business_visible(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        async with factory() as db:
            session = Session.create(identity=TEST_IDENTITY, title="artifacts")
            session_repo = SessionRepository(db, TEST_IDENTITY)
            await session_repo.add(session)
            chat = session.open_chat(
                text="分析 DAU", datasource_id="ds_demo", has_active_chat=False
            )
            await session_repo.save(session)
            await ChatRepository(db, TEST_IDENTITY).add(chat)
            await db.commit()
        stream = OutputStream(
            factory,
            session_id=session.id,
            chat_id=chat.id,
            identity=TEST_IDENTITY,
            last_sequence_number=chat.last_sequence_number,
        )
        await stream.artifact_registered(
            name="dau.csv",
            path="analysis/dau.csv",
        )
        await stream.segment(
            segment_id="seg_0001",
            title="定位变化点",
            coverage={"start_seq": 1, "end_seq": 1},
            artifact=[
                {
                    "name": "dau.csv",
                    "description": "变化点结果",
                    "relative_path": "analysis/dau.csv",
                },
            ],
        )
        async with factory() as db:
            bundle = await SnapshotRepository(db, TEST_IDENTITY).bundle_chat(chat)
            assert len(bundle.artifacts) == 1
            assert bundle.artifacts[0]["path"] == "analysis/dau.csv"
            assert "business_visible" not in bundle.artifacts[0]
            assert len(bundle.segments) == 1
            assert bundle.segments[0]["artifact"][0]["relative_path"] == (
                "analysis/dau.csv"
            )
    finally:
        await reset_engine()

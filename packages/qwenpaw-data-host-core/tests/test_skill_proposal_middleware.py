# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator

from agentscope.event import ReplyEndEvent
from agentscope.message import UserMsg

from qwenpaw_data.host.core.agent.middleware.settlement import (
    SkillEvolutionDetectionMiddleware,
)
from qwenpaw_data.host.core.algo.skill_evolution.models import EvolutionProposal
from qwenpaw_data.host.core.algo.skill_evolution.manager import _signal_from_chat
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import SessionRepository, SkillProposalRepository
from qwenpaw_data.host.core.repositories.skill_revision import SkillRevisionRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.algo.skill_evolution import SkillEvolutionManager
from qwenpaw_data.host.core.utils.skill import write_skill_files


class _FakeDb:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    def __call__(self) -> Any:
        factory = self

        class _Ctx:
            async def __aenter__(self) -> _FakeDb:
                return factory._db

            async def __aexit__(self, *args: Any) -> None:
                return None

        return _Ctx()


class _FakeStream:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity


class _FakeEnvelope:
    def __init__(self, session_factory: Any, identity: Identity) -> None:
        self._session_factory = session_factory
        self.stream = _FakeStream(identity)


def _identity() -> Identity:
    return Identity(
        user_id=TEST_USER_ID,
        tenant_id=TEST_TENANT_ID,
        workspace_id=TEST_WORKSPACE_ID,
    )


def _run_context(tmp_path: Path | None = None) -> RunContext:
    home = tmp_path or Path(".")
    identity = _identity()
    return RunContext(
        session_id="ses_1",
        chat_id="chat_1",
        workspace=SimpleNamespace(),
        paths=UserAgentPaths(
            home=home,
            tenant_id=identity.tenant_id,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            agent_id="default",
            session_id="ses_1",
        ),
        user_runtime_config=SimpleNamespace(default=SimpleNamespace()),
        identity=identity,
        session_factory=_FakeSessionFactory(_FakeDb()),
    )


def _reply_end() -> ReplyEndEvent:
    return ReplyEndEvent(session_id="ses_1", reply_id="rpl_1")


def _handler(events: list[Any]):
    async def next_handler() -> AsyncGenerator:
        for ev in events:
            yield ev

    return next_handler


def _make_middleware() -> SkillEvolutionDetectionMiddleware:
    identity = _identity()
    return SkillEvolutionDetectionMiddleware(
        run_context=_run_context(),
        envelope=_FakeEnvelope(_FakeSessionFactory(_FakeDb()), identity),
    )


async def test_run_calls_after_reply(monkeypatch) -> None:
    db = _FakeDb()
    identity = _identity()
    factory = _FakeSessionFactory(db)
    envelope = _FakeEnvelope(factory, identity)
    ctx = _run_context()
    mw = SkillEvolutionDetectionMiddleware(run_context=ctx, envelope=envelope)

    calls: list[tuple[str, Any]] = []

    async def _after_reply(
        received_factory: Any,
        *,
        identity: Identity,
        paths: Any,
        chat_id: str,
        session_id: str,
    ) -> None:
        calls.append((received_factory, identity, paths, chat_id, session_id))

    monkeypatch.setattr(SkillEvolutionManager, "after_reply", _after_reply)

    await mw._run()

    assert calls == [(factory, identity, ctx.paths, "chat_1", "ses_1")]


async def test_reply_end_schedules_skill_evolution(monkeypatch) -> None:
    mw = _make_middleware()
    started = asyncio.Event()

    async def _mark() -> None:
        started.set()

    monkeypatch.setattr(mw, "_run", _mark)

    seen: list[Any] = []
    async for ev in mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="hi")},
        next_handler=_handler([_reply_end()]),
    ):
        seen.append(ev)

    assert len(seen) == 1
    await asyncio.wait_for(started.wait(), timeout=1)


def test_signal_counts_chat_user_input_when_events_lack_user() -> None:
    signal, texts = _signal_from_chat(
        [
            {
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "ok"}],
            }
        ],
        ["请按周汇总"],
    )
    assert signal.user_feedback_count == 1
    assert texts == ["请按周汇总"]

    dup, dup_texts = _signal_from_chat(
        [
            {
                "role": "user",
                "type": "message",
                "status": "completed",
                "content": [{"type": "text", "text": "请按周汇总"}],
            }
        ],
        ["请按周汇总"],
    )
    assert dup.user_feedback_count == 1
    assert dup_texts == ["请按周汇总"]

    empty, empty_texts = _signal_from_chat([], ["", "  "])
    assert empty.user_feedback_count == 0
    assert empty_texts == []


async def test_manager_propose_writes_one_row(tmp_path: Path, monkeypatch) -> None:
    await reset_engine()
    await init_db()
    identity = _identity()
    proposal = EvolutionProposal(
        summary="补步骤",
        skills=["sql-analyst"],
        patch_count=1,
        patches=[
            {
                "patch_id": "skpatch_1",
                "skill_name": "sql-analyst",
                "relative_path": "SKILL.md",
                "before": "a",
                "after": "b",
                "diff": "",
                "reason": "",
                "hunks": [{"hunk_id": "skpatch_1:e0", "header": "@@"}],
                "status": "pending",
            }
        ],
    )

    async def _fake_propose(**_kwargs: Any) -> EvolutionProposal:
        return proposal

    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.skill_evolution.manager.propose_evolution",
        _fake_propose,
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.providers.factory.build_model",
        lambda _cfg, **_kwargs: SimpleNamespace(),
    )

    class _Prefs:
        def resolve(self) -> Any:
            return SimpleNamespace(default=SimpleNamespace())

    class _PrefRepo:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def load(self) -> _Prefs:
            return _Prefs()

    monkeypatch.setattr(
        "qwenpaw_data.host.core.repositories.PreferencesRepository",
        _PrefRepo,
    )

    async with get_session_factory()() as db:
        session = Session.create(
            identity=identity, agent_id="default", title="skill-mw"
        )
        await SessionRepository(db, identity).add(session)
        await db.commit()
        paths = UserAgentPaths.from_identity(
            home=tmp_path,
            identity=identity,
            agent_id=session.agent_id,
            session_id=session.id,
        )
        session_id = session.id

    async def _window(self, *, session_id: str, chat_id: str):
        _ = self, session_id, chat_id
        return [], []

    monkeypatch.setattr(SkillEvolutionManager, "_window_messages", _window)
    await SkillEvolutionManager.after_reply(
        get_session_factory(),
        identity=identity,
        paths=paths,
        chat_id="chat_1",
        session_id=session_id,
    )

    async with get_session_factory()() as db:
        rows = await SkillProposalRepository(db, identity).list_by_session(
            session_id, status="pending"
        )
        loaded = (
            await SkillProposalRepository(db, identity).get(
                rows[0]["id"], session_id=session_id
            )
        )["file_changes"]

    assert len(rows) == 1
    assert rows[0]["summary"] == "补步骤"
    assert "file_changes" not in rows[0]
    assert loaded == proposal.patches


_SQL_ANALYST_MD = "---\nname: sql-analyst\ndescription: d\n---\na"


def _sample_patch(
    summary_suffix: str = "1",
    *,
    relative_path: str = "SKILL.md",
) -> list[dict[str, Any]]:
    return [
        {
            "patch_id": f"skpatch_{summary_suffix}",
            "skill_name": "sql-analyst",
            "relative_path": relative_path,
            "before": _SQL_ANALYST_MD if relative_path == "SKILL.md" else "a",
            "after": f"b{summary_suffix}",
            "diff": "",
            "reason": "补步骤",
            "hunks": [
                {
                    "hunk_id": f"skpatch_{summary_suffix}:e0",
                    "header": "@@",
                    "reason": "补步骤",
                    "status": "pending",
                }
            ],
            "status": "pending",
        }
    ]


def _user_msg(text: str, *, chat_id: str) -> dict[str, Any]:
    return {
        "role": "user",
        "type": "message",
        "status": "completed",
        "content": [{"type": "text", "text": text}],
        "_chat_id": chat_id,
        "_chat_user_input": text,
    }


async def test_manager_signal_uses_chat_user_input(tmp_path: Path, monkeypatch) -> None:
    await reset_engine()
    await init_db()
    identity = _identity()
    captured: list[dict[str, Any]] = []
    _stub_runtime(monkeypatch, captured)

    async with get_session_factory()() as db:
        session = Session.create(
            identity=identity, agent_id="default", title="skill-signal"
        )
        await SessionRepository(db, identity).add(session)
        await db.commit()
        paths = UserAgentPaths.from_identity(
            home=tmp_path,
            identity=identity,
            agent_id=session.agent_id,
            session_id=session.id,
        )
        session_id = session.id

    async def _this(self, *, session_id: str, chat_id: str):
        _ = self, session_id, chat_id
        return (
            [
                {
                    "role": "assistant",
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "text", "text": "好的"}],
                }
            ],
            ["请按周汇总"],
        )

    async def _window(self, *, session_id: str, chat_id: str):
        _ = self, session_id, chat_id
        return [], ["请按周汇总"]

    monkeypatch.setattr(SkillEvolutionManager, "_this_chat_messages", _this)
    monkeypatch.setattr(SkillEvolutionManager, "_window_messages", _window)
    await SkillEvolutionManager.after_reply(
        get_session_factory(),
        identity=identity,
        paths=paths,
        chat_id="chat_1",
        session_id=session_id,
    )

    assert captured[0]["signal"].user_feedback_count == 1
    assert captured[0]["feedback_texts"] == ["请按周汇总"]


def _stub_runtime(monkeypatch, captured: list[dict[str, Any]]) -> None:
    async def _fake_propose(**kwargs: Any) -> EvolutionProposal:
        captured.append(kwargs)
        n = str(len(captured))
        path = "SKILL.md" if n == "1" else "examples/query.md"
        return EvolutionProposal(
            summary=f"rev-{n}",
            skills=["sql-analyst"],
            patch_count=1,
            patches=_sample_patch(n, relative_path=path),
        )

    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.skill_evolution.manager.propose_evolution",
        _fake_propose,
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.providers.factory.build_model",
        lambda _cfg, **_kwargs: SimpleNamespace(),
    )

    class _Prefs:
        def resolve(self) -> Any:
            return SimpleNamespace(default=SimpleNamespace())

    class _PrefRepo:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def load(self) -> _Prefs:
            return _Prefs()

    monkeypatch.setattr(
        "qwenpaw_data.host.core.repositories.PreferencesRepository",
        _PrefRepo,
    )


async def test_manager_first_window_then_pending_upsert_then_accept_increment(
    tmp_path: Path, monkeypatch
) -> None:
    await reset_engine()
    await init_db()
    identity = _identity()
    captured: list[dict[str, Any]] = []
    _stub_runtime(monkeypatch, captured)

    this_first = [_user_msg("this-first", chat_id="chat_1")]
    window_msgs = [
        _user_msg("window-old", chat_id="chat_0"),
        _user_msg("this-first", chat_id="chat_1"),
    ]
    later_msgs = [_user_msg("later-only", chat_id="chat_2")]
    after_accept_msgs = [_user_msg("after-accept", chat_id="chat_3")]
    this_by_chat = {
        "chat_1": (this_first, ["this-first"]),
        "chat_2": (later_msgs, ["later-only"]),
        "chat_3": (after_accept_msgs, ["after-accept"]),
    }

    async with get_session_factory()() as db:
        session = Session.create(
            identity=identity, agent_id="default", title="skill-inc"
        )
        await SessionRepository(db, identity).add(session)
        await db.commit()
        paths = UserAgentPaths.from_identity(
            home=tmp_path,
            identity=identity,
            agent_id=session.agent_id,
            session_id=session.id,
        )
        session_id = session.id
        write_skill_files(
            paths.skills / "sql-analyst",
            {"SKILL.md": _SQL_ANALYST_MD, "examples/query.md": "a"},
        )

    async def _window(self, *, session_id: str, chat_id: str):
        _ = self, session_id, chat_id
        return window_msgs, ["window-old", "this-first"]

    async def _this(self, *, session_id: str, chat_id: str):
        _ = self, session_id
        return this_by_chat[chat_id]

    monkeypatch.setattr(SkillEvolutionManager, "_window_messages", _window)
    monkeypatch.setattr(SkillEvolutionManager, "_this_chat_messages", _this)

    async def _after(chat_id: str) -> None:
        await SkillEvolutionManager.after_reply(
            get_session_factory(),
            identity=identity,
            paths=paths,
            chat_id=chat_id,
            session_id=session_id,
        )

    await _after("chat_1")
    first = captured[0]
    assert first["incremental"] is False
    assert first["prior_pending"] is None
    assert first["messages"] == window_msgs
    assert first["feedback_texts"] == ["this-first"]

    async with get_session_factory()() as db:
        repo = SkillProposalRepository(db, identity)
        rows = await repo.list_by_session(session_id, status="pending")
        assert len(rows) == 1
        first_id = rows[0]["id"]
        first_detail = await repo.get(first_id, session_id=session_id)

    await _after("chat_2")
    later = captured[1]
    assert later["incremental"] is True
    assert later["prior_pending"] is not None
    assert later["prior_pending"].patches == first_detail["file_changes"]
    assert later["messages"] == later_msgs
    assert later["chat_user_inputs"] == ["later-only"]
    assert later["feedback_texts"] == ["later-only"]

    async with get_session_factory()() as db:
        repo = SkillProposalRepository(db, identity)
        rows = await repo.list_by_session(session_id, status="pending")
        assert len(rows) == 1
        assert rows[0]["id"] == first_id
        later_detail = await repo.get(first_id, session_id=session_id)
        assert later_detail["summary"] == "rev-1；rev-2"
        file_paths = {item["relative_path"] for item in later_detail["file_changes"]}
        assert file_paths == {"SKILL.md", "examples/query.md"}
        skill_md = next(
            item
            for item in later_detail["file_changes"]
            if item["relative_path"] == "SKILL.md"
        )
        assert skill_md["after"] == "b1"
        assert skill_md["patch_id"] == first_detail["file_changes"][0]["patch_id"]

        mgr = SkillEvolutionManager(db, identity=identity, paths=paths)
        await mgr.accept(first_id, session_id=session_id)
        accepted = await repo.get(first_id, session_id=session_id)
        assert accepted["status"] in ("accepted", "partial")
        history = await SkillRevisionRepository(
            db, identity, agent_id=paths.agent_id
        ).list_history("sql-analyst")
        assert any(
            item["origin"] == "evolution_accept" and item["source_ref"] == first_id
            for item in history
        )
        await db.commit()
    assert (paths.skills / "sql-analyst" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "b1"
    assert (paths.skills / "sql-analyst" / "examples" / "query.md").read_text(
        encoding="utf-8"
    ) == "b2"

    await _after("chat_3")
    after = captured[2]
    assert after["incremental"] is True
    assert after["prior_pending"] is None
    assert after["messages"] == after_accept_msgs
    assert after["feedback_texts"] == ["after-accept"]

    async with get_session_factory()() as db:
        pending = await SkillProposalRepository(db, identity).list_by_session(
            session_id, status="pending"
        )
        assert len(pending) == 1
        assert pending[0]["id"] != first_id

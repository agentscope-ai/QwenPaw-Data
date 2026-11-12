from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.api.models.requests import ConsoleChatRequest
from qwenpaw_data.host.core.api.routers import console
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
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.stream.hub import reset_hub

_IDENTITY = Identity(user_id="u", tenant_id="t", workspace_id="w")
_TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def test_open_chat_copies_session_enable_clarification() -> None:
    session = Session.create(identity=_IDENTITY, enable_clarification=False)
    chat = session.open_chat(text="work", datasource_id=None, has_active_chat=False)
    assert chat.enable_clarification is False
    assert session.enable_clarification is False


def test_open_chat_override_leaves_session_flag() -> None:
    session = Session.create(identity=_IDENTITY)
    chat = session.open_chat(
        text="cron",
        datasource_id=None,
        has_active_chat=False,
        enable_clarification=False,
    )
    assert chat.enable_clarification is False
    assert session.enable_clarification is True


def test_fork_copies_session_enable_clarification() -> None:
    session = Session.create(identity=_IDENTITY, enable_clarification=False)
    chat = session.open_chat(text="work", datasource_id=None, has_active_chat=False)
    chat.mark_status("completed")
    chat.agent_state = {"messages": []}
    forked = session.fork(at_chat=chat, has_active_chat=False)
    assert forked.enable_clarification is False


def _patch_console_chat(monkeypatch, tmp_path, *, session, chat):
    session_repo = SimpleNamespace(
        has_active_chat=AsyncMock(return_value=False),
        save=AsyncMock(),
    )
    monkeypatch.setattr(console, "access_session", AsyncMock(return_value=session))
    monkeypatch.setattr(console, "SessionRepository", lambda *args: session_repo)
    monkeypatch.setattr(
        console, "ChatRepository", lambda *args: SimpleNamespace(add=AsyncMock())
    )
    monkeypatch.setattr(
        console,
        "PreferencesRepository",
        lambda *args: SimpleNamespace(
            load=AsyncMock(return_value=SimpleNamespace(resolve=Mock())),
        ),
    )
    monkeypatch.setattr(
        console,
        "AttachmentRepository",
        lambda *args: SimpleNamespace(
            require_for_session=AsyncMock(return_value=[]),
        ),
    )
    monkeypatch.setattr(
        console, "_session_paths", lambda *args: SimpleNamespace(workspace=tmp_path)
    )
    monkeypatch.setattr(console, "chat_to_schema", lambda item: {"id": item.id})
    monkeypatch.setattr(console, "get_session_factory", lambda: None)
    return session_repo


async def test_console_chat_reads_session_enable_clarification(
    monkeypatch, tmp_path
) -> None:
    body = ConsoleChatRequest.model_validate({"session_id": "session", "text": "work"})
    chat = SimpleNamespace(id="chat")
    session = SimpleNamespace(id="session", open_chat=Mock(return_value=chat))
    _patch_console_chat(monkeypatch, tmp_path, session=session, chat=chat)
    monkeypatch.setattr(console, "generate_temporary_token", lambda identity: None)
    background = BackgroundTasks()
    result = await console.console_chat(
        body,
        SimpleNamespace(headers={}),
        background,
        identity=_IDENTITY,
        db=AsyncMock(),
    )
    assert result == {"chat": {"id": "chat"}}
    assert session.open_chat.call_args.kwargs.get("enable_clarification") is None


async def test_console_chat_mints_temporary_token(monkeypatch, tmp_path):
    body = ConsoleChatRequest.model_validate({"session_id": "session", "text": "work"})
    chat = SimpleNamespace(id="chat")
    session = SimpleNamespace(id="session", open_chat=Mock(return_value=chat))
    session_repo = _patch_console_chat(
        monkeypatch, tmp_path, session=session, chat=chat
    )
    order: list[str] = []

    async def _commit() -> None:
        order.append("commit")

    def _mint(identity: Identity) -> str:
        order.append("mint")
        return "tok-minted"

    monkeypatch.setattr(console, "generate_temporary_token", _mint)
    background = BackgroundTasks()
    db = AsyncMock()
    db.commit = AsyncMock(side_effect=_commit)
    result = await console.console_chat(
        body,
        SimpleNamespace(headers={}),
        background,
        identity=_IDENTITY,
        db=db,
    )
    assert result == {"chat": {"id": "chat"}}
    assert order == ["commit", "mint"]
    assert background.tasks[0].kwargs["access_token"] == "tok-minted"
    session_repo.save.assert_awaited()


async def test_console_chat_mint_failure_does_not_start_runtime(monkeypatch, tmp_path):
    body = ConsoleChatRequest.model_validate({"session_id": "session", "text": "work"})
    chat = SimpleNamespace(id="chat")
    session = SimpleNamespace(id="session", open_chat=Mock(return_value=chat))
    _patch_console_chat(monkeypatch, tmp_path, session=session, chat=chat)

    def _mint(identity: Identity) -> str:
        raise RuntimeError("auth temporary-access request failed")

    monkeypatch.setattr(console, "generate_temporary_token", _mint)
    background = BackgroundTasks()
    db = AsyncMock()
    with pytest.raises(RuntimeError, match="temporary-access"):
        await console.console_chat(
            body,
            SimpleNamespace(headers={}),
            background,
            identity=_IDENTITY,
            db=db,
        )
    assert background.tasks == []
    db.commit.assert_awaited()


async def test_patch_session_updates_running_chat_enable_clarification() -> None:
    await reset_engine()
    reset_hub()
    await init_db()
    async with get_session_factory()() as db:
        session = Session.create(identity=_TEST_IDENTITY, title="coffee")
        await SessionRepository(db, _TEST_IDENTITY).add(session)
        chat = session.open_chat(
            text="work",
            datasource_id=None,
            has_active_chat=False,
        )
        await SessionRepository(db, _TEST_IDENTITY).save(session)
        await ChatRepository(db, _TEST_IDENTITY).add(chat)
        await db.commit()
        session_id, chat_id = session.id, chat.id
    app = create_app()
    with TestClient(app) as client:
        patched = client.patch(
            f"/api/sessions/{session_id}",
            json={"enable_clarification": False},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["session"]["enable_clarification"] is False
    async with get_session_factory()() as db:
        stored_session = await SessionRepository(db, _TEST_IDENTITY).get(session_id)
        stored_chat = await ChatRepository(db, _TEST_IDENTITY).get(chat_id)
        assert stored_session.enable_clarification is False
        assert stored_chat.enable_clarification is False
        assert stored_chat.status == "running"

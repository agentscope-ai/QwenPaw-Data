# -*- coding: utf-8 -*-
"""Attachments: domain rules, store conformance, console routes, turn input."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.turn import TurnInput, compose_agent_input
from qwenpaw_data.host.core.store.json_store import (
    JSONAttachmentStore,
    JSONChatStore,
)

sqlalchemy = pytest.importorskip("sqlalchemy")

from qwenpaw_data.host.core.db.engine import (  # noqa: E402
    create_engine_and_factory,
    init_db,
)
from qwenpaw_data.host.core.store.sql_store import (  # noqa: E402
    SQLAttachmentStore,
    SQLChatStore,
)


# ---------------------------------------------------------------------------
# Domain


def _receive(tmp_path: Path, filename: str = "data.csv", data: bytes = b"a,b\n"):
    return Attachment.receive(
        session_id="ses1",
        identity=Identity.anonymous(),
        filename=filename,
        data=data,
        dest_dir=tmp_path / "uploads" / "ses1",
    )


def test_receive_writes_file_and_ref(tmp_path: Path) -> None:
    attachment = _receive(tmp_path)
    assert attachment.storage_path == "uploads/ses1/data.csv"
    assert (tmp_path / attachment.storage_path).read_bytes() == b"a,b\n"
    ref = attachment.to_ref()
    assert ref == {"attachment_id": attachment.id, "filename": "data.csv"}
    assert attachment.require_file(tmp_path).name == "data.csv"


def test_receive_rejects_empty_and_duplicates(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        _receive(tmp_path, data=b"")
    _receive(tmp_path)
    with pytest.raises(ValueError, match="duplicate filename"):
        _receive(tmp_path)


def test_receive_rejects_unsafe_names(tmp_path: Path) -> None:
    for bad in ("..", "a/b.csv", "a\\b.csv", " padded.csv", ""):
        with pytest.raises(ValueError):
            _receive(tmp_path, filename=bad)


def test_require_file_rejects_escape(tmp_path: Path) -> None:
    attachment = _receive(tmp_path)
    attachment.storage_path = "../outside.csv"
    with pytest.raises(ValueError, match="escapes workspace"):
        attachment.require_file(tmp_path)


def test_receive_rejects_oversize(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "qwenpaw_data.host.core.domain.attachment.MAX_SIZE_BYTES", 4
    )
    with pytest.raises(ValueError, match="exceeds"):
        _receive(tmp_path, data=b"12345")


# ---------------------------------------------------------------------------
# Turn composition


def test_compose_plain_text_is_unchanged() -> None:
    assert compose_agent_input("hi", None, None) == "hi"


def test_compose_includes_attachments_and_comments() -> None:
    text = compose_agent_input(
        "分析一下",
        [{"path": "out/a.md", "line_start": 1, "line_end": 2, "comment": "改这里"}],
        [{"attachment_id": "att_1", "filename": "data.csv"}],
        session_id="ses1",
    )
    assert "uploads/ses1/data.csv" in text
    assert "out/a.md" in text and "改这里" in text
    assert text.endswith("分析一下")


def test_compose_requires_session_id_for_attachments() -> None:
    with pytest.raises(ValueError, match="session_id"):
        compose_agent_input(
            "hi", None, [{"attachment_id": "a", "filename": "f"}]
        )


def test_compose_rejects_incomplete_items() -> None:
    with pytest.raises(ValueError, match="attachment missing"):
        compose_agent_input("hi", None, [{"filename": "f"}], session_id="s")
    with pytest.raises(ValueError, match="artifact comment missing"):
        compose_agent_input("hi", [{"path": "p"}], None)


def test_turn_input_carries_chat_fields() -> None:
    chat = Chat.start(
        session_id="ses1",
        identity=Identity.anonymous(),
        sequence=1,
        datasource_id=None,
        text="",
        artifact_comments=[
            {"path": "p", "line_start": 1, "line_end": 1, "comment": "c"}
        ],
        attachments=[{"attachment_id": "att_1", "filename": "f.csv"}],
    )
    turn = TurnInput.from_chat(chat)
    assert turn.session_id == "ses1"
    assert turn.attachments == [{"attachment_id": "att_1", "filename": "f.csv"}]
    assert turn.artifact_comments[0]["comment"] == "c"
    assert chat.references_attachment("att_1")
    assert not chat.references_attachment("att_2")


def test_chat_start_requires_text_or_attachment() -> None:
    with pytest.raises(ValueError, match="text is required"):
        Chat.start(
            session_id="s",
            identity=Identity.anonymous(),
            sequence=1,
            datasource_id=None,
            text="  ",
        )


# ---------------------------------------------------------------------------
# Store conformance (both backends)


class Backend:
    def __init__(self, name, attachments, chats) -> None:
        self.name = name
        self.attachments = attachments
        self.chats = chats


@pytest.fixture(params=["json", "sql"])
async def backend(request, tmp_path: Path):
    if request.param == "json":
        yield Backend(
            "json", JSONAttachmentStore(tmp_path), JSONChatStore(tmp_path)
        )
        return
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
    )
    await init_db(engine)
    yield Backend("sql", SQLAttachmentStore(factory), SQLChatStore(factory))
    await engine.dispose()


async def test_store_roundtrip_and_scoping(backend: Backend, tmp_path) -> None:
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)

    loaded = await backend.attachments.get("local", attachment.id)
    assert loaded.filename == "data.csv"
    assert loaded.storage_path == attachment.storage_path
    assert loaded.session_id == "ses1"

    with pytest.raises(LookupError):
        await backend.attachments.get("other-user", attachment.id)
    with pytest.raises(RuntimeError, match="CONFLICT"):
        await backend.attachments.add(attachment)


async def test_store_find_by_filename(backend: Backend, tmp_path) -> None:
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)

    found = await backend.attachments.find_by_filename("local", "ses1", "data.csv")
    assert found is not None and found.id == attachment.id
    assert (
        await backend.attachments.find_by_filename("local", "ses2", "data.csv")
        is None
    )
    assert (
        await backend.attachments.find_by_filename("local", "ses1", "other.csv")
        is None
    )
    assert (
        await backend.attachments.find_by_filename("nobody", "ses1", "data.csv")
        is None
    )


async def test_store_require_for_session(backend: Backend, tmp_path) -> None:
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)

    items = await backend.attachments.require_for_session(
        "local", "ses1", [attachment.id], workspace=tmp_path
    )
    assert [item.id for item in items] == [attachment.id]

    with pytest.raises(ValueError, match="duplicate attachment_id"):
        await backend.attachments.require_for_session(
            "local", "ses1", [attachment.id, attachment.id], workspace=tmp_path
        )
    with pytest.raises(LookupError):
        await backend.attachments.require_for_session(
            "local", "ses2", [attachment.id], workspace=tmp_path
        )
    (tmp_path / attachment.storage_path).unlink()
    with pytest.raises(LookupError, match="file not found"):
        await backend.attachments.require_for_session(
            "local", "ses1", [attachment.id], workspace=tmp_path
        )


async def test_store_delete(backend: Backend, tmp_path) -> None:
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)
    with pytest.raises(LookupError):
        await backend.attachments.delete("other", attachment.id)
    await backend.attachments.delete("local", attachment.id)
    with pytest.raises(LookupError):
        await backend.attachments.get("local", attachment.id)


async def test_chat_persists_attachment_fields(backend: Backend) -> None:
    chat = Chat.start(
        session_id="ses1",
        identity=Identity.anonymous(),
        sequence=1,
        datasource_id=None,
        text="hi",
        artifact_comments=[
            {"path": "p", "line_start": 1, "line_end": 2, "comment": "c"}
        ],
        attachments=[{"attachment_id": "att_1", "filename": "f.csv"}],
    )
    await backend.chats.add(chat)
    loaded = await backend.chats.get(chat.id)
    assert loaded.attachments == [{"attachment_id": "att_1", "filename": "f.csv"}]
    assert loaded.artifact_comments[0]["line_end"] == 2

    loaded.mark_status("completed")
    await backend.chats.save(loaded)
    again = await backend.chats.get(chat.id)
    assert again.attachments == loaded.attachments


# ---------------------------------------------------------------------------
# Console routes

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402


@asynccontextmanager
async def console_client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            created = await http.post("/api/v1/sessions", json={"title": "附件"})
            assert created.status_code == 200, created.text
            yield http, created.json()["session"]["id"]


async def _upload(http, session_id: str, filename: str = "data.csv"):
    return await http.post(
        "/api/v1/console/upload",
        data={"session_id": session_id},
        files={"file": (filename, b"a,b\n1,2\n", "text/csv")},
    )


async def test_upload_and_duplicate(tmp_path, monkeypatch) -> None:
    async with console_client(tmp_path, monkeypatch) as (http, session_id):
        uploaded = await _upload(http, session_id)
        assert uploaded.status_code == 200, uploaded.text
        ref = uploaded.json()["attachment"]
        assert ref["filename"] == "data.csv"
        assert ref["attachment_id"].startswith("att")

        again = await _upload(http, session_id)
        assert again.status_code in (400, 422), again.text

        missing = await _upload(http, "ses_missing")
        assert missing.status_code == 404


async def test_delete_attachment(tmp_path, monkeypatch) -> None:
    async with console_client(tmp_path, monkeypatch) as (http, session_id):
        ref = (await _upload(http, session_id)).json()["attachment"]
        deleted = await http.delete(
            f"/api/v1/console/attachments/{ref['attachment_id']}"
        )
        assert deleted.status_code == 204
        again = await http.delete(
            f"/api/v1/console/attachments/{ref['attachment_id']}"
        )
        assert again.status_code == 404
        # File removed too; the same name can be uploaded again.
        assert (await _upload(http, session_id)).status_code == 200


async def test_console_chat_carries_attachments(tmp_path, monkeypatch) -> None:
    async with console_client(tmp_path, monkeypatch) as (http, session_id):
        ref = (await _upload(http, session_id)).json()["attachment"]
        response = await http.post(
            "/api/v1/console/chat",
            json={
                "session_id": session_id,
                "text": "看看这个文件",
                "attachment_ids": [ref["attachment_id"]],
                "artifact_comments": [
                    {
                        "path": "out/report.md",
                        "line_start": 3,
                        "line_end": 4,
                        "comment": "补充口径",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        chat = response.json()["chat"]
        assert chat["attachments"] == [ref]
        assert chat["artifact_comments"][0]["comment"] == "补充口径"

        # The attachment is now referenced: deletion must conflict.
        conflicted = await http.delete(
            f"/api/v1/console/attachments/{ref['attachment_id']}"
        )
        assert conflicted.status_code == 409

        stopped = await http.post(
            "/api/v1/console/chat/stop",
            params={"session_id": session_id, "chat_id": chat["id"]},
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["chat"]["status"] in (
            "canceled",
            "failed",
            "completed",
        )


async def test_console_chat_unknown_attachment(tmp_path, monkeypatch) -> None:
    async with console_client(tmp_path, monkeypatch) as (http, session_id):
        response = await http.post(
            "/api/v1/console/chat",
            json={
                "session_id": session_id,
                "text": "hi",
                "attachment_ids": ["att_missing"],
            },
        )
        assert response.status_code == 404

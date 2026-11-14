"""Attachments: current SQL repositories, authenticated HTTP and file safety."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.repositories import AttachmentRepository, ChatRepository
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.runtime.turn import TurnInput, compose_agent_input
from .conftest import TEST_IDENTITY
from . import test_runtime_settings
from .test_service_smoke import ScriptedAgent, service_client, _new_session

database = test_runtime_settings.database


def _receive(tmp_path: Path, filename: str = "data.csv", data: bytes = b"a,b\n"):
    return Attachment.receive(
        session_id="ses1",
        identity=TEST_IDENTITY,
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
    monkeypatch.setattr("qwenpaw_data.host.core.utils.files.MAX_SIZE_BYTES", 4)
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
        compose_agent_input("hi", None, [{"attachment_id": "a", "filename": "f"}])


def test_compose_rejects_incomplete_items() -> None:
    with pytest.raises(ValueError, match="attachment missing"):
        compose_agent_input("hi", None, [{"filename": "f"}], session_id="s")
    with pytest.raises(ValueError, match="artifact comment missing"):
        compose_agent_input("hi", [{"path": "p"}], None)


def test_turn_input_carries_chat_fields() -> None:
    chat = Chat.start(
        session_id="ses1",
        identity=TEST_IDENTITY,
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
            identity=TEST_IDENTITY,
            sequence=1,
            datasource_id=None,
            text="  ",
        )


# ---------------------------------------------------------------------------

# SQL persistence: transactions and identity are owned by the caller.


@pytest.fixture
async def backend(database):
    async with database() as db:
        yield SimpleNamespace(
            db=db,
            factory=database,
            attachments=AttachmentRepository(db, TEST_IDENTITY),
            chats=ChatRepository(db, TEST_IDENTITY),
        )


async def test_store_roundtrip_and_scoping(backend, tmp_path):
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)
    await backend.db.commit()
    async with backend.factory() as db:
        loaded = await AttachmentRepository(db, TEST_IDENTITY).get(attachment.id)
        assert loaded.filename == "data.csv"
        assert loaded.storage_path == attachment.storage_path
        assert loaded.session_id == "ses1"
        for field in ("user_id", "tenant_id", "workspace_id"):
            other = AttachmentRepository(db, replace(TEST_IDENTITY, **{field: "other"}))
            with pytest.raises(LookupError):
                await other.get(attachment.id)
            with pytest.raises(LookupError):
                await other.delete(attachment)
            assert await other.find_by_filename("ses1", "data.csv") is None
    with pytest.raises(IntegrityError):
        async with backend.db.begin_nested():
            await backend.attachments.add(attachment)


async def test_store_find_by_filename(backend, tmp_path):
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)
    found = await backend.attachments.find_by_filename("ses1", "data.csv")
    assert found.id == attachment.id
    assert await backend.attachments.find_by_filename("ses2", "data.csv") is None
    assert await backend.attachments.find_by_filename("ses1", "other.csv") is None


async def test_store_require_for_session(backend, tmp_path):
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)
    items = await backend.attachments.require_for_session(
        "ses1", [attachment.id], workspace=tmp_path
    )
    assert [item.id for item in items] == [attachment.id]
    with pytest.raises(ValueError, match="duplicate attachment_id"):
        await backend.attachments.require_for_session(
            "ses1", [attachment.id, attachment.id], workspace=tmp_path
        )
    with pytest.raises(LookupError):
        await backend.attachments.require_for_session(
            "ses2", [attachment.id], workspace=tmp_path
        )
    (tmp_path / attachment.storage_path).unlink()
    with pytest.raises(LookupError, match="file not found"):
        await backend.attachments.require_for_session(
            "ses1", [attachment.id], workspace=tmp_path
        )


async def test_store_delete(backend, tmp_path):
    attachment = _receive(tmp_path)
    await backend.attachments.add(attachment)
    await backend.attachments.delete(attachment)
    await backend.db.commit()
    async with backend.factory() as db:
        with pytest.raises(LookupError):
            await AttachmentRepository(db, TEST_IDENTITY).get(attachment.id)


async def test_chat_persists_attachment_fields(backend):
    chat = Chat.start(
        session_id="ses1",
        identity=TEST_IDENTITY,
        sequence=1,
        datasource_id=None,
        text="hi",
        artifact_comments=[
            {"path": "p", "line_start": 1, "line_end": 2, "comment": "c"}
        ],
        attachments=[{"attachment_id": "att_1", "filename": "f.csv"}],
    )
    await backend.chats.add(chat)
    await backend.db.commit()
    async with backend.factory() as db:
        repo = ChatRepository(db, TEST_IDENTITY)
        loaded = await repo.get(chat.id)
        assert loaded.attachments == chat.attachments
        assert loaded.artifact_comments == chat.artifact_comments
        loaded.mark_status("completed")
        await repo.save(loaded)
        await db.commit()
    async with backend.factory() as db:
        saved = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
        assert saved.status == "completed"
        assert saved.attachments == chat.attachments


@asynccontextmanager
async def console_client(monkeypatch):
    async with service_client(ScriptedAgent(), monkeypatch) as http:
        yield http, await _new_session(http)


async def _upload(http, session_id: str, filename: str = "data.csv"):
    return await http.post(
        "/api/console/upload",
        data={"session_id": session_id},
        files={"file": (filename, b"a,b\n1,2\n", "text/csv")},
    )


async def test_upload_and_duplicate(tmp_path, monkeypatch) -> None:
    async with console_client(monkeypatch) as (http, session_id):
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
    async with console_client(monkeypatch) as (http, session_id):
        ref = (await _upload(http, session_id)).json()["attachment"]
        deleted = await http.delete(f"/api/console/attachments/{ref['attachment_id']}")
        assert deleted.status_code == 204
        again = await http.delete(f"/api/console/attachments/{ref['attachment_id']}")
        assert again.status_code == 404
        # File removed too; the same name can be uploaded again.
        assert (await _upload(http, session_id)).status_code == 200


async def test_console_chat_carries_attachments(tmp_path, monkeypatch) -> None:
    async with console_client(monkeypatch) as (http, session_id):
        ref = (await _upload(http, session_id)).json()["attachment"]
        response = await http.post(
            "/api/console/chat",
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
            f"/api/console/attachments/{ref['attachment_id']}"
        )
        assert conflicted.status_code == 409

        stopped = await http.post(
            "/api/console/chat/stop",
            params={"session_id": session_id, "chat_id": chat["id"]},
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["chat"]["status"] in (
            "canceled",
            "failed",
            "completed",
        )


async def test_console_chat_unknown_attachment(tmp_path, monkeypatch) -> None:
    async with console_client(monkeypatch) as (http, session_id):
        response = await http.post(
            "/api/console/chat",
            json={
                "session_id": session_id,
                "text": "hi",
                "attachment_ids": ["att_missing"],
            },
        )
        assert response.status_code == 404


@pytest.mark.parametrize(
    "escape", ["parent", "absolute", "directory_symlink", "file_symlink"]
)
def test_delete_rejects_paths_outside_workspace(tmp_path, escape):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "keep.csv"
    victim.write_bytes(b"keep")
    attachment = _receive(root)
    if escape == "parent":
        attachment.storage_path = "../outside/keep.csv"
    elif escape == "absolute":
        attachment.storage_path = str(victim)
    elif escape == "directory_symlink":
        (root / "alias").symlink_to(outside, target_is_directory=True)
        attachment.storage_path = "alias/keep.csv"
    else:
        (root / "alias.csv").symlink_to(victim)
        attachment.storage_path = "alias.csv"
    with pytest.raises(ValueError, match="escapes workspace"):
        attachment.remove_file(root)
    assert victim.read_bytes() == b"keep"


def test_delete_missing_file_is_idempotent(tmp_path):
    attachment = _receive(tmp_path)
    attachment.remove_file(tmp_path)
    attachment.remove_file(tmp_path)
    assert not (tmp_path / attachment.storage_path).exists()


async def test_foreign_session_attachment_is_rejected_before_chat_creation(monkeypatch):
    async with console_client(monkeypatch) as (http, first):
        ref = (await _upload(http, first)).json()["attachment"]
        second = await _new_session(http)
        response = await http.post(
            "/api/console/chat",
            json={
                "session_id": second,
                "text": "hi",
                "attachment_ids": [ref["attachment_id"]],
            },
        )
        assert response.status_code == 404
        async with get_session_factory()() as db:
            assert (
                await ChatRepository(db, TEST_IDENTITY).list_for_session(second) == []
            )
        # Failed admission does not remove or invalidate the source attachment.
        assert (
            await http.delete(f"/api/console/attachments/{ref['attachment_id']}")
        ).status_code == 204


async def test_delete_rejects_symlink_escape_and_preserves_sql_row(
    monkeypatch, tmp_path
):
    async with console_client(monkeypatch) as (http, session_id):
        ref = (await _upload(http, session_id)).json()["attachment"]
        paths = UserAgentPaths.from_identity(
            home=get_settings().paths.home,
            identity=TEST_IDENTITY,
            agent_id="default",
            session_id=session_id,
        )
        # Simulate a workspace process replacing its uploads directory with a link.
        original = paths.uploads_dir.with_name("saved-uploads")
        paths.uploads_dir.rename(original)
        outside = tmp_path / "outside-api"
        outside.mkdir()
        victim = outside / "data.csv"
        victim.write_bytes(b"untouched")
        paths.uploads_dir.symlink_to(outside, target_is_directory=True)
        response = await http.delete(f"/api/console/attachments/{ref['attachment_id']}")
        assert response.status_code == 400, response.text
        assert victim.read_bytes() == b"untouched"
        async with get_session_factory()() as db:
            saved = await AttachmentRepository(db, TEST_IDENTITY).get(
                ref["attachment_id"]
            )
            assert saved.filename == "data.csv"


@pytest.mark.parametrize("linked_parent", [False, True])
async def test_upload_rejects_linked_directory_without_file_or_row(
    monkeypatch, tmp_path, linked_parent
):
    async with console_client(monkeypatch) as (http, session_id):
        paths = UserAgentPaths.from_identity(
            home=get_settings().paths.home,
            identity=TEST_IDENTITY,
            agent_id="default",
            session_id=session_id,
        )
        outside = tmp_path / "outside-upload"
        outside.mkdir()
        linked = paths.uploads_dir.parent if linked_parent else paths.uploads_dir
        linked.parent.mkdir(parents=True, exist_ok=True)
        linked.symlink_to(outside, target_is_directory=True)
        response = await _upload(http, session_id)
        assert response.status_code == 400, response.text
        assert list(outside.iterdir()) == []
        async with get_session_factory()() as db:
            assert (
                await AttachmentRepository(db, TEST_IDENTITY).find_by_filename(
                    session_id, "data.csv"
                )
                is None
            )

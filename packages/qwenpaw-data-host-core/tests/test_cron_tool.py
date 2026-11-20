# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agentscope.message import ToolResultState

from qwenpaw_data.host.core.agent.tools.cron_job import CronJobTool
from qwenpaw_data.host.core.cron.manager import CronManager
from qwenpaw_data.host.core.repositories.channel_binding import ChannelBindingRepository
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.cron.files import JobFiles
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.attachment import AttachmentRepository
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import get_settings

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)

_SCHEDULE = {
    "schedule_type": "cron",
    "cron_expression": "30 9 * * 1-5",
    "timezone": "Asia/Shanghai",
}


def test_job_files_reject_symlinked_job_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'qwenpaw_data.host.core.cron.files.get_settings',
        lambda: SimpleNamespace(paths=SimpleNamespace(home=tmp_path)),
    )
    files = JobFiles(TEST_IDENTITY, 'job-safe')
    root = files.root
    root.parent.mkdir(parents=True)
    outside = tmp_path / 'another-user'
    outside.mkdir()
    (outside / 'keep.txt').write_text('private')
    root.symlink_to(outside, target_is_directory=True)
    for operation in (files.filenames, files.clear, lambda: files.add('x.txt', b'payload')):
        with pytest.raises(ValueError, match='unsafe job attachment directory'):
            operation()
    assert (outside / 'keep.txt').read_text() == 'private'


def test_job_files_do_not_read_or_overwrite_symlinked_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'qwenpaw_data.host.core.cron.files.get_settings',
        lambda: SimpleNamespace(paths=SimpleNamespace(home=tmp_path)),
    )
    files = JobFiles(TEST_IDENTITY, 'job-safe')
    files.root.mkdir(parents=True)
    outside = tmp_path / 'private.txt'
    outside.write_bytes(b'private')
    (files.root / 'link.txt').symlink_to(outside)
    assert files.filenames() == []
    with pytest.raises(ValueError, match='duplicate filename'):
        files.add('link.txt', b'replace')
    assert outside.read_bytes() == b'private'


def _run_context(
    *,
    identity: Identity = TEST_IDENTITY,
    datasource_id: str | None = "ds_demo",
    session_id: str = "session-a",
) -> RunContext:
    request_context: dict = {}
    if datasource_id is not None:
        request_context["datasource_id"] = datasource_id
    return RunContext(
        session_id=session_id,
        chat_id="chat-a",
        workspace=SimpleNamespace(),
        paths=UserAgentPaths(
            home=Path("."),
            tenant_id=identity.tenant_id,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            agent_id="agent-a",
            session_id="session-a",
        ),
        user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
        identity=identity,
        session_factory=get_session_factory(),
        request_context=request_context,
    )


def _text(chunk) -> str:
    return "".join(block.text for block in chunk.content)


@pytest.fixture
async def cron_mgr():
    await reset_engine()
    await init_db()
    mgr = CronManager(get_session_factory(), MagicMock())
    await mgr.start()
    yield mgr
    await mgr.shutdown()


def test_require_before_start_raises() -> None:
    with pytest.raises(RuntimeError, match="has not started"):
        CronManager.require()


@pytest.mark.asyncio
async def test_create_requires_schedule(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    tool = CronJobTool(_run_context())
    chunk = await tool(
        action="create",
        name="日报",
        message="生成本日简报",
    )
    assert chunk.state == ToolResultState.ERROR
    assert "schedule_type is required" in _text(chunk)


@pytest.mark.asyncio
async def test_create_requires_datasource(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    tool = CronJobTool(_run_context(datasource_id=None))
    chunk = await tool(
        action="create",
        name="日报",
        message="生成本日简报",
        **_SCHEDULE,
    )
    assert chunk.state == ToolResultState.ERROR
    assert "datasource_id is required" in _text(chunk)


@pytest.mark.asyncio
async def test_create_list_scoped_to_identity(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    tool = CronJobTool(_run_context())
    created = await tool(
        action="create",
        name="日报",
        message="生成本日简报",
        **_SCHEDULE,
    )
    assert created.state != ToolResultState.ERROR
    job = json.loads(_text(created))["job"]
    assert job["name"] == "日报"
    assert job["datasource_id"] == "ds_demo"
    assert job["channel"] == "console"
    assert job["session_id"] is None
    assert job["target_external_key"] is None
    assert job["id"]

    listed = await tool(action="list")
    payload = json.loads(_text(listed))
    assert payload["count"] == 1
    assert payload["jobs"][0]["id"] == job["id"]

    other = CronJobTool(
        _run_context(
            identity=Identity(
                user_id="other-user",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
            ),
        ),
    )
    other_list = json.loads(_text(await other(action="list")))
    assert other_list["count"] == 0


@pytest.mark.asyncio
async def test_list_targets_returns_public_fields(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    async with get_session_factory()() as db:
        await ChannelBindingRepository(db).point_to(
            TEST_IDENTITY,
            "feishu",
            "oc_group_a",
            "ses_demo",
            target_meta={"target_type": "group", "send_meta": {"chat_id": "secret"}},
            display_name="销售群",
        )
        await db.commit()

    tool = CronJobTool(_run_context())
    chunk = await tool(action="list_targets", channel="feishu")
    assert chunk.state != ToolResultState.ERROR
    payload = json.loads(_text(chunk))
    assert payload["count"] == 1
    target = payload["targets"][0]
    assert target["external_key"] == "oc_group_a"
    assert target["target_type"] == "group"
    assert target["display_name"] == "销售群"
    assert "last_active_at" in target
    assert "send_meta" not in target

    empty = json.loads(_text(await tool(action="list_targets", channel="wecom")))
    assert empty["count"] == 0
    assert empty["targets"] == []


@pytest.mark.asyncio
async def test_run_requires_job_id(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    tool = CronJobTool(_run_context())
    chunk = await tool(action="run")
    assert chunk.state == ToolResultState.ERROR
    assert "job_id is required" in _text(chunk)


@pytest.mark.asyncio
async def test_run_only_marks_pending(cron_mgr: CronManager) -> None:
    tool = CronJobTool(_run_context())
    created = await tool(
        action="create",
        name="日报",
        message="生成本日简报",
        **_SCHEDULE,
    )
    job_id = json.loads(_text(created))["job"]["id"]

    with patch.object(cron_mgr, "run", new=AsyncMock()) as run:
        chunk = await tool(action="run", job_id=job_id)
        assert chunk.state != ToolResultState.ERROR
        body = json.loads(_text(chunk))
        assert body["ok"] is True
        assert body["job"]["id"] == job_id
        await cron_mgr.tick()
        if cron_mgr._runs:
            await asyncio.gather(*cron_mgr._runs, return_exceptions=True)
        run.assert_awaited_once()
        assert run.await_args.args[0]["id"] == job_id


async def _session_with_attachment(filename: str, data: bytes) -> Session:
    session = Session.create(
        identity=TEST_IDENTITY,
        title="source",
        channel="console",
        datasource_id="ds_demo",
    )
    uploads_dir = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=TEST_IDENTITY,
        agent_id=session.agent_id,
        session_id=session.id,
    ).uploads_dir
    attachment = Attachment.receive(
        session_id=session.id,
        identity=TEST_IDENTITY,
        filename=filename,
        data=data,
        dest_dir=uploads_dir,
    )
    async with get_session_factory()() as db:
        await SessionRepository(db, TEST_IDENTITY).add(session)
        await AttachmentRepository(db, TEST_IDENTITY).add(attachment)
        await db.commit()
    return session


@pytest.mark.asyncio
async def test_create_copies_current_session_attachment_by_filename(
    cron_mgr: CronManager,
) -> None:
    _ = cron_mgr
    session = await _session_with_attachment("a.txt", b"hello")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="带附件",
        message="读取附件",
        attachment_filenames=["a.txt"],
        **_SCHEDULE,
    )
    assert created.state != ToolResultState.ERROR, _text(created)
    job = json.loads(_text(created))["job"]
    files = JobFiles(TEST_IDENTITY, job["id"])
    try:
        assert job["attachments"] == ["a.txt"]
        assert (files.root / "a.txt").read_bytes() == b"hello"
    finally:
        files.clear()


async def _session_with_artifact(relative: str, data: bytes) -> tuple[Session, Path]:
    session = Session.create(
        identity=TEST_IDENTITY,
        title="source",
        channel="console",
        datasource_id="ds_demo",
    )
    artifact_dir = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=TEST_IDENTITY,
        agent_id=session.agent_id,
        session_id=session.id,
    ).artifact_dir
    target = artifact_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    async with get_session_factory()() as db:
        await SessionRepository(db, TEST_IDENTITY).add(session)
        await db.commit()
    return session, artifact_dir


@pytest.mark.asyncio
async def test_create_copies_artifact_by_relative_path(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    session, artifact_dir = await _session_with_artifact(
        "report-reproduction-export/rr-bundle.zip", b"bundle"
    )
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="带产物",
        message="读取产物",
        attachment_filenames=[
            f"artifacts/{session.id}/report-reproduction-export/rr-bundle.zip"
        ],
        **_SCHEDULE,
    )
    assert created.state != ToolResultState.ERROR, _text(created)
    job = json.loads(_text(created))["job"]
    files = JobFiles(TEST_IDENTITY, job["id"])
    try:
        assert job["attachments"] == ["rr-bundle.zip"]
        assert (files.root / "rr-bundle.zip").read_bytes() == b"bundle"
    finally:
        files.clear()
        target = artifact_dir / "report-reproduction-export" / "rr-bundle.zip"
        target.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_create_rejects_artifact_path_outside_artifact_dir(
    cron_mgr: CronManager,
) -> None:
    _ = cron_mgr
    session, artifact_dir = await _session_with_artifact("inside.txt", b"inside")
    outside = artifact_dir.parent / "secret.txt"
    outside.write_bytes(b"secret")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="越界",
        message="不应读到",
        attachment_filenames=[f"artifacts/{session.id}/../secret.txt"],
        **_SCHEDULE,
    )
    try:
        assert created.state == ToolResultState.ERROR
        assert "escapes" in _text(created)
        listed = json.loads(_text(await tool(action="list")))
        assert listed["count"] == 0
    finally:
        outside.unlink(missing_ok=True)
        (artifact_dir / "inside.txt").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_create_rejects_missing_artifact(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    session, _artifact_dir = await _session_with_artifact("inside.txt", b"inside")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="缺失",
        message="找不到",
        attachment_filenames=[f"artifacts/{session.id}/missing.zip"],
        **_SCHEDULE,
    )
    assert created.state == ToolResultState.ERROR
    assert "missing.zip" in _text(created)
    listed = json.loads(_text(await tool(action="list")))
    assert listed["count"] == 0


@pytest.mark.asyncio
async def test_replace_rejects_duplicate_artifact_name(cron_mgr: CronManager) -> None:
    _ = cron_mgr
    session, artifact_dir = await _session_with_artifact("export/bundle.zip", b"bundle")
    (artifact_dir / "other").mkdir(parents=True, exist_ok=True)
    (artifact_dir / "other" / "bundle.zip").write_bytes(b"other")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="重复名",
        message="第一次",
        attachment_filenames=[
            f"artifacts/{session.id}/export/bundle.zip",
            f"artifacts/{session.id}/other/bundle.zip",
        ],
        **_SCHEDULE,
    )
    assert created.state == ToolResultState.ERROR
    assert "duplicate filename" in _text(created)
    listed = json.loads(_text(await tool(action="list")))
    assert listed["count"] == 0
    for path in (
        artifact_dir / "export" / "bundle.zip",
        artifact_dir / "other" / "bundle.zip",
    ):
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_create_fails_when_attachment_is_not_in_current_session(
    cron_mgr: CronManager,
) -> None:
    _ = cron_mgr
    session = await _session_with_attachment("a.txt", b"hello")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="带附件",
        message="读取附件",
        attachment_filenames=["missing.txt"],
        **_SCHEDULE,
    )
    assert created.state == ToolResultState.ERROR
    assert "missing.txt" in _text(created)
    listed = json.loads(_text(await tool(action="list")))
    assert listed["count"] == 0


@pytest.mark.asyncio
async def test_create_copies_upload_given_as_workspace_path(
    cron_mgr: CronManager,
) -> None:
    _ = cron_mgr
    session = await _session_with_attachment("a.txt", b"hello")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="带路径",
        message="读取附件",
        attachment_filenames=[f"uploads/{session.id}/a.txt"],
        **_SCHEDULE,
    )
    assert created.state != ToolResultState.ERROR, _text(created)
    job = json.loads(_text(created))["job"]
    files = JobFiles(TEST_IDENTITY, job["id"])
    try:
        assert job["attachments"] == ["a.txt"]
        assert (files.root / "a.txt").read_bytes() == b"hello"
    finally:
        files.clear()


@pytest.mark.asyncio
async def test_create_rejects_upload_from_another_session(
    cron_mgr: CronManager,
) -> None:
    _ = cron_mgr
    session = await _session_with_attachment("a.txt", b"hello")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="跨会话",
        message="不应读到",
        attachment_filenames=["uploads/other-session/a.txt"],
        **_SCHEDULE,
    )
    assert created.state == ToolResultState.ERROR
    assert "outside" in _text(created)
    listed = json.loads(_text(await tool(action="list")))
    assert listed["count"] == 0


@pytest.mark.asyncio
async def test_create_rejects_path_outside_uploads_and_artifacts(
    cron_mgr: CronManager,
) -> None:
    _ = cron_mgr
    session = await _session_with_attachment("a.txt", b"hello")
    tool = CronJobTool(_run_context(session_id=session.id))
    created = await tool(
        action="create",
        name="无关路径",
        message="不应读到",
        attachment_filenames=["notes/file.txt"],
        **_SCHEDULE,
    )
    assert created.state == ToolResultState.ERROR
    assert "outside" in _text(created)
    listed = json.loads(_text(await tool(action="list")))
    assert listed["count"] == 0

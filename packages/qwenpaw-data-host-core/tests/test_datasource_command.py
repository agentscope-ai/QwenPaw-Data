# -*- coding: utf-8 -*-
"""``/datasource`` command test"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qwenpaw_data.host.core.channels.base import BaseChannel, is_control_command
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import SessionRepository

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def test_bind_from_none() -> None:
    session = Session.create(identity=TEST_IDENTITY, title="t")
    assert session.datasource_id is None
    session.bind_datasource("mysql-abc")
    assert session.datasource_id == "mysql-abc"


def test_bind_same_value_is_noop() -> None:
    session = Session.create(identity=TEST_IDENTITY, title="t")
    session.bind_datasource("mysql-abc")
    session.bind_datasource("mysql-abc")
    assert session.datasource_id == "mysql-abc"


def test_bind_datasource_rejects_mismatch() -> None:
    """bind_datasource: rebind not allowed."""
    session = Session.create(identity=TEST_IDENTITY, title="t")
    session.bind_datasource("mysql-abc")
    with pytest.raises(ValueError):
        session.bind_datasource("pg-xyz")


def test_bind_empty_raises() -> None:
    session = Session.create(identity=TEST_IDENTITY, title="t")
    with pytest.raises(ValueError):
        session.bind_datasource("   ")


# ---------------------------------------------------------------------
# is_control_command / _handle_control_command
# ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["/datasource", "/datasource ", "/DATASOURCE"])
def test_is_control_command_recognizes_datasource(text: str) -> None:
    assert is_control_command(text) is True


def test_is_control_command_ignores_lookalike() -> None:
    assert is_control_command("/datasourcex") is False
    assert is_control_command("分析 /datasource") is False


@pytest.mark.asyncio
async def test_handle_control_command_dispatches_datasource(tmp_path: Path) -> None:
    """``/datasource`` matches → dispatches to ``_handle_datasource`` → returns True."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        with patch.object(
            channel,
            "_handle_datasource",
            new=AsyncMock(),  # type: ignore[method-assign]
        ) as m:
            handled = await channel._handle_control_command(
                channel._native, "/datasource"
            )
        assert handled is True
        m.assert_awaited_once()
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# _handle_datasource: selectable vs view-only
# ---------------------------------------------------------------------


async def _seed_session(datasource_id: str | None = None) -> str:
    """Seed a session and bind ``feishu-abcd1234`` → it; return its real id."""
    from qwenpaw_data.host.core.repositories import (
        ChannelBindingRepository,
        SessionRepository,
    )

    async with get_session_factory()() as db:
        repo = SessionRepository(db, TEST_IDENTITY)
        session = Session.create(
            identity=TEST_IDENTITY,
            title="t",
            channel="feishu",
            datasource_id=datasource_id,
        )
        await repo.add(session)
        await ChannelBindingRepository(db).point_to(
            TEST_IDENTITY, "feishu", "feishu-abcd1234", session.id
        )
        await db.commit()
        return session.id


def _fake_ds_list() -> Any:
    fake_ds = MagicMock(
        datasource_id="mysql-1",
        datasource_name="MySQL",
        datasource_type="mysql",
    )
    return MagicMock(items=[fake_ds])


@pytest.mark.asyncio
async def test_handle_datasource_unbound_sends_selectable_card(tmp_path: Path) -> None:
    """Unbound session → ``send_datasource_card(..., selectable=True)``."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        await _seed_session(datasource_id=None)
        with (
            patch(
                "qwenpaw_data.host.core.integrations.cm.client.ContextManagerClient.list_datasources",
                return_value=_fake_ds_list(),
            ),
            patch.object(
                channel,
                "send_datasource_card",
                new=AsyncMock(),  # type: ignore[method-assign]
            ) as send_card,
        ):
            await channel._handle_datasource(channel._native)
        send_card.assert_awaited_once()
        assert send_card.call_args.kwargs["selectable"] is True
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_handle_datasource_bound_sends_viewonly_card(tmp_path: Path) -> None:
    """Bound session → ``send_datasource_card(..., selectable=False)`` (view-only)."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        await _seed_session(datasource_id="mysql-1")
        with (
            patch(
                "qwenpaw_data.host.core.integrations.cm.client.ContextManagerClient.list_datasources",
                return_value=_fake_ds_list(),
            ),
            patch.object(
                channel,
                "send_datasource_card",
                new=AsyncMock(),  # type: ignore[method-assign]
            ) as send_card,
        ):
            await channel._handle_datasource(channel._native)
        send_card.assert_awaited_once()
        assert send_card.call_args.kwargs["selectable"] is False
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_handle_datasource_no_session_asks_first(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        with patch(
            "qwenpaw_data.host.core.integrations.cm.client.ContextManagerClient.list_datasources",
            return_value=_fake_ds_list(),
        ):
            await channel._handle_datasource(channel._native)
        assert channel.sent[-1] == "会话未建立，请先发一条消息"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_handle_datasource_empty_list_sends_text(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        await _seed_session(datasource_id=None)
        with patch(
            "qwenpaw_data.host.core.integrations.cm.client.ContextManagerClient.list_datasources",
            return_value=MagicMock(items=[]),
        ):
            await channel._handle_datasource(channel._native)
        assert channel.sent[-1] == "暂无可用数据源，请先在管控台添加"
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# _consume_one_request: proactive pop when unbound
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_unbound_pops_card_no_chat(tmp_path: Path) -> None:
    """Unbound session + normal message → pop card, no chat, no agent."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel._native = _make_text_native("帮我查销量")
        with (
            patch(
                "qwenpaw_data.host.core.integrations.cm.client.ContextManagerClient.list_datasources",
                return_value=_fake_ds_list(),
            ),
            patch.object(
                channel,
                "send_datasource_card",
                new=AsyncMock(),  # type: ignore[method-assign]
            ) as send_card,
            patch("qwenpaw_data.host.core.channels.base.ChatRuntime") as runtime_cls,
            patch("qwenpaw_data.host.core.channels.base.get_hub"),
        ):
            await channel._consume_one_request(channel._native)
        send_card.assert_awaited_once()
        assert send_card.call_args.kwargs["selectable"] is True
        runtime_cls.assert_not_called()  # agent not run
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_consume_bound_runs_agent(tmp_path: Path) -> None:
    """Bound session + normal message → run agent (no card)."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel._native = _make_text_native("帮我查销量")
        await _seed_session(datasource_id="mysql-1")
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_empty_async_iter())
        with (
            patch(
                "qwenpaw_data.host.core.integrations.cm.client.ContextManagerClient.list_datasources",
                return_value=_fake_ds_list(),
            ),
            patch.object(
                channel,
                "send_datasource_card",
                new=AsyncMock(),  # type: ignore[method-assign]
            ) as send_card,
            patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub),
            patch("qwenpaw_data.host.core.channels.base.ChatRuntime") as runtime_cls,
        ):
            runtime_cls.return_value.run = AsyncMock()
            await channel._consume_one_request(channel._native)
        send_card.assert_not_called()
        runtime_cls.assert_called_once()
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# ChannelBindingRepository: (channel, external_key) → active session pointer
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_binding_points_to_active(tmp_path: Path) -> None:
    """``point_to`` creates the binding; ``get_active_session_id`` reads it back."""
    from qwenpaw_data.host.core.repositories import ChannelBindingRepository

    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as db:
            repo = SessionRepository(db, TEST_IDENTITY)
            binding = ChannelBindingRepository(db)
            session = Session.create(
                identity=TEST_IDENTITY, title="t", channel="feishu"
            )
            session.id = "ses_aaa"
            await repo.add(session)
            await db.commit()

            assert (
                await binding.get_active_session_id(
                    TEST_IDENTITY, "feishu", "feishu-abcd1234"
                )
                is None
            )
            await binding.point_to(
                TEST_IDENTITY, "feishu", "feishu-abcd1234", session.id
            )
            await db.commit()
            assert (
                await binding.get_active_session_id(
                    TEST_IDENTITY, "feishu", "feishu-abcd1234"
                )
                == "ses_aaa"
            )
            # a different conversation is unbound
            assert (
                await binding.get_active_session_id(
                    TEST_IDENTITY, "feishu", "feishu-other"
                )
                is None
            )
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_channel_binding_repoint_switches_active(tmp_path: Path) -> None:
    """``/session`` semantics: repointing moves the active session; old one retained."""
    from qwenpaw_data.host.core.repositories import ChannelBindingRepository

    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as db:
            repo = SessionRepository(db, TEST_IDENTITY)
            binding = ChannelBindingRepository(db)
            old = Session.create(identity=TEST_IDENTITY, title="old", channel="feishu")
            old.id = "ses_old"
            new = Session.create(identity=TEST_IDENTITY, title="new", channel="feishu")
            new.id = "ses_new"
            await repo.add(old)
            await repo.add(new)
            await db.commit()

            await binding.point_to(TEST_IDENTITY, "feishu", "feishu-abcd1234", old.id)
            await binding.point_to(TEST_IDENTITY, "feishu", "feishu-abcd1234", new.id)
            await db.commit()
            assert (
                await binding.get_active_session_id(
                    TEST_IDENTITY, "feishu", "feishu-abcd1234"
                )
                == "ses_new"
            )
            # old session still exists for webui
            assert (await repo.get("ses_old")).id == "ses_old"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_resolve_active_session_skips_stale_pointer(tmp_path: Path) -> None:
    """A pointer at a soft-deleted session is treated as unbound."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        sid = await _seed_session()
        async with get_session_factory()() as db:
            repo = SessionRepository(db, TEST_IDENTITY)
            session = await repo.get(sid)
            session.soft_delete(has_active_chat=False)
            await repo.save(session)
            await db.commit()
        # binding still points at the deleted session → adapter sees None
        async with get_session_factory()() as db:
            assert await channel._resolve_active_session(db, "feishu-abcd1234") is None
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# /session command
# ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["/session", "/session ", "/SESSION"])
def test_is_control_command_recognizes_session(text: str) -> None:
    assert is_control_command(text) is True


def test_is_control_command_ignores_session_lookalike() -> None:
    assert is_control_command("/sessionx") is False
    assert is_control_command("新建 /session") is False


@pytest.mark.asyncio
async def test_handle_session_creates_unbound_and_pops_card(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        with (
            patch(
                "qwenpaw_data.host.core.integrations.cm.client.ContextManagerClient.list_datasources",
                return_value=_fake_ds_list(),
            ),
            patch.object(
                channel,
                "send_datasource_card",
                new=AsyncMock(),  # type: ignore[method-assign]
            ) as send_card,
        ):
            await channel._handle_session(channel._native)
        send_card.assert_awaited_once()
        assert send_card.call_args.kwargs["selectable"] is True
        # one new session row, bound as active for this conversation
        async with get_session_factory()() as db:
            from qwenpaw_data.host.core.repositories import ChannelBindingRepository

            binding = ChannelBindingRepository(db)
            active_id = await binding.get_active_session_id(
                TEST_IDENTITY, "feishu", "feishu-abcd1234"
            )
            assert active_id is not None
            assert active_id.startswith("ses-")
            active = await SessionRepository(db, TEST_IDENTITY).get(active_id)
        assert active.datasource_id is None
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_handle_session_refused_while_chat_running(tmp_path: Path) -> None:
    """Active session has a running chat → refuse, no new session created."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        sid = await _seed_session(datasource_id="mysql-1")
        # seed a running chat on that session
        from qwenpaw_data.host.core.domain.chat import Chat
        from qwenpaw_data.host.core.repositories.chat import ChatRepository

        async with get_session_factory()() as db:
            srepo = SessionRepository(db, TEST_IDENTITY)
            crepo = ChatRepository(db, TEST_IDENTITY)
            session = await srepo.get(sid)
            session.register_chat()
            await srepo.save(session)
            chat = Chat.start(
                session_id=session.id,
                identity=TEST_IDENTITY,
                sequence=1,
                datasource_id="mysql-1",
                text="hi",
            )
            await crepo.add(chat)
            await db.commit()

        with patch.object(
            channel,
            "send_datasource_card",
            new=AsyncMock(),  # type: ignore[method-assign]
        ) as send_card:
            await channel._handle_session(channel._native)
        send_card.assert_not_called()
        assert channel.sent[-1] == "当前有任务正在运行，请先 /stop 再新建会话"
        # still only one session row — binding unchanged
        async with get_session_factory()() as db:
            from sqlalchemy import func, select

            from qwenpaw_data.host.core.db.tables import SessionRow
            from qwenpaw_data.host.core.repositories import ChannelBindingRepository

            count = await db.scalar(
                select(func.count())
                .select_from(SessionRow)
                .where(SessionRow.channel == "feishu")
            )
            assert count == 1
            active_id = await ChannelBindingRepository(db).get_active_session_id(
                TEST_IDENTITY, "feishu", "feishu-abcd1234"
            )
        assert active_id == sid
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# feishu card callback: exact primary-key lookup
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_callback_uses_exact_session_id(tmp_path: Path) -> None:
    """The feishu card carries ``session.id``; lookup is an exact pk ``get``."""
    await reset_engine()
    await init_db()
    try:
        sid = await _seed_session(datasource_id=None)
        async with get_session_factory()() as db:
            repo = SessionRepository(db, TEST_IDENTITY)
            assert (await repo.get(sid)) is not None
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# /stop: live cancel vs orphaned-row recovery
# ---------------------------------------------------------------------


async def _seed_running_chat(session_id: str) -> str:
    """Seed a running chat row on ``session_id``; return its id."""
    from qwenpaw_data.host.core.domain.chat import Chat
    from qwenpaw_data.host.core.repositories.chat import ChatRepository

    async with get_session_factory()() as db:
        srepo = SessionRepository(db, TEST_IDENTITY)
        crepo = ChatRepository(db, TEST_IDENTITY)
        session = await srepo.get(session_id)
        session.register_chat()
        await srepo.save(session)
        chat = Chat.start(
            session_id=session.id,
            identity=TEST_IDENTITY,
            sequence=1,
            datasource_id="mysql-1",
            text="hi",
        )
        await crepo.add(chat)
        await db.commit()
        return chat.id


@pytest.mark.asyncio
async def test_stop_no_active_chat(tmp_path: Path) -> None:
    """No running chat."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        await _seed_session(datasource_id="mysql-1")
        await channel._handle_stop(channel._native)
        assert channel.sent[-1] == "无运行中任务"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stop_no_session(tmp_path: Path) -> None:
    """No session at all."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        await channel._handle_stop(channel._native)
        assert channel.sent[-1] == "无运行中任务"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stop_writes_cancel_command(tmp_path: Path) -> None:
    from qwenpaw_data.host.core.repositories.chat import ChatRepository
    from qwenpaw_data.host.core.repositories.chat_command import ChatCommandRepository

    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        sid = await _seed_session(datasource_id="mysql-1")
        chat_id = await _seed_running_chat(sid)
        await channel._handle_stop(channel._native)
        assert channel.sent[-1] == "已停止"
        async with get_session_factory()() as db:
            chat = await ChatRepository(db, TEST_IDENTITY).get(chat_id)
            assert chat.status == "running"
            assert await ChatCommandRepository(db).has_cancel(chat_id)
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# _stream_to_platform: reasoning deltas are filtered out
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_filters_out_reasoning_deltas(tmp_path: Path) -> None:
    """Reasoning deltas (msg_id not in the ``type=="message"`` whitelist) must not reach the card.

    Regression: the empty-whitelist fallback ``not assistant_msg_ids or ...`` leaked reasoning
    deltas that arrive before the first ``type=="message"`` message_start.
    """
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.streaming_enabled = True
        deltas: list[str] = []
        channel.on_streaming_delta = AsyncMock(
            side_effect=lambda n, h, d: deltas.append(d)
        )  # type: ignore[assignment]
        channel.on_streaming_start = AsyncMock(return_value={"id": "handle"})  # type: ignore[assignment]
        channel.on_streaming_end = AsyncMock()  # type: ignore[assignment]

        events = [
            _msg_event(
                "msg_reasoning", "reasoning", "in_progress"
            ),  # reasoning message_start
            _delta_event("msg_reasoning", "让我想想"),  # reasoning delta — must drop
            _delta_event("msg_reasoning", "再想想"),  # reasoning delta — must drop
            _msg_event("msg_answer", "message", "in_progress"),  # answer message_start
            _delta_event("msg_answer", "答案是"),  # answer delta — keep
            _delta_event("msg_answer", "42"),  # answer delta — keep
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        assert "".join(deltas) == "答案是42"
        channel.on_streaming_end.assert_awaited_once()
        # on_streaming_end receives the full filtered text
        assert channel.on_streaming_end.call_args.args[2] == "答案是42"
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# _stream_to_platform: artifact.registered pushes deliverables to IM
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_dispatches_image_artifact_to_send_image(tmp_path: Path) -> None:
    """An image artifact is resolved against artifact_dir and pushed via send_image."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.send_image = AsyncMock()  # type: ignore[assignment]
        channel.send_file = AsyncMock()  # type: ignore[assignment]
        events = [
            _artifact_registered_event("plot.png"),
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        channel.send_image.assert_awaited_once_with(
            channel._native, str(tmp_path / "plot.png")
        )
        channel.send_file.assert_not_called()
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_dispatches_report_artifact_to_send_file(tmp_path: Path) -> None:
    """A non-image deliverable (.html) is pushed via send_file."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.send_image = AsyncMock()  # type: ignore[assignment]
        channel.send_file = AsyncMock()  # type: ignore[assignment]
        events = [
            _artifact_registered_event("report.html"),
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        channel.send_file.assert_awaited_once_with(
            channel._native, str(tmp_path / "report.html")
        )
        channel.send_image.assert_not_called()
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_skips_dataset_artifacts(tmp_path: Path) -> None:
    """Raw dataset files (.csv/.xlsx/…) are NOT pushed to IM — the user fetches
    them from webui, matching the webUI segment card which hides them too."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.send_image = AsyncMock()  # type: ignore[assignment]
        channel.send_file = AsyncMock()  # type: ignore[assignment]
        events = [
            _artifact_registered_event("anomaly_anchor.csv"),
            _artifact_registered_event("trend_analysis.py"),  # .py still pushed
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        # .csv suppressed; .py pushed as a file.
        channel.send_image.assert_not_called()
        assert channel.send_file.await_count == 1
        pushed_path = channel.send_file.call_args.args[1]
        assert pushed_path.endswith("trend_analysis.py")
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_dedups_repeated_artifact_registration(tmp_path: Path) -> None:
    """A file re-registered on size/mtime change is pushed only once."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.send_image = AsyncMock()  # type: ignore[assignment]
        channel.send_file = AsyncMock()  # type: ignore[assignment]
        events = [
            _artifact_registered_event("plot.png"),
            _artifact_registered_event("plot.png"),  # re-registered (changed)
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        channel.send_image.assert_awaited_once()
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_delivers_artifact_right_after_its_segment_card(
    tmp_path: Path,
) -> None:
    """The image is registered mid-tool (before the segment card) but buffered
    and flushed immediately after that segment's conclusion card — so IM sees
    the card first, then the image, with the final answer card after."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.streaming_enabled = True
        order: list[str] = []

        async def _send_segment(native: Any, seg: Any) -> None:
            order.append("segment_card")

        async def _send_image(native: Any, path: str) -> None:
            order.append("image")

        async def _end(native: Any, handle: Any, full_text: str) -> None:
            order.append("answer_card_end")

        channel.send_segment = _send_segment  # type: ignore[assignment]
        channel.send_image = _send_image  # type: ignore[assignment]
        channel.send_file = AsyncMock()  # type: ignore[assignment]
        channel.on_streaming_start = AsyncMock(return_value={"id": "h"})  # type: ignore[assignment]
        channel.on_streaming_delta = AsyncMock()  # type: ignore[assignment]
        channel.on_streaming_end = _end  # type: ignore[assignment]

        events = [
            _artifact_registered_event("gaap_trend_march_2026.png"),  # tool made it
            _segment_event(
                title="绘制GAAP趋势并识别异常", conclusion="生成趋势图"
            ),  # card after
            _msg_event("msg_a", "message", "in_progress"),
            _delta_event("msg_a", "3月10日异常"),
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        # Segment card → image (tightly paired) → answer card — image not before,
        # not held to the end.
        assert order == ["segment_card", "image", "answer_card_end"]
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# _stream_to_platform: segment events are sent to IM as text
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_sends_segment_as_text(tmp_path: Path) -> None:
    """A ``segment`` event is formatted (输入/执行/结论) and sent to IM."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        events = [
            _segment_event(
                title="明确指标口径并生成分析计划",
                input="全局 query：分析GAAP",
                behavior="调用 search_context",
                conclusion="成功锁定指标",
                started_at=1000.0,
                ended_at=1187.0,  # 187s = 3分7秒
            ),
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        sent = "".join(channel.sent)
        assert "明确指标口径并生成分析计划" in sent
        assert "用时 3分7秒" in sent
        assert "输入" in sent and "全局 query" in sent
        assert "执行" in sent and "search_context" in sent
        assert "结论" in sent and "成功锁定指标" in sent
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_segment_omits_empty_sections(tmp_path: Path) -> None:
    """A segment with only a title + conclusion omits empty 输入/执行."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        events = [
            _segment_event(title="收尾", input="", behavior="", conclusion="完成"),
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        sent = "".join(channel.sent)
        assert "收尾" in sent and "完成" in sent
        assert "输入" not in sent
        assert "执行" not in sent
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_aborts_when_no_terminal_response(tmp_path: Path) -> None:
    """If the iterator ends without a ``response`` terminal event (e.g. the
    runtime crashed before emitting one — the disk-full case where
    ``response_failed`` itself can't write), surface a fallback notice and
    finalize the half-built card instead of leaving the user silent."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.streaming_enabled = True
        channel.on_streaming_start = AsyncMock(return_value={"id": "handle"})  # type: ignore[assignment]
        channel.on_streaming_end = AsyncMock()  # type: ignore[assignment]
        # No terminal ``response`` event: just a text delta then the iterator ends.
        events = [
            _msg_event("msg_answer", "message", "in_progress"),
            _delta_event("msg_answer", "部分"),
            _delta_event("msg_answer", "内容"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        # Card finalized with whatever text accumulated, and a fallback notice sent.
        channel.on_streaming_end.assert_awaited_once()
        assert channel.on_streaming_end.call_args.args[2] == "部分内容"
        assert channel.sent[-1] == "⚠️ 执行异常中断，未能完成；请稍后重试或联系管理员。"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_abort_no_card_sends_only_notice(tmp_path: Path) -> None:
    """Abort with no streaming card (handle is None) still sends the notice."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.streaming_enabled = True
        channel.on_streaming_start = AsyncMock(return_value=None)  # type: ignore[assignment]
        channel.on_streaming_end = AsyncMock()  # type: ignore[assignment]
        events: list[Any] = []  # empty stream — iterator ends immediately
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        channel.on_streaming_end.assert_not_awaited()
        assert channel.sent == ["⚠️ 执行异常中断，未能完成；请稍后重试或联系管理员。"]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_stream_completed_does_not_trigger_abort_notice(tmp_path: Path) -> None:
    """A normal ``response completed`` must NOT emit the abort fallback."""
    await reset_engine()
    await init_db()
    try:
        channel = _StubChannel()
        channel.streaming_enabled = False
        events = [
            _msg_event("msg_answer", "message", "in_progress"),
            _delta_event("msg_answer", "答案"),
            _response_event("completed"),
        ]
        hub = MagicMock()
        hub.subscribe_live = MagicMock(return_value=_iter_events(events))
        with patch("qwenpaw_data.host.core.channels.base.get_hub", return_value=hub):
            await channel._stream_to_platform(
                channel._native, "chat_1", "session_1", tmp_path
            )

        assert "⚠️ 执行异常中断" not in "".join(channel.sent)
    finally:
        await reset_engine()


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _artifact_registered_event(rel_path: str) -> Any:
    """An ``artifact.registered`` stream event carrying an ArtifactSchema."""
    from types import SimpleNamespace

    art = SimpleNamespace(path=rel_path)
    return SimpleNamespace(
        object="artifact.registered",
        sequence_number=0,
        artifact=art,
    )


def _segment_event(
    *,
    title: str,
    input: str = "",
    behavior: str = "",
    conclusion: str = "",
    started_at: float | None = None,
    ended_at: float | None = None,
) -> Any:
    """A ``segment`` stream event carrying a FrontendSegment-like object."""
    from types import SimpleNamespace

    seg = SimpleNamespace(
        title=title,
        input=input or None,
        behavior=behavior,
        conclusion=conclusion,
        started_at=started_at,
        ended_at=ended_at,
    )
    return SimpleNamespace(object="segment", sequence_number=0, segment=seg)


def _msg_event(msg_id: str, msg_type: str, status: str) -> Any:
    """A ``message`` stream event (message_start / message_complete)."""
    e = MagicMock()
    e.object = "message"
    e.type = msg_type
    e.id = msg_id
    e.status = status
    return e


def _delta_event(msg_id: str, text: str) -> Any:
    """A ``content`` text-delta stream event."""
    e = MagicMock()
    e.object = "content"
    e.type = "text"
    e.delta = True
    e.msg_id = msg_id
    e.text = text
    return e


def _response_event(status: str) -> Any:
    """A terminal ``response`` stream event."""
    e = MagicMock()
    e.object = "response"
    e.status = status
    e.error = None
    return e


async def _iter_events(events: list[Any]) -> Any:
    for e in events:
        yield e


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _make_text_native(text: str) -> Any:
    from qwenpaw_data.host.core.channels.schema import NativePayload, TextContent

    return NativePayload(
        channel_id="feishu",
        sender_id="ou_x",
        content_parts=[TextContent(text=text)],
        meta={
            "chat_id": "oc_abcd1234",
            "receive_id": "oc_abcd1234",
            "receive_id_type": "chat_id",
        },
    )


async def _empty_async_iter() -> Any:
    if False:  # pragma: no cover
        yield


class _StubChannel(BaseChannel):
    """Minimal BaseChannel subclass: records sends, for control-command tests."""

    channel = "feishu"

    def __init__(self) -> None:
        super().__init__()
        self._owner_identity = TEST_IDENTITY
        self.sent: list[str] = []
        self._native = _make_text_native("")

    async def _resolve_access_token(self) -> str | None:
        # Tests exercise the card/binding flow, not token resolution.
        return "test-token"

    def resolve_session_id(self, native: Any) -> str:
        return "feishu-abcd1234"

    async def send(self, native: Any, text: str) -> None:
        self.sent.append(text)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _stop(self) -> None:
        pass

    def extract_target_meta(self, native: Any) -> dict[str, Any] | None:
        return None

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        pass

    async def on_streaming_start(self, native: Any, msg_id: str) -> Any:
        return None

    async def on_streaming_delta(self, native: Any, handle: Any, delta: str) -> None:
        pass

    async def on_streaming_end(self, native: Any, handle: Any, full_text: str) -> None:
        pass

    async def on_streaming_close(self, native: Any, handle: Any, summary: str) -> None:
        pass

    async def on_streaming_reasoning_delta(
        self, native: Any, handle: Any, accumulated: str
    ) -> None:
        pass

    async def on_consume_start(self, native: Any) -> None:
        pass

    async def on_consume_end(self, native: Any, handle: Any, status: str) -> None:
        pass

    async def send_image(self, native: Any, path: str) -> None:
        pass

    async def send_file(self, native: Any, path: str) -> None:
        pass

    async def send_clarification_card(self, native: Any, questions: Any) -> str:
        return ""

    async def send_datasource_card(
        self,
        native: Any,
        session: Any,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        pass

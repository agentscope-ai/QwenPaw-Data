# -*- coding: utf-8 -*-
"""Channel subsystem core: config logic, stores, base streaming, manager."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import qwenpaw_data.host.core.channels.base as base_module
import qwenpaw_data.host.core.channels.manager as manager_module
from qwenpaw_data.host.core.api.models.cron import CronJobWrite
from qwenpaw_data.host.core.api.models.stream_objects import parse_stream_object
from qwenpaw_data.host.core.channels.base import (
    BaseChannel,
    ChannelServices,
    degrade_local_image_md,
    is_control_command,
)
from qwenpaw_data.host.core.channels.clarification_question import (
    build_clarification_questions,
)
from qwenpaw_data.host.core.channels.config import (
    ChannelIdConflictError,
    apply_channel_update,
    check_channel_id_conflict,
    initial_config,
    mask_channel,
    test_channel_config as validate_channel_config,
)
from qwenpaw_data.host.core.channels.manager import ChannelManager
from qwenpaw_data.host.core.channels.schema import (
    NativePayload,
    TextContent,
    flatten_content_parts_to_str,
)
from qwenpaw_data.host.core.channels.segment_markup import render_segment_spans
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.json_store import (
    JSONChannelBindingStore,
    JSONChannelConfigStore,
    JSONChatEventStore,
    JSONChatStore,
    JSONSessionStore,
)

sqlalchemy = pytest.importorskip("sqlalchemy")

from qwenpaw_data.host.core.db.engine import (  # noqa: E402
    create_engine_and_factory,
    init_db,
)
from qwenpaw_data.host.core.store.sql_store import (  # noqa: E402
    SQLChannelBindingStore,
    SQLChannelConfigStore,
)


# --- config logic ---------------------------------------------------------------


def test_apply_channel_update_secret_defense() -> None:
    config = initial_config()
    config = apply_channel_update(
        config, "feishu", {"enabled": True, "app_id": " cli_x ", "app_secret": "real-secret-123"}
    )
    assert config["feishu"]["enabled"] is True
    assert config["feishu"]["app_id"] == "cli_x"
    assert config["feishu"]["app_secret"] == "real-secret-123"

    masked = mask_channel("feishu", config["feishu"])
    assert masked["app_secret"] != "real-secret-123"
    assert masked["app_secret"].startswith("real")

    # masked echo and empty string must not overwrite the real secret
    config = apply_channel_update(
        config, "feishu", {"app_secret": masked["app_secret"]}
    )
    assert config["feishu"]["app_secret"] == "real-secret-123"
    config = apply_channel_update(config, "feishu", {"app_secret": "  "})
    assert config["feishu"]["app_secret"] == "real-secret-123"
    # None means "no change"
    config = apply_channel_update(config, "feishu", {"enabled": None})
    assert config["feishu"]["enabled"] is True


def test_test_channel_config_required_fields() -> None:
    config = initial_config()
    result = validate_channel_config(config, "dingtalk")
    assert result["success"] is False
    assert "client_id" in result["message"]

    config = apply_channel_update(
        config, "dingtalk", {"client_id": "c1", "client_secret": "s1"}
    )
    assert validate_channel_config(config, "dingtalk")["success"] is True


async def test_check_channel_id_conflict(tmp_path: Path) -> None:
    configs = JSONChannelConfigStore(tmp_path)
    alice = apply_channel_update(initial_config(), "wecom", {"bot_id": "bot-1"})
    await configs.save("alice", alice)

    mine = apply_channel_update(initial_config(), "wecom", {"bot_id": "bot-1"})
    with pytest.raises(ChannelIdConflictError) as exc:
        await check_channel_id_conflict(configs, "wecom", mine, "bob")
    assert "bob" not in str(exc.value)  # sanitized message

    # same user re-saving its own id is fine
    await check_channel_id_conflict(configs, "wecom", mine, "alice")
    # a different id is fine
    other = apply_channel_update(initial_config(), "wecom", {"bot_id": "bot-2"})
    await check_channel_id_conflict(configs, "wecom", other, "bob")


# --- stores ---------------------------------------------------------------------


@pytest.fixture(params=["json", "sql"])
async def channel_stores(request, tmp_path: Path):
    if request.param == "json":
        yield JSONChannelConfigStore(tmp_path), JSONChannelBindingStore(tmp_path)
        return
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{tmp_path / 'channels.db'}",
    )
    await init_db(engine)
    yield SQLChannelConfigStore(factory), SQLChannelBindingStore(factory)
    await engine.dispose()


async def test_config_store_roundtrip(channel_stores) -> None:
    configs, _ = channel_stores
    assert await configs.load("local") == initial_config()
    assert await configs.list_user_ids() == []

    updated = apply_channel_update(
        initial_config(), "feishu", {"enabled": True, "app_id": "cli_1"}
    )
    await configs.save("local", updated)
    loaded = await configs.load("local")
    assert loaded["feishu"]["enabled"] is True
    assert loaded["feishu"]["app_id"] == "cli_1"
    assert await configs.list_user_ids() == ["local"]


async def test_binding_store_roundtrip(channel_stores) -> None:
    _, bindings = channel_stores
    assert await bindings.get_active_session_id("local", "feishu", "feishu:oc1") is None
    assert await bindings.exists("local", "feishu", "feishu:oc1") is False

    await bindings.point_to(
        "local", "feishu", "feishu:oc1", "ses_1",
        target_meta={"target_type": "group", "send_meta": {"chat_id": "oc1"}},
        display_name="ses_1",
    )
    assert await bindings.get_active_session_id("local", "feishu", "feishu:oc1") == "ses_1"
    assert await bindings.exists("local", "feishu", "feishu:oc1") is True
    meta = await bindings.get_target_meta("local", "feishu", "feishu:oc1")
    assert meta == {
        "target_type": "group",
        "display_name": "ses_1",
        "send_meta": {"chat_id": "oc1"},
    }

    # repoint without target_meta keeps the send address
    await bindings.point_to("local", "feishu", "feishu:oc1", "ses_2")
    assert await bindings.get_active_session_id("local", "feishu", "feishu:oc1") == "ses_2"
    meta = await bindings.get_target_meta("local", "feishu", "feishu:oc1")
    assert meta["send_meta"] == {"chat_id": "oc1"}

    await asyncio.sleep(0.01)
    await bindings.point_to("local", "feishu", "feishu:oc2", "ses_3")
    listed = await bindings.list_by_channel("local", "feishu")
    assert [t["external_key"] for t in listed] == ["feishu:oc2", "feishu:oc1"]
    assert await bindings.list_by_channel("other", "feishu") == []


# --- schema / markup / clarification helpers -------------------------------------


def test_flatten_content_parts() -> None:
    parts = [
        TextContent(text="你好"),
        SimpleNamespace(type="image", text=None),
        TextContent(text="世界"),
    ]
    assert flatten_content_parts_to_str(parts) == "你好[图片]世界"


def test_control_command_detection() -> None:
    assert is_control_command("/stop")
    assert is_control_command("  /DATASOURCE now")
    assert is_control_command("帮助")
    assert not is_control_command("分析下销量")
    assert not is_control_command("")


def test_render_segment_spans() -> None:
    body = '增长 <span class="text-green-600 font-bold">12%</span>'
    assert render_segment_spans(body, target="plain") == "增长 12%"
    assert (
        render_segment_spans(body, target="feishu")
        == '增长 <font color="green">**12%**</font>'
    )
    assert render_segment_spans(body, target="dingtalk") == "增长 **12%**"
    unknown = '<span class="text-pink-100">x</span>'
    assert render_segment_spans(unknown, target="feishu") == unknown


def test_degrade_local_image_md() -> None:
    assert (
        degrade_local_image_md("看 ![图](/tmp/a.png) 这里")
        == "看 `(/tmp/a.png)` 这里"
    )
    assert degrade_local_image_md("![x](https://a/b.png)") == "![x](https://a/b.png)"


def _plugin_call_message() -> Any:
    return parse_stream_object(
        {
            "object": "message",
            "id": "msg_1",
            "sequence": 1,
            "type": "plugin_call",
            "role": "assistant",
            "status": "completed",
            "sequence_number": 7,
            "session_id": "ses_1",
            "chat_id": "chat_1",
            "content": [
                {
                    "object": "content",
                    "type": "data",
                    "data": {
                        "call_id": "call_1",
                        "name": "ask_user_question",
                        "arguments": (
                            '{"title": "口径", "questions": [{"question": "取哪种口径?",'
                            ' "multiSelect": false, "options": ['
                            '{"label": "GAAP", "description": null},'
                            '{"label": "Non-GAAP", "description": "调整后"}]}]}'
                        ),
                    },
                }
            ],
        }
    )


def test_build_clarification_questions() -> None:
    group = build_clarification_questions(_plugin_call_message())
    assert group is not None
    assert group.id == "call_1"
    assert group.chat_id == "chat_1"
    assert group.title == "口径"
    assert [o.label for o in group.questions[0].options] == ["GAAP", "Non-GAAP"]


# --- fake channel + services ------------------------------------------------------


class FakeChannel(BaseChannel):
    channel = "faketalk"
    streaming_enabled = True

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.stream_calls: list[tuple[str, Any]] = []
        self.images: list[str] = []
        self.files: list[str] = []
        self.followups: list[list[str]] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def _stop(self) -> None:
        self.stopped = True

    def resolve_session_id(self, native: NativePayload) -> str:
        return f"faketalk:{native.meta.get('conv_id', native.sender_id)}"

    def extract_target_meta(self, native: NativePayload) -> dict[str, Any] | None:
        return {"target_type": "group", "send_meta": dict(native.meta)}

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        self.stream_calls.append(("cron", cron_job_config["id"]))

    async def send(self, native: NativePayload, text: str) -> None:
        self.sent.append(text)

    async def on_streaming_start(self, native: NativePayload, msg_id: str) -> Any:
        self.stream_calls.append(("start", msg_id))
        return {"full_text": ""}

    async def on_streaming_delta(self, native, handle, delta) -> None:
        self.stream_calls.append(("delta", delta))

    async def on_streaming_end(self, native, handle, full_text) -> None:
        self.stream_calls.append(("end", full_text))

    async def on_streaming_close(self, native, handle, summary) -> None:
        self.stream_calls.append(("close", summary))

    async def on_streaming_reasoning_delta(self, native, handle, accumulated) -> None:
        self.stream_calls.append(("reasoning", accumulated))

    async def on_consume_start(self, native) -> None:
        self.stream_calls.append(("consume_start", None))

    async def on_consume_end(self, native, handle, status) -> None:
        self.stream_calls.append(("consume_end", status))

    async def send_image(self, native, path) -> None:
        self.images.append(path)

    async def send_file(self, native, path) -> None:
        self.files.append(path)

    async def send_clarification_card(self, native, questions) -> str:
        self.stream_calls.append(("clarification", questions.id))
        return "card_1"

    async def send_followups(self, native, questions) -> None:
        self.followups.append(list(questions))

    async def send_datasource_card(self, native, session, items, selectable=True) -> None:
        self.stream_calls.append(("ds_card", (session.id, selectable, len(items))))


def _services(tmp_path: Path) -> ChannelServices:
    return ChannelServices(
        sessions=JSONSessionStore(tmp_path),
        chats=JSONChatStore(tmp_path),
        events=JSONChatEventStore(tmp_path),
        bindings=JSONChannelBindingStore(tmp_path),
        configs=JSONChannelConfigStore(tmp_path),
        hosts=None,
    )


def _native(text: str = "hi", conv: str = "c1") -> NativePayload:
    return NativePayload(
        channel_id="faketalk",
        sender_id="u1",
        content_parts=[TextContent(text=text)],
        meta={"conv_id": conv},
    )


def _channel(tmp_path: Path) -> FakeChannel:
    ch = FakeChannel()
    ch.set_owner_identity(Identity.anonymous())
    ch.set_services(_services(tmp_path))
    return ch


# --- base behaviors ----------------------------------------------------------------


async def test_handle_session_creates_session_and_binding(tmp_path) -> None:
    ch = _channel(tmp_path)
    handled = await ch._handle_control_command(_native("/session"), "/session")
    assert handled is True
    key = "faketalk:c1"
    sid = await ch.services.bindings.get_active_session_id("local", "faketalk", key)
    assert sid is not None
    session = await ch.services.sessions.get(sid)
    assert session.channel == "faketalk"
    # unbound datasource → datasource selection was attempted (send or card)
    assert ch.sent or any(c[0] == "ds_card" for c in ch.stream_calls)


async def test_handle_stop_without_session(tmp_path) -> None:
    ch = _channel(tmp_path)
    await ch._handle_control_command(_native("/stop"), "/stop")
    assert ch.sent == ["无运行中任务"]


async def test_help_menu(tmp_path) -> None:
    ch = _channel(tmp_path)
    await ch._handle_control_command(_native("help"), "help")
    assert ch.sent and "/stop" in ch.sent[0]


class _FakeHub:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def subscribe_live(self, chat_id: str):
        for ev in self.events:
            yield ev


def _msg(payload: dict[str, Any]) -> Any:
    return parse_stream_object(
        {"sequence_number": 1, "session_id": "ses_1", "chat_id": "chat_1", **payload}
    )


async def test_stream_to_platform_lifecycle(tmp_path, monkeypatch) -> None:
    ch = _channel(tmp_path)
    events = [
        None,  # heartbeat is skipped
        _msg({"object": "response", "id": "rsp_1", "status": "in_progress"}),
        _msg(
            {
                "object": "message",
                "id": "m_r",
                "sequence": 0,
                "type": "reasoning",
                "role": "assistant",
                "status": "in_progress",
            }
        ),
        _msg(
            {
                "object": "content",
                "msg_id": "m_r",
                "type": "text",
                "delta": True,
                "text": "想一想",
            }
        ),
        _msg(
            {
                "object": "message",
                "id": "m_a",
                "sequence": 1,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
            }
        ),
        _msg(
            {
                "object": "content",
                "msg_id": "m_a",
                "type": "text",
                "delta": True,
                "text": "答案",
            }
        ),
        _msg(
            {
                "object": "artifact.registered",
                "artifact": {
                    "id": "art_1",
                    "session_id": "ses_1",
                    "name": "chart.png",
                    "path": "chart.png",
                    "created_at": "2026-09-02T00:00:00Z",
                    "updated_at": "2026-09-02T00:00:00Z",
                },
            }
        ),
        _msg(
            {
                "object": "artifact.registered",
                "artifact": {
                    "id": "art_2",
                    "session_id": "ses_1",
                    "name": "rows.csv",
                    "path": "rows.csv",
                    "created_at": "2026-09-02T00:00:00Z",
                    "updated_at": "2026-09-02T00:00:00Z",
                },
            }
        ),
        _msg({"object": "response", "id": "rsp_1", "status": "completed"}),
    ]
    monkeypatch.setattr(base_module, "get_hub", lambda: _FakeHub(events))
    # a followup event persisted for this chat is delivered on completion
    await ch.services.events.append(
        session_id="ses_1",
        chat_id="chat_1",
        payload={"object": "followup.generated", "followup": {"questions": ["然后呢?"]}},
    )

    await ch._stream_to_platform(_native(), "chat_1", "ses_1", tmp_path)

    kinds = [k for k, _ in ch.stream_calls]
    assert kinds[0] == "start"
    assert ("reasoning", "想一想") in ch.stream_calls
    assert ("delta", "答案") in ch.stream_calls
    assert ("end", "答案") in ch.stream_calls
    assert ("consume_end", "completed") in ch.stream_calls
    # png delivered, csv suppressed
    assert ch.images == [str(tmp_path / "chart.png")]
    assert ch.files == []
    assert ch.followups == [["然后呢?"]]


async def test_stream_to_platform_failure_message(tmp_path, monkeypatch) -> None:
    ch = _channel(tmp_path)
    events = [
        _msg(
            {
                "object": "response",
                "id": "rsp_1",
                "status": "failed",
                "error": {"code": "VALIDATION", "message": "模型不可用"},
            }
        ),
    ]
    monkeypatch.setattr(base_module, "get_hub", lambda: _FakeHub(events))
    await ch._stream_to_platform(_native(), "chat_1", "ses_1", tmp_path)
    assert any("模型不可用" in s for s in ch.sent)
    assert ("consume_end", "failed") in ch.stream_calls


async def test_stream_abort_without_terminal_event(tmp_path, monkeypatch) -> None:
    ch = _channel(tmp_path)
    monkeypatch.setattr(base_module, "get_hub", lambda: _FakeHub([]))
    await ch._stream_to_platform(_native(), "chat_1", "ses_1", tmp_path)
    assert any("执行异常中断" in s for s in ch.sent)


async def test_clarification_card_roundtrip(tmp_path, monkeypatch) -> None:
    ch = _channel(tmp_path)
    events = [
        _plugin_call_message(),
        _msg({"object": "response", "id": "rsp_1", "status": "completed"}),
    ]
    monkeypatch.setattr(base_module, "get_hub", lambda: _FakeHub(events))
    await ch._stream_to_platform(_native(), "chat_1", "ses_1", tmp_path)
    assert ("clarification", "call_1") in ch.stream_calls
    assert "card_1" in ch._pending_clarification_cards

    answered: list[dict[str, Any]] = []

    class _Runtime:
        def answer(self, *, clarification_id: str, result: dict[str, Any]) -> None:
            answered.append({"id": clarification_id, "result": result})

    monkeypatch.setattr(
        base_module,
        "get_runtime_registry",
        lambda: SimpleNamespace(get=lambda chat_id: _Runtime()),
    )
    ok = await ch._submit_clarification_card_answer("card_1", {"0": ["GAAP"]})
    assert ok is True
    assert answered[0]["id"] == "call_1"
    assert answered[0]["result"]["answers"][0]["selected_options"] == ["GAAP"]
    # unknown card
    assert await ch._submit_clarification_card_answer("card_x", {}) is False


# --- manager -----------------------------------------------------------------------


async def test_manager_builds_enabled_channels(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path)
    config = apply_channel_update(
        initial_config(), "feishu", {"enabled": True, "app_id": "cli_1"}
    )
    await services.configs.save("local", config)

    built: list[tuple[str, str]] = []

    def _fake_build(identity: Identity, channel_type: str):
        built.append((identity.user_id, channel_type))
        ch = FakeChannel()
        ch.set_owner_identity(identity)
        return ch

    monkeypatch.setattr(manager_module, "build_channel", _fake_build)
    mgr = await ChannelManager.from_services(services)
    assert built == [("local", "feishu")]
    assert mgr.get_channel("local", "feishu") is not None
    assert mgr.get_channel("local", "dingtalk") is None

    await mgr.start_all()
    assert mgr.get_channel("local", "feishu").started is True

    # disable and reload one → stopped and removed
    await services.configs.save(
        "local", apply_channel_update(config, "feishu", {"enabled": False})
    )
    result = await mgr.reload("local", "feishu")
    assert result["stopped"] == ["local/feishu"]
    assert result["started"] == []
    assert mgr.get_channel("local", "feishu") is None


async def test_manager_cron_routing(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path)
    await services.configs.save(
        "local",
        apply_channel_update(initial_config(), "wecom", {"enabled": True, "bot_id": "b1"}),
    )
    monkeypatch.setattr(
        manager_module,
        "build_channel",
        lambda identity, key: (
            lambda ch: (ch.set_owner_identity(identity), ch)[1]
        )(FakeChannel()),
    )
    mgr = await ChannelManager.from_services(services)

    with pytest.raises(ValueError, match="no active channel"):
        await mgr.run_cron_job(
            {"id": "cron_1", "user_id": "local", "channel": "dingtalk"}
        )

    await mgr.run_cron_job({"id": "cron_1", "user_id": "local", "channel": "wecom"})
    assert ("cron", "cron_1") in mgr.get_channel("local", "wecom").stream_calls


async def test_manager_cron_eligibility(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path)
    await services.configs.save(
        "local",
        apply_channel_update(initial_config(), "wecom", {"enabled": True, "bot_id": "b1"}),
    )
    monkeypatch.setattr(
        manager_module,
        "build_channel",
        lambda identity, key: (
            lambda ch: (ch.set_owner_identity(identity), ch)[1]
        )(FakeChannel()),
    )
    mgr = await ChannelManager.from_services(services)

    body = CronJobWrite(
        name="日报",
        message="生成日报",
        datasource_id="ds1",
        channel="wecom",
        target_external_key="wecom:room1",
        schedule={"type": "cron", "cron": "0 8 * * *"},
    )
    # target never spoke to the bot → rejected
    with pytest.raises(ValueError, match="target external key"):
        await mgr.is_channel_eligible_for_cron_job(Identity.anonymous(), body)

    await services.bindings.point_to("local", "wecom", "wecom:room1", "ses_1")
    await mgr.is_channel_eligible_for_cron_job(Identity.anonymous(), body)


def test_cron_write_requires_target_for_im() -> None:
    with pytest.raises(ValueError, match="target_external_key"):
        CronJobWrite(
            name="x",
            message="y",
            datasource_id="ds1",
            channel="feishu",
            schedule={"type": "cron", "cron": "0 8 * * *"},
        )
    job = CronJobWrite(
        name="x",
        message="y",
        datasource_id="ds1",
        schedule={"type": "cron", "cron": "0 8 * * *"},
    )
    assert job.channel == "console"
    assert job.target_external_key is None

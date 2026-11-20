# -*- coding: utf-8 -*-
"""Feishu channel: streaming hooks + Typing reaction."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qwenpaw_data.host.core.channels.feishu.channel import FeishuChannel
from qwenpaw_data.host.core.channels.schema import NativePayload, TextContent


def _make_native(text: str = "") -> NativePayload:
    """A native payload mimicking feishu _on_message's meta."""
    return NativePayload(
        channel_id="feishu",
        sender_id="ou_sender",
        content_parts=[TextContent(text=text)],
        meta={
            "feishu_message_id": "om_msg_1",
            "chat_id": "oc_chat_1",
            "receive_id": "oc_chat_1",
            "receive_id_type": "chat_id",
        },
    )


def _channel(streaming: bool = True) -> FeishuChannel:
    ch = FeishuChannel()
    ch.streaming_enabled = streaming
    # _client is set by start(); fake it with an AsyncMock so acreate() is awaitable.
    ch._client = MagicMock()
    return ch


def _ok_resp() -> MagicMock:
    """A successful lark response: success() is True, data.card_id set."""
    resp = MagicMock()
    resp.success.return_value = True
    resp.data = MagicMock(card_id="card_1")
    return resp


# ---------------------------------------------------------------------
# on_streaming_start: pre-create placeholder
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_start_precreate_writes_thinking_placeholder() -> None:
    """Pre-create path (msg_id="") seeds the card with 💭 thinking..."""
    ch = _channel(streaming=True)
    create_card = AsyncMock(return_value={"card_id": "card_1", "message_id": "om_card"})
    with patch.object(ch, "_create_streaming_card", create_card):
        handle = await ch.on_streaming_start(_make_native(), "")
    assert handle is not None
    # initial_text passed to _create_streaming_card carries the 💭 placeholder
    args, _ = create_card.call_args
    assert args[2] == "💭 thinking..."


@pytest.mark.asyncio
async def test_streaming_start_normal_path_uses_ellipsis() -> None:
    """Normal path (non-empty msg_id) keeps the original '...' placeholder."""
    ch = _channel(streaming=True)
    create_card = AsyncMock(return_value={"card_id": "card_1", "message_id": "om_card"})
    with patch.object(ch, "_create_streaming_card", create_card):
        handle = await ch.on_streaming_start(_make_native(), "msg_1")
    assert handle is not None
    args, _ = create_card.call_args
    assert args[2] == "..."


@pytest.mark.asyncio
async def test_streaming_start_disabled_returns_none() -> None:
    ch = _channel(streaming=False)
    assert await ch.on_streaming_start(_make_native(), "") is None


# ---------------------------------------------------------------------
# on_streaming_reasoning_delta
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_delta_renders_thinking_prefix() -> None:
    """Reasoning accumulated text is rendered with a 💭 prefix, full-replace."""
    ch = _channel(streaming=True)
    update = AsyncMock(return_value=True)
    with patch.object(ch, "_update_streaming_text", update):
        handle: dict[str, Any] = {
            "card_id": "card_1",
            "message_id": "om_card",
            "sequence": 0,
            "last_update": 0.0,
            "full_text": "",
        }
        await ch.on_streaming_reasoning_delta(_make_native(), handle, "analyzing")
    update.assert_called_once_with("card_1", "💭 analyzing", 1)


@pytest.mark.asyncio
async def test_reasoning_delta_does_not_pollute_full_text() -> None:
    """Reasoning must not touch handle['full_text'] (reserved for the final answer)."""
    ch = _channel(streaming=True)
    with patch.object(ch, "_update_streaming_text", AsyncMock(return_value=True)):
        handle: dict[str, Any] = {
            "card_id": "card_1",
            "message_id": "om_card",
            "sequence": 0,
            "last_update": 0.0,
            "full_text": "",
        }
        await ch.on_streaming_reasoning_delta(_make_native(), handle, "thinking hard")
    assert handle["full_text"] == ""


@pytest.mark.asyncio
async def test_reasoning_delta_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reasoning shares the delta throttle budget (last_update)."""
    monkeypatch.setattr(
        "qwenpaw_data.host.core.channels.feishu.channel.FEISHU_STREAM_MIN_INTERVAL_S",
        100.0,
    )
    ch = _channel(streaming=True)
    update = AsyncMock(return_value=True)
    with patch.object(ch, "_update_streaming_text", update):
        handle: dict[str, Any] = {
            "card_id": "card_1",
            "message_id": "om_card",
            "sequence": 0,
            "last_update": 0.0,
            "full_text": "",
        }
        await ch.on_streaming_reasoning_delta(_make_native(), handle, "first")
        await ch.on_streaming_reasoning_delta(_make_native(), handle, " second")
    update.assert_called_once_with("card_1", "💭 first", 1)


@pytest.mark.asyncio
async def test_reasoning_delta_no_handle_is_noop() -> None:
    ch = _channel(streaming=True)
    await ch.on_streaming_reasoning_delta(_make_native(), None, "x")  # must not raise


# ---------------------------------------------------------------------
# on_streaming_delta (message answer): accumulate-before-throttle
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_delta_accumulates_before_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttled delta is still accumulated into full_text — only the API
    push is rate-limited. Without this the live card drops every throttled
    delta and freezes on partial text (e.g. mid-image-link)."""
    monkeypatch.setattr(
        "qwenpaw_data.host.core.channels.feishu.channel.FEISHU_STREAM_MIN_INTERVAL_S",
        100.0,
    )
    ch = _channel(streaming=True)
    update = AsyncMock(return_value=True)
    with patch.object(ch, "_update_streaming_text", update):
        handle: dict[str, Any] = {
            "card_id": "card_1",
            "message_id": "om_card",
            "sequence": 0,
            "last_update": 0.0,
            "full_text": "",
        }
        await ch.on_streaming_delta(_make_native(), handle, "hello")
        # second delta within the throttle window -> push skipped, but accumulated
        await ch.on_streaming_delta(_make_native(), handle, " world")
    update.assert_called_once_with("card_1", "hello", 1)
    assert handle["full_text"] == "hello world"


# ---------------------------------------------------------------------
# on_streaming_end: final full-text push before seal
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_end_pushes_full_text_before_finalize() -> None:
    """on_streaming_end pushes the complete full_text once (backstopping any
    throttled-away live update) then seals the card via _finalize_streaming_card."""
    ch = _channel(streaming=True)
    update = AsyncMock(return_value=True)
    finalize = AsyncMock(return_value=True)
    with (
        patch.object(ch, "_update_streaming_text", update),
        patch.object(ch, "_finalize_streaming_card", finalize),
    ):
        handle: dict[str, Any] = {
            "card_id": "card_1",
            "message_id": "om_card",
            "sequence": 3,
            "last_update": 0.0,
            "full_text": "partial",
        }
        await ch.on_streaming_end(_make_native(), handle, "the full answer")
    update.assert_awaited_once_with("card_1", "the full answer", 4)
    finalize.assert_awaited_once_with("card_1", "the full answer", 4)


@pytest.mark.asyncio
async def test_streaming_end_push_failure_falls_back_to_send() -> None:
    """If the final full-text push fails, send() the (cleaned) answer so the
    user still gets it; the card is sealed regardless."""
    ch = _channel(streaming=True)
    update = AsyncMock(return_value=False)
    finalize = AsyncMock(return_value=True)
    send = AsyncMock()
    with (
        patch.object(ch, "_update_streaming_text", update),
        patch.object(ch, "_finalize_streaming_card", finalize),
        patch.object(ch, "send", send),
    ):
        handle: dict[str, Any] = {
            "card_id": "card_1",
            "message_id": "om_card",
            "sequence": 3,
            "last_update": 0.0,
            "full_text": "",
        }
        await ch.on_streaming_end(_make_native(), handle, "the full answer")
    send.assert_awaited_once()  # cleaned full text delivered
    assert send.call_args.args[1] == "the full answer"
    finalize.assert_awaited_once()  # card sealed even on push failure


@pytest.mark.asyncio
async def test_finalize_card_degrades_local_image_in_summary_preview() -> None:
    """The collapsed summary preview must match the card's displayed (degraded)
    content — not carry raw ``![alt](/local/path)`` syntax that CardKit can't
    render. ``_update_streaming_text`` degrades; ``_finalize_streaming_card``
    must do the same to the preview it seals."""
    ch = _channel(streaming=True)
    resp = MagicMock()
    resp.success.return_value = True
    ch._client.cardkit.v1.card.asettings = AsyncMock(return_value=resp)

    captured: dict[str, Any] = {}
    real_dumps = json.dumps

    def _capture(obj: Any, *a: Any, **k: Any) -> str:
        if isinstance(obj, dict):
            captured.update(obj)
        return real_dumps(obj, *a, **k)

    summary = "see ![chart](/home/u/.qwenpaw-data/ses_x/gaap_t.png) end"
    with patch(
        "qwenpaw_data.host.core.channels.feishu.channel.json.dumps", side_effect=_capture
    ):
        await ch._finalize_streaming_card("card_1", summary, 5)

    preview = captured["config"]["summary"]["content"]
    assert "![chart](/home/u/.qwenpaw-data/ses_x/gaap_t.png)" not in preview
    assert "(/home/u/.qwenpaw-data/ses_x/gaap_t.png)" in preview  # degraded inline-code


# ---------------------------------------------------------------------
# degrade_local_image_md: replace unrenderable local-path images
# ---------------------------------------------------------------------


def test_degrade_local_image_md_replaces_local_path() -> None:
    """A complete ![alt](/local/path) is replaced with an inline-code path
    (CardKit can't render it; replacing keeps it visible instead of vanishing)."""
    from qwenpaw_data.host.core.channels.base import degrade_local_image_md

    text = "before\n![chart](/home/user/.qwenpaw-data/ses_x/gaap_trend)\nafter"
    assert (
        degrade_local_image_md(text)
        == "before\n`(/home/user/.qwenpaw-data/ses_x/gaap_trend)`\nafter"
    )


def test_degrade_local_image_md_keeps_image_key_and_http() -> None:
    """Feishu image_key (img_v3_…) and http URL images are preserved."""
    from qwenpaw_data.host.core.channels.base import degrade_local_image_md

    text = "![ok](img_v3_abc) and ![url](https://x/y.png)"
    assert degrade_local_image_md(text) == text


# ---------------------------------------------------------------------
# on_consume_start: Typing reaction
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_start_sends_typing_reaction() -> None:
    """on_consume_start adds a Typing reaction to the incoming message."""
    ch = _channel(streaming=True)
    # _client.im.v1.message_reaction.acreate -> async success response
    reaction_create = AsyncMock(return_value=_ok_resp())
    ch._client.im.v1.message_reaction.acreate = reaction_create
    with (
        patch("lark_oapi.api.im.v1.CreateMessageReactionRequest") as ReqCtor,
        patch("lark_oapi.api.im.v1.CreateMessageReactionRequestBody") as BodyCtor,
        patch("lark_oapi.api.im.v1.Emoji") as EmojiCtor,
    ):
        # builder() chains return self for .message_id / .request_body / .build()
        req_builder = MagicMock()
        req_builder.message_id.return_value = req_builder
        req_builder.request_body.return_value = req_builder
        req_builder.build.return_value = MagicMock()
        ReqCtor.builder.return_value = req_builder
        body_builder = MagicMock()
        body_builder.reaction_type.return_value = body_builder
        body_builder.build.return_value = MagicMock()
        BodyCtor.builder.return_value = body_builder
        emoji_builder = MagicMock()
        emoji_builder.emoji_type.return_value = emoji_builder
        emoji_builder.build.return_value = MagicMock()
        EmojiCtor.builder.return_value = emoji_builder

        await ch.on_consume_start(_make_native())

    reaction_create.assert_awaited_once()
    # The built request is passed through to acreate
    req_builder.message_id.assert_called_once_with("om_msg_1")


@pytest.mark.asyncio
async def test_consume_start_no_message_id_is_noop() -> None:
    """No feishu_message_id in meta -> no reaction call."""
    ch = _channel(streaming=True)
    ch._client.im.v1.message_reaction.acreate = AsyncMock(return_value=_ok_resp())
    native = NativePayload(
        channel_id="feishu",
        sender_id="ou_sender",
        content_parts=[TextContent(text="hi")],
        meta={},  # no feishu_message_id
    )
    await ch.on_consume_start(native)
    ch._client.im.v1.message_reaction.acreate.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_start_reaction_failure_is_swallowed() -> None:
    """A failed reaction response must not raise (best-effort)."""
    ch = _channel(streaming=True)
    fail_resp = MagicMock()
    fail_resp.success.return_value = False
    fail_resp.code = "99991"
    fail_resp.msg = "no permission"
    ch._client.im.v1.message_reaction.acreate = AsyncMock(return_value=fail_resp)
    with (
        patch("lark_oapi.api.im.v1.CreateMessageReactionRequest") as ReqCtor,
        patch("lark_oapi.api.im.v1.CreateMessageReactionRequestBody") as BodyCtor,
        patch("lark_oapi.api.im.v1.Emoji") as EmojiCtor,
    ):
        req_builder = MagicMock()
        req_builder.message_id.return_value = req_builder
        req_builder.request_body.return_value = req_builder
        req_builder.build.return_value = MagicMock()
        ReqCtor.builder.return_value = req_builder
        body_builder = MagicMock()
        body_builder.reaction_type.return_value = body_builder
        body_builder.build.return_value = MagicMock()
        BodyCtor.builder.return_value = body_builder
        emoji_builder = MagicMock()
        emoji_builder.emoji_type.return_value = emoji_builder
        emoji_builder.build.return_value = MagicMock()
        EmojiCtor.builder.return_value = emoji_builder
        await ch.on_consume_start(_make_native())  # must not raise
    ch._client.im.v1.message_reaction.acreate.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_start_no_client_is_noop() -> None:
    """No lark client -> _add_reaction early-returns, no raise."""
    ch = _channel(streaming=True)
    ch._client = None
    await ch.on_consume_start(_make_native())  # must not raise


# ---------------------------------------------------------------------
# on_consume_end: DONE reaction on the user's incoming message
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_end_sends_done_reaction_to_user_message() -> None:
    """on_consume_end adds a DONE reaction to the user's feishu_message_id,
    not to the reply card's message_id."""
    ch = _channel(streaming=True)
    add_reaction = AsyncMock()
    with patch.object(ch, "_add_reaction", add_reaction):
        await ch.on_consume_end(
            _make_native(), {"message_id": "om_card_msg"}, "completed"
        )
    add_reaction.assert_awaited_once_with("om_msg_1", "DONE")


@pytest.mark.asyncio
async def test_consume_end_no_feishu_message_id_is_noop() -> None:
    """No feishu_message_id in meta -> no reaction call."""
    ch = _channel(streaming=True)
    add_reaction = AsyncMock()
    with patch.object(ch, "_add_reaction", add_reaction):
        native = NativePayload(
            channel_id="feishu",
            sender_id="ou_sender",
            content_parts=[TextContent(text="hi")],
            meta={},
        )
        await ch.on_consume_end(native, {"message_id": "om_card"}, "completed")
    add_reaction.assert_not_awaited()


# ---------------------------------------------------------------------
# send_segment: BizTrace segment → interactive card
# ---------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from qwenpaw_data.host.core.channels.feishu.cards import (  # noqa: E402
    build_segment_card,
)


def _seg(
    *,
    title: str = "",
    input: str = "",
    behavior: str = "",
    conclusion: str = "",
    started_at: float | None = None,
    ended_at: float | None = None,
    artifact: list[Any] | None = None,
) -> Any:
    return SimpleNamespace(
        title=title,
        input=input or None,
        behavior=behavior,
        conclusion=conclusion,
        started_at=started_at,
        ended_at=ended_at,
        artifact=artifact,
    )


def test_build_segment_card_full() -> None:
    """Full segment → header with title+duration, 3 markdown blocks, 2 dividers."""
    card = build_segment_card(
        _seg(
            title="明确指标口径并生成分析计划",
            input="全局 query：分析GAAP",
            behavior="调用 search_context",
            conclusion="成功锁定指标",
            started_at=1000.0,
            ended_at=1187.0,  # 187s = 3分7秒
        )
    )
    assert card is not None
    header = card["header"]["title"]["content"]
    assert "明确指标口径并生成分析计划" in header
    assert "用时 3分7秒" in header
    # 3 markdown sections + 2 hr dividers = 5 elements
    md = [e for e in card["elements"] if e.get("tag") == "markdown"]
    hrs = [e for e in card["elements"] if e.get("tag") == "hr"]
    assert len(md) == 3
    assert len(hrs) == 2
    joined = "\n".join(e["content"] for e in md)
    assert "📥 输入" in joined and "全局 query" in joined
    assert "⚙️ 执行" in joined and "search_context" in joined
    assert "✅ 结论" in joined and "成功锁定指标" in joined
    assert card["header"]["template"] == "blue"


def test_build_segment_card_omits_empty_sections() -> None:
    """Only title + conclusion → single markdown block, no divider."""
    card = build_segment_card(_seg(title="收尾", conclusion="完成"))
    assert card is not None
    md = [e for e in card["elements"] if e.get("tag") == "markdown"]
    hrs = [e for e in card["elements"] if e.get("tag") == "hr"]
    assert len(md) == 1
    assert len(hrs) == 0
    assert "✅ 结论" in md[0]["content"] and "完成" in md[0]["content"]
    assert "输入" not in md[0]["content"]
    assert "执行" not in md[0]["content"]


def test_build_segment_card_no_duration() -> None:
    """Missing started_at/ended_at → header is just the title."""
    card = build_segment_card(_seg(title="某步骤", conclusion="ok"))
    assert card is not None
    assert card["header"]["title"]["content"] == "某步骤"


def test_build_segment_card_empty_returns_none() -> None:
    """No title and no sections → None (caller falls back / skips)."""
    assert build_segment_card(_seg()) is None


# ---------------------------------------------------------------------
# send_followups: 「继续追问」button card
# ---------------------------------------------------------------------


def _followup_action_data(question: str, **value_extra: Any) -> Any:
    """A lark ``card.action.trigger`` payload whose action.value is followup_select."""
    action = MagicMock()
    action.value = {
        "type": "followup_select",
        "question": question,
        "receive_id": "oc_chat_1",
        "receive_id_type": "chat_id",
        "chat_id": "oc_chat_1",
        "sender_id": "ou_sender",
        **value_extra,
    }
    event = MagicMock()
    event.action = action
    data = MagicMock()
    data.event = event
    return data


@pytest.mark.asyncio
async def test_send_followups_builds_interactive_card() -> None:
    """Questions become one button each; value carries routing + full question."""
    ch = _channel(streaming=True)
    send = AsyncMock(return_value="om_followup")
    with patch.object(ch, "_send_interactive", send):
        await ch.send_followups(_make_native(), ["问题一追问", "问题二追问"])
    assert send.await_count == 1
    _, _, payload = send.call_args.args
    card = json.loads(payload)
    assert card["header"]["title"]["content"] == "继续追问"
    actions = card["elements"][-1]["actions"]
    assert [a["text"]["content"] for a in actions] == ["问题一追问", "问题二追问"]
    assert all(a["value"]["type"] == "followup_select" for a in actions)
    assert actions[0]["value"]["question"] == "问题一追问"
    assert actions[0]["value"]["chat_id"] == "oc_chat_1"
    assert actions[0]["value"]["receive_id_type"] == "chat_id"


@pytest.mark.asyncio
async def test_send_followups_trims_and_drops_blank() -> None:
    """Whitespace-only questions are dropped before building the card."""
    ch = _channel(streaming=True)
    send = AsyncMock(return_value="om_followup")
    with patch.object(ch, "_send_interactive", send):
        await ch.send_followups(_make_native(), ["  有效问题  ", "   ", ""])
    _, _, payload = send.call_args.args
    card = json.loads(payload)
    actions = card["elements"][-1]["actions"]
    assert len(actions) == 1
    # trimmed into the button value
    assert actions[0]["value"]["question"] == "有效问题"


@pytest.mark.asyncio
async def test_send_followups_empty_is_noop() -> None:
    """Empty question list → no card sent."""
    ch = _channel(streaming=True)
    send = AsyncMock()
    with patch.object(ch, "_send_interactive", send):
        await ch.send_followups(_make_native(), [])
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_followups_no_client_is_noop() -> None:
    ch = _channel(streaming=True)
    ch._client = None
    send = AsyncMock()
    with patch.object(ch, "_send_interactive", send):
        await ch.send_followups(_make_native(), ["问题"])
    send.assert_not_awaited()


# ---------------------------------------------------------------------
# _on_followup_select: follow-up button click (echo + enqueue)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_followup_select_echoes_and_enqueues() -> None:
    """Clicking a follow-up button echoes it and enqueues a normal inbound message."""
    ch = _channel(streaming=True)
    received: list[NativePayload] = []
    ch._enqueue = lambda native: received.append(native)  # type: ignore[method-assign]
    sent: list[str] = []
    with patch.object(
        ch, "send", new=AsyncMock(side_effect=lambda _native, text: sent.append(text))
    ):
        value = _followup_action_data("performance yoy").event.action.value
        await ch._on_followup_select(value)
    assert sent == ["💬 performance yoy"]
    assert len(received) == 1
    native = received[0]
    assert native.sender_id == "ou_sender"
    assert native.meta["chat_id"] == "oc_chat_1"
    assert native.meta["receive_id_type"] == "chat_id"
    assert native.meta["receive_id"] == "oc_chat_1"
    assert "".join(p.text for p in native.content_parts) == "performance yoy"


@pytest.mark.asyncio
async def test_on_followup_select_blank_question_is_noop() -> None:
    """Blank question in value → nothing echoed, nothing enqueued."""
    ch = _channel(streaming=True)
    received: list[NativePayload] = []
    ch._enqueue = lambda native: received.append(native)  # type: ignore[method-assign]
    sent: list[str] = []
    with patch.object(
        ch, "send", new=AsyncMock(side_effect=lambda _native, text: sent.append(text))
    ):
        value = _followup_action_data("   ").event.action.value
        await ch._on_followup_select(value)
    assert sent == []
    assert received == []


# ---------------------------------------------------------------------
# send_datasource_card + _on_datasource_select_sync/_on_datasource_select
# ---------------------------------------------------------------------


def _ds_items() -> list[Any]:
    return [
        SimpleNamespace(datasource_id="mysql-1", datasource_name="MySQL"),
        SimpleNamespace(datasource_id="pg-2", datasource_name="Postgres"),
    ]


def _ds_action_data(
    card_id: str,
    ds_id: str,
    ds_name: str = "MySQL",
    session_id: str = "ses_1",
    **value_extra: Any,
) -> Any:
    """A lark ``card.action.trigger`` payload whose action.value is datasource_select."""
    action = MagicMock()
    action.value = {
        "type": "datasource_select",
        "card_id": card_id,
        "ds_id": ds_id,
        "ds_name": ds_name,
        "session_id": session_id,
        "receive_id": "oc_chat_1",
        "receive_id_type": "chat_id",
        "chat_id": "oc_chat_1",
        "sender_id": "ou_sender",
        **value_extra,
    }
    event = MagicMock()
    event.action = action
    event.context = MagicMock(open_message_id="om_card_1")
    data = MagicMock()
    data.event = event
    return data


@pytest.mark.asyncio
async def test_send_datasource_card_selectable_builds_initial_card() -> None:
    """Unbound → blue header, all buttons default + clickable, value carries card_id."""
    ch = _channel(streaming=True)
    session = SimpleNamespace(id="ses_1", datasource_id=None)
    send = AsyncMock(return_value="om_ds")
    with patch.object(ch, "_send_interactive", send):
        await ch.send_datasource_card(
            _make_native(), session, _ds_items(), selectable=True
        )
    assert send.await_count == 1
    _, _, payload = send.call_args.args
    card = json.loads(payload)
    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "选择数据源"
    actions = card["elements"][-1]["actions"]
    assert [a["text"]["content"] for a in actions] == ["MySQL", "Postgres"]
    assert all(a["type"] == "default" for a in actions)
    assert not any("disabled" in a for a in actions)
    assert actions[0]["value"]["type"] == "datasource_select"
    assert actions[0]["value"]["card_id"]
    # items cached for the click-time freeze rebuild
    assert list(ch._ds_cards.values()) == [[("mysql-1", "MySQL"), ("pg-2", "Postgres")]]


@pytest.mark.asyncio
async def test_send_datasource_card_view_only_disables_all_and_highlights_bound() -> (
    None
):
    """Bound session → view-only: green header, all disabled, bound button ✓+primary."""
    ch = _channel(streaming=True)
    session = SimpleNamespace(id="ses_1", datasource_id="pg-2")
    send = AsyncMock(return_value="om_ds")
    with patch.object(ch, "_send_interactive", send):
        await ch.send_datasource_card(
            _make_native(), session, _ds_items(), selectable=False
        )
    _, _, payload = send.call_args.args
    card = json.loads(payload)
    assert card["header"]["template"] == "green"
    actions = card["elements"][-1]["actions"]
    assert all(a.get("disabled") for a in actions)
    bound = [a for a in actions if a["value"]["ds_id"] == "pg-2"][0]
    assert bound["type"] == "primary"
    assert bound["text"]["content"] == "✓ Postgres"


@pytest.mark.asyncio
async def test_datasource_select_sync_freezes_green_and_schedules() -> None:
    """Click → optimistic freeze: green header, clicked primary+✓, all disabled;
    items popped from the cache; the async bind is scheduled on the main loop."""
    ch = _channel(streaming=True)
    ch._main_loop = MagicMock()
    ch._main_loop.is_closed.return_value = False
    ch._ds_cards["cid"] = [("mysql-1", "MySQL"), ("pg-2", "Postgres")]
    data = _ds_action_data("cid", "mysql-1", "MySQL")
    with patch(
        "qwenpaw_data.host.core.channels.feishu.channel.asyncio.run_coroutine_threadsafe"
    ) as sched:
        resp = ch._on_datasource_select_sync(data, data.event.action.value)
    assert "cid" not in ch._ds_cards  # consumed
    card = resp.card.data
    assert card["header"]["template"] == "green"
    actions = card["elements"][-1]["actions"]
    assert [a["text"]["content"] for a in actions] == ["✓ MySQL", "Postgres"]
    assert all(a.get("disabled") for a in actions)
    assert actions[0]["type"] == "primary"
    assert resp.toast.content == "正在绑定…"
    assert sched.call_count == 1
    # the scheduled coroutine is the async bind (cr_name carries the func name)
    coro = sched.call_args.args[0]
    assert coro.cr_frame.f_code.co_name == "_on_datasource_select"
    coro.close()  # mocked scheduler never awaited it; close to suppress GC warning


@pytest.mark.asyncio
async def test_on_datasource_select_success_binds_and_sends() -> None:
    """Async bind on an unbound session: bind_datasource + commit + success text."""
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
    from qwenpaw_data.host.core.repositories import SessionRepository

    identity = Identity(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID
    )
    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as db:
            session = Session.create(identity=identity, title="t", channel="feishu")
            await SessionRepository(db, identity).add(session)
            await db.commit()
            sid = session.id
        ch = _channel(streaming=True)
        ch._owner_identity = identity
        sent: list[str] = []
        with patch.object(
            ch, "send", new=AsyncMock(side_effect=lambda _n, text: sent.append(text))
        ):
            await ch._on_datasource_select(
                {
                    "type": "datasource_select",
                    "card_id": "cid",
                    "ds_id": "mysql-1",
                    "ds_name": "MySQL",
                    "session_id": sid,
                    "receive_id": "oc_chat_1",
                    "receive_id_type": "chat_id",
                    "chat_id": "oc_chat_1",
                    "sender_id": "ou_sender",
                },
                "om_card_1",
                [("mysql-1", "MySQL"), ("pg-2", "Postgres")],
            )
        assert sent == ["已绑定 MySQL，现在可以提问了"]
        async with get_session_factory()() as db:
            assert (
                await SessionRepository(db, identity).get(sid)
            ).datasource_id == "mysql-1"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_on_datasource_select_already_bound_different_corrects_card() -> None:
    """Session bound elsewhere to a different ds → correct the wrong green + reject text."""
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
    from qwenpaw_data.host.core.repositories import SessionRepository

    identity = Identity(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID
    )
    await reset_engine()
    await init_db()
    try:
        async with get_session_factory()() as db:
            session = Session.create(
                identity=identity,
                title="t",
                channel="feishu",
                datasource_id="pg-2",
            )
            await SessionRepository(db, identity).add(session)
            await db.commit()
            sid = session.id
        ch = _channel(streaming=True)
        ch._owner_identity = identity
        sent: list[str] = []
        updates: list[Any] = []
        with (
            patch.object(
                ch,
                "send",
                new=AsyncMock(side_effect=lambda _n, text: sent.append(text)),
            ),
            patch.object(
                ch,
                "_update_interactive_message",
                new=AsyncMock(side_effect=lambda mid, card: updates.append(card)),
            ),
        ):
            await ch._on_datasource_select(
                {
                    "type": "datasource_select",
                    "card_id": "cid",
                    "ds_id": "mysql-1",
                    "ds_name": "MySQL",  # user clicked A
                    "session_id": sid,
                    "receive_id": "oc_chat_1",
                    "receive_id_type": "chat_id",
                    "chat_id": "oc_chat_1",
                    "sender_id": "ou_sender",
                },
                "om_card_1",
                [("mysql-1", "MySQL"), ("pg-2", "Postgres")],
            )
        assert sent == ["已绑定数据源，不可更改"]
        assert len(updates) == 1
        actions = updates[0]["elements"][-1]["actions"]
        # real bound ds (pg-2/Postgres) is highlighted, not the clicked one
        assert [a["text"]["content"] for a in actions] == ["MySQL", "✓ Postgres"]
        assert [a["type"] for a in actions] == ["default", "primary"]
        assert updates[0]["header"]["template"] == "green"
    finally:
        await reset_engine()


def test_build_segment_card_includes_artifacts_block() -> None:
    """A segment with artifacts renders a 4th 关键产物 block (name + caption)."""
    card = build_segment_card(
        _seg(
            title="绘制GAAP趋势并识别异常",
            conclusion="生成趋势图",
            artifact=[
                SimpleNamespace(name="gaap_trend_march_2026.png", description="趋势图"),
                SimpleNamespace(name="anomaly.csv", description=""),
            ],
        )
    )
    assert card is not None
    md = [e for e in card["elements"] if e.get("tag") == "markdown"]
    joined = "\n".join(e["content"] for e in md)
    assert "📎 关键产物" in joined
    assert "gaap_trend_march_2026.png" in joined and "趋势图" in joined
    # artifact with empty description → name only, no dangling " — "
    assert "anomaly.csv" in joined
    assert "anomaly.csv — " not in joined


def test_build_segment_card_no_artifacts_omits_block() -> None:
    """No artifacts → no 关键产物 block (only the 3 base sections present)."""
    card = build_segment_card(_seg(title="步骤", conclusion="done"))
    assert card is not None
    joined = "\n".join(
        e["content"] for e in card["elements"] if e.get("tag") == "markdown"
    )
    assert "关键产物" not in joined


@pytest.mark.asyncio
async def test_send_segment_sends_interactive_card() -> None:
    """With a client, send_segment sends a card via _send_interactive."""
    ch = _channel(streaming=True)
    send_interactive = AsyncMock(return_value="om_card")
    with patch.object(ch, "_send_interactive", send_interactive):
        await ch.send_segment(_make_native(), _seg(title="步骤一", conclusion="done"))
    send_interactive.assert_awaited_once()
    rid_type, rid, card_json = send_interactive.call_args.args
    assert rid_type == "chat_id" and rid == "oc_chat_1"
    import json as _json

    card = _json.loads(card_json)
    assert card["header"]["title"]["content"] == "步骤一"
    assert any(e.get("tag") == "markdown" for e in card["elements"])


@pytest.mark.asyncio
async def test_send_segment_falls_back_when_no_client() -> None:
    """No client → fall back to base text rendering via send()."""
    ch = _channel(streaming=True)
    ch._client = None
    sent: list[str] = []
    with patch.object(ch, "send", AsyncMock(side_effect=lambda n, t: sent.append(t))):
        await ch.send_segment(
            _make_native(),
            _seg(title="步骤二", conclusion="ok", started_at=1.0, ended_at=2.0),
        )
    assert sent and "步骤二" in sent[0] and "ok" in sent[0]

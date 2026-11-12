# -*- coding: utf-8 -*-
"""DingTalk channel: streaming hooks + artifact delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from qwenpaw_data.host.core.channels.dingtalk.channel import DingTalkChannel
from qwenpaw_data.host.core.channels.clarification_question import (
    ClarificationQuestion,
    ClarificationQuestionAnswer,
    ClarificationQuestionGroup,
)
from qwenpaw_data.host.core.channels.schema import NativePayload, TextContent


def _make_native(text: str = "") -> NativePayload:
    """A native payload mimicking dingtalk _on_message's meta (incl. incoming_message)."""
    incoming = MagicMock()
    incoming.conversation_type = "1"
    incoming.sender_staff_id = "staff_x"
    incoming.conversation_id = "conv_y"
    return NativePayload(
        channel_id="dingtalk",
        sender_id="staff_x",
        content_parts=[TextContent(text=text)],
        meta={"incoming_message": incoming},
    )


def _channel(streaming: bool = True) -> DingTalkChannel:
    ch = DingTalkChannel()
    ch.streaming_enabled = streaming
    # _handler.dingtalk_client is set by the SDK at register time; fake it here.
    ch._handler = MagicMock()
    ch._handler.dingtalk_client = MagicMock()
    ch._client_id = "cli_123"
    return ch


# ---------------------------------------------------------------------
# on_streaming_start
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_start_disabled_returns_none() -> None:
    """streaming_enabled=False -> on_streaming_start returns None (base falls back)."""
    ch = _channel(streaming=False)
    assert await ch.on_streaming_start(_make_native(), "msg_1") is None


@pytest.mark.asyncio
async def test_streaming_start_no_incoming_returns_none() -> None:
    """No incoming_message in meta -> None."""
    ch = _channel(streaming=True)
    native = NativePayload(
        channel_id="dingtalk",
        sender_id="s",
        content_parts=[],
        meta={},
    )
    assert await ch.on_streaming_start(native, "msg_1") is None


@pytest.mark.asyncio
async def test_streaming_start_no_handler_returns_none() -> None:
    ch = _channel(streaming=True)
    ch._handler = None
    assert await ch.on_streaming_start(_make_native(), "msg_1") is None


@pytest.mark.asyncio
async def test_streaming_start_creates_card_and_returns_handle() -> None:
    """ai_start sets card_instance_id -> handle returned with the instance."""
    ch = _channel(streaming=True)
    fake_instance = MagicMock()
    fake_instance.card_instance_id = "track_abc"
    with patch(
        "dingtalk_stream.card_instance.AIMarkdownCardInstance",
        return_value=fake_instance,
    ) as ctor:
        # ai_start must run in threadpool; patch the instance method to a sync stub.
        fake_instance.ai_start = MagicMock()
        handle = await ch.on_streaming_start(_make_native(), "msg_1")
    assert handle is not None
    assert handle["instance"] is fake_instance
    assert handle["full_text"] == ""
    ctor.assert_called_once()
    fake_instance.ai_start.assert_called_once()


@pytest.mark.asyncio
async def test_streaming_start_ai_start_failure_returns_none() -> None:
    """ai_start leaves card_instance_id empty -> None (no card to stream into)."""
    ch = _channel(streaming=True)
    fake_instance = MagicMock()
    fake_instance.card_instance_id = ""
    fake_instance.ai_start = MagicMock()
    with patch(
        "dingtalk_stream.card_instance.AIMarkdownCardInstance",
        return_value=fake_instance,
    ):
        assert await ch.on_streaming_start(_make_native(), "msg_1") is None


# ---------------------------------------------------------------------
# on_streaming_delta (throttle)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_delta_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two deltas within the throttle window -> ai_streaming called once, but
    both deltas are accumulated (throttle gates the push, not the buffer)."""
    monkeypatch.setattr(
        "qwenpaw_data.host.core.channels.dingtalk.channel.DINGTALK_STREAM_MIN_INTERVAL_S",
        100.0,
    )
    ch = _channel(streaming=True)
    instance = MagicMock()
    instance.ai_streaming = MagicMock()
    handle: dict[str, Any] = {
        "instance": instance,
        "last_update": 0.0,
        "full_text": "",
    }
    await ch.on_streaming_delta(_make_native(), handle, "hello")
    await ch.on_streaming_delta(_make_native(), handle, " world")
    instance.ai_streaming.assert_called_once()
    assert handle["full_text"] == "hello world"


@pytest.mark.asyncio
async def test_streaming_delta_accumulates_full_text() -> None:
    """ai_streaming receives the accumulated full text (append=False)."""
    ch = _channel(streaming=True)
    instance = MagicMock()
    instance.ai_streaming = MagicMock()
    handle: dict[str, Any] = {
        "instance": instance,
        "last_update": 0.0,
        "full_text": "pre",
    }
    # last_update=0 -> first call passes the throttle (monotonic() > 0).
    await ch.on_streaming_delta(_make_native(), handle, " appended")
    instance.ai_streaming.assert_called_once_with(markdown="pre appended", append=False)


# ---------------------------------------------------------------------
# on_streaming_reasoning_delta
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_delta_renders_thinking_prefix() -> None:
    """Reasoning accumulated text is rendered with a 💭 prefix, full-replace."""
    ch = _channel(streaming=True)
    instance = MagicMock()
    instance.ai_streaming = MagicMock()
    handle: dict[str, Any] = {
        "instance": instance,
        "last_update": 0.0,
        "full_text": "",
    }
    await ch.on_streaming_reasoning_delta(_make_native(), handle, "analyzing")
    instance.ai_streaming.assert_called_once_with(markdown="💭 analyzing", append=False)


@pytest.mark.asyncio
async def test_reasoning_delta_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reasoning shares the delta throttle budget (last_update)."""
    monkeypatch.setattr(
        "qwenpaw_data.host.core.channels.dingtalk.channel.DINGTALK_STREAM_MIN_INTERVAL_S",
        100.0,
    )
    ch = _channel(streaming=True)
    instance = MagicMock()
    instance.ai_streaming = MagicMock()
    handle: dict[str, Any] = {
        "instance": instance,
        "last_update": 0.0,
        "full_text": "",
    }
    await ch.on_streaming_reasoning_delta(_make_native(), handle, "first")
    await ch.on_streaming_reasoning_delta(_make_native(), handle, " second")
    instance.ai_streaming.assert_called_once_with(markdown="💭 first", append=False)


# ---------------------------------------------------------------------
# on_streaming_start placeholder (pre-create path)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_start_precreate_writes_placeholder() -> None:
    """Pre-create path (msg_id="") writes the 💭 思考中… placeholder."""
    ch = _channel(streaming=True)
    fake_instance = MagicMock()
    fake_instance.card_instance_id = "track_pre"
    with patch(
        "dingtalk_stream.card_instance.AIMarkdownCardInstance",
        return_value=fake_instance,
    ):
        handle = await ch.on_streaming_start(_make_native(), "")
    assert handle is not None
    fake_instance.ai_streaming.assert_called_once_with(
        markdown="💭 thinking...", append=False
    )


@pytest.mark.asyncio
async def test_streaming_start_with_msg_id_skips_placeholder() -> None:
    """Normal path (non-empty msg_id) does not write the placeholder."""
    ch = _channel(streaming=True)
    fake_instance = MagicMock()
    fake_instance.card_instance_id = "track_msg"
    with patch(
        "dingtalk_stream.card_instance.AIMarkdownCardInstance",
        return_value=fake_instance,
    ):
        handle = await ch.on_streaming_start(_make_native(), "msg_1")
    assert handle is not None
    fake_instance.ai_streaming.assert_not_called()


# ---------------------------------------------------------------------
# on_streaming_end
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_end_finalizes_card() -> None:
    ch = _channel(streaming=True)
    instance = MagicMock()
    instance.ai_finish = MagicMock()
    handle = {"instance": instance, "last_update": 0.0, "full_text": "done"}
    await ch.on_streaming_end(_make_native(), handle, "done")
    instance.ai_finish.assert_called_once_with(markdown="done")


@pytest.mark.asyncio
async def test_streaming_end_degrades_local_path_image() -> None:
    """A local-path ![alt](/local/path) in the answer is replaced with an
    inline-code path before ai_finish — DingTalk can't render a local path
    (it wants a media_id); replacing (not removing) keeps it visible."""
    ch = _channel(streaming=True)
    instance = MagicMock()
    instance.ai_finish = MagicMock()
    handle = {"instance": instance, "last_update": 0.0, "full_text": ""}
    text = "see chart:\n![GAAP](/home/user/.qwenpaw-data/ses_x/gaap_trend)\nend"
    await ch.on_streaming_end(_make_native(), handle, text)
    instance.ai_finish.assert_called_once_with(
        markdown="see chart:\n`(/home/user/.qwenpaw-data/ses_x/gaap_trend)`\nend"
    )


@pytest.mark.asyncio
async def test_streaming_close_shows_segment_done_banner() -> None:
    """Segment boundary overwrites the reasoning caption and finalizes.

    ai_streaming pushes "✅ Segment Done!" into the *displayed* streaming
    buffer (ai_finish alone only flips flowStatus and leaves the last
    reasoning caption on screen); ai_finish then ends the INPUTING state so
    the banner carries no typing spinner. Both calls receive the banner text.
    """
    ch = _channel(streaming=True)
    instance = MagicMock()
    instance.ai_streaming = MagicMock()
    instance.ai_finish = MagicMock()
    handle = {"instance": instance, "last_update": 0.0, "full_text": "exploring"}
    await ch.on_streaming_close(_make_native(), handle, "some summary")
    instance.ai_streaming.assert_called_once_with(
        markdown="✅ Segment Done!", append=False
    )
    instance.ai_finish.assert_called_once_with(markdown="✅ Segment Done!")


@pytest.mark.asyncio
async def test_streaming_round_reset_clears_full_text() -> None:
    """New agent round: the prior round's accumulated answer is dropped so the
    next round's deltas don't reprint it as a prefix (ai_streaming is
    full-replace)."""
    ch = _channel(streaming=True)
    handle = {
        "instance": MagicMock(),
        "last_update": 0.0,
        "full_text": "上一轮的答案会在这里被反复打印",
    }
    await ch.on_streaming_round_reset(_make_native(), handle)
    assert handle["full_text"] == ""


@pytest.mark.asyncio
async def test_streaming_end_fallback_sends_full_text() -> None:
    """No handle (card creation failed) -> send the full text directly."""
    ch = _channel(streaming=True)
    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    await ch.on_streaming_end(_make_native(), None, "the full answer")
    assert sent == ["the full answer"]


@pytest.mark.asyncio
async def test_streaming_end_fallback_empty_text_sends_nothing() -> None:
    ch = _channel(streaming=True)
    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    await ch.on_streaming_end(_make_native(), None, "")
    assert sent == []


# ---------------------------------------------------------------------
# send_image / send_file
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_image_no_handler_silent_noop(tmp_path: Path) -> None:
    """No handler -> no upload, no fallback text (artifact silently dropped)."""
    ch = _channel(streaming=True)
    ch._handler = None
    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    await ch.send_image(_make_native(), str(p))
    assert sent == []


@pytest.mark.asyncio
async def test_send_image_upload_failure_sends_fallback(tmp_path: Path) -> None:
    """Upload returns no media_id -> fallback text notice."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.upload_to_dingtalk = MagicMock(return_value=None)
    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    await ch.send_image(_make_native(), str(p))
    assert sent and sent[0].startswith("⚠️ 图片发送失败")


@pytest.mark.asyncio
async def test_send_file_upload_failure_sends_fallback(tmp_path: Path) -> None:
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.upload_to_dingtalk = MagicMock(return_value=None)
    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    p = tmp_path / "a.csv"
    p.write_bytes(b"x")
    await ch.send_file(_make_native(), str(p))
    assert sent and sent[0].startswith("⚠️ 文件发送失败")


@pytest.mark.asyncio
async def test_send_image_inline_via_webhook(tmp_path: Path) -> None:
    """With a sessionWebhook, the image is sent inline via markdown ![](media_id)."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.upload_to_dingtalk = MagicMock(return_value="media_1")
    p = tmp_path / "a.png"
    p.write_bytes(b"img")
    with (
        patch.object(ch, "_post_inline_image", return_value=True) as inline,
        patch.object(ch, "_send_media_message") as send_media,
    ):
        await ch.send_image(_make_native(), str(p))
    inline.assert_called_once()
    webhook, name, media_id = inline.call_args.args
    assert name == "a.png" and media_id == "media_1"
    # Inline succeeded -> no file-message fallback.
    send_media.assert_not_called()


@pytest.mark.asyncio
async def test_send_image_falls_back_to_file_when_no_webhook(tmp_path: Path) -> None:
    """No sessionWebhook -> fall back to a sampleFile message (download to view)."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.upload_to_dingtalk = MagicMock(return_value="media_1")
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    native = _make_native()
    native.meta["incoming_message"].session_webhook = None  # no webhook
    p = tmp_path / "a.png"
    p.write_bytes(b"img")
    with patch.object(ch, "_send_media_message") as send_media:
        await ch.send_image(native, str(p))
    send_media.assert_called_once()
    args, kwargs = send_media.call_args
    assert args[2] == "sampleFile"
    assert args[3] == {"mediaId": "media_1", "fileName": "a.png", "fileType": "png"}


@pytest.mark.asyncio
async def test_send_image_falls_back_when_webhook_fails(tmp_path: Path) -> None:
    """sessionWebhook present but inline post fails -> sampleFile fallback."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.upload_to_dingtalk = MagicMock(return_value="media_1")
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    p = tmp_path / "a.png"
    p.write_bytes(b"img")
    with (
        patch.object(ch, "_post_inline_image", return_value=False),
        patch.object(ch, "_send_media_message") as send_media,
    ):
        await ch.send_image(_make_native(), str(p))
    send_media.assert_called_once()
    assert send_media.call_args.args[2] == "sampleFile"


def test_post_inline_image_success() -> None:
    """A 200 + errcode=0 response -> True."""
    ch = _channel(streaming=True)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
    with patch(
        "qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post", return_value=resp
    ) as post:
        assert ch._post_inline_image("https://wh", "a.png", "media_1") is True
    payload = post.call_args.kwargs["json"]
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["text"] == "![a.png](media_1)"


def test_post_inline_image_errcode_returns_false() -> None:
    """A 200 but errcode!=0 -> False (caller falls back)."""
    ch = _channel(streaming=True)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"errcode": 400105, "errmsg": "unsupported"}
    with patch(
        "qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post", return_value=resp
    ):
        assert ch._post_inline_image("https://wh", "a.png", "media_1") is False


def test_post_inline_image_http_error_returns_false() -> None:
    """A non-2xx status -> False."""
    ch = _channel(streaming=True)
    resp = MagicMock(status_code=500, text="boom")
    resp.json.return_value = {}
    with patch(
        "qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post", return_value=resp
    ):
        assert ch._post_inline_image("https://wh", "a.png", "media_1") is False


@pytest.mark.asyncio
async def test_send_file_success_calls_send_media(tmp_path: Path) -> None:
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.upload_to_dingtalk = MagicMock(return_value="media_2")
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    p = tmp_path / "a.xlsx"
    p.write_bytes(b"data")
    with patch.object(ch, "_send_media_message") as send_media:
        await ch.send_file(_make_native(), str(p))
    args, kwargs = send_media.call_args
    assert args[2] == "sampleFile"
    assert args[3] == {"mediaId": "media_2", "fileName": "a.xlsx", "fileType": "xlsx"}


# ---------------------------------------------------------------------
# send_file: HTML special-cased to OSS download link (DingTalk bans .html)
# ---------------------------------------------------------------------


class _FakeOssStore:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, bytes]] = []

    def upload_file(self, oss_path: str, file_bytes: bytes) -> None:
        self.uploaded.append((oss_path, file_bytes))

    def create_signed_download_url(
        self, oss_path: str, *, expires_in: int, download_filename: str
    ) -> str:
        return (
            f"https://example.invalid/{oss_path}"
            f"?expires={expires_in}&name={download_filename}"
        )


@pytest.mark.asyncio
async def test_send_file_html_uploads_oss_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTML → upload to OSS, reply with a markdown download link (never a file upload)."""
    from qwenpaw_data.host.core.settings import clear_settings

    monkeypatch.setenv("QWENPAW_DATA_OSS_ACCESS_KEY_ID", "test-id")
    monkeypatch.setenv("QWENPAW_DATA_OSS_ACCESS_KEY_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_OSS_BUCKET", "qwenpaw-data")
    clear_settings()

    ch = _channel(streaming=True)
    store = _FakeOssStore()
    monkeypatch.setattr(
        "qwenpaw_data.host.core.integrations.oss.get_oss_store", lambda: store
    )
    # _deliver_media must NOT run for .html (would hit DingTalk's ban).
    deliver_media = MagicMock(return_value=None)
    monkeypatch.setattr(ch, "_deliver_media", deliver_media)

    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    p = tmp_path / "report.html"
    p.write_bytes(b"<html>")

    await ch.send_file(_make_native(), str(p))

    assert deliver_media.call_count == 0
    assert len(store.uploaded) == 1
    oss_path, data = store.uploaded[0]
    assert data == b"<html>"
    # OSS key scoped by external session id + filename.
    assert oss_path.endswith("/report.html")
    assert sent and sent[0].startswith("📄 [report.html](")
    assert "https://example.invalid/" in sent[0]


@pytest.mark.asyncio
async def test_send_file_html_no_oss_sends_webui_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTML with OSS unconfigured → webui redirect notice, no upload attempted."""
    from qwenpaw_data.host.core.settings import clear_settings

    monkeypatch.delenv("QWENPAW_DATA_OSS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_OSS_ACCESS_KEY_SECRET", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_OSS_BUCKET", raising=False)
    clear_settings()

    ch = _channel(streaming=True)
    deliver_media = MagicMock(return_value=None)
    monkeypatch.setattr(ch, "_deliver_media", deliver_media)

    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    p = tmp_path / "report.html"
    p.write_bytes(b"<html>")

    await ch.send_file(_make_native(), str(p))

    assert deliver_media.call_count == 0
    assert sent and "WebUI" in sent[0] and "report.html" in sent[0]


@pytest.mark.asyncio
async def test_send_file_non_html_unchanged(tmp_path: Path) -> None:
    """Non-HTML files keep the DingTalk file-upload path (no security ban)."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.upload_to_dingtalk = MagicMock(return_value="media_3")
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    p = tmp_path / "notes.txt"
    p.write_bytes(b"hi")
    with patch.object(ch, "_send_media_message") as send_media:
        await ch.send_file(_make_native(), str(p))
    send_media.assert_called_once()


# ---------------------------------------------------------------------
# _send_media_message: single vs group chat
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# send_segment: BizTrace segment → titled markdown card
# ---------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from qwenpaw_data.host.core.channels.dingtalk.channel import _build_segment_card_data  # noqa: E402


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


def _card_markdown(card: dict[str, Any]) -> list[str]:
    """All markdown block texts in a card's contents, in order."""
    return [c["text"] for c in card["contents"] if c["type"] == "markdown"]


def _card_divider_count(card: dict[str, Any]) -> int:
    return sum(1 for c in card["contents"] if c["type"] == "divider")


def test_build_segment_card_data_full() -> None:
    card = _build_segment_card_data(
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
    assert "明确指标口径并生成分析计划" in card["header"]["title"]["text"]
    assert "用时 3分7秒" in card["header"]["title"]["text"]
    texts = _card_markdown(card)
    assert any("📥 输入" in t and "全局 query" in t for t in texts)
    assert any("⚙️ 执行" in t and "search_context" in t for t in texts)
    assert any("✅ 结论" in t and "成功锁定指标" in t for t in texts)
    # three sections -> two dividers between them
    assert _card_divider_count(card) == 2
    # bold labels (StandardCard markdown strips <font color>, so labels are
    # bold-only — no dead font tag)
    assert all(t.startswith("**") for t in texts)
    assert not any("<font color=" in t for t in texts)


def test_build_segment_card_data_omits_empty_sections() -> None:
    card = _build_segment_card_data(_seg(title="收尾", conclusion="完成"))
    assert card is not None
    assert card["header"]["title"]["text"] == "收尾"
    texts = _card_markdown(card)
    assert len(texts) == 1
    assert "✅ 结论" in texts[0] and "完成" in texts[0]
    assert _card_divider_count(card) == 0


def test_build_segment_card_data_empty_returns_none() -> None:
    assert _build_segment_card_data(_seg()) is None


def test_build_segment_card_data_includes_artifacts_block() -> None:
    """A segment with artifacts renders a 4th 关键产物 block (name + caption)."""
    from types import SimpleNamespace

    card = _build_segment_card_data(
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
    texts = _card_markdown(card)
    art = [t for t in texts if "关键产物" in t]
    assert art and "gaap_trend_march_2026.png" in art[0] and "趋势图" in art[0]
    assert "anomaly.csv" in art[0]
    assert "anomaly.csv — " not in art[0]  # no caption → no em-dash suffix
    # conclusion + artifacts -> one divider between them
    assert _card_divider_count(card) == 1


def test_build_segment_card_data_no_artifacts_omits_block() -> None:
    """No artifacts → no 关键产物 block."""
    card = _build_segment_card_data(_seg(title="步骤", conclusion="done"))
    assert card is not None
    assert not any("关键产物" in t for t in _card_markdown(card))


@pytest.mark.asyncio
async def test_send_segment_calls_reply_card() -> None:
    """With a handler + incoming message, send_segment replies an interactive card."""
    ch = _channel(streaming=True)
    ch._handler.reply_card = MagicMock(return_value="card_biz_1")
    await ch.send_segment(
        _make_native(),
        _seg(title="步骤一", conclusion="done", started_at=1.0, ended_at=2.0),
    )
    ch._handler.reply_card.assert_called_once()
    card_data, _incoming = ch._handler.reply_card.call_args.args
    assert card_data["header"]["title"]["text"] == "步骤一  用时 1秒"
    assert any("✅ 结论" in t for t in _card_markdown(card_data))


@pytest.mark.asyncio
async def test_send_segment_falls_back_without_handler() -> None:
    """No handler -> fall back to base text rendering via send()."""
    ch = _channel(streaming=True)
    ch._handler = None
    sent: list[str] = []

    async def _send(native: Any, text: str) -> None:
        sent.append(text)

    ch.send = _send  # type: ignore[method-assign]
    await ch.send_segment(_make_native(), _seg(title="步骤二", conclusion="ok"))
    assert sent and "步骤二" in sent[0] and "ok" in sent[0]


# ---------------------------------------------------------------------
# _send_media_message: single vs group chat
# ---------------------------------------------------------------------


def test_send_media_message_single_chat_posts_oto() -> None:
    """conversation_type="1" -> oToMessages/batchSend with userIds."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    incoming = MagicMock()
    incoming.conversation_type = "1"
    incoming.sender_staff_id = "staff_1"
    with patch("qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post") as post:
        post.return_value = MagicMock(status_code=200, text="ok")
        ch._send_media_message(
            ch._handler.dingtalk_client, incoming, "sampleImage", {"photoMediaid": "m"}
        )
    post.assert_called_once()
    url = post.call_args.args[0]
    body = post.call_args.kwargs["json"]
    assert "oToMessages/batchSend" in url
    assert body["userIds"] == ["staff_1"]
    assert body["robotCode"] == "cli_123"


def test_send_media_message_group_chat_posts_group() -> None:
    """conversation_type="2" -> groupMessages/send with openConversationId."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    incoming = MagicMock()
    incoming.conversation_type = "2"
    incoming.conversation_id = "cid_group"
    with patch("qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post") as post:
        post.return_value = MagicMock(status_code=200, text="ok")
        ch._send_media_message(
            ch._handler.dingtalk_client, incoming, "sampleFile", {"mediaId": "m"}
        )
    url = post.call_args.args[0]
    body = post.call_args.kwargs["json"]
    assert "groupMessages/send" in url
    assert body["openConversationId"] == "cid_group"


def test_send_media_message_no_token_is_noop() -> None:
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="")
    with patch("qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post") as post:
        ch._send_media_message(
            ch._handler.dingtalk_client, MagicMock(), "sampleImage", {}
        )
    post.assert_not_called()


# ---------------------------------------------------------------------
# on_consume_start / on_consume_end: emoji reactions
# ---------------------------------------------------------------------


def _emotion_native() -> NativePayload:
    """Native with the meta fields _send_emotion reads (message_id, conversation_id)."""
    return NativePayload(
        channel_id="dingtalk",
        sender_id="staff_x",
        content_parts=[TextContent(text="hi")],
        meta={
            "incoming_message": MagicMock(),
            "message_id": "msg_1",
            "conversation_id": "conv_y",
        },
    )


@pytest.mark.asyncio
async def test_consume_start_sends_thinking_reaction() -> None:
    """on_consume_start posts a 🤔Thinking reply-emotion to the user's message."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    with patch("qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post") as post:
        post.return_value = MagicMock(status_code=200, text="ok")
        await ch.on_consume_start(_emotion_native())
    post.assert_called_once()
    url = post.call_args.args[0]
    body = post.call_args.kwargs["json"]
    assert url.endswith("/robot/emotion/reply")
    assert body["emotionName"] == "🤔Thinking"
    assert body["openMsgId"] == "msg_1"
    assert body["openConversationId"] == "conv_y"


@pytest.mark.asyncio
async def test_consume_end_recalls_thinking_and_adds_done() -> None:
    """on_consume_end recalls 🤔Thinking (recall endpoint) then adds 🥳Done (reply)."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    with patch("qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post") as post:
        post.return_value = MagicMock(status_code=200, text="ok")
        await ch.on_consume_end(
            _emotion_native(), {"instance": MagicMock()}, "completed"
        )
    assert post.call_count == 2
    urls = [c.args[0] for c in post.call_args_list]
    names = [c.kwargs["json"]["emotionName"] for c in post.call_args_list]
    # recall Thinking, then reply Done
    assert urls[0].endswith("/robot/emotion/recall")
    assert urls[1].endswith("/robot/emotion/reply")
    assert names == ["🤔Thinking", "🥳Done"]


@pytest.mark.asyncio
async def test_consume_start_no_message_id_is_noop() -> None:
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    native = NativePayload(
        channel_id="dingtalk",
        sender_id="s",
        content_parts=[],
        meta={"incoming_message": MagicMock()},  # no message_id / conversation_id
    )
    with patch("qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post") as post:
        await ch.on_consume_start(native)
    post.assert_not_called()


@pytest.mark.asyncio
async def test_send_emotion_failure_is_swallowed() -> None:
    """A non-2xx response must not raise (best-effort)."""
    ch = _channel(streaming=True)
    ch._handler.dingtalk_client.get_access_token = MagicMock(return_value="tok")
    with patch("qwenpaw_data.host.core.channels.dingtalk.channel.httpx.post") as post:
        post.return_value = MagicMock(status_code=403, text="forbidden")
        await ch.on_consume_start(_emotion_native())  # must not raise
    post.assert_called_once()


# ---------------------------------------------------------------------
# Clarification card (text-card scheme; user replies with bare digits)
# ---------------------------------------------------------------------


def _clarification_group() -> ClarificationQuestionGroup:
    return ClarificationQuestionGroup(
        id="call_1",
        chat_id="chat_1",
        title="测试答题",
        questions=[
            ClarificationQuestion(
                question="第一题",
                description="",
                multi_select=False,
                options=[
                    ClarificationQuestionAnswer(label="甲", description="a"),
                    ClarificationQuestionAnswer(label="乙", description="b"),
                ],
            ),
            ClarificationQuestion(
                question="第二题",
                description="",
                multi_select=False,
                options=[
                    ClarificationQuestionAnswer(label="丙", description=None),
                    ClarificationQuestionAnswer(label="丁", description=None),
                    ClarificationQuestionAnswer(label="戊", description=None),
                ],
            ),
        ],
        expires_at=None,
    )


def _clarification_group_with_multi() -> ClarificationQuestionGroup:
    """q0 single-select (甲/乙), q1 multi-select (丙/丁/戊)."""
    return ClarificationQuestionGroup(
        id="call_m",
        chat_id="chat_m",
        title="多选测试",
        questions=[
            ClarificationQuestion(
                question="第一题",
                description="",
                multi_select=False,
                options=[
                    ClarificationQuestionAnswer(label="甲", description="a"),
                    ClarificationQuestionAnswer(label="乙", description="b"),
                ],
            ),
            ClarificationQuestion(
                question="第二题",
                description="",
                multi_select=True,
                options=[
                    ClarificationQuestionAnswer(label="丙", description=None),
                    ClarificationQuestionAnswer(label="丁", description=None),
                    ClarificationQuestionAnswer(label="戊", description=None),
                ],
            ),
        ],
        expires_at=None,
    )


def _clarification_native(conv: str = "conv_1") -> NativePayload:
    """A native payload carrying the conversation_id send_clarification_card reads."""
    return NativePayload(
        channel_id="dingtalk",
        sender_id="staff_x",
        content_parts=[TextContent(text="")],
        meta={"conversation_id": conv, "incoming_message": MagicMock()},
    )


def _seed_pending(ch: DingTalkChannel, card_id: str, conv: str) -> None:
    """Mirror what base._stream_to_platform + send_clarification_card set up."""
    from qwenpaw_data.host.core.channels.base import _ClarificationCardPending

    ch._pending_clarification_cards[card_id] = _ClarificationCardPending(
        session_id="ses_x",
        chat_id="chat_x",
        questions=_clarification_group(),
    )
    ch._clarif_by_conv[conv] = card_id


@pytest.mark.asyncio
async def test_send_clarification_card_pushes_text_and_indexes_by_conv() -> None:
    ch = _channel(streaming=False)
    sent: list[str] = []

    async def _fake_send(native: NativePayload, text: str) -> None:
        sent.append(text)

    ch.send = _fake_send  # type: ignore[assignment]
    native = _clarification_native(conv="conv_1")
    card_id = await ch.send_clarification_card(native, _clarification_group())
    assert card_id.startswith("dingtalkqa-")
    assert ch._clarif_by_conv["conv_1"] == card_id
    assert len(sent) == 1
    body = sent[0]
    # The user is NOT asked to echo the card_id; multi-question card uses commas.
    assert card_id not in body
    assert "用逗号分隔" in body  # 2-question hint
    assert "1. 第一题" in body
    assert "（单选）" in body  # each single-select question is marked
    assert "2. 乙" in body  # option line (substring of "   2. 乙：b")
    assert "3. 戊" in body  # option with no description has no trailing colon


@pytest.mark.asyncio
async def test_send_clarification_card_single_question_hint() -> None:
    ch = _channel(streaming=False)
    sent: list[str] = []

    async def _fake_send(native: NativePayload, text: str) -> None:
        sent.append(text)

    ch.send = _fake_send  # type: ignore[assignment]
    one_q = ClarificationQuestionGroup(
        id="c",
        chat_id=None,
        title="t",
        questions=[
            ClarificationQuestion(
                question="选一个",
                description="",
                multi_select=False,
                options=[
                    ClarificationQuestionAnswer(label="A", description=None),
                    ClarificationQuestionAnswer(label="B", description=None),
                ],
            )
        ],
        expires_at=None,
    )
    await ch.send_clarification_card(_clarification_native("conv_s"), one_q)
    assert "直接回复选项编号" in sent[0]
    assert "用逗号分隔" not in sent[0]


@pytest.mark.asyncio
async def test_send_clarification_card_rolls_back_index_on_failure() -> None:
    ch = _channel(streaming=False)

    async def _boom_send(native: NativePayload, text: str) -> None:
        raise RuntimeError("send failed")

    ch.send = _boom_send  # type: ignore[assignment]
    card_id = await ch.send_clarification_card(
        _clarification_native("conv_x"), _clarification_group()
    )
    assert card_id == ""
    assert "conv_x" not in ch._clarif_by_conv


@pytest.mark.asyncio
async def test_try_handle_maps_digits_to_labels_and_submits() -> None:
    ch = _channel(streaming=False)
    card_id = "dingtalkqa_seed1234"
    _seed_pending(ch, card_id, conv="conv_1")
    submitted: list[tuple[str, dict[str, list[str]]]] = []

    async def _fake_submit(cid: str, sels: dict[str, list[str]]) -> bool:
        submitted.append((cid, sels))
        return True

    ch._submit_clarification_card_answer = _fake_submit  # type: ignore[assignment]

    handled = await ch._try_handle_clarification_card_answer("conv_1", "1,3")
    assert handled is True
    assert submitted == [(card_id, {"0": ["甲"], "1": ["戊"]})]
    # Index drained after handling.
    assert "conv_1" not in ch._clarif_by_conv


@pytest.mark.asyncio
async def test_try_handle_out_of_range_yields_empty_and_still_submits() -> None:
    ch = _channel(streaming=False)
    card_id = "dingtalkqa_oor00000"
    _seed_pending(ch, card_id, conv="conv_2")
    submitted: list[dict[str, list[str]]] = []

    async def _fake_submit(cid: str, sels: dict[str, list[str]]) -> bool:
        submitted.append(sels)
        return True

    ch._submit_clarification_card_answer = _fake_submit  # type: ignore[assignment]

    # "9" out of range for q0 (2 options); "1" valid for q1.
    handled = await ch._try_handle_clarification_card_answer("conv_2", "9,1")
    assert handled is True
    assert submitted == [{"0": [], "1": ["丙"]}]


@pytest.mark.asyncio
async def test_try_handle_bare_digits_without_comma_fall_through() -> None:
    """Bare '13' is ambiguous (reads as thirteen) — must not be consumed."""
    ch = _channel(streaming=False)
    _seed_pending(ch, "dingtalkqa_b1", conv="conv_b")
    handled = await ch._try_handle_clarification_card_answer("conv_b", "13")
    assert handled is False
    # Index preserved; the message falls through to the normal inbound path.
    assert ch._clarif_by_conv.get("conv_b") == "dingtalkqa_b1"


@pytest.mark.asyncio
async def test_try_handle_single_question_accepts_single_digit() -> None:
    ch = _channel(streaming=False)
    from qwenpaw_data.host.core.channels.base import _ClarificationCardPending

    one_q = ClarificationQuestionGroup(
        id="c",
        chat_id=None,
        title="t",
        questions=[
            ClarificationQuestion(
                question="选一个",
                description="",
                multi_select=False,
                options=[
                    ClarificationQuestionAnswer(label="A", description=None),
                    ClarificationQuestionAnswer(label="B", description=None),
                ],
            )
        ],
        expires_at=None,
    )
    ch._pending_clarification_cards["dingtalkqa_one"] = _ClarificationCardPending(
        session_id="ses", chat_id="chat", questions=one_q
    )
    ch._clarif_by_conv["conv_o"] = "dingtalkqa_one"
    submitted: list[dict[str, list[str]]] = []

    async def _fake_submit(cid: str, sels: dict[str, list[str]]) -> bool:
        submitted.append(sels)
        return True

    ch._submit_clarification_card_answer = _fake_submit  # type: ignore[assignment]
    handled = await ch._try_handle_clarification_card_answer("conv_o", "2")
    assert handled is True
    assert submitted == [{"0": ["B"]}]


@pytest.mark.asyncio
async def test_send_clarification_card_marks_multi_select_and_hint() -> None:
    ch = _channel(streaming=False)
    sent: list[str] = []

    async def _fake_send(native: NativePayload, text: str) -> None:
        sent.append(text)

    ch.send = _fake_send  # type: ignore[assignment]
    await ch.send_clarification_card(
        _clarification_native("conv_m"), _clarification_group_with_multi()
    )
    body = sent[0]
    # Each question is marked with its select-kind.
    assert "1. 第一题（单选）" in body
    assert "2. 第二题（可多选）" in body
    # Multi-select present => hint explains the "/" within-question form.
    assert "1, 2/3" in body
    assert "/ 连写" in body


@pytest.mark.asyncio
async def test_try_handle_multi_select_slash_maps_multiple_labels() -> None:
    ch = _channel(streaming=False)
    card_id = "dingtalkqa_mult0001"
    from qwenpaw_data.host.core.channels.base import _ClarificationCardPending

    ch._pending_clarification_cards[card_id] = _ClarificationCardPending(
        session_id="ses", chat_id="chat", questions=_clarification_group_with_multi()
    )
    ch._clarif_by_conv["conv_m"] = card_id
    submitted: list[dict[str, list[str]]] = []

    async def _fake_submit(cid: str, sels: dict[str, list[str]]) -> bool:
        submitted.append(sels)
        return True

    ch._submit_clarification_card_answer = _fake_submit  # type: ignore[assignment]
    # q0 single -> 1 (甲); q1 multi -> 2/3 (丁, 戊).
    handled = await ch._try_handle_clarification_card_answer("conv_m", "1, 2/3")
    assert handled is True
    assert submitted == [{"0": ["甲"], "1": ["丁", "戊"]}]
    assert "conv_m" not in ch._clarif_by_conv


@pytest.mark.asyncio
async def test_try_handle_non_digit_text_falls_through() -> None:
    ch = _channel(streaming=False)
    _seed_pending(ch, "dingtalkqa_p1", conv="conv_3")
    # A pending card exists, but the user typed a real question, not the format.
    handled = await ch._try_handle_clarification_card_answer("conv_3", "随便一句话")
    assert handled is False
    # Index preserved so a later correctly-formatted reply can still match.
    assert ch._clarif_by_conv.get("conv_3") == "dingtalkqa_p1"


@pytest.mark.asyncio
async def test_try_handle_unknown_conversation_falls_through() -> None:
    ch = _channel(streaming=False)
    handled = await ch._try_handle_clarification_card_answer("conv_none", "1")
    assert handled is False


@pytest.mark.asyncio
async def test_try_handle_stale_index_drains_when_pending_already_gone() -> None:
    ch = _channel(streaming=False)
    # Index points at a card_id base already drained (submitted/expired).
    ch._clarif_by_conv["conv_4"] = "dingtalkqa_gone"
    # Valid-format reply passes the regex, then finds no pending -> drains index.
    handled = await ch._try_handle_clarification_card_answer("conv_4", "1")
    assert handled is False
    assert "conv_4" not in ch._clarif_by_conv


def _incoming_answer(msg_id: str, conv: str, text: str) -> Any:
    """A ChatbotMessage-like object for _on_message."""
    inc = MagicMock()
    inc.message_id = msg_id
    inc.conversation_id = conv
    inc.conversation_type = "1"
    inc.sender_staff_id = "staff_x"
    inc.sender_id = ""
    inc.text = MagicMock(content=text)
    return inc


@pytest.mark.asyncio
async def test_on_message_answer_reacts_thumbs_up_and_skips_enqueue() -> None:
    ch = _channel(streaming=False)
    _seed_pending(ch, "dingtalkqa_ack1", conv="conv_ack")

    submitted: list[str] = []
    emotions: list[str] = []

    async def _fake_submit(cid: str, sels: dict[str, list[str]]) -> bool:
        submitted.append(cid)
        return True

    async def _fake_emotion(
        native: NativePayload, emoji: str, *, recall: bool = False
    ) -> None:
        emotions.append(emoji)

    ch._submit_clarification_card_answer = _fake_submit  # type: ignore[assignment]
    ch._send_emotion = _fake_emotion  # type: ignore[assignment]
    # Spy on enqueue: an answer must NOT be enqueued as a normal message.
    enqueued: list[NativePayload] = []
    ch._enqueue = lambda _native: enqueued.append(_native)  # noqa: E731

    await ch._on_message(_incoming_answer("mid_1", "conv_ack", "1,3"))

    assert submitted == ["dingtalkqa_ack1"]
    assert emotions == ["👍"]
    assert enqueued == []


@pytest.mark.asyncio
async def test_on_message_malformed_answer_hints_and_does_not_enqueue() -> None:
    """A pending card + wrong-format reply: nudge the user, keep the card
    waiting, do NOT enqueue the message (so it can't leak to the agent)."""
    ch = _channel(streaming=False)
    _seed_pending(ch, "dingtalkqa_ack1", conv="conv_ack")

    submitted: list[str] = []
    sent: list[str] = []
    emotions: list[str] = []

    async def _fake_submit(cid: str, sels: dict[str, list[str]]) -> bool:
        submitted.append(cid)
        return True

    async def _fake_send(native: NativePayload, text: str) -> None:
        sent.append(text)

    async def _fake_emotion(
        native: NativePayload, emoji: str, *, recall: bool = False
    ) -> None:
        emotions.append(emoji)

    ch.send = _fake_send  # type: ignore[assignment]
    ch._send_emotion = _fake_emotion  # type: ignore[assignment]
    ch._submit_clarification_card_answer = _fake_submit  # type: ignore[assignment]
    enqueued: list[NativePayload] = []
    ch._enqueue = lambda _native: enqueued.append(_native)  # noqa: E731

    # "13" is bare digits without the required comma separator.
    await ch._on_message(_incoming_answer("mid_1", "conv_ack", "13"))

    # No submission happened, no 👍 (the answer was rejected, not accepted).
    assert submitted == []
    assert emotions == []
    # The card stays in flight so a correctly-formatted retry can still match.
    assert ch._clarif_by_conv.get("conv_ack") == "dingtalkqa_ack1"
    # A format hint was sent instead.
    assert len(sent) == 1
    assert "⚠️" in sent[0]
    assert "格式" in sent[0]
    # The malformed message was NOT enqueued toward the agent.
    assert enqueued == []


@pytest.mark.asyncio
async def test_on_message_no_pending_card_enqueues_normally() -> None:
    """No in-flight card: a numeric-looking message is just a normal user
    message and must reach the agent via the queue."""
    ch = _channel(streaming=False)
    enqueued: list[NativePayload] = []
    sent: list[str] = []

    async def _fake_send(native: NativePayload, text: str) -> None:
        sent.append(text)

    ch.send = _fake_send  # type: ignore[assignment]
    ch._enqueue = lambda _native: enqueued.append(_native)  # noqa: E731

    await ch._on_message(_incoming_answer("mid_1", "conv_plain", "13"))

    assert sent == []  # no hint
    assert len(enqueued) == 1

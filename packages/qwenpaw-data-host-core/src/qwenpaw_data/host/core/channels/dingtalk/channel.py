# -*- coding: utf-8 -*-
"""DingTalk channel."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import dingtalk_stream
import httpx

from qwenpaw_data.host.core.channels.base import (
    DATASOURCE_SELECT_TIMEOUT_S,
    BaseChannel,
    _PendingDsSelect,
    degrade_local_image_md,
)
from qwenpaw_data.host.core.channels.clarification_question import (
     ClarificationQuestionGroup,
)
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.channels.segment_markup import render_segment_spans
from qwenpaw_data.host.core.channels.schema import (
    Content,
    NativePayload,
    TextContent,
)
from qwenpaw_data.host.core.channels.dingtalk.constants import (
    DINGTALK_MARKDOWN_MAX_CHARS,
    DINGTALK_PROCESSED_IDS_MAX,
    DINGTALK_STREAM_MIN_INTERVAL_S,
)

log = logging.getLogger("qwenpaw_data.channels.dingtalk")


class DingTalkChannel(BaseChannel):
    channel = "dingtalk"
    streaming_enabled = True  # AI-card typewriter; toggled by config in _load_config

    # Clarification answer = comma-separated segments (one per question),
    # each segment "/"-separated picks: "1, 2/3" => q0→opt1, q1→opt2+opt3.
    _CLARIFICATION_ANSWER_RE = re.compile(
        r'^[1-9](?:/[1-9])*(?:\s*[,，]\s*[1-9](?:/[1-9])*)*$'
    )

    def __init__(self) -> None:
        super().__init__()
        self._client_id = ""
        self._client_secret = ""
        self._card_template_id = ""
        self._card_template_key = "content"
        self._stream_client: Any = None
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._handler: Optional["_DingTalkHandler"] = None
        self._processed_message_ids: dict[str, None] = {}
        # conversation_id -> card_id for in-flight clarification cards.
        # Populated in send_clarification_card, drained on answer. Lets the
        # user reply with bare digits instead of echoing the card_id.
        self._clarif_by_conv: dict[str, str] = {}

    # ---- config loading ----

    async def _load_config(self) -> None:
        cfg = (await self.services.configs.load(self._user_id)).get("dingtalk") or {}
        self._client_id = cfg.get("client_id") or ""
        self._client_secret = cfg.get("client_secret") or ""
        self._card_template_id = cfg.get("card_template_id") or ""
        self._card_template_key = cfg.get("card_template_key") or "content"
        self.streaming_enabled = bool(cfg.get("streaming_enabled", True))
        if not self._client_id or not self._client_secret:
            raise RuntimeError(
                "dingtalk channel enabled but client_id/client_secret missing"
            )

    def owner_lookup_id(self) -> str:
        return self._client_id

    # ---- lifecycle ----

    async def start(self) -> None:
        import dingtalk_stream
        from dingtalk_stream import ChatbotMessage

        await self._load_config()
        self._main_loop = asyncio.get_running_loop()
        self._stop_event.clear()

        credential = dingtalk_stream.Credential(self._client_id, self._client_secret)
        self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)
        self._handler = _DingTalkHandler(self)
        self._stream_client.register_callback_handler(ChatbotMessage.TOPIC, self._handler)

        self._stream_thread = threading.Thread(
            target=self._run_stream_forever, daemon=True
        )
        self._stream_thread.start()
        log.info("dingtalk channel started (client_id=%s)", self._client_id[:12])

    async def _stop(self) -> None:
        self._stop_event.set()
        if self._stream_thread:
            self._stream_thread.join(timeout=5)
            if self._stream_thread.is_alive():
                log.warning("dingtalk stream thread did not stop within timeout")
        self._stream_client = None
        self._handler = None
        self._stream_thread = None
        log.info("dingtalk channel stopped")

    def _run_stream_forever(self) -> None:
        """Run the SDK's async start() in a daemon thread."""
        try:
            if self._stream_client:
                asyncio.run(self._stream_loop())
        except Exception:
            if not self._stop_event.is_set():
                log.exception("dingtalk stream thread failed")
        finally:
            log.info("dingtalk stream thread stopped")

    async def _stream_loop(self) -> None:
        """Drive the SDK start(); on stop, close the websocket and keep cancelling to exit.

        dingtalk_stream ``start()`` is a ``while True`` reconnect loop that swallows
        CancelledError as a network error and ``continue``s — a single cancel cannot exit it,
        so we keep cancelling until main_task is done.
        """
        client = self._stream_client
        if not client:
            return
        main_task = asyncio.create_task(client.start())

        async def _stop_watcher() -> None:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
            ws = getattr(client, "websocket", None)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            # The SDK swallows CancelledError and continues reconnecting, so a single cancel is ineffective.
            while not main_task.done():
                main_task.cancel()
                await asyncio.sleep(0.1)

        watcher_task = asyncio.create_task(_stop_watcher())
        try:
            await main_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("dingtalk stream start() failed")
        if not watcher_task.done():
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

    # ---- inbound (called by _DingTalkHandler) ----

    async def _on_message(self, incoming: Any) -> None:
        """Handle a single ChatbotMessage: dedup -> parse text -> enqueue."""
        try:
            message_id = str(getattr(incoming, "message_id", "") or "").strip()
            if not message_id or message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            while len(self._processed_message_ids) > DINGTALK_PROCESSED_IDS_MAX:
                # Plain dicts iterate in insertion order: evict the oldest id.
                oldest = next(iter(self._processed_message_ids))
                del self._processed_message_ids[oldest]

            text = ""
            text_obj = getattr(incoming, "text", None)
            if text_obj:
                text = (getattr(text_obj, "content", "") or "").strip()
            if not text:
                return  # text only

            sender_staff_id = str(getattr(incoming, "sender_staff_id", "") or "")
            sender_id = str(getattr(incoming, "sender_id", "") or "")
            conversation_id = str(getattr(incoming, "conversation_id", "") or "")
            conversation_type = str(
                getattr(incoming, "conversation_type", "1") or "1"
            )  # "1" = 1:1 chat, "2" = group chat

            meta = {
                "message_id": message_id,
                "sender_staff_id": sender_staff_id,
                "sender_id": sender_id,
                "conversation_id": conversation_id,
                "conversation_type": conversation_type,
                "is_group": conversation_type == "2",
                "incoming_message": incoming,
            }
            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_staff_id or sender_id,
                content_parts=[TextContent(text=text)],
                meta=meta,
            )

            # Reply to an in-flight clarification card? Consume it before
            # enqueuing and ACK with 👍 for immediate feedback while the
            # agent resumes.
            if conversation_id:
                pending_card = self._clarif_by_conv.get(conversation_id)
                if pending_card:
                    handled = await self._try_handle_clarification_card_answer(
                        conversation_id, text
                    )
                    if handled:
                        log.info('dingtalk: handled clarification card answer message')
                        await self._send_emotion(native, "👍")
                        return
                    # Card in flight but the reply didn't match the answer
                    # grammar. Nudge with the expected format and keep the
                    # card waiting — enqueuing would leak the malformed reply
                    # to the agent once the card resolves.
                    log.info(
                        'dingtalk: rejected malformed clarification answer, '
                        'card_id=%s text=%r', pending_card, text
                    )
                    await self.send(
                        native,
                        '⚠️ 请按格式回复：题间用逗号分隔，多选用 / 连写，'
                        '例如 1, 2/3 表示第一题选 1、第二题选 2 和 3。',
                    )
                    return

            self._enqueue(native)
        except Exception:
            log.exception("dingtalk _on_message failed")

    # ---- session / send ----

    def resolve_session_id(self, native: NativePayload) -> str:
        conv = native.meta.get("conversation_id") or ""
        if conv:
            return f"dingtalk:{conv}"
        if native.sender_id:
            return f"dingtalk:{native.sender_id}"
        raise ValueError(
            "dingtalk inbound payload has neither conversation_id nor sender_id"
        )

    def extract_target_meta(self, native: NativePayload) -> dict[str, Any] | None:
        """Record the dingtalk conversation so a cron job can later push to it.

        Dingtalk's external_key only carries ``conversation_id``; the proactive
        OpenAPI send also needs ``conversation_type`` and ``sender_staff_id``
        (a group targets by ``conversation_id``, a DM by ``sender_staff_id``).
        All three are persisted here and looked up by ``inject_cron_job`` via
        ``_load_target_send_meta`` — without this row a cron run cannot rebuild
        the send address and fails with "target not recorded".
        """
        conversation_id = str(native.meta.get("conversation_id") or "")
        conversation_type = str(native.meta.get("conversation_type") or "1")
        sender_staff_id = str(native.meta.get("sender_staff_id") or "")
        if not conversation_id and not sender_staff_id:
            return None
        return {
            "target_type": "group" if conversation_type == "2" else "single",
            "send_meta": {
                "conversation_id": conversation_id,
                "conversation_type": conversation_type,
                "sender_staff_id": sender_staff_id,
            },
        }

    async def send(self, native: NativePayload, text: str) -> None:
        """Reply via the SDK's reply_markdown; degrade to reply_text truncation over 3500 chars.

        When there is no live ``incoming_message`` (a cron proactive push has a
        synthetic one with no ``session_webhook``), fall back to the OpenAPI
        robot send so the result still reaches the group/person — the SDK
        ``reply_*`` calls POST to ``session_webhook``, which is None for a cron
        push and would silently drop the message.
        """
        if not text:
            return
        incoming = native.meta.get("incoming_message")
        if not incoming or not getattr(incoming, "session_webhook", None):
            # No live webhook (cron proactive, or webhook expired): push via
            # the OpenAPI robot send endpoints from the recorded address.
            await self._send_openapi_text(native, text)
            return
        try:
            if len(text) <= DINGTALK_MARKDOWN_MAX_CHARS:
                self._handler.reply_markdown("回复", text, incoming)
            else:
                self._handler.reply_text(text[:DINGTALK_MARKDOWN_MAX_CHARS], incoming)
        except Exception:
            log.exception("dingtalk send failed")

    def _post_openapi_message(
        self, msg_key: str, msg_param: dict[str, Any], native: NativePayload
    ) -> None:
        """Push one robot message via OpenAPI to the remembered address.

        Cron has no live ``incoming_message`` to reply to, so use the robot send
        endpoints with ``conversation_id`` / ``sender_staff_id`` recorded in send_meta.
        """
        if not self._handler:
            log.warning("dingtalk openapi send: channel not connected")
            return
        client = getattr(self._handler, "dingtalk_client", None)
        if client is None:
            log.warning("dingtalk openapi send: no dingtalk_client")
            return
        access_token = client.get_access_token()
        if not access_token:
            log.warning("dingtalk openapi send: no access token")
            return
        conv_type = str(native.meta.get("conversation_type") or "1")
        conversation_id = str(native.meta.get("conversation_id") or "")
        sender_staff_id = str(native.meta.get("sender_staff_id") or "")
        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": access_token,
        }
        if conv_type == "2":  # group
            if not conversation_id:
                log.warning("dingtalk openapi send: group but no conversation_id")
                return
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            body = {
                "robotCode": self._client_id,
                "openConversationId": conversation_id,
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
        else:  # DM
            if not sender_staff_id:
                log.warning("dingtalk openapi send: DM but no sender_staff_id")
                return
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            body = {
                "robotCode": self._client_id,
                "userIds": [sender_staff_id],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=10.0)
        except Exception:
            log.exception("dingtalk openapi send: http failed")
            return
        if resp.status_code >= 300:
            log.warning(
                "dingtalk openapi send failed: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )

    async def _send_openapi_text(self, native: NativePayload, text: str) -> None:
        """Cron proactive text: markdown via OpenAPI, truncated to the markdown limit."""
        from fastapi.concurrency import run_in_threadpool

        body_text = (
            text
            if len(text) <= DINGTALK_MARKDOWN_MAX_CHARS
            else text[:DINGTALK_MARKDOWN_MAX_CHARS] + "\n\n…（内容过长已截断）"
        )
        try:
            await run_in_threadpool(
                self._post_openapi_message,
                "sampleMarkdown",
                {"title": "定时任务结果", "text": body_text},
                native,
            )
        except Exception:
            log.exception("dingtalk _send_openapi_text failed")

    def _build_synthetic_incoming(
        self, conversation_id: str, conversation_type: str, sender_staff_id: str
    ) -> Any:
        """Build a ``ChatbotMessage`` shell so the AI card / media / segment
        paths run for a cron push (which has no live inbound frame).

        Only the addressing attributes are populated; ``session_webhook`` and
        ``message_id`` stay ``None`` — ``send()`` and ``_send_emotion`` detect
        that and fall back to the OpenAPI proactive path instead of the webhook
        reply, so no message is lost and no reaction is attempted on a phantom
        message.
        """
        from dingtalk_stream.chatbot import ChatbotMessage

        msg = ChatbotMessage()
        msg.conversation_id = conversation_id
        msg.conversation_type = conversation_type
        msg.sender_staff_id = sender_staff_id
        return msg

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        """Make a cron job request look like an inbound dingtalk message; enqueue it.

        Dingtalk's external_key only carries ``conversation_id`` — not enough to push
        (also need ``conversation_type`` + ``sender_staff_id``), so the send_meta
        recorded when the target last spoke to the bot is looked up asynchronously.
        The synthetic native also carries a ``ChatbotMessage`` shell in
        ``meta["incoming_message"]`` (no ``session_webhook`` / ``message_id``): this
        lets the AI streaming card and the media / segment send paths run unchanged,
        while ``send()`` detects the missing webhook and routes text fallbacks through
        the OpenAPI proactive push so nothing is lost.
        """
        if self._loop is None:
            log.warning("dingtalk inject_cron_job: host loop not bound")
            return
        self._loop.call_soon_threadsafe(
            asyncio.create_task,
            self._inject_cron_job_async(cron_job_config),
        )

    async def _inject_cron_job_async(self, cron_job_config: dict[str, Any]) -> None:
        try:
            text = (cron_job_config.get("message", "") or "").strip()
            if not text:
                log.error(
                    "dingtalk inject_cron_job: no message, job=%s",
                    cron_job_config.get("id"),
                )
                return
            external_key = (cron_job_config.get("target_external_key", "") or "").strip()
            if not external_key:
                log.error(
                    "dingtalk inject_cron_job: no target_external_key, job=%s",
                    cron_job_config.get("id"),
                )
                return
            send_meta = await self._load_target_send_meta(external_key)
            if not send_meta:
                log.error(
                    "dingtalk inject_cron_job: target %s not recorded, job=%s",
                    external_key,
                    cron_job_config.get("id"),
                )
                return
            conversation_id = str(send_meta.get("conversation_id") or "")
            conversation_type = str(send_meta.get("conversation_type") or "1")
            sender_staff_id = str(send_meta.get("sender_staff_id") or "")
            meta = {
                "conversation_id": conversation_id,
                "conversation_type": conversation_type,
                "sender_staff_id": sender_staff_id,
                "is_group": conversation_type == "2",
                "cron_datasource_id": (cron_job_config.get("datasource_id") or "").strip() or None,
                # A synthetic incoming_message lets the AI streaming card
                # (AIMarkdownCardInstance) and the media / segment send paths
                # run unchanged — they only read conversation_type /
                # conversation_id / sender_staff_id (hosting_context may be
                # None). It carries no session_webhook and no message_id, so
                # send() stays on the OpenAPI proactive path and _send_emotion
                # no-ops rather than POSTing to a None webhook.
                "incoming_message": self._build_synthetic_incoming(
                    conversation_id, conversation_type, sender_staff_id
                ),
            }
            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_staff_id or conversation_id,
                content_parts=[TextContent(text=text)],
                meta=meta,
            )
            log.info(
                "dingtalk got cron job: target=%s job=%s",
                external_key,
                cron_job_config.get("id"),
            )
            self._enqueue(native)
        except Exception:
            log.exception("dingtalk inject_cron_job failed")

    async def send_datasource_card(
        self,
        native: NativePayload,
        session: Any,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        """Send a datasource selection list (plain-text numbered)."""
        if selectable:
            lines = ["请选择数据源，回复对应编号：", ""]
        else:
            lines = ["当前数据源（已绑定，不可更改）：", ""]
        for i, item in enumerate(items, 1):
            ds_name = str(getattr(item, "datasource_name", "") or "").strip()
            ds_name = ds_name or str(getattr(item, "datasource_id", "") or "")
            mark = " ✓（当前）" if session.datasource_id == getattr(item, "datasource_id", None) else ""
            lines.append(f"{i}. {ds_name}{mark}")
        await self.send(native, "\n".join(lines))

        if selectable:
            external_key = self.resolve_session_id(native)
            self._pending_ds_select[external_key] = _PendingDsSelect(
                items=list(items),
                expires_at=time.monotonic() + DATASOURCE_SELECT_TIMEOUT_S,
            )
        log.info(
            "dingtalk send_datasource_card: items=%d bound=%s selectable=%s",
            len(items), session.datasource_id, selectable,
        )

    # ---- streaming hooks (AI Markdown card) ----

    async def on_streaming_start(self, native: NativePayload, msg_id: str) -> Any:
        """Create an AI Markdown card; returns card state, or None to fall back to plain text.

        The DingTalk AI card (``AIMarkdownCardInstance``) drives a typewriter via
        ``ai_start -> ai_streaming -> ai_finish``. All three are synchronous HTTP
        calls inside the SDK (``requests``), so they run in a threadpool to avoid
        blocking the event loop that ``_stream_to_platform`` awaits on.
        """
        from fastapi.concurrency import run_in_threadpool

        from dingtalk_stream.card_instance import AIMarkdownCardInstance

        incoming = native.meta.get("incoming_message")
        if not self.streaming_enabled or not incoming or not self._handler:
            return None
        client = getattr(self._handler, "dingtalk_client", None)
        if client is None:
            return None
        instance = AIMarkdownCardInstance(client, incoming)
        try:
            await run_in_threadpool(instance.ai_start)
        except Exception:
            log.exception("dingtalk on_streaming_start: ai_start failed")
            return None
        # ai_start sets card_instance_id; "" means create/deliver failed.
        if not getattr(instance, "card_instance_id", None):
            log.warning("dingtalk on_streaming_start: ai_start produced no card")
            return None
        # Pre-create path (response.in_progress, before any text): write a
        # placeholder so the card isn't empty through the reasoning silence.
        if not msg_id:
            try:
                await run_in_threadpool(
                    instance.ai_streaming,
                    markdown="💭 thinking...",
                    append=False,
                )
            except Exception:
                log.debug("dingtalk on_streaming_start: placeholder failed", exc_info=True)
        return {
            "instance": instance,
            "last_update": 0.0,
            "full_text": "",
        }

    async def on_streaming_delta(
        self, native: NativePayload, handle: Any, delta: str
    ) -> None:
        from fastapi.concurrency import run_in_threadpool

        if not handle or not self._handler:
            return

        handle["full_text"] = handle.get("full_text", "") + delta
        now = time.monotonic()
        if now - handle.get("last_update", 0.0) < DINGTALK_STREAM_MIN_INTERVAL_S:
            return  # throttle the push, not the accumulation
        handle["last_update"] = now
        try:
            await run_in_threadpool(
                handle["instance"].ai_streaming,
                markdown=degrade_local_image_md(handle["full_text"]),
                append=False,
            )
        except Exception:
            log.debug("dingtalk on_streaming_delta failed", exc_info=True)

    async def on_streaming_reasoning_delta(
        self, native: NativePayload, handle: Any, accumulated: str
    ) -> None:
        """Render reasoning live into the card with a 💭 prefix (same card as the answer).

        Reuses the delta throttle (``last_update``) so reasoning and answer
        updates share one rate-limit budget per card. ``accumulated`` is the
        full reasoning text so far (base accumulates it) — full-replace mode.
        """
        from fastapi.concurrency import run_in_threadpool

        if not handle:
            return
        now = time.monotonic()
        if now - handle.get("last_update", 0.0) < DINGTALK_STREAM_MIN_INTERVAL_S:
            return  # throttle
        handle["last_update"] = now
        try:
            await run_in_threadpool(
                handle["instance"].ai_streaming,
                markdown=f"💭 {accumulated}",
                append=False,
            )
        except Exception:
            log.debug("dingtalk on_streaming_reasoning_delta failed", exc_info=True)

    async def on_streaming_end(
        self, native: NativePayload, handle: Any, full_text: str
    ) -> None:
        from fastapi.concurrency import run_in_threadpool

        if not handle:
            # Non-streaming fallback: send the full text directly.
            if full_text:
                await self.send(native, full_text)
            return
        try:
            await run_in_threadpool(
                handle["instance"].ai_finish,
                markdown=degrade_local_image_md(full_text),
            )
        except Exception:
            log.debug("dingtalk on_streaming_end failed", exc_info=True)

    async def on_streaming_close(
        self, native: NativePayload, handle: Any, summary: str
    ) -> None:
        """Freeze the exploratory 💭 card at a segment boundary.

        Overwrite the card with a done banner and finalize it (no typing
        spinner). Two SDK calls hit different endpoints: ``ai_streaming``
        pushes the banner into the streaming buffer — the actually displayed
        content (``ai_finish`` alone only flips ``flowStatus`` and leaves the
        last reasoning caption on screen); ``ai_finish`` then ends the
        INPUTING state so the banner carries no spinner.
        """
        from fastapi.concurrency import run_in_threadpool

        if not handle:
            return
        try:
            await run_in_threadpool(
                handle["instance"].ai_streaming,
                markdown="✅ Segment Done!",
                append=False,
            )
            await run_in_threadpool(
                handle["instance"].ai_finish,
                markdown="✅ Segment Done!",
            )
        except Exception:
            log.debug("dingtalk on_streaming_close failed", exc_info=True)

    # ---- inbound ACK (emoji reaction) ----

    async def on_consume_start(self, native: NativePayload) -> None:
        """React to the user's incoming message with 🤔Thinking before the agent runs."""
        await self._send_emotion(native, "🤔Thinking")

    async def on_consume_end(
        self, native: NativePayload, handle: Any, status: str
    ) -> None:
        """Recall the 🤔Thinking reaction and add 🥳Done."""
        await self._send_emotion(native, "🤔Thinking", recall=True)
        await self._send_emotion(native, "🥳Done")

    async def _send_emotion(
        self, native: NativePayload, emoji_name: str, *, recall: bool = False
    ) -> None:
        """Add / recall an emoji reaction on the user's incoming message."""
        from fastapi.concurrency import run_in_threadpool

        incoming = native.meta.get("incoming_message")
        if not incoming or not self._handler:
            return
        client = getattr(self._handler, "dingtalk_client", None)
        if client is None:
            return
        message_id = str(native.meta.get("message_id") or "")
        conversation_id = str(native.meta.get("conversation_id") or "")
        if not message_id or not conversation_id:
            return
        try:
            await run_in_threadpool(
                self._post_emotion,
                client,
                message_id,
                conversation_id,
                emoji_name,
                recall,
            )
        except Exception:
            log.debug("dingtalk _send_emotion failed", exc_info=True)

    def _post_emotion(
        self,
        client: Any,
        open_msg_id: str,
        open_conversation_id: str,
        emoji_name: str,
        recall: bool = False,
    ) -> None:
        """Synchronous POST (SDK access_token call is sync); runs in threadpool."""
        access_token = client.get_access_token()
        if not access_token:
            log.debug("dingtalk _send_emotion: no access token")
            return
        action = "recall" if recall else "reply"
        url = f"https://api.dingtalk.com/v1.0/robot/emotion/{action}"
        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": access_token,
        }
        body = {
            "robotCode": self._client_id,
            "openMsgId": open_msg_id,
            "openConversationId": open_conversation_id,
            "emotionName": emoji_name,
            "emotionType": 2,
            "textEmotion": {
                "emotionId": "2659900",
                "emotionName": emoji_name,
                "text": emoji_name,
                "backgroundId": "im_bg_1",
            },
        }
        resp = httpx.post(url, headers=headers, json=body, timeout=10.0)
        if resp.status_code >= 300:
            log.debug(
                "dingtalk _send_emotion %s failed: status=%s body=%s",
                action,
                resp.status_code,
                resp.text[:200],
            )

    # ---- artifact delivery (driven by artifact.registered events) ----

    async def send_image(self, native: NativePayload, path: str) -> None:
        """Upload an image and deliver it inline when possible."""
        from fastapi.concurrency import run_in_threadpool

        incoming = native.meta.get("incoming_message")
        if not incoming or not self._handler:
            return
        client = getattr(self._handler, "dingtalk_client", None)
        if client is None:
            return
        try:
            media_id = await run_in_threadpool(self._upload_media, client, path, "image")
        except Exception:
            log.exception("dingtalk image upload failed: %s", path)
            media_id = None
        if not media_id:
            await self.send(native, f"⚠️ 图片发送失败：{Path(path).name}")
            return

        name = Path(path).name
        # Inline preview via sessionWebhook markdown (![](media_id)).
        webhook = getattr(incoming, "session_webhook", None)
        if webhook and await run_in_threadpool(
            self._post_inline_image, webhook, name, media_id
        ):
            return
        # Fallback: file message (download to view).
        file_type = Path(path).suffix.lstrip(".").lower()
        try:
            await run_in_threadpool(
                self._send_media_message,
                client,
                incoming,
                "sampleFile",
                {"mediaId": media_id, "fileName": name, "fileType": file_type},
            )
        except Exception:
            log.exception("dingtalk image send failed: %s", path)
            await self.send(native, f"⚠️ 图片发送失败：{name}")

    def _post_inline_image(self, webhook: str, name: str, media_id: str) -> bool:
        """Post ``![name](media_id)`` markdown to the sessionWebhook for inline preview."""
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": name, "text": f"![{name}]({media_id})"},
        }
        try:
            resp = httpx.post(
                webhook,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=10.0,
            )
        except Exception:
            log.debug("dingtalk inline image webhook post failed", exc_info=True)
            return False
        if resp.status_code >= 300:
            log.warning(
                "dingtalk inline image failed: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        try:
            return resp.json().get("errcode", 0) == 0
        except Exception:
            return False

    async def send_file(self, native: NativePayload, path: str) -> None:
        """Upload a file and deliver it as a DingTalk file message (see ``send_image``).

        HTML is special-cased: DingTalk's file-security ban blocks ``.html``
        uploads, so we never attempt the banned upload — we tell the user to
        view the artifact in the webui.
        """
        if Path(path).suffix.lower() == ".html":
            await self._send_html_download_link(native, path)
            return
        result = await self._deliver_media(native, path, kind="file")
        if result is False:
            await self.send(native, f"⚠️ 文件发送失败：{Path(path).name}")

    async def _send_html_download_link(
        self, native: NativePayload, path: str
    ) -> None:
        """Redirect the user to the webui for an HTML artifact.

        DingTalk rejects ``.html`` file uploads for security, so we never
        retry the banned upload and point the user at the webui instead.
        """
        name = Path(path).name
        await self.send(
            native,
            f"⚠️ 钉钉不允许直接发送 HTML 文件，请到 WebUI 查看本产物：{name}",
        )


    async def send_clarification_card(self, native: NativePayload, questions: ClarificationQuestionGroup) -> str:
        """Deliver an ask_user_question clarification as a plain-text answer card.

        DingTalk has no interactive-button card callback wired (only AI/markdown
        cards for one-way output), so we use a numbered question list and the
        user replies with 1-based option picks, one per question, separated by
        commas. Unlike the WeChat text scheme, the user does NOT have to echo
        the card_id — inbound carries ``conversation_id``, so we resolve the
        in-flight card for that conversation via ``_clarif_by_conv``.
        """
        card_id = create_id('dingtalkqa')
        conversation_id = str((native.meta or {}).get("conversation_id") or "")
        if conversation_id:
            self._clarif_by_conv[conversation_id] = card_id

        nq = len(questions.questions)
        has_multi = any(q.multi_select for q in questions.questions)
        if nq <= 1 and not has_multi:
            hint = '直接回复选项编号即可，例如 2'
        elif nq <= 1:
            hint = '可多选，用 / 连写，例如 1/3 表示选 1 和 3'
        elif not has_multi:
            hint = f'按题号顺序回复编号，用逗号分隔，例如 1,{nq} 表示第一题选 1、第{nq}题选 {nq}'
        else:
            hint = ('按题号顺序回复，题间用逗号；多选用 / 连写，'
                    '例如 1, 2/3 表示第一题选 1、第二题选 2 和 3')
        lines = [f'📝 请回答下列问题，{hint}', '']
        for i, q in enumerate(questions.questions, 1):
            tag = '（可多选）' if q.multi_select else '（单选）'
            lines.append(f'{i}. {q.question}{tag}')
            for j, a in enumerate(q.options, 1):
                desc = f'：{a.description}' if a.description else ''
                lines.append(f'   {j}. {a.label}{desc}')
            lines.append('')

        try:
            await self.send(native, "\n".join(lines))
            log.info(f'dingtalk sent clarification card, card_id:{card_id}, conv:{conversation_id}')
            return card_id
        except Exception:
            # Roll back the in-flight index so a stale entry doesn't swallow
            # later numeric messages.
            self._clarif_by_conv.pop(conversation_id, None)
            log.exception(f'dingtalk failed to send clarification card, card_id:{card_id}')
            return ''

    async def _try_handle_clarification_card_answer(
        self, conversation_id: str, text: str
    ) -> bool:
        """Consume a reply to an in-flight card.

        Looks up the pending card by ``conversation_id`` (set in
        ``send_clarification_card``), maps each 1-based digit to the matching
        question's option label, and submits via
        ``_submit_clarification_card_answer``. Returns True if the message was
        consumed (a pending card existed and the text matched the answer
        format), False to let it fall through to the normal inbound path.

        Answer grammar: one segment per question, comma-separated; within a
        segment, multiple picks are "/"-separated for a multi-select question.
        e.g. "1, 2/3" => q0→[opt1], q1→[opt2, opt3]. A single-question card is
        just one segment ("2" or "1/3"). Bare "13" is rejected so it isn't
        misread as "thirteen"; the user must write "1,3".
        """
        card_id = self._clarif_by_conv.get(conversation_id)
        if not card_id:
            return False
        # Only the documented answer form (digits, "," between questions, "/"
        # within a question) is treated as an answer; anything else — including
        # a bare "13" the user might intend as two picks — falls through.
        if not DingTalkChannel._CLARIFICATION_ANSWER_RE.match(text):
            return False

        pending = self._pending_clarification_cards.get(card_id)
        if pending is None:
            # Base already drained it (submitted / expired). Drop our stale index.
            self._clarif_by_conv.pop(conversation_id, None)
            return False

        segments = [s for s in re.split(r'[,，]', text) if s.strip()]
        group = pending.questions
        selections: dict[str, list[str]] = {}
        for idx, question in enumerate(group.questions):
            if idx >= len(segments):
                selections[str(idx)] = []
                log.warning(
                    f'dingtalk no answer for clarification card {card_id}, question {idx},'
                    f'{len(segments)} segments, {len(group.questions)} questions'
                )
                continue

            picks = [p for p in re.split(r'/', segments[idx]) if p.strip()]
            labels: list[str] = []
            for pick in picks:
                pick = pick.strip()
                # Regex gate guarantees pick is a single 1-9 digit
                choice = int(pick)
                if choice > len(question.options):
                    log.warning(
                        f'dingtalk answer for clarification card {card_id}, question {idx},'
                        f'user answer {choice} out of range [1..{len(question.options)}]'
                    )
                    continue
                label = question.options[choice - 1].label
                if label not in labels:
                    labels.append(label)
            selections[str(idx)] = labels

        # Drain our index regardless of downstream success; base pops the
        # pending entry inside _submit_clarification_card_answer.
        self._clarif_by_conv.pop(conversation_id, None)
        log.info(
            f'dingtalk handled clarification card answer '
            f'card_id={card_id}, selections={selections}'
        )
        try:
            await self._submit_clarification_card_answer(card_id, selections)
        except Exception:
            log.exception(f'dingtalk failed to handle clarification answer, card_id:{card_id}, text:{text}')
        return True


    async def send_segment(self, native: NativePayload, seg: Any) -> None:
        """Send a BizTrace segment as a DingTalk interactive card (StandardCard)."""
        card_data = _build_segment_card_data(seg)
        if card_data is None:
            return
        incoming = native.meta.get("incoming_message")
        if incoming is None or not self._handler:
            await super().send_segment(native, seg)
            return
        try:
            card_biz_id = self._handler.reply_card(card_data, incoming)
            if not card_biz_id:
                # Card send failed (token/scope) — degrade to plain text.
                await super().send_segment(native, seg)
        except Exception:
            log.exception("dingtalk send_segment failed")

    async def _deliver_media(
        self, native: NativePayload, path: str, *, kind: str
    ) -> Optional[str] | bool:
        """Upload ``path`` and send it.

        Returns the ``media_id`` (str) on success; ``False`` on upload/send failure
        (caller surfaces a fallback notice); ``None`` when the channel is not
        connected (no handler/client) — silent no-op, no fallback.

        ``kind`` is ``"image"`` or ``"file"``. Both upload and send are synchronous
        (SDK uses ``requests``; the send is a raw httpx call), so they run in a
        threadpool.
        """
        from fastapi.concurrency import run_in_threadpool

        incoming = native.meta.get("incoming_message")
        if not incoming or not self._handler:
            return None
        client = getattr(self._handler, "dingtalk_client", None)
        if client is None:
            return None
        try:
            media_id = await run_in_threadpool(self._upload_media, client, path, kind)
        except Exception:
            log.exception("dingtalk %s upload failed: %s", kind, path)
            return False
        if not media_id:
            return False
        file_name = Path(path).name
        file_type = Path(path).suffix.lstrip(".").lower()
        msg_key, msg_param = "sampleFile", {
            "mediaId": media_id,
            "fileName": file_name,
            "fileType": file_type,
        }
        try:
            await run_in_threadpool(
                self._send_media_message, client, incoming, msg_key, msg_param
            )
        except Exception:
            log.exception("dingtalk %s send failed: %s", kind, path)
            return False
        return media_id

    def _upload_media(self, client: Any, path: str, kind: str) -> Optional[str]:
        """Read ``path`` and upload via the SDK; returns media_id or None."""
        file_name = Path(path).name
        mime_type = mimetypes.guess_type(file_name)[0] or (
            "image/png" if kind == "image" else "application/octet-stream"
        )
        filetype = "image" if kind == "image" else "file"
        with open(path, "rb") as f:
            content = f.read()
        media_id = client.upload_to_dingtalk(
            content, filetype=filetype, filename=file_name, mimetype=mime_type
        )
        return media_id

    def _send_media_message(
        self,
        client: Any,
        incoming: Any,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> None:
        """Send an image/file message via the OpenAPI robot send endpoint."""
        access_token = client.get_access_token()
        if not access_token:
            log.warning("dingtalk send_media: no access token")
            return
        robot_code = self._client_id
        msg_param_str = json.dumps(msg_param, ensure_ascii=False)
        conv_type = str(getattr(incoming, "conversation_type", "1") or "1")
        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": access_token,
        }
        if conv_type == "2":
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            body = {
                "robotCode": robot_code,
                "openConversationId": getattr(incoming, "conversation_id", "") or "",
                "msgKey": msg_key,
                "msgParam": msg_param_str,
            }
        else:
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            body = {
                "robotCode": robot_code,
                "userIds": [getattr(incoming, "sender_staff_id", "") or ""],
                "msgKey": msg_key,
                "msgParam": msg_param_str,
            }
        resp = httpx.post(url, headers=headers, json=body, timeout=10.0)
        if resp.status_code >= 300:
            log.warning(
                "dingtalk send_media failed: status=%s body=%s",
                resp.status_code, resp.text[:200],
            )


def _build_segment_card_data(seg: Any) -> dict[str, Any] | None:
    """Build a DingTalk StandardCard (interactive card) for a BizTrace segment."""
    title = str(getattr(seg, "title", "") or "").strip()
    started = getattr(seg, "started_at", None)
    ended = getattr(seg, "ended_at", None)
    dur = ""
    if started and ended and ended > started:
        secs = int(round(ended - started))
        dur = f"用时 {secs // 60}分{secs % 60}秒" if secs >= 60 else f"用时 {secs}秒"
    header_title = f"{title}  {dur}" if (title and dur) else (title or "分析步骤")

    # (label, attr, icon) — color dropped: DingTalk StandardCard markdown
    # strips <font color> (hex renders nowhere — see segment_markup), so the
    # label is bold-only, same as the body numbers.
    section_specs = (
        ("输入", "input", "📥"),
        ("执行", "behavior", "⚙️"),
        ("结论", "conclusion", "✅"),
    )
    contents: list[dict[str, Any]] = []
    idx = 0
    for label, attr, icon in section_specs:
        body = str(getattr(seg, attr, None) or "").strip()
        if not body:
            continue
        body = render_segment_spans(body, target="dingtalk")
        if contents:
            contents.append({"type": "divider", "id": f"div_{idx}"})
            idx += 1
        contents.append(
            {
                "type": "markdown",
                "text": f"**{icon} {label}**\n\n{body}",
                "id": f"md_{idx}",
            }
        )
        idx += 1

    artifact_block = _format_segment_artifacts(seg)
    if artifact_block:
        if contents:
            contents.append({"type": "divider", "id": f"div_{idx}"})
            idx += 1
        contents.append(
            {
                "type": "markdown",
                "text": f"**📎 关键产物**\n\n{artifact_block}",
                "id": f"md_{idx}",
            }
        )

    if not title and not contents:
        return None
    return {
        "config": {"autoLayout": True, "enableForward": True},
        "header": {"title": {"type": "text", "text": header_title}},
        "contents": contents,
    }


def _format_segment_artifacts(seg: Any) -> str:
    """Render ``seg.artifact`` as a one-line-per-file markdown list."""
    items = getattr(seg, "artifact", None) or []
    lines: list[str] = []
    for item in items:
        name = str(getattr(item, "name", "") or "").strip()
        if not name:
            continue
        desc = str(getattr(item, "description", "") or "").strip()
        lines.append(f"- {name}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


class _DingTalkHandler(dingtalk_stream.ChatbotHandler):
    """Subclasses the SDK ChatbotHandler, only overriding process."""

    def __init__(self, channel: "DingTalkChannel") -> None:
        super().__init__()
        self._channel = channel

    async def process(self, callback: Any) -> tuple[int, str]:
        """Called by the SDK when a ChatbotMessage.TOPIC arrives."""
        try:
            from dingtalk_stream import ChatbotMessage

            incoming = ChatbotMessage.from_dict(callback.data)
            loop = self._channel._main_loop
            if loop is None or loop.is_closed():
                return 0, "no loop"
            asyncio.run_coroutine_threadsafe(
                self._channel._on_message(incoming), loop
            )
        except Exception:
            log.exception("dingtalk handler process failed")
        return 0, "OK"

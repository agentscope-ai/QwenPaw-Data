# -*- coding: utf-8 -*-
"""Feishu channel."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from qwenpaw_data.host.core.channels.base import BaseChannel, degrade_local_image_md
from qwenpaw_data.host.core.channels.clarification_question import (
     ClarificationQuestionGroup,
)
from qwenpaw_data.host.core.channels.schema import (
    Content,
    NativePayload,
    TextContent,
)
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.channels.feishu.cards import (
    build_clarification_done_card,
    build_clarification_card,
    build_datasource_card,
    build_followup_card,
    build_segment_card,
    clarification_response,
    resolved_question_keys,
)
from qwenpaw_data.host.core.channels.feishu.constants import (
    FEISHU_PROCESSED_IDS_MAX,
    FEISHU_STREAM_ELEMENT_ID,
    FEISHU_STREAM_MIN_INTERVAL_S,
    FEISHU_WS_BACKOFF_FACTOR,
    FEISHU_WS_INITIAL_RETRY_DELAY,
    FEISHU_WS_MAX_RETRY_DELAY,
)
from qwenpaw_data.host.core.channels.feishu.utils import (
    accumulated_text,
    collect_text_parts,
    extract_json_key,
    extract_post_text,
    feishu_file_type,
    strip_mention_placeholders,
)

log = logging.getLogger("qwenpaw_data.channels.feishu")


class _EventLoopProxy:
    """Forward lark_oapi.ws.client's module-level ``loop`` to the current thread's running loop.

    The lark SDK captures a global loop (the main thread's) at import time; calling
    ``loop.run_until_complete`` inside a daemon thread hits "Event loop is already running".
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(asyncio.get_running_loop(), name)
        except RuntimeError:
            return getattr(asyncio.get_event_loop(), name)


# Replace lark's module-level loop at import time (if lark is installed).
try:
    import lark_oapi.ws.client as _ws_mod  # noqa: F401

    _ws_mod.loop = _EventLoopProxy()
except Exception:
    pass


class FeishuChannel(BaseChannel):
    channel = "feishu"
    streaming_enabled = True

    def __init__(self) -> None:
        super().__init__()
        self._app_id = ""
        self._app_secret = ""
        self._encrypt_key = ""
        self._verification_token = ""
        self._client: Any = None  # lark.Client
        self._ws_client: Any = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._processed_message_ids: dict[str, None] = {}
        self._bot_open_id: str = ""
        # {card_id: {qk: [opt, ...]}} accumulated selections on in-flight cards.
        self._clarif_pending: dict[str, dict[str, list[str]]] = {}
        # {card_id: {qk}} multi-select questions closed via their confirm button.
        self._clarif_resolved: dict[str, set[str]] = {}
        # {card_id: [question, ...]} options for rebuilding a frozen follow-up card.
        self._followup_cards: dict[str, list[str]] = {}
        # {card_id: [(ds_id, ds_name), ...]} for rebuilding a frozen datasource card.
        self._ds_cards: dict[str, list[tuple[str, str]]] = {}

    # ---- config loading ----

    async def _load_config(self) -> None:
        cfg = (await self.services.configs.load(self._user_id)).get("feishu") or {}
        self._app_id = cfg.get("app_id") or ""
        self._app_secret = cfg.get("app_secret") or ""
        self._encrypt_key = cfg.get("encrypt_key") or ""
        self._verification_token = cfg.get("verification_token") or ""
        self.streaming_enabled = bool(cfg.get("streaming_enabled", True))
        if not self._app_id or not self._app_secret:
            raise RuntimeError(
                "feishu channel enabled but app_id/app_secret missing in config"
            )

    def owner_lookup_id(self) -> str:
        return self._app_id

    # ---- lifecycle ----

    async def start(self) -> None:
        import lark_oapi as lark

        await self._load_config()
        self._main_loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(lark.FEISHU_DOMAIN)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self._ws_thread = threading.Thread(target=self._run_ws_forever, daemon=True)
        self._ws_thread.start()
        log.info("feishu channel started (app_id=%s)", self._app_id[:12])

    async def _stop(self) -> None:
        self._stop_event.set()
        if self._ws_loop and not self._ws_loop.is_closed():
            try:
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
            except Exception:
                log.debug("feishu ws_loop.stop failed", exc_info=True)
        if self._ws_thread:
            self._ws_thread.join(timeout=5)
            if self._ws_thread.is_alive():
                log.warning("feishu ws thread did not stop within timeout")
        self._client = None
        self._ws_client = None
        self._ws_thread = None
        self._ws_loop = None
        log.info("feishu channel stopped")

    def _run_ws_forever(self) -> None:
        """Long-lived WebSocket connection with exponential-backoff reconnect.

        Does not call ``ws_client.start()`` (it uses lark's module-level loop and hits
        already-running); instead runs ``run_until_complete(_drive_connection)`` in its own ws_loop.
        """
        import lark_oapi as lark

        retry_delay = FEISHU_WS_INITIAL_RETRY_DELAY
        while not self._stop_event.is_set():
            self._ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)
            try:
                event_handler = (
                    lark.EventDispatcherHandler.builder(
                        self._encrypt_key,
                        self._verification_token,
                    )
                    .register_p2_im_message_receive_v1(self._on_message_sync)
                    .register_p2_im_message_reaction_created_v1(lambda _evt: None)
                    .register_p2_im_message_reaction_deleted_v1(lambda _evt: None)
                    .register_p2_card_action_trigger(self._on_card_action_sync)
                    .build()
                )
                self._ws_client = lark.ws.Client(
                    self._app_id,
                    self._app_secret,
                    event_handler=event_handler,
                    log_level=lark.LogLevel.INFO,
                    domain=lark.FEISHU_DOMAIN,
                )

                async def _select() -> None:
                    while not self._stop_event.is_set():
                        await asyncio.sleep(3600)

                async def _drive_connection() -> None:
                    await self._ws_client._connect()
                    self._ws_loop.create_task(self._ws_client._ping_loop())
                    await _select()

                log.info("feishu ws connecting...")
                self._ws_loop.run_until_complete(_drive_connection())
                if self._stop_event.is_set():
                    break
                log.info("feishu ws disconnected, reconnecting...")
            except RuntimeError as e:
                if "Event loop stopped" in str(e) or "stopped" in str(e):
                    log.debug("feishu ws stopped normally: %s", e)
                    break
                log.exception("feishu ws runtime error")
            except Exception:
                log.exception("feishu ws error, reconnect in %.1fs", retry_delay)
            finally:
                self._drain_ws_loop()
            if self._stop_event.is_set():
                break
            self._stop_event.wait(timeout=retry_delay)
            retry_delay = min(
                retry_delay * FEISHU_WS_BACKOFF_FACTOR, FEISHU_WS_MAX_RETRY_DELAY
            )

    def _drain_ws_loop(self) -> None:
        """Cleanly shut down ws_loop: disconnect websocket -> cancel and drain lingering tasks -> close loop.

        If the long-running tasks started by the lark ``Client`` are not explicitly cancelled,
        ``loop.close()`` triggers a cascade of "Task was destroyed but it is pending" +
        "Event loop is closed" errors.
        """
        loop = self._ws_loop
        if loop is None or loop.is_closed():
            self._ws_loop = None
            return
        try:
            if self._ws_client is not None and hasattr(self._ws_client, "_disconnect"):
                try:
                    loop.run_until_complete(self._ws_client._disconnect())
                except Exception:
                    log.debug("feishu ws disconnect failed", exc_info=True)
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                try:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                except Exception:
                    log.debug("feishu ws task drain failed", exc_info=True)
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._ws_loop = None

    # ---- inbound ----

    def _on_message_sync(self, data: Any) -> None:
        """Triggered by the SDK on the ws_loop thread; schedule onto the main loop across threads."""
        if self._main_loop is None or self._main_loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._on_message(data), self._main_loop)

    def _on_card_action_sync(self, data: Any) -> Any:
        """``card.action.trigger`` fires on the ws_loop thread."""
        event = getattr(data, "event", None)
        action = getattr(event, "action", None) if event else None
        value = getattr(action, "value", None) if action else None
        if not isinstance(value, dict):
            return None
        action_type = value.get("type")
        if action_type in ("clarification_opt", "clarification_confirm"):
            try:
                return self._on_clarification_sync(value)
            except Exception:
                log.exception("feishu clarification sync handler failed")
            return None
        if action_type == "followup_select":
            try:
                return self._on_followup_select_sync(value)
            except Exception:
                log.exception("feishu followup sync handler failed")
            return None
        if action_type == "datasource_select":
            try:
                return self._on_datasource_select_sync(data, value)
            except Exception:
                log.exception("feishu datasource sync handler failed")
            return None
        return None

    def _on_clarification_sync(self, value: dict[str, Any]) -> Any:
        """Handle one clarification click; return the refreshed card + toast."""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard,
            CallBackToast,
            P2CardActionTriggerResponse,
        )

        action_type = value.get("type")
        cardinal = str(value.get("card_id") or "").strip()
        raw_qk = value.get("qk")
        opt = str(value.get("opt") or "").strip() if action_type == "clarification_opt" else ""
        if not cardinal or raw_qk is None:
            self._drop_clarif_state(cardinal)
            return None
        pending = self._pending_clarification_cards.get(cardinal)
        if pending is None:
            self._drop_clarif_state(cardinal)
            return None
        qk = str(raw_qk)
        idx = int(qk) if qk.isdigit() else -1
        if not (0 <= idx < len(pending.questions.questions)):
            return None
        question = pending.questions.questions[idx]
        state = self._clarif_pending.setdefault(cardinal, {})

        if action_type == "clarification_opt":
            if not opt:
                return None
            if question.multi_select:
                cur = state.setdefault(qk, [])
                cur.remove(opt) if opt in cur else cur.append(opt)
                toast = f"已选：{'、'.join(cur) or '无'}"
            else:
                state[qk] = [opt]
                toast = f"已选：{opt}"
        elif action_type == "clarification_confirm":
            if not question.multi_select:
                return None
            if not state.get(qk):
                card = build_clarification_card(
                    cardinal,
                    pending.questions,
                    state,
                    self._clarif_resolved.get(cardinal, set()),
                    warn="请至少选择一个选项后再确认",
                )
                return clarification_response(card, "请至少选择一个选项后再确认")
            self._clarif_resolved.setdefault(cardinal, set()).add(qk)
            toast = "已完成此题"
        else:
            return None

        resolved = resolved_question_keys(
            pending.questions, state, self._clarif_resolved.get(cardinal, set())
        )
        if len(resolved) < len(pending.questions.questions):
            card = build_clarification_card(
                cardinal,
                pending.questions,
                state,
                self._clarif_resolved.get(cardinal, set()),
            )
            return clarification_response(card, toast)

        selections = {key: list(labels) for key, labels in state.items() if labels}

        card = build_clarification_card(
            cardinal,
            pending.questions,
            state,
            self._clarif_resolved.get(cardinal, set()),
            submitted=True,
        )
        self._drop_clarif_state(cardinal)
        if self._main_loop is not None and not self._main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._submit_clarification_card_answer(cardinal, selections),
                self._main_loop,
            )
        return clarification_response(card, "已提交")

    def _on_followup_select_sync(self, value: dict[str, Any]) -> Any:
        """Freeze the card green+✓ on the clicked button and schedule the
        echo + enqueue. The returned response makes Feishu swap the card
        instantly (no ``message_id`` round-trip), like the clarification card.
        """
        card_id = str(value.get("card_id") or "").strip()
        question = str(value.get("question") or "").strip()
        if not question:
            return None
        questions = self._followup_cards.pop(card_id, None) if card_id else None
        if not questions:
            questions = [question]
        receive_id = str(value.get("receive_id") or "")
        receive_id_type = str(value.get("receive_id_type") or "open_id")
        chat_id = str(value.get("chat_id") or "")
        sender_id = str(value.get("sender_id") or "")
        card = build_followup_card(
            card_id=card_id,
            questions=questions,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            chat_id=chat_id,
            sender_id=sender_id,
            clicked=question,
        )
        if self._main_loop is not None and not self._main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._on_followup_select(value), self._main_loop
            )
        return clarification_response(card, "已追问")

    def _on_datasource_select_sync(self, data: Any, value: dict[str, Any]) -> Any:
        """Optimistically freeze the card on the clicked ds and schedule the
        real async bind. Safe because binding is final and the first click
        disables every button; an external bind race leaves a wrong green
        that ``_on_datasource_select`` corrects via ``message_id``.
        """
        event = getattr(data, "event", None)
        context = getattr(event, "context", None) if event else None
        message_id = str(getattr(context, "open_message_id", "") or "") if context else ""
        card_id = str(value.get("card_id") or "").strip()
        ds_id = str(value.get("ds_id") or "").strip()
        if not ds_id:
            return None
        ds_name = str(value.get("ds_name") or ds_id)
        items = self._ds_cards.pop(card_id, None) or [(ds_id, ds_name)]
        receive_id = str(value.get("receive_id") or "")
        receive_id_type = str(value.get("receive_id_type") or "open_id")
        chat_id = str(value.get("chat_id") or "")
        sender_id = str(value.get("sender_id") or "")
        external_key = str(value.get("session_id") or "").strip()
        card = build_datasource_card(
            card_id=card_id,
            items=items,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            chat_id=chat_id,
            sender_id=sender_id,
            session_id=external_key,
            selectable=True,
            clicked_ds_id=ds_id,
            note=f"已选择 {ds_name}，正在绑定…",
        )
        if self._main_loop is not None and not self._main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._on_datasource_select(value, message_id, items),
                self._main_loop,
            )
        return clarification_response(card, "正在绑定…")

    async def _on_datasource_select(
        self,
        value: dict[str, Any],
        message_id: str,
        items: list[tuple[str, str]] | None,
    ) -> None:
        """Real DB bind + card correction. ``items`` lets a wrong-green race
        rebuild the card highlighting the real bound ds; ``None`` (no cached
        items) skips correction, text-only.
        """
        try:
            ds_id = str(value.get("ds_id") or "").strip()
            ds_name = str(value.get("ds_name") or ds_id)
            external_key = str(value.get("session_id") or "").strip()
            if not ds_id or not external_key:
                log.warning("feishu card action: missing ds_id/session_id: %s", value)
                return
            receive_id_type = str(value.get("receive_id_type") or "open_id")
            receive_id = str(value.get("receive_id") or "")
            chat_id = str(value.get("chat_id") or "")
            sender_id = str(value.get("sender_id") or "")

            from qwenpaw_data.host.core.channels.schema import NativePayload

            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_id,
                content_parts=[],
                meta={
                    "chat_id": chat_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                },
            )

            sessions = self.services.sessions
            try:
                session = await sessions.get(external_key)
            except LookupError:
                session = None
            if session is None:
                await self.send(native, "会话未找到，请重新发送消息")
                if message_id and items:
                    await self._update_interactive_message(
                        message_id,
                        build_datasource_card(
                            card_id=str(value.get("card_id") or ""),
                            items=items,
                            receive_id=receive_id,
                            receive_id_type=receive_id_type,
                            chat_id=chat_id,
                            sender_id=sender_id,
                            session_id=external_key,
                            selectable=True,
                            clicked_ds_id=ds_id,
                            note="⚠️ 会话未找到，请重新发送消息",
                        ),
                    )
                return
            if session.datasource_id is not None:
                if session.datasource_id == ds_id:
                    # Same ds as clicked — optimistic freeze already correct.
                    await self.send(native, f"已绑定 {ds_name}，现在可以提问了")
                else:
                    # Different ds — correct the wrong green.
                    await self.send(native, "已绑定数据源，不可更改")
                    if message_id and items:
                        await self._update_interactive_message(
                            message_id,
                            build_datasource_card(
                                card_id=str(value.get("card_id") or ""),
                                items=items,
                                receive_id=receive_id,
                                receive_id_type=receive_id_type,
                                chat_id=chat_id,
                                sender_id=sender_id,
                                session_id=external_key,
                                selectable=True,
                                clicked_ds_id=session.datasource_id,
                                note="已绑定数据源，不可更改",
                            ),
                        )
                return
            session.bind_datasource(ds_id)
            await sessions.save(session)
            # Sync already froze the card correctly on ds_id.
            await self.send(native, f"已绑定 {ds_name}，现在可以提问了")
        except Exception:
            log.exception("feishu _on_datasource_select failed")

    async def _on_followup_select(self, value: dict[str, Any]) -> None:
        """Follow-up button click: echo the question, then queue it as a message."""
        try:
            question = str(value.get("question") or "").strip()
            if not question:
                return
            receive_id_type = str(value.get("receive_id_type") or "open_id")
            receive_id = str(value.get("receive_id") or "")
            chat_id = str(value.get("chat_id") or "")
            sender_id = str(value.get("sender_id") or "")

            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_id,
                content_parts=[TextContent(text=question)],
                meta={
                    "chat_id": chat_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                },
            )
            # Echo the clicked question as a real follow-up, then route inbound.
            await self.send(native, f"💬 {question}")
            self._enqueue(native)
        except Exception:
            log.exception("feishu _on_followup_select failed")

    # ---- clarification card ----

    async def send_clarification_card(
        self, native: NativePayload, questions: ClarificationQuestionGroup
    ) -> str:
        """Deliver an "ask_user_question" clarification as an interactive card.

        Raw-JSON cards only give a button click callback (``card.action.trigger``),
        so each question renders one option-button row: single-select resolves
        on click (re-click replaces), multi-select resolves via its per-question
        close button, and the group auto-submits once all questions resolve.
        """
        if not self._client:
            return ""
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        card_id = create_id("feishu-clarification")
        self._clarif_pending[card_id] = {}
        self._clarif_resolved[card_id] = set()
        card = build_clarification_card(card_id, questions, {}, set())
        msg_id = await self._send_interactive(
            receive_id_type, receive_id, json.dumps(card, ensure_ascii=False)
        )
        if not msg_id:
            self._drop_clarif_state(card_id)
            return ""
        return card_id

    def _drop_clarif_state(self, card_id: str) -> None:
        self._clarif_pending.pop(card_id, None)
        self._clarif_resolved.pop(card_id, None)

    async def _update_interactive_message(
        self, message_id: str, card: dict[str, Any]
    ) -> None:
        """In-place update of a raw interactive card message (``im.v1``),
        used to re-render cards on toggles and swap in the final state."""
        if not message_id or self._client is None:
            return
        from lark_oapi.api.im.v1 import (
            UpdateMessageRequest,
            UpdateMessageRequestBody,
        )

        try:
            req = (
                UpdateMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    UpdateMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(json.dumps(card, ensure_ascii=False))
                    .build(),
                )
                .build()
            )
            resp = await self._client.im.v1.message.aupdate(req)
            if not resp.success():
                log.warning(
                    "feishu update interactive message failed code=%s msg=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                )
        except Exception:
            log.warning("feishu update interactive message failed", exc_info=True)

    async def _on_message(self, data: Any) -> None:
        try:
            event = getattr(data, "event", None)
            message = getattr(event, "message", None) if event else None
            sender = getattr(event, "sender", None) if event else None
            if not message or not sender:
                return

            message_id = str(getattr(message, "message_id", "") or "").strip()
            if not message_id or message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            while len(self._processed_message_ids) > FEISHU_PROCESSED_IDS_MAX:
                self._processed_message_ids.popitem(last=False)

            sender_id_obj = getattr(sender, "sender_id", None)
            sender_id = ""
            if sender_id_obj and getattr(sender_id_obj, "open_id", None):
                sender_id = str(sender_id_obj.open_id).strip()
            if not sender_id:
                sender_id = f"unknown_{message_id[:8]}"

            # Ignore messages sent by the bot itself
            sender_type = getattr(sender, "sender_type", "") or ""
            if sender_type == "bot" and self._bot_open_id and sender_id == self._bot_open_id:
                return

            chat_id = str(getattr(message, "chat_id", "") or "").strip()
            chat_type = str(getattr(message, "chat_type", "p2p") or "p2p").strip()
            msg_type = str(getattr(message, "message_type", "text") or "text").strip()
            content_raw = getattr(message, "content", None) or ""

            # In group chats, only respond when the message @-mentions the bot.
            if chat_type == "group" and not self._is_bot_mentioned(message):
                return

            content_parts = self._parse_content(msg_type, content_raw)
            if not content_parts:
                return  # no text (image-only etc.), ignore

            # In group chats, text that @-mentions the bot carries an @_user_1 placeholder
            meta = {
                "feishu_message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "sender_id": sender_id,
                "receive_id": chat_id or sender_id,
                "receive_id_type": "chat_id" if chat_id else "open_id",
            }
            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_id,
                content_parts=content_parts,
                meta=meta,
            )
            self._enqueue(native)
        except Exception:
            log.exception("feishu _on_message failed")

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Whether this message @-mentions the bot (group chats only)."""
        mentions = getattr(message, "mentions", None) or []
        for m in mentions:
            if str(getattr(m, "mentioned_type", "") or "") == "bot":
                return True
            if self._bot_open_id:
                m_id = getattr(m, "id", None)
                open_id = getattr(m_id, "open_id", None) if m_id else None
                if open_id and str(open_id) == self._bot_open_id:
                    return True
        return False

    def _parse_content(self, msg_type: str, content_raw: str) -> list[Content]:
        """Extract text from text/post messages (media is not downloaded)."""
        if msg_type == "text":
            text = extract_json_key(content_raw, "text")
            if text and text.strip():
                # Strip the @_user_N mention placeholder
                cleaned = strip_mention_placeholders(text)
                return [TextContent(text=cleaned.strip())] if cleaned.strip() else []
            return []
        if msg_type == "post":
            text = extract_post_text(content_raw)
            if text and text.strip():
                cleaned = strip_mention_placeholders(text)
                return [TextContent(text=cleaned.strip())] if cleaned.strip() else []
            return []
        # Ignore non-text messages (image/audio/file are not downloaded; deferred)
        return []

    # ---- session / send ----

    def resolve_session_id(self, native: NativePayload) -> str:
        chat_id = native.meta.get("chat_id") or ""
        if chat_id:
            return f"feishu:{chat_id}"
        if native.sender_id:
            return f"feishu:{native.sender_id}"
        raise ValueError("feishu inbound payload has neither chat_id nor sender_id")

    def extract_target_meta(self, native: NativePayload) -> dict[str, Any] | None:
        """Record the feishu chat/person so a cron job can later push to it.

        Feishu's external_key is self-describing (``feishu:{oc_ chat_id}`` for a
        group, ``feishu:{ou_ open_id}`` for a DM), so ``inject_cron_job`` rebuilds
        the send address from the external_key alone — the send_meta persisted
        here is kept for parity and for any future lookup path. A group carries a
        ``chat_id`` with ``receive_id_type == "chat_id"``; a DM targets a person
        ``open_id``.
        """
        chat_id = str(native.meta.get("chat_id") or "")
        receive_id = str(native.meta.get("receive_id") or "")
        receive_id_type = str(native.meta.get("receive_id_type") or "open_id")
        sender_id = str(native.sender_id or "")
        if not receive_id and not chat_id:
            return None
        is_group = bool(chat_id) and receive_id_type == "chat_id"
        return {
            "target_type": "group" if is_group else "single",
            "send_meta": {
                "chat_id": chat_id,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "sender_id": sender_id,
            },
        }

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        """Treat a cron job run as an inbound feishu message; enqueue it.

        Feishu's external_key is self-describing (``feishu:{chat_id}`` for group with
        ``oc_`` prefix, ``feishu:{open_id}`` for DM with ``ou_`` prefix), so the send
        address is reconstructed from the external_key alone — no DB lookup needed
        (same idea as wecom). The synthetic native reproduces the original external_key
        so ``_enqueue_session`` resumes the existing session; feishu's ``send()`` and
        streaming hooks are already proactive (driven by ``receive_id`` from meta, not an
        incoming frame), so the normal consume flow streams the answer back.
        """
        try:
            text = (cron_job_config.get("message", "") or "").strip()
            if not text:
                log.error(
                    "feishu inject_cron_job: no message, job=%s",
                    cron_job_config.get("id"),
                )
                return
            external_key = (cron_job_config.get("target_external_key", "") or "").strip()
            if not external_key or not external_key.startswith("feishu:"):
                log.error(
                    "feishu inject_cron_job: bad target_external_key %r, job=%s",
                    external_key,
                    cron_job_config.get("id"),
                )
                return
            target_id = external_key[len("feishu:"):].strip()
            if not target_id:
                log.error(
                    "feishu inject_cron_job: empty target id, job=%s",
                    cron_job_config.get("id"),
                )
                return
            # Feishu id prefixes: oc_ = group chat_id, ou_ = person open_id.
            if target_id.startswith("oc_"):
                chat_id = target_id
                receive_id = target_id
                receive_id_type = "chat_id"
                chat_type = "group"
                sender_id = ""
            else:
                # DM (open_id, ou_...) or any non-chat id: treat as a person.
                chat_id = ""
                receive_id = target_id
                receive_id_type = "open_id"
                chat_type = "p2p"
                sender_id = target_id
            meta = {
                "chat_id": chat_id,
                "chat_type": chat_type,
                "sender_id": sender_id,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "cron_datasource_id": (cron_job_config.get("datasource_id") or "").strip() or None,
            }
            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_id,
                content_parts=[TextContent(text=text)],
                meta=meta,
            )
            log.info(
                "feishu got cron job: target=%s job=%s",
                external_key,
                cron_job_config.get("id"),
            )
            self._enqueue(native)
        except Exception:
            log.exception("feishu inject_cron_job failed")

    async def send(self, native: NativePayload, text: str) -> None:
        """Send text back to Feishu (im.v1.message.acreate, msg_type=text)."""
        if not self._client or not text:
            return
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        await self._send_text(receive_id_type, receive_id, text)

    async def _send_text(
        self, receive_id_type: str, receive_id: str, text: str
    ) -> Optional[str]:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        try:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}, ensure_ascii=False))
                    .build(),
                )
                .build()
            )
            resp = await self._client.im.v1.message.acreate(req)
            if not resp.success():
                log.warning(
                    "feishu send failed code=%s msg=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                )
                return None
            return getattr(resp.data, "message_id", None) if resp.data else None
        except Exception:
            log.exception("feishu _send_text failed")
            return None

    # ---- artifact delivery (driven by artifact.registered events) ----

    async def send_image(self, native: NativePayload, path: str) -> None:
        if not self._client or not path:
            return
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        image_key = await self._upload_image(path)
        if not image_key:
            await self.send(native, f"⚠️ 图片发送失败：{Path(path).name}")
            return
        await self._send_image_message(receive_id_type, receive_id, image_key)

    async def send_file(self, native: NativePayload, path: str) -> None:
        if not self._client or not path:
            return
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        file_key = await self._upload_file(path)
        if not file_key:
            await self.send(native, f"⚠️ 文件发送失败：{Path(path).name}")
            return
        await self._send_file_message(receive_id_type, receive_id, file_key)

    async def send_segment(self, native: NativePayload, seg: Any) -> None:
        """Send a BizTrace segment as a structured interactive card."""
        if not self._client:
            await super().send_segment(native, seg)
            return
        card = build_segment_card(seg)
        if card is None:
            await super().send_segment(native, seg)
            return
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        await self._send_interactive(
            receive_id_type, receive_id, json.dumps(card, ensure_ascii=False)
        )

    async def _upload_image(self, path: str) -> Optional[str]:
        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
        )

        try:
            with open(path, "rb") as f:
                req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build(),
                    )
                    .build()
                )
                resp = await self._client.im.v1.image.acreate(req)
            if not resp.success():
                log.warning(
                    "feishu upload image failed code=%s msg=%s log_id=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                    getattr(resp, "log_id", ""),
                )
                return None
            return getattr(resp.data, "image_key", None) if resp.data else None
        except Exception:
            log.exception("feishu _upload_image failed: %s", path)
            return None

    async def _upload_file(self, path: str) -> Optional[str]:
        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
        )

        file_name = Path(path).name
        file_type = feishu_file_type(file_name)
        try:
            with open(path, "rb") as f:
                req = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build(),
                    )
                    .build()
                )
                resp = await self._client.im.v1.file.acreate(req)
            if not resp.success():
                log.warning(
                    "feishu upload file failed code=%s msg=%s log_id=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                    getattr(resp, "log_id", ""),
                )
                return None
            return getattr(resp.data, "file_key", None) if resp.data else None
        except Exception:
            log.exception("feishu _upload_file failed: %s", path)
            return None

    async def _send_image_message(
        self, receive_id_type: str, receive_id: str, image_key: str
    ) -> None:
        await self._send_typed_message(
            receive_id_type, receive_id, "image", {"image_key": image_key}
        )

    async def _send_file_message(
        self, receive_id_type: str, receive_id: str, file_key: str
    ) -> None:
        await self._send_typed_message(
            receive_id_type, receive_id, "file", {"file_key": file_key}
        )

    async def _send_typed_message(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: dict[str, Any],
    ) -> None:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        try:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(json.dumps(content, ensure_ascii=False))
                    .build(),
                )
                .build()
            )
            resp = await self._client.im.v1.message.acreate(req)
            if not resp.success():
                log.warning(
                    "feishu send %s message failed code=%s msg=%s log_id=%s",
                    msg_type,
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                    getattr(resp, "log_id", ""),
                )
        except Exception:
            log.exception("feishu _send_typed_message failed: %s", msg_type)

    async def _send_card(
        self, receive_id_type: str, receive_id: str, card_id: str
    ) -> Optional[str]:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        msg_content = json.dumps(
            {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
        )
        try:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("interactive")
                    .content(msg_content)
                    .build(),
                )
                .build()
            )
            resp = await self._client.im.v1.message.acreate(req)
            if not resp.success():
                log.warning(
                    "feishu send_card failed code=%s msg=%s log_id=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                    getattr(resp, "log_id", ""),
                )
                return None
            return getattr(resp.data, "message_id", None) if resp.data else None
        except Exception:
            log.exception("feishu _send_card failed")
            return None

    async def _send_interactive(
        self, receive_id_type: str, receive_id: str, card_json: str
    ) -> Optional[str]:
        """Send a raw interactive card (``msg_type=interactive``, content=card JSON).

        Unlike ``_send_card`` (which sends an already-created CardKit card_id), this sends raw
        card JSON directly — used for static selection cards (buttons).
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        try:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("interactive")
                    .content(card_json)
                    .build(),
                )
                .build()
            )
            resp = await self._client.im.v1.message.acreate(req)
            if not resp.success():
                log.warning(
                    "feishu send_interactive failed code=%s msg=%s log_id=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                    getattr(resp, "log_id", ""),
                )
                return None
            return getattr(resp.data, "message_id", None) if resp.data else None
        except Exception:
            log.exception("feishu _send_interactive failed")
            return None

    async def send_datasource_card(
        self,
        native: NativePayload,
        session: Any,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        """Send a datasource selection card (raw interactive).

        ``selectable=True`` (unbound): a click freezes the card green+✓ on the
        chosen ds (all disabled) in ``_on_datasource_select_sync``; the bind runs
        async. ``selectable=False`` (bound, view-only): all buttons disabled, bound
        ds highlighted ✓+green.
        """
        if not self._client or not items:
            return
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        chat_id = native.meta.get("chat_id") or ""
        sender_id = native.sender_id
        external_key = session.id

        clean: list[tuple[str, str]] = []
        for item in items:
            ds_id = str(getattr(item, "datasource_id", "") or "").strip()
            if not ds_id:
                continue
            ds_name = (
                str(getattr(item, "datasource_name", "") or "").strip() or ds_id
            )
            clean.append((ds_id, ds_name))
        if not clean:
            return

        if selectable:
            card_id = create_id("feishu-datasource")
            self._ds_cards[card_id] = clean
            card = build_datasource_card(
                card_id=card_id,
                items=clean,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                chat_id=chat_id,
                sender_id=sender_id,
                session_id=external_key,
                selectable=True,
            )
        else:
            card_id = create_id("feishu-datasource")
            card = build_datasource_card(
                card_id=card_id,
                items=clean,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                chat_id=chat_id,
                sender_id=sender_id,
                session_id=external_key,
                selectable=False,
                bound_ds_id=str(session.datasource_id or ""),
            )
        log.info(
            "feishu send_datasource_card: recv=%s/%s buttons=%d bound=%s selectable=%s",
            receive_id_type, receive_id, len(clean), session.datasource_id, selectable,
        )
        msg_id = await self._send_interactive(
            receive_id_type, receive_id, json.dumps(card, ensure_ascii=False)
        )
        if selectable and not msg_id:
            self._ds_cards.pop(card_id, None)
            return
        log.info("feishu send_datasource_card: sent msg_id=%s", msg_id)

    async def send_followups(
        self, native: NativePayload, questions: list[str]
    ) -> None:
        """Send a follow-up card — one button per recommended question.

        A click freezes it green+✓ (clicked primary, all disabled) in
        ``_on_followup_select_sync`` and echoes + enqueues the question.
        """
        if not self._client or not questions:
            return
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        chat_id = native.meta.get("chat_id") or ""
        sender_id = native.sender_id

        clean = [str(q or "").strip() for q in questions if str(q or "").strip()]
        if not clean:
            return
        card_id = create_id("feishu-followup")
        self._followup_cards[card_id] = clean
        card = build_followup_card(
            card_id=card_id,
            questions=clean,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            chat_id=chat_id,
            sender_id=sender_id,
        )
        msg_id = await self._send_interactive(
            receive_id_type, receive_id, json.dumps(card, ensure_ascii=False)
        )
        if not msg_id:
            self._followup_cards.pop(card_id, None)
            return
        log.info(
            "feishu send_followups: sent msg_id=%s card_id=%s questions=%d",
            msg_id, card_id, len(clean),
        )

    # ---- streaming hooks (CardKit) ----

    async def on_streaming_start(self, native: NativePayload, msg_id: str) -> Any:
        """Create the streaming card; returns card state (incl. card_id + sequence).

        ``msg_id=""`` is the pre-create path (``response.in_progress``, before
        any text): seed the card with a 💭 placeholder so it isn't empty through
        the reasoning silence. Non-empty ``msg_id`` is the normal path — the
        first delta will fill the card.
        """
        if not self.streaming_enabled or not self._client:
            return None
        receive_id_type = native.meta.get("receive_id_type", "open_id")
        receive_id = native.meta.get("receive_id") or native.sender_id
        initial_text = "💭 thinking..." if not msg_id else "..."
        card_info = await self._create_streaming_card(receive_id_type, receive_id, initial_text)
        if not card_info:
            log.warning("feishu on_streaming_start: create card failed, fallback to plain text")
            return None
        return {
            "card_id": card_info["card_id"],
            "message_id": card_info["message_id"],
            "sequence": 0,
            "last_update": 0.0,
            "receive_id_type": receive_id_type,
            "receive_id": receive_id,
        }

    async def on_streaming_delta(self, native: NativePayload, handle: Any, delta: str) -> None:
        if not handle or not self._client:
            return
        text = accumulated_text(handle, delta)
        now = time.monotonic()
        if now - handle.get("last_update", 0) < FEISHU_STREAM_MIN_INTERVAL_S:
            return  # throttle the push, not the accumulation
        handle["sequence"] += 1
        handle["last_update"] = now
        await self._update_streaming_text(handle["card_id"], text, handle["sequence"])

    async def on_streaming_reasoning_delta(
        self, native: NativePayload, handle: Any, accumulated: str
    ) -> None:
        """Render reasoning live into the card with a 💭 prefix (same card as the answer).

        Reuses the delta throttle (``last_update``) so reasoning and answer
        updates share one rate-limit budget per card. ``accumulated`` is the
        full reasoning text so far (base accumulates it) — full-replace, does
        not touch ``handle["full_text"]`` (reserved for the final answer).
        """
        if not handle or not self._client:
            return
        now = time.monotonic()
        if now - handle.get("last_update", 0) < FEISHU_STREAM_MIN_INTERVAL_S:
            return  # throttle
        handle["sequence"] += 1
        handle["last_update"] = now
        await self._update_streaming_text(
            handle["card_id"], f"💭 {accumulated}", handle["sequence"]
        )


    async def on_streaming_end(self, native: NativePayload, handle: Any, full_text: str) -> None:
        if not handle or not self._client:
            # Non-streaming fallback: send the full text directly
            if full_text:
                await self.send(native, full_text)
            return

        handle["sequence"] += 1
        ok = await self._update_streaming_text(
            handle["card_id"], full_text, handle["sequence"]
        )
        if not ok:
            log.warning(
                "feishu on_streaming_end: final full-text push failed "
                "(card_id=%s, len=%d); falling back to send()",
                handle.get("card_id"),
                len(full_text),
            )

            if full_text:
                await self.send(native, degrade_local_image_md(full_text))
        await self._finalize_streaming_card(
            handle["card_id"], full_text, handle["sequence"]
        )

    async def on_streaming_close(
        self, native: NativePayload, handle: Any, summary: str
    ) -> None:
        """Freeze the exploratory 💭 card at a segment boundary.

        Marks the exploration as done by overwriting the element with a
        ✅-prefixed.
        """
        if not handle or not self._client:
            return
        text = "✅ Segment Done!"
        handle["sequence"] += 1
        await self._update_streaming_text(
            handle["card_id"], text, handle["sequence"]
        )
        await self._finalize_streaming_card(
            handle["card_id"], text, handle["sequence"]
        )

    # ---- inbound ACK (Typing reaction) ----

    async def on_consume_start(self, native: NativePayload) -> None:
        """React to the user's incoming message with a Typing emoji before the agent runs.

        Best-effort: any failure (no permission, no client) is logged at debug
        and swallowed — the streaming card still provides feedback.
        """
        message_id = str(native.meta.get("feishu_message_id") or "")
        if not message_id:
            return
        await self._add_reaction(message_id, "Typing")

    async def on_consume_end(
        self, native: NativePayload, handle: Any, status: str
    ) -> None:
        """Add a DONE reaction to the user's incoming message."""
        message_id = str(native.meta.get("feishu_message_id") or "")
        if not message_id:
            return
        await self._add_reaction(message_id, "DONE")

    async def _add_reaction(self, message_id: str, emoji_type: str = "Typing") -> None:
        """Add an emoji reaction to a message (non-blocking, best-effort).

        Mirrors QwenPaw feishu: ``im.v1.message_reaction.acreate`` with an
        ``Emoji``. Failures are debug-logged only — reactions are a nicety.
        """
        if not self._client or not message_id:
            return
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            req = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(
                        Emoji.builder().emoji_type(emoji_type).build(),
                    )
                    .build(),
                )
                .build()
            )
            resp = await self._client.im.v1.message_reaction.acreate(req)
            if not resp.success():
                log.debug(
                    "feishu reaction failed code=%s msg=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                )
        except Exception as e:
            log.debug("feishu reaction error: %s", e)

    # ---- CardKit card operations ----

    async def _create_streaming_card(
        self, receive_id_type: str, receive_id: str, initial_text: str
    ) -> Optional[dict[str, str]]:
        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest,
            CreateCardRequestBody,
        )

        card_json = {
            "schema": "2.0",
            "config": {"streaming_mode": True},
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": initial_text,
                        "element_id": FEISHU_STREAM_ELEMENT_ID,
                    }
                ]
            },
        }
        try:
            create_req = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build(),
                )
                .build()
            )
            resp = await self._client.cardkit.v1.card.acreate(create_req)
            if not resp.success():
                log.warning(
                    "feishu create card failed code=%s msg=%s",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                )
                return None
            card_id = getattr(resp.data, "card_id", None) if resp.data else None
            if not card_id:
                return None
        except Exception:
            log.exception("feishu create streaming card failed")
            return None
        message_id = await self._send_card(receive_id_type, receive_id, card_id)
        if not message_id:
            return None
        return {"card_id": card_id, "message_id": message_id}

    async def _update_streaming_text(
        self, card_id: str, text: str, sequence: int
    ) -> bool:
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        content = degrade_local_image_md(text)
        try:
            req = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(FEISHU_STREAM_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .uuid(str(uuid.uuid4()))
                    .sequence(sequence)
                    .build(),
                )
                .build()
            )
            resp = await self._client.cardkit.v1.card_element.acontent(req)
            if not resp.success():
                log.warning(
                    "feishu stream update rejected code=%s msg=%s len=%d",
                    getattr(resp, "code", ""),
                    getattr(resp, "msg", ""),
                    len(content),
                )
                return False
            return True
        except Exception:
            log.debug("feishu stream update failed", exc_info=True)
            return False

    async def _finalize_streaming_card(
        self, card_id: str, summary_text: str, sequence: int
    ) -> bool:
        from lark_oapi.api.cardkit.v1 import (
            SettingsCardRequest,
            SettingsCardRequestBody,
        )

        preview = degrade_local_image_md(summary_text or "").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        settings_json = json.dumps(
            {"config": {"streaming_mode": False, "summary": {"content": preview}}},
            ensure_ascii=False,
        )
        try:
            req = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_json)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build(),
                )
                .build()
            )
            resp = await self._client.cardkit.v1.card.asettings(req)
            return bool(resp.success())
        except Exception:
            log.warning("feishu finalize card failed", exc_info=True)
            return False

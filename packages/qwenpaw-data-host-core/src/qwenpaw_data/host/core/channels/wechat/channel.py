# -*- coding: utf-8 -*-
# pylint: disable=too-many-instance-attributes,too-many-branches
"""WeChat (iLink Bot) channel.

Implement based on QwenPaw ``app/channels/wechat/channel.py``.

Incoming message: monitoring thread -> ``_on_message`` to extract WeChatMessage -> NativePayload ->
``_enqueue``
Outgoing message: iLink API (no streaming card support)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from qwenpaw_data.host.core.channels.base import (
    DATASOURCE_SELECT_TIMEOUT_S,
    BaseChannel,
    _PendingDsSelect,
)
from qwenpaw_data.host.core.channels.clarification_question import (
     ClarificationQuestionGroup,
)
from qwenpaw_data.host.core.channels.schema import (
    NativePayload,
    TextContent,
)
from qwenpaw_data.host.core.channels.wechat.bot_client import WeChatILinkClient
from qwenpaw_data.host.core.utils.ids import create_id

logger = logging.getLogger("qwenpaw_data.channels.wechat")

# Dedup cache cap (context_token / msg_id -> None, FIFO eviction)
_WECHAT_PROCESSED_IDS_MAX = 2000
# Content-based dedup window (same user + same text across two polls)
_TEXT_DEDUP_TTL = 30.0

# Typing-ticket cache TTL (24h, per user)
_TYPING_TICKET_TTL = 24 * 3600.0
_TYPING_REFRESH_INTERVAL = 5.0
_SENDING_DELTA_INTERVAL = 25.0


def _parse_user_id_from_handle(to_handle: str) -> str:
    """``wechat:group:<gid>`` → ``<gid>``；``wechat:<uid>`` → ``<uid>``。"""
    h = (to_handle or "").strip()
    if h.startswith("wechat:group:"):
        return h[len("wechat:group:"):]
    if h.startswith("wechat:"):
        return h[len("wechat:"):]
    return h


class WeChatChannel(BaseChannel):
    """WeChat AI bot backend.
    """

    channel = "wechat"

    _CLARIFICATION_CARD_ID_PATTERN = re.compile(r'^(wechatqa[^:]+):([0-9]+)$')

    def __init__(self) -> None:
        super().__init__()
        self._bot_token: str = ""
        # send data to wechat user
        self._client: Optional[WeChatILinkClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._poll_loop: Optional[asyncio.AbstractEventLoop] = None
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._loop_accepting = threading.Event()  # cleared on stop() to reject new dispatches

        # long-poll cursor (get_updates_buf)
        self._cursor: str = ""

        # inbound dedup: context_token / msg_id -> None
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._processed_ids_lock = threading.Lock()
        # content dedup: {user_id:content_hash -> timestamp}
        self._text_dedup: OrderedDict[str, float] = OrderedDict()

        # cache each user's latest context_token as a fallback for proactive sends
        self._user_context_tokens: Dict[str, str] = {}

        # typing indicator: ticket cache + active stop functions
        self._typing_tickets: Dict[str, Tuple[str, float]] = {}
        self._typing_lock = threading.Lock()
        self._typing_stop_funcs: Dict[str, Callable[[], None]] = {}
        self._typing_stop_lock = threading.Lock()

    async def _load_config(self) -> None:
        cfg = (await self.services.configs.load(self._user_id)).get("wechat") or {}
        self._bot_token = cfg.get("bot_token") or ""
        self.streaming_enabled = bool(cfg.get("streaming_enabled", False))
        if not self._bot_token:
            raise RuntimeError(
                "wechat channel enabled but bot_token missing in config"
            )
        logger.info('wechat channel loaded config')

    def owner_lookup_id(self) -> str:
        # bot_token uniquely identifies a logged-in wechat instance; used by /datasource to look up owner identity.
        return self._bot_token

    # ---- life cycle ----

    async def start(self) -> None:
        await self._load_config()
        self._client = WeChatILinkClient(bot_token=self._bot_token)
        await self._client.start()
        logger.debug('wechat channel started client for sending message to user')

        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._loop_accepting.set()
        self._poll_thread = threading.Thread(
            target=self._run_poll_forever, daemon=True, name="wechat-poll",
        )
        self._poll_thread.start()
        logger.info("wechat channel started (token=%s…)", (self._bot_token or "")[:12])

    async def _stop(self) -> None:
        # reject new dispatches first, then stop the poll thread
        self._loop_accepting.clear()
        self._stop_event.set()
        if self._poll_loop is not None and self._poll_task is not None:
            try:
                self._poll_loop.call_soon_threadsafe(self._poll_task.cancel)
            except Exception:
                pass
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
        self._poll_thread = None
        logger.debug('wechat channel stopped poll_thread')
        # stop all typing indicators
        with self._typing_stop_lock:
            stop_funcs = list(self._typing_stop_funcs.values())
            self._typing_stop_funcs.clear()
        for func in stop_funcs:
            try:
                func()
            except Exception:
                pass
        logger.debug('wechat channel stopped stop_funcs')
        if self._client:
            await self._client.stop()
            logger.debug(f'wechat channel stopped client')
        self._client = None
        logger.info("wechat channel stopped")

    def _run_poll_forever(self) -> None:
        """Thread for getting incoming Wechat message."""
        import sys

        poll_loop = (
            asyncio.SelectorEventLoop()
            if sys.platform == "darwin"
            else asyncio.new_event_loop()
        )
        asyncio.set_event_loop(poll_loop)
        self._poll_loop = poll_loop
        try:
            self._poll_task = poll_loop.create_task(self._poll_loop_async())
            poll_loop.run_until_complete(self._poll_task)
        except asyncio.CancelledError:
            logger.info("wechat: poll task cancelled (graceful stop)")
        except Exception:
            logger.exception("wechat: poll thread failed", exc_info=True)
        finally:
            self._poll_task = None
            try:
                pending = [t for t in asyncio.all_tasks(poll_loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    poll_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                poll_loop.run_until_complete(poll_loop.shutdown_asyncgens())
                poll_loop.close()
            except Exception:
                logger.warning("wechat: poll loop cleanup failed", exc_info=True)
            self._poll_loop = None

    async def _poll_loop_async(self) -> None:
        """Keep probing any incoming message.
        Send message to _on_message to process.
        """
        client = WeChatILinkClient(bot_token=self._bot_token)
        await client.start()
        logger.info('wechat started client for receiving user messages')
        cursor = self._cursor
        consecutive_failures = 0
        max_backoff = 120

        try:
            while not self._stop_event.is_set():
                try:
                    data = await client.getupdates(cursor)
                    ret = data.get("ret", -1)
                    new_cursor = data.get("get_updates_buf")
                    if new_cursor is not None:
                        cursor = new_cursor
                        self._cursor = cursor
                    msgs: List[Dict[str, Any]] = data.get("msgs") or []
                    for msg in msgs:
                        await self._on_message(msg, client)
                    consecutive_failures = 0
                    if ret != 0 and not msgs:
                        if ret == -1:
                            # it is just user has not typed anything
                            logger.debug("wechat getupdates timeout (ret=-1), continue")
                        else:
                            logger.warning(
                                "wechat getupdates non-zero ret=%s (no msgs), retry in 3s",
                                ret,
                            )
                            await asyncio.sleep(3)
                except asyncio.CancelledError:
                    break
                except Exception:
                    consecutive_failures += 1
                    backoff = min(5 * (2 ** (consecutive_failures - 1)), max_backoff)
                    logger.exception(
                        "wechat poll error (%d consecutive), retry in %ds",
                        consecutive_failures, backoff,
                    )
                    if not self._stop_event.is_set():
                        await asyncio.sleep(backoff)
        finally:
            await client.stop()

    # ---- inbound ----

    async def _on_message(
        self, msg: Dict[str, Any], client: WeChatILinkClient
    ) -> None:
        """Analyze an incoming WeChatMessage from user and enqueue it.
        client: unused, keep it for future inbound media download.
        msg:{
            'seq': 6,
            'message_id': 7500263088888423176,
            'from_user_id': 'o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
            'to_user_id': 'cd04134bdf25@im.bot',
            'client_id': 'mmassistant_bypmsg_inbox_mmo9cq805UvyGwrblUVp3btqGZAhPY@weclaw_1788202067_84_xwechat_9',
            'create_time_ms': 1788202068439,
            'update_time_ms': 1788202068571,
            'delete_time_ms': 0,
            'session_id': '',
            'group_id': '',
            'message_type': 1,
            'message_state': 2,
            'item_list': [
                {
                    'type': 1,
                    'create_time_ms': 1788202068439,
                    'update_time_ms': 1788202068439,
                    'is_completed': True,
                    'msg_id': 'v1:3349355860176322291',
                    'button_item_list': [],
                    'at_bot_username_list': [],
                    'text_item': {'text': '地球直徑是多少'}
                }
            ],
            'context_token': '...',
            'root_id': 0,
            'parent_id': 0
        }
        -->
        native:{
            'channel_id': 'wechat',
            'sender_id': 'o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
            'content_parts': [{'type': 'text', 'text': '地球直徑是多少'}],
            'meta': {
                'wechat_from_user_id': 'o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
                'wechat_to_user_id': 'cd04134bdf25@im.bot',
                'wechat_context_token': '...',
                'wechat_group_id': '',
                'is_group': False
            }
        }
        """
        try:
            from_user_id = msg.get("from_user_id", "")
            to_user_id = msg.get("to_user_id", "")
            context_token = msg.get("context_token", "")
            group_id = msg.get("group_id", "")
            msg_type = msg.get("message_type", 0)

            # only handle user→bot messages (message_type == 1)
            if msg_type != 1:
                logger.warning(f'wechat channel ignored non-user->bot msg, from:{from_user_id}, '
                             f'to:{to_user_id}, group_id:{group_id}, type:{msg_type}')
                return

            # dedup: context_token as unique id
            dedup_key = context_token or f"{from_user_id}_{msg.get('msg_id', '')}"
            if dedup_key and self._is_duplicate(dedup_key):
                logger.warning("wechat: duplicate message skipped: %s", dedup_key[:40])
                return

            text_parts: List[str] = []
            for item in msg.get("item_list") or []:
                item_type = item.get("type", 0)
                if item_type == 1:
                    t = (item.get("text_item") or {}).get("text", "").strip()
                    if t:
                        text_parts.append(t)
                elif item_type == 2:
                    text_parts.append("[图片]")
                elif item_type == 3:
                    # voice: transcribe to text via ASR
                    voice_item = item.get("voice_item") or {}
                    asr_text = (
                        voice_item.get("text_item", {}).get("text", "").strip()
                        if isinstance(voice_item.get("text_item"), dict)
                        else voice_item.get("text", "").strip()
                    )
                    text_parts.append(asr_text if asr_text else "[语音]")
                elif item_type == 4:
                    text_parts.append("[文件]")
                elif item_type == 5:
                    text_parts.append("[视频]")
                else:
                    text_parts.append(f"[unsupported type: {item_type}]")

                # quoted (reply) message: prepend the quoted text
                ref_msg = item.get("ref_msg")
                if ref_msg:
                    quoted = self._extract_quoted_text(ref_msg)
                    if quoted:
                        text_parts.insert(0, f"[quoted message: {quoted}]")

            text = "\n".join(text_parts).strip()
            # content dedup (same message across polls with different context_token)
            if text and self._is_text_duplicate(from_user_id, text):
                logger.warning(
                    "wechat: content-duplicate skipped: user=%s len=%d",
                    (from_user_id or "")[:12], len(text),
                )
                return
            if not text:
                logger.warning('wechat: skipped not-text')
                return

            handled = await self._try_handle_clarification_card_answer(text)
            if handled:
                logger.info('wechat: handled clarification card answer message')
                return

            is_group = bool(group_id)
            meta: Dict[str, Any] = {
                "wechat_from_user_id": from_user_id,
                "wechat_to_user_id": to_user_id,
                "wechat_context_token": context_token,
                "wechat_group_id": group_id,
                "is_group": is_group,
            }
            # cache latest context_token as fallback for proactive sends
            if from_user_id and context_token:
                self._user_context_tokens[from_user_id] = context_token

            native = NativePayload(
                channel_id=self.channel,
                sender_id=from_user_id,
                content_parts=[TextContent(text=text)],
                meta=meta,
            )
            logger.info(f'wechat received msg: from_user_id={from_user_id}, group_id:{group_id}, text_len={len(text)}')
            self._enqueue(native)
        except Exception:
            logger.exception("wechat _on_message failed", exc_info=True)

    @staticmethod
    def _extract_quoted_text(ref_msg: Dict[str, Any]) -> str:
        """Extract quoted text from ref_msg.message_item"""
        quoted_item = ref_msg.get("message_item") or {}
        qt = quoted_item.get("type", 0)
        if qt == 1:
            return (quoted_item.get("text_item") or {}).get("text", "").strip()
        if qt == 3:
            voice_item = quoted_item.get("voice_item") or {}
            asr = (
                voice_item.get("text_item", {}).get("text", "").strip()
                if isinstance(voice_item.get("text_item"), dict)
                else voice_item.get("text", "").strip()
            )
            return f"[voice: {asr}]" if asr else "[voice: no transcription]"
        if qt == 2:
            return "[image]"
        if qt == 4:
            return "[file]"
        if qt == 5:
            return "[video]"
        if qt:
            return f"[unsupported type: {qt}]"
        return ""


    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        """ Treat a cron job run as an incoming message. Just queue it.
        cron job config: as defined in CronJobRow, CronJobRepository _to_dict(CronJobRow)
        {
            'id': 'cron_gJ4KpNwT',
            'user_id': '1f8b9611718f4b7a',
            'tenant_id': 'fb48c81f2e8741a9',
            'workspace_id': 'd35d88a968f84ffc',
            'name': 'wechat-2min',
            'enabled': True,
            'message': '讲一个简单的地理知识',
            'datasource_id': 'postgresql-6d09bce3',
            'channel': 'wechat',
            'session_id': None,
            'target_external_key': 'wechat:o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
            'schedule': {'type': 'cron', 'cron': '*/2 * * * *', 'run_at': None, 'timezone': 'Asia/Shanghai'},
            'created_at': datetime.datetime(2026, 8, 31, 18, 29, 56, 900478),
            'updated_at': datetime.datetime(2026, 8, 31, 18, 29, 56, 900478)
        }
        ->
        native:
        {
            'channel_id': 'wechat',
            'sender_id': 'o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
            'content_parts': [
                {
                    'type': 'text',
                    'text': '讲一个简单的地理知识'
                }
            ],
            'meta': {
                'wechat_from_user_id': 'o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
                'wechat_to_user_id': 'cd04134bdf25@im.bot',
                'wechat_context_token': '...',
                'wechat_group_id': '',
                'is_group': False
            }
        }
        """
        try:
            # wechat:group:groupid or wechat:sender_id
            external_key = cron_job_config.get('target_external_key')
            if not external_key:
                raise ValueError('wechat channel cannot handle a cron job request, no external key in job config, '
                                 f'config:{cron_job_config}')
            send_meta = await self._load_target_send_meta(external_key)
            if not send_meta:
                raise ValueError(f'wechat channel cannot handle a cron job request, no send_meta, '
                                 f'config:{cron_job_config}')

            sender_id = ''
            parts = external_key.split(':')
            parts_len = len(parts)
            if parts_len == 2 and parts[0] == 'wechat':
                sender_id = parts[1]
            elif parts_len == 3 and parts[0] == 'wechat' and parts[1] == 'group':
                sender_id = parts[2]

            if not sender_id:
                logger.error(f'wechat channel cannot handle a cron job request, no sender_id:{cron_job_config}')
                return

            text = (cron_job_config.get('message', '') or '').strip()
            if not text:
                logger.error(f'wechat channel cannot handle a cron job request, no text:{cron_job_config}')
                return

            send_meta["cron_datasource_id"] = (cron_job_config.get("datasource_id") or "").strip() or None,
            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_id,
                content_parts=[TextContent(text=text)],
                meta=send_meta,
            )
            self._enqueue(native)
        except Exception:
            logger.exception("wechat inject_cron_job failed")

    # ---- de-dup ----

    def _is_duplicate(self, msg_id: str) -> bool:
        with self._processed_ids_lock:
            if msg_id in self._processed_ids:
                return True
            self._processed_ids[msg_id] = None
            while len(self._processed_ids) > _WECHAT_PROCESSED_IDS_MAX:
                self._processed_ids.popitem(last=False)
        return False

    def _is_text_duplicate(self, from_user_id: str, text: str) -> bool:
        content_hash = hashlib.md5(text.encode()).hexdigest()[:16]
        key = f"{from_user_id}:{content_hash}"
        now = time.time()
        with self._processed_ids_lock:
            prev = self._text_dedup.get(key)
            if prev is not None and now - prev < _TEXT_DEDUP_TTL:
                return True
            self._text_dedup[key] = now
            while len(self._text_dedup) > _WECHAT_PROCESSED_IDS_MAX:
                self._text_dedup.popitem(last=False)
        return False

    # ---- session / send ----

    def resolve_session_id(self, native: NativePayload) -> str:
        meta = native.meta
        group_id = (meta.get("wechat_group_id") or "").strip()
        if group_id:
            return f"wechat:group:{group_id}"
        if native.sender_id:
            return f"wechat:{native.sender_id}"
        raise ValueError(
            "wechat inbound payload has neither wechat_group_id nor sender_id"
        )

    def _resolve_send_target(self, native: NativePayload) -> Tuple[str, str]:
        meta = native.meta
        to_user_id = (
            meta.get("wechat_from_user_id")
            or _parse_user_id_from_handle(self.resolve_session_id(native))
            or ""
        )
        context_token = meta.get("wechat_context_token", "") or (
            self._user_context_tokens.get(to_user_id, "")
        )
        return to_user_id, context_token

    async def send(self, native: NativePayload, text: str, sender: str = None) -> None:
        """Send text back to WeChat as the output of the Bot."""
        if not text or not self._client:
            logger.warning('wechat send return on no-text-or-no-client')
            return
        to_user_id, context_token = self._resolve_send_target(native)
        if not to_user_id or not context_token:
            logger.warning("wechat send: no to_user_id/context_token to reply to")
            return
        try:
            resp = await self._client.send_text(to_user_id, text, context_token)
        except Exception:
            logger.exception("wechat send_text failed", exc_info=True)
            return
        if isinstance(resp, dict):
            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            if ret != 0 or errcode != 0:
                  logger.warning(f'wechat send_text rejected: sender:{sender}, '
                                 f'to_user_id:{to_user_id}, resp:{resp}')

    async def send_datasource_card(
        self,
        native: NativePayload,
        session: Any,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        """Send a list of data sources for user to select.
        WeChat does not support card.
        """
        lines = ["请选择数据源，回复对应编号：", ""]
        for i, item in enumerate(items, 1):
            ds_name = str(getattr(item, "datasource_name", "") or "").strip()
            ds_name = ds_name or str(getattr(item, "datasource_id", "") or "")
            mark = (
                " ✓（当前）"
                if session.datasource_id == getattr(item, "datasource_id", None)
                else ""
            )
            lines.append(f"{i}. {ds_name}{mark}")
        await self.send(native, "\n".join(lines), 'ds-card')
        external_key = self.resolve_session_id(native)
        self._pending_ds_select[external_key] = _PendingDsSelect(
            items=list(items),
            expires_at=time.monotonic() + DATASOURCE_SELECT_TIMEOUT_S,
        )
        logger.info(
            "wechat send_datasource_card: items=%s current session ds=%s",
            len(items), session.datasource_id,
        )


    def extract_target_meta(self, native: NativePayload) -> dict[str, Any] | None:
        """Extract essential data and save it on the channel binding.
        Use it when buidling a native object
        native:{
            'channel_id': 'wechat',
            'sender_id': 'o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
            'content_parts': [{'type': 'text', 'text': '地球直徑是多少'}],
            'meta': {
                'wechat_from_user_id': 'o9cq80wfZH6JB1MlhVORiEhrn5j8@im.wechat',
                'wechat_to_user_id': 'cd04134bdf25@im.bot',
                'wechat_context_token': '...',
                'wechat_group_id': '',
                'is_group': False
            }
        }
        """
        is_group = bool(native.meta.get('is_group', False))
        chat_type = 'group' if is_group else 'user'
        return {
            "target_type": chat_type,
            "send_meta": native.meta,
        }

    # ---- streaming hooks (iLink has no streaming bubble, all no-op; base calls send on completed) ----

    async def on_streaming_start(self, native: NativePayload, msg_id: str) -> Any:
        if not msg_id:
            await self.send(native, "💭 thinking...", 'on-strm-start')
        return {
            # not starting from 0 to delay the 1st reply
            'last_update': _SENDING_DELTA_INTERVAL,
            'accumulated_text_length': 0
        }

    async def on_streaming_delta(
        self, native: NativePayload, handle: Any, delta: str
    ) -> None:
        if delta:
            handle['accumulated_text_length'] += len(delta)
        now = time.monotonic()
        if now - handle.get("last_update", 0.0) < _SENDING_DELTA_INTERVAL:
            # reduce frequency to avoid server blocking
            return
        handle["last_update"] = now
        await self.send(native, f"⏳ answering... {handle['accumulated_text_length']} bytes so far", 'on-strm-delta')

    async def on_streaming_end(
        self, native: NativePayload, handle: Any, full_text: str
    ) -> None:
        if full_text:
            await self.send(native, '✅ here is the answer', 'on-strm-end')
            await self.send(native, full_text, 'on-strm-end')


    async def on_streaming_close(
        self, native: NativePayload, handle: Any, summary: str
    ) -> None:
        await self.send(native, "✅ Segment Done!", 'on-strm-close')
        return None

    async def on_streaming_reasoning_delta(
        self, native: NativePayload, handle: Any, accumulated: str
    ) -> None:
        now = time.monotonic()
        if now - handle.get("last_update", 0.0) < _SENDING_DELTA_INTERVAL:
            # reduce frequency to avoid server blocking
            return
        handle["last_update"] = now
        return None

    async def on_consume_start(self, native: NativePayload) -> None:
        """Set title of Wechat conversation window as 'Typing...'. """
        to_user_id, context_token = self._resolve_send_target(native)
        if not to_user_id or not context_token:
            logger.warning('wechat on_cosume_start return on no-to-userid-or-no-ctx-token, '
                           f'channel:{native.channel_id}, sender:{native.sender_id}, '
                           f'to_user_id:{to_user_id}, context_token:{context_token}')
            return
        # stop the user's previous typing indicator
        self._stop_typing_for_user(to_user_id)
        try:
            stop_func = await self._start_typing(to_user_id, context_token)
            with self._typing_stop_lock:
                self._typing_stop_funcs[to_user_id] = stop_func
        except Exception:
            logger.debug("wechat on_consume_start typing failed", exc_info=True)

    async def on_consume_end(
        self, native: NativePayload, handle: Any, status: str
    ) -> None:
        """Remove the 'Typing...' title from Wechat conversation window."""
        to_user_id, _ = self._resolve_send_target(native)
        if to_user_id:
            self._stop_typing_for_user(to_user_id)


    # ---- artifact delivery ----

    async def send_image(self, native: NativePayload, path: str) -> None:
        await self._send_media(native, path, "image")

    async def send_file(self, native: NativePayload, path: str) -> None:
        await self._send_media(native, path, "file")

    async def _send_media(
        self, native: NativePayload, path: str, kind: str
    ) -> None:
        """Upload file to WeChat server, which will then send it to user."""
        if not self._client:
            logger.warning(f'wechat send_media, no client, {kind}, {path}, channel:{native.channel_id}, sender:{native.sender_id}')
            return
        to_user_id, context_token = self._resolve_send_target(native)
        if not to_user_id or not context_token:
            logger.warning("wechat _send_media: no to_user_id/context_token")
            return
        p = Path(path)
        if not p.is_file():
            logger.warning("wechat _send_media: file not found: %s", path)
            return
        try:
            if kind == "image":
                logger.info(f'wechat _send_media, {kind}, {path}, channel:{native.channel_id}, sender:{native.sender_id}')
                await self._client.send_image(to_user_id, str(p), context_token)
            else:
                logger.info(f'wechat _send_media, {kind}, {path}, channel:{native.channel_id}, sender:{native.sender_id}')
                await self._client.send_file(to_user_id, str(p), p.name, context_token)
        except Exception:
            logger.exception(
                "wechat _send_media failed kind=%s path=%s", kind, path[:60],
                exc_info=True,
            )
            await self.send(native, f"⚠️ Failed to send file:{p.name}", f'_send_media, kind:{kind}, path:{path}')


    async def send_clarification_card(self, native: NativePayload, questions: ClarificationQuestionGroup) -> str:
        """Build a multi-selection card and send it to user.
        Implement the card as a multi-line text message.
        """
        card_id = create_id('wechatqa')
        lines = [f'答题卡ID:{card_id}',
                 '--------------------',
                 '请回答下列问题，答案格式 "答题卡ID:顺序写入所有题的答案编号"',
                 '比如答题卡aaa，有2道题，第一题选1，第二题选3，就回复aaa:13',
                 '--------------------',
                 '']

        for i, q in enumerate(questions.questions, 1):
            lines.append(f'题{i}. {q.question}')
            for j, a in enumerate(q.options, 1):
                desc = f':{a.description}' if a.description else ''
                lines.append(f'{j}. {a.label}{desc}')
            lines.append('')

        try:
            await self.send(native, "\n".join(lines), 'clarification-card')
            logger.info(f'wechat sent clarification card, card_id:{card_id}, lines:{lines}')
            return card_id
        except Exception:
            logger.exception(f'wechat failed to send clarification card, card_id:{card_id}, lines:{lines}')
            return ''


    async def _try_handle_clarification_card_answer(self, text: str) -> bool:
        """Detect and handle user's answer to an existing clarification card.
        """
        if not text:
            return False

        match = WeChatChannel._CLARIFICATION_CARD_ID_PATTERN.search(text)
        if match and match.groups() and len(match.groups()) == 2:
            card_id = match.groups()[0]
            # 1-based selections
            # `answers` carries one 1-based selection digit per question, in order
            # (e.g. "13" = q0→option 1, q1→option 3; see send_clarification_card).
            answers = match.groups()[1]
            logger.info(f'found user answer, card_id:{card_id}, answers:{answers}')
        else:
            return False

        pending = self._pending_clarification_cards.get(card_id, None)
        if not pending:
            logger.warning(f'pending clarification card not found for card_id:{card_id}')
            return False

        # ClarificationQuestionGroup
        group = pending.questions

        # {question id: [answers, answers]}, 1-item array for single-selection question
        # Parse the answer for each question. Fall back to blank answer if parsing failed.
        selections: dict[str, list[str]] = {}
        # question: ClarificationQuestion
        for idx, question in enumerate(group.questions):
            if idx >= len(answers):
                selections[str(idx)] = []
                logger.warning(
                    f'wechat, no answer for clarification card {card_id}, question {idx},'
                    f'{len(answers)} answers, {len(group.questions)} questions'
                )
                continue

            digit = answers[idx]
            if not digit.isdigit():
                selections[str(idx)] = []
                logger.warning(
                    f'wechat, answer for clarification card {card_id}, question {idx},'
                    f'not a digit {digit!r} '
                )
                continue

            choice = int(digit)
            if choice < 1 or choice > len(question.options):
                selections[str(idx)] = []
                logger.warning(
                    f'wechat, answer for clarification card {card_id}, question {idx},'
                    f'user answer {choice} out of range [1..{len(question.options)}]'
                )
                continue

            selections[str(idx)] = [question.options[choice - 1].label]

        try:
            logger.info('wechat handled clarification card answer'
                        f'card_id={card_id}, selections={selections}'
            )
            await self._submit_clarification_card_answer(card_id, selections)
        except Exception:
            logger.exception(f"wechat failed to handle clarification answer, card_id:{card_id}, text:{text}")
        return True


    # ---- typing indicator ----

    async def _get_typing_ticket(
        self, user_id: str, context_token: str
    ) -> str:
        now = time.time()
        with self._typing_lock:
            if user_id in self._typing_tickets:
                ticket, expiry = self._typing_tickets[user_id]
                if now < expiry:
                    return ticket
                del self._typing_tickets[user_id]
        if not self._client:
            return ""
        try:
            resp = await self._client.getconfig(
                ilink_user_id=user_id, context_token=context_token,
            )
            if resp.get("ret", 1) == 0 and (resp.get("errcode") or 0) == 0:
                ticket = (resp.get("typing_ticket") or "").strip()
                if ticket:
                    with self._typing_lock:
                        self._typing_tickets[user_id] = (ticket, now + _TYPING_TICKET_TTL)
                    return ticket
        except Exception:
            logger.warning("wechat getconfig failed", exc_info=True)
        return ""

    async def _start_typing(
        self, user_id: str, context_token: str
    ) -> Callable[[], None]:
        """Let the title of Wechat conversation window showing as 'Typing...'
        Return a stop() function to call later."""
        ticket = await self._get_typing_ticket(user_id, context_token)
        if not ticket:
            return lambda: None

        stop_event = asyncio.Event()

        async def _refresh() -> None:
            while not stop_event.is_set():
                client = self._client
                if client is None:
                    break
                try:
                    await client.sendtyping(user_id, ticket, status=1)
                except Exception:
                    logger.warning("wechat sendtyping refresh failed", exc_info=True)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_TYPING_REFRESH_INTERVAL)
                except asyncio.TimeoutError:
                    pass

        asyncio.create_task(_refresh())

        client = self._client
        if client:
            try:
                await client.sendtyping(user_id, ticket, status=1)
            except Exception:
                logger.warning("wechat sendtyping initial failed", exc_info=True)

        def stop() -> None:
            stop_event.set()
            c = self._client
            if c:
                try:
                    asyncio.ensure_future(self._stop_typing(user_id, ticket))
                except RuntimeError:
                    pass

        return stop

    async def _stop_typing(self, user_id: str, ticket: str) -> None:
        if self._client:
            try:
                await self._client.sendtyping(user_id, ticket, status=2)
            except Exception:
                pass

    def _stop_typing_for_user(self, user_id: str) -> None:
        with self._typing_stop_lock:
            stop_func = self._typing_stop_funcs.pop(user_id, None)
        if stop_func:
            try:
                stop_func()
            except Exception:
                pass

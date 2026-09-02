# -*- coding: utf-8 -*-
# pylint: disable=too-many-instance-attributes,too-many-branches
# pylint: disable=protected-access  # drive SDK connect/disconnect on its own loop
# pylint: disable=broad-exception-caught
"""WeCom (Enterprise WeChat) channel —— 适配 QwenPaw-Data BaseChannel 契约。

参考 QwenPaw wecom/channel.py 的 aibot SDK 用法（``WSClient`` + ``reply_stream``
流式回复 + ``on("message", ...)`` 入站），但用 QwenPaw-Data 自己的 NativePayload/meta，
并复用 QwenPaw-Data 已就绪的聊天管线（建/取 Chat + ChatRuntime.run + 订阅 EventHub）。

第一版：text / voice(ASR 文本) / mixed-文本 入站（无媒体下载）+ ``reply_stream``
流式文本回复 + "Thinking…" 占位流保活。会话粒度：单聊 ``wecom:<userid>``，群聊
``wecom:group:<chatid>``。入站 frame 存进 meta，回复时原路 ``reply_stream`` 回。
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
import hashlib
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    flatten_content_parts_to_str,
)
from qwenpaw_data.host.core.utils.ids import create_id

logger = logging.getLogger("qwenpaw_data.channels.wecom")

# Bridge aibot SDK logs into Python standard logging.
_sdk_logger = logging.getLogger("aibot")

# Dedup cache cap (msgid -> None, FIFO eviction)
_WECOM_PROCESSED_IDS_MAX = 2000

# Minimum interval between streaming bubble overwrites (seconds). WeCom
# reply_stream overwrites the whole bubble each call; throttle to avoid flooding.
_WECOM_STREAM_MIN_INTERVAL_S = 0.15

# ---- "Thinking…" processing indicator keepalive ----
# WeCom closes a stream server-side if it stays idle too long while the agent is
# still running (tool calls / reasoning before the first assistant token). A
# placeholder stream is refreshed periodically and force-finished before the
# limit so the real reply can start a fresh stream_id (issue #3947).
_WECOM_PROCESSING_REFRESH_INTERVAL = 20.0
_WECOM_PROCESSING_MAX_DURATION = 180.0
_WECOM_PROCESSING_TEXT = "🤔 Thinking..."
_WECOM_DONE_TEXT = "🥳Done"

# SDK reconnect: -1 = retry forever (let the aibot SDK manage backoff).
_WECOM_MAX_RECONNECT_ATTEMPTS = -1

# Media upload via the WebSocket long-connection (raw protocol; the aibot
# SDK exposes no outbound file API — see _upload_media). Three custom cmds
# not present in the SDK's WsCmd; acks carry no msgtype so we intercept
# them in start() via a ws_manager.on_message patch.
_UPLOAD_CHUNK_SIZE = 512 * 1024  # 512 KB of raw data per chunk
_UPLOAD_CMD_INIT = "aibot_upload_media_init"
_UPLOAD_CMD_CHUNK = "aibot_upload_media_chunk"
_UPLOAD_CMD_FINISH = "aibot_upload_media_finish"
_UPLOAD_CMDS = (_UPLOAD_CMD_INIT, _UPLOAD_CMD_CHUNK, _UPLOAD_CMD_FINISH)
_UPLOAD_ACK_TIMEOUT = 30.0  # seconds to wait for each upload ack

# Map upload media_type -> WeCom send msgtype.
_MEDIA_MSGTYPE: Dict[str, str] = {
    "image": "image",
    "voice": "voice",
    "video": "video",
    "file": "file",
}

class _SdkLoggerAdapter:
    """满足 aibot SDK ``Logger`` 协议，委托给标准 ``logging.Logger``。"""

    def __init__(self, std_logger: logging.Logger) -> None:
        self._log = std_logger

    def debug(self, message: str, *args: object) -> None:
        self._log.debug(message, *args)

    def info(self, message: str, *args: object) -> None:
        self._log.info(message, *args)

    def warn(self, message: str, *args: object) -> None:
        self._log.warning(message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log.error(message, *args)


def _parse_chatid_from_handle(to_handle: str) -> str:
    """``wecom:group:<chatid>`` → ``<chatid>``；``wecom:<userid>`` → ``<userid>``。"""
    h = (to_handle or "").strip()
    if h.startswith("wecom:group:"):
        return h.removeprefix("wecom:group:")
    if h.startswith("wecom:"):
        return h.removeprefix("wecom:")
    return h


class WecomChannel(BaseChannel):
    """WeCom AI Bot channel：aibot WebSocket 收发。

    入站 frame 存进 ``native.meta["wecom_frame"]``，回复时通过同一条连接
    ``reply_stream`` 回，避免另起 HTTP 通道。
    """

    channel = "wecom"

    def __init__(self) -> None:
        super().__init__()
        self._bot_id = ""
        self._secret = ""
        self._client: Any = None  # aibot.WSClient
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # 主 loop
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._processed_message_ids: dict[str, None] = {}

        # "Thinking…" 占位流：external_key -> (stream_id, keepalive_task)。
        # 在 _consume_one_request 起流，由 on_streaming_start 复用其 stream_id
        # （首条 delta 覆写占位气泡），或在 finally 兜底收尾。
        self._processing: dict[str, tuple[str, "asyncio.Task[None]"]] = {}

        # pending upload-ack futures: req_id -> Future[WsFrame]. Upload acks
        # carry no msgtype, so the SDK routes them to on_message; we intercept
        # there (see start()) and resolve the matching future.
        self._upload_ack_futures: dict[str, "asyncio.Future[Any]"] = {}
        self._upload_lock: Optional[asyncio.Lock] = None  # init in start()

    # ---- 配置加载 ----

    async def _load_config(self) -> None:
        cfg = (await self.services.configs.load(self._user_id)).get("wecom") or {}
        self._bot_id = cfg.get("bot_id") or ""
        self._secret = cfg.get("secret") or ""
        self.streaming_enabled = bool(cfg.get("streaming_enabled", True))
        if not self._bot_id or not self._secret:
            raise RuntimeError(
                "wecom channel enabled but bot_id/secret missing in config"
            )

    def owner_lookup_id(self) -> str:
        return self._bot_id

    # ---- 生命周期 ----

    async def start(self) -> None:
        from aibot import WSClient, WSClientOptions

        await self._load_config()
        self._loop = asyncio.get_running_loop()
        self._upload_lock = asyncio.Lock()
        options = WSClientOptions(
            bot_id=self._bot_id,
            secret=self._secret,
            max_reconnect_attempts=_WECOM_MAX_RECONNECT_ATTEMPTS,
            logger=_SdkLoggerAdapter(_sdk_logger),
        )
        self._client = WSClient(options)

        # Intercept raw WS frames before MessageHandler so upload acks (which
        # carry no msgtype and would otherwise be logged as "unknown frame")
        # are routed to the waiting upload futures. Upload cmd acks' req_id
        # starts with one of _UPLOAD_CMDS.
        _orig_on_message = self._client._ws_manager.on_message

        def _ws_raw_handler(frame: Any) -> None:
            req_id = (frame.get("headers") or {}).get("req_id", "")
            if req_id and req_id.startswith(_UPLOAD_CMDS):
                fut = self._upload_ack_futures.get(req_id)
                if fut and not fut.done() and self._loop:
                    self._loop.call_soon_threadsafe(fut.set_result, frame)
                return
            if _orig_on_message:
                _orig_on_message(frame)

        self._client._ws_manager.on_message = _ws_raw_handler

        self._client.on("message", self._on_message_sync)
        # (aibot_event_callback -> event.template_card_event)
        # expand to more event types if needed
        self._client.on("event.template_card_event", self._on_card_event_sync)
        # 可观测性：把 SDK 的连接事件桥到日志。
        self._client.on(
            "disconnected",
            lambda reason: logger.info(f"wecom disconnected: {reason}"),
        )
        self._client.on(
            "reconnecting",
            lambda attempt: logger.info(f"wecom reconnecting: attempt {attempt}"),
        )
        self._client.on("error", lambda error: logger.error(f"wecom error: {error}"))

        self._ws_thread = threading.Thread(
            target=self._run_ws_forever, daemon=True, name="wecom-ws"
        )
        self._ws_thread.start()
        logger.info(f"wecom channel started, bot_id={self._bot_id}")

    async def _stop(self) -> None:
        # disconnect() 内部用 ensure_future 绑定当前 loop，必须在 _ws_loop 上调度。
        if (
            self._client
            and self._ws_loop is not None
            and self._ws_loop.is_running()
        ):
            try:
                self._ws_loop.call_soon_threadsafe(self._client.disconnect)
            except Exception:
                logger.debug("wecom disconnect schedule failed", exc_info=True)
            try:
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
            except Exception:
                logger.debug("wecom ws_loop.stop failed", exc_info=True)
        if self._ws_thread:
            self._ws_thread.join(timeout=5)
            if self._ws_thread.is_alive():
                logger.warning("wecom ws thread did not stop within timeout")
        self._client = None
        self._ws_thread = None
        self._ws_loop = None
        logger.info("wecom channel stopped")

    def _run_ws_forever(self) -> None:
        """后台线程：跑 SDK 的事件 loop。"""
        # macOS / py3.12+ 用 SelectorEventLoop 避免 Proactor 兼容问题。
        ws_loop = (
            asyncio.SelectorEventLoop()
            if sys.platform == "darwin"
            else asyncio.new_event_loop()
        )
        asyncio.set_event_loop(ws_loop)
        self._ws_loop = ws_loop
        threading.current_thread().name = "wecom-ws"
        try:
            ws_loop.run_until_complete(self._client.connect())
            ws_loop.run_forever()
        except Exception:
            logger.exception("wecom WebSocket thread failed", exc_info=True)
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(ws_loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    ws_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                ws_loop.run_until_complete(ws_loop.shutdown_asyncgens())
                ws_loop.close()
            except Exception:
                logger.debug("wecom ws loop cleanup failed", exc_info=True)
            self._ws_loop = None

    # ---- inbound ----

    def _on_message_sync(self, frame: Any) -> None:
        """SDK 在 ws_loop 线程触发；跨线程调度到主 loop。"""
        if self._loop is None or self._loop.is_closed():
            logger.warning("wecom: main loop not set/running, drop message")
            return
        asyncio.run_coroutine_threadsafe(self._on_message(frame), self._loop)

    async def _on_message(self, frame: Any) -> None:
        """解析并入站一条消息
        channel:wecom,
        identity:Identity(user_id='1f8b9611718f4b7a', tenant_id='fb48c81f2e8741a9', workspace_id='d35d88a968f84ffc')

        frame, type:<class 'dict'>, value:
        {
            'cmd': 'aibot_msg_callback',
            'headers': {'req_id': '1EdS2RBgRu6d3TV149TnuwAA'},
            'body': {
                'msgid': '5b44daf1b6164d5570bf3da90282b1fa',
                'aibotid': 'aibLDYtFeDVcLwiE6aNZql9vLx31eArMX79',
                'chattype': 'single',
                'from': {'userid': 'ZhangWei'},
                'msgtype': 'text',
                'response_url': 'https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=...',
                'text': {'content': 'test'}
            }
        }

        native, type:<class 'qwenpaw_data.host.core.channels.schema.NativePayload'>, value:
        {
            'channel_id': 'wecom',
            'sender_id': 'ZhangWei',
            'content_parts': [{'type': 'text', 'text': 'test'}],
            'meta': {
                'wecom_frame': {
                    'cmd': 'aibot_msg_callback',
                    'headers': {'req_id': '1EdS2RBgRu6d3TV149TnuwAA'},
                    'body': {
                        'msgid': '5b44daf1b6164d5570bf3da90282b1fa',
                        'aibotid': 'aibLDYtFeDVcLwiE6aNZql9vLx31eArMX79',
                        'chattype': 'single',
                        'from': {'userid': 'ZhangWei'},
                        'msgtype': 'text',
                        'response_url': 'https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=...',
                        'text': {'content': 'test'}
                    }
                },
                'wecom_sender_id': 'ZhangWei',
                'wecom_chatid': '',
                'wecom_chat_type': 'single',
                'is_group': False
            }
        }
        """
        try:
            body = (frame or {}).get("body") or {}
            msgtype = body.get("msgtype") or ""
            sender_id = (body.get("from") or {}).get("userid", "")
            chatid = body.get("chatid", "")
            chat_type = body.get("chattype", "single")

            msg_id = body.get("msgid") or f"{sender_id}_{body.get('send_time', '')}"
            if msg_id and msg_id in self._processed_message_ids:
                return
            self._processed_message_ids[msg_id] = None
            while len(self._processed_message_ids) > _WECOM_PROCESSED_IDS_MAX:
                self._processed_message_ids.popitem(last=False)

            text_parts: list[str] = []
            if msgtype == "text":
                t = (body.get("text") or {}).get("content", "").strip()
                if t:
                    if chat_type == "group":
                        # 群聊里 @bot 的文本：仅当包裹 / 命令时剥掉 @ 占位，保留普通文本。
                        t = re.sub(r"^@\S+\s+(?=/)", "", t).strip()
                        if t.startswith("/"):
                            t = re.sub(r"@\S+$", "", t).strip()
                    if t:
                        text_parts.append(t)
            elif msgtype == "voice":
                # WeCom 自带 ASR 文本，无需下载音频。
                asr = (body.get("voice") or {}).get("content", "").strip()
                if asr:
                    text_parts.append(asr)
            elif msgtype == "mixed":
                for item in (body.get("mixed") or {}).get("msg_item", []) or []:
                    if item.get("msgtype") == "text":
                        t = (item.get("text") or {}).get("content", "").strip()
                        if t:
                            text_parts.append(t)
            else:
                # 第一版：image/file/video 等媒体消息不下载，忽略。
                return

            # 引用（回复）消息：把被引文本拼到前面。
            quote = body.get("quote")
            if quote and quote.get("msgtype") == "text":
                qt = (quote.get("text") or {}).get("content", "").strip()
                if qt:
                    text_parts.insert(0, f"[quoted message: {qt}]")

            text = "\n".join(text_parts).strip()
            if not text:
                return

            meta = {
                "wecom_frame": frame,
                "wecom_sender_id": sender_id,
                "wecom_chatid": chatid,
                "wecom_chat_type": chat_type,
                "is_group": chat_type == "group",
            }
            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_id,
                content_parts=[TextContent(text=text)],
                meta=meta,
            )
            logger.info(
                f"wecom recv: sender={sender_id} chatid={chatid} "
                f"msgtype={msgtype} text_len={len(text)}"
            )
            self._enqueue(native)
        except Exception:
            logger.exception("wecom _on_message failed", exc_info=True)


    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        """ Treat a cron job run as an incoming message. Just queue it.
        cron job config: as defined in CronJobRow, CronJobRepository _to_dict(CronJobRow)
        {
            'id': 'cron_6gUeHrnP',
            'user_id': '1f8b9611718f4b7a',
            'tenant_id': 'fb48c81f2e8741a9',
            'workspace_id': 'd35d88a968f84ffc',
            'name': 'foo',
            'enabled': True,
            'message': '讲一个一句话笑话',
            'datasource_id': 'postgresql-6d09bce3',
            'channel': 'wecom',
            'session_id': None,
            'target_external_key': 'wecom:ZhangWei',
            'schedule': {'type': 'cron', 'cron': '*/2 * * * *', 'run_at': None, 'timezone': 'Asia/Shanghai'},
            'created_at': datetime.datetime(2026, 8, 28, 22, 32, 17, 371534),
            'updated_at': datetime.datetime(2026, 8, 28, 22, 32, 17, 371534)
        }
        ->
        native
        {
            'channel_id': 'wecom',
            'sender_id': 'ZhangWei',
            'content_parts': [{'type': 'text', 'text': 'test'}],
            'meta': {
                'wecom_frame': {
                    'cmd': 'aibot_msg_callback',
                    'headers': {'req_id': '1EdS2RBgRu6d3TV149TnuwAA'},
                    'body': {
                        'msgid': '5b44daf1b6164d5570bf3da90282b1fa',
                        'aibotid': 'aibLDYtFeDVcLwiE6aNZql9vLx31eArMX79',
                        'chattype': 'single',
                        'from': {'userid': 'ZhangWei'},
                        'msgtype': 'text',
                        'response_url': 'https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=...',
                        'text': {'content': 'test'}
                    }
                },
                'wecom_sender_id': 'ZhangWei',
                'wecom_chatid': '',
                'wecom_chat_type': 'single',
                'is_group': False
            }
        }
        """
        try:
            # wecom:group:chatid or wecom:sender_id
            external_key = cron_job_config.get('target_external_key')
            if not external_key:
                raise ValueError('wecom channel cannot handle a cron job request, no external key in job config, '
                                 f'config:{cron_job_config}')
            send_meta = await self._load_target_send_meta(external_key)
            if not send_meta:
                raise ValueError(f'wecom channel cannot handle a cron job request, no send_meta, '
                                 f'config:{cron_job_config}')

            sender_id = ''
            chatid = ''
            chat_type = ''
            parts = external_key.split(':')
            parts_len = len(parts)
            if parts_len == 2 and parts[0] == 'wecom':
                sender_id = parts[1]
                chat_type = 'single'
            elif parts_len == 3 and parts[0] == 'wecom' and parts[1] == 'group':
                chatid = parts[2]
                chat_type = 'group'

            if not chat_type:
                logger.error(f'wecom channel cannot handle a cron job request, no chat_type:{cron_job_config}')
                return

            text = (cron_job_config.get('message', '') or '').strip()
            if not text:
                logger.error(f'wecom channel cannot handle a cron job request, no text:{cron_job_config}')
                return

            meta = {
                "wecom_frame": send_meta.get('wecom_frame', {}),
                "wecom_sender_id": sender_id,
                "wecom_chatid": chatid,
                "wecom_chat_type": chat_type,
                "is_group": chat_type == "group",
                "cron_datasource_id": (cron_job_config.get("datasource_id") or "").strip() or None,
            }
            native = NativePayload(
                channel_id=self.channel,
                sender_id=sender_id,
                content_parts=[TextContent(text=text)],
                meta=meta,
            )
            logger.info(f"wecom got cron job: sender={sender_id}, chatid={chatid}, "
                        f"chat_type:{chat_type}, cron job:{cron_job_config['id']}")
            self._enqueue(native)
        except Exception:
            logger.exception("wecom inject_cron_job failed")

    # ---- multiple_interaction card submit callback ----

    def _on_card_event_sync(self, frame: Any) -> None:
        """SDK fires this on the ws_loop thread for ``template_card_event``."""
        if self._loop is None or self._loop.is_closed():
            logger.warning("wecom: main loop not set/running, drop card event")
            return
        asyncio.run_coroutine_threadsafe(self._on_card_event(frame), self._loop)

    async def _on_card_event(self, frame: Any) -> None:
        """Convert a clarification card answer submission from user into an ``answer_clarification`` call.
        Can be extended to support other cart event types if needed.
        body example:
        {
            'msgid': '4e127f434b7bf2903823cd75efe72f80',
            'aibotid': 'aibLDYtFeDVcLwiE6aNZql9vLx31eArMX79',
            'chattype': 'single',
            'from': {'userid': 'ZhangWei'},
            'msgtype': 'event',
            'create_time': 1787086905,
            'response_url': 'https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=MbYCSDsyQm',
            'event':
            {
                'eventtype': 'template_card_event',
                'template_card_event':
                {
                    'card_type': 'multiple_interaction',
                    'event_key': 'key',
                    'task_id': 'wecom-clarificaiton_GQ3EQ3X9',
                    'selected_items':
                    {
                        'selected_item':
                        [
                            {'question_key': '0', 'option_ids': {'option_id': ['自然月']}},
                            {'question_key': '1', 'option_ids': {'option_id': ['渠道']}}
                        ]
                    }
                }
            }
        }
        """
        fd = frame
        try:
            fd = json.dumps(frame)
        except Exception:
            pass
        logger.info('\n---------------- wecom _on_card_event -------------------\n'
                    f'frame type:{type(frame)}\n'
                    f'frame value:{fd}\n'
                    '\n---------------- ---------------------------------------\n')
        body = (frame or {}).get("body") or {}
        try:
            event = body.get("event") if isinstance(body.get("event"), dict) else body
            event_type = event.get("eventtype", '') or event.get("event_type", '') or ''
            if event_type and isinstance(event.get(event_type), dict):
                event = event.get(event_type)

            # Some gateway revisions nest the payload under event["data"].
            payload = event.get("data") if isinstance(event.get("data"), dict) else event

            task_id = (
                payload.get("task_id")
                or payload.get("TaskId")
                or event.get("task_id")
                or event.get("TaskId")
            )
            if not task_id:
                logger.info(f"wecom card event without task_id, frame:{body}")
                return

            # Selections: {question_key: option_id | [option_id, ...]}.
            raw_selections = (
                payload.get("response_data")
                or payload.get("selected_items")
                or payload.get("item_list")
                or event.get("response_data")
                or event.get("selected_items")
                or {}
            )
            if 'selected_item' in raw_selections:
                raw_selections = raw_selections['selected_item']

            # { q0: [a00, a01], q1: [a10], }
            selections: dict[str, list[str]] = {}
            if isinstance(raw_selections, dict):
                # {q0: [a00, a01], q1: a10 }
                for key, val in raw_selections.items():
                    if isinstance(val, list):
                        ids = [str(v) for v in val if v is not None]
                    else:
                        ids = [str(val)] if val is not None else []
                    if ids:
                        selections[str(key)] = ids
            elif isinstance(raw_selections, list):
                # [ {q0: [a00, a01]}, {q1: a10} }
                for item in raw_selections:
                    if not isinstance(item, dict):
                        continue
                    qk = str(item.get("question_key") or item.get("QuestionKey") or "")
                    sid = item.get("selected_id") or item.get("SelectedId") or item.get("option_ids") or item.get("option_id")
                    if 'option_id' in sid:
                        sid = sid['option_id']

                    if qk and sid:
                        selections.setdefault(qk, []).extend(
                            [str(sid)] if not isinstance(sid, list) else [str(s) for s in sid if s is not None]
                        )

            logger.info(
                f"wecom card event: task_id={task_id} event_type:{event_type}, selections={selections}"
            )
            await self._submit_clarification_card_answer(task_id, selections)
        except Exception:
            logger.exception(f"wecom _on_card_event failed, body:{body}")

    # ---- session / send ----

    def resolve_session_id(self, native: NativePayload) -> str:
        meta = native.meta
        chatid = (meta.get("wecom_chatid") or "").strip()
        chat_type = (meta.get("wecom_chat_type") or "single").strip()
        if chat_type == "group" and chatid:
            return f"wecom:group:{chatid}"
        if native.sender_id:
            return f"wecom:{native.sender_id}"
        raise ValueError(
            "wecom inbound payload has neither wecom_chatid (group) nor sender_id"
        )

    async def send(self, native: NativePayload, text: str) -> None:
        """Send text back to WeCom: reply_stream (finish=True) when a frame is present, else send_message."""
        if not text:
            return
        frame = native.meta.get("wecom_frame")
        if frame and self._client:
            from aibot import generate_req_id

            sid = generate_req_id("stream")
            await self._reply_stream(frame, sid, text, finish=True)
            return
        chatid = native.meta.get("wecom_chatid") or _parse_chatid_from_handle(
            self.resolve_session_id(native)
        )
        if chatid and self._client:
            try:
                await self._client.send_message(
                    chatid,
                    {"msgtype": "markdown", "markdown": {"content": text}},
                )
            except Exception:
                logger.exception(f"wecom send_message failed chatid={chatid}")
        else:
            logger.warning("wecom send: no frame/chatid to reply to")


    def extract_target_meta(self, native: NativePayload) -> dict[str, Any] | None:
        """记下这个群或单聊的 conversation_id，定时任务按它推卡片
            native, type:<class 'qwenpaw_data.host.core.channels.schema.NativePayload'>, value:
            {
                'channel_id': 'wecom',
                'sender_id': 'ZhangWei',
                'content_parts': [{'type': 'text', 'text': 'test'}],
                'meta': {
                    'wecom_frame': {
                        'cmd': 'aibot_msg_callback',
                        'headers': {'req_id': '1EdS2RBgRu6d3TV149TnuwAA'},
                        'body': {
                            'msgid': '5b44daf1b6164d5570bf3da90282b1fa',
                            'aibotid': 'aibLDYtFeDVcLwiE6aNZql9vLx31eArMX79',
                            'chattype': 'single',
                            'from': {'userid': 'ZhangWei'},
                            'msgtype': 'text',
                            'response_url': 'https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=dUedjHR9ToCXNHImw5w3EgAA_kNmEOUpogonc3TdD9ygcIbMXdl4HpoBykVf5ytXjZWbUJP_OJEKGkG7OiMtJxcgx',
                            'text': {'content': 'test'}
                        }
                    },
                    'wecom_sender_id': 'ZhangWei',
                    'wecom_chatid': '',
                    'wecom_chat_type': 'single',
                    'is_group': False
                }
            }
        """
        chat_type = (native.meta.get("wecom_chat_type") or "single").strip()
        if chat_type not in ('single', 'group'):
            chat_type = 'single'

        return {
            "target_type": chat_type,
            "send_meta": native.meta,
        }


    async def send_datasource_card(
        self,
        native: NativePayload,
        session: Any,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        """发数据源选择列表（纯文本编号），用户回复编号 -> ``_try_datasource_reply`` 拦截 rebind。

        WeCom template_card 需额外卡片模板配置，第一版用编号文本（与 dingtalk 一致）。
        发送后存 pending，仅对下一条消息生效。
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
        await self.send(native, "\n".join(lines))

        external_key = self.resolve_session_id(native)
        self._pending_ds_select[external_key] = _PendingDsSelect(
            items=list(items),
            expires_at=time.monotonic() + DATASOURCE_SELECT_TIMEOUT_S,
        )
        logger.info(
            f"wecom send_datasource_card: items={len(items)} "
            f"bound={session.datasource_id}"
        )

    # ---- "Thinking…" 占位流 ----

    async def _start_processing(self, frame: Any) -> str:
        """起一条占位流（finish=False），返回 stream_id；失败返回 ""。"""
        from aibot import generate_req_id

        sid = generate_req_id("stream")
        try:
            await self._client.reply_stream(
                frame, stream_id=sid, content=_WECOM_PROCESSING_TEXT, finish=False
            )
            return sid
        except Exception:
            logger.debug("wecom: failed to send processing indicator", exc_info=True)
            return ""

    async def _keepalive_processing(self, frame: Any, stream_id: str) -> None:
        """定时刷新占位流，避免 WeCom 服务端超时；到上限前 force-finish。"""
        elapsed = 0.0
        try:
            while elapsed + _WECOM_PROCESSING_REFRESH_INTERVAL <= _WECOM_PROCESSING_MAX_DURATION:
                await asyncio.sleep(_WECOM_PROCESSING_REFRESH_INTERVAL)
                elapsed += _WECOM_PROCESSING_REFRESH_INTERVAL
                await self._reply_stream(
                    frame, stream_id, _WECOM_PROCESSING_TEXT, finish=False
                )
            # 到上限：收尾这条流，让后续回复起一个新 stream_id。
            await self._reply_stream(
                frame, stream_id, _WECOM_PROCESSING_TEXT, finish=True
            )
            logger.info(
                f"wecom keepalive force-finished after "
                f"{_WECOM_PROCESSING_MAX_DURATION:.0f}s sid={stream_id[:20]}"
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug(f"wecom keepalive failed sid={stream_id[:20]}", exc_info=True)

    async def _cancel_task(self, task: "asyncio.Task[None]") -> None:
        if task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ---- 主消费逻辑（覆盖：套一层占位流保活）----

    async def _consume_one_request(self, native: NativePayload) -> None:
        """在 base 的消费流程外包一层 "Thinking…" 占位流。

        base 无 pre-consume hook，这里在跑 agent 前起占位流；on_streaming_start
        复用其 stream_id（首条 delta 覆写占位气泡），finally 兜底收尾未被复用的流。
        跳过：非流式模式、无文本、/datasource 待选回复（pending 中的编号回复）。
        """
        external_key = self.resolve_session_id(native)
        text = flatten_content_parts_to_str(native.content_parts)
        frame = native.meta.get("wecom_frame")
        started = False
        if (
            text
            and frame
            and self._client
            and self.streaming_enabled
            and external_key not in self._pending_ds_select
        ):
            sid = await self._start_processing(frame)
            logger.info(f'wecom channel, _consume_1_req, sid:{sid}')
            if sid:
                task = asyncio.create_task(self._keepalive_processing(frame, sid))
                self._processing[external_key] = (sid, task)
                started = True

        try:
            await super()._consume_one_request(native)
        finally:
            if started:
                entry = self._processing.pop(external_key, None)
                if entry is not None:
                    # 未被 on_streaming_start 复用 → 兜底收尾（agent 失败/无文本输出）。
                    sid, task = entry
                    keepalive_force_finished = task.done()
                    await self._cancel_task(task)
                    if not keepalive_force_finished:
                        await self._reply_stream(frame, sid, "✅", finish=True)

    # ---- 流式 hooks（reply_stream 覆写气泡）----

    async def on_streaming_start(self, native: NativePayload, msg_id: str) -> Any:
        if not self.streaming_enabled or not self._client:
            return None
        frame = native.meta.get("wecom_frame")
        if not frame:
            return None
        logger.warning(f'wecomChannel on_streaming_start, {native.channel_id}, {native.sender_id}')
        # 复用占位流的 stream_id（首条 delta 覆写 "Thinking…"）；若 keepalive 已
        # force-finished（task done），则起一条新流。
        external_key = self.resolve_session_id(native)
        entry = self._processing.pop(external_key, None)
        if entry is not None:
            sid, task = entry
            if not task.done():
                await self._cancel_task(task)
                stream_id = sid
            else:
                stream_id = _new_stream_id()
        else:
            stream_id = _new_stream_id()

        return {
            "frame": frame,
            "stream_id": stream_id,
            "full_text": "",
            "last_update": 0.0,
        }

    async def on_streaming_delta(
        self, native: NativePayload, handle: Any, delta: str
    ) -> None:
        if not handle or not self._client:
            return
        now = time.monotonic()
        if now - handle.get("last_update", 0.0) < _WECOM_STREAM_MIN_INTERVAL_S:
            return  # 节流
        handle["last_update"] = now
        handle["full_text"] = handle.get("full_text", "") + delta
        await self._reply_stream(
            handle["frame"], handle["stream_id"], handle["full_text"], finish=False
        )

    async def on_streaming_end(
        self, native: NativePayload, handle: Any, full_text: str
    ) -> None:
        if not handle or not self._client:
            # 非流式 fallback：直接发完整文本
            if full_text:
                await self.send(native, full_text)
            return
        await self._reply_stream(
            handle["frame"], handle["stream_id"], full_text or "✅", finish=True
        )

    async def on_streaming_close(
        self, native: NativePayload, handle: Any, summary: str
    ) -> None:
        pass

    async def on_streaming_reasoning_delta(
        self, native: NativePayload, handle: Any, accumulated: str
    ) -> None:
        if not handle or not self._client:
            return
        now = time.monotonic()
        if now - handle.get("last_update", 0.0) < _WECOM_STREAM_MIN_INTERVAL_S:
            return  # 节流
        handle["last_update"] = now
        await self._reply_stream(
            handle["frame"], handle["stream_id"], f"💭 {accumulated}", finish=False
        )
        return None

    async def on_consume_start(self, native: NativePayload):
        """started in _start_processing()"""
        return None

    async def on_consume_end(
        self, native: NativePayload, handle: Any, status: str
    ) -> None:
        return None

    # artifact delivery

    async def send_image(self, native: NativePayload, path: str) -> None:
        """上传图片并以 WeCom image 消息发给客户。"""
        await self._send_media(native, path, "image")

    async def send_file(self, native: NativePayload, path: str) -> None:
        """上传文件并以 WeCom file 消息发给客户"""
        await self._send_media(native, path, "file")

    async def _send_media(
        self, native: NativePayload, path: str, media_type: str
    ) -> None:
        """把位于``path``的文件内容上传到wecom server，并按 ``media_type`` 发送媒体消息。
        """
        media_id = await self._upload_media(path, media_type)
        if not media_id:
            await self.send(native, f"⚠️ 文件发送失败：{Path(path).name}")
            logger.error(f'no media id from upload, {media_type}, {path}')
            return
        msgtype = _MEDIA_MSGTYPE.get(media_type, "file")
        body: Dict[str, Any] = {
            "msgtype": msgtype,
            msgtype: {"media_id": media_id},
        }
        frame = native.meta.get("wecom_frame")
        try:
            if frame and self._client:
                await self._client.reply(frame, body)
            else:
                chatid = native.meta.get("wecom_chatid") or _parse_chatid_from_handle(
                    self.resolve_session_id(native)
                )
                if chatid and self._client:
                    await self._client.send_message(chatid, body)
                else:
                    logger.warning(f"no frame/chatid to reply to, {media_type}, {path}")
        except Exception:
            logger.exception(f"failed to send media, {media_type}, {path}", exc_info=True)
            await self.send(native, f"⚠️ 文件发送失败：{Path(path).name}")


    async def send_clarification_card(self, native: NativePayload, questions: ClarificationQuestionGroup) -> str:
        """Build a multi-selection card object and send it to user.
        Card data structure comes from
        https://developer.work.weixin.qq.com/document/path/101032#%E5%A4%9A%E9%A1%B9%E9%80%89%E6%8B%A9%E6%A8%A1%E7%89%88%E5%8D%A1%E7%89%87
        """
        card_id = create_id('wecom-clarification')
        template_card = {
            "card_type" : "multiple_interaction",
            "source" : {
                "icon_url": "",
                "desc": f'问答卡:{card_id}'
            },
            "main_title" : {
                "title" : questions.title,
                "desc" : ''
            },
            "task_id": card_id,
            "select_list": [],
            # select_list item example:
            # {
            #     "question_key": "question_key1",
            #     "title": "选择器标签1",
            #     "selected_id": "selection_id1",
            #     "option_list": [
            #         {
            #             "id": "selection_id1",
            #             "text": "选择器选项1"
            #         },
            #         {
            #             "id": "selection_id2",
            #             "text": "选择器选项2"
            #         }
            #     ]
            # },
            "submit_button": {
                "text": "提交",
                "key": "submit_key"
            }
        }
        for i, q in enumerate(questions.questions):
            d = {}
            d['question_key'] = str(i)
            d['title'] = q.question
            d['option_list'] = []
            for a in q.options:
                d['option_list'].append({'id': a.label, 'text': f'{a.label}: {a.description}'})
            if d['option_list']:
                d['selected_id'] = d['option_list'][0]['id']
            template_card['select_list'].append(d)

        frame = native.meta.get('wecom_frame', {})
        try:
            rs = await self._client.reply_template_card(frame=frame, template_card=template_card)
            logger.debug(f'wecom sent clarification card, frame:{frame}, card_id:{card_id}, result:{rs}')
            return card_id
        except Exception:
            logger.exception(f'wecom failed to send clarification card, frame:{frame}, card_id:{card_id}, card:{template_card}')
            return ''

    # ---- 媒体上传（WebSocket 分片协议）----

    async def _send_ws_cmd(
        self, cmd: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """发送一条裸 WebSocket 命令帧并等待回执。

        上传命令不走 ``send_reply``（不在 WsCmd 中），故经 ``ws_manager.send``
        发出，并通过 ``start`` 里的 ``on_message`` patch 把回执 resolve 给等待
        中的 future。返回回执 body dict；超时 / errcode!=0 抛错。
        """
        from aibot import generate_req_id

        if not self._client or self._ws_loop is None:
            logger.error(f'wecom ws not initialized, {cmd}')
            raise RuntimeError("wecom ws not initialized")

        req_id = generate_req_id(cmd)
        main_loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Any]" = main_loop.create_future()
        # 注册 future 在 ws_loop 检查之后，避免检查抛错时 future 泄漏。
        self._upload_ack_futures[req_id] = fut

        async def _send() -> None:
            await self._client._ws_manager.send(
                {"cmd": cmd, "headers": {"req_id": req_id}, "body": body},
            )

        try:
            # 在 WS 线程的 loop 上调度发送，主 loop 等回执。
            send_future = asyncio.run_coroutine_threadsafe(_send(), self._ws_loop)
            send_future.add_done_callback(
                lambda f: f.result() if not f.cancelled() else None,
            )
            ack = await asyncio.wait_for(
                asyncio.shield(fut), timeout=_UPLOAD_ACK_TIMEOUT
            )
        finally:
            self._upload_ack_futures.pop(req_id, None)

        errcode = ack.get("errcode", -1)
        if errcode != 0:
            logger.error(f'_send_ws_cmd, {cmd}, errcode:{errcode}, errmsg:{ack.get("errmsg")}')
            raise RuntimeError(
                f"wecom upload cmd={cmd} failed: "
                f"errcode={errcode} errmsg={ack.get('errmsg')}"
            )
        return ack.get("body") or {}

    async def _upload_media(  # pylint: disable=too-many-locals
        self, path: str, media_type: str
    ) -> Optional[str]:
        """通过 WebSocket 分片上传本地文件，返回 media_id；失败返回 None。

        ``media_type`` 为 image / voice / video / file。image 超限会先压缩。
        ``_upload_lock`` 串行化，避免并发上传分片交错。
        """
        if not self._client or not self._upload_lock:
            logger.error(f'_upload_media not ready')
            return None
        p = Path(path)
        if not p.is_file():
            logger.error(f'_upload_media: file not found:{path}')
            return None

        # WeCom 图片限制 2MB，超限压缩；其余类型直读。
        if media_type == "image":
            data, filename = _compress_image_for_wecom(path)
        else:
            data = p.read_bytes()
            filename = p.name

        total_size = len(data)
        md5 = hashlib.md5(data).hexdigest()
        chunks: List[bytes] = [
            data[i : i + _UPLOAD_CHUNK_SIZE]
            for i in range(0, total_size, _UPLOAD_CHUNK_SIZE)
        ]
        total_chunks = len(chunks)
        logger.info(f'_upload_media: media_type:{media_type}, filename:{filename}, '
                    f'size:{total_size}, md5:{md5}, chunk count:{total_chunks}')
        async with self._upload_lock:
            try:
                # Step 1: init
                init_body = await self._send_ws_cmd(
                    _UPLOAD_CMD_INIT,
                    {
                        "type": media_type,
                        "filename": filename,
                        "total_size": total_size,
                        "total_chunks": total_chunks,
                        "md5": md5,
                    },
                )
                upload_id = init_body.get("upload_id", "")
                if not upload_id:
                    raise RuntimeError("wecom upload: empty upload_id")

                # Step 2: chunks
                for idx, chunk in enumerate(chunks):
                    await self._send_ws_cmd(
                        _UPLOAD_CMD_CHUNK,
                        {
                            "upload_id": upload_id,
                            "chunk_index": idx,
                            "base64_data": base64.b64encode(chunk).decode(),
                        },
                    )

                # Step 3: finish
                finish_body = await self._send_ws_cmd(
                    _UPLOAD_CMD_FINISH, {"upload_id": upload_id}
                )
                media_id = finish_body.get("media_id", "")
                if not media_id:
                    logger.error(f'_upload_media: no media id, media_type:{media_type}, filename:{filename}')
                    raise RuntimeError("wecom upload: empty media_id")
                logger.info(f"_upload_media done, media_id={media_id}, media_type={media_type}, filename:{filename}")
                return media_id
            except Exception:
                logger.exception(f'_upload_media failed: type={media_type}, file_name:{filename}', exc_info=True)
                return None

    # ---- 底层发送 ----

    async def _reply_stream(
        self, frame: Any, stream_id: str, content: str, finish: bool
    ) -> None:
        """reply_stream 包装：统一吞异常 + 日志，避免污染上层流式循环。"""
        if not self._client or not stream_id:
            return
        try:
            await self._client.reply_stream(
                frame, stream_id=stream_id, content=content, finish=finish
            )
        except Exception:
            logger.debug(f"wecom reply_stream failed sid={stream_id[:20]}", exc_info=True)


def _new_stream_id() -> str:
    from aibot import generate_req_id

    return generate_req_id("stream")


# WeCom 图片上传限制 2MB，留余量用 1.9MB 作阈值。
_WECOM_IMAGE_MAX_SIZE = 1.9 * 1024 * 1024  # 1.9 MB


def _compress_image_for_wecom(
    image_path: str,
    max_size: float = _WECOM_IMAGE_MAX_SIZE,
) -> tuple[bytes, str]:
    """压缩图片以符合 WeCom 上传大小限制。

    策略：1) PNG/其它格式转 JPEG（通常更小）；2) 仍超限则逐级降 JPEG 质量；
    3) 还不够则逐步缩放。PIL 不可用或压缩失败时返回原始字节。

    Returns:
        (压缩后图片字节, 新文件名)。
    """
    import io

    path = Path(image_path)
    try:
        from PIL import Image
    except ImportError:
        logger.warning("PIL not available, skipping image compression")
        return path.read_bytes(), path.name

    original_data = path.read_bytes()
    original_size = len(original_data)
    if original_size <= max_size:
        return original_data, path.name

    logger.info(
        "wecom compress_image: original size %.2fMB > limit %.2fMB",
        original_size / 1024 / 1024,
        max_size / 1024 / 1024,
    )

    try:
        img = Image.open(io.BytesIO(original_data))

        # 透明 PNG 等转 RGB（白底），其余非 RGB 也转 RGB。
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(
                img,
                mask=img.split()[-1] if "A" in img.mode else None,
            )
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        new_filename = path.stem + ".jpg"

        # 逐级降质量。
        for quality in (85, 70, 50, 30):
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= max_size:
                logger.info(
                    "wecom compress_image: compressed to %.2fMB (quality=%d)",
                    len(data) / 1024 / 1024,
                    quality,
                )
                return data, new_filename

        # 仍超限：逐步缩放。
        width, height = img.size
        for scale in (0.75, 0.5, 0.25):
            new_width = int(width * scale)
            new_height = int(height * scale)
            resized = img.resize(
                (new_width, new_height),
                Image.Resampling.LANCZOS,
            )
            buffer = io.BytesIO()
            resized.save(buffer, format="JPEG", quality=70, optimize=True)
            data = buffer.getvalue()
            if len(data) <= max_size:
                logger.info(
                    "wecom compress_image: resized to %dx%d, %.2fMB",
                    new_width,
                    new_height,
                    len(data) / 1024 / 1024,
                )
                return data, new_filename

        # 返回能拿到的最小版本。
        logger.warning(
            "wecom compress_image: could not compress below limit, "
            "returning smallest version (%.2fMB)",
            len(data) / 1024 / 1024,
        )
        return data, new_filename
    except Exception:
        logger.exception("wecom compress_image failed, using original")
        return original_data, path.name

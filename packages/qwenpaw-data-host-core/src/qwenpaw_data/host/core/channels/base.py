# -*- coding: utf-8 -*-
"""Channel base: inbound IM messages → ChatRuntime turns → outbound rendering."""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.api.models.chat import (
    AskUserQuestionAnswerSchema,
    AskUserQuestionAnsweredResultSchema,
)
from qwenpaw_data.host.core.channels.clarification_question import (
    ClarificationQuestionGroup,
    build_clarification_questions,
)
from qwenpaw_data.host.core.channels.schema import (
    NativePayload,
    flatten_content_parts_to_str,
)
from qwenpaw_data.host.core.channels.segment_markup import render_segment_spans
from qwenpaw_data.host.core.cm_client import (
    API_TOKEN_ENV,
    CLIENT_API_TOKEN_ENV,
    ContextManagerClient,
)
from qwenpaw_data.host.core.domain.chat import ACTIVE
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.stream.hub import get_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream

logger = logging.getLogger("qwenpaw_data.channels.base")

# /datasource text-number selection: pending fallback timeout (user sends
# /datasource then walks away).
DATASOURCE_SELECT_TIMEOUT_S = 300

FOLLOWUP_ENABLED_ENV = "QWENPAW_DATA_FOLLOWUP_ENABLED"

_IMAGE_ARTIFACT_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

_SKIP_ARTIFACT_EXTS = frozenset({".csv", ".tsv", ".parquet", ".xlsx"})

__HELP_COMMANDS_MENU = {
    '/help': '列出所有可用的命令',
    'help': '列出所有可用的命令',
    'hello': '列出所有可用的命令',
    '你好': '列出所有可用的命令',
    '帮助': '列出所有可用的命令',
    '?': '列出所有可用的命令',
    '？': '列出所有可用的命令',
}
_HELP_COMMANDS = set(__HELP_COMMANDS_MENU.keys())
_CONTROL_COMMANDS_HELP_MENU = {
    '/stop': '停止当前运行中的任务',
    '/datasource': '列出所有的数据源',
    '/session': '创建新的会话',
} | __HELP_COMMANDS_MENU
_CONTROL_COMMANDS = set(_CONTROL_COMMANDS_HELP_MENU.keys())


@dataclass
class ChannelServices:
    """Host services a channel needs, injected by the ChannelManager.

    Mirrors the enterprise edition's DB-session/repository access with the
    OSS store layer so ``BaseChannel`` stays free of persistence details.
    """

    sessions: Any
    chats: Any
    events: Any
    bindings: Any
    configs: Any
    hosts: Any
    prefs: Any = None
    settlement: Any = None

    def chat_runtime(self) -> ChatRuntime:
        return ChatRuntime(
            chats=self.chats,
            events=self.events,
            hosts=self.hosts,
            prefs=self.prefs,
            sessions=self.sessions,
            settlement=self.settlement,
        )


def _followup_enabled() -> bool:
    raw = (os.environ.get(FOLLOWUP_ENABLED_ENV) or "1").strip().lower()
    return raw not in {"0", "false", "off"}


def _extract_failure_message(obj: Any) -> str | None:
    err = getattr(obj, "error", None)
    if err is None:
        return None
    msg = getattr(err, "message", None)
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return str(err) or None


@dataclass
class _PendingDsSelect:
    """Pending state awaiting a user's numeric reply after ``/datasource``."""

    items: list[Any] = field(default_factory=list)
    expires_at: float = 0.0


@dataclass
class _ClarificationCardPending:
    """Context needed to route a card-submit callback back to the runtime.

    Keyed by the card ``task_id`` returned from ``send_clarification_card`` so a
    channel that receives the submit out-of-band (e.g. WeCom's
    ``template_card_event``) can resolve session/chat/group without a text reply.
    """

    session_id: str
    chat_id: str
    questions: ClarificationQuestionGroup


def _extract_first_word(text: str) -> str:
    if not text:
        return text
    return (text.lstrip().split(None, 1)[0]).lower()


def is_control_command(text: str) -> bool:
    """prefixed control command."""
    if not text:
        return False
    first = _extract_first_word(text)
    return first in _CONTROL_COMMANDS


def _is_image_artifact(name: str) -> bool:
    """Whether a session artifact is an image."""
    return Path(name).suffix.lower() in _IMAGE_ARTIFACT_EXTS


def _is_skipped_artifact(name: str) -> bool:
    """Whether an artifact is suppressed on IM (raw datasets the user fetches
    from webui, not chat). See ``_SKIP_ARTIFACT_EXTS``.
    """
    return Path(name).suffix.lower() in _SKIP_ARTIFACT_EXTS


_LOCAL_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\((/[^\)]*)\)")


def degrade_local_image_md(text: str) -> str:
    """Replace local-path markdown image refs with a visible inline-code span.

    A ``![alt](/local/path)`` can't render in an IM streaming card — feishu
    CardKit wants an uploaded image_key, dingtalk a media_id, not a
    server-local path. Worse, feishu CardKit *validates* the content and
    rejects the whole element-content update once the image syntax closes,
    freezing the card mid-answer. Replacing (not removing) the image syntax
    with an inline-code path keeps the reference visible to the user instead
    of having it vanish; the image, if deliverable, is still sent separately
    via ``send_image`` (artifact delivery: upload -> image_key/media_id).
    """
    return _LOCAL_IMAGE_MD_RE.sub(lambda m: f"`({m.group(1)})`", text)


def _format_segment_text(seg: Any) -> str:
    """Render a BizTrace segment as a plain-text IM message."""
    title = str(getattr(seg, "title", "") or "").strip()
    parts: list[str] = []
    if title:
        dur = ""
        started = getattr(seg, "started_at", None)
        ended = getattr(seg, "ended_at", None)
        if started and ended and ended > started:
            secs = int(round(ended - started))
            if secs >= 60:
                dur = f"  用时 {secs // 60}分{secs % 60}秒"
            else:
                dur = f"  用时 {secs}秒"
        parts.append(title + dur)
    for label, attr in (("输入", "input"), ("执行", "behavior"), ("结论", "conclusion")):
        body = str(getattr(seg, attr, None) or "").strip()
        if body:
            parts.append(f"{label}\n{render_segment_spans(body, target='plain')}")

    for artifact in (getattr(seg, 'artifact', []) or []):
        a_name = str(getattr(artifact, 'name', '') or '').strip()
        a_desc = str(getattr(artifact, 'description', '') or '').strip()
        a_text = ''
        if a_name:
            a_text = a_name
            if a_desc:
                a_text += f':{a_desc}'
            parts.append(f'关键产物 {a_text}')
    return "\n\n".join(parts)


class BaseChannel(abc.ABC):
    """channel"""

    channel: str = ""
    streaming_enabled: bool = False

    def __init__(self) -> None:
        # Host event loop, bound by the manager in ``start_all``. Inbound handlers run on
        # SDK threads (feishu ws / dingtalk stream / wecom ws / wechat poll), so they hand
        # work to the host loop via ``self._loop.call_soon_threadsafe``.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Per-session serial queues: ``{session_id: (Queue, consumer_task)}``. A single
        # consumer per session awaits ``_consume_one_request`` one at a time; the next
        # message is dequeued only after the previous returns (ChatRuntime finishes), so a
        # fast follow-up never hits ``has_active_chat`` CONFLICT. Keyed by external_key
        # (``resolve_session_id``); each channel owns its own queues, so two channels never
        # share a consumer (and their external_keys are channel-prefixed anyway).
        self._session_queues: dict[
            str, tuple[asyncio.Queue[Any], asyncio.Task[None]]
        ] = {}
        self._pending_ds_select: dict[str, _PendingDsSelect] = {}
        # {card task_id, _ClarificationCardPending} — answered via the card's
        # own submit callback (platforms that push card events, e.g. WeCom).
        self._pending_clarification_cards: dict[str, _ClarificationCardPending] = {}
        self._owner_identity: Identity | None = None
        self._services: ChannelServices | None = None
        # Per-chat delivered artifact paths (de-dup: the runtime re-registers a
        # file on size/mtime change, but we push each only once).
        self._delivered_artifacts: set[str] = set()

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the channel (runs the SDK WebSocket on a daemon thread)."""

    @abc.abstractmethod
    async def _stop(self) -> None:
        """Platform-specific SDK teardown (stop ws/poll threads, drop clients).
        Implemented by each subclass.
        """

    async def stop(self) -> None:
        """Stop the channel: SDK teardown, then per-session queue teardown."""
        try:
            await self._stop()
        finally:
            await self._stop_session_queues()

    async def health_check(self) -> bool:
        return True

    # ---- manager injection ----

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_owner_identity(self, identity: Identity) -> None:
        self._owner_identity = identity

    def set_services(self, services: ChannelServices) -> None:
        self._services = services

    @property
    def services(self) -> ChannelServices:
        if self._services is None:
            raise RuntimeError(
                f"channel {self.channel} has no services bound; "
                "the ChannelManager must call set_services() before start()"
            )
        return self._services

    @property
    def _user_id(self) -> str:
        return (self._owner_identity or Identity.anonymous()).user_id

    def owner_lookup_id(self) -> str:
        """This channel's own platform id (app_id / client_id / bot_id).

        Subclasses override to return their id field; used for logging and the
        config reverse lookup.
        """
        return ""

    @abc.abstractmethod
    def extract_target_meta(self, native: NativePayload) -> dict[str, Any] | None:
        """Extract attributes for building an IM outbound message when handling a
        cron job run. Saved on the channel binding.
        """

    # ---- inbound path ----
    @abc.abstractmethod
    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        """Make a cron job request as a message from IM user. enqueue it. Let channel handle it."""

    def _enqueue(self, native: NativePayload) -> None:
        """Called by inbound handlers in subclasses. Route ``native`` onto the host loop.

        Control commands bypass the queue; normal messages enter the per-session
        serial queue. Both are scheduled via ``call_soon_threadsafe`` because this
        runs on the channel's SDK thread, not the host event loop.
        """
        external_key = self.resolve_session_id(native)
        text = flatten_content_parts_to_str(native.content_parts)
        logger.info(
            "inbound message: channel=%s external_key=%s sender=%s text=%r",
            self.channel,
            external_key,
            native.sender_id,
            text,
        )
        if self._loop is None:
            logger.warning("host loop not bound: channel=%s", self.channel)
            return
        if is_control_command(text):
            # Bypass the queue and schedule handling directly.
            self._loop.call_soon_threadsafe(
                asyncio.create_task,
                self._handle_control_command(native, text),
            )
            return
        # Normal messages enter the per-session serial queue.
        self._loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self._enqueue_session(external_key, native),
        )

    # ---- per-session serial queue ----

    async def _enqueue_session(self, session_id: str, native: NativePayload) -> None:
        """Enqueue a message for ``session_id`` on this channel."""
        queue, _ = await self._ensure_session_consumer(session_id)
        await queue.put(native)

    async def _ensure_session_consumer(
        self, session_id: str
    ) -> tuple[asyncio.Queue[Any], asyncio.Task[None]]:
        entry = self._session_queues.get(session_id)
        if entry is not None:
            return entry
        queue: asyncio.Queue[Any] = asyncio.Queue()
        task = asyncio.create_task(self._consume_session_loop(session_id, queue))
        self._session_queues[session_id] = (queue, task)
        logger.info(
            "session queue consumer started: session=%s channel=%s",
            session_id, self.channel,
        )
        return self._session_queues[session_id]

    async def _consume_session_loop(
        self, session_id: str, queue: asyncio.Queue[Any]
    ) -> None:
        """Single consumer per session: await ``_consume_one_request`` one at a time.

        The next item is dequeued only after the previous finishes, so a fast
        follow-up never races a still-running chat into ``has_active_chat``.
        """
        while True:
            native = await queue.get()
            if native is None:
                break
            try:
                await self._consume_one_request(native)
            except Exception:
                logger.exception(
                    "session queue consume failed: session=%s channel=%s",
                    session_id, self.channel,
                )

    async def _clear_session_queue(self, session_id: str) -> int:
        """Drop queued messages for ``session_id`` (used by ``/stop``).

        Returns the count dropped. An already-dequeued, in-flight
        ``_consume_one_request`` is left to finish naturally.
        """
        entry = self._session_queues.get(session_id)
        if entry is None:
            return 0
        queue, _ = entry
        n = 0
        while not queue.empty():
            try:
                queue.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                break
        logger.info("session queue cleared: session=%s count=%d", session_id[:30], n)
        return n

    async def _stop_session_queues(self) -> int:
        """Stop every consumer for this channel (across all its sessions).

        Used on stop/reload: the channel instance is being discarded, so all its
        per-session queues are torn down. An in-flight ``_consume_one_request``
        finishes naturally (no hard cancel — that would orphan a running chat);
        idle consumers stop on the next ``queue.get()`` via the ``None``
        sentinel. Returns the number of queues torn down.
        """
        keys = list(self._session_queues.keys())
        for session_id in keys:
            queue, _ = self._session_queues.pop(session_id)
            await queue.put(None)
        logger.info("session queues stopped for channel=%s: count=%d", self.channel, len(keys))
        return len(keys)

    @abc.abstractmethod
    def resolve_session_id(self, native: NativePayload) -> str:
        """Resolve the external_session_key from the native payload (e.g. ``feishu:<chat_id>``)."""

    async def _resolve_active_session(self, external_key: str) -> Session | None:
        """Active session for an external key, via the channel binding pointer.

        Returns None if no binding exists, or if the bound session is gone
        (stale pointer). ``Session`` itself stays unaware of IM routing —
        only this adapter layer touches the binding store.
        """
        sid = await self.services.bindings.get_active_session_id(
            self._user_id, self.channel, external_key
        )
        if sid is None:
            return None
        try:
            return await self.services.sessions.get(sid)
        except LookupError:
            return None

    async def _load_target_send_meta(self, external_key: str) -> dict[str, Any]:
        """Load the ``send_meta`` recorded for a target (group/person) by its external_key.

        Used by cron ``inject_cron_job`` to rebuild a synthetic inbound payload when the
        external_key alone does not encode the full send address (e.g. dingtalk). Reads
        from the channel binding (the send address is folded in there on inbound), so no
        separate target table is needed. Returns ``{}`` when the target is unknown.
        """
        if not external_key:
            return {}
        meta = await self.services.bindings.get_target_meta(
            self._user_id, self.channel, external_key
        )
        return (meta or {}).get("send_meta") or {}

    @abc.abstractmethod
    async def send(self, native: NativePayload, text: str) -> None:
        """Send a text reply back to the platform."""

    # ---- streaming hooks (overridden by dingtalk/feishu/wecom/wechat) ----

    @abc.abstractmethod
    async def on_streaming_start(self, native: NativePayload, msg_id: str) -> Any:
        """Pre-create the streaming card/bubble before the first text delta.

        Returns an opaque, platform-specific handle passed to the subsequent
        ``on_streaming_*`` hooks (or ``None`` when streaming is unavailable).
        """

    @abc.abstractmethod
    async def on_streaming_delta(
        self, native: NativePayload, handle: Any, delta: str
    ) -> None:
        """Render an incremental text delta into the streaming card/bubble."""

    @abc.abstractmethod
    async def on_streaming_end(
        self, native: NativePayload, handle: Any, full_text: str
    ) -> None:
        """Finalize the streaming card/bubble with the complete response text."""

    @abc.abstractmethod
    async def on_streaming_close(
        self, native: NativePayload, handle: Any, summary: str
    ) -> None:
        """Seal the current thinking card mid-turn at a segment boundary."""

    @abc.abstractmethod
    async def on_streaming_reasoning_delta(
        self, native: NativePayload, handle: Any, accumulated: str
    ) -> None:
        """Reasoning text accumulated so far; override to render 💭 thinking live."""

    async def on_streaming_round_reset(
        self, native: NativePayload, handle: Any
    ) -> None:
        """A new agent round began mid-turn; clear per-round card accumulators."""
        if handle:
            handle["full_text"] = ""

    @abc.abstractmethod
    async def on_consume_start(self, native: NativePayload) -> None:
        """Called right before the agent runs for an inbound message.

        Best-effort immediate ACK (e.g. a DingTalk emoji reaction) so the user
        sees feedback before the first streaming event. Default no-op.
        """

    @abc.abstractmethod
    async def on_consume_end(
        self, native: NativePayload, handle: Any, status: str
    ) -> None:
        """Called once the response terminates (completed/failed/cancelled)."""

    # ---- artifact delivery (IM subclasses implement; driven by
    # artifact.registered events) ----

    @abc.abstractmethod
    async def send_image(self, native: NativePayload, path: str) -> None:
        """Deliver an image file to the IM chat."""

    @abc.abstractmethod
    async def send_file(self, native: NativePayload, path: str) -> None:
        """Deliver a non-image file to the IM chat."""

    @abc.abstractmethod
    async def send_clarification_card(
        self, native: NativePayload, questions: ClarificationQuestionGroup
    ) -> str:
        """Deliver a selection-card to the IM user.
        Return a unique card id if the card is delivered successfully, None or empty string otherwise.
        """

    async def send_segment(self, native: NativePayload, seg: Any) -> None:
        """Render a BizTrace segment to the user."""
        text = _format_segment_text(seg)
        if text:
            await self.send(native, text)

    # ---- recommended follow-up questions ----

    async def _maybe_send_followups(self, native: NativePayload, chat_id: str) -> None:
        """Push the recommended follow-up questions after a completed turn.

        Reads the ``followup.generated`` events the runtime persisted for this
        chat and hands the questions to ``send_followups``. Best-effort: any
        failure is logged and swallowed so a followup card can never break the
        conversation.
        """
        try:
            if not _followup_enabled():
                return
            questions: list[str] = []
            for obj in await self.services.events.read_after(chat_id, -1):
                if getattr(obj, "object", None) == "followup.generated":
                    questions = list(obj.followup.questions)
            if not questions:
                return
            await self.send_followups(native, questions)
        except Exception:
            logger.exception(
                "channel %s: send followups failed for chat %s",
                self.channel,
                chat_id,
            )

    async def send_followups(self, native: NativePayload, questions: list[str]) -> None:
        """Render recommended follow-up questions for the user to click.

        Default no-op; Feishu overrides with a clickable card. DingTalk/WeCom
        can follow with a numbered plain-text list first, buttons later.
        """

    # ---- control commands ----

    async def _handle_control_command(
        self, native: NativePayload, text: str
    ) -> bool:
        """Handle control commands bypassing the queue. Returns True if handled (not enqueued)."""
        if not is_control_command(text):
            return False
        first = _extract_first_word(text)
        if first == "/stop":
            await self._handle_stop(native)
            return True
        if first == "/datasource":
            await self._handle_datasource(native)
            return True
        if first == "/session":
            await self._handle_session(native)
            return True
        if first in _HELP_COMMANDS:
            await self.send(
                native,
                "预置命令列表:\n"
                f"{json.dumps(_CONTROL_COMMANDS_HELP_MENU, ensure_ascii=False, indent=2)}")
            return True
        return False

    async def _handle_stop(self, native: NativePayload) -> None:
        external_key = self.resolve_session_id(native)
        session = await self._resolve_active_session(external_key)
        if session is None:
            await self.send(native, "无运行中任务")
            return
        chats = self.services.chats
        active = await chats.get_active_for_session(session.id)
        if active is None:
            await self.send(native, "无运行中任务")
            return
        runtime = get_runtime_registry().get(active.id)
        if runtime is not None:
            await runtime.cancel()
            active = await chats.get(active.id, session_id=session.id)
        # Live cancel can return with status still running; force store cancel.
        if active.status == "running":
            await OutputStream(
                self.services.events,
                session_id=session.id,
                chat_id=active.id,
                identity=active.identity,
            ).response_cancelled()
            active.cancel()
            await chats.reload_event_watermark(active)
            await chats.save(active)
        if self._session_queues:
            # The serial queue is keyed by external_key (``resolve_session_id``),
            # not session.id — clearing by session.id would miss and leave queued
            # messages draining.
            await self._clear_session_queue(external_key)
        await self.send(native, "已停止")

    async def _handle_datasource(self, native: NativePayload) -> None:
        """``/datasource``: pop the datasource card — selectable if unbound, view-only if bound.

        Bypasses the queue, no chat, no agent. If no session yet, ask the user to send a message
        first (which would create one and proactively pop the card).
        """
        external_key = self.resolve_session_id(native)
        logger.info("datasource cmd: channel=%s external_key=%s", self.channel, external_key)
        session = await self._resolve_active_session(external_key)
        if session is None:
            logger.info("datasource cmd: no session, asking to send a message first")
            await self.send(native, "会话未建立，请先发一条消息")
            return
        await self._send_datasource_selection(native, session)

    async def _handle_session(self, native: NativePayload) -> None:
        """``/session``: start a new session in this IM conversation.

        Creates a fresh session and points the channel binding at it; the old
        session is retained for webui. Created unbound, so
        ``_send_datasource_selection`` pops a selectable card. Refused while the
        active session has a running chat (serial queue).
        """
        external_key = self.resolve_session_id(native)
        logger.info("session cmd: channel=%s external_key=%s", self.channel, external_key)
        sessions = self.services.sessions
        active = await self._resolve_active_session(external_key)
        if active is not None:
            if await self.services.chats.get_active_for_session(active.id) is not None:
                await self.send(native, "当前有任务正在运行，请先 /stop 再新建会话")
                return
        session = Session.create(
            identity=self._owner_identity or Identity.anonymous(),
            title=external_key,
            channel=self.channel,
        )
        await sessions.add(session)
        await self.services.bindings.point_to(
            self._user_id,
            self.channel,
            external_key,
            session.id,
            target_meta=self.extract_target_meta(native),
            display_name=session.id,
        )
        logger.info("session cmd: created session=%s", session.id)
        await self._send_datasource_selection(native, session)

    def _resolve_access_token(self) -> str | None:
        """CM token from the environment; None means an auth-less local CM."""
        return (
            (os.environ.get(CLIENT_API_TOKEN_ENV) or "").strip()
            or (os.environ.get(API_TOKEN_ENV) or "").strip()
            or None
        )

    async def _send_datasource_selection(
        self, native: NativePayload, session: Session
    ) -> None:
        """Fetch the datasource list and pop the card / fallback text.

        Shared by ``/datasource`` and the proactive pop on a normal message without a bound
        datasource. ``selectable`` is derived from ``session.datasource_id is None``: unbound →
        selectable card (first-time bind on click); bound → view-only card (clicks rejected).
        """
        from fastapi.concurrency import run_in_threadpool

        try:
            client = ContextManagerClient(api_token=self._resolve_access_token())
            result = await run_in_threadpool(client.list_datasources)
        except Exception:
            logger.exception("datasource list failed: channel=%s", self.channel)
            await self.send(native, "数据源列表获取失败，请稍后重试")
            return
        items = result.items if result is not None else []
        logger.info(
            "datasource selection: session=%s bound=%s list_total=%s",
            session.id, session.datasource_id, len(items),
        )
        if not items:
            await self.send(native, "暂无可用数据源，请先在管控台添加")
            return
        # CardKit limits buttons per card; cap at 10.
        selected = items[:10]
        await self.send_datasource_card(
            native, session, items=selected, selectable=session.datasource_id is None
        )

    @abc.abstractmethod
    async def send_datasource_card(
        self,
        native: NativePayload,
        session: Session,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        """Send the datasource selection card (overridden by subclasses).

        ``selectable=True`` → buttons bind on click (first-time only); ``selectable=False`` →
        view-only (buttons rendered but clicks rejected).
        """
        raise NotImplementedError

    # ---- main consume logic ----

    async def _try_datasource_reply(self, native: NativePayload) -> bool:
        """Intercept the message right after ``/datasource``: bind on a plain in-range number.

        First-time bind only — rebinding is no longer allowed. ``pending`` is stored only when the
        card was selectable (session unbound), so this normally fires once for the initial binding;
        the ``datasource_id is not None`` guard defends against races. Pending is cleared
        immediately after handling (hit or miss) — only the next message is honored. Feishu uses
        card buttons and never stores pending, so this always returns False for Feishu.
        """
        external_key = self.resolve_session_id(native)
        pending = self._pending_ds_select.pop(external_key, None)
        if pending is None:
            return False
        text = flatten_content_parts_to_str(native.content_parts).strip()
        if (
            time.monotonic() > pending.expires_at
            or not text.isdigit()
        ):
            return False
        n = int(text)
        if n < 1 or n > len(pending.items):
            return False

        item = pending.items[n - 1]
        ds_id = str(getattr(item, "datasource_id", "") or "").strip()
        ds_name = str(getattr(item, "datasource_name", "") or ds_id)
        if not ds_id:
            return False
        try:
            session = await self._resolve_active_session(external_key)
            if session is None:
                await self.send(native, "会话未找到，请重新发送消息")
                return True
            if session.datasource_id is not None:
                # Already bound — rebind no longer allowed.
                await self.send(native, "已绑定数据源，不可更改")
                return True
            session.bind_datasource(ds_id)
            await self.services.sessions.save(session)
            await self.send(native, f"已绑定 {ds_name}，现在可以提问了")
        except Exception:
            logger.exception(f"datasource reply bind failed: channel={self.channel}")
            await self.send(native, "数据源绑定失败，请重试")
        return True

    async def _submit_clarification_card_answer(
        self,
        card_id: str,
        selections_by_key: dict[str, list[str]],
    ) -> bool:
        """Submit a card's selections to the runtime clarification handler.

        ``selections_by_key`` maps the per-question key the card carries (the
        ``question_key`` each platform assigns in ``send_clarification_card``,
        e.g. WeCom uses ``str(index)``) to the list of selected option labels.
        Returns True if a pending card was found and the answer was submitted
        (regardless of downstream success), False if no pending card matched.

        selections_by_key example: {'0': ['滚动 30 天'], '1': ['地区']}
        """
        pending = self._pending_clarification_cards.pop(card_id, None)
        if pending is None:
            logger.info(f'clarification card answer for unknown/expired card_id:{card_id}')
            return False

        group = pending.questions
        # Map question_key (str index) -> question text + selected option labels.
        answers: list[AskUserQuestionAnswerSchema] = []
        for idx, question in enumerate(group.questions):
            selected = selections_by_key.get(str(idx)) or []
            # Option ids sent in the card == option labels (see send_clarification_card).
            answers.append(
                AskUserQuestionAnswerSchema(
                    question=question.question,
                    selected_options=list(selected),
                    custom_text=None,
                )
            )
        logger.debug(f'_submit_clarification_card_answer, pending:{asdict(pending)}')

        result = AskUserQuestionAnsweredResultSchema(
            status="answered",
            answers=answers,
        )
        try:
            runtime = get_runtime_registry().get(pending.chat_id)
            if runtime is None:
                raise LookupError(
                    f"no running chat runtime for chat {pending.chat_id}"
                )
            runtime.answer(
                clarification_id=group.id,
                result=result.model_dump(mode="json"),
            )
            logger.info(
                f'clarification card {card_id} answered, '
                f'session:{pending.session_id}, chat:{pending.chat_id}'
            )
        except Exception:
            logger.exception(
                f'failed to answer clarification card {card_id}, '
                f'session:{pending.session_id}, chat:{pending.chat_id}'
            )
        return True

    async def _consume_one_request(self, native: NativePayload) -> None:
        """Create/get Session+Chat → trigger ChatRuntime.run → subscribe EventHub → send to platform."""
        if await self._try_datasource_reply(native):
            return
        external_key = self.resolve_session_id(native)
        text = flatten_content_parts_to_str(native.content_parts)
        if not text:
            logger.error(f'no content in native payload, channel:{native.channel_id}, sender:{native.sender_id}')
            return

        sessions = self.services.sessions
        cron_ds = (native.meta.get("cron_datasource_id") or "").strip() or None
        session = await self._resolve_active_session(external_key)
        if session is None:
            session = Session.create(
                identity=self._owner_identity or Identity.anonymous(),
                title=text[:30],
                channel=self.channel,
                datasource_id=cron_ds,
            )
            await sessions.add(session)
            logger.info(f'created new session for request, channel:{native.channel_id}, sender:{native.sender_id}, new session id:{session.id}')
        elif cron_ds and session.datasource_id is None:
            # Reused session never had a datasource bound (the user walked
            # away after /datasource). Bind the cron's so this run doesn't
            # get discarded into the selection-card branch below. If the
            # session already has one, leave it — the existing flow uses
            # ``session.datasource_id`` for ``open_chat``.
            session.bind_datasource(cron_ds)
            await sessions.save(session)
        # Refresh the send address (and active-session pointer) on the binding so a
        # later cron job can read send_meta. ``extract_target_meta`` returns None for
        # control messages; ``point_to`` still updates ``active_session_id`` then.
        await self.services.bindings.point_to(
            self._user_id,
            self.channel,
            external_key,
            session.id,
            target_meta=self.extract_target_meta(native),
            display_name=session.id,
        )
        # No datasource bound yet → proactively pop the selection card instead of running
        # the agent. The original message is dropped (user re-asks after binding).
        if session.datasource_id is None:
            await self._send_datasource_selection(native, session)
            logger.error(f'session has no datasource, channel:{native.channel_id}, sender:{native.sender_id}, session:{session.id}')
            return

        chat = session.open_chat(
            text=text,
            datasource_id=session.datasource_id,
            has_active_chat=await sessions.has_active_chat(session.id),
        )
        await sessions.save(session)
        await self.services.chats.add(chat)
        session_id = session.id
        chat_id = chat.id

        await self.on_consume_start(native)

        # Resolve this session's artifact dir once; ``artifact.registered``
        # events carry rel paths that we resolve against it to push files to IM.
        artifact_dir = Path(
            self.services.hosts.get(session_id=session.id).paths.artifact_dir
        )
        identity = self._owner_identity or Identity.anonymous()
        run_task = asyncio.create_task(
            self.services.chat_runtime().run(chat_id, identity=identity)
        )
        try:
            await self._stream_to_platform(native, chat_id, session_id, artifact_dir)
        finally:
            await self._finalize_chat_run(chat_id, session_id, run_task, native)

    async def _stream_to_platform(
        self, native: NativePayload, chat_id: str, session_id: str, artifact_dir: Path
    ) -> None:
        """Consume the EventHub stream and map events to platform actions.

        Streaming card lifecycle:
        - ``response.in_progress``: pre-create the card with a placeholder so the
          user sees it immediately (no silence before the first text delta).
        - ``reasoning`` message deltas: render live into the same card with a 💭
          prefix (``on_streaming_reasoning_delta``) — visible thinking.
        - ``message`` (final answer) deltas: render into the same card, replacing
          the reasoning content. Only this text is kept on ``response`` finalize.
        - ``biz_event`` with ``card_type=="tool"`` carries a real-time,
          human-readable caption per tool call.
          Each caption replaces the thinking card's content as a one-shot, so the
          user sees live activity through long tool calls.
        """
        hub = get_hub()
        handle: Any = None
        full_text: list[str] = []
        reasoning_text: list[str] = []
        # "💭 1 - …", "💭 2 - …".
        think_seq = 0
        # Once the final-answer message starts streaming, segment boundaries no
        # longer seal the thinking card — a segment arriving mid-answer must not
        # split the answer across two cards. Latched on the first ``message``
        # text delta and held for the rest of the turn.
        answer_streaming = False
        # msg_id -> "reasoning" | "message": whitelists which text deltas to render.
        # Reasoning shares the same content ``type=="text"`` channel as the final
        # message; its message_start carries ``type=="reasoning"`` and is recorded
        # here so its deltas route to the 💭 path instead of being dropped.
        assistant_msg_ids: dict[str, str] = {}
        terminated = False

        pending_artifacts: list[tuple[str, bool]] = []
        async for obj in hub.subscribe_live(chat_id):
            if obj is None:
                # transport heartbeat
                continue
            object_type = getattr(obj, "object", None)
            status = getattr(obj, "status", None)
            if object_type == "artifact.registered":
                # A file appeared in the session artifact dir.
                art = getattr(obj, "artifact", None)
                rel_path = getattr(art, "path", "") if art else ""
                if not rel_path:
                    continue
                if rel_path in self._delivered_artifacts:
                    continue
                name = Path(rel_path).name
                if _is_skipped_artifact(name):
                    continue
                self._delivered_artifacts.add(rel_path)
                abs_path = str(artifact_dir / rel_path)
                pending_artifacts.append((abs_path, _is_image_artifact(name)))
                continue
            if object_type == "segment":
                # BizTrace segment — render to IM. Channels override
                # ``send_segment`` for card layouts; base sends plain text.
                #
                # A segment is a work-phase boundary. If the current card is
                # still in the exploratory 💭 stage (no final answer streaming
                # yet), seal it in place so the segment card and any later
                # exploration land below it on a fresh card — otherwise the
                # next 💭 caption overwrites the card sitting *above* the
                # segment, which reads as activity jumping back in time.
                if (
                    self.streaming_enabled
                    and handle is not None
                    and not answer_streaming
                ):
                    summary = "".join(reasoning_text) or "💭"
                    await self.on_streaming_close(native, handle, summary)
                    handle = None
                    reasoning_text = []
                seg = getattr(obj, "segment", None)
                if seg is not None:
                    await self.send_segment(native, seg)
                # Flush artifacts produced during this segment's work right
                # after its conclusion card
                await self._deliver_pending_artifacts(native, pending_artifacts)
                pending_artifacts.clear()
                continue
            if object_type == "biz_event":
                # Real-time tool/activity signal. The algo side emits a
                # ``biz_event`` with ``card_type=="tool"`` (caption like
                # "查询产品X用户GAAP趋势") as each tool runs — including the
                # long ``search_context`` call that can span minutes with no
                # other event in between. Route these captions into the 💭
                # card so the user sees live activity through the silent gap
                # instead of an empty card.
                #
                # Only ``tool`` captions carry real-time activity; ``user``
                # (input echo), ``text`` (post-answer summary) and ``hint``
                # are not exploration and stay out of the thinking card.
                #
                # The caption is a one-shot full-replace of the thinking text:
                # it resets ``reasoning_text`` so the next caption (or the next
                # reasoning/message block's deltas) replaces rather than
                # appends to it.
                be = getattr(obj, "biz_event", None)
                pres = getattr(be, "presentation", None) if be else None
                card_type = (
                    getattr(pres, "card_type", None)
                    if not isinstance(pres, dict)
                    else pres.get("card_type")
                )
                if card_type == "tool" and self.streaming_enabled:
                    caption = (
                        getattr(pres, "caption", "")
                        if not isinstance(pres, dict)
                        else pres.get("caption", "")
                    )
                    caption = (caption or "").strip()
                    if caption:
                        think_seq += 1
                        labeled = f"{think_seq} - {caption}"
                        if handle is None:
                            handle = await self.on_streaming_start(
                                native, ""
                            )
                        reasoning_text = [labeled]
                        await self.on_streaming_reasoning_delta(
                            native, handle, labeled
                        )
                continue
            if object_type == "response" and status == "in_progress":
                # Pre-create the card once, before any text delta, so the user
                # sees immediate feedback instead of silence through reasoning.
                if self.streaming_enabled and handle is None:
                    handle = await self.on_streaming_start(native, "")
                continue
            if object_type == "message" and status == "in_progress":
                mtype = getattr(obj, "type", None)
                if mtype in ("message", "reasoning"):
                    msg_id = getattr(obj, "id", "")
                    # A new assistant message arriving *after* an answer has
                    # already streamed marks a new agent round (the model ran
                    # another tool loop and is exploring again). Drop the
                    # prior round's accumulated reasoning/answer text so the
                    # card shows only the current round — otherwise each
                    # round's deltas append to the previous round's, the same
                    # reasoning text is reprinted over and over, and the card
                    # grows toward the platform size limit.
                    if (
                        msg_id
                        and msg_id not in assistant_msg_ids
                        and answer_streaming
                        and handle is not None
                    ):
                        full_text.clear()
                        reasoning_text = []
                        answer_streaming = False  # new round: segments may seal again
                        try:
                            await self.on_streaming_round_reset(native, handle)
                        except Exception:
                            logger.error(
                                "channel %s: on_streaming_round_reset failed",
                                self.channel,
                                exc_info=True,
                            )
                    assistant_msg_ids[msg_id] = mtype
                    if handle is None:
                        handle = await self.on_streaming_start(native, msg_id)
            elif object_type == "content" and getattr(obj, "delta", False):
                if getattr(obj, "type", "text") == "text":
                    delta = getattr(obj, "text", "") or ""
                    msg_id = getattr(obj, "msg_id", "")
                    mtype = assistant_msg_ids.get(msg_id)
                    # ``message_start`` always precedes its ``text_delta``, so
                    # the whitelist is populated before the first matching
                    # delta — non-whitelisted msg_ids (plugin_call /
                    # plugin_call_output) are dropped here.
                    if delta and mtype:
                        if mtype == "reasoning":
                            # Rebuild the card if a segment sealed the prior
                            # exploration card (handle is None).
                            if handle is None:
                                handle = await self.on_streaming_start(
                                    native, msg_id
                                )
                            reasoning_text.append(delta)
                            await self.on_streaming_reasoning_delta(
                                native, handle, "".join(reasoning_text)
                            )
                        else:  # "message"
                            # The final answer is streaming.
                            answer_streaming = True
                            if handle is None:
                                handle = await self.on_streaming_start(
                                    native, msg_id
                                )
                            full_text.append(delta)
                            await self.on_streaming_delta(native, handle, delta)
            elif object_type == "message" and status == "completed":
                if getattr(obj, "type", None) == "plugin_call":
                    clarification_questions = build_clarification_questions(obj)
                    if clarification_questions:
                        try:
                            card_id = await self.send_clarification_card(native, clarification_questions)
                            if card_id:
                                self._pending_clarification_cards[card_id] = _ClarificationCardPending(
                                    session_id=session_id,
                                    chat_id=chat_id,
                                    questions=clarification_questions,
                                )
                                logger.info(f'wrote card_id to _pending_clarification_cards: {card_id}')
                        except Exception:
                            logger.exception('failed to send clarification question to user')
                elif (
                    not self.streaming_enabled
                    and getattr(obj, "type", None) == "message"
                    and full_text
                ):
                    await self.send(native, "".join(full_text))
                    full_text.clear()
            elif object_type == "response" and status in (
                "completed",
                "failed",
                "cancelled",
            ):
                # Finalize the streaming card once on response termination
                # (covers multiple text blocks). handle may be None if the
                # model failed before the first assistant message (e.g. 404) —
                # then there's no card to finalize.
                if self.streaming_enabled and handle is not None:
                    await self.on_streaming_end(native, handle, "".join(full_text))
                if status == "failed":
                    # Surface the failure to IM; otherwise feishu/dingtalk stays silent.
                    msg = _extract_failure_message(obj)
                    await self.send(
                        native,
                        f"⚠️ 执行失败：{msg}" if msg else "⚠️ 执行失败，请稍后重试",
                    )
                # Flush any artifacts not already delivered after a segment
                # card (e.g. produced in the final-answer step) so they follow
                # the now-finalized answer card instead of being lost.
                await self._deliver_pending_artifacts(native, pending_artifacts)
                pending_artifacts.clear()
                # Completion signal (recall thinking / add done) — best-effort.
                try:
                    await self.on_consume_end(native, handle, status)
                except Exception:
                    logger.error(
                        "channel %s: on_consume_end failed", self.channel, exc_info=True
                    )
                if status == "completed":
                    # Recommended follow-up questions.
                    await self._maybe_send_followups(native, chat_id)
                terminated = True
                break

        if not terminated:
            # The stream ended without a terminal ``response`` event
            if self.streaming_enabled and handle is not None:
                try:
                    await self.on_streaming_end(native, handle, "".join(full_text))
                except Exception:
                    logger.error("channel %s: finalize on abort failed", self.channel, exc_info=True)
            await self.send(
                native,
                "⚠️ 执行异常中断，未能完成；请稍后重试或联系管理员。",
            )
            await self._deliver_pending_artifacts(native, pending_artifacts)
            pending_artifacts.clear()
            try:
                await self.on_consume_end(native, handle, "failed")
            except Exception:
                logger.error(
                    "channel %s: on_consume_end (abort) failed", self.channel, exc_info=True
                )

    async def _finalize_chat_run(
        self, chat_id: str, session_id: str, run_task: asyncio.Task[None], native: NativePayload
    ) -> None:
        """Ensure the chat reaches a terminal status after its runtime run.

        ``ChatRuntime.run`` persists the terminal status in ``_finish`` and
        publishes the terminal ``response`` event *before* that save, so
        ``_stream_to_platform`` can return while the save is still pending.
        Awaiting the run here lets the save (and ``run``'s ``finally``)
        land before the next ``_consume_one_request`` checks
        ``has_active_chat`` — without this, a fast follow-up could race the
        save and hit a spurious "session already has an active chat".

        If ``run`` raised before ``_finish`` or inside ``_finish``, the save
        never happened and the chat is left stuck ``running``, which would
        block every later request for the session. Observing the task
        surfaces its exception instead of leaving it as an unobserved "Task
        exception was never retrieved", and ``_force_failed_if_running``
        reconciles the stuck row so the session queue keeps moving.
        """
        try:
            await run_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "channel %s: ChatRuntime.run failed for chat %s",
                self.channel,
                chat_id,
                exc_info=exc,
            )
        await self._force_failed_if_running(chat_id, session_id, native)

    async def _force_failed_if_running(
        self, chat_id: str, session_id: str, native: NativePayload
    ) -> None:
        """Reconcile a chat the runtime left ``running`` to ``failed``.

        A no-op once the chat is already terminal (the normal path, where
        ``run`` persisted). It only fires when ``run`` failed to persist a
        terminal status.
        """
        _ = native
        chats = self.services.chats
        try:
            chat = await chats.get(chat_id)
        except LookupError:
            return
        if chat.status not in ACTIVE:
            return

        error_dict = {
            "code": "CONFLICT",
            "message": "Chat runtime did not finalize",
        }
        try:
            await OutputStream(
                self.services.events,
                session_id=chat.session_id,
                chat_id=chat.id,
                identity=chat.identity,
            ).response_failed(error=error_dict)
            chat.error = error_dict
            chat.mark_status("failed")
            await chats.reload_event_watermark(chat)
            await chats.save(chat)
            logger.warning(
                "channel %s: reconciled stuck chat %s to failed",
                self.channel,
                chat_id,
            )
        except Exception:
            logger.warning(
                "channel %s: failed to reconcile stuck chat %s to failed",
                self.channel,
                chat_id,
                exc_info=True,
            )

    async def _deliver_pending_artifacts(
        self, native: NativePayload, pending: list[tuple[str, bool]]
    ) -> None:
        """Flush buffered deliverable artifacts to IM (images via ``send_image``,
        others via ``send_file``), in arrival order.

        Best-effort: a failing push is logged and skipped so one bad file
        doesn't block the rest.
        """
        for abs_path, is_image in pending:
            try:
                if is_image:
                    await self.send_image(native, abs_path)
                else:
                    await self.send_file(native, abs_path)
            except Exception:
                logger.error(
                    "channel %s: deliver artifact %s failed", self.channel, abs_path, exc_info=True
                )

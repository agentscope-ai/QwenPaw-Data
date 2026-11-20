# -*- coding: utf-8 -*-
"""SettlementManager: cloud-adapted detection orchestrator.

Reads chat events from the DB, runs SettlementDetector for extraction,
and commits new cards to settlement_cards table.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .cm_utils import (
    SettlementCmClient,
    extract_content_text,
    feedback_ack_status,
    feedback_dry_run_recommendable,
    settlement_ingest,
)
from .models import DetectedItem
from .settings import SettlementSettings
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.repositories.settlement import SettlementRepository
from qwenpaw_data.host.core.utils.auth import generate_temporary_token

logger = logging.getLogger(__name__)

# Keep strong references to fire-and-forget ingest tasks until they finish.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


class SettlementManager:
    """Orchestrates settlement detection for one chat turn."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        identity: Identity | None = None,
        settings: SettlementSettings | None = None,
    ) -> None:
        self._db = db
        self._identity = identity or Identity(
            user_id="system", tenant_id="default", workspace_id="default"
        )
        self._settings = settings if settings is not None else SettlementSettings()
        self._repo = SettlementRepository(db, self._identity)
        self._chats = ChatRepository(db, self._identity)
        self._events = ChatEventRepository(db, self._identity)

    async def _resolve_access_token(self) -> str | None:
        try:
            return await asyncio.to_thread(generate_temporary_token, self._identity)
        except (LookupError, PermissionError, RuntimeError):
            logger.exception(
                "Settlement temporary token mint failed for user=%s",
                self._identity.user_id,
            )
            return None

    async def _build_model(self, *, session_id: str, chat_id: str) -> Any:
        from .llm import for_structured_calls
        from qwenpaw_data.host.core.domain.model_usage import (
            ModelUsageContext,
            ModelUsageSource,
        )
        from qwenpaw_data.host.core.providers.factory import build_model
        from qwenpaw_data.host.core.repositories import PreferencesRepository

        prefs = await PreferencesRepository(self._db, self._identity).load()
        config = prefs.resolve()
        active = config.light or config.default
        return for_structured_calls(
            build_model(
                active,
                usage=ModelUsageContext(
                    ModelUsageSource.SETTLEMENT,
                    self._identity,
                    session_id,
                    chat_id,
                ),
            )
        )

    async def on_chat_finish(self, *, chat_id: str, session_id: str) -> None:
        """Background entry point after a chat completes."""
        try:
            new_cards = await self.detect_for_chat(
                chat_id=chat_id,
                session_id=session_id,
            )
            if new_cards:
                logger.info(
                    "Settlement: %d cards detected for chat %s",
                    len(new_cards),
                    chat_id,
                )
        except Exception:
            logger.warning(
                "Settlement detection failed for chat %s", chat_id, exc_info=True
            )

    async def schedule_cm_ingest(
        self,
        card: dict[str, Any],
        *,
        datasource_id: str | None = None,
    ) -> None:
        """After repo.confirm: async CM feedback_card writeback (fail-soft, non-blocking).

        ``datasource_id`` / temporary token must be resolved before scheduling —
        the background task must not reuse this request AsyncSession.
        """
        ds = (datasource_id or "").strip() or None
        if not ds:
            logger.warning(
                "Settlement CM ingest not scheduled for card %s: missing datasource_id",
                card.get("id"),
            )
            return
        access_token = await self._resolve_access_token()
        _track(
            asyncio.create_task(
                self._ingest_confirmed_safe(
                    card,
                    datasource_id=ds,
                    access_token=access_token,
                )
            )
        )

    async def _ingest_confirmed_safe(
        self,
        card: dict[str, Any],
        *,
        datasource_id: str,
        access_token: str | None = None,
    ) -> None:
        try:
            record = await settlement_ingest(
                card,
                datasource_id=datasource_id,
                access_token=access_token,
            )
            if not str(record.get("status") or "").startswith("ok"):
                logger.warning(
                    "Settlement CM ingest non-ok for card %s: %s",
                    card.get("id"),
                    record.get("status"),
                )
        except Exception:
            logger.warning(
                "Settlement CM ingest failed for card %s",
                card.get("id"),
                exc_info=True,
            )

    async def detect_for_chat(
        self, *, chat_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        """Run detection on a sliding window of recent chats."""
        if not self._settings.enabled:
            return []

        window_events = await self._read_window_events(session_id, chat_id)
        if not window_events:
            logger.warning(
                "Settlement: no window events for chat=%s session=%s identity=%s/%s/%s",
                chat_id,
                session_id,
                self._identity.user_id,
                self._identity.tenant_id,
                self._identity.workspace_id,
            )
            return []

        recent_turns = self._extract_conversation(window_events)
        if not recent_turns:
            logger.warning(
                "Settlement: window had %d events but no message turns chat=%s identity=%s/%s/%s",
                len(window_events),
                chat_id,
                self._identity.user_id,
                self._identity.tenant_id,
                self._identity.workspace_id,
            )
            return []

        pool_summary = await self._load_pool_summary(session_id)
        datasource_id = await self._load_datasource_id(session_id)
        cm = await self._create_cm_client(datasource_id=datasource_id)
        domain_names = await self._resolve_domain_names(cm)
        if not domain_names:
            # 无可用业务域白名单时不做推荐（避免 LLM 自造域名打爆 CM）
            logger.warning(
                "Settlement: skip recommend — list_domains empty chat=%s session=%s ds=%s",
                chat_id,
                session_id,
                datasource_id or "",
            )
            return []

        model = await self._build_model(session_id=session_id, chat_id=chat_id)
        cfg = self._settings
        result = await self._detect(
            model,
            recent_turns,
            pool_summary,
            domain_names=domain_names,
            timeout=cfg.llm_timeout,
            attempts=cfg.llm_attempts,
        )
        if result is None or not result.items:
            return []

        items = result.items

        # DB 去重 — 已拒绝的 cards 不再推荐
        items = await self._filter_dismissed(model, items, session_id)
        if not items:
            logger.info("Settlement: all items filtered by dismissed history")
            return []

        confirmed_items = await self._confirm_with_cm(
            model,
            cm,
            items,
            session_id=session_id,
            chat_id=chat_id,
            datasource_id=datasource_id,
            domain_names=domain_names,
        )
        if not confirmed_items:
            return []

        # CM feedback_card dry-run：只有能落图的才入库给用户确认
        confirmed_items = await self._filter_feedback_dry_run(
            confirmed_items,
            session_id=session_id,
            chat_id=chat_id,
            datasource_id=datasource_id,
            cm=cm,
        )
        if not confirmed_items:
            logger.info("Settlement: all items filtered by feedback_card dry-run")
            return []

        return await self._commit_cards(
            confirmed_items,
            session_id=session_id,
            chat_id=chat_id,
        )

    async def _read_window_events(
        self, session_id: str, current_chat_id: str
    ) -> list[Any]:
        """读取最近 N 个未消费 chat 的 events（滑动窗口）。

        历史轮仅纳入 completed；当前 chat 无论 status 都纳入
        （Host 可能在 ReplyEnd 时触发，此时尚未 mark completed）。
        已成功产出 settlement cards 的 chat 经水位截断后不再纳入窗口。
        """
        all_chats = await self._chats.list_for_session(session_id)
        # list_for_session 按 sequence 升序
        valid = [
            c for c in all_chats if c.status == "completed" or c.id == current_chat_id
        ]

        watermark_chat_id = await self._get_watermark_chat_id(session_id)
        if watermark_chat_id:
            # 在完整会话顺序里定位水位，再截断 valid 列表。
            # 水位 chat 本身不必仍是 completed（避免水位「静默失效」回扫更早轮次）。
            wm_seq = next(
                (c.sequence for c in all_chats if c.id == watermark_chat_id),
                None,
            )
            if wm_seq is None:
                logger.warning(
                    "Settlement: watermark chat %s not in session %s; ignoring watermark",
                    watermark_chat_id,
                    session_id,
                )
            else:
                valid = [c for c in valid if c.sequence > wm_seq]

        current_idx = None
        for i, c in enumerate(valid):
            if c.id == current_chat_id:
                current_idx = i
                break

        if current_idx is None:
            # 当前 chat 不在 valid 窗口（异常：identity/水位/数据不一致）→ 跳过
            logger.warning(
                "Settlement: current chat %s not in session window; skip",
                current_chat_id,
            )
            return []

        start = max(0, current_idx - self._settings.window_size + 1)
        window_chats = valid[start : current_idx + 1]

        window_events: list[Any] = []
        for chat in window_chats:
            events = await self._events.read_after(chat.id, after=0)
            window_events.extend(events)

        return window_events

    async def _get_watermark_chat_id(self, session_id: str) -> str | None:
        """查找 session 内最近一次成功产出 card 的 source_chat_id。"""
        all_cards = await self._repo.list_by_session(session_id)
        if not all_cards:
            return None
        # list_by_session 按 created_at desc 排序，[0] 是最新的 card
        return all_cards[0].get("source_chat_id")

    def _extract_conversation(self, window_events: list[Any]) -> list[dict[str, Any]]:
        """Extract conversation turns from window events."""
        recent_turns: list[dict[str, Any]] = []
        for ev in window_events:
            if getattr(ev, "object", None) != "message":
                continue
            role = getattr(ev, "role", None)
            if not role:
                continue
            combined = extract_content_text(getattr(ev, "content", []))
            if not combined:
                continue
            recent_turns.append({"role": role, "content": combined})
        return recent_turns

    async def _load_pool_summary(self, session_id: str) -> list[dict[str, Any]]:
        """本 session 已算过的项（pending/queried/confirmed），供 Detector 避免重复提取。"""
        pending = await self._repo.list_by_session(session_id, status="pending")
        queried = await self._repo.list_by_session(session_id, status="queried")
        confirmed = await self._repo.list_by_session(session_id, status="confirmed")
        # list_by_session 均为 created_at desc；合并后截断
        merged = pending + queried + confirmed
        return [
            {"id": c["id"], "type": c["type"], "fields": c["fields"]}
            for c in merged[: self._settings.pool_summary_limit]
        ]

    async def _load_datasource_id(self, session_id: str) -> str | None:
        try:
            from qwenpaw_data.host.core.db.tables import SessionRow

            row = await self._db.get(SessionRow, session_id)
            return row.datasource_id if row else None
        except Exception:
            logger.debug(
                "Failed to load datasource_id for session %s", session_id, exc_info=True
            )
            return None

    async def _filter_dismissed(
        self, model: Any, items: list[DetectedItem], session_id: str
    ) -> list[DetectedItem]:
        """过滤掉用户已拒绝过的语义等价 cards（LLM 判断）。"""
        dismissed = await self._repo.list_by_session(session_id, status="dismissed")
        if not dismissed:
            return items

        # list_by_session 按 created_at desc；只取最近 N 条，避免 prompt 无限膨胀
        limit = self._settings.dismissed_summary_limit
        dismissed = dismissed[:limit]

        from .dismissed_filter import DismissedFilter

        f = DismissedFilter(model)
        return await f.filter(
            items,
            dismissed,
            timeout=self._settings.llm_timeout,
            attempts=self._settings.llm_attempts,
        )

    async def _confirm_with_cm(
        self,
        model: Any,
        cm: SettlementCmClient,
        items: list[Any],
        *,
        session_id: str,
        chat_id: str,
        datasource_id: str | None = None,
        domain_names: list[str] | None = None,
    ) -> list[Any]:
        """调 CM 确认，过滤掉语义层已有的等价知识。"""
        try:
            from .confirmer import SettlementConfirmer

            confirmer = SettlementConfirmer(
                model,
                session_id=session_id,
                chat_id=chat_id,
                cm=cm,
                datasource_id=datasource_id,
                domain_names=domain_names,
                concurrency=self._settings.confirmer_concurrency,
                retrieval_max_iters=self._settings.retrieval_max_iters,
                cm_timeout=self._settings.cm_timeout,
            )
            return await confirmer.confirm(items)
        except Exception:
            logger.warning(
                "CM confirmation failed, dropping all items (uncertain → skip)",
                exc_info=True,
            )
            return []

    async def _filter_feedback_dry_run(
        self,
        items: list[DetectedItem],
        *,
        session_id: str,
        chat_id: str,
        datasource_id: str | None,
        cm: SettlementCmClient,
    ) -> list[DetectedItem]:
        """Call CM ``feedback_card?mode=test``; keep only items that can land."""
        ds = (datasource_id or "").strip()
        if not ds:
            logger.warning(
                "Settlement: feedback dry-run dropped all — missing datasource_id"
            )
            return []
        if not items:
            return []

        from qwenpaw_data.host.core.utils.ids import create_id

        sem = asyncio.Semaphore(max(1, self._settings.confirmer_concurrency))

        async def _one(item: DetectedItem) -> DetectedItem | None:
            type_key = str(getattr(item.type, "value", item.type))
            preview = {
                "id": create_id("preview"),
                "type": type_key,
                "fields": dict(item.fields or {}),
                "session_id": session_id,
                "source_chat_id": chat_id,
            }
            async with sem:
                record = await settlement_ingest(
                    preview,
                    datasource_id=ds,
                    cm=cm,
                    mode="test",
                )
            ack = feedback_ack_status(record)
            if feedback_dry_run_recommendable(record):
                return item
            logger.info(
                "Settlement: drop item after feedback dry-run type=%s http=%s ack=%s",
                type_key,
                record.get("status"),
                ack,
            )
            return None

        results = await asyncio.gather(*[_one(it) for it in items])
        return [it for it in results if it is not None]

    async def _detect(
        self,
        model: Any,
        recent_turns: list[dict[str, Any]],
        pool_summary: list[dict[str, Any]],
        *,
        domain_names: list[str] | None = None,
        timeout: float = 30.0,
        attempts: int = 2,
    ) -> Any | None:
        from .detector import SettlementDetector

        detector = SettlementDetector(model)
        return await detector.detect(
            recent_turns,
            pool_summary,
            domain_names=domain_names,
            timeout=timeout,
            attempts=attempts,
        )

    async def _create_cm_client(
        self, *, datasource_id: str | None = None
    ) -> SettlementCmClient:
        """创建 settlement 用的 CM REST client。"""
        return SettlementCmClient(
            access_token=await self._resolve_access_token(),
            datasource_id=datasource_id,
            timeout=self._settings.cm_timeout,
        )

    async def _resolve_domain_names(self, cm: SettlementCmClient) -> list[str]:
        """调 CM list_domains 获取可用 domain 名称列表。"""
        try:
            return await cm.list_domain_names()
        except Exception:
            logger.debug("Failed to resolve domain names", exc_info=True)
            return []

    async def _commit_cards(
        self,
        items: list[Any],
        *,
        session_id: str,
        chat_id: str,
    ) -> list[dict[str, Any]]:
        """Write pending cards; replace same-subject pending/queried by delete+insert."""
        from .subject import normalize_item_fields, subject_key

        pending = await self._repo.list_by_session(session_id, status="pending")
        queried = await self._repo.list_by_session(session_id, status="queried")
        # subject_key -> all actionable card ids (session + this batch)
        pending_by_subject: dict[str, list[str]] = {}
        for card in pending + queried:
            key = subject_key(card["type"], card.get("fields") or {})
            if key:
                pending_by_subject.setdefault(key, []).append(card["id"])

        new_cards: list[dict[str, Any]] = []
        for item in items:
            type_key = str(getattr(item.type, "value", item.type))
            fields = normalize_item_fields(type_key, dict(item.fields or {}))
            key = subject_key(type_key, fields)

            if key:
                for old_id in pending_by_subject.pop(key, []):
                    if await self._repo.delete_if_unconfirmed(
                        old_id, session_id=session_id
                    ):
                        logger.info(
                            "Settlement: replaced card %s subject=%s",
                            old_id,
                            key,
                        )

            card = await self._repo.add(
                session_id=session_id,
                source_chat_id=chat_id,
                type=type_key,
                fields=fields,
            )
            new_cards.append(card)
            if key:
                pending_by_subject[key] = [card["id"]]

        await self._db.flush()
        return new_cards

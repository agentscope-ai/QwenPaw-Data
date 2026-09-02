# -*- coding: utf-8 -*-
"""SettlementManager: detection orchestrator on the host's store layer.

Reads chat events from the event store, runs SettlementDetector for
extraction, and commits new cards to the settlement store.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.llm import (
    StructuredLLM,
    for_structured_calls,
)
from qwenpaw_data.host.core.cm_client import API_TOKEN_ENV, CLIENT_API_TOKEN_ENV
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.protocols import (
    ChatEventStore,
    ChatStore,
    SessionStore,
    SettlementStore,
)

from .cm_utils import (
    SettlementCmClient,
    extract_content_text,
    feedback_ack_status,
    feedback_dry_run_recommendable,
    settlement_ingest,
)
from .models import DetectedItem
from .settings import SettlementSettings

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
        *,
        sessions: SessionStore,
        chats: ChatStore,
        events: ChatEventStore,
        cards: SettlementStore,
        identity: Identity | None = None,
        user_runtime_config: Any = None,
        settings: SettlementSettings | None = None,
        access_token: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._chats = chats
        self._events = events
        self._cards = cards
        self._identity = identity or Identity.anonymous()
        self._user_runtime_config = user_runtime_config
        self._settings = settings if settings is not None else SettlementSettings()
        self._access_token = access_token

    @property
    def _user_id(self) -> str:
        return self._identity.user_id

    def _resolve_access_token(self) -> str | None:
        if self._access_token is not None:
            return self._access_token.strip() or None
        return (
            (os.environ.get(CLIENT_API_TOKEN_ENV) or "").strip()
            or (os.environ.get(API_TOKEN_ENV) or "").strip()
            or None
        )

    def _build_llm(self) -> StructuredLLM | None:
        """The light model if configured, else default, else env; None disables.

        Settlement has no rules-only fallback: without a usable model the
        turn simply settles nothing.
        """
        from qwenpaw_data.host.core.model import build_model_from_env
        from qwenpaw_data.host.core.providers.factory import build_model

        config = self._user_runtime_config
        active = None
        if config is not None:
            active = getattr(config, "light", None) or getattr(
                config, "default", None
            )
        try:
            if active is not None:
                model = for_structured_calls(build_model(active))
            else:
                model = for_structured_calls(build_model_from_env())
        except Exception:
            logger.exception("Settlement has no usable model; skipping detection")
            return None
        return StructuredLLM(
            model,
            timeout=self._settings.llm_timeout,
            attempts=self._settings.llm_attempts,
            concurrency=self._settings.confirmer_concurrency,
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
            logger.warning("Settlement detection failed for chat %s", chat_id, exc_info=True)

    def schedule_cm_ingest(
        self,
        card: dict[str, Any],
        *,
        datasource_id: str | None = None,
    ) -> None:
        """After confirm: async CM feedback_card writeback (fail-soft, non-blocking)."""
        ds = (datasource_id or "").strip() or None
        if not ds:
            logger.warning(
                "Settlement CM ingest not scheduled for card %s: missing datasource_id",
                card.get("id"),
            )
            return
        access_token = self._resolve_access_token()
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
                "Settlement: no window events for chat=%s session=%s user=%s",
                chat_id,
                session_id,
                self._user_id,
            )
            return []

        recent_turns = self._extract_conversation(window_events)
        if not recent_turns:
            logger.warning(
                "Settlement: window had %d events but no message turns chat=%s user=%s",
                len(window_events),
                chat_id,
                self._user_id,
            )
            return []

        pool_summary = await self._load_pool_summary(session_id)
        datasource_id = await self._load_datasource_id(session_id)
        cm = self._create_cm_client(datasource_id=datasource_id)
        domain_names = await self._resolve_domain_names(cm)
        if not domain_names:
            # Without a domain allowlist there is nothing to recommend against
            # (the LLM inventing domains would hammer CM with bogus names).
            logger.warning(
                "Settlement: skip recommend — list_domains empty chat=%s session=%s ds=%s",
                chat_id,
                session_id,
                datasource_id or "",
            )
            return []

        llm = self._build_llm()
        if llm is None:
            return []
        result = await self._detect(
            llm, recent_turns, pool_summary,
            domain_names=domain_names,
        )
        if result is None or not result.items:
            return []

        items = result.items

        # Store-level dedupe — dismissed cards are never re-recommended.
        items = await self._filter_dismissed(llm, items, session_id)
        if not items:
            logger.info("Settlement: all items filtered by dismissed history")
            return []

        confirmed_items = await self._confirm_with_cm(
            llm, cm, items,
            datasource_id=datasource_id,
            domain_names=domain_names,
        )
        if not confirmed_items:
            return []

        # CM feedback_card dry-run: only items that can land reach the user.
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
        """Events of the last N unconsumed chats (sliding window).

        Prior turns count only when completed; the current chat counts
        regardless of status (the host may trigger before it is marked
        completed). Chats before the last card-producing chat are cut off
        by the watermark.
        """
        all_chats = sorted(
            await self._chats.list_for_session(session_id),
            key=lambda c: c.sequence,
        )
        valid = [
            c
            for c in all_chats
            if c.status == "completed" or c.id == current_chat_id
        ]

        watermark_chat_id = await self._get_watermark_chat_id(session_id)
        if watermark_chat_id:
            # Locate the watermark in the full session order, then truncate the
            # valid list. The watermark chat itself need not still be completed
            # (a silently void watermark would rescan earlier turns).
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
            # Current chat missing from the window (watermark/data mismatch) → skip.
            logger.warning(
                "Settlement: current chat %s not in session window; skip",
                current_chat_id,
            )
            return []

        start = max(0, current_idx - self._settings.window_size + 1)
        window_chats = valid[start: current_idx + 1]

        window_events: list[Any] = []
        for chat in window_chats:
            events = await self._events.read_after(chat.id, -1)
            window_events.extend(events)

        return window_events

    async def _get_watermark_chat_id(self, session_id: str) -> str | None:
        """The source_chat_id of the session's latest card, if any."""
        all_cards = await self._cards.list_by_session(self._user_id, session_id)
        if not all_cards:
            return None
        # list_by_session is created_at desc; [0] is the newest card
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
        """Items already settled this session, so the detector avoids repeats."""
        pending = await self._cards.list_by_session(
            self._user_id, session_id, status="pending"
        )
        queried = await self._cards.list_by_session(
            self._user_id, session_id, status="queried"
        )
        confirmed = await self._cards.list_by_session(
            self._user_id, session_id, status="confirmed"
        )
        # Each list is created_at desc; merge then truncate.
        merged = pending + queried + confirmed
        return [
            {"id": c["id"], "type": c["type"], "fields": c["fields"]}
            for c in merged[: self._settings.pool_summary_limit]
        ]

    async def _load_datasource_id(self, session_id: str) -> str | None:
        try:
            session = await self._sessions.get(session_id)
            return session.datasource_id
        except Exception:
            logger.debug(
                "Failed to load datasource_id for session %s", session_id,
                exc_info=True,
            )
            return None

    async def _filter_dismissed(
        self, llm: StructuredLLM, items: list[DetectedItem], session_id: str
    ) -> list[DetectedItem]:
        """Drop cards the user already dismissed (semantic equality, LLM-judged)."""
        dismissed = await self._cards.list_by_session(
            self._user_id, session_id, status="dismissed"
        )
        if not dismissed:
            return items

        # created_at desc; only the most recent N, to bound the prompt.
        limit = self._settings.dismissed_summary_limit
        dismissed = dismissed[:limit]

        from .dismissed_filter import DismissedFilter

        f = DismissedFilter(llm)
        return await f.filter(items, dismissed)

    async def _confirm_with_cm(
        self,
        llm: StructuredLLM,
        cm: SettlementCmClient,
        items: list[Any],
        *,
        datasource_id: str | None = None,
        domain_names: list[str] | None = None,
    ) -> list[Any]:
        """Ask CM to filter out knowledge the semantic layer already has."""
        try:
            from .confirmer import SettlementConfirmer

            confirmer = SettlementConfirmer(
                llm,
                cm=cm,
                datasource_id=datasource_id,
                domain_names=domain_names,
                concurrency=self._settings.confirmer_concurrency,
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
                "Settlement: drop item after feedback dry-run type=%s "
                "http=%s ack=%s",
                type_key,
                record.get("status"),
                ack,
            )
            return None

        results = await asyncio.gather(*[_one(it) for it in items])
        return [it for it in results if it is not None]

    async def _detect(
        self,
        llm: StructuredLLM,
        recent_turns: list[dict[str, Any]],
        pool_summary: list[dict[str, Any]],
        *,
        domain_names: list[str] | None = None,
    ) -> Any | None:
        from .detector import SettlementDetector

        detector = SettlementDetector(llm)
        return await detector.detect(
            recent_turns, pool_summary,
            domain_names=domain_names,
        )

    def _create_cm_client(
        self, *, datasource_id: str | None = None
    ) -> SettlementCmClient:
        """The CM REST client settlement calls use."""
        return SettlementCmClient(
            access_token=self._resolve_access_token(),
            datasource_id=datasource_id,
            timeout=self._settings.cm_timeout,
        )

    async def _resolve_domain_names(self, cm: SettlementCmClient) -> list[str]:
        """Fetch the available domain names via CM list_domains."""
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

        pending = await self._cards.list_by_session(
            self._user_id, session_id, status="pending"
        )
        queried = await self._cards.list_by_session(
            self._user_id, session_id, status="queried"
        )
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
                    if await self._cards.delete_if_unconfirmed(
                        self._user_id, old_id, session_id=session_id
                    ):
                        logger.info(
                            "Settlement: replaced card %s subject=%s",
                            old_id,
                            key,
                        )

            card = await self._cards.add(
                user_id=self._user_id,
                session_id=session_id,
                source_chat_id=chat_id,
                type=type_key,
                fields=fields,
            )
            new_cards.append(card)
            if key:
                pending_by_subject[key] = [card["id"]]

        return new_cards

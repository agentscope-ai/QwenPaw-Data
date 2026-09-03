# -*- coding: utf-8 -*-
"""FastAPI app factory for the headless host-core service.

Middleware order mirrors context_manager/api/server.py: auth installed
first (inner), CORS added last (outermost) so error responses carry CORS
headers. Single-process only: EventHub and the runtime registry are
in-process, so run uvicorn with ``--workers 1``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from qwenpaw_data.host.core.agent.middleware import (
    ConfirmedSettlementPromptMiddleware,
    SteerMiddleware,
)
from qwenpaw_data.host.core.api.auth import install_api_token_auth
from qwenpaw_data.host.core.api.deps import ServiceState
from qwenpaw_data.host.core.api.errors import http_exception_handler
from qwenpaw_data.host.core.api.routers import artifacts as artifacts_router
from qwenpaw_data.host.core.api.routers import channels as channels_router
from qwenpaw_data.host.core.api.routers import chats as chats_router
from qwenpaw_data.host.core.api.routers import clarification as clarification_router
from qwenpaw_data.host.core.api.routers import console as console_router
from qwenpaw_data.host.core.api.routers import cron as cron_router
from qwenpaw_data.host.core.api.routers import datasources as datasources_router
from qwenpaw_data.host.core.api.routers import events as events_router
from qwenpaw_data.host.core.api.routers import feedback as feedback_router
from qwenpaw_data.host.core.api.routers import files as files_router
from qwenpaw_data.host.core.api.routers import plan as plan_router
from qwenpaw_data.host.core.api.routers import preferences as preferences_router
from qwenpaw_data.host.core.api.routers import sessions as sessions_router
from qwenpaw_data.host.core.api.routers import settlement as settlement_router
from qwenpaw_data.host.core.api.routers import steer as steer_router
from qwenpaw_data.host.core.api.routers import trace as trace_router
from qwenpaw_data.host.core.cron import CronManager
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.model import build_model_from_env
from qwenpaw_data.host.core.paths import resolve_qwenpaw_data_home
from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.registry import QwenPawDataHostRegistry
from qwenpaw_data.host.core.runtime.registry import reset_runtime_registry
from qwenpaw_data.host.core.channels import ChannelManager, ChannelServices
from qwenpaw_data.host.core.store.json_store import (
    JSONAttachmentStore,
    JSONChannelBindingStore,
    JSONChannelConfigStore,
    JSONChatEventStore,
    JSONChatStore,
    JSONCronStore,
    JSONFeedbackStore,
    JSONPreferencesStore,
    JSONSessionStore,
    JSONSettlementStore,
)
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream

logger = logging.getLogger(__name__)

_CORS_ORIGINS_ENV = "QWENPAW_DATA_CORS_ALLOW_ORIGINS"
STORE_ENV = "QWENPAW_DATA_STORE"


def _cors_origins() -> list[str]:
    raw = (os.environ.get(_CORS_ORIGINS_ENV) or "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return ["http://127.0.0.1", "http://localhost"]


async def _cancel_orphaned_chats(state: ServiceState) -> None:
    """JSON-store analog of the enterprise cancel_all_active on startup."""
    for chat in await state.chats.list_active():
        logger.warning("cancelling orphaned running chat %s", chat.id)
        await OutputStream(
            state.events,
            session_id=chat.session_id,
            chat_id=chat.id,
            identity=chat.identity,
        ).response_cancelled()
        chat.cancel()
        await state.chats.reload_event_watermark(chat)
        await state.chats.save(chat)


def create_app(
    *,
    home: str | Path | None = None,
    model: Any = None,
    workspace: Any = None,
) -> FastAPI:
    resolved_home = resolve_qwenpaw_data_home(home)
    store_root = resolved_home / "host"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reset_hub()
        reset_runtime_registry()

        engine = None
        store_mode = (os.environ.get(STORE_ENV) or "").strip().lower()
        if store_mode == "json":
            sessions_store: Any = JSONSessionStore(store_root)
            chats_store: Any = JSONChatStore(store_root)
            events_store: Any = JSONChatEventStore(store_root)
            prefs_store: Any = JSONPreferencesStore(store_root)
            cron_store: Any = JSONCronStore(store_root)
            settlement_store: Any = JSONSettlementStore(store_root)
            channel_config_store: Any = JSONChannelConfigStore(store_root)
            channel_binding_store: Any = JSONChannelBindingStore(store_root)
            attachment_store: Any = JSONAttachmentStore(store_root)
            feedback_store: Any = JSONFeedbackStore(store_root)
        else:
            from qwenpaw_data.host.core.db.engine import (
                create_engine_and_factory,
                init_db,
                resolve_db_url,
            )
            from qwenpaw_data.host.core.store.sql_store import (
                SQLAttachmentStore,
                SQLChannelBindingStore,
                SQLChannelConfigStore,
                SQLChatEventStore,
                SQLChatStore,
                SQLCronStore,
                SQLFeedbackStore,
                SQLPreferencesStore,
                SQLSessionStore,
                SQLSettlementStore,
            )

            engine, factory = create_engine_and_factory(
                resolve_db_url(resolved_home),
            )
            await init_db(engine)
            sessions_store = SQLSessionStore(factory)
            chats_store = SQLChatStore(factory)
            events_store = SQLChatEventStore(factory)
            prefs_store = SQLPreferencesStore(factory)
            cron_store = SQLCronStore(factory)
            settlement_store = SQLSettlementStore(factory)
            channel_config_store = SQLChannelConfigStore(factory)
            channel_binding_store = SQLChannelBindingStore(factory)
            attachment_store = SQLAttachmentStore(factory)
            feedback_store = SQLFeedbackStore(factory)

        async def resolve_model() -> Any:
            """Prefer the local user's configured default model over env."""
            try:
                prefs = await prefs_store.load(Identity.anonymous().user_id)
                active = prefs.active_default()
                if active is not None:
                    return build_model(active)
            except Exception:
                logger.exception(
                    "failed to resolve model from preferences; using env",
                )
            return build_model_from_env()

        async def confirmed_settlement_cards(session_id: str) -> list[dict[str, Any]]:
            session = await sessions_store.get(session_id)
            return await settlement_store.list_by_session(
                session.identity.user_id,
                session_id,
                status="confirmed",
            )

        state = ServiceState(
            sessions=sessions_store,
            chats=chats_store,
            events=events_store,
            prefs=prefs_store,
            cron=cron_store,
            settlement=settlement_store,
            channel_configs=channel_config_store,
            channel_bindings=channel_binding_store,
            attachments=attachment_store,
            feedback=feedback_store,
            hosts=QwenPawDataHostRegistry(
                home=resolved_home,
                model=model,
                workspace=workspace,
                extra_middlewares_factory=lambda: [
                    SteerMiddleware(),
                    ConfirmedSettlementPromptMiddleware(
                        loader=confirmed_settlement_cards,
                    ),
                ],
                model_factory=None if model is not None else resolve_model,
            ),
            tasks=set(),
        )
        app.state.service = state
        await _cancel_orphaned_chats(state)
        state.cron_manager = CronManager(
            cron=state.cron,
            sessions=state.sessions,
            chats=state.chats,
            events=state.events,
            hosts=state.hosts,
            prefs=state.prefs,
            settlement=state.settlement,
        )
        await state.cron_manager.start()
        try:
            state.channel_manager = await ChannelManager.from_services(
                ChannelServices(
                    sessions=state.sessions,
                    chats=state.chats,
                    events=state.events,
                    bindings=state.channel_bindings,
                    configs=state.channel_configs,
                    hosts=state.hosts,
                    prefs=state.prefs,
                    settlement=state.settlement,
                )
            )
            app.state.channel_manager = state.channel_manager
            state.cron_manager.channel_manager = state.channel_manager
            await state.channel_manager.start_all()
        except Exception:
            logger.exception("channel manager startup failed; IM channels disabled")
            state.channel_manager = None
            app.state.channel_manager = None
        try:
            yield
        finally:
            if state.channel_manager is not None:
                try:
                    await state.channel_manager.stop_all()
                except Exception:
                    logger.exception("channel manager shutdown failed")
            await state.cron_manager.shutdown()
            # Cancel tracked tasks AND wait for them to unwind before the
            # hub/hosts/engine go away, or a chat runtime mid-_finish races
            # engine.dispose() and can wedge event-loop teardown.
            pending = [task for task in state.tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for host in list(state.hosts._items.values()):
                try:
                    await host.close()
                except Exception:
                    logger.exception("failed to close host workspace")
            reset_hub()
            reset_runtime_registry()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="QwenPaw Data Host Service",
        lifespan=lifespan,
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.include_router(sessions_router.router, prefix="/api/v1")
    app.include_router(chats_router.router, prefix="/api/v1")
    app.include_router(console_router.router, prefix="/api/v1")
    app.include_router(events_router.router, prefix="/api/v1")
    app.include_router(plan_router.router, prefix="/api/v1")
    app.include_router(trace_router.router, prefix="/api/v1")
    app.include_router(feedback_router.router, prefix="/api/v1")
    app.include_router(steer_router.router, prefix="/api/v1")
    app.include_router(clarification_router.router, prefix="/api/v1")
    app.include_router(preferences_router.router, prefix="/api/v1")
    app.include_router(datasources_router.router, prefix="/api/v1")
    app.include_router(cron_router.router, prefix="/api/v1")
    app.include_router(settlement_router.router, prefix="/api/v1")
    app.include_router(channels_router.router, prefix="/api/v1")
    app.include_router(artifacts_router.router, prefix="/api/v1")
    app.include_router(files_router.router, prefix="/api/v1")
    app.include_router(files_router.shared_router, prefix="/api/v1")
    app.include_router(channels_router.config_router, prefix="/api/v1")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Auth first (inner), CORS last (outermost).
    install_api_token_auth(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-User-Id"],
    )
    return app

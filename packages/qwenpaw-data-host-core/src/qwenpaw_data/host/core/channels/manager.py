# -*- coding: utf-8 -*-
"""ChannelManager: create channels by enabled flag, ``start_all``/``stop_all``, ``reload``."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from qwenpaw_data.host.core.api.models.cron import CronJobWrite
from qwenpaw_data.host.core.channels.base import BaseChannel
from qwenpaw_data.host.core.channels.channel_config_store import (
    get_channel_config_store,
    iter_identities,
)
from qwenpaw_data.host.core.channels.registry import build_channel
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories.channel_binding import ChannelBindingRepository
from qwenpaw_data.host.core.settings import get_settings

logger = logging.getLogger("qwenpaw_data.channels.manager")


class ChannelManager:
    def __init__(self) -> None:
        self._channels: dict[tuple[Identity, str], BaseChannel] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    async def from_config(cls) -> ChannelManager:
        """Create channel instances per ChannelConfigStore's enabled flag.

        Channels whose SDK is not installed are skipped (factory raises ImportError -> log + skip).
        """
        mgr = cls()
        await mgr._load_enabled_channels()
        return mgr

    async def _load_enabled_channels(self) -> None:
        """Re-read DB config and populate ``self._channels`` by enabled flag.

        Channel config is per-identity (one row in the ``channels`` table per
        tenant/user/workspace); this scans every identity's store and builds one instance per
        enabled (user, channel_type). Reads the DB directly so a save takes effect on reload.
        """

        allowed = get_settings().catalog.channel_ids
        for identity in await iter_identities():
            store = get_channel_config_store(identity)
            try:
                config = await store.load()
            except Exception as exc:  # pragma: no cover - skip a corrupt config row
                logger.warning(
                    "channel config for user %s unreadable, skip: %s",
                    identity.user_id,
                    exc,
                )
                continue
            for key, ch_cfg in config.items():
                if not ch_cfg.get("enabled") or key not in allowed:
                    continue
                ch = build_channel(identity, key)
                if ch is not None:
                    self._channels[(identity, key)] = ch

    async def is_channel_eligible_for_cron_job(
        self, identity: Identity, cron_job_config: CronJobWrite
    ) -> None:
        """创建或整单替换 IM 定时任务时的校验。"""
        errors = []
        if not identity:
            errors.append("missing Identity argument")
        if not cron_job_config:
            errors.append("missing CronJobWrite argument")
        if not cron_job_config.target_external_key:
            errors.append("missing CronJobWrite.target_external_key argument")
        if not cron_job_config.datasource_id:
            errors.append("missing CronJobWrite.datasource_id argument")

        ch = self._channels.get((identity, cron_job_config.channel))
        if not ch:
            errors.append(f"failed to find channel instance")

        try:
            async with get_session_factory()() as db:
                ok = await ChannelBindingRepository(db).exists(
                    identity,
                    str(cron_job_config.channel),
                    cron_job_config.target_external_key,
                )
        except Exception as e:
            errors.append(f"channel target check failed, {str(e)}")
            ok = False
        if not ok:
            errors.append(
                f"channel has no target external key {cron_job_config.target_external_key} "
                f"(the bot must have spoken to it first)"
            )

        if errors:
            raise ValueError(
                f"cannot create new cron job for identity {identity} "
                f"using configuration {cron_job_config}: {errors}"
            )

    async def run_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        """按定时任务在对应 IM 上跑一轮，并把结果推给目标对象。
        Send a failure message to the IM user otherwise. Never throw.
        job: as defined in CronJobRepository _to_dict(CronJobRow)
        {
            "id": row.id,
            "user_id": row.user_id,
            "tenant_id": row.tenant_id,
            "workspace_id": row.workspace_id,
            "name": row.name,
            "enabled": row.enabled,
            "message": row.message,
            "datasource_id": row.datasource_id,
            "channel": row.channel,
            "session_id": row.session_id,
            "target_external_key": row.target_external_key,
            "schedule": ScheduleSpec.model_validate(row.schedule_json).model_dump(
                mode="json"
            ),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

        期望效果：
        - 用 ``message`` 当这一轮的用户输入，用 ``datasource_id`` 当数据源。
        - ``session_id`` 能对上已有 session 就在这条 session 上跑
          （推送到 ``target_external_key`` 指定的 IM 对象）；
          为空或对不上就新开一条。
        - 结果推到 ``target_external_key`` 对应的 IM 对象（群/人）。
        - 跑失败也要让该 IM 对象看得到失败，不能只写 host 日志。
        """
        identity = Identity(
            user_id=cron_job_config["user_id"],
            tenant_id=cron_job_config["tenant_id"],
            workspace_id=cron_job_config["workspace_id"],
        )

        channel = self._channels.get((identity, cron_job_config["channel"]))
        if not channel:
            raise ValueError(
                f"no active channel of type {cron_job_config['channel']} for identity {identity}, cron config:{cron_job_config}"
            )

        await channel.inject_cron_job(cron_job_config)

    async def _start_channel(
        self, identity: Identity, key: str, ch: BaseChannel
    ) -> None:
        """Bind the host loop onto the channel and start it."""
        ch.set_loop(self._loop)  # type: ignore[arg-type]
        try:
            await ch.start()
            logger.info("channel started: user=%s channel=%s", identity.user_id, key)
        except Exception:
            logger.exception(
                "channel start failed: user=%s channel=%s", identity.user_id, key
            )

    async def _stop_channel(
        self, identity: Identity, key: str, ch: BaseChannel
    ) -> None:
        try:
            await ch.stop()
        except Exception:
            logger.exception(
                "channel stop failed: user=%s channel=%s", identity.user_id, key
            )

    async def start_all(self) -> None:
        self._loop = asyncio.get_running_loop()
        for (identity, key), ch in self._channels.items():
            await self._start_channel(identity, key, ch)

    async def stop_all(self) -> None:
        for (identity, key), ch in list(self._channels.items()):
            await self._stop_channel(identity, key, ch)

    async def reload(
        self, identity: Identity | None = None, channel: str | None = None
    ) -> dict[str, list[str]]:
        """Re-read DB config, stop old channels -> rebuild per new config -> start.

        Called by ``POST /api/channels/reload``: after CM saves channel config it calls back so
        the host-core process reconnects with the new config (channel secret etc. is cached on the
        instance, so the instance must be rebuilt to take effect). ``self._loop`` was set by
        ``start_all``; reload runs on the same event loop, so the rebound ``set_loop`` works.

        With ``(identity, channel)`` this changes a single channel of a single identity only.
        """
        if identity is not None and channel is not None:
            return await self._reload_one(identity, channel)
        return await self._reload_all()

    async def _reload_all(self) -> dict[str, list[str]]:
        stopped = [f"{i.user_id}/{k}" for (i, k) in self._channels.keys()]
        await self.stop_all()
        self._channels.clear()
        await self._load_enabled_channels()  # re-read DB -> populate _channels
        await self.start_all()
        started = [f"{i.user_id}/{k}" for (i, k) in self._channels.keys()]
        logger.info("channel reloaded: stopped=%s started=%s", stopped, started)
        return {"stopped": stopped, "started": started}

    async def _reload_one(
        self, identity: Identity, channel: str
    ) -> dict[str, list[str]]:
        """Reload a single ``(identity, channel)``: stop the old instance, rebuild, start.

        Only this channel is touched — other channels (same or other identity) keep running
        and their session queues are preserved. If the channel is no longer enabled in config,
        the old instance is stopped and removed with nothing started in its place.
        """
        key = (identity, channel)
        stopped: list[str] = []
        old = self._channels.pop(key, None)
        if old is not None:
            await self._stop_channel(identity, channel, old)
            stopped.append(f"{identity.user_id}/{channel}")

        # Re-read this identity's config for this channel; start a fresh instance if enabled.
        store = get_channel_config_store(identity)
        config = await store.load()
        ch_cfg = config.get(channel) or {}
        started: list[str] = []
        if ch_cfg.get("enabled") and channel in get_settings().catalog.channel_ids:
            ch = build_channel(identity, channel)
            if ch is not None:
                self._channels[key] = ch
                await self._start_channel(identity, channel, ch)
                started.append(f"{identity.user_id}/{channel}")
        logger.info("channel reloaded: stopped=%s started=%s", stopped, started)
        return {"stopped": stopped, "started": started}

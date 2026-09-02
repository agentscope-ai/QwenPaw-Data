# -*- coding: utf-8 -*-
"""ChannelManager: create channels by enabled flag, ``start_all``/``stop_all``, ``reload``."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from qwenpaw_data.host.core.api.models.cron import CronJobWrite
from qwenpaw_data.host.core.channels.base import BaseChannel, ChannelServices
from qwenpaw_data.host.core.channels.registry import build_channel
from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger("qwenpaw_data.channels.manager")


class ChannelManager:
    def __init__(self, services: ChannelServices) -> None:
        self._services = services
        self._channels: dict[tuple[str, str], BaseChannel] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    async def from_services(cls, services: ChannelServices) -> ChannelManager:
        """Create channel instances per the config store's enabled flag.

        Channels whose SDK is not installed are skipped (factory raises
        ImportError -> log + skip).
        """
        mgr = cls(services)
        await mgr._load_enabled_channels()
        return mgr

    async def _load_enabled_channels(self) -> None:
        """Re-read the config store and populate ``self._channels`` by enabled flag.

        Channel config is per-user; this scans every user's config and builds
        one instance per enabled (user, channel_type). Reads the store directly
        so a save takes effect on reload.
        """
        configs = self._services.configs
        for user_id in await configs.list_user_ids():
            try:
                config = await configs.load(user_id)
            except Exception as exc:  # pragma: no cover - skip a corrupt config
                logger.warning(
                    "channel config for user %s unreadable, skip: %s", user_id, exc
                )
                continue
            for key, ch_cfg in config.items():
                if not (ch_cfg or {}).get("enabled"):
                    continue
                ch = build_channel(Identity(user_id=user_id), key)
                if ch is not None:
                    ch.set_services(self._services)
                    self._channels[(user_id, key)] = ch

    def get_channel(self, user_id: str, channel: str) -> BaseChannel | None:
        return self._channels.get((user_id, channel))

    async def is_channel_eligible_for_cron_job(
        self, identity: Identity, cron_job_config: CronJobWrite
    ) -> None:
        """Validation when creating or fully replacing an IM cron job."""
        errors = []
        if not identity:
            errors.append('missing Identity argument')
        if not cron_job_config:
            errors.append('missing CronJobWrite argument')
        if not cron_job_config.target_external_key:
            errors.append('missing CronJobWrite.target_external_key argument')
        if not cron_job_config.datasource_id:
            errors.append('missing CronJobWrite.datasource_id argument')

        ch = self._channels.get((identity.user_id, cron_job_config.channel))
        if not ch:
            errors.append('failed to find channel instance')

        try:
            ok = await self._services.bindings.exists(
                identity.user_id,
                str(cron_job_config.channel),
                cron_job_config.target_external_key or "",
            )
        except Exception as e:
            errors.append(f'channel target check failed, {str(e)}')
            ok = False
        if not ok:
            errors.append(
                f'channel has no target external key {cron_job_config.target_external_key} '
                f'(the bot must have spoken to it first)'
            )

        if errors:
            raise ValueError(
                f'cannot create new cron job for identity {identity} '
                f'using configuration {cron_job_config}: {errors}'
            )

    async def run_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        """Run one cron round on the matching IM channel and push the result.

        Expected effect:
        - ``message`` is this round's user input; ``datasource_id`` the datasource.
        - ``session_id`` matching an existing session runs on it (pushed to the
          ``target_external_key`` IM target); empty or stale opens a new one.
        - The result is pushed to the ``target_external_key`` IM target (group/user).
        - Failures must also reach that IM target, not just the host log.
        """
        identity = Identity(user_id=cron_job_config['user_id'])

        channel = self._channels.get(
            (identity.user_id, cron_job_config['channel'])
        )
        if not channel:
            raise ValueError(
                f'no active channel of type {cron_job_config["channel"]} for '
                f'identity {identity}, cron config:{cron_job_config}'
            )

        await channel.inject_cron_job(cron_job_config)

    async def _start_channel(self, user_id: str, key: str, ch: BaseChannel) -> None:
        """Bind the host loop onto the channel and start it."""
        ch.set_loop(self._loop)  # type: ignore[arg-type]
        try:
            await ch.start()
            logger.info("channel started: user=%s channel=%s", user_id, key)
        except Exception:
            logger.exception("channel start failed: user=%s channel=%s", user_id, key)

    async def _stop_channel(self, user_id: str, key: str, ch: BaseChannel) -> None:
        try:
            await ch.stop()
        except Exception:
            logger.exception("channel stop failed: user=%s channel=%s", user_id, key)

    async def start_all(self) -> None:
        self._loop = asyncio.get_running_loop()
        for (user_id, key), ch in self._channels.items():
            await self._start_channel(user_id, key, ch)

    async def stop_all(self) -> None:
        for (user_id, key), ch in list(self._channels.items()):
            await self._stop_channel(user_id, key, ch)

    async def reload(
        self, user_id: str | None = None, channel: str | None = None
    ) -> dict[str, list[str]]:
        """Re-read config, stop old channels -> rebuild per new config -> start.

        Called by ``POST /channels/reload``: after the config is saved this
        reconnects with the new config (channel secret etc. is cached on the
        instance, so the instance must be rebuilt to take effect). ``self._loop``
        was set by ``start_all``; reload runs on the same event loop, so the
        rebound ``set_loop`` works.

        With ``(user_id, channel)`` this changes a single channel of a single
        user only.
        """
        if user_id is not None and channel is not None:
            return await self._reload_one(user_id, channel)
        return await self._reload_all()

    async def _reload_all(self) -> dict[str, list[str]]:
        stopped = [f"{u}/{k}" for (u, k) in self._channels.keys()]
        await self.stop_all()
        self._channels.clear()
        await self._load_enabled_channels()  # re-read store -> populate _channels
        await self.start_all()
        started = [f"{u}/{k}" for (u, k) in self._channels.keys()]
        logger.info("channel reloaded: stopped=%s started=%s", stopped, started)
        return {"stopped": stopped, "started": started}

    async def _reload_one(self, user_id: str, channel: str) -> dict[str, list[str]]:
        """Reload a single ``(user_id, channel)``: stop the old instance, rebuild, start.

        Only this channel is touched — other channels (same or other user) keep
        running and their session queues are preserved. If the channel is no
        longer enabled in config, the old instance is stopped and removed with
        nothing started in its place.
        """
        key = (user_id, channel)
        stopped: list[str] = []
        old = self._channels.pop(key, None)
        if old is not None:
            await self._stop_channel(user_id, channel, old)
            stopped.append(f"{user_id}/{channel}")

        # Re-read this user's config for this channel; start a fresh instance if enabled.
        config = await self._services.configs.load(user_id)
        ch_cfg = config.get(channel) or {}
        started: list[str] = []
        if ch_cfg.get("enabled"):
            ch = build_channel(Identity(user_id=user_id), channel)
            if ch is not None:
                ch.set_services(self._services)
                self._channels[key] = ch
                await self._start_channel(user_id, channel, ch)
                started.append(f"{user_id}/{channel}")
        logger.info("channel reloaded: stopped=%s started=%s", stopped, started)
        return {"stopped": stopped, "started": started}

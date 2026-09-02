# -*- coding: utf-8 -*-
"""Channel config API routes over the live service app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402


@asynccontextmanager
async def channels_client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http, app


async def test_channel_config_roundtrip(tmp_path, monkeypatch) -> None:
    async with channels_client(tmp_path, monkeypatch) as (http, app):
        listed = await http.get("/api/v1/system/channel-config/")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert set(body) == {"feishu", "dingtalk", "wecom", "wechat"}
        assert body["feishu"]["enabled"] is False

        updated = await http.put(
            "/api/v1/system/channel-config/feishu",
            json={"enabled": False, "app_id": "cli_1", "app_secret": "real-secret-123"},
        )
        assert updated.status_code == 200, updated.text
        cfg = updated.json()["config"]
        assert cfg["app_id"] == "cli_1"
        assert cfg["app_secret"] != "real-secret-123"  # masked in the response

        # secrets stay masked on read, real value kept in the store
        again = await http.get("/api/v1/system/channel-config/")
        assert again.json()["feishu"]["app_secret"] == cfg["app_secret"]
        state = app.state.service
        raw = await state.channel_configs.load("local")
        assert raw["feishu"]["app_secret"] == "real-secret-123"

        unknown = await http.put(
            "/api/v1/system/channel-config/telegram", json={"enabled": True}
        )
        assert unknown.status_code == 404

        test = await http.post("/api/v1/system/channel-config/feishu/test")
        assert test.status_code == 200
        assert test.json()["success"] is True

        missing = await http.post("/api/v1/system/channel-config/wecom/test")
        assert missing.json()["success"] is False

        reloaded = await http.post("/api/v1/channels/reload")
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json() == {"stopped": [], "started": []}


async def test_cron_targets_endpoint(tmp_path, monkeypatch) -> None:
    async with channels_client(tmp_path, monkeypatch) as (http, app):
        state = app.state.service
        await state.channel_bindings.point_to(
            "local", "feishu", "feishu:oc1", "ses_1",
            target_meta={"target_type": "group", "send_meta": {"chat_id": "oc1"}},
            display_name="数据群",
        )
        listed = await http.get("/api/v1/cron/targets", params={"channel": "feishu"})
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] == 1
        assert body["targets"][0]["external_key"] == "feishu:oc1"
        assert body["targets"][0]["display_name"] == "数据群"
        assert "send_meta" not in body["targets"][0]


async def test_im_cron_job_requires_known_target(tmp_path, monkeypatch) -> None:
    async with channels_client(tmp_path, monkeypatch) as (http, app):
        job = {
            "name": "日报",
            "message": "生成日报",
            "datasource_id": "ds1",
            "channel": "feishu",
            "target_external_key": "feishu:oc1",
            "schedule": {"type": "cron", "cron": "0 8 * * *"},
        }
        rejected = await http.post("/api/v1/cron/jobs", json=job)
        assert rejected.status_code == 400, rejected.text

        # console jobs stay unaffected
        console = await http.post(
            "/api/v1/cron/jobs",
            json={
                "name": "晨报",
                "message": "生成销售日报",
                "datasource_id": "ds1",
                "schedule": {"type": "cron", "cron": "0 8 * * *"},
            },
        )
        assert console.status_code == 200, console.text
        assert console.json()["job"]["channel"] == "console"

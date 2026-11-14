# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.integrations.oss import region_from_endpoint
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub


def test_region_from_endpoint() -> None:
    assert region_from_endpoint("https://oss-cn-hangzhou.aliyuncs.com") == "cn-hangzhou"
    assert region_from_endpoint("oss-cn-beijing.aliyuncs.com") == "cn-beijing"
    with pytest.raises(ValueError):
        region_from_endpoint("https://oss-accelerate.aliyuncs.com")


class _FakeStore:
    def __init__(self) -> None:
        self.put_calls = 0

    def upload_file(self, oss_path: str, file_bytes: bytes) -> None:
        self.put_calls += 1
        self.last_path = oss_path
        self.last_data = file_bytes

    def create_signed_download_url(
        self, oss_path: str, *, expires_in: int, download_filename: str
    ) -> str:
        return (
            f"https://example.invalid/{oss_path}"
            f"?expires_in={expires_in}&filename={download_filename}&disposition=attachment"
        )


def _prepare_share_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    oss_configured: bool,
) -> tuple[TestClient, _FakeStore | None]:
    asyncio.run(reset_engine())
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    if oss_configured:
        monkeypatch.setenv("QWENPAW_DATA_OSS_ACCESS_KEY_ID", "test-id")
        monkeypatch.setenv("QWENPAW_DATA_OSS_ACCESS_KEY_SECRET", "test-secret")
        monkeypatch.setenv("QWENPAW_DATA_OSS_BUCKET", "qwenpaw-data")
    else:
        monkeypatch.delenv("QWENPAW_DATA_OSS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("QWENPAW_DATA_OSS_ACCESS_KEY_SECRET", raising=False)
        monkeypatch.delenv("QWENPAW_DATA_OSS_BUCKET", raising=False)
    clear_settings()
    store = _FakeStore() if oss_configured else None
    if store is not None:
        monkeypatch.setattr(
            "qwenpaw_data.host.core.api.routers.artifact.get_oss_store",
            lambda: store,
        )
    return TestClient(create_app()), store


def test_share_api_uploads_and_rejects_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    client, store = _prepare_share_app(tmp_path, monkeypatch, oss_configured=True)
    assert store is not None
    with client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        created = client.post("/api/sessions", json={"title": "share"})
        assert created.status_code == 200, created.text
        session = created.json()["session"]
        paths = UserAgentPaths.from_identity(
            home=tmp_path / "home",
            identity=Identity(
                user_id=TEST_USER_ID,
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
            ),
            agent_id=session["agent_id"],
            session_id=session["id"],
        )
        report = paths.artifact_dir / "report.html"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("<html>share</html>", encoding="utf-8")

        first = client.post(
            f"/api/sessions/{session['id']}/files/share",
            json={"path": "report.html"},
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["name"] == "report.html"
        assert body["url"].startswith("https://example.invalid/")
        assert "disposition=attachment" in body["url"]
        assert store.put_calls == 1
        assert store.last_data == b"<html>share</html>"

        second = client.post(
            f"/api/sessions/{session['id']}/files/share",
            json={"path": "report.html"},
        )
        assert second.status_code == 200, second.text
        assert store.put_calls == 2

        traversal = client.post(
            f"/api/sessions/{session['id']}/files/share",
            json={"path": "../secret.txt"},
        )
        assert traversal.status_code == 400
        assert traversal.json()["code"] == "VALIDATION"


def test_share_api_fails_when_oss_not_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
) -> None:
    client, _ = _prepare_share_app(tmp_path, monkeypatch, oss_configured=False)
    with client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        created = client.post("/api/sessions", json={"title": "share"})
        assert created.status_code == 200, created.text
        session = created.json()["session"]
        paths = UserAgentPaths.from_identity(
            home=tmp_path / "home",
            identity=Identity(
                user_id=TEST_USER_ID,
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
            ),
            agent_id=session["agent_id"],
            session_id=session["id"],
        )
        report = paths.artifact_dir / "report.html"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("<html>x</html>", encoding="utf-8")
        response = client.post(
            f"/api/sessions/{session['id']}/files/share",
            json={"path": "report.html"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION"
        assert "OSS" in response.json()["message"]

# -*- coding: utf-8 -*-
"""Artifact list/download routes over the live service app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.db.engine import (  # noqa: E402
    TEST_TENANT_ID, TEST_USER_ID, TEST_WORKSPACE_ID, get_session_factory,
)
from qwenpaw_data.host.core.domain.identity import Identity  # noqa: E402
from qwenpaw_data.host.core.paths import UserAgentPaths  # noqa: E402
from qwenpaw_data.host.core.repositories import ArtifactRepository, ChatRepository, SessionRepository  # noqa: E402
from qwenpaw_data.host.core.settings import get_settings  # noqa: E402


@asynccontextmanager
async def artifacts_client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as http:
            created = await http.post("/api/v1/sessions", json={"title": "产物"})
            assert created.status_code == 200, created.text
            session_id = created.json()["session"]["id"]
            identity = Identity(user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID)
            artifact_dir = UserAgentPaths.from_identity(
                home=get_settings().paths.home, identity=identity,
                agent_id=created.json()["session"]["agent_id"], session_id=session_id,
            ).artifact_dir
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "chart.png").write_bytes(b"png-bytes")
            (artifact_dir / "sub").mkdir()
            (artifact_dir / "sub" / "rows.csv").write_bytes(b"a,b\n1,2\n")
            (tmp_path / "secret.txt").write_text("nope")
            async with get_session_factory()() as db:
                sessions = SessionRepository(db, identity)
                session = await sessions.get(session_id)
                chat = session.open_chat(text="Create artifacts", datasource_id=None, has_active_chat=False)
                chat.mark_status("completed")
                await sessions.save(session)
                await ChatRepository(db, identity).add(chat)
                repo = ArtifactRepository(db, identity)
                for path in ("chart.png", "sub/rows.csv"):
                    await repo.register(session_id=session_id, chat_id=chat.id, name=Path(path).name, path=path)
                await db.commit()
            yield http, session_id


async def test_list_and_download(tmp_path, monkeypatch) -> None:
    async with artifacts_client(tmp_path, monkeypatch) as (http, session_id):
        listed = await http.get(f"/api/v1/sessions/{session_id}/artifacts")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert len(body["artifacts"]) == 2
        rel_paths = {item["path"] for item in body["artifacts"]}
        assert rel_paths == {"chart.png", "sub/rows.csv"}

        downloaded = await http.get(
            f"/api/v1/sessions/{session_id}/artifacts/file",
            params={"path": "chart.png"},
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"png-bytes"
        assert "chart.png" in downloaded.headers.get("content-disposition", "")

        nested = await http.get(
            f"/api/v1/sessions/{session_id}/artifacts/file",
            params={"path": "sub/rows.csv"},
        )
        assert nested.status_code == 200
        assert nested.text == "a,b\n1,2\n"


async def test_download_rejects_traversal_and_missing(
    tmp_path, monkeypatch
) -> None:
    async with artifacts_client(tmp_path, monkeypatch) as (http, session_id):
        for bad in (
            "../secret.txt",
            "../../secret.txt",
            "..%2Fsecret.txt",
            "",
            "/etc/hosts",
            "~/secret.txt",
            "sub/../../secret.txt",
            "sub\\..\\secret.txt",
            "./chart.png",
            "sub//rows.csv",
        ):
            response = await http.get(
                f"/api/v1/sessions/{session_id}/artifacts/file",
                params={"path": bad},
            )
            assert response.status_code in (404, 422), (bad, response.text)

        missing = await http.get(
            f"/api/v1/sessions/{session_id}/artifacts/file",
            params={"path": "nope.bin"},
        )
        assert missing.status_code == 404


async def test_unknown_session_is_not_found(tmp_path, monkeypatch) -> None:
    async with artifacts_client(tmp_path, monkeypatch) as (http, _session_id):
        response = await http.get("/api/v1/sessions/ses_missing/artifacts")
        assert response.status_code == 404

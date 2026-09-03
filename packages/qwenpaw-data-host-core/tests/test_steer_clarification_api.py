# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.domain.clarification import (  # noqa: E402
    ClarificationConflict,
)
from qwenpaw_data.host.core.domain.identity import Identity  # noqa: E402
from qwenpaw_data.host.core.domain.session import Session  # noqa: E402
from qwenpaw_data.host.core.runtime.registry import (  # noqa: E402
    get_runtime_registry,
)


class FakeRuntime:
    def __init__(self) -> None:
        self.steers: list[tuple[str, list[dict[str, Any]]]] = []
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def steer(
        self,
        text: str,
        artifact_comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.steers.append((text, list(artifact_comments or [])))

    def answer(self, *, clarification_id: str, result: dict[str, Any]) -> None:
        if clarification_id == "clar_conflict":
            raise ClarificationConflict(
                "clarification is not awaiting this answer",
                reason="CLARIFICATION_ALREADY_RESOLVED",
            )
        self.answers.append((clarification_id, result))


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch):
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


async def _seed_running_chat(app) -> tuple[str, str]:
    """Create a session + running chat directly in the stores."""
    state = app.state.service
    session = Session.create(identity=Identity.anonymous())
    await state.sessions.add(session)
    chat = session.open_chat(text="hi", datasource_id=None, has_active_chat=False)
    await state.sessions.save(session)
    await state.chats.add(chat)
    return session.id, chat.id


ANSWER_BODY = {
    "clarification_id": "clar_1",
    "result": {
        "status": "answered",
        "answers": [
            {"question": "哪个区域？", "selected_options": ["华东"]}
        ],
    },
}


async def test_steer_paths(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        # missing chat → 404
        missing = await http.post(
            "/api/v1/sessions/s/chats/c/steer", json={"text": "x"}
        )
        assert missing.status_code == 404

        sid, cid = await _seed_running_chat(app)

        # running chat but no in-flight runtime → 409 CHAT_ENDED
        gone = await http.post(
            f"/api/v1/sessions/{sid}/chats/{cid}/steer", json={"text": "x"}
        )
        assert gone.status_code == 409
        assert gone.json()["details"]["reason"] == "CHAT_ENDED"

        # live runtime → 204 and text delivered
        runtime = FakeRuntime()
        get_runtime_registry().register(cid, runtime)  # type: ignore[arg-type]
        try:
            ok = await http.post(
                f"/api/v1/sessions/{sid}/chats/{cid}/steer",
                json={"text": "聚焦华东"},
            )
            assert ok.status_code == 204
            assert runtime.steers == [("聚焦华东", [])]

            commented = await http.post(
                f"/api/v1/sessions/{sid}/chats/{cid}/steer",
                json={
                    "text": "改一下图",
                    "artifact_comments": [
                        {
                            "path": "out/chart.png",
                            "line_start": 1,
                            "line_end": 1,
                            "comment": "换成柱状图",
                        }
                    ],
                },
            )
            assert commented.status_code == 204
            text, comments = runtime.steers[-1]
            assert text == "改一下图"
            assert comments[0]["comment"] == "换成柱状图"
        finally:
            get_runtime_registry().unregister(cid)

        # terminal chat → 409 even with a runtime present
        chat = await app.state.service.chats.get(cid)
        chat.mark_status("completed")
        await app.state.service.chats.save(chat)
        done = await http.post(
            f"/api/v1/sessions/{sid}/chats/{cid}/steer", json={"text": "late"}
        )
        assert done.status_code == 409


async def test_clarification_paths(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_running_chat(app)

        # no runtime → 404 clarification not found
        no_runtime = await http.post(
            f"/api/v1/sessions/{sid}/chats/{cid}/clarification/answer",
            json=ANSWER_BODY,
        )
        assert no_runtime.status_code == 404

        runtime = FakeRuntime()
        get_runtime_registry().register(cid, runtime)  # type: ignore[arg-type]
        try:
            ok = await http.post(
                f"/api/v1/sessions/{sid}/chats/{cid}/clarification/answer",
                json=ANSWER_BODY,
            )
            assert ok.status_code == 200
            assert ok.json()["chat"]["id"] == cid
            clar_id, result = runtime.answers[0]
            assert clar_id == "clar_1"
            assert result["status"] == "answered"

            conflict = await http.post(
                f"/api/v1/sessions/{sid}/chats/{cid}/clarification/answer",
                json={**ANSWER_BODY, "clarification_id": "clar_conflict"},
            )
            assert conflict.status_code == 409

            bad = await http.post(
                f"/api/v1/sessions/{sid}/chats/{cid}/clarification/answer",
                json={"clarification_id": "x", "result": {"status": "nonsense"}},
            )
            assert bad.status_code == 422
        finally:
            get_runtime_registry().unregister(cid)

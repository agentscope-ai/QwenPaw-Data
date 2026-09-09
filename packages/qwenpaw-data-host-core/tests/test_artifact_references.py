# -*- coding: utf-8 -*-
"""Declared deliverables must be referenced in the message timeline.

``artifact.registered`` is sorted into its own snapshot bucket, so before
this a generated report was reachable from the Outputs panel yet had no
reference anywhere in the conversation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.orchestration.artifact import ArtifactItem
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.envelope import Envelope
from qwenpaw_data.host.core.store.json_store import JSONChatEventStore
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream


class RecordingStream:
    """Records emitter calls; carries a real session_id for URL building."""

    session_id = "ses_WB7ux84g"
    chat_id = "chat_1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        async def record(*args: Any, **kwargs: Any) -> None:
            if args:
                assert len(args) == 1 and isinstance(args[0], dict)
                self.calls.append((name, args[0]))
            else:
                self.calls.append((name, kwargs))

        return record

    def named(self, name: str) -> list[dict[str, Any]]:
        return [payload for call, payload in self.calls if call == name]


async def test_declared_artifact_becomes_a_file_block_on_a_message() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)  # type: ignore[arg-type]

    await envelope.send_artifact_message(
        [
            {
                "name": "report.html",
                "path": "graph_286KNtmx/report/report.html",
                "mime_type": "text/html",
            }
        ]
    )

    assert [call for call, _ in stream.calls] == [
        "message_start",
        "file_end",
        "message_complete",
    ]
    (completed,) = stream.named("message_complete")
    assert completed["role"] == "assistant"
    assert completed["content"] == [
        {
            "object": "content",
            "type": "file",
            "delta": False,
            "index": 0,
            "filename": "report.html",
            "file_url": (
                "/api/v1/sessions/ses_WB7ux84g/files/access"
                "?path=graph_286KNtmx%2Freport%2Freport.html&purpose=preview"
            ),
        }
    ]


async def test_file_url_points_at_the_bearer_route_not_a_signed_link() -> None:
    """A signed /files/shared link is itself the credential; persisting one
    into a chat event would bake a replayable credential into the DB."""
    stream = RecordingStream()
    envelope = Envelope(stream)  # type: ignore[arg-type]

    await envelope.send_artifact_message(
        [{"name": "a b.csv", "path": "node/../a b.csv", "mime_type": "text/csv"}]
    )

    (block,) = stream.named("file_end")
    parsed = urlparse(block["file_url"])
    assert parsed.path == "/api/v1/sessions/ses_WB7ux84g/files/access"
    assert "shared" not in block["file_url"]
    assert "sig" not in parse_qs(parsed.query)
    # Traversal and spaces survive as an opaque escaped value; containment is
    # enforced by resolve_session_file when the route is actually called.
    assert parse_qs(parsed.query)["path"] == ["node/../a b.csv"]


async def test_emitted_events_are_valid_on_the_wire(tmp_path: Path) -> None:
    reset_hub()
    try:
        events = JSONChatEventStore(tmp_path)
        stream = OutputStream(
            events,
            session_id="ses_WB7ux84g",
            chat_id="chat_1",
            identity=Identity.anonymous(),
        )
        await Envelope(stream).send_artifact_message(
            [{"name": "report.html", "path": "report/report.html"}]
        )

        persisted = await events.read_after("chat_1", -1)
        assert [obj.object for obj in persisted] == [
            "message",
            "content",
            "message",
        ]
        content = persisted[1]
        assert content.type == "file"
        assert content.filename == "report.html"
        # The raw trace router replays object == "message", so the reference
        # survives a reload, not just the live stream.
        completed = persisted[2]
        assert completed.status == "completed"
        assert completed.content[0].type == "file"
        assert completed.content[0].file_url == content.file_url
    finally:
        reset_hub()


async def test_nothing_is_emitted_without_a_usable_artifact() -> None:
    for artifacts in ([], [{"name": "orphan"}], [{"path": "no/name.html"}]):
        stream = RecordingStream()
        envelope = Envelope(stream)  # type: ignore[arg-type]
        await envelope.send_artifact_message(artifacts)  # type: ignore[arg-type]
        assert stream.calls == []


async def test_nothing_is_emitted_after_the_terminal_frame() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)  # type: ignore[arg-type]
    await envelope.complete()
    stream.calls.clear()

    await envelope.send_artifact_message(
        [{"name": "report.html", "path": "report/report.html"}]
    )

    assert stream.calls == []


class FakeNotebook:
    def __init__(self, artifacts: list[ArtifactItem]) -> None:
        self.artifacts = artifacts


class FakeAgent:
    def __init__(self, artifacts: list[ArtifactItem]) -> None:
        self.plan_notebook = FakeNotebook(artifacts)


def _item(name: str, path: str) -> ArtifactItem:
    return ArtifactItem(
        graph_id="graph_1",
        node_id="node_1",
        name=name,
        path=path,
        mime_type="text/html",
    )


def _runtime(artifacts: list[ArtifactItem]) -> ChatRuntime:
    runtime = ChatRuntime(chats=None, events=None, hosts=None)  # type: ignore[arg-type]
    runtime._agent = FakeAgent(artifacts)
    return runtime


def test_only_this_turns_declared_artifacts_are_referenced() -> None:
    """The agent is session-scoped and its artifact list append-only, so an
    earlier turn's report must not be re-announced."""
    artifacts = [_item("old.html", "graph_0/old.html")]
    runtime = _runtime(artifacts)
    runtime._declared_artifact_offset = len(artifacts)

    artifacts.append(_item("new.html", "graph_1/new.html"))

    assert runtime._declared_artifacts() == [
        {
            "name": "new.html",
            "path": "graph_1/new.html",
            "mime_type": "text/html",
        }
    ]


def test_a_regenerated_path_is_referenced_once() -> None:
    runtime = _runtime(
        [
            _item("report.html", "graph_1/report.html"),
            _item("report.html", "graph_1/report.html"),
        ]
    )

    assert len(runtime._declared_artifacts()) == 1


def test_missing_agent_or_notebook_yields_no_references() -> None:
    runtime = ChatRuntime(chats=None, events=None, hosts=None)  # type: ignore[arg-type]
    assert runtime._declared_artifacts() == []

    runtime._agent = object()
    assert runtime._declared_artifacts() == []


async def test_persisted_file_url_resolves_against_the_real_app(
    tmp_path: Path, monkeypatch
) -> None:
    """A wrong route prefix would turn every reference into a dead link."""
    pytest.importorskip("fastapi")
    import httpx

    from qwenpaw_data.host.core.api.app import create_app

    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    reset_hub()
    try:
        app = create_app(home=tmp_path, model=object())
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                created = await http.post("/api/v1/sessions", json={"title": "月报"})
                session_id = created.json()["session"]["id"]
                chat = await http.post(
                    "/api/v1/console/chat",
                    json={"session_id": session_id, "text": "出个报告"},
                )
                chat_id = chat.json()["chat"]["id"]
                state = app.state.service
                artifact_dir = Path(
                    state.hosts.get(session_id=session_id).paths.artifact_dir
                )
                report = artifact_dir / "graph_1" / "report" / "report.html"
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text("<h1>DAU</h1>", encoding="utf-8")

                stream = OutputStream(
                    state.events,
                    session_id=session_id,
                    chat_id=chat_id,
                    identity=Identity.anonymous(),
                )
                await Envelope(stream).send_artifact_message(
                    [
                        {
                            "name": "report.html",
                            "path": "graph_1/report/report.html",
                            "mime_type": "text/html",
                        }
                    ]
                )
                # The chat's own run appends events too; find ours by shape.
                file_urls = [
                    block.file_url
                    for obj in await state.events.read_after(chat_id, -1)
                    if obj.object == "message"
                    for block in obj.content
                    if block.type == "file"
                ]
                assert len(file_urls) == 1
                file_url = file_urls[0]

                served = await http.get(file_url)
                assert served.status_code == 200, served.text
                assert served.text == "<h1>DAU</h1>"
                assert "inline" in served.headers["content-disposition"]
    finally:
        reset_hub()

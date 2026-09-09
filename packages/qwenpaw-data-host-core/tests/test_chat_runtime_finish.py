# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime


class FakeChats:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def reload_event_watermark(self, _chat: Any) -> None:
        self.calls.append("reload")

    async def save(self, _chat: Any) -> None:
        self.calls.append("save")


class FakeEnvelope:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def complete(self) -> None:
        self.calls.append("complete")

    async def cancel(self) -> None:
        self.calls.append("cancel")


class FakeChat:
    id = "chat-1"
    session_id = "session-1"
    identity = None
    error: dict[str, Any] | None = None

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def mark_status(self, status: str) -> None:
        self.calls.append(f"status:{status}")

    def cancel(self) -> None:
        self.calls.append("status:canceled")


def _runtime(chats: FakeChats) -> ChatRuntime:
    return ChatRuntime(chats=chats, events=None, hosts=None)  # type: ignore[arg-type]


async def test_completed_saves_before_completing_response() -> None:
    calls: list[str] = []
    runtime = _runtime(FakeChats(calls))
    await runtime._finish(
        FakeChat(calls),  # type: ignore[arg-type]
        FakeEnvelope(calls),  # type: ignore[arg-type]
        "completed",
    )

    assert calls == ["status:completed", "reload", "save", "complete"]


async def test_canceled_saves_before_cancelling_response() -> None:
    calls: list[str] = []
    runtime = _runtime(FakeChats(calls))
    await runtime._finish(
        FakeChat(calls),  # type: ignore[arg-type]
        FakeEnvelope(calls),  # type: ignore[arg-type]
        "canceled",
    )

    assert calls == ["status:canceled", "reload", "save", "cancel"]


async def test_failed_with_no_envelope_still_marks_failed(monkeypatch) -> None:
    """If preparation fails before the envelope exists, _finish must still:
    (1) emit a terminal response failed event (otherwise consumers hang);
    (2) mark the chat failed and persist it.
    Regression: this path used to leave chats stuck in running forever.
    """
    calls: list[str] = []
    published: list[dict[str, Any]] = []

    class FakeOutputStream:
        def __init__(
            self, events: Any, *, session_id: str, chat_id: str, identity: Any
        ) -> None:
            pass

        async def response_failed(self, *, error: dict[str, Any]) -> None:
            published.append(error)
            calls.append("failed_event")

    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.OutputStream",
        FakeOutputStream,
    )

    chat = FakeChat(calls)
    runtime = _runtime(FakeChats(calls))
    await runtime._finish(
        chat,  # type: ignore[arg-type]
        None,  # envelope=None — preparation failed before it was built
        "failed",
        error={"code": "VALIDATION", "message": "model is not configured"},
    )

    assert published == [
        {"code": "VALIDATION", "message": "model is not configured"}
    ]
    assert chat.error == {
        "code": "VALIDATION",
        "message": "model is not configured",
    }
    assert calls == ["status:failed", "reload", "save", "failed_event"]

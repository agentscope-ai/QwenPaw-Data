# -*- coding: utf-8 -*-
"""Regression: a failed ``ChatRuntime.run`` must not leave its chat stuck
``running`` — otherwise every later ``_consume_one_request`` for the session
hits "session already has an active chat". The channel layer now reconciles
the row to ``failed`` once the run task is done.
"""

from __future__ import annotations

import asyncio
from typing import Any

from qwenpaw_data.host.core.channels import base as base_module
from qwenpaw_data.host.core.channels.base import BaseChannel
from qwenpaw_data.host.core.domain.identity import Identity


class _ConcreteChannel(BaseChannel):
    channel = "test"

    async def start(self) -> None: ...

    async def _stop(self) -> None: ...

    async def stop(self) -> None: ...

    def resolve_session_id(self, native: Any) -> str:
        return ""

    def extract_target_meta(self, native: Any) -> dict[str, Any] | None:
        return None

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None: ...

    async def on_consume_start(self, native: Any) -> None: ...

    async def on_consume_end(self, native: Any, handle: Any, status: str) -> None: ...

    async def send(self, native: Any, text: str) -> None: ...

    async def send_image(self, native: Any, path: str) -> None: ...

    async def send_file(self, native: Any, path: str) -> None: ...

    async def send_datasource_card(
        self, native: Any, *args: Any, **kwargs: Any
    ) -> None: ...

    async def send_clarification_card(self, native: Any, questions: Any) -> str:
        return ""

    async def on_streaming_start(self, native: Any, msg_id: str) -> Any:
        return None

    async def on_streaming_delta(
        self, native: Any, handle: Any, delta: str
    ) -> None: ...

    async def on_streaming_reasoning_delta(
        self, native: Any, handle: Any, text: str
    ) -> None: ...

    async def on_streaming_end(self, native: Any, handle: Any, text: str) -> None: ...

    async def on_streaming_close(
        self, native: Any, handle: Any, summary: str
    ) -> None: ...


class FakeDb:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def get(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def add(self, _row: Any) -> None:
        self.calls.append("score")

    async def commit(self) -> None:
        self.calls.append("commit")


class FakeChat:
    def __init__(self, status: str, calls: list[str]) -> None:
        self.status = status
        self.calls = calls
        self.id = "chat-1"
        self.session_id = "session-1"
        self.identity = Identity(user_id="u", tenant_id="t", workspace_id="w")
        self.last_sequence_number = -1
        self.error: dict[str, Any] | None = None

    def is_active(self) -> bool:
        return self.status == "running"

    def mark_failed(self, failure: Any) -> None:
        self.calls.append("status:failed")
        self.status = "failed"
        self.error = failure.to_dict()


def _install_fakes(
    monkeypatch,
    chat: FakeChat,
    calls: list[str],
    failed_errors: list[dict[str, Any]],
) -> None:
    class FakeChatRepository:
        def __init__(self, db: Any, identity: Any) -> None:
            pass

        async def get(self, chat_id: str) -> FakeChat:
            return chat

        async def reload_event_watermark(self, _chat: Any) -> None:
            calls.append("reload")

        async def save(self, _chat: Any) -> None:
            calls.append("save")

    class FakeOutputStream:
        def __init__(
            self,
            session_factory: Any,
            *,
            session_id: str,
            chat_id: str,
            identity: Any,
            last_sequence_number: int,
        ) -> None:
            pass

        async def response_failed(self, *, error: dict[str, Any]) -> None:
            failed_errors.append(error)

    class FakeSessionFactory:
        def __call__(self) -> Any:
            class _Ctx:
                async def __aenter__(_self) -> FakeDb:
                    return FakeDb(calls)

                async def __aexit__(_self, *exc: Any) -> None:
                    return None

            return _Ctx()

    monkeypatch.setattr(base_module, "ChatRepository", FakeChatRepository)
    monkeypatch.setattr(base_module, "OutputStream", FakeOutputStream)
    monkeypatch.setattr(base_module, "get_session_factory", FakeSessionFactory)


async def test_force_failed_noop_when_terminal(monkeypatch) -> None:
    calls: list[str] = []
    failed_errors: list[dict[str, Any]] = []
    chat = FakeChat("completed", calls)
    _install_fakes(monkeypatch, chat, calls, failed_errors)

    channel = _ConcreteChannel()
    await channel._force_failed_if_running("chat-1", "ses-1", None)

    assert chat.status == "completed"
    assert failed_errors == []
    assert calls == []  # already terminal: no save / reload / commit
    assert "score" not in calls  # 终态不写 chat_status score


async def test_force_failed_reconciles_running_chat(monkeypatch) -> None:
    calls: list[str] = []
    failed_errors: list[dict[str, Any]] = []
    chat = FakeChat("running", calls)
    _install_fakes(monkeypatch, chat, calls, failed_errors)

    channel = _ConcreteChannel()
    await channel._force_failed_if_running("chat-1", "ses-1", None)

    assert chat.status == "failed"
    assert calls == ["status:failed", "save", "score", "commit"]
    assert len(failed_errors) == 1
    assert failed_errors[0]["code"] == "INTERNAL"


async def test_finalize_reconciles_when_run_raises(monkeypatch) -> None:
    """run() raised before/within _finish → commit never landed → reconcile."""
    calls: list[str] = []
    failed_errors: list[dict[str, Any]] = []
    chat = FakeChat("running", calls)
    _install_fakes(monkeypatch, chat, calls, failed_errors)

    async def boom() -> None:
        raise RuntimeError("workspace acquire failed")

    run_task = asyncio.create_task(boom())

    channel = _ConcreteChannel()
    await channel._finalize_chat_run("chat-1", "ses-1", run_task, None)

    assert run_task.done()
    assert chat.status == "failed"  # reconciled, next request won't CONFLICT
    assert failed_errors  # terminal response event emitted

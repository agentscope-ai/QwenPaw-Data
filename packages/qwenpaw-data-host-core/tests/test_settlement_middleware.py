# -*- coding: utf-8 -*-
"""Prompt middleware and ChatRuntime settlement scheduling."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import qwenpaw_data.host.core.runtime.chat_runtime as chat_runtime_module
from qwenpaw_data.host.core.agent.middleware import (
    ConfirmedSettlementPromptMiddleware,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime


def _agent(session_id: str | None = "sess1") -> Any:
    if session_id is None:
        return SimpleNamespace()
    return SimpleNamespace(session_id=session_id)


def test_format_confirmed_settlement_cards() -> None:
    text = ConfirmedSettlementPromptMiddleware.format_cards(
        [
            {
                "type": "metric_caliber",
                "fields": {"caliber": "支付金额", "metric_name": "GMV"},
            }
        ]
    )
    assert text.startswith("<confirmed_settlement_cards>")
    assert text.endswith("</confirmed_settlement_cards>")
    assert "type=metric_caliber" in text
    assert '"metric_name": "GMV"' in text
    assert '"caliber": "支付金额"' in text


async def test_prompt_middleware_appends_cards() -> None:
    seen: list[str] = []

    async def loader(session_id: str):
        seen.append(session_id)
        return [{"type": "metric_caliber", "fields": {"metric_name": "GMV"}}]

    mw = ConfirmedSettlementPromptMiddleware(loader=loader)
    prompt = await mw.on_system_prompt(_agent(), "BASE")
    assert prompt.startswith("BASE\n\n<confirmed_settlement_cards>")
    assert "GMV" in prompt
    assert seen == ["sess1"]


async def test_prompt_middleware_degrades_safely() -> None:
    async def empty(session_id: str):
        return []

    async def boom(session_id: str):
        raise RuntimeError("store down")

    assert (
        await ConfirmedSettlementPromptMiddleware(loader=empty).on_system_prompt(
            _agent(), "BASE"
        )
        == "BASE"
    )
    assert (
        await ConfirmedSettlementPromptMiddleware(loader=boom).on_system_prompt(
            _agent(), "BASE"
        )
        == "BASE"
    )
    assert (
        await ConfirmedSettlementPromptMiddleware(loader=empty).on_system_prompt(
            _agent(session_id=None), "BASE"
        )
        == "BASE"
    )


def _chat() -> Chat:
    return Chat.start(
        session_id="sess1",
        identity=Identity.anonymous(),
        sequence=1,
        datasource_id="ds1",
        text="hi",
    )


class _FakeManager:
    instances: list["_FakeManager"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.finished: list[tuple[str, str]] = []
        self.done = asyncio.Event()
        _FakeManager.instances.append(self)

    async def on_chat_finish(self, *, chat_id: str, session_id: str) -> None:
        self.finished.append((chat_id, session_id))
        self.done.set()


async def test_runtime_schedules_settlement_on_completed(monkeypatch) -> None:
    _FakeManager.instances = []
    monkeypatch.setattr(chat_runtime_module, "SettlementManager", _FakeManager)
    runtime = ChatRuntime(
        chats=object(),  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        hosts=None,  # type: ignore[arg-type]
        sessions=object(),
        settlement=object(),
    )
    chat = _chat()
    identity = Identity.anonymous()

    runtime._schedule_settlement(chat, identity)

    (manager,) = _FakeManager.instances
    await asyncio.wait_for(manager.done.wait(), timeout=1)
    assert manager.finished == [(chat.id, "sess1")]
    assert manager.kwargs["identity"] is identity


async def test_runtime_skips_settlement_without_stores(monkeypatch) -> None:
    _FakeManager.instances = []
    monkeypatch.setattr(chat_runtime_module, "SettlementManager", _FakeManager)
    runtime = ChatRuntime(
        chats=object(),  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        hosts=None,  # type: ignore[arg-type]
    )
    runtime._schedule_settlement(_chat(), Identity.anonymous())
    assert _FakeManager.instances == []


async def test_runtime_respects_disabled_env(monkeypatch) -> None:
    _FakeManager.instances = []
    monkeypatch.setattr(chat_runtime_module, "SettlementManager", _FakeManager)
    monkeypatch.setenv("QWENPAW_DATA_SETTLEMENT_ENABLED", "0")
    runtime = ChatRuntime(
        chats=object(),  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        hosts=None,  # type: ignore[arg-type]
        sessions=object(),
        settlement=object(),
    )
    runtime._schedule_settlement(_chat(), Identity.anonymous())
    assert _FakeManager.instances == []

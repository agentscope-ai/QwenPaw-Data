# -*- coding: utf-8 -*-
"""Settlement prompt guard: a failing card load degrades the turn safely.

Re-graft of the OSS 0.3 behavior: when confirmed settlement cards cannot be
loaded (or formatted), the system prompt is returned unchanged, a warning is
logged, and nothing partial is appended.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw_data.host.core.agent.middleware import settlement as settlement_mod
from qwenpaw_data.host.core.agent.middleware.settlement import (
    ConfirmedSettlementPromptMiddleware,
)
from qwenpaw_data.host.core.settings import clear_settings

PROMPT = "You are a data agent."


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "QWENPAW_DATA_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
    )
    clear_settings()
    yield
    clear_settings()


def _session_factory(*, fail_open: bool = False):
    @asynccontextmanager
    async def factory():
        if fail_open:
            raise ConnectionError("database unavailable")
        yield object()

    return factory


def _middleware(*, fail_open: bool = False) -> ConfirmedSettlementPromptMiddleware:
    run_context = SimpleNamespace(session_id="sess-1", chat_id="chat-1")
    envelope = SimpleNamespace(
        _session_factory=_session_factory(fail_open=fail_open),
        stream=SimpleNamespace(identity=SimpleNamespace(user_id="u1")),
    )
    return ConfirmedSettlementPromptMiddleware(
        run_context=run_context,  # type: ignore[arg-type]
        envelope=envelope,  # type: ignore[arg-type]
    )


def _fake_repository(cards: Any = None, exc: BaseException | None = None):
    class _Repo:
        def __init__(self, db: Any, identity: Any) -> None:
            self.db = db
            self.identity = identity

        async def list_by_session(self, session_id: str, status: Any = None):
            assert session_id == "sess-1"
            assert status == "confirmed"
            if exc is not None:
                raise exc
            return cards

    return _Repo


async def test_confirmed_cards_are_appended(monkeypatch) -> None:
    cards = [{"type": "metric", "fields": {"name": "gmv", "unit": "CNY"}}]
    monkeypatch.setattr(
        settlement_mod, "SettlementRepository", _fake_repository(cards)
    )

    prompt = await _middleware().on_system_prompt(None, PROMPT)

    assert prompt.startswith(PROMPT + "\n\n<confirmed_settlement_cards>")
    assert prompt.endswith("</confirmed_settlement_cards>")
    assert '"name": "gmv"' in prompt


async def test_no_cards_keeps_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        settlement_mod, "SettlementRepository", _fake_repository([])
    )

    assert await _middleware().on_system_prompt(None, PROMPT) == PROMPT


async def test_repository_failure_degrades_with_warning(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        settlement_mod,
        "SettlementRepository",
        _fake_repository(exc=RuntimeError("no such table: settlement_cards")),
    )

    with caplog.at_level(logging.WARNING, logger=settlement_mod.__name__):
        prompt = await _middleware().on_system_prompt(None, PROMPT)

    assert prompt == PROMPT
    assert any(
        rec.levelno == logging.WARNING and "sess-1" in rec.getMessage()
        for rec in caplog.records
    )


async def test_session_factory_failure_degrades(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        settlement_mod, "SettlementRepository", _fake_repository([])
    )

    with caplog.at_level(logging.WARNING, logger=settlement_mod.__name__):
        prompt = await _middleware(fail_open=True).on_system_prompt(None, PROMPT)

    assert prompt == PROMPT
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


async def test_malformed_cards_leave_no_partial_prompt(monkeypatch, caplog) -> None:
    cards = [
        {"type": "metric", "fields": {"name": "gmv"}},
        {"fields": {"name": "missing type"}},
    ]
    monkeypatch.setattr(
        settlement_mod, "SettlementRepository", _fake_repository(cards)
    )

    with caplog.at_level(logging.WARNING, logger=settlement_mod.__name__):
        prompt = await _middleware().on_system_prompt(None, PROMPT)

    assert prompt == PROMPT
    assert "<confirmed_settlement_cards>" not in prompt
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)

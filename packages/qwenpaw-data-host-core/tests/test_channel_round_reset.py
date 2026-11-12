# -*- coding: utf-8 -*-
"""Base ``_stream_to_platform`` round-reset: a multi-round agent turn must
not reprint earlier rounds' reasoning/answer text into the same card."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw_data.host.core.channels import base as base_mod
from qwenpaw_data.host.core.channels.base import BaseChannel
from qwenpaw_data.host.core.channels.schema import NativePayload


class _RecordingChannel(BaseChannel):
    """Minimal BaseChannel that mirrors answer deltas into ``handle["full_text"]``
    and records round-resets / final full-text — enough to assert round behaviour."""

    channel = "test"
    streaming_enabled = True

    def __init__(self) -> None:
        super().__init__()
        self.round_resets: int = 0
        self.reasoning_pushes: list[str] = []
        self.end_full_text: str | None = None

    async def start(self) -> None:
        pass

    def resolve_session_id(self, native: Any) -> str:
        return "test-session"

    async def _stop(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def extract_target_meta(self, native: Any) -> dict[str, Any] | None:
        return None

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        pass

    async def send(self, native: Any, text: str) -> None:
        pass

    async def on_streaming_start(self, native: Any, msg_id: str) -> Any:
        return {"full_text": "", "last_update": 0.0}

    async def on_streaming_delta(self, native: Any, handle: Any, delta: str) -> None:
        handle["full_text"] = handle.get("full_text", "") + delta

    async def on_streaming_end(self, native: Any, handle: Any, full_text: str) -> None:
        self.end_full_text = full_text

    async def on_streaming_close(self, native: Any, handle: Any, summary: str) -> None:
        pass

    async def on_streaming_reasoning_delta(
        self, native: Any, handle: Any, accumulated: str
    ) -> None:
        self.reasoning_pushes.append(accumulated)

    async def on_streaming_round_reset(self, native: Any, handle: Any) -> None:
        self.round_resets += 1
        handle["full_text"] = ""

    async def on_consume_start(self, native: Any) -> None:
        pass

    async def on_consume_end(self, native: Any, handle: Any, status: str) -> None:
        pass

    async def send_image(self, native: Any, path: str) -> None:
        pass

    async def send_file(self, native: Any, path: str) -> None:
        pass

    async def send_followups(self, native: Any, questions: list[str]) -> None:
        pass

    async def send_datasource_card(
        self, native: Any, session: Any, items: list[Any], selectable: bool = True
    ) -> None:
        pass

    async def send_clarification_card(self, native: Any, questions: Any) -> str:
        return ""


def _native() -> NativePayload:
    return NativePayload(channel_id="test", sender_id="ou_s", content_parts=[])


class _FakeHub:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def _gen(self):
        for e in self._events:
            yield e

    def subscribe_live(self, chat_id: str):  # noqa: ANN201
        return self._gen()


def _msg(mid: str, mtype: str) -> SimpleNamespace:
    return SimpleNamespace(object="message", status="in_progress", type=mtype, id=mid)


def _content(text: str, mid: str) -> SimpleNamespace:
    return SimpleNamespace(
        object="content", delta=True, type="text", text=text, msg_id=mid
    )


@pytest.mark.asyncio
async def test_round_reset_clears_prior_round_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two agent rounds in one turn. Without a reset, round 2's reasoning/answer
    append to round 1's, so the same reasoning is reprinted and the answer card
    shows "ans1ans2". With the reset, round 2 shows only its own text."""
    ch = _RecordingChannel()
    events = [
        _msg("r1", "reasoning"),
        _content("I need to load data-intent-router", "r1"),
        _msg("m1", "message"),
        _content("ans1", "m1"),
        # --- new agent round ---
        _msg("r2", "reasoning"),
        _content("I need to load data-intent-router", "r2"),
        _msg("m2", "message"),
        _content("ans2", "m2"),
    ]
    monkeypatch.setattr(base_mod, "get_hub", lambda: _FakeHub(events))

    await ch._stream_to_platform(_native(), "chat-1", "ses-1", Path("/tmp/arts"))

    # Round reset fired exactly once (at the start of round 2).
    assert ch.round_resets == 1
    # The final card shows only round 2's answer, not "ans1ans2".
    assert ch.end_full_text == "ans2"
    # The last reasoning push shows round 2's text once, not duplicated.
    assert ch.reasoning_pushes[-1] == "I need to load data-intent-router"


@pytest.mark.asyncio
async def test_no_reset_within_single_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """One round (reasoning then answer, new message_ids) must NOT reset —
    the reset only fires once an answer has already streamed (answer_streaming)."""
    ch = _RecordingChannel()
    events = [
        _msg("r1", "reasoning"),
        _content("thinking", "r1"),
        _msg("m1", "message"),
        _content("the answer", "m1"),
    ]
    monkeypatch.setattr(base_mod, "get_hub", lambda: _FakeHub(events))

    await ch._stream_to_platform(_native(), "chat-1", "ses-1", Path("/tmp/arts"))

    assert ch.round_resets == 0
    assert ch.end_full_text == "the answer"

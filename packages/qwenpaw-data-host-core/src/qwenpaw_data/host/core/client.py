"""Async client for the current Host API; never constructs a local runtime."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import httpx


class HostClientError(RuntimeError):
    """Host transport, protocol or turn failure safe to show in CLI errors."""


def _id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid Host resource identifier")
    return value


class HostClient:
    """Use server-side identity, model preferences and workspace configuration.

    ``base_url`` is the Host origin (not the Console gateway). Tokens are passed
    explicitly; this library never reads environment credentials. Use an async
    context manager and close partially consumed ``run`` streams with aclosing.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError(
                "host URL must be an HTTP(S) origin without credentials or path"
            )
        self._http = httpx.AsyncClient(
            base_url=str(url).rstrip("/") + "/api/",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(30, read=90),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._active: dict[str, str] = {}

    async def __aenter__(self) -> HostClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if not response.is_success:
            # Do not reflect URLs, response bodies or credentials in errors.
            raise HostClientError(f"Host request failed (HTTP {response.status_code})")

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
            self._check(response)
            return response.json()
        except httpx.HTTPError:
            raise HostClientError("Host connection failed or timed out") from None

    async def create_session(self, *, datasource_id: str | None = None) -> str:
        body = await self._request(
            "POST",
            "sessions",
            json={
                "datasource_id": datasource_id,
                "enable_clarification": False,
            },
        )
        return _id(body["session"]["id"])

    async def stop(self, session_id: str, chat_id: str) -> None:
        await self._request(
            "POST",
            "console/chat/stop",
            params={
                "session_id": _id(session_id),
                "chat_id": _id(chat_id),
            },
        )
        self._active.pop(chat_id, None)

    async def answer_tool_confirmation(
        self,
        session_id: str,
        chat_id: str,
        call_id: str,
        *,
        approved: bool,
    ) -> None:
        await self._request(
            "POST",
            f"sessions/{_id(session_id)}/chats/{_id(chat_id)}/tool-confirmation/answer",
            json={"call_id": call_id, "decision": "approved" if approved else "denied"},
        )

    async def events(
        self,
        session_id: str,
        chat_id: str,
        *,
        after_sequence_number: int = -1,
    ) -> AsyncIterator[dict[str, Any]]:
        """Read/replay existing events without creating or cancelling a turn."""
        path = f"sessions/{_id(session_id)}/chats/{_id(chat_id)}/events"
        cursor = after_sequence_number
        data: list[str] = []
        try:
            async with self._http.stream(
                "GET",
                path,
                params={
                    "after_sequence_number": cursor,
                },
            ) as response:
                self._check(response)
                if not response.headers.get("content-type", "").startswith(
                    "text/event-stream"
                ):
                    raise HostClientError("Host did not return an event stream")
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif not line and data:
                        event = json.loads("\n".join(data))
                        data.clear()
                        if (
                            event.get("session_id") != session_id
                            or event.get("chat_id") != chat_id
                        ):
                            raise HostClientError(
                                "Host event scope does not match the requested turn"
                            )
                        sequence = event["sequence_number"]
                        if not isinstance(sequence, int) or sequence <= cursor:
                            raise HostClientError(
                                "Host event sequence is not increasing"
                            )
                        cursor = sequence
                        yield event
                if data:
                    raise HostClientError("Host event stream ended inside an event")
        except httpx.HTTPError:
            raise HostClientError("Host event connection failed or timed out") from None

    async def run(
        self,
        session_id: str,
        text: str,
        *,
        datasource_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Start one turn, yield wire events, require a successful terminal event.

        Disconnection or closing this generator requests cancellation of this
        client's turn only. POST admission is never retried: its response may
        have been lost after the server accepted the turn.
        """
        _id(session_id)
        if not text.strip():
            raise ValueError("prompt is required")
        body = await self._request(
            "POST",
            "console/chat",
            json={
                "session_id": session_id,
                "text": text,
                "datasource_id": datasource_id,
            },
        )
        chat_id = _id(body["chat"]["id"])
        self._active[chat_id] = session_id
        terminal = False
        try:
            async with aclosing(self.events(session_id, chat_id)) as events:
                async for event in events:
                    if event["object"] == "response" and event.get("status") in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        terminal = True
                        self._active.pop(chat_id, None)
                    yield event
                    if terminal:
                        if event["status"] != "completed":
                            raise HostClientError(f"Host turn {event['status']}")
                        return
            raise HostClientError("Host event stream ended before a terminal response")
        finally:
            if not terminal:
                await self._stop_owned(session_id, chat_id)

    async def _stop_owned(self, session_id: str, chat_id: str) -> None:
        cleanup = asyncio.create_task(self.stop(session_id, chat_id))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def close(self) -> None:
        try:
            for chat_id, session_id in list(self._active.items()):
                await self._stop_owned(session_id, chat_id)
        finally:
            await self._http.aclose()

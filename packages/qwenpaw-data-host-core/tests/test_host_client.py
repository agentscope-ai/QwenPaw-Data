"""Host client protocol, cancellation and credential boundaries."""

import asyncio
import json
from contextlib import aclosing

import httpx
import pytest

from qwenpaw_data.host.core.client import HostClient, HostClientError


def wire(events):
    return "".join(
        "data: "
        + json.dumps(
            {"session_id": "s1", "chat_id": "c1", "sequence_number": n, **event}
        )
        + "\n\n"
        for n, event in enumerate(events)
    )


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "truncated"])
async def test_run_requires_terminal_and_stops_interrupted_turn(status):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-test-token"
        if request.url.path == "/api/console/chat":
            return httpx.Response(200, json={"chat": {"id": "c1"}})
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=wire(
                    []
                    if status == "truncated"
                    else [{"object": "response", "status": status}]
                ),
            )
        assert request.url.path == "/api/console/chat/stop"
        assert dict(request.url.params) == {"session_id": "s1", "chat_id": "c1"}
        return httpx.Response(200, json={})

    async with HostClient(
        "http://host", token="private-test-token", transport=httpx.MockTransport(handle)
    ) as client:
        if status == "completed":
            assert len([event async for event in client.run("s1", "hello")]) == 1
        else:
            with pytest.raises(HostClientError):
                _ = [event async for event in client.run("s1", "hello")]
    assert sum(r.url.path.endswith("/stop") for r in calls) == (status == "truncated")


async def test_closing_stream_cancels_owned_turn_and_replay_does_not():
    stopped = []

    def handle(request):
        if request.url.path.endswith("/chat"):
            return httpx.Response(200, json={"chat": {"id": "c1"}})
        if request.url.path.endswith("/stop"):
            stopped.append(request)
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=wire([{"object": "response", "status": "in_progress"}]),
        )

    async with HostClient(
        "http://host", transport=httpx.MockTransport(handle)
    ) as client:
        async with aclosing(client.run("s1", "hi")) as events:
            await anext(events)
        assert len(stopped) == 1
        _ = [event async for event in client.events("s1", "c1")]
        assert len(stopped) == 1


async def test_cancellation_during_stream_read_requests_stop():
    entered = asyncio.Event()
    stopped = []

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

    def handle(request):
        if request.url.path.endswith("/chat"):
            return httpx.Response(200, json={"chat": {"id": "c1"}})
        if request.url.path.endswith("/stop"):
            stopped.append(request)
            return httpx.Response(200, json={})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Waiting()
        )

    async with HostClient(
        "http://host", transport=httpx.MockTransport(handle)
    ) as client:

        async def collect():
            return [event async for event in client.run("s1", "hi")]

        task = asyncio.create_task(collect())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(stopped) == 1


@pytest.mark.parametrize("status", [401, 403, 307])
async def test_auth_failures_and_redirects_are_not_followed_or_reflected(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://untrusted.invalid"},
            text="private-test-token",
        )

    async with HostClient(
        "http://host", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(HostClientError, match=f"HTTP {status}") as error:
            await client.create_session()
        assert "private-test-token" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@host",
        "file:///tmp/x",
        "https://host/api",
        "https://host?token=x",
    ],
)
def test_base_url_has_no_implicit_credentials_or_api_path(url):
    with pytest.raises(ValueError):
        HostClient(url)

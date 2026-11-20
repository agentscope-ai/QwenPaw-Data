"""Strict client-side dependency gates over the real Host wire contract."""

import asyncio
import json
from contextlib import aclosing

import httpx
import pytest

from qwenpaw_data.host.core.client import HostClient, HostClientError
from qwenpaw_data.host.core.domain.sop import SOP


def plan():
    # Deliberately reverse dependency order in the input file.
    return SOP.from_dict(
        {
            "name": "Analysis",
            "description": "Inspect then report",
            "expected_outcome": "Evidence",
            "nodes": [
                {
                    "node_id": key,
                    "name": key,
                    "description": key,
                    "expected_outcome": key,
                    "deps": deps,
                }
                for key, deps in [
                    ("report", ["left", "right"]),
                    ("right", ["inspect"]),
                    ("left", ["inspect"]),
                    ("inspect", []),
                ]
            ],
        }
    )


class WireHost:
    def __init__(self, status="completed"):
        self.status = status
        self.prompts = []
        self.stopped = []
        self.entered = asyncio.Event()

    def __call__(self, request):
        if request.url.path.endswith("/chat"):
            self.prompts.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"chat": {"id": f"c{len(self.prompts)}"}})
        if request.url.path.endswith("/stop"):
            self.stopped.append(request.url.params["chat_id"])
            return httpx.Response(200, json={})
        assert request.url.path.endswith("/events")
        chat_id = request.url.path.split("/")[-2]
        if self.status == "waiting":
            entered = self.entered

            class Waiting(httpx.AsyncByteStream):
                async def __aiter__(self):
                    entered.set()
                    await asyncio.Event().wait()
                    yield b""

            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Waiting()
            )
        events = [
            {
                "object": "message",
                "id": f"m{chat_id}",
                "role": "assistant",
                "type": "message",
                "status": "completed",
                "content": [{"type": "text", "text": f"evidence-{chat_id}"}],
            }
        ]
        if self.status != "truncated":
            events.append({"object": "response", "status": self.status})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(
                "data: "
                + json.dumps(
                    {
                        "session_id": "s1",
                        "chat_id": chat_id,
                        "sequence_number": index,
                        **event,
                    }
                )
                + "\n\n"
                for index, event in enumerate(events)
            ),
        )


async def test_diamond_waits_for_both_dependencies_and_passes_outputs():
    host = WireHost()
    async with HostClient("http://host", transport=httpx.MockTransport(host)) as client:
        events = [event async for event in client.execute("s1", plan().to_yaml())]
    payloads = [json.loads(prompt.split("\n", 1)[1]) for prompt in host.prompts]
    ids = [payload["node"]["node_id"] for payload in payloads]
    assert ids[0] == "inspect" and ids[-1] == "report"
    assert set(ids[1:3]) == {"left", "right"}
    assert payloads[0]["dependency_outputs"] == {}
    assert payloads[1]["dependency_outputs"] == {"inspect": "evidence-c1"}
    assert payloads[-1]["dependency_outputs"] == {
        ids[1]: "evidence-c2",
        ids[2]: "evidence-c3",
    }
    assert [event["sop_node_id"] for event in events[::2]] == ids
    assert not host.stopped


@pytest.mark.parametrize("status", ["failed", "cancelled", "truncated"])
async def test_failure_never_admits_successors(status):
    host = WireHost(status)
    async with HostClient("http://host", transport=httpx.MockTransport(host)) as client:
        with pytest.raises(HostClientError):
            _ = [event async for event in client.execute("s1", plan())]
    assert len(host.prompts) == 1
    assert host.stopped == (["c1"] if status == "truncated" else [])


async def test_partial_close_stops_active_node_and_never_admits_successors():
    host = WireHost()
    async with HostClient("http://host", transport=httpx.MockTransport(host)) as client:
        async with aclosing(client.execute("s1", plan())) as events:
            await anext(events)
    assert len(host.prompts) == 1
    assert host.stopped == ["c1"]


async def test_cancellation_during_read_stops_active_node():
    host = WireHost("waiting")
    async with HostClient("http://host", transport=httpx.MockTransport(host)) as client:

        async def collect():
            return [event async for event in client.execute("s1", plan())]

        task = asyncio.create_task(collect())
        await host.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(host.prompts) == 1 and host.stopped == ["c1"]


@pytest.mark.parametrize("corruption", ["empty", "cycle", "unknown", "duplicate"])
async def test_mutated_plan_revalidated_before_any_network_request(corruption):
    sop = plan()
    if corruption == "empty":
        sop.nodes.clear()
    elif corruption == "cycle":
        sop.nodes[-1].deps = ["report"]
    elif corruption == "unknown":
        sop.nodes[-1].deps = ["absent"]
    else:
        sop.nodes[-1].node_id = "report"
    host = WireHost()
    async with HostClient("http://host", transport=httpx.MockTransport(host)) as client:
        with pytest.raises(ValueError):
            _ = [event async for event in client.execute("s1", sop)]
    assert host.prompts == []

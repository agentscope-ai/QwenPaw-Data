"""Terminal adapter for the current Host service, separate from local embedding."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from contextlib import aclosing

from qwenpaw_data.host.core.client import HostClient, HostClientError


def read_terminal_input() -> str:
    """Read on the CLI main thread without stranding an executor on Ctrl+C.

    The Host keeps running remotely while the terminal waits for its user.
    asyncio.Runner's SIGINT handler only cancels tasks, which cannot interrupt
    a blocking input call; use the normal terminal handler for this read only.
    Restore Runner's handler before async cleanup requests cancellation.
    """
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        return input()
    finally:
        signal.signal(signal.SIGINT, previous)


def add_host_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host-url",
        default=os.getenv("QWENPAW_DATA_HOST_URL"),
        help="Host origin (or QWENPAW_DATA_HOST_URL); uses server configuration",
    )
    parser.add_argument(
        "--session-id", help="Continue an existing authorized Host session"
    )


def create_client(args: argparse.Namespace) -> HostClient:
    if not args.host_url:
        raise ValueError(
            "--host-url or QWENPAW_DATA_HOST_URL is required; local embedding is not yet restored"
        )
    return HostClient(args.host_url, token=os.getenv("QWENPAW_DATA_HOST_TOKEN"))


async def print_turn(
    client: HostClient,
    session_id: str,
    text: str,
    *,
    stream: bool,
    datasource_id: str | None = None,
) -> None:
    text_messages: set[str] = set()
    streamed: dict[str, list[str]] = {}
    confirmed: set[str] = set()
    async with aclosing(
        client.run(session_id, text, datasource_id=datasource_id)
    ) as events:
        async for event in events:
            if event["object"] == "message":
                if event.get("role") == "assistant" and event["type"] == "message":
                    text_messages.add(event["id"])
                    if event["status"] == "completed":
                        full_text = "".join(
                            block.get("text", "")
                            for block in event["content"]
                            if block["type"] == "text"
                        )
                        prefix = "".join(streamed.pop(event["id"], []))
                        # Host compacts deltas at completion. A slow subscriber
                        # may have seen only a prefix before the final snapshot.
                        if stream and full_text.startswith(prefix):
                            print(full_text[len(prefix) :], end="", flush=True)
                        else:
                            if prefix:
                                print(flush=True)
                            print(full_text, end="", flush=True)
                        print(flush=True)
                if (
                    event["type"] == "plugin_call_output"
                    and event["status"] == "completed"
                ):
                    for block in event["content"]:
                        result = block.get("data") if block["type"] == "data" else None
                        if result and result.get("state") in {
                            "error",
                            "denied",
                            "interrupted",
                        }:
                            print(
                                f"[tool {result['state']}] {result.get('name', '?')}: "
                                f"{result.get('output', '')}",
                                file=sys.stderr,
                            )
                if (
                    event["type"] == "plugin_call"
                    and event["status"] == "completed"
                    and any(
                        block.get("type") == "data"
                        and (block.get("data") or {}).get("name") == "ask_user_question"
                        for block in event["content"]
                    )
                ):
                    raise HostClientError(
                        "This session requires clarification; continue in Console"
                    )
                if (
                    event["type"] == "plugin_call"
                    and event["status"] == "completed"
                    and (event.get("metadata") or {}).get("awaiting_confirmation")
                ):
                    call_id = event["source_id"]
                    if call_id not in confirmed:
                        confirmed.add(call_id)
                        approved = False
                        if sys.stdin.isatty():
                            print(
                                f"Tool approval requested: {event['content']}",
                                file=sys.stderr,
                            )
                            try:
                                print(
                                    "Allow this call? [y/N] ",
                                    end="",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                answer = read_terminal_input()
                                approved = answer.strip().lower() in {"y", "yes"}
                            except EOFError:
                                pass
                        else:
                            print(
                                "Tool approval denied: non-interactive input.",
                                file=sys.stderr,
                            )
                        await client.answer_tool_confirmation(
                            session_id, event["chat_id"], call_id, approved=approved
                        )
            elif (
                stream
                and event["object"] == "content"
                and event.get("type") == "text"
                and event.get("delta")
                and event["msg_id"] in text_messages
            ):
                streamed.setdefault(event["msg_id"], []).append(event.get("text") or "")
                print(event.get("text") or "", end="", flush=True)

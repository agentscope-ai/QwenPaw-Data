"""Interactive chat through the current Host service."""

from __future__ import annotations

import argparse
import asyncio
import sys

from qwenpaw_data.cli.host import add_host_args, create_client, print_turn
from qwenpaw_data.cli.util import add_datasource_id_arg


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "chat", help="Chat with an explicitly selected Host service"
    )
    add_datasource_id_arg(parser)
    add_host_args(parser)
    parser.set_defaults(handler=handle)


async def handle(args: argparse.Namespace) -> int:
    async with create_client(args) as client:
        session_id = args.session_id
        print("QwenPaw Data chat. Type 'exit' or 'quit' to quit.", file=sys.stderr)
        while True:
            try:
                print("> ", end="", file=sys.stderr, flush=True)
                text = (await asyncio.to_thread(input)).strip()
            except EOFError:
                return 0
            if text.lower() in {"exit", "quit"}:
                return 0
            if not text:
                continue
            if session_id is None:
                session_id = await client.create_session(
                    datasource_id=args.datasource_id
                )
                print(f"Session: {session_id}", file=sys.stderr)
            await print_turn(
                client, session_id, text, stream=True, datasource_id=args.datasource_id
            )

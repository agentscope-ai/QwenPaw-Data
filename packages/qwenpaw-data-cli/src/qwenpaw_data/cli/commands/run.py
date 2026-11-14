"""Run one turn through the current Host service."""

from __future__ import annotations

import argparse
import sys

from qwenpaw_data.cli.host import add_host_args, create_client, print_turn
from qwenpaw_data.cli.util import (
    add_datasource_id_arg,
    add_prompt_args,
    add_stream_arg,
    read_prompt,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "run", help="Run a prompt on an explicitly selected Host service"
    )
    add_prompt_args(parser)
    add_stream_arg(parser)
    add_datasource_id_arg(parser)
    add_host_args(parser)
    parser.set_defaults(handler=handle)


async def handle(args: argparse.Namespace) -> int:
    prompt = read_prompt(args)
    async with create_client(args) as client:
        session_id = args.session_id or await client.create_session(
            datasource_id=args.datasource_id
        )
        print(f"Session: {session_id}", file=sys.stderr)
        await print_turn(
            client,
            session_id,
            prompt,
            stream=args.stream,
            datasource_id=args.datasource_id,
        )
    return 0

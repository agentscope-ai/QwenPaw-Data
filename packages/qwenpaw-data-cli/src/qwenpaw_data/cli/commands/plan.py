"""Generate a portable SOP on the current Host without executing it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qwenpaw_data.cli.host import add_host_args, create_client
from qwenpaw_data.cli.util import add_datasource_id_arg, add_prompt_args, read_prompt


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "plan", help="Generate SOP YAML without executing tools"
    )
    add_prompt_args(parser)
    add_datasource_id_arg(parser)
    add_host_args(parser)
    parser.add_argument("--output", "-o", type=Path, help="Create a new SOP YAML file")
    parser.set_defaults(handler=handle)


async def handle(args: argparse.Namespace) -> int:
    prompt = read_prompt(args)
    if args.output and args.output.exists():
        raise ValueError("refusing to overwrite an existing SOP file")
    if args.session_id and args.datasource_id:
        raise ValueError(
            "plan --session-id uses the session datasource; omit --datasource-id"
        )
    async with create_client(args) as client:
        session_id = args.session_id or await client.create_session(
            datasource_id=args.datasource_id
        )
        print(f"Session: {session_id}", file=sys.stderr)
        plan = await client.plan(session_id, prompt)
    text = plan.to_yaml()
    if args.output:
        # Exclusive creation also rejects a dangling symlink or a concurrent writer.
        with args.output.open("x", encoding="utf-8") as output:
            output.write(text)
        print(f"Wrote SOP to {args.output}", file=sys.stderr)
    else:
        print(text, end="")
    return 0

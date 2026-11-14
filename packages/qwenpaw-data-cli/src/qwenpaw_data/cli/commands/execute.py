"""Execute a portable SOP with strict dependency ordering through Host."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qwenpaw_data.cli.host import add_host_args, create_client, print_events
from qwenpaw_data.cli.util import add_datasource_id_arg, add_stream_arg
from qwenpaw_data.host.core.domain.sop import SOP


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "execute", help="Execute a SOP YAML DAG through Host"
    )
    parser.add_argument("sop", type=Path, help="SOP YAML file to execute")
    add_stream_arg(parser)
    add_datasource_id_arg(parser)
    add_host_args(parser)
    parser.set_defaults(handler=handle)


async def handle(args: argparse.Namespace) -> int:
    sop = SOP.from_yaml(args.sop.expanduser().read_text(encoding="utf-8"))
    if not sop.nodes:
        raise ValueError("SOP execution requires at least one node")
    if args.session_id and args.datasource_id:
        raise ValueError(
            "execute --session-id uses the session datasource; omit --datasource-id"
        )
    async with create_client(args) as client:
        session_id = args.session_id or await client.create_session(
            datasource_id=args.datasource_id
        )
        print(f"Session: {session_id}", file=sys.stderr)
        await print_events(
            client, session_id, client.execute(session_id, sop), stream=args.stream
        )
    return 0

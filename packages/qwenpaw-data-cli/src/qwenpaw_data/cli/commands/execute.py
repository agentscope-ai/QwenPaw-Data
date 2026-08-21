"""Execute command."""

from __future__ import annotations

import argparse
from pathlib import Path

from qwenpaw_data.cli.util import (
    add_datasource_id_arg,
    add_permission_mode_arg,
    add_stream_arg,
    add_workspace_arg,
    build_cli_confirmation_handler,
    create_qwenpaw_data,
    print_execution_summary,
    print_event_stream,
    print_msg,
    request_context_from_args,
    resolve_permission_mode,
    resolve_workspace_type,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("execute", help="Execute a SOP YAML file")
    parser.add_argument("sop", type=Path, help="SOP YAML file to execute")
    add_stream_arg(parser)
    add_datasource_id_arg(parser)
    add_workspace_arg(parser)
    add_permission_mode_arg(parser)
    parser.set_defaults(handler=handle)


async def handle(args: argparse.Namespace) -> int:
    workspace_type = resolve_workspace_type(args)
    dp = create_qwenpaw_data(
        request_context=request_context_from_args(args),
        workspace_type=workspace_type,
        permission_mode=resolve_permission_mode(args, workspace_type),
        confirmation_handler=build_cli_confirmation_handler(),
    )
    try:
        sop = args.sop.expanduser().read_text(encoding="utf-8")
        if args.stream:
            events = await dp.execute(sop, stream=True)
            await print_event_stream(events)
            return 0

        msg = await dp.execute(sop)
        print_msg(msg)
        print_execution_summary(msg)
        return 0
    finally:
        await dp.close()

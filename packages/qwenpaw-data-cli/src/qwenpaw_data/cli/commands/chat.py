"""Chat command."""

from __future__ import annotations

import argparse
import sys

from qwenpaw_data.cli.util import (
    add_datasource_id_arg,
    add_permission_mode_arg,
    add_workspace_arg,
    build_cli_confirmation_handler,
    create_qwenpaw_data,
    print_execution_summary,
    print_msg,
    request_context_from_args,
    resolve_permission_mode,
    resolve_workspace_type,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("chat", help="Start an interactive chat")
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
    print("QwenPaw Data chat. Type 'exit' or 'quit' to quit.", file=sys.stderr)
    try:
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                print()
                return 0

            if not text:
                continue
            if text.lower() in {"exit", "quit"}:
                return 0

            msg = await dp.run(text)
            print_msg(msg)
            print_execution_summary(msg)
    finally:
        await dp.close()

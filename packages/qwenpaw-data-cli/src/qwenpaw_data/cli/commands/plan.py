"""Plan command."""

from __future__ import annotations

import argparse
from pathlib import Path

from qwenpaw_data.cli.util import (
    add_datasource_id_arg,
    add_permission_mode_arg,
    add_prompt_args,
    add_workspace_arg,
    build_cli_confirmation_handler,
    create_qwenpaw_data,
    read_prompt,
    request_context_from_args,
    resolve_permission_mode,
    resolve_workspace_type,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("plan", help="Create a SOP from a prompt")
    add_prompt_args(parser)
    add_datasource_id_arg(parser)
    add_workspace_arg(parser)
    add_permission_mode_arg(parser)
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write SOP YAML to this file instead of stdout",
    )
    parser.set_defaults(handler=handle)


async def handle(args: argparse.Namespace) -> int:
    from qwenpaw_data.host.core.orchestration.task_graph import SOP

    workspace_type = resolve_workspace_type(args)
    dp = create_qwenpaw_data(
        request_context=request_context_from_args(args),
        workspace_type=workspace_type,
        permission_mode=resolve_permission_mode(args, workspace_type),
        confirmation_handler=build_cli_confirmation_handler(),
    )
    try:
        msg = await dp.plan(read_prompt(args))
        plan = msg.metadata.get("plan")
        if not isinstance(plan, dict):
            raise RuntimeError("planning did not return SOP metadata")

        yaml_text = SOP.from_dict(plan).to_yaml()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(yaml_text, encoding="utf-8")
            print(f"Wrote SOP to {args.output}")
        else:
            print(yaml_text, end="" if yaml_text.endswith("\n") else "\n")
        return 0
    finally:
        await dp.close()

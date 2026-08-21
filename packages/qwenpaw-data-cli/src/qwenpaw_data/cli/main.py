from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import sys

from qwenpaw_data.cli.env import load_qwenpaw_data_env
from qwenpaw_data.cli.logging_config import configure_cli_logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    load_qwenpaw_data_env(override=False)
    try:
        configure_cli_logging()
    except OSError as exc:
        print(f"qwenpaw-data: error: unable to initialize log file: {exc}", file=sys.stderr)
        return 1

    arguments = sys.argv[1:] if argv is None else argv
    parser = build_parser(include_internal=bool(arguments and arguments[0] == "mcp"))
    args = parser.parse_args(arguments)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        result = handler(args)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        return int(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception(
            "QwenPaw Data CLI command failed: command=%s",
            getattr(args, "command", None) or "-",
        )
        for line in _flatten_exception(exc):
            print(f"qwenpaw-data: error: {line}", file=sys.stderr)
        return 1


def _flatten_exception(exc: BaseException) -> list[str]:
    """Flatten (nested) ExceptionGroups into human-readable leaf errors.

    anyio TaskGroups (used by MCP clients) wrap the real failure in
    ``ExceptionGroup``s whose ``str()`` hides the actual cause, e.g.
    "unhandled errors in a TaskGroup (1 sub-exception)".
    """
    if isinstance(exc, BaseExceptionGroup):
        lines: list[str] = []
        for sub in exc.exceptions:
            lines.extend(_flatten_exception(sub))
        return lines or [f"{type(exc).__name__}: {exc}"]

    lines = [f"{type(exc).__name__}: {exc}"]
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        lines.extend(f"caused by: {line}" for line in _flatten_exception(cause))
    return lines


def build_parser(*, include_internal: bool = False) -> argparse.ArgumentParser:
    from qwenpaw_data.cli.commands import COMMANDS

    parser = argparse.ArgumentParser(prog="qwenpaw-data")
    subparsers = parser.add_subparsers(dest="command")
    for command in COMMANDS:
        command.register(subparsers)
    if include_internal:
        from qwenpaw_data.cli.commands import mcp

        mcp.register(subparsers)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI-owned logging configuration."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from datapaw.host.core.paths import host_root

_LOG_FILENAME = "datapaw.log"
_LOG_LEVEL = logging.INFO
_MAX_BYTES = 50 * 1024 * 1024
_BACKUP_COUNT = 2
_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-7s | "
    "%(name)s:%(funcName)s:%(lineno)d - %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _DataPawCLIFileHandler(RotatingFileHandler):
    """Marker type for the file handler owned by the DataPaw CLI."""


def _is_terminal_stream(stream: TextIO | None) -> bool:
    if stream is None:
        return False
    if stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        return True
    try:
        return stream.fileno() in {1, 2}
    except (AttributeError, OSError, ValueError):
        return False


def _is_terminal_handler(handler: logging.Handler) -> bool:
    return (
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and _is_terminal_stream(getattr(handler, "stream", None))
    )


def _is_agentscope_console_handler(handler: logging.Handler) -> bool:
    # AgentScope owns logger "as" and installs a plain StreamHandler for its
    # console output. Treat that handler as terminal even when a test runner
    # has replaced stderr with a captured stream whose file descriptor is not 2.
    return isinstance(handler, logging.StreamHandler) and not isinstance(
        handler,
        logging.FileHandler,
    )


def _remove_handler(logger: logging.Logger, handler: logging.Handler) -> None:
    logger.removeHandler(handler)
    handler.close()


def _configure_root_logger(log_path: Path) -> _DataPawCLIFileHandler:
    root = logging.getLogger()
    owned_handler: _DataPawCLIFileHandler | None = None

    for handler in list(root.handlers):
        if isinstance(handler, _DataPawCLIFileHandler):
            if Path(handler.baseFilename) == log_path and owned_handler is None:
                owned_handler = handler
            else:
                _remove_handler(root, handler)
        elif _is_terminal_handler(handler):
            _remove_handler(root, handler)

    if owned_handler is None:
        owned_handler = _DataPawCLIFileHandler(
            log_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        root.addHandler(owned_handler)

    owned_handler.setLevel(_LOG_LEVEL)
    owned_handler.maxBytes = _MAX_BYTES
    owned_handler.backupCount = _BACKUP_COUNT
    owned_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.setLevel(_LOG_LEVEL)
    return owned_handler


def _route_agentscope_logger_to_root() -> None:
    # Importing AgentScope installs its own stderr handler on logger "as".
    # Do this only after the root file handler exists, then route that logger
    # through the root so workspace initialization logs use datapaw.log too.
    agentscope_logger = logging.getLogger("as")
    existing_handlers = list(agentscope_logger.handlers)
    preserved_handlers = [
        handler
        for handler in existing_handlers
        if not _is_agentscope_console_handler(handler)
    ]

    import agentscope  # noqa: F401

    for handler in existing_handlers:
        if (
            handler not in agentscope_logger.handlers
            and _is_agentscope_console_handler(handler)
        ):
            handler.close()
    for handler in list(agentscope_logger.handlers):
        if _is_agentscope_console_handler(handler):
            _remove_handler(agentscope_logger, handler)
    for handler in preserved_handlers:
        if handler not in agentscope_logger.handlers:
            agentscope_logger.addHandler(handler)
    agentscope_logger.setLevel(_LOG_LEVEL)
    agentscope_logger.propagate = True


def configure_cli_logging() -> Path:
    """Route CLI process logs to ``DATAPAW_HOME/host/datapaw.log``."""

    log_path = host_root() / _LOG_FILENAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _configure_root_logger(log_path)
    _route_agentscope_logger_to_root()
    return log_path


__all__ = ["configure_cli_logging"]

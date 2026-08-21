# -*- coding: utf-8 -*-
"""Client-side MCP diagnostics log.

Mirrors DataBridge's ``mcp_access.log`` on the CLI/host side so that a hung
call can be attributed by comparing both files:

- CLI has ``MCP_DISCOVERY_START``/``MCP_TOOL_CALL_START`` but CM has no
  ``MCP_START``: client/transport send side.
- CM has ``MCP_START`` but no ``MCP_END``: CM internal.
- CM ``MCP_END`` present but CLI has no matching ``*_END``: response
  transport / client-side parsing.

Every line carries ``run=<id>``, a per-process id also injected as the
``x-qwenpaw-data-run`` HTTP header on CM MCP clients, so both logs can be joined.
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

MCP_CLIENT_RUN_ID = uuid4().hex[:8]
MCP_RUN_HEADER = "x-qwenpaw-data-run"

_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_logger = logging.getLogger("qwenpaw_data.mcp_client")


def ensure_mcp_client_log() -> logging.Logger:
    """Lazy-init RotatingFileHandler at ``<home>/host/logs/mcp_client.log``."""
    if _logger.handlers:
        return _logger

    from logging.handlers import RotatingFileHandler

    from ..paths import host_root

    log_path = host_root() / "logs" / "mcp_client.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=_LOG_MAX_BYTES,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    return _logger


def log_mcp_client_event(fmt: str, *args: object) -> None:
    """Append one timestamped, run-tagged line to the client MCP log."""
    try:
        log = ensure_mcp_client_log()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info("[%s] run=%s " + fmt, ts, MCP_CLIENT_RUN_ID, *args)
    except Exception:  # diagnostics must never break the agent
        logging.getLogger(__name__).debug(
            "failed to write MCP client log", exc_info=True,
        )

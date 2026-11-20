# -*- coding: utf-8 -*-
from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import queue
from enum import Enum
from typing import Any

from qwenpaw_data.host.core.settings import get_settings


class Step(str, Enum):
    CHAT_START = "chat_start"
    CHAT_STOP = "chat_stop"
    CHAT_STEER = "chat_steer"
    CHAT_PLAN = "chat_plan"
    CHAT_CLARIFICATION = "chat_clarification"
    CHAT_END = "chat_end"
    CHAT_FAILED = "chat_failed"
    USER_INPUT = "user_input"
    ASSISTANT_REPLY = "assistant_reply"
    STEER = "steer"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    MODEL_CALL_START = "model_call_start"
    MODEL_CALL_END = "model_call_end"
    MODEL_CALL_CANCEL = "model_call_cancel"
    MODEL_CALL_FAILED = "model_call_failed"
    CONTEXT_COMPRESS = "context_compress"
    EXCEED_MAX_ITERS = "exceed_max_iters"
    MCP_CONNECT = "mcp_connect"
    MCP_CONNECT_FAILED = "mcp_connect_failed"
    ARTIFACT_PREVIEW = "artifact_preview"
    ARTIFACT_DOWNLOAD = "artifact_download"


class _QueueHandler(logging.handlers.QueueHandler):
    def emit(self, record: logging.LogRecord) -> None:
        self.queue.put_nowait(record)


def log_info(
    *,
    request_id: str,
    step: Step,
    message: str,
    context: dict[str, Any] | None = None,
) -> None:
    _write("info", request_id, step, message, context)


def log_error(
    *,
    request_id: str,
    step: Step,
    message: str,
    context: dict[str, Any] | None = None,
) -> None:
    _write("error", request_id, step, message, context)


_logger: logging.Logger | None = None
_listener: logging.handlers.QueueListener | None = None
_atexit_registered = False


def _write(
    level: str,
    request_id: str,
    step: Step,
    message: str,
    context: dict[str, Any] | None,
) -> None:
    request_id = request_id.strip()
    if not request_id:
        raise ValueError("request_id is required")
    if not isinstance(step, Step):
        raise TypeError("step must be Step")
    message = message.strip()
    if not message:
        raise ValueError("message is required")
    line = json.dumps(
        {
            "level": level,
            "request_id": request_id,
            "step": step.value,
            "message": message,
            "context": _context(context),
        },
        ensure_ascii=False,
        default=str,
    )
    try:
        _get_logger().info(line)
    except queue.Full as exc:
        raise RuntimeError("query log queue is full") from exc


def reset_logger() -> None:
    global _logger
    _stop_listener()
    if _logger is None:
        return
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        handler.close()
    _logger = None


def _stop_listener() -> None:
    global _listener
    if _listener is None:
        return
    handlers = tuple(_listener.handlers)
    _listener.stop()
    for handler in handlers:
        handler.close()
    _listener = None


def _get_logger() -> logging.Logger:
    global _logger, _listener, _atexit_registered
    if _logger is not None:
        return _logger
    settings = get_settings().log
    settings.path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        settings.path,
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(
        maxsize=settings.queue_maxsize
    )
    _listener = logging.handlers.QueueListener(log_queue, handler)
    _listener.start()
    if not _atexit_registered:
        atexit.register(_stop_listener)
        _atexit_registered = True
    _logger = logging.getLogger(settings.name)
    _logger.setLevel(settings.level)
    _logger.propagate = False
    _logger.addHandler(_QueueHandler(log_queue))
    return _logger


def _context(context: dict[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {}
    if not isinstance(context, dict):
        raise TypeError("context must be a dict")
    limit = get_settings().log.context_max_chars
    bulky = {"text", "input", "output"}
    out: dict[str, Any] = {}
    for key, value in context.items():
        if key in bulky and isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit]
            out[f"{key}_truncated"] = True
            out[f"{key}_chars"] = len(value)
        else:
            out[key] = value
    return out

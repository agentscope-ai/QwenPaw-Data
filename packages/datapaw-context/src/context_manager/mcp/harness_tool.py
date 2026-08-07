"""Register CM MCP tools with harness-only args hidden from tools/list.

``metadata`` and ``session_ref`` stay on the Python handler signature for
harness injection at tools/call time, but are excluded from the JSON Schema
the model sees. Tool descriptions use only the docstring summary (text before
``Args:``), so harness参数说明不会进入模型上下文。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp.server import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.utilities.context_injection import find_context_parameter
from mcp.server.fastmcp.utilities.func_metadata import func_metadata
from mcp.types import Icon, ToolAnnotations

from datapaw.context.paths import mcp_access_log_path as _mcp_access_log_path

# ---- MCP tool call access log ----
_mcp_access_log = logging.getLogger("mcp.tool_access")
_MCP_LOG_PATH_STR = str(_mcp_access_log_path())
_MCP_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_MCP_RESULT_TRUNCATE = 0  # 0 = 不截断，完整记录


def ensure_mcp_access_log_handler() -> logging.Logger:
    """Lazy-init RotatingFileHandler for the shared MCP access log.

    Shared by tool-level logging (:class:`HarnessTool`) and protocol-level
    request logging (``http_common._McpMount``).
    """
    if _mcp_access_log.handlers:
        return _mcp_access_log
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_path = Path(_MCP_LOG_PATH_STR)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(
        str(log_path),
        maxBytes=_MCP_LOG_MAX_BYTES,
        backupCount=2,
        encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    _mcp_access_log.addHandler(h)
    _mcp_access_log.setLevel(logging.INFO)
    _mcp_access_log.propagate = False
    return _mcp_access_log


HARNESS_ARG_NAMES: frozenset[str] = frozenset({"metadata", "session_ref"})


def model_facing_description(fn: Callable[..., Any], explicit: str | None = None) -> str:
    """Docstring summary only — drop Args and anything harness-related."""
    if explicit:
        return explicit.strip()
    doc = (fn.__doc__ or "").strip()
    if not doc:
        return ""
    if "\n    Args:" in doc:
        doc = doc.split("\n    Args:")[0].strip()
    elif "\nArgs:" in doc:
        doc = doc.split("\nArgs:")[0].strip()
    return doc


class HarnessTool(Tool):
    """FastMCP Tool that hides harness kwargs from the published input schema."""

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        context_kwarg: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> HarnessTool:
        func_name = name or fn.__name__
        func_doc = model_facing_description(fn, description)
        from mcp.server.fastmcp.tools.base import _is_async_callable

        is_async = _is_async_callable(fn)
        if context_kwarg is None:
            context_kwarg = find_context_parameter(fn)

        skip = list(HARNESS_ARG_NAMES)
        if context_kwarg is not None:
            skip.append(context_kwarg)

        func_arg_metadata = func_metadata(
            fn,
            skip_names=skip,
            structured_output=structured_output,
        )
        parameters = func_arg_metadata.arg_model.model_json_schema(by_alias=True)

        return cls(
            fn=fn,
            name=func_name,
            title=title,
            description=func_doc,
            parameters=parameters,
            fn_metadata=func_arg_metadata,
            is_async=is_async,
            context_kwarg=context_kwarg,
            annotations=annotations,
            icons=icons,
            meta=meta,
        )

    async def run(
        self,
        arguments: dict[str, Any],
        context: Any | None = None,
        convert_result: bool = False,
    ) -> Any:
        visible = dict(arguments)
        harness: dict[str, Any] = {}
        for key in HARNESS_ARG_NAMES:
            if key in visible:
                harness[key] = visible.pop(key)

        # Ensure handler defaults when harness omits optional keys.
        import inspect

        sig = inspect.signature(self.fn)
        if "metadata" in sig.parameters and "metadata" not in harness:
            harness["metadata"] = "{}"
        if "session_ref" in sig.parameters and "session_ref" not in harness:
            harness["session_ref"] = ""

        return await self._run_with_harness(visible, harness, context, convert_result)

    # ---- MCP tool call access logging ----

    @staticmethod
    def _setup_mcp_log_handler() -> None:
        """Lazy-init RotatingFileHandler for MCP tool call log."""
        ensure_mcp_access_log_handler()

    @staticmethod
    def _format_args_for_log(args: dict[str, Any]) -> str:
        """Serialize tool args, redact credentials, and truncate."""
        import json

        try:
            s = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            s = str(args)
        from context_manager.secrets.redact import _redact_str

        s = _redact_str(s)
        if _MCP_RESULT_TRUNCATE > 0 and len(s) > _MCP_RESULT_TRUNCATE:
            s = s[:_MCP_RESULT_TRUNCATE] + "…[truncated]"
        return s

    async def _run_with_harness(
        self,
        visible: dict[str, Any],
        harness: dict[str, Any],
        context: Any | None,
        convert_result: bool,
    ) -> Any:
        from mcp.server.fastmcp.exceptions import ToolError
        from mcp.shared.exceptions import UrlElicitationRequiredError

        self._setup_mcp_log_handler()
        from datetime import datetime
        from uuid import uuid4

        call_id = uuid4().hex[:8]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_args = {**visible, **{k: v for k, v in harness.items() if k != "metadata" or v != "{}"}}
        args_str = self._format_args_for_log(log_args)

        # Log the incoming request immediately — before execution — so that a
        # crashed/hung handler still leaves a trace of what was requested.
        _mcp_access_log.info(
            "[%s] tool=%s call=%s RECEIVED\n  args: %s",
            ts, self.name, call_id, args_str,
        )

        t0 = time.monotonic()
        try:
            direct = dict(harness)
            if self.context_kwarg is not None and context is not None:
                direct[self.context_kwarg] = context
            result = await self.fn_metadata.call_fn_with_arg_validation(
                self.fn,
                self.is_async,
                visible,
                direct,
            )
            if convert_result:
                result = self.fn_metadata.convert_result(result)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            result_str = result if isinstance(result, str) else str(result)
            if _MCP_RESULT_TRUNCATE > 0 and len(result_str) > _MCP_RESULT_TRUNCATE:
                result_str = result_str[:_MCP_RESULT_TRUNCATE] + "…[truncated]"
            done_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _mcp_access_log.info(
                "[%s] tool=%s call=%s %dms OK\n  args: %s\n  result: %s",
                done_ts, self.name, call_id, elapsed_ms, args_str, result_str,
            )
            return result
        except UrlElicitationRequiredError:
            raise
        except BaseException as e:
            # BaseException: also record cancellations / stream teardown, which
            # otherwise leave no completion line after RECEIVED.
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            done_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _mcp_access_log.info(
                "[%s] tool=%s call=%s %dms ERROR %s: %s\n  args: %s",
                done_ts, self.name, call_id, elapsed_ms, type(e).__name__, e, args_str,
            )
            if not isinstance(e, Exception):
                raise
            raise ToolError(f"Error executing tool {self.name}: {e}") from e


def patch_mcp_tool_manager(mcp: FastMCP) -> None:
    """Route all ``mcp.tool()`` registrations through :class:`HarnessTool`."""

    manager = mcp._tool_manager

    def add_tool(
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> HarnessTool:
        tool = HarnessTool.from_function(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )
        existing = manager._tools.get(tool.name)
        if existing:
            if manager.warn_on_duplicate_tools:
                from mcp.server.fastmcp.utilities.logging import get_logger

                get_logger(__name__).warning("Tool already exists: %s", tool.name)
            return existing
        manager._tools[tool.name] = tool
        return tool

    manager.add_tool = add_tool  # type: ignore[method-assign]

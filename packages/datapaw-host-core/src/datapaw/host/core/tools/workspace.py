# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

from agentscope.message import TextBlock
from agentscope.tool import Bash, Edit, Glob, Grep, Read, ToolChunk, Write


class _WorkspaceToolMixin:
    def __init__(self, workdir: str | Path) -> None:
        super().__init__()
        self.workdir = str(Path(workdir).resolve())

    def _resolve_contained_path(
        self,
        raw_path: Any,
    ) -> tuple[str | None, str | None]:
        if not raw_path or not isinstance(raw_path, str):
            return None, "path is required"
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = Path(self.workdir) / candidate
        resolved = candidate.resolve()
        root = Path(self.workdir)
        if resolved == root or resolved.is_relative_to(root):
            return str(resolved), None
        return None, (
            f"Access denied: {raw_path} resolves outside the workspace "
            f"({self.workdir}). Use paths inside the workspace."
        )

    def _with_default_path(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_input.get("path"):
            return tool_input
        return {**tool_input, "path": self.workdir}


class _ContainedFileToolMixin:
    """Reject file paths that resolve outside the allowed roots.

    Paths are resolved (symlinks and ``..`` collapsed) before comparison so
    traversal tricks such as ``workspace/../../etc/passwd`` are caught.
    """

    def __init__(
        self,
        workdir: str | Path,
        *,
        extra_roots: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.workdir = str(Path(workdir).resolve())
        self._allowed_roots = [Path(self.workdir)] + [
            Path(p).resolve() for p in (extra_roots or [])
        ]

    def _resolve_contained(self, file_path: Any) -> tuple[str | None, str | None]:
        """返回 (解析后绝对路径, 错误信息)；二者有且仅有一个非 None。"""
        if not file_path or not isinstance(file_path, str):
            return None, "file_path is required"
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = Path(self.workdir) / candidate
        resolved = candidate.resolve()
        for root in self._allowed_roots:
            if resolved == root or resolved.is_relative_to(root):
                return str(resolved), None
        return None, (
            f"Access denied: {file_path} resolves outside the workspace "
            f"({self.workdir}). Use paths inside the workspace."
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        resolved, error = self._resolve_contained(kwargs.get("file_path"))
        if error is not None:
            return ToolChunk(
                content=[TextBlock(text=error)],
                state="error",
                is_last=True,
            )
        kwargs = {**kwargs, "file_path": resolved}
        return await super().__call__(*args, **kwargs)  # type: ignore[misc]


class WorkspaceRead(_ContainedFileToolMixin, Read):
    """Read constrained to the workspace (plus read-only skill roots)."""


class WorkspaceWrite(_ContainedFileToolMixin, Write):
    """Write constrained to the workspace."""


class WorkspaceEdit(_ContainedFileToolMixin, Edit):
    """Edit constrained to the workspace."""


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_sec: float = 3.0,
) -> None:
    """Terminate the complete child tree and reap the direct child."""
    if process.returncode is not None:
        return

    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, PermissionError, ValueError):
            try:
                process.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_sec)
            return
        except asyncio.TimeoutError:
            pass

        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_sec)
        except asyncio.TimeoutError:
            pass
        return

    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None

    def _signal_group(sig: signal.Signals) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass

    _signal_group(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_sec)
    except asyncio.TimeoutError:
        _signal_group(signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_sec)
        except asyncio.TimeoutError:
            pass


class WorkspaceBash(Bash):
    """AgentScope Bash variant that executes relative paths in a workspace."""

    def __init__(self, workdir: str | Path) -> None:
        super().__init__()
        self.workdir = str(Path(workdir))

    async def __call__(  # type: ignore[override]
        self,
        command: str,
        description: str = "",
        timeout: int = 120000,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Execute bash with the DataPaw workspace as subprocess cwd."""

        timeout_ms = min(timeout, 600000)
        timeout_sec = timeout_ms / 1000.0
        process: asyncio.subprocess.Process | None = None

        try:
            # 独立进程组：超时/取消时可整组回收，避免子进程泄漏到后台
            group_options: dict[str, Any]
            if os.name == "nt":
                group_options = {
                    "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
                }
            else:
                group_options = {"start_new_session": True}
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                **group_options,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_sec,
            )

            stdout = stdout_bytes.decode("utf-8", errors="replace").replace(
                "\r\n",
                "\n",
            )
            stderr = stderr_bytes.decode("utf-8", errors="replace").replace(
                "\r\n",
                "\n",
            )

            output = stdout
            if stderr:
                if output:
                    output += "\n"
                output += stderr

            if len(output) > 30000:
                output = output[:30000] + "\n... (output truncated)"

            if process.returncode != 0:
                result = f"Command failed: {command}\n"
                if stdout:
                    result += f"\nStdout:\n{stdout}"
                if stderr:
                    result += f"\nStderr:\n{stderr}"
                if len(result) > 30000:
                    result = result[:30000] + "\n... (output truncated)"

                yield ToolChunk(
                    content=[TextBlock(text=result)],
                    state="error",
                    is_last=True,
                )
            else:
                yield ToolChunk(
                    content=[TextBlock(text=output)],
                    state="running",
                    is_last=True,
                )

        except asyncio.TimeoutError:
            if process is not None:
                await _terminate_process_group(process)
            error_msg = f"Command timed out after {timeout_ms}ms: {command}"
            yield ToolChunk(
                content=[TextBlock(text=error_msg)],
                state="error",
                is_last=True,
            )

        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process_group(process)
            raise

        except Exception as e:
            if process is not None:
                await _terminate_process_group(process)
            error_msg = f"Command failed: {command}\nError: {str(e)}"
            yield ToolChunk(
                content=[TextBlock(text=error_msg)],
                state="error",
                is_last=True,
            )


class WorkspaceGlob(_WorkspaceToolMixin, Glob):
    """AgentScope Glob variant whose default search path is the workspace."""

    async def __call__(  # type: ignore[override]
        self,
        pattern: str,
        path: str | None = None,
    ) -> ToolChunk:
        resolved, error = self._resolve_contained_path(path or self.workdir)
        if error is not None:
            return ToolChunk(
                content=[TextBlock(text=error)],
                state="error",
                is_last=True,
            )
        return await super().__call__(pattern=pattern, path=resolved)

    def match_rule(
        self,
        rule_content: str | None,
        tool_input: dict[str, Any],
    ) -> bool:
        return super().match_rule(rule_content, self._with_default_path(tool_input))

    def generate_suggestions(self, tool_input: dict[str, Any]) -> list:
        return super().generate_suggestions(self._with_default_path(tool_input))


class WorkspaceGrep(_WorkspaceToolMixin, Grep):
    """AgentScope Grep variant whose default search path is the workspace."""

    async def __call__(  # type: ignore[override]
        self,
        pattern: str,
        path: str | None = None,
        output_mode: Literal[
            "content",
            "files_with_matches",
            "count",
        ] = "files_with_matches",
        glob: str | None = None,
        type: str | None = None,  # pylint: disable=redefined-builtin
        i: bool = False,
        case_insensitive: bool = False,
        context: int | None = None,
        multiline: bool = False,
        head_limit: int | None = None,
        offset: int = 0,
        n: bool = True,
        **kwargs: Any,
    ) -> ToolChunk:
        resolved, error = self._resolve_contained_path(path or self.workdir)
        if error is not None:
            return ToolChunk(
                content=[TextBlock(text=error)],
                state="error",
                is_last=True,
            )
        return await super().__call__(
            pattern=pattern,
            path=resolved,
            output_mode=output_mode,
            glob=glob,
            type=type,
            i=i,
            case_insensitive=case_insensitive,
            context=context,
            multiline=multiline,
            head_limit=head_limit,
            offset=offset,
            n=n,
            **kwargs,
        )

    def match_rule(
        self,
        rule_content: str | None,
        tool_input: dict[str, Any],
    ) -> bool:
        return super().match_rule(rule_content, self._with_default_path(tool_input))

    def generate_suggestions(self, tool_input: dict[str, Any]) -> list:
        return super().generate_suggestions(self._with_default_path(tool_input))

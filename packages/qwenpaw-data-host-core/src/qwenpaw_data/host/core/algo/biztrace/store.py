# -*- coding: utf-8 -*-
"""Append-only JSONL storage for BizTrace rows, segments and judge logs."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorePaths:
    """The three JSONL files a session writes."""

    biz_trace: Path
    segments: Path
    judge: Path


def build_store_paths(*, session_id: str, log_dir: str | None = None) -> StorePaths:
    """Locate the JSONL files for one session.

    They live outside every agent workspace on purpose: anything under one is
    reported by ``list_session_files`` and would pollute artifact verification.
    """

    from qwenpaw_data.host.core.paths import resolve_qwenpaw_data_home

    root = (
        Path(log_dir)
        if log_dir
        else resolve_qwenpaw_data_home() / "host" / "biztrace"
    )
    return StorePaths(
        biz_trace=root / "biz_trace" / f"{session_id}.jsonl",
        segments=root / "segments" / f"{session_id}.jsonl",
        judge=root / "segments" / f"{session_id}.judge.jsonl",
    )


def _append_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                # Partial line from an interrupted process; readers skip it.
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


class BizTraceStore:
    """Write and read the derived JSONL streams off the event loop."""

    def __init__(self, paths: StorePaths, *, enabled: bool = True) -> None:
        self.paths = paths
        self.enabled = enabled

    async def append_event(self, *, seq: int, event: dict[str, Any]) -> None:
        await self._append(self.paths.biz_trace, {"seq": seq, "event": event})

    async def append_segment(self, *, seq: int, segment: dict[str, Any]) -> None:
        await self._append(self.paths.segments, {"seq": seq, "segment": segment})

    async def append_judge(self, entry: dict[str, Any]) -> None:
        await self._append(self.paths.judge, entry)

    async def _append(self, path: Path, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        line = json.dumps(payload, ensure_ascii=False)
        try:
            await asyncio.to_thread(_append_lines, path, [line])
        except OSError:
            logger.exception("Failed to append BizTrace row to %s", path)

    async def read_events(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_read_rows, self.paths.biz_trace)

    async def read_segments(self, *, after_seq: int = -1) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(_read_rows, self.paths.segments)
        return [row for row in rows if int(row.get("seq", -1)) > after_seq]


__all__ = ["BizTraceStore", "StorePaths", "build_store_paths"]

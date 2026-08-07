# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Callable, Optional


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", name)[:120] or "_"


class DAGBroadcaster:
    """In-process connection table keyed by session id."""

    _queues_by_sid: dict[str, set[asyncio.Queue]] = {}

    @classmethod
    def subscribe(cls, sid: str, maxsize: int = 32) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        cls._queues_by_sid.setdefault(sid, set()).add(queue)
        return queue

    @classmethod
    def unsubscribe(cls, sid: str, queue: asyncio.Queue) -> None:
        queues = cls._queues_by_sid.get(sid)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            cls._queues_by_sid.pop(sid, None)

    @classmethod
    def push(cls, sid: str, snapshot: dict) -> None:
        for queue in tuple(cls._queues_by_sid.get(sid, set())):
            try:
                queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass


class DAGStore:
    """The only reader/writer for per-session DAG snapshot files."""

    def __init__(
        self,
        dag_root: str | Path,
        *,
        user_id: str = "default",
    ) -> None:
        self.dag_root = Path(dag_root)
        self.user_id = user_id or "default"
        self._on_write: list[Callable[[str, dict], None]] = [
            DAGBroadcaster.push,
        ]

    def on_write(self, callback: Callable[[str, dict], None]) -> None:
        self._on_write.append(callback)

    def dag_path(self, sid: str) -> Path:
        safe_sid = _sanitize(sid)
        safe_user = _sanitize(self.user_id)
        return self.dag_root / f"{safe_user}_{safe_sid}.json"

    async def read(self, sid: str) -> Optional[dict]:
        return await asyncio.to_thread(self.read_sync, sid)

    def read_sync(self, sid: str) -> Optional[dict]:
        path = self.dag_path(sid)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    async def write(self, sid: str, snapshot: dict) -> None:
        for callback in list(self._on_write):
            callback(sid, snapshot)
        await asyncio.to_thread(self.write_sync, sid, snapshot)

    def write_sync(self, sid: str, snapshot: dict) -> None:
        path = self.dag_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(snapshot, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)


def format_sse(snapshot: dict) -> str:
    """Serialize one DAG snapshot as a Server-Sent Events frame."""
    event_id = snapshot.get("sequence_number")
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("event: task_status")
    lines.append(
        "data: " + json.dumps(snapshot, ensure_ascii=False),
    )
    return "\n".join(lines) + "\n\n"

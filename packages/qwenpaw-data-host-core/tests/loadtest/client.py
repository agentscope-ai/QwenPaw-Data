# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

_TERMINAL = frozenset({"completed", "failed", "cancelled", "canceled", "timeout"})


@dataclass
class PluginCall:
    name: str
    t_ms: int


@dataclass
class ChatSample:
    index: int
    prompt: str
    chat_id: str = ""
    t0_ms: int | None = None
    t1_ms: int | None = None
    tmcp_ms: int | None = None
    tllm_ms: int | None = None
    t3_ms: int | None = None
    tend_ms: int | None = None
    status: str = ""
    http_status: int = 0
    error: str = ""
    plugin_calls: list[PluginCall] = field(default_factory=list)


class HostClient:
    def __init__(
        self,
        base: str,
        prefix: str,
        cookie: str,
        datasource_id: str,
    ) -> None:
        self.base = base.rstrip("/")
        self.prefix = prefix
        self.datasource_id = datasource_id
        self._http = httpx.AsyncClient(
            base_url=self.base,
            cookies={"qwenpaw_data_access": cookie},
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(30.0, read=300.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, path: str) -> str:
        return f"{self.prefix}{path}"

    async def allow_bash(self) -> None:
        path = self._url("/preferences/runtime-settings")
        resp = await self._http.get(path)
        resp.raise_for_status()
        body = resp.json()
        settings = body.get("runtime_settings") if isinstance(body, dict) else None
        if (
            isinstance(settings, dict)
            and settings.get("tool_confirmation_level") == "off"
        ):
            return
        put = await self._http.put(path, json={"tool_confirmation_level": "off"})
        put.raise_for_status()

    async def create_session(self) -> str:
        resp = await self._http.post(
            self._url("/sessions"),
            json={"title": "loadtest", "datasource_id": self.datasource_id},
        )
        resp.raise_for_status()
        session = resp.json().get("session") or {}
        session_id = session.get("id")
        if not session_id:
            raise RuntimeError("session id missing")
        return session_id

    async def start_chat(self, session_id: str, prompt: str) -> tuple[str, int]:
        t0 = time.perf_counter()
        resp = await self._http.post(
            self._url("/console/chat"),
            json={
                "session_id": session_id,
                "text": prompt,
                "datasource_id": self.datasource_id,
            },
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        resp.raise_for_status()
        chat = resp.json().get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            raise RuntimeError("chat id missing")
        return chat_id, elapsed

    async def wait_events(
        self,
        session_id: str,
        chat_id: str,
        origin: float,
        timeout_s: float,
    ) -> tuple[
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
        str,
        list[PluginCall],
    ]:
        t1: int | None = None
        tmcp: int | None = None
        tllm: int | None = None
        t3: int | None = None
        tend: int | None = None
        status = ""
        calls: list[PluginCall] = []
        msg_type: dict[str, str] = {}
        path = self._url(f"/sessions/{session_id}/chats/{chat_id}/events")
        deadline = origin + timeout_s
        async with self._http.stream("GET", path) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if time.perf_counter() > deadline:
                    return t1, tmcp, tllm, t3, tend, status or "timeout", calls
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                now = int((time.perf_counter() - origin) * 1000)
                kind = obj.get("object")
                if kind == "response" and t1 is None:
                    t1 = now
                if kind == "mcp.connect" and tmcp is None:
                    tmcp = now
                if kind == "model.call" and tllm is None:
                    tllm = now
                if kind == "message":
                    msg_id = obj.get("id")
                    msg_kind = obj.get("type")
                    if isinstance(msg_id, str) and isinstance(msg_kind, str):
                        msg_type[msg_id] = msg_kind
                    if msg_kind == "plugin_call":
                        name = ""
                        content = obj.get("content")
                        if isinstance(content, list) and content:
                            data = (
                                content[0].get("data")
                                if isinstance(content[0], dict)
                                else None
                            )
                            if isinstance(data, dict):
                                name = str(data.get("name") or "")
                        calls.append(PluginCall(name=name, t_ms=now))
                if kind == "content" and t3 is None:
                    msg_id = obj.get("msg_id")
                    if isinstance(msg_id, str) and msg_type.get(msg_id) == "message":
                        t3 = now
                if kind == "response":
                    event_status = obj.get("status")
                    if event_status in _TERMINAL:
                        if event_status == "completed":
                            tend = now
                        return t1, tmcp, tllm, t3, tend, event_status, calls
        return t1, tmcp, tllm, t3, tend, status or "stream_end", calls

    async def wait_chat_done(
        self,
        session_id: str,
        chat_id: str,
        origin: float,
        timeout_s: float,
    ) -> tuple[int | None, str]:
        deadline = origin + timeout_s
        path = self._url(f"/sessions/{session_id}/snapshot")
        while time.perf_counter() < deadline:
            resp = await self._http.get(path, params={"limit": 15})
            resp.raise_for_status()
            chats = resp.json().get("chats") or []
            row = next((c for c in chats if c.get("id") == chat_id), None)
            if row is not None:
                chat_status = row.get("status") or ""
                if chat_status in _TERMINAL:
                    tend = None
                    if chat_status == "completed":
                        tend = int((time.perf_counter() - origin) * 1000)
                    return tend, chat_status
            await asyncio.sleep(0.4)
        return None, "timeout"

    async def run_chat(self, index: int, prompt: str, timeout_s: float) -> ChatSample:
        sample = ChatSample(index=index, prompt=prompt)
        origin = time.perf_counter()
        try:
            session_id = await self.create_session()
            chat_id, t0 = await self.start_chat(session_id, prompt)
            sample.chat_id = chat_id
            sample.t0_ms = t0
            sample.http_status = 200
            t1, tmcp, tllm, t3, tend, status, calls = await self.wait_events(
                session_id, chat_id, origin, timeout_s
            )
            if status == "stream_end":
                tend, status = await self.wait_chat_done(
                    session_id, chat_id, origin, timeout_s
                )
            sample.t1_ms = t1
            sample.tmcp_ms = tmcp
            sample.tllm_ms = tllm
            sample.t3_ms = t3
            sample.tend_ms = tend
            sample.status = status
            sample.plugin_calls = calls
        except httpx.HTTPStatusError as exc:
            sample.http_status = exc.response.status_code
            sample.status = f"http_{exc.response.status_code}"
            try:
                sample.error = exc.response.text[:300]
            except httpx.ResponseNotRead:
                sample.error = exc.response.reason_phrase
        except Exception as exc:
            sample.status = "error"
            sample.error = str(exc)
        return sample

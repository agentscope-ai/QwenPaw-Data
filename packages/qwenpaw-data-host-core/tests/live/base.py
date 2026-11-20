# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import time
import unittest
from typing import Any
from urllib.parse import urljoin

import httpx


def _login_access_cookie() -> str:
    """POST Auth ``/api/auth/login`` and return the ``qwenpaw_data_access`` cookie."""
    auth_base = os.environ.get("QWENPAW_DATA_AUTH_URL", "http://127.0.0.1:8088").rstrip("/")
    username = os.environ.get("QWENPAW_DATA_LIVE_USERNAME", "alice").strip()
    password = (
        os.environ.get("QWENPAW_DATA_LIVE_PASSWORD", "").strip()
        or os.environ.get("DP_WALKTHROUGH_PASSWORD", "").strip()
    )
    if not password:
        raise unittest.SkipTest(
            "Host requires auth; set QWENPAW_DATA_ACCESS_COOKIE, or "
            "QWENPAW_DATA_LIVE_PASSWORD / DP_WALKTHROUGH_PASSWORD to login via Auth"
        )
    try:
        resp = httpx.post(
            f"{auth_base}/api/auth/login",
            json={"username": username, "password": password},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise unittest.SkipTest(f"Auth not reachable at {auth_base}: {exc}") from exc
    if resp.status_code != 200:
        raise unittest.SkipTest(
            f"Auth login failed for {username!r}: {resp.status_code} {resp.text}"
        )
    cookie = resp.cookies.get("qwenpaw_data_access", "").strip()
    if not cookie:
        raise unittest.SkipTest(
            "Auth login ok but no qwenpaw_data_access cookie in response"
        )
    return cookie


class LiveAPITestCase(unittest.TestCase):
    """Shared helpers for HTTP live tests against a running Host.

    Env:
      QWENPAW_DATA_API_BASE         default http://127.0.0.1:8089
      QWENPAW_DATA_ACCESS_COOKIE    optional; if empty and Host needs auth, auto-login
      QWENPAW_DATA_AUTH_URL         default http://127.0.0.1:8088
      QWENPAW_DATA_LIVE_USERNAME    default alice
      QWENPAW_DATA_LIVE_PASSWORD / DP_WALKTHROUGH_PASSWORD  used for auto-login
      QWENPAW_DATA_DATASOURCE_ID    default ds_demo
      QWENPAW_DATA_LIVE_SSE_TIMEOUT_S  default 180
    """

    base_url: str
    datasource_id: str
    sse_timeout_s: float
    client: httpx.Client

    @classmethod
    def setUpClass(cls) -> None:
        cls.base_url = os.environ.get(
            "QWENPAW_DATA_API_BASE", "http://127.0.0.1:8089"
        ).rstrip("/")
        cookie = os.environ.get("QWENPAW_DATA_ACCESS_COOKIE", "").strip()
        cls.datasource_id = os.environ.get("QWENPAW_DATA_DATASOURCE_ID", "ds_demo")
        cls.sse_timeout_s = float(os.environ.get("QWENPAW_DATA_LIVE_SSE_TIMEOUT_S", "180"))
        cls.client = httpx.Client(
            base_url=cls.base_url,
            cookies={"qwenpaw_data_access": cookie} if cookie else None,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(30.0, read=cls.sse_timeout_s),
        )
        try:
            r = cls.client.get("/api/auth/me")
        except httpx.HTTPError as exc:
            raise unittest.SkipTest(
                f"Host not reachable at {cls.base_url}: {exc}"
            ) from exc
        if r.status_code == 401 and not cookie:
            cookie = _login_access_cookie()
            cls.client.cookies.set("qwenpaw_data_access", cookie)
            r = cls.client.get("/api/auth/me")
        if r.status_code != 200:
            raise unittest.SkipTest(
                f"Host auth failed at {cls.base_url}: {r.status_code} {r.text}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def api_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def create_session(self, *, title: str) -> dict[str, Any]:
        r = self.client.post("/api/sessions", json={"title": title})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("session", body)
        self.assertTrue(body["session"].get("id"))
        return body["session"]

    def get_session(self, session_id: str) -> dict[str, Any]:
        r = self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["session"]

    def list_sessions(
        self,
        *,
        search_text: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search_text is not None:
            params["search_text"] = search_text
        r = self.client.get("/api/sessions", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("items", body)
        self.assertIn("total", body)
        return body

    def delete_session(self, session_id: str) -> None:
        r = self.client.delete(f"/api/sessions/{session_id}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("status"), "ok")

    def get_snapshot(self, session_id: str) -> dict[str, Any]:
        r = self.client.get(f"/api/sessions/{session_id}/snapshot")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def get_trace(
        self, session_id: str, chat_id: str, *, view: str = "raw"
    ) -> dict[str, Any]:
        r = self.client.get(
            f"/api/sessions/{session_id}/chats/{chat_id}/trace",
            params={"view": view},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def start_chat(
        self,
        *,
        session_id: str,
        text: str,
        datasource_id: str | None = None,
    ) -> dict[str, Any]:
        r = self.client.post(
            "/api/console/chat",
            json={
                "session_id": session_id,
                "text": text,
                "datasource_id": datasource_id or self.datasource_id,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        chat = r.json()["chat"]
        self.assertTrue(chat.get("id"))
        return chat

    def wait_chat_not_running(
        self, session_id: str, chat_id: str, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Poll snapshot until the chat leaves ``running``."""
        deadline = time.time() + (timeout_s or self.sse_timeout_s)
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            snap = self.get_snapshot(session_id)
            for chat in snap.get("chats") or []:
                if chat.get("id") == chat_id:
                    last = chat
                    if chat.get("status") != "running":
                        return chat
            time.sleep(0.2)
        self.fail(f"chat {chat_id} still running after timeout; last={last}")

    def subscribe_events_until_terminal(
        self,
        *,
        session_id: str,
        chat_id: str,
        after_sequence_number: int = -1,
        timeout_s: float | None = None,
    ) -> list[dict[str, Any]]:
        """Read SSE until ``response`` reaches a terminal status."""
        timeout_s = timeout_s or self.sse_timeout_s
        url = (
            f"/api/sessions/{session_id}/chats/{chat_id}/events"
            f"?after_sequence_number={after_sequence_number}"
        )
        events: list[dict[str, Any]] = []
        deadline = time.time() + timeout_s
        with self.client.stream(
            "GET",
            url,
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(30.0, read=timeout_s),
        ) as resp:
            self.assertEqual(resp.status_code, 200, resp.headers)
            event_name: str | None = None
            for line in resp.iter_lines():
                if time.time() > deadline:
                    self.fail(
                        f"SSE timeout after {timeout_s}s; got {len(events)} events"
                    )
                if line == "":
                    event_name = None
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                payload = json.loads(raw)
                if event_name and "object" not in payload:
                    payload = {**payload, "object": event_name}
                events.append(payload)
                if payload.get("object") == "response" and payload.get("status") in (
                    "completed",
                    "failed",
                    "cancelled",
                ):
                    break
        return events

    def fetch_session_bundle(self, session_id: str) -> dict[str, Any]:
        """snapshot + per-chat raw trace."""
        snap = self.get_snapshot(session_id)
        traces: dict[str, Any] = {}
        for chat in snap.get("chats") or []:
            cid = chat["id"]
            traces[cid] = self.get_trace(session_id, cid, view="raw")
        return {
            "session": snap["session"],
            "chats": snap.get("chats") or [],
            "traces": traces,
        }

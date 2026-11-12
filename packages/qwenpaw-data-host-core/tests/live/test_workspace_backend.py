# -*- coding: utf-8 -*-
"""Live checks for workspace backend (local vs docker).

Same machine as Host. Reads ``QWENPAW_DATA_WORKSPACE_BACKEND`` (default local),
same env the Host uses — only the matching case runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import unittest
from typing import Any

from .base import LiveAPITestCase


def _require_backend(expected: str) -> None:
    raw = os.environ.get("QWENPAW_DATA_WORKSPACE_BACKEND", "local").strip().lower()
    if raw != expected:
        raise unittest.SkipTest(f"QWENPAW_DATA_WORKSPACE_BACKEND={raw!r}, need {expected!r}")


def _container_name(me: dict[str, Any], *, agent_id: str) -> str:
    raw = f"{me['tenant_id']}\0{me['active_workspace_id']}\0{me['user_id']}\0{agent_id}"
    key = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"as_ws_{key}"


def _docker_inspect(name: str) -> dict[str, Any] | None:
    proc = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    items = json.loads(proc.stdout)
    return items[0] if items else None


def _wait_container_running(name: str, *, timeout_s: float) -> dict[str, Any]:
    """Poll docker until the workspace container is running (cold build ok)."""
    deadline = time.time() + timeout_s
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        info = _docker_inspect(name)
        if info is not None:
            last = info
            if info.get("State", {}).get("Running"):
                return info
        time.sleep(1.0)
    raise AssertionError(
        f"container {name} not running within {timeout_s}s; last={last}"
    )


class TestWorkspaceDockerLive(LiveAPITestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _require_backend("docker")
        super().setUpClass()

    def test_docker_container_running(self) -> None:
        me = self.client.get("/api/auth/me").json()
        session = self.create_session(title="live-ws-docker")
        session_id = session["id"]
        name = _container_name(me, agent_id=session.get("agent_id") or "default")
        # Cold image build can exceed the chat SSE timeout; wait on the
        # container itself, not chat completion.
        timeout_s = float(
            os.environ.get(
                "QWENPAW_DATA_LIVE_DOCKER_TIMEOUT_S",
                str(max(self.sse_timeout_s, 600.0)),
            )
        )
        chat_id: str | None = None
        try:
            chat = self.start_chat(session_id=session_id, text="只回复一个字：好")
            chat_id = chat["id"]

            info = _wait_container_running(name, timeout_s=timeout_s)
            labels = info.get("Config", {}).get("Labels") or {}
            self.assertEqual(labels.get("agentscope.workspace"), "true")

            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "sh",
                    "-c",
                    "test -f /.dockerenv && test -d /workspace && echo DOCKER_OK",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), "DOCKER_OK")
        finally:
            if chat_id:
                try:
                    self.wait_chat_not_running(session_id, chat_id, timeout_s=timeout_s)
                except Exception:
                    pass
            self.client.delete(f"/api/sessions/{session_id}")


class TestWorkspaceLocalLive(LiveAPITestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _require_backend("local")
        super().setUpClass()

    def test_local_has_no_docker_container(self) -> None:
        me = self.client.get("/api/auth/me").json()
        session = self.create_session(title="live-ws-local")
        session_id = session["id"]
        name = _container_name(me, agent_id=session.get("agent_id") or "default")
        try:
            chat = self.start_chat(
                session_id=session_id,
                text=(
                    "请只用 Bash 工具执行："
                    "test -f /.dockerenv && echo DOCKER_OK || echo HOST_OK"
                ),
            )
            events = self.subscribe_events_until_terminal(
                session_id=session_id,
                chat_id=chat["id"],
            )
            self.wait_chat_not_running(session_id, chat["id"])

            self.assertIsNone(
                _docker_inspect(name),
                f"local backend created unexpected container {name}",
            )
            blob = json.dumps(events, ensure_ascii=False)
            self.assertIn("HOST_OK", blob)
            self.assertNotIn("DOCKER_OK", blob)
        finally:
            self.client.delete(f"/api/sessions/{session_id}")

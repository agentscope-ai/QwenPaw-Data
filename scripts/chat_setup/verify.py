"""Validate Bash setup results and run real chat probes; never initialize configuration."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import sys
import time
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from types import SimpleNamespace
from typing import Any, Self

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_FILE = (
    pathlib.Path(os.environ.get("QWENPAW_DATA_ENV_FILE") or REPO_ROOT / ".env")
    .expanduser()
    .resolve()
)
GENERATED = ENV_FILE.with_name(f"{ENV_FILE.name}.chat-setup")
STATE_FILE = GENERATED / "state.env"
AUTH_URL = (
    os.environ.get("QWENPAW_DATA_AUTH_URL")
    or f"http://localhost:{os.environ.get('AUTH_PORT', '8088')}"
).rstrip("/")
CONTEXT_URL = (
    os.environ.get("QWENPAW_DATA_CM_BASE_URL")
    or f"http://localhost:{os.environ.get('CONTEXT_PORT', '8765')}"
).rstrip("/")
HOST_URL = (
    os.environ.get("QWENPAW_DATA_HOST_BASE_URL")
    or f"http://localhost:{os.environ.get('QWENPAW_PORT', '8089')}"
).rstrip("/")
HOST_UI_URL = (
    os.environ.get("VITE_HOST_FRONTEND_URL")
    or f"http://localhost:{os.environ.get('HOST_FRONTEND_PORT', '3100')}"
).rstrip("/")
DEFAULT_TIMEOUT = float(os.environ.get("QWENPAW_DATA_SETUP_HTTP_TIMEOUT", "1800"))
CHAT_TIMEOUT = 180.0


class ApiError(RuntimeError):
    """One API call failed in a way the builder cannot treat as idempotent."""

    def __init__(self, method: str, url: str, response: httpx.Response) -> None:
        body = response.text
        if len(body) > 600:
            body = f"{body[:600]}…"
        super().__init__(f"{method} {url} -> {response.status_code} {body}")


@dataclass
class Result:
    """A decoded API response with optional data-envelope access."""

    json: Any

    def data(self, *keys: str) -> Any:
        """Dig into common envelopes: ``{ok,data}``, ``{code,data}``, or raw."""
        payload = self.json
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        for key in keys:
            if not isinstance(payload, dict):
                return None
            payload = payload.get(key)
        return payload


class Session:
    """One authenticated identity with an in-memory cookie jar."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.client = httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False)

    def close(self) -> None:
        """Release the underlying connection pool."""
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(self, method: str, url: str, **kwargs: Any) -> Result:
        response = self.client.request(method, url, **kwargs)
        if method.upper() == "GET" and response.status_code == 401:
            refresh = self.client.post(f"{AUTH_URL}/api/auth/refresh")
            if refresh.status_code == 200:
                response = self.client.request(method, url, **kwargs)
        status = response.status_code
        if 200 <= status < 300:
            return Result(_safe_json(response))
        raise ApiError(method, url, response)

    def get(self, url: str, **kwargs: Any) -> Result:
        """GET helper."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Result:
        """POST helper."""
        return self.request("POST", url, **kwargs)

    def switch_workspace(self, workspace_id: str) -> Result:
        """Move this session's active Workspace; required before scoped writes."""
        result = self.post(
            f"{AUTH_URL}/api/auth/switch-workspace", json={"workspace_id": workspace_id}
        )
        return result


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


@dataclass
class Workspace:
    """One resolved Workspace: fixture key, real id, and owning tenant."""

    key: str
    name: str
    workspace_id: str
    tenant_key: str
    tenant_id: str


@dataclass
class Organization:
    """Resolved organization handed to every later phase."""

    accounts: dict[str, Any]
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    user_ids: dict[str, str] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)


def user_targets(org: Organization):
    """Yield each account once per intended workspace, including tenant admins."""
    for entry in org.accounts["users"]:
        keys = (
            [entry["workspace"]]
            if entry["workspace"]
            else [
                key
                for key, ws in org.workspaces.items()
                if ws.tenant_key == entry["tenant"]
            ]
        )
        if not keys:
            raise RuntimeError(f"{entry['username']}: no Workspace target")
        for key in keys:
            session = org.sessions[entry["username"]]
            ws = org.workspaces[key]
            session.switch_workspace(ws.workspace_id)
            yield (entry["username"], key, ws, session)


def assert_models_ready(session: Session) -> dict:
    document = session.get(f"{CONTEXT_URL}/api/v1/config").json
    if not document["configured"]:
        raise RuntimeError(
            f"{session.name}: CM models not ready: {document.get('errors')}"
        )
    space = document["embedding_space"]
    if not space["adopted"] or space["aligned"] is not True:
        raise RuntimeError(
            f"{session.name}: workspace Embedding configuration is not aligned"
        )
    return document


def _has_answer(chat: dict) -> bool:
    return any(
        str(s.get("conclusion") or "").strip() for s in chat.get("segments", [])
    ) or any(
        e.get("presentation", {}).get("card_type") == "text"
        and str(e.get("presentation", {}).get("body") or "").strip()
        for e in chat.get("biz_events", [])
        if e.get("presentation")
    )


def smoke_chat(
    ctx: Any, session: Session, datasource_id: str | None, label: str
) -> str:
    created = session.post(
        f"{HOST_URL}/api/sessions",
        json={
            "title": f"Chat setup smoke: {label}",
            "datasource_id": datasource_id,
            "enable_clarification": False,
        },
    ).data("session")
    session_id = created["id"]
    ctx.log("chat", f"{label}: session {session_id}")
    chat = session.post(
        f"{HOST_URL}/api/console/chat",
        json={
            "session_id": session_id,
            "datasource_id": datasource_id,
            "text": "这是聊天环境连通性检查。请直接回复：聊天环境已就绪。无需查询数据或修改任何内容。",
        },
    ).data("chat")
    chat_id = chat["id"]
    deadline = time.monotonic() + CHAT_TIMEOUT
    while True:
        snapshot = session.get(f"{HOST_URL}/api/sessions/{session_id}/snapshot").json
        current = next(
            (item for item in snapshot["chats"] if item["id"] == chat_id), None
        )
        if current:
            if current["status"] in {"failed", "canceled"} or current.get("error"):
                raise RuntimeError(
                    f"{label}: smoke chat {chat_id} failed: {current.get('error')}"
                )
            if current["status"] == "completed":
                if not _has_answer(current):
                    raise RuntimeError(
                        f"{label}: completed smoke chat has no visible answer"
                    )
                return session_id
        if time.monotonic() >= deadline:
            session.post(
                f"{HOST_URL}/api/console/chat/stop",
                params={"session_id": session_id, "chat_id": chat_id},
            )
            raise RuntimeError(f"{label}: smoke chat timed out after {CHAT_TIMEOUT}s")
        time.sleep(2)


def save_state(values: dict[str, Any]) -> pathlib.Path:
    """Overwrite the diagnostic state with results from the current run."""
    GENERATED.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by scripts/setup_chat.sh — do not edit by hand.",
        "# Diagnostic IDs and chat results from this run; not loaded by setup.",
    ]
    lines.extend(
        f"export {key}={shlex.quote(_coerce(values[key]))}" for key in sorted(values)
    )
    STATE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return STATE_FILE


def _coerce(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def verify(ctx):
    org = ctx.org
    completed = 0
    for username, key, workspace, session in user_targets(org):
        label = f"{username}@{workspace.name}"
        identity = session.get(f"{HOST_URL}/api/auth/me").json
        if (
            identity.get("user_id"),
            identity.get("tenant_id"),
            identity.get("active_workspace_id"),
        ) != (org.user_ids[username], workspace.tenant_id, workspace.workspace_id):
            raise RuntimeError(f"{label}: Host identity/scope mismatch")
        assert_models_ready(session)
        entry = next(u for u in org.accounts["users"] if u["username"] == username)
        allowed = {
            "member": {"member", "ws_admin", "tenant_admin"},
            "ws_admin": {"ws_admin", "tenant_admin"},
            "tenant_admin": {"tenant_admin"},
        }
        if identity.get("role") not in allowed[entry["role"]]:
            raise RuntimeError(f"{label}: unexpected Host role")
        required = {ctx.datasources[key]} if key in ctx.datasources else set()
        sources = (
            session.get(f"{HOST_URL}/api/datasources").data("items") if required else []
        )
        visible = {s["id"] for s in sources}
        if not required <= visible:
            raise RuntimeError(f"{label}: Host cannot see all configured datasources")
        active = None
        if ctx.workbook:
            active = session.get(f"{CONTEXT_URL}/api/datasources/active").data(
                "datasource_id"
            )
            if not active or active not in visible:
                raise RuntimeError(
                    f"{label}: default datasource is missing or inaccessible"
                )
            for datasource_id in required | {active}:
                datasets = session.get(
                    f"{HOST_URL}/api/datasets", params={"datasource_id": datasource_id}
                ).data("items")
                if not datasets:
                    raise RuntimeError(
                        f"{label}: Host sees no datasets for {datasource_id}"
                    )
        chat_session = smoke_chat(ctx, session, active if ctx.workbook else None, label)
        ctx.state[f"DP_SMOKE_{username.upper().replace('-', '_')}_{key.upper()}"] = (
            chat_session
        )
        ctx.log("ready", f"{label}: chat passed")
        completed += 1
    if not completed:
        raise RuntimeError("No user chats were verified")
    ctx.state["DP_CHAT_READY"] = True
    print(f"\nChat ready: {HOST_UI_URL}")
    if not ctx.workbook:
        print(
            "Accounts and models are ready. Configure datasource/semantics in the UI before data analysis."
        )


def main():
    if len(sys.argv) != 1:
        raise SystemExit(
            "verify.py reads the setup result from stdin; no arguments accepted"
        )
    manifest = json.load(sys.stdin)
    org = Organization(accounts=manifest["accounts"], user_ids=manifest["user_ids"])
    org.workspaces = {
        key: Workspace(**value) for key, value in manifest["workspaces"].items()
    }
    state = {
        "DP_CHAT_READY": False,
        "QWENPAW_DATA_AUTH_URL": AUTH_URL,
        "QWENPAW_DATA_CM_BASE_URL": CONTEXT_URL,
        "QWENPAW_DATA_HOST_BASE_URL": HOST_URL,
    }
    state.update(
        {
            f"DP_WORKSPACE_{key.upper()}": w.workspace_id
            for key, w in org.workspaces.items()
        }
    )
    state.update(
        {f"DP_TENANT_{t['key'].upper()}": t["id"] for t in org.accounts["tenants"]}
    )
    state.update(
        {
            f"DP_USER_{u.replace('-', '_').upper()}": uid
            for u, uid in org.user_ids.items()
        }
    )
    state.update(
        {
            f"DP_DATASOURCE_{key.upper()}": value
            for key, value in manifest["datasources"].items()
        }
    )
    ctx = SimpleNamespace(
        org=org,
        datasources=manifest["datasources"],
        workbook=manifest["workbook"],
        state=state,
        log=lambda scope, message: print(f"    [{scope}] {message}", flush=True),
    )
    try:
        # Reuse the sessions that successfully logged in during initialization.
        for entry in org.accounts["users"]:
            username = entry["username"]
            jar = MozillaCookieJar(manifest["cookies"][username])
            jar.load(ignore_discard=True)
            # curl stores host-only localhost cookies literally; Python's cookie
            # policy matches single-label hosts against <host>.local instead.
            for cookie in list(jar):
                if not cookie.domain_specified and "." not in cookie.domain:
                    jar.clear(cookie.domain, cookie.path, cookie.name)
                    cookie.domain += ".local"
                    jar.set_cookie(cookie)
            session = Session(username)
            session.client.cookies = httpx.Cookies(jar)
            org.sessions[username] = session
        verify(ctx)
        return 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - report validation failure
        print(f"Chat verification failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for session in org.sessions.values():
            session.close()
        save_state(state)


if __name__ == "__main__":
    raise SystemExit(main())

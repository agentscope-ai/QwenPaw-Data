"""SSRF callback policy, DNS pinning and resource-limit tests."""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from context_manager import net_guard
from context_manager.net_guard import (
    CallbackRequestError,
    CallbackUrlError,
    ensure_safe_callback_url,
)


def _dns(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/cb",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
        "http://",
        "https://user:password@example.com/cb",
        "https://example.com/cb#fragment",
    ],
)
def test_rejects_malformed_callback_urls(url: str) -> None:
    with pytest.raises(CallbackUrlError):
        ensure_safe_callback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/cb",
        "http://localhost/cb",
        "http://[::1]/cb",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.8/cb",
        "http://192.168.1.1/cb",
        "http://0.0.0.0/cb",
        "https://93.184.216.34/cb",
    ],
)
def test_callbacks_are_deny_by_default(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATAPAW_CALLBACK_ALLOWLIST", raising=False)
    with pytest.raises(CallbackUrlError):
        ensure_safe_callback_url(url)


def test_builtin_internal_callback_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAPAW_CALLBACK_ALLOWLIST", raising=False)
    allowed = (
        "http://127.0.0.1:8765"
        "/api/semantic-config/weave-task/callback"
    )
    assert ensure_safe_callback_url(f"  {allowed}  ") == allowed
    with pytest.raises(CallbackUrlError):
        ensure_safe_callback_url(f"{allowed}?target=/api/admin")


def test_internal_callback_configuration_is_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATAPAW_INTERNAL_CALLBACK_ORIGINS",
        "http://10.0.0.8:8765",
    )
    with pytest.raises(CallbackUrlError, match="loopback origins"):
        ensure_safe_callback_url(
            "http://10.0.0.8:8765/api/semantic-config/weave-task/callback"
        )


def test_internal_callback_dns_must_remain_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATAPAW_INTERNAL_CALLBACK_ORIGINS", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: _dns("10.0.0.8", 8765),
    )
    with pytest.raises(CallbackUrlError, match="outside loopback"):
        ensure_safe_callback_url(
            "http://localhost:8765/api/semantic-config/weave-task/callback"
        )


def test_allowlist_requires_exact_origin_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATAPAW_CALLBACK_ALLOWLIST",
        "https://callbacks.example.com:8443",
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: _dns("93.184.216.34", 8443),
    )
    url = "https://callbacks.example.com:8443/events?id=1"
    assert ensure_safe_callback_url(url) == url
    with pytest.raises(CallbackUrlError):
        ensure_safe_callback_url("https://callbacks.example.com/events")


def test_allowlisted_private_ip_literal_is_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAPAW_CALLBACK_ALLOWLIST", "http://127.0.0.1:9000")
    url = "http://127.0.0.1:9000/callback"
    assert ensure_safe_callback_url(url) == url
    with pytest.raises(CallbackUrlError):
        ensure_safe_callback_url("http://127.0.0.1:9001/callback")


def test_allowlisted_hostname_cannot_rebind_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATAPAW_CALLBACK_ALLOWLIST",
        "https://callbacks.example.com",
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: _dns("127.0.0.1"),
    )
    with pytest.raises(CallbackUrlError, match="non-public"):
        ensure_safe_callback_url("https://callbacks.example.com/events")


async def test_pinned_resolver_never_performs_a_second_dns_lookup() -> None:
    resolver = net_guard._PinnedResolver(
        "callbacks.example.com",
        ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
    )
    results = await resolver.resolve(
        "callbacks.example.com",
        443,
        socket.AF_UNSPEC,
    )
    assert {result["host"] for result in results} == {
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    }
    with pytest.raises(OSError):
        await resolver.resolve("attacker.example", 443, socket.AF_UNSPEC)


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _: int):
        for chunk in self.chunks:
            yield chunk


async def test_response_body_limit_is_enforced() -> None:
    response = SimpleNamespace(
        content_length=None,
        content=_FakeContent([b"1234", b"56789"]),
    )
    with pytest.raises(CallbackRequestError, match="response exceeds"):
        await net_guard._read_limited(response, 8)


async def test_safe_post_disables_redirects_proxies_and_decompression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = net_guard._CallbackTarget(
        "https://callbacks.example.com/event",
        "callbacks.example.com",
        443,
        ("93.184.216.34",),
    )
    monkeypatch.setattr(net_guard, "_resolve_target", lambda _: target)
    observed: dict[str, object] = {}

    class _Response:
        status = 204
        content_length = 0
        content = _FakeContent([])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def __init__(self, **kwargs):
            observed["session"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            observed["url"] = url
            observed["post"] = kwargs
            return _Response()

    monkeypatch.setattr(
        net_guard.aiohttp,
        "TCPConnector",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(net_guard.aiohttp, "ClientSession", _Session)
    result = await net_guard.post_safe_callback(
        target.url,
        payload={"status": "ok"},
    )

    assert result.status_code == 204
    assert observed["url"] == target.url
    assert observed["session"]["trust_env"] is False
    assert observed["session"]["auto_decompress"] is False
    assert observed["post"]["allow_redirects"] is False

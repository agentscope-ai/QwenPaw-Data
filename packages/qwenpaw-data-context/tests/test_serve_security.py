import importlib.util
import sys
from pathlib import Path


_SERVE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "serve.py"
_SPEC = importlib.util.spec_from_file_location("qwenpaw_data_context_serve", _SERVE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SERVE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SERVE)


def test_loopback_host_detection() -> None:
    assert _SERVE._is_loopback_host("127.0.0.1")
    assert _SERVE._is_loopback_host("127.0.0.2")
    assert _SERVE._is_loopback_host("::1")
    assert _SERVE._is_loopback_host("[::1]")
    assert _SERVE._is_loopback_host("localhost")


def test_external_host_detection() -> None:
    assert not _SERVE._is_loopback_host("0.0.0.0")
    assert not _SERVE._is_loopback_host("192.168.1.20")
    assert not _SERVE._is_loopback_host("qwenpaw_data.example.com")


def test_scoped_keys_count_as_external_bind_authentication(monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_API_KEYS", raising=False)
    assert not _SERVE._authentication_configured()
    monkeypatch.setenv("QWENPAW_DATA_API_KEYS", '[{"id":"reader"}]')
    assert _SERVE._authentication_configured()


def test_http_concurrency_limit_is_positive_and_configurable(monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HTTP_MAX_CONCURRENCY", "64")
    assert _SERVE._positive_int_env("QWENPAW_DATA_HTTP_MAX_CONCURRENCY", 128) == 64
    monkeypatch.setenv("QWENPAW_DATA_HTTP_MAX_CONCURRENCY", "0")
    assert _SERVE._positive_int_env("QWENPAW_DATA_HTTP_MAX_CONCURRENCY", 128) == 1
    monkeypatch.setenv("QWENPAW_DATA_HTTP_MAX_CONCURRENCY", "invalid")
    assert _SERVE._positive_int_env("QWENPAW_DATA_HTTP_MAX_CONCURRENCY", 128) == 128


def test_proxy_auth_requirement_fails_closed_on_loopback(
    monkeypatch,
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_API_KEYS", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["serve.py", "--host", "127.0.0.1", "--require-auth"],
    )

    assert _SERVE.main() == 2

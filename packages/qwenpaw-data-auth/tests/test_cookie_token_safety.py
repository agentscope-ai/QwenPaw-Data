from http.cookies import SimpleCookie

import pytest
from fastapi.responses import Response

from qwenpaw_data_auth.auth_api import _set_access_cookie, _set_auth_cookies
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.token import TokenIssuer


def test_signed_claims_cannot_inject_cookie_headers(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_ENV", "production")
    cfg = AuthConfig.from_env()
    issuer = TokenIssuer("test-key")
    access = issuer.issue_access("user\r\nSet-Cookie: injected=1", "t", "w", "member")
    refresh = issuer.issue_refresh("user; injected=1", "t")
    response = Response()
    _set_auth_cookies(response, access, refresh, cfg)
    headers = [value.decode("latin-1") for key, value in response.raw_headers if key == b"set-cookie"]
    assert len(headers) == 2
    for header in headers:
        assert "\r" not in header and "\n" not in header
        cookie = SimpleCookie()
        cookie.load(header)
        assert len(cookie) == 1
        assert next(iter(cookie.values()))["httponly"]
        assert next(iter(cookie.values()))["secure"]
    assert headers[0].startswith(f"qwenpaw_data_access={access};")
    assert headers[1].startswith(f"qwenpaw_data_refresh={refresh};")


def test_rejects_invalid_token_before_setting_either_cookie(monkeypatch):
    cfg = AuthConfig.from_env()
    valid = TokenIssuer("test-key").issue_access("u", "t", "w", "member")
    response = Response()
    with pytest.raises(ValueError, match="Invalid signed cookie token"):
        _set_auth_cookies(response, valid, "injected\r\nSet-Cookie: admin=1", cfg)
    assert not any(key == b"set-cookie" for key, _ in response.raw_headers)
    with pytest.raises(ValueError, match="Invalid signed cookie token"):
        _set_access_cookie(response, valid + "; admin=1", cfg)

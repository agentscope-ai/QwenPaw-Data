"""Access-token TTL defaults and QWENPAW_DATA_ACCESS_TOKEN_TTL override."""

from qwenpaw_data_auth.config import (
    ACCESS_TOKEN_TTL_DEFAULT,
    REFRESH_TOKEN_TTL_DEFAULT,
    AuthConfig,
)


def test_access_token_ttl_defaults_to_24h_in_development(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_ENV", "development")
    monkeypatch.delenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", raising=False)
    assert AuthConfig.from_env().access_token_ttl == ACCESS_TOKEN_TTL_DEFAULT
    assert ACCESS_TOKEN_TTL_DEFAULT == 86400


def test_access_token_ttl_defaults_to_24h_in_production(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_ENV", "production")
    monkeypatch.delenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", raising=False)
    assert AuthConfig.from_env().access_token_ttl == 86400


def test_access_token_ttl_can_be_overridden(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", "7200")
    assert AuthConfig.from_env().access_token_ttl == 7200


def test_invalid_access_token_ttl_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", "nope")
    assert AuthConfig.from_env().access_token_ttl == 86400
    monkeypatch.setenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", "0")
    assert AuthConfig.from_env().access_token_ttl == 86400
    monkeypatch.setenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", "-10")
    assert AuthConfig.from_env().access_token_ttl == 86400


def test_access_token_ttl_is_clamped_to_the_refresh_ttl(monkeypatch):
    """QWENPAW_DATA_ACCESS_TOKEN_TTL is capped at the refresh ttl."""
    monkeypatch.setenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", "86400000")
    cfg = AuthConfig.from_env()
    assert cfg.access_token_ttl == REFRESH_TOKEN_TTL_DEFAULT
    assert cfg.access_token_ttl <= cfg.refresh_token_ttl


def test_cookie_secure_still_follows_the_environment(monkeypatch):
    """cookie_secure is false in development and true otherwise."""
    monkeypatch.setenv("QWENPAW_DATA_ENV", "development")
    assert AuthConfig.from_env().cookie_secure is False
    monkeypatch.setenv("QWENPAW_DATA_ENV", "production")
    assert AuthConfig.from_env().cookie_secure is True

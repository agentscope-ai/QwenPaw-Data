from context_manager.secrets.redact import _redact_str


def test_redacts_nested_json_credentials() -> None:
    raw = (
        '{"password":"db-pass","access_key_id":"LTAIEXAMPLE123456",'
        '"access_key_secret":"cloud-secret","sts_token":"session-secret",'
        '"client_secret":"oauth-secret","refresh_token":"refresh-secret"}'
    )

    redacted = _redact_str(raw)

    for secret in (
        "db-pass",
        "LTAIEXAMPLE123456",
        "cloud-secret",
        "session-secret",
        "oauth-secret",
        "refresh-secret",
    ):
        assert secret not in redacted


def test_redacts_authorization_bearer() -> None:
    assert _redact_str("Authorization: Bearer abc.def-123") == (
        "Authorization: Bearer [REDACTED]"
    )

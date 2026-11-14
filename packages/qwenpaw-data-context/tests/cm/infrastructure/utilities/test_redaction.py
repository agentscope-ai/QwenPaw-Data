"""Verify credentials cannot cross the process logging boundary."""

from __future__ import annotations

import io
import logging

import pytest
from context_manager.utils.redaction import install_credential_redaction
from uvicorn.logging import AccessFormatter, DefaultFormatter


@pytest.fixture(autouse=True)
def restore_handler_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the process logging method after each test."""
    monkeypatch.setattr(logging.Handler, "format", logging.Handler.format)


def _emit(
    message: str,
    *args: object,
    extra: dict[str, object] | None = None,
    formatter: str | logging.Formatter = "%(message)s",
    with_exception: bool = False,
) -> str:
    install_credential_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(formatter) if isinstance(formatter, str) else formatter
    )
    logger = logging.getLogger(f"context_manager.tests.redaction.{id(stream)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        if with_exception:
            try:
                raise RuntimeError("password=traceback-secret")
            except RuntimeError:
                logger.exception(message, *args, extra=extra)
        else:
            logger.info(message, *args, extra=extra)
        return stream.getvalue()
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        pytest.param("api_key=%s", "provider-secret", id="formatted-api-key"),
        pytest.param(
            "Authorization: Bearer %s",
            "header.secret.token",
            id="bearer-authorization",
        ),
        pytest.param(
            "warehouse=postgresql://reader:%s@db.internal/context",
            "database-secret",
            id="credential-url",
        ),
        pytest.param("cloud key %s", "AKIA1234567890ABCDEF", id="aws-access-key"),
        pytest.param("cloud key %s", "LTAI1234567890ABCDEF", id="aliyun-access-key"),
        pytest.param(
            "provider rejected %s",
            "sk-provider123456",
            id="provider-api-key",
        ),
        pytest.param(
            "OPENAI_API_KEY=%s",
            "prefixed-field-secret",
            id="prefixed-environment-field",
        ),
        pytest.param(
            "key=%s",
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
            id="pem-private-key",
        ),
    ],
)
def test_process_logs_redact_each_supported_credential_shape(
    message: str,
    secret: str,
) -> None:
    """Every supported credential form is removed before handler output."""
    rendered = _emit(message, secret)

    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_process_logs_redact_structured_secret_fields() -> None:
    """Structured logging extras cannot bypass message redaction."""
    rendered = _emit(
        "provider request failed",
        extra={
            "credential_payload": {
                "api_key": "structured-secret",
                "provider": "provider-a",
            }
        },
        formatter="%(message)s %(credential_payload)s",
    )

    assert "structured-secret" not in rendered
    assert "[REDACTED]" in rendered
    assert "provider-a" in rendered


def test_process_logs_redact_exceptions_and_preserve_traceback_context() -> None:
    """Exception rendering keeps diagnostics without leaking either secret."""
    rendered = _emit(
        "provider failed with token=%s",
        "message-secret",
        with_exception=True,
    )

    assert "message-secret" not in rendered
    assert "traceback-secret" not in rendered
    assert rendered.count("[REDACTED]") >= 2
    assert "[REDACTED]]" not in rendered
    assert "RuntimeError" in rendered
    assert "provider failed" in rendered


def test_process_logs_preserve_non_secret_operational_language() -> None:
    """Credential words without values remain useful diagnostic context."""
    message = "token count is 42; password policy validation failed"

    assert _emit(message).strip() == message


@pytest.mark.parametrize("use_colors", [False, True])
@pytest.mark.parametrize(
    ("path", "safe_path"),
    [
        ("/api/health", "/api/health"),
        (
            "/api/health?api_key=query-secret&probe=1",
            "/api/health?api_key=[REDACTED]&probe=1",
        ),
    ],
)
def test_uvicorn_access_logs_preserve_request_fields_and_redact_credentials(
    use_colors: bool,
    path: str,
    safe_path: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rendered = _emit(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:43210",
        "GET",
        path,
        "1.1",
        200,
        formatter=AccessFormatter(
            '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=use_colors,
        ),
    )

    assert "127.0.0.1:43210" in rendered
    assert f"GET {safe_path} HTTP/1.1" in rendered
    assert "200 OK" in rendered
    assert "query-secret" not in rendered
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("use_colors", [False, True])
def test_uvicorn_default_logs_preserve_message_templates_until_formatting(
    use_colors: bool,
) -> None:
    rendered = _emit(
        "provider api_key=%s",
        "provider-secret",
        extra={"color_message": "colored provider api_key=%s"},
        formatter=DefaultFormatter(
            "%(levelprefix)s %(message)s", use_colors=use_colors
        ),
    )

    assert "provider api_key=[REDACTED]" in rendered
    assert "provider-secret" not in rendered
    assert ("colored provider" in rendered) is use_colors


def test_process_logs_redact_text_created_by_the_formatter() -> None:
    class ProviderFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return f"{super().format(record)} token=formatter-secret"

    rendered = _emit("provider request failed", formatter=ProviderFormatter())

    assert rendered.strip() == "provider request failed token=[REDACTED]"


def test_structured_credentials_are_redacted_even_without_the_field_name() -> None:
    rendered = _emit(
        "provider request failed",
        extra={"api_key": "structured-secret"},
        formatter="%(message)s %(api_key)s",
    )

    assert rendered.strip() == "provider request failed [REDACTED]"


def test_installation_is_idempotent_and_covers_future_handlers() -> None:
    """Repeated bootstrap redacts future handlers without mutating log records."""
    install_credential_redaction()
    install_credential_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("context_manager.tests.redaction.future-handler")
    logger.addHandler(handler)
    try:
        install_credential_redaction()
        install_credential_redaction()
        record = logger.makeRecord(
            logger.name,
            logging.INFO,
            __file__,
            0,
            "api_key=%s",
            ("late-secret",),
            None,
        )
        assert handler.format(record) == "api_key=[REDACTED]"
        handler.setFormatter(logging.Formatter("changed: %(message)s"))
        assert handler.format(record) == "changed: api_key=[REDACTED]"
        assert record.msg == "api_key=%s"
        assert record.args == ("late-secret",)
    finally:
        logger.removeHandler(handler)


def test_installation_covers_existing_handlers() -> None:
    """Existing handlers pick up redaction without per-handler installation."""
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(logging.Formatter("existing: %(message)s"))
    install_credential_redaction()
    record = logging.LogRecord(
        "existing",
        logging.INFO,
        __file__,
        0,
        "token=%s",
        ("old-secret",),
        None,
    )

    assert handler.format(record) == "existing: token=[REDACTED]"


def test_formatting_preserves_original_record_and_nested_fields() -> None:
    """Each handler formats a separate copy, preserving shared caller data."""
    payload = {
        "api_key": "structured-secret",
        "nested": [{"password": "nested-secret"}],
        "other": ({"token": "tuple-secret"},),
        "provider": "provider-a",
    }
    record = logging.makeLogRecord({"msg": "request failed", "payload": payload})
    original_fields = record.__dict__.copy()
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(logging.Formatter("%(message)s %(payload)s"))
    install_credential_redaction()

    rendered = handler.format(record)

    assert "structured-secret" not in rendered
    assert "nested-secret" not in rendered
    assert "tuple-secret" not in rendered
    assert "provider-a" in rendered
    assert record.__dict__ == original_fields
    assert payload["api_key"] == "structured-secret"
    assert payload["nested"][0]["password"] == "nested-secret"
    assert payload["other"][0]["token"] == "tuple-secret"

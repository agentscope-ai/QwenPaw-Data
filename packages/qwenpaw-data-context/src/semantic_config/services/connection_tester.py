"""Data-source connection test, delegated to the unified Connector layer."""

from __future__ import annotations

import asyncio
import time

from semantic_config.models.datasource import ConnectionTestResponse
from semantic_config.models.datasource_config import resolve_type, validate_config

CONNECT_TIMEOUT = 10  # seconds; driver-level connect timeout (Connector connect_args)
_OVERALL_TIMEOUT = (
    CONNECT_TIMEOUT + 5
)  # seconds; wait_for backstop (ODPS has no connect_timeout)
_CREDENTIAL_FIELDS = frozenset(
    {"password", "access_key_id", "access_key_secret", "sts_token", "credentials_json"}
)


def _safe_error_message(exc: Exception, config: dict) -> str:
    """Redact known credential values even if a driver echoes them verbatim."""
    from context_manager.secrets.redact import _redact_str

    message = _redact_str(str(exc))
    for key in _CREDENTIAL_FIELDS:
        value = config.get(key)
        if value not in (None, ""):
            message = message.replace(str(value), "***")
    return message[:500]


def _run_test(datasource_type: str, normalized: dict) -> tuple[bool, str, int | None]:
    """Probe supplied credentials through CM's relational connector."""
    from dataclasses import replace

    from context_manager.sql import (
        Datasource,
        MySqlConfig,
        PostgresConfig,
        RdbConnector,
        SqliteConfig,
    )

    target = Datasource(
        tenant_id="connection-test",
        workspace_id="connection-test",
        datasource_id="connection-test",
        datasource_name="connection-test",
        datasource_type=datasource_type,
        config=normalized,
    ).to_rdb_target()
    if isinstance(target.config, (PostgresConfig, MySqlConfig, SqliteConfig)):
        target = replace(
            target, config=replace(target.config, connect_timeout=CONNECT_TIMEOUT)
        )
    result = RdbConnector().probe(target)
    return result.healthy, "连接成功" if result.healthy else "连接失败", None


async def test_connection(
    datasource_type: str | None, config: dict | None
) -> ConnectionTestResponse:
    """Validate config and test the connection. Bad config → BadRequestError(400); connect failure → success=False."""
    normalized = validate_config(datasource_type, config)
    canonical = resolve_type(datasource_type).value

    t0 = time.monotonic()
    try:
        success, message, tables_found = await asyncio.wait_for(
            asyncio.to_thread(_run_test, canonical, normalized),
            timeout=_OVERALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        success, message, tables_found = (
            False,
            f"连接超时（>{_OVERALL_TIMEOUT}s）",
            None,
        )
    except Exception as exc:  # noqa: BLE001 — construction/driver errors become the failure message
        success, message, tables_found = False, _safe_error_message(exc, normalized), None

    return ConnectionTestResponse(
        success=success,
        message=message,
        tables_found=tables_found,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

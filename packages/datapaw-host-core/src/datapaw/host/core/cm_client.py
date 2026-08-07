"""HTTP client for Context Manager discovery APIs."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

CM_BASE_URL_ENV = "DATAPAW_CM_BASE_URL"
API_TOKEN_ENV = "DATAPAW_API_TOKEN"
CLIENT_API_TOKEN_ENV = "DATAPAW_CLIENT_API_TOKEN"
DEFAULT_CM_BASE_URL = "http://127.0.0.1:8765"
DATASOURCE_LIST_PATH = "/api/v1/cm/datasources"
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 10.0


class CMClientError(RuntimeError):
    """Raised when CM cannot provide a valid datasource list."""


@dataclass(frozen=True)
class CMDatasource:
    """Datasource record returned by the CM discovery endpoint."""

    datasource_id: str
    datasource_name: str | None
    datasource_type: str | None
    config: dict[str, Any] | None = field(repr=False)


@dataclass(frozen=True)
class CMDatasourceList:
    """Complete, unpaginated datasource result."""

    items: list[CMDatasource]
    total: int


def resolve_cm_base_url(env: Mapping[str, str] | None = None) -> str:
    """Resolve the CM origin from the environment or the local default."""

    values = os.environ if env is None else env
    configured = str(values.get(CM_BASE_URL_ENV, "") or "").strip()
    return (configured or DEFAULT_CM_BASE_URL).rstrip("/")


def _trust_env_for_url(url: str) -> bool:
    """Avoid proxying loopback CM requests while honoring proxies remotely."""

    hostname = (urlparse(url).hostname or "").strip().strip("[]").lower()
    if hostname == "localhost":
        return False
    try:
        return not ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return True


def _required_int(payload: dict[str, Any], field_name: str, *, minimum: int) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CMClientError(f"CM datasource response has invalid {field_name}")
    return value


def _nullable_string(record: dict[str, Any], field_name: str, *, index: int) -> str | None:
    value = record.get(field_name)
    if value is not None and not isinstance(value, str):
        raise CMClientError(
            f"CM datasource response item {index} has invalid {field_name}",
        )
    return value


def _parse_datasource(record: Any, *, index: int) -> CMDatasource:
    if not isinstance(record, dict):
        raise CMClientError(f"CM datasource response item {index} is not an object")

    datasource_id = record.get("datasource_id")
    if not isinstance(datasource_id, str) or not datasource_id.strip():
        raise CMClientError(
            f"CM datasource response item {index} has invalid datasource_id",
        )

    return CMDatasource(
        datasource_id=datasource_id,
        datasource_name=_nullable_string(record, "datasource_name", index=index),
        datasource_type=_nullable_string(record, "datasource_type", index=index),
        # Discovery uses the credential-free metadata endpoint. Ignore an
        # unexpected legacy config field so the CLI cannot accidentally echo it.
        config=None,
    )


class ContextManagerClient:
    """Minimal synchronous client used by the standalone CLI."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        api_token: str | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self.base_url = (base_url or resolve_cm_base_url()).strip().rstrip("/")
        if not self.base_url:
            self.base_url = DEFAULT_CM_BASE_URL
        self.page_size = page_size
        self.timeout = timeout
        self.transport = transport
        self.api_token = (
            (
                (os.environ.get(CLIENT_API_TOKEN_ENV) or "").strip()
                or (os.environ.get(API_TOKEN_ENV) or "").strip()
            )
            if api_token is None
            else api_token.strip()
        )

    def list_datasources(self) -> CMDatasourceList:
        """Fetch every datasource page and validate the new CM contract."""

        items: list[CMDatasource] = []
        expected_total: int | None = None
        page = 1

        headers = (
            {"Authorization": f"Bearer {self.api_token}"}
            if self.api_token
            else None
        )
        with httpx.Client(
            timeout=self.timeout,
            transport=self.transport,
            trust_env=_trust_env_for_url(self.base_url),
            headers=headers,
        ) as client:
            while True:
                payload = self._get_page(client, page)
                records = payload.get("records")
                if not isinstance(records, list):
                    raise CMClientError("CM datasource response has invalid records")

                total = _required_int(payload, "total", minimum=0)
                response_page = _required_int(payload, "page", minimum=1)
                _required_int(payload, "size", minimum=1)
                if response_page != page:
                    raise CMClientError("CM datasource response has an unexpected page")

                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    raise CMClientError("CM datasource response total changed during pagination")

                start_index = len(items)
                items.extend(
                    _parse_datasource(record, index=start_index + offset)
                    for offset, record in enumerate(records)
                )

                if len(items) > expected_total:
                    raise CMClientError("CM datasource response contains more records than total")
                if len(items) == expected_total:
                    return CMDatasourceList(items=items, total=expected_total)
                if not records:
                    raise CMClientError("CM datasource response ended before total was reached")
                page += 1

    def _get_page(self, client: httpx.Client, page: int) -> dict[str, Any]:
        try:
            response = client.get(
                f"{self.base_url}{DATASOURCE_LIST_PATH}",
                params={"page": page, "size": self.page_size},
            )
        except httpx.TimeoutException as exc:
            raise CMClientError(f"CM datasource request timed out on page {page}") from exc
        except httpx.RequestError as exc:
            raise CMClientError(f"CM datasource request failed on page {page}") from exc

        if not response.is_success:
            raise CMClientError(
                f"CM datasource request returned HTTP {response.status_code} on page {page}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CMClientError("CM datasource response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CMClientError("CM datasource response is not an object")
        return payload


__all__ = [
    "CM_BASE_URL_ENV",
    "CLIENT_API_TOKEN_ENV",
    "CMDatasource",
    "CMDatasourceList",
    "CMClientError",
    "ContextManagerClient",
    "DEFAULT_CM_BASE_URL",
    "resolve_cm_base_url",
]

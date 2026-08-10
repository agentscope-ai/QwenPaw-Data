"""HTTP client for the DataBridge semantic-config editing APIs.

Complements :mod:`datapaw.host.core.cm_client` (read-only discovery) with the
authenticated CRUD surface under ``/api/semantic-config``: datasources,
semantic objects, Excel import, and weave-task management.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

import httpx

from datapaw.host.core.cm_client import (
    API_TOKEN_ENV,
    CLIENT_API_TOKEN_ENV,
    DEFAULT_CM_BASE_URL,
    _trust_env_for_url,
    resolve_cm_base_url,
)

SEMANTIC_CONFIG_PREFIX = "/api/semantic-config"
DEFAULT_PAGE_SIZE = 20
DEFAULT_TIMEOUT_SECONDS = 30.0
_AUTH_HINT = (
    "hint: set DATAPAW_CLIENT_API_TOKEN (or DATAPAW_API_TOKEN) with the "
    "required scope"
)


class SemanticConfigClientError(RuntimeError):
    """Raised when a semantic-config API call fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


def _error_message(response: httpx.Response) -> str:
    """Extract the server error protocol {timestamp,status,error,message}."""

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("detail")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return f"semantic-config request returned HTTP {response.status_code}"


class SemanticConfigClient:
    """Minimal synchronous client for the semantic-config editing layer."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        api_token: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self.base_url = (base_url or resolve_cm_base_url()).strip().rstrip("/")
        if not self.base_url:
            self.base_url = DEFAULT_CM_BASE_URL
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

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        files: Any | None = None,
    ) -> Any:
        """Issue one request and return the decoded JSON body (or ``{}``)."""

        url = f"{self.base_url}{path}"
        headers = (
            {"Authorization": f"Bearer {self.api_token}"}
            if self.api_token
            else None
        )
        try:
            with httpx.Client(
                timeout=self.timeout,
                transport=self.transport,
                trust_env=_trust_env_for_url(self.base_url),
                headers=headers,
            ) as client:
                response = client.request(
                    method,
                    url,
                    params=self._clean_params(params),
                    json=json,
                    files=files,
                )
        except httpx.TimeoutException as exc:
            raise SemanticConfigClientError(
                f"semantic-config request timed out: {method} {path}",
            ) from exc
        except httpx.RequestError as exc:
            raise SemanticConfigClientError(
                f"semantic-config request failed: {method} {path}",
            ) from exc

        if not response.is_success:
            message = _error_message(response)
            if response.status_code in (401, 403):
                message = f"{message}\n{_AUTH_HINT}"
            raise SemanticConfigClientError(
                message,
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise SemanticConfigClientError(
                f"semantic-config response is not valid JSON: {method} {path}",
            ) from exc

    @staticmethod
    def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if params is None:
            return None
        cleaned = {key: value for key, value in params.items() if value is not None}
        return cleaned or None

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json: Any | None = None,
        files: Any | None = None,
    ) -> Any:
        return self.request("POST", path, json=json, files=files)

    def put(self, path: str, *, json: Any | None = None) -> Any:
        return self.request("PUT", path, json=json)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def list_page(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page: int = 1,
        size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Fetch one page of a ``Page{records,total,page,size}`` endpoint."""

        payload = self.get(path, params={**(params or {}), "page": page, "size": size})
        return self._validate_page(payload, expected_page=page)

    def list_all(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        size: int = 100,
    ) -> dict[str, Any]:
        """Fetch every page and return ``{"records": [...], "total": N}``."""

        records: list[Any] = []
        expected_total: int | None = None
        page = 1
        while True:
            payload = self.list_page(path, params=params, page=page, size=size)
            total = payload["total"]
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise SemanticConfigClientError(
                    "semantic-config response total changed during pagination",
                )
            records.extend(payload["records"])
            if len(records) > expected_total:
                raise SemanticConfigClientError(
                    "semantic-config response contains more records than total",
                )
            if len(records) == expected_total:
                return {"records": records, "total": expected_total}
            if not payload["records"]:
                raise SemanticConfigClientError(
                    "semantic-config response ended before total was reached",
                )
            page += 1

    @staticmethod
    def _validate_page(payload: Any, *, expected_page: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SemanticConfigClientError(
                "semantic-config list response is not an object",
            )
        records = payload.get("records")
        if not isinstance(records, list):
            raise SemanticConfigClientError(
                "semantic-config list response has invalid records",
            )
        total = payload.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise SemanticConfigClientError(
                "semantic-config list response has invalid total",
            )
        page = payload.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page != expected_page:
            raise SemanticConfigClientError(
                "semantic-config list response has an unexpected page",
            )
        return {"records": records, "total": total, "page": page}


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "SEMANTIC_CONFIG_PREFIX",
    "SemanticConfigClient",
    "SemanticConfigClientError",
]

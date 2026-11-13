"""HTTP client for Context Manager discovery APIs."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from qwenpaw_data.host.core.settings import CmSettings, get_settings

DATASOURCE_LIST_PATH = "/api/semantic-config/datasource"
DATASOURCE_METADATA_PATH = "/api/v1/cm/datasources"
DATASET_LIST_PATH = "/api/semantic-config/dataset-meta"
ACTIVE_DATASOURCE_PATH = "/api/datasources/active"
GLOBAL_MEMORIES_PATH = "/api/v1/ug/me/global-memories"
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 10.0
LIST_RETRY_ATTEMPTS = 2

logger = logging.getLogger(__name__)


REBUILD_LOCK_MESSAGE = "图谱正在重建，其他功能暂时不可用，请稍后刷新重试"


class CMClientError(RuntimeError):
    """Raised when CM cannot provide a valid datasource list."""


class CMLockedError(CMClientError):
    def __init__(self) -> None:
        super().__init__(REBUILD_LOCK_MESSAGE)


@dataclass(frozen=True)
class CMDatasource:
    """Datasource record returned by the CM discovery endpoint."""

    datasource_id: str
    datasource_name: str | None
    datasource_type: str | None
    config: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class CMDatasourceList:
    """Complete, unpaginated datasource result."""

    items: list[CMDatasource]
    total: int


@dataclass(frozen=True)
class CMDataset:
    """Dataset record returned by the CM dataset-meta list."""

    dataset_id: int
    dataset_name: str
    description: str | None


@dataclass(frozen=True)
class CMDatasetList:
    """Complete, unpaginated dataset result for one datasource."""

    items: list[CMDataset]
    total: int


def resolve_cm_base_url() -> str:
    """Resolve the CM origin from ``get_settings().cm`` (or the local default)."""
    return get_settings().cm.client_base_url()


async def fetch_datasource_type(
    access_token: str | None,
    *,
    datasource_id: str | None,
) -> str | None:
    if not access_token or not datasource_id:
        return None
    try:
        url = f"{resolve_cm_base_url()}{DATASOURCE_LIST_PATH}/{datasource_id}"
        async with httpx.AsyncClient(trust_env=_trust_env_for_url(url)) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
            return (payload.get("datasource_type") or "").strip() or None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


async def fetch_global_memories(
    access_token: str | None,
    *,
    chat_id: str,
) -> dict[str, Any] | None:
    """Read current user memories, logging and skipping CM request failures."""
    if not access_token:
        logger.error("Global memory skipped: chat=%s missing access token", chat_id)
        return None
    try:
        url = f"{resolve_cm_base_url()}{GLOBAL_MEMORIES_PATH}"
        async with httpx.AsyncClient(
            trust_env=_trust_env_for_url(url),
        ) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(
            "Global memory skipped: chat=%s error=%s: %s",
            chat_id,
            type(exc).__name__,
            exc,
        )
        return None


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


def _nullable_string(
    record: dict[str, Any], field_name: str, *, index: int
) -> str | None:
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

    config = record.get("config")
    if config is not None and not isinstance(config, dict):
        raise CMClientError(f"CM datasource response item {index} has invalid config")

    return CMDatasource(
        datasource_id=datasource_id,
        datasource_name=_nullable_string(record, "datasource_name", index=index),
        datasource_type=_nullable_string(record, "datasource_type", index=index),
        config=dict(config) if config is not None else None,
    )


def _parse_dataset(record: Any, *, index: int) -> CMDataset:
    if not isinstance(record, dict):
        raise CMClientError(f"CM dataset response item {index} is not an object")

    dataset_id = record.get("dataset_id")
    if isinstance(dataset_id, bool) or not isinstance(dataset_id, int):
        raise CMClientError(f"CM dataset response item {index} has invalid dataset_id")

    dataset_name = record.get("dataset_name")
    if not isinstance(dataset_name, str) or not dataset_name.strip():
        raise CMClientError(
            f"CM dataset response item {index} has invalid dataset_name",
        )

    return CMDataset(
        dataset_id=dataset_id,
        dataset_name=dataset_name.strip(),
        description=_nullable_string(record, "description", index=index),
    )


def _parse_datasource_metadata(record: Any, *, index: int) -> CMDatasource:
    if isinstance(record, dict) and "config" in record:
        raise CMClientError(f"CM datasource metadata item {index} unexpectedly contains config")
    return _parse_datasource(record, index=index)


class ContextManagerClient:
    """Minimal synchronous client used by the administrative CLI."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        access_token: str | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self.base_url = (base_url or resolve_cm_base_url()).strip().rstrip("/")
        if not self.base_url:
            self.base_url = CmSettings.DEFAULT_BASE_URL
        self.page_size = page_size
        self.timeout = timeout
        self.transport = transport
        self.access_token = access_token

    def list_datasources(self) -> CMDatasourceList:
        """Fetch every datasource page and validate the new CM contract."""
        items, total = self._collect_pages(
            path=DATASOURCE_LIST_PATH,
            parse=_parse_datasource,
            subject="datasource",
        )
        return CMDatasourceList(items=items, total=total)

    def list_datasource_metadata(self) -> CMDatasourceList:
        """List datasource names without fetching connection credentials."""
        items, total = self._collect_pages(
            path=DATASOURCE_METADATA_PATH,
            parse=_parse_datasource_metadata,
            subject="datasource",
        )
        return CMDatasourceList(items=items, total=total)

    def list_datasets(self, datasource_id: str) -> CMDatasetList:
        """Fetch every dataset page for one datasource."""
        ds = (datasource_id or "").strip()
        if not ds:
            raise ValueError("datasource_id is required")
        items, total = self._collect_pages(
            path=DATASET_LIST_PATH,
            parse=_parse_dataset,
            extra_params={"datasource_id": ds},
            subject="dataset",
        )
        return CMDatasetList(items=items, total=total)

    def get_active_datasource_id(self) -> str | None:
        """Return the user's DataBridge active datasource, or None if unset."""

        with httpx.Client(
            timeout=self.timeout,
            transport=self.transport,
            trust_env=_trust_env_for_url(self.base_url),
        ) as client:
            try:
                response = client.get(
                    f"{self.base_url}{ACTIVE_DATASOURCE_PATH}",
                    headers=self._headers(),
                )
            except httpx.RequestError:
                return None
            if not response.is_success:
                return None
            try:
                payload = response.json()
            except ValueError:
                return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("datasource_id")
        if not isinstance(value, str):
            return None
        return value.strip() or None

    def _headers(self) -> dict[str, str]:
        token = (self.access_token or "").strip()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def lookup_datasets(
        self,
        *,
        dataset_id=None,
        datasource_id=None,
        domain_id=None,
        name=None,
        page=1,
        size=20,
    ) -> dict:
        if not self.access_token:
            raise CMClientError("CM identity required")
        path = (
            f"{DATASET_LIST_PATH}/{dataset_id}"
            if dataset_id is not None
            else DATASET_LIST_PATH
        )
        params = (
            None
            if dataset_id is not None
            else {
                key: value
                for key, value in {
                    "datasource_id": datasource_id,
                    "domain_id": domain_id,
                    "dataset_name": name,
                    "page": page,
                    "size": min(size, 50),
                }.items()
                if value is not None
            }
        )
        with httpx.Client(
            timeout=self.timeout,
            transport=self.transport,
            trust_env=_trust_env_for_url(self.base_url),
        ) as client:
            response = client.get(
                f"{self.base_url}{path}", params=params, headers=self._headers()
            )
            response.raise_for_status()
            value = response.json()
        if not isinstance(value, dict):
            raise CMClientError("Invalid dataset response")
        if dataset_id is None and (
            not isinstance(value.get("records"), list)
            or not isinstance(value.get("total"), int)
            or len(value["records"]) > size
        ):
            raise CMClientError("Invalid dataset page")
        return value

    def _collect_pages(
        self,
        *,
        path: str,
        parse: Any,
        extra_params: dict[str, Any] | None = None,
        subject: str,
    ) -> tuple[list[Any], int]:
        items: list[Any] = []
        expected_total: int | None = None
        page = 1
        with httpx.Client(
            timeout=self.timeout,
            transport=self.transport,
            trust_env=_trust_env_for_url(self.base_url),
        ) as client:
            while True:
                payload = self._get_page(
                    client,
                    page,
                    path=path,
                    extra_params=extra_params,
                    subject=subject,
                )
                records = payload.get("records")
                if not isinstance(records, list):
                    raise CMClientError(f"CM {subject} response has invalid records")

                total = _required_int(payload, "total", minimum=0)
                response_page = _required_int(payload, "page", minimum=1)
                _required_int(payload, "size", minimum=1)
                if response_page != page:
                    raise CMClientError(f"CM {subject} response has an unexpected page")

                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    raise CMClientError(
                        f"CM {subject} response total changed during pagination",
                    )

                start_index = len(items)
                items.extend(
                    parse(record, index=start_index + offset)
                    for offset, record in enumerate(records)
                )

                if len(items) > expected_total:
                    raise CMClientError(
                        f"CM {subject} response contains more records than total",
                    )
                if len(items) == expected_total:
                    return items, expected_total
                if not records:
                    raise CMClientError(
                        f"CM {subject} response ended before total was reached",
                    )
                page += 1

    def _get_page(
        self,
        client: httpx.Client,
        page: int,
        *,
        path: str = DATASOURCE_LIST_PATH,
        extra_params: dict[str, Any] | None = None,
        subject: str = "datasource",
    ) -> dict[str, Any]:
        last_error: CMClientError | None = None
        params: dict[str, Any] = {"page": page, "size": self.page_size}
        if extra_params:
            params.update(extra_params)
        for _attempt in range(LIST_RETRY_ATTEMPTS):
            try:
                response = client.get(
                    f"{self.base_url}{path}",
                    params=params,
                    headers=self._headers(),
                )
            except httpx.TimeoutException as exc:
                last_error = CMClientError(
                    f"CM {subject} request timed out on page {page}",
                )
                last_error.__cause__ = exc
                continue
            except httpx.RequestError as exc:
                last_error = CMClientError(
                    f"CM {subject} request failed on page {page}",
                )
                last_error.__cause__ = exc
                continue

            if response.status_code == 423:
                raise CMLockedError()
            if not response.is_success:
                raise CMClientError(
                    f"CM {subject} request returned HTTP {response.status_code} on page {page}",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise CMClientError(f"CM {subject} response is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise CMClientError(f"CM {subject} response is not an object")
            return payload
        assert last_error is not None
        raise last_error


__all__ = [
    "CMDataset",
    "CMDatasetList",
    "CMDatasource",
    "CMDatasourceList",
    "CMClientError",
    "CMLockedError",
    "ContextManagerClient",
    "fetch_datasource_type",
    "fetch_global_memories",
    "resolve_cm_base_url",
]

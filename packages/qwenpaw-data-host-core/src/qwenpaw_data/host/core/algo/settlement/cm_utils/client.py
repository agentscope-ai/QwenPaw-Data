# -*- coding: utf-8 -*-
"""Host-side CM REST client for settlement (domains / confirmer / feedback_card)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from qwenpaw_data.host.core.cm_client import (
    _trust_env_for_url,
    resolve_cm_base_url,
)

logger = logging.getLogger(__name__)

CM_API_PREFIX = "/api/v1/cm"
FEEDBACK_CARD_PATH = "/api/v1/semantic/feedback_card"
_DEFAULT_TIMEOUT_SECONDS = 180.0
_FEEDBACK_TIMEOUT_SECONDS = 30.0


def _format_cm_error(exc: BaseException) -> str:
    detail = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def _meta_datasource_id(metadata: Any) -> str | None:
    if isinstance(metadata, dict):
        raw = metadata.get("datasource_id")
    else:
        text = str(metadata or "").strip()
        if not text or text == "{}":
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        raw = payload.get("datasource_id")
    value = str(raw or "").strip()
    return value or None


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class SettlementCmClient:
    """CM REST client: list/search/get + feedback_card writeback."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        datasource_id: str | None = None,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/") or None
        self.access_token = (access_token or "").strip() or None
        self.datasource_id = (datasource_id or "").strip() or None
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return (self._base_url or resolve_cm_base_url()).strip().rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _resolve_ds(self, kwargs: dict[str, Any]) -> str | None:
        return _meta_datasource_id(kwargs.get("metadata")) or self.datasource_id

    def _with_ds(self, params: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in params.items() if v is not None and v != ""}
        ds = self._resolve_ds(kwargs)
        if ds:
            out.setdefault("datasource_id", ds)
        return out

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(
            timeout=timeout or self.timeout,
            trust_env=_trust_env_for_url(self.base_url),
        ) as client:
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
            )
        if not response.is_success:
            detail = response.text[:500] if response.text else f"HTTP {response.status_code}"
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("detail") is not None:
                    raw = body["detail"]
                    detail = (
                        json.dumps(raw, ensure_ascii=False)
                        if isinstance(raw, (dict, list))
                        else str(raw)
                    )
            except Exception:
                pass
            raise RuntimeError(f"{path} [{response.status_code}]: {detail}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # --- capability endpoints -------------------------------------------------

    async def list_domains(self, kwargs: dict[str, Any] | None = None) -> Any:
        return await self._request(
            "GET",
            f"{CM_API_PREFIX}/domains",
            params=self._with_ds({}, kwargs or {}),
        )

    async def search_metrics(self, kwargs: dict[str, Any] | None = None) -> Any:
        kwargs = kwargs or {}
        return await self._request(
            "GET",
            f"{CM_API_PREFIX}/search-metrics",
            params=self._with_ds(
                {
                    "query": kwargs.get("query", ""),
                    "domain": kwargs.get("domain"),
                    "k": kwargs.get("k", 10),
                },
                kwargs,
            ),
        )

    async def get_dimension(self, kwargs: dict[str, Any] | None = None) -> Any:
        kwargs = kwargs or {}
        return await self._request(
            "GET",
            f"{CM_API_PREFIX}/dimensions",
            params=self._with_ds(
                {
                    "name": kwargs.get("name", ""),
                    "domain": kwargs.get("domain"),
                },
                kwargs,
            ),
        )

    async def get_dataset(self, kwargs: dict[str, Any] | None = None) -> Any:
        kwargs = kwargs or {}
        return await self._request(
            "GET",
            f"{CM_API_PREFIX}/datasets",
            params=self._with_ds(
                {
                    "name": kwargs.get("name", ""),
                    "domain": kwargs.get("domain"),
                },
                kwargs,
            ),
        )

    async def search_context(self, kwargs: dict[str, Any] | None = None) -> Any:
        kwargs = kwargs or {}
        body: dict[str, Any] = {
            "query": kwargs.get("query", ""),
            "stream": False,
        }
        ds = self._resolve_ds(kwargs)
        if ds:
            body["datasource_id"] = ds
        domain = kwargs.get("domain")
        if domain:
            body["scope"] = {"domain": domain}
        return await self._request(
            "POST", f"{CM_API_PREFIX}/search_context", json_body=body
        )

    async def feedback_card(
        self,
        kwargs: dict[str, Any] | None = None,
        *,
        mode: str,
    ) -> Any:
        """POST feedback_card. ``mode=test`` dry-runs; ``mode=confirm`` writes."""
        mode_norm = str(mode).strip().lower()
        if mode_norm not in ("confirm", "test"):
            raise ValueError(
                f"feedback_card mode must be 'confirm' or 'test', got {mode!r}"
            )
        return await self._request(
            "POST",
            FEEDBACK_CARD_PATH,
            params={"mode": mode_norm},
            json_body=kwargs or {},
            timeout=_FEEDBACK_TIMEOUT_SECONDS,
        )

    # --- call facade (legacy tool-name dispatch) ------------------------------

    async def call(
        self,
        tool_name: str,
        kwargs: dict[str, Any] | None = None,
        *,
        max_len: int = 2000,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Call a CM capability by legacy tool name; return {tool, kwargs, status, result}."""
        kwargs = dict(kwargs or {})
        record: dict[str, Any] = {
            "tool": tool_name,
            "kwargs": kwargs,
            "status": "pending",
            "result": "",
        }
        if tool_name == "feedback_card":
            try:
                if mode is None:
                    raise ValueError(
                        "mode is required for feedback_card ('confirm' or 'test')"
                    )
                data = await self.feedback_card(kwargs, mode=mode)
                text = data if isinstance(data, str) else _dumps(data)
                record["status"] = "ok"
                record["result"] = text[:max_len] if max_len else text
            except Exception as e:
                detail = _format_cm_error(e)
                record["status"] = f"error: {detail}"
                record["result"] = detail
                logger.debug("CM REST %s failed", tool_name, exc_info=True)
            return record

        handlers = {
            "list_domains": self.list_domains,
            "search_metrics": self.search_metrics,
            "get_dimension": self.get_dimension,
            "get_dataset": self.get_dataset,
            "search_context": self.search_context,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            record["status"] = f"error: unsupported CM REST capability: {tool_name}"
            record["result"] = record["status"]
            return record
        try:
            data = await handler(kwargs)
            text = data if isinstance(data, str) else _dumps(data)
            record["status"] = "ok"
            record["result"] = text[:max_len] if max_len else text
        except Exception as e:
            detail = _format_cm_error(e)
            record["status"] = f"error: {detail}"
            record["result"] = detail
            logger.debug("CM REST %s failed", tool_name, exc_info=True)
        return record

    async def list_domain_names(self) -> list[str]:
        record = await self.call("list_domains", {}, max_len=0)
        if not str(record.get("status") or "").startswith("ok"):
            return []
        try:
            domains = json.loads(record.get("result") or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(domains, list):
            return []
        return [
            d["name"]
            for d in domains
            if isinstance(d, dict) and d.get("name")
        ]

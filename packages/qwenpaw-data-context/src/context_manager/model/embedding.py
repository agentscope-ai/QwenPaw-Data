"""Provide bounded batch embedding with a configured SDK client."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import random
import secrets
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

import httpx
from openai import AsyncOpenAI

from ..settings import get_settings
from ..utils.validation import (
    optional_trimmed_text,
    require_nonempty_text,
    require_trimmed_text,
)
from ..utils.vector import require_vector_batch
from .embedding_cache import embedding_cache
from .llm import LlmError
from .usage import ModelUsageTracker

log = logging.getLogger(__name__)
_CACHE_IDENTITY_KEY = secrets.token_bytes(32)


@runtime_checkable
class TextEmbedder(Protocol):
    """Define a provider-neutral embedding client and its lifecycle."""

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimensions: int | None = None,
        model: str | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Return one embedding vector per input text in input order."""
        ...

    async def aclose(self) -> None:
        """Release transport resources owned by the embedder."""
        ...


class OpenAIEmbedCaller:
    """Embed text using fixed connection settings and configurable batch limits."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        batch_size: int | None = None,
        max_concurrency: int | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        client: Any | None = None,
        usage_tracker: ModelUsageTracker | None = None,
        run_sync: Callable[..., Awaitable[Any]] | None = None,
        cache_namespace: tuple[str, str, str] | None = None,
    ) -> None:
        """Capture model defaults and optionally borrow an existing SDK client."""
        self._usage_tracker = usage_tracker
        self._model = optional_trimmed_text(model, "model")
        self._base_url = optional_trimmed_text(base_url, "base_url")
        self._settings = get_settings().embedding
        if max_retries is None:
            max_retries = self._settings.sdk_max_retries
        batch_size = self._settings.batch_size if batch_size is None else batch_size
        max_concurrency = (
            self._settings.max_concurrency
            if max_concurrency is None
            else max_concurrency
        )
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if dimensions is not None and (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or dimensions <= 0
        ):
            raise ValueError("dimensions must be a positive integer")
        for name, value in (
            ("batch_size", batch_size),
            ("max_concurrency", max_concurrency),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if cache_namespace is not None and (
            not isinstance(cache_namespace, tuple)
            or len(cache_namespace) != 3
            or any(
                not isinstance(value, str) or not value.strip() or value != value.strip()
                for value in cache_namespace
            )
        ):
            raise ValueError("cache_namespace must be a trusted tenant/workspace/user tuple")
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency
        self._run_sync = run_sync or asyncio.to_thread
        self._closed = False
        self._owns_client = client is None
        if client is not None:
            self._client = client
            # Borrowed clients have opaque credentials, so their cache stays local.
            self._cache_identity = secrets.token_hex(32)
            return
        try:
            api_key = require_nonempty_text(api_key, "api_key")
        except (TypeError, ValueError) as exc:
            raise ValueError("api_key is required when client is not provided") from exc
        # A process-local HMAC keeps credentials and raw attribution out of cache
        # keys. Only callers with the same trusted tenant/workspace/user scope and
        # credential share vectors; credential rotation changes the identity.
        identity = json.dumps(
            [
                cache_namespace if cache_namespace is not None else secrets.token_hex(32),
                api_key,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._cache_identity = hmac.new(
            _CACHE_IDENTITY_KEY, identity, "sha256"
        ).hexdigest()
        options: dict[str, object] = {
            "api_key": api_key,
            "max_retries": max_retries,
        }
        timeout = (
            get_settings().model.timeout if timeout_seconds is None else timeout_seconds
        )
        if timeout is not None and timeout > 0:
            options["timeout"] = timeout
        if self._base_url is not None:
            options["base_url"] = self._base_url
        if get_settings().model.ignore_proxy:
            options["http_client"] = httpx.AsyncClient(trust_env=False)
        self._client = AsyncOpenAI(**options)

    @property
    def dimensions(self) -> int | None:
        """Return the configured default vector dimension."""
        return self._dimensions

    @property
    def config_snapshot(self) -> dict[str, object]:
        """Return a fresh credential-free snapshot of embedding defaults."""
        return {
            "model": self._model,
            "base_url": self._base_url,
            "dim": self._dimensions,
        }

    def _require_open(self) -> None:
        if self._closed:
            raise LlmError("model caller is closed")

    @staticmethod
    def _resolve_model(
        override: str | None,
        configured: str | None,
        purpose: str,
    ) -> str:
        """Resolve and validate one per-call or caller-level model name."""
        resolved = override if override is not None else configured
        try:
            return require_trimmed_text(resolved, f"{purpose} model")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{purpose} model must be configured and trimmed") from exc

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimensions: int | None = None,
        model: str | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Embed bounded concurrent batches and return all vectors in input order."""
        self._require_open()
        dimensions = self._dimensions if dimensions is None else dimensions
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)):
            raise TypeError("texts must be a sequence of strings")
        if not texts:
            raise ValueError("texts must not be empty")
        try:
            for text in texts:
                require_nonempty_text(text, "embedding text")
        except (TypeError, ValueError) as exc:
            raise ValueError("texts must contain only non-empty strings") from exc
        if dimensions is not None and (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or dimensions <= 0
        ):
            raise ValueError("dimensions must be a positive integer")
        values = tuple(text[: self._settings.max_input_chars] for text in texts)
        selected_model = self._resolve_model(model, self._model, "embedding")
        if self._settings.cache_ttl_seconds <= 0:
            return await self._embed_uncached(
                values, model=selected_model, dimensions=dimensions
            )

        endpoint = self._base_url or str(getattr(self._client, "base_url", ""))
        keys = tuple(
            (self._cache_identity, endpoint, selected_model, dimensions, text)
            for text in values
        )
        resolved = {}
        missing = {}
        for key in keys:
            vector = embedding_cache.get(
                key, ttl_seconds=self._settings.cache_ttl_seconds
            )
            if vector is None:
                missing.setdefault(key, None)
            else:
                resolved[key] = vector
        if missing:
            vectors = await self._embed_uncached(
                tuple(key[-1] for key in missing),
                model=selected_model,
                dimensions=dimensions,
            )
            resolved.update(zip(missing, vectors, strict=True))
        result = tuple(resolved[key] for key in keys)
        if len({len(vector) for vector in result}) != 1:
            raise LlmError("embedding response has unexpected dimensions")
        for key in missing:
            embedding_cache.put(
                key, resolved[key], max_entries=self._settings.cache_max_entries
            )
        return result

    async def _embed_uncached(
        self,
        values: Sequence[str],
        *,
        model: str,
        dimensions: int | None,
    ) -> tuple[tuple[float, ...], ...]:
        """Run bounded workers over cache misses and restore input order."""
        batch_size = self._batch_size
        starts = iter(range(0, len(values), batch_size))
        result: list[tuple[float, ...]] = [()] * len(values)
        expected_dimensions = dimensions

        async def run() -> None:
            nonlocal expected_dimensions
            for start in starts:
                vectors = await self._embed_batch(
                    values[start : start + batch_size],
                    model=model,
                    dimensions=dimensions,
                )
                if expected_dimensions is None:
                    expected_dimensions = len(vectors[0])
                if len(vectors[0]) != expected_dimensions:
                    raise LlmError("embedding response has unexpected dimensions")
                result[start : start + len(vectors)] = vectors

        worker_count = min(
            self._max_concurrency,
            (len(values) + batch_size - 1) // batch_size,
        )
        workers = [asyncio.create_task(run()) for _ in range(worker_count)]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        return tuple(result)

    async def _embed_batch(
        self,
        texts: Sequence[str],
        *,
        model: str,
        dimensions: int | None,
    ) -> tuple[tuple[float, ...], ...]:
        """Validate one provider response and restore its local input order."""
        options: dict[str, object] = {
            "model": model,
            "input": list(texts),
        }
        if dimensions is not None:
            options["dimensions"] = dimensions
        for attempt in range(self._settings.rate_limit_retries + 1):
            tracking = (
                self._usage_tracker.track_call(model_type="embedding", model_id=model)
                if self._usage_tracker is not None
                else nullcontext()
            )
            with tracking as record_response:
                try:
                    # Read the body asynchronously; defer SDK JSON/base64 decoding.
                    response = await self._client.embeddings.with_raw_response.create(
                        **options
                    )
                except Exception as exc:  # noqa: BLE001 - provider exceptions vary.
                    message = str(exc).lower()
                    is_rate_limit = (
                        getattr(exc, "status_code", None) == 429
                        or "429" in message
                        or "rate" in message
                        or "limit_burst_rate" in message
                    )
                    if (
                        not is_rate_limit
                        or attempt == self._settings.rate_limit_retries
                    ):
                        raise LlmError("embedding request failed") from None
                    delay = 0.5 * 2**attempt + random.uniform(0, 0.25)
                    log.warning(
                        "Embedding rate limited; retry %d/%d in %.2fs",
                        attempt + 1,
                        self._settings.rate_limit_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                return await self._run_sync(
                    self._process_response,
                    response,
                    len(texts),
                    record_response,
                )

    def _process_response(
        self,
        raw_response: Any,
        expected_count: int,
        record_response: Callable[[object], bool] | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Decode and validate a fully read response in one synchronous step."""
        try:
            response = raw_response.parse()
        except Exception:  # noqa: BLE001 - SDK parsers expose provider errors.
            raise LlmError("embedding request failed") from None
        if record_response is not None:
            record_response(response)
        return self._validate_response(response, expected_count)

    @staticmethod
    def _validate_response(
        response: Any,
        expected_count: int,
    ) -> tuple[tuple[float, ...], ...]:
        """Restore provider order and validate every vector before publication."""
        data = getattr(response, "data", None)
        if not isinstance(data, Sequence) or len(data) != expected_count:
            raise LlmError("embedding response count does not match input count")
        ordered = sorted(data, key=lambda item: getattr(item, "index", -1))
        if [getattr(item, "index", None) for item in ordered] != list(
            range(expected_count)
        ):
            raise LlmError("embedding response indexes are invalid")

        try:
            return require_vector_batch(
                tuple(getattr(item, "embedding", None) for item in ordered),
                "embedding response",
                expected_count=expected_count,
            )
        except (TypeError, ValueError) as exc:
            raise LlmError("embedding response contains invalid vectors") from exc

    async def aclose(self) -> None:
        """Close only an SDK client created by this caller."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> Self:
        """Return this caller for one application-managed async lifetime."""
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close resources when leaving the async context manager."""
        await self.aclose()


__all__ = ["OpenAIEmbedCaller", "TextEmbedder"]

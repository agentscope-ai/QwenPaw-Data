"""Manage user model configuration and operation-scoped clients."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field
import hashlib
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from httpx import URL, InvalidURL

from context_manager.runtime_model_binding import llm_binding

from ..settings import get_settings
from .repository import ModelChannel, ModelConfigError, ModelConfigRepository
from .llm import ChatMessage, LlmCaller, LlmError, OpenAILlmCaller
from .embedding import TextEmbedder, OpenAIEmbedCaller
from .usage import ModelUsageEvent, ModelUsageTracker, bind_usage_context


def embedding_fingerprint(model: str, base_url: str, dim: int | None) -> str:
    """Identify a vector configuration without credentials."""
    raw = f"{model.strip()}|{base_url.strip()}|{int(dim or 0)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """Carry resolved client options without exposing credentials in repr."""

    provider_id: str | None
    model: str
    base_url: str
    api_key: str = field(repr=False)
    dim: int | None = None

    def embedding_space(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "dim": self.dim,
            "fingerprint": embedding_fingerprint(self.model, self.base_url, self.dim),
        }


class EmbeddingSpaceConflict(ModelConfigError):
    """Report a configuration conflict detected when saving or using a model."""

    def __init__(self, *, workspace: dict, mine: dict) -> None:
        super().__init__("Embedding settings must match the workspace convention")
        self.details = {"workspace": workspace, "mine": mine}


class ModelService:
    """Manage model settings and lend clients for each complete operation.

    Read, save and clear selections through ModelConfigRepository, enforce workspace
    rules when saving, and return masked settings or connection-test results.
    """

    def __init__(
        self,
        repository: ModelConfigRepository,
        *,
        llm_factory: Callable[..., LlmCaller] | None = None,
        embed_factory: Callable[..., TextEmbedder] | None = None,
        enqueue_usage: Callable[[ModelUsageEvent], bool] | None = None,
    ) -> None:
        self.repository = repository
        self._llm_factory = llm_factory or OpenAILlmCaller
        self._embed_factory = embed_factory or OpenAIEmbedCaller
        self._enqueue_usage = enqueue_usage

    def create_usage_tracker(self, config: ResolvedModel) -> ModelUsageTracker | None:
        """Bind metering to this model user's identity and resolved Provider."""
        if self._enqueue_usage is None or config.provider_id is None:
            return None
        return ModelUsageTracker(
            self._enqueue_usage,
            tenant_id=self.repository.tenant_id,
            workspace_id=self.repository.workspace_id,
            user_id=self.repository.user_id,
            provider_id=config.provider_id,
        )

    @asynccontextmanager
    async def llm(
        self, config: ResolvedModel | None = None
    ) -> AsyncIterator[LlmCaller]:
        """Resolve the current selection and close the client after use."""
        if config is None:
            config = await asyncio.to_thread(self.resolve_for_use, "llm")
        caller = self._llm_factory(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            usage_tracker=self.create_usage_tracker(config),
        )
        async with aclosing(caller):
            yield caller

    @asynccontextmanager
    async def embedder(
        self, config: ResolvedModel | None = None
    ) -> AsyncIterator[TextEmbedder]:
        """Resolve the current selection and close the client after use."""
        if config is None:
            config = await asyncio.to_thread(self.resolve_for_use, "embedding")
        caller = self._embed_factory(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            dimensions=config.dim,
            usage_tracker=self.create_usage_tracker(config),
            cache_namespace=(
                self.repository.tenant_id,
                self.repository.workspace_id,
                self.repository.user_id,
            ),
        )
        async with aclosing(caller):
            yield caller

    @asynccontextmanager
    async def open(self) -> AsyncIterator[tuple[LlmCaller, TextEmbedder]]:
        """Keep both model selections fixed until the operation ends."""
        llm_config, embed_config = await asyncio.to_thread(self.resolve_pair)
        async with AsyncExitStack() as stack:
            llm = await stack.enter_async_context(self.llm(llm_config))
            embedder = await stack.enter_async_context(self.embedder(embed_config))
            yield llm, embedder

    @property
    def ready(self) -> bool:
        """Report whether both model selections can be resolved."""
        try:
            for channel in ("llm", "embedding"):
                self.resolve_for_use(channel)
            return True
        except ModelConfigError:
            return False

    def resolve_for_use(self, channel: ModelChannel) -> ResolvedModel:
        """Resolve one channel and enforce its workspace vector convention."""
        config = self.resolve(channel)
        if channel == "embedding":
            space = self.read_embedding_space()
            mine = config.embedding_space()
            if space is not None and space["fingerprint"] != mine["fingerprint"]:
                raise EmbeddingSpaceConflict(workspace=space, mine=mine)
        return config

    def resolve_pair(self) -> tuple[ResolvedModel, ResolvedModel]:
        """Resolve both channels for one operation."""
        return self.resolve_for_use("llm"), self.resolve_for_use("embedding")

    def resolve(
        self,
        channel: ModelChannel,
        *,
        selection: dict | None = None,
    ) -> ResolvedModel:
        """Resolve saved credentials; candidate writes always require a Provider."""
        if channel not in {"llm", "embedding"}:
            raise ValueError("unknown model channel")
        # Request credentials apply only to LLM usage, never candidates or embeddings.
        if channel == "llm" and selection is None:
            binding = llm_binding.get()
            if binding is not None:
                return ResolvedModel(
                    None, binding["model"], binding["base_url"], binding["api_key"]
                )
        row = self.repository.read_model(channel, selection=selection)
        if row is None:
            raise ModelConfigError(
                "model configuration user is unavailable in this tenant"
            )
        selected = (
            selection
            if selection is not None
            else {
                "provider_id": row["selected_provider"],
                "model": row["model"],
                "dim": row["embed_dim"],
            }
        )
        provider_id, model = selected.get("provider_id"), selected.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ModelConfigError("model is required")
        if model != model.strip():
            raise ModelConfigError("model must be trimmed")
        legacy = row["legacy"]
        source = "Legacy CM" if legacy else "Host Provider"
        if not legacy and (not isinstance(provider_id, str) or not provider_id.strip()):
            raise ModelConfigError("Host provider_id is required")
        if not legacy and row["provider_id"] is None:
            raise ModelConfigError(
                "configure this Provider in Host before selecting it"
            )
        dim = selected.get("dim") if channel == "embedding" else None
        if legacy and channel == "embedding" and dim is None:
            raise ModelConfigError("legacy embedding dimension is required")
        if dim is not None and (
            isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0
        ):
            raise ModelConfigError("embedding dimension must be a positive integer")
        base_url = str(row["base_url"] or "").strip()
        try:
            # Reject malformed bracketed hosts that the SDK parser may tolerate,
            # then use its parser for transport-compatible scheme/port checks.
            urlparse(base_url)
            parsed = URL(base_url)
        except (InvalidURL, ValueError):
            raise ModelConfigError(f"{source} endpoint is invalid") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.userinfo
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ModelConfigError(
                f"{source} endpoint must be an absolute HTTP(S) URL without credentials and with a valid port"
            )
        encrypted_key = row["api_key_enc"]
        if not encrypted_key:
            raise ModelConfigError(f"{source} credential is missing")
        return ResolvedModel(
            None if legacy else provider_id,
            model,
            base_url,
            self._decrypt(str(encrypted_key), source=source),
            dim,
        )

    def read_embedding_space(self) -> dict | None:
        """Describe the workspace convention with its derived fingerprint."""
        space = self.repository.read_embedding_space()
        if space is not None:
            space["fingerprint"] = embedding_fingerprint(**space)
        return space

    def read(self) -> dict:
        """Return masked choices, readiness and workspace configuration alignment."""
        result = {
            "configured": True,
            "key_status": "ok",
            "seeded": False,
            "missing_fields": [],
            "errors": {},
        }
        embedding = None
        selections = self.repository.read_selections()
        for channel, selection in selections.items():
            result[channel] = {**selection, "api_key": ""}
            missing = [
                f"{channel}.{name}" for name in ("model",) if not selection[name]
            ]
            result["missing_fields"].extend(missing)
            try:
                resolved = self.resolve_for_use(channel)
                result[channel] = self._masked(resolved, channel)
                if channel == "embedding":
                    embedding = resolved.embedding_space()
            except EmbeddingSpaceConflict as exc:
                embedding = exc.details["mine"]
                result["configured"] = False
                result["errors"][channel] = str(exc)
            except ModelConfigError as exc:
                result.update(configured=False, key_status="undecryptable")
                result["errors"][channel] = str(exc)
        space = self.read_embedding_space()
        result["embedding_space"] = {
            "adopted": space is not None,
            "aligned": (
                embedding["fingerprint"] == space["fingerprint"]
                if space is not None and embedding is not None
                else None
            ),
            **(space or {}),
        }
        return result

    def save(
        self,
        channel: ModelChannel,
        update: dict,
        *,
        can_override_space: bool = False,
    ) -> tuple[dict, bool]:
        """Save the choice, Provider reference and workspace convention atomically."""
        previous, config = self._candidate(channel, update)
        document = self._masked(config, channel)
        changed = any(previous.get(name) != document.get(name) for name in previous)
        with self.repository.transaction() as connection:
            self.repository.save_selection(connection, channel, config)
            if channel == "embedding":
                current = self.repository.adopt_embedding_space(connection, config)
                current["fingerprint"] = embedding_fingerprint(**current)
                mine = config.embedding_space()
                if current["fingerprint"] != mine["fingerprint"]:
                    if not can_override_space:
                        raise EmbeddingSpaceConflict(workspace=current, mine=mine)
                    self.repository.update_embedding_space(connection, config)
                    changed = True
        return document, changed

    def delete(self) -> None:
        """Clear personal choices, preserving Host Providers and workspace spaces."""
        self.repository.delete()

    async def probe(self, channel: ModelChannel, update: dict) -> dict:
        """Probe a candidate using a temporary client, without saving it."""
        _, config = await asyncio.to_thread(self._candidate, channel, update)
        clients = self.llm(config) if channel == "llm" else self.embedder(config)
        try:
            with bind_usage_context(probe=True):
                async with clients as caller:
                    if channel == "llm":
                        await caller.complete(
                            (ChatMessage("user", "Reply with pong."),),
                            temperature=0,
                            max_tokens=5,
                        )
                        return {
                            "success": True,
                            "message": f"Connected. Model: {config.model}",
                        }
                    vectors = await caller.embed(("dimension probe",))
                    dim = len(vectors[0])
                    return {
                        "success": True,
                        "message": f"Connected. Dimension: {dim}",
                        "detected_dim": dim,
                    }
        except (LlmError, TypeError, ValueError, IndexError):
            return {"success": False, "message": "Model connection failed"}

    def _candidate(
        self, channel: ModelChannel, update: dict
    ) -> tuple[dict, ResolvedModel]:
        """Read the previous choice once and resolve the proposed partial update."""
        current = self.repository.read_selections()[channel]
        if set(update) - set(current):
            raise ValueError(
                "model configuration accepts only Provider/model selection fields"
            )
        selected = {
            **current,
            **{name: value for name, value in update.items() if value is not None},
        }
        for name in ("provider_id", "model", "base_url"):
            if selected.get(name) is not None:
                if not isinstance(selected[name], str):
                    raise ValueError(f"{name} must be text")
                selected[name] = selected[name].strip()
        supplied_url = update.get("base_url")
        if supplied_url:
            parsed = urlparse(supplied_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "base_url must be an absolute HTTP(S) URL without credentials"
                )
        config = self.resolve(channel, selection=selected)
        if supplied_url and supplied_url.rstrip("/") != config.base_url.rstrip("/"):
            raise ValueError("base_url must match the selected Host Provider")
        return current, config

    @staticmethod
    def _masked(config: ResolvedModel, channel: ModelChannel) -> dict:
        result = {
            "provider_id": config.provider_id,
            "model": config.model,
            "base_url": config.base_url,
            "api_key": "****",
        }
        if channel == "embedding":
            result["dim"] = config.dim
        return result

    @staticmethod
    def _decrypt(value: str, *, source: str = "Host Provider") -> str:
        """Decode shared ENC/Fernet using the installation's persisted master key."""
        try:
            key = base64.urlsafe_b64encode(get_settings().model.master_key())
        except RuntimeError as exc:
            raise ModelConfigError(str(exc)) from None
        try:
            if not value.startswith("ENC:"):
                raise ValueError("encrypted Provider credential required")
            result = Fernet(key).decrypt(value[4:].encode("ascii")).decode("utf-8")
            if not result.strip():
                raise ValueError("empty Provider credential")
            return result
        except (ValueError, InvalidToken, UnicodeError):
            raise ModelConfigError(
                f"{source} credential could not be decrypted"
            ) from None

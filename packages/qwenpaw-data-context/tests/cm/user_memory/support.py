"""Provide deterministic provider doubles for User algorithm tests."""

from __future__ import annotations

from collections.abc import Sequence

from context_manager.model import ChatMessage
from context_manager.user import IngestAdapter

from cm.infrastructure.execution.support import executor


class FakeLlmCaller:
    """Return queued completions and configured embedding vectors."""

    def __init__(
        self,
        completions: Sequence[str] = (),
        embeddings: Sequence[Sequence[float]] = (),
    ) -> None:
        """Initialize provider outputs and request logs."""
        self.completions = list(completions)
        self.embeddings = tuple(tuple(vector) for vector in embeddings)
        self.complete_calls: list[
            tuple[tuple[ChatMessage, ...], dict[str, object]]
        ] = []
        self.embed_calls: list[tuple[tuple[str, ...], int | None]] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        model: str | None = None,
        json_mode: bool = False,
        enable_reasoning: bool | None = None,
    ) -> str:
        """Return the next exact completion and record caller options."""
        self.complete_calls.append(
            (
                tuple(messages),
                {
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "model": model,
                    "json_mode": json_mode,
                    "enable_reasoning": enable_reasoning,
                },
            )
        )
        if not self.completions:
            raise AssertionError("unexpected completion request")
        return self.completions.pop(0)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimensions: int | None = None,
        model: str | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Return configured vectors in input order."""
        del model
        self.embed_calls.append((tuple(texts), dimensions))
        if len(self.embeddings) != len(texts):
            raise AssertionError("embedding fixture count mismatch")
        return self.embeddings

    async def aclose(self) -> None:
        """Provide the complete LlmCaller lifecycle contract."""


__all__ = ["FakeLlmCaller"]


def history_adapter(runtime, *, loader=None):
    """Compose the history workflow against a test Runtime's public resources."""
    return IngestAdapter(
        lambda context: runtime.graph_store(runtime.issue_graph_read_scope(context)),
        lambda context: runtime.models(context),
        executor=executor,
        loader=loader,
    )

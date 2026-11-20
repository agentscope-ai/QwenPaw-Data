"""Expose configured model clients and per-user model access."""

from .llm import ChatMessage, LlmCaller, LlmError, OpenAILlmCaller
from .embedding import OpenAIEmbedCaller, TextEmbedder
from .repository import ModelChannel, ModelConfigError, ModelConfigRepository
from .service import EmbeddingSpaceConflict, ModelService, ResolvedModel
from .usage import ModelUsageTracker, bind_usage_context, current_usage_context

__all__ = [
    "ChatMessage",
    "EmbeddingSpaceConflict",
    "LlmCaller",
    "LlmError",
    "ModelChannel",
    "ModelConfigError",
    "ModelService",
    "ModelConfigRepository",
    "ModelUsageTracker",
    "OpenAIEmbedCaller",
    "OpenAILlmCaller",
    "ResolvedModel",
    "TextEmbedder",
    "bind_usage_context",
    "current_usage_context",
]

# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from agentscope.credential import DashScopeCredential, OpenAICredential
from agentscope.formatter import (
    DashScopeChatFormatter,
    FormatterBase,
)
from agentscope.model import ChatModelBase, DashScopeChatModel, OpenAIChatModel
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext
from qwenpaw_data.host.core.domain.preference import ActiveModel
from qwenpaw_data.host.core.tracing.adapters import attach_generation_span
from qwenpaw_data.host.core.providers.builtins import provider_registry
from qwenpaw_data.host.core.providers.registry import BuiltinModel
from qwenpaw_data.host.core.providers.usage import attach_model_usage


def _field_default(cls: type[FormatterBase], name: str) -> list[str]:
    """Evaluate a formatter ``Field(default_factory=...)`` class default."""
    field = cls.model_fields[name]
    if field.default_factory is not None:
        return field.default_factory()
    return field.default


_DEFAULT_INPUT_TYPES = BuiltinModel("foo", "bar", 1).input_types


def build_model(model: ActiveModel, *, usage: ModelUsageContext) -> ChatModelBase:
    """Build an AgentScope chat model from an active preference model."""
    if not model.api_key:
        raise ValueError("api_key is required")
    if not model.model_id:
        raise ValueError("model_id is required")
    credential_kwargs: dict[str, str] = {"api_key": model.api_key}
    if model.base_url:
        credential_kwargs["base_url"] = model.base_url
    parameters = _parameters(model)
    context_size = _context_size(model)

    if model.provider_id == "dashscope":
        built: ChatModelBase = DashScopeChatModel(
            credential=DashScopeCredential(**credential_kwargs),
            model=model.model_id,
            stream=True,
            parameters=DashScopeChatModel.Parameters(**parameters),
            context_size=context_size,
            formatter=DashScopeChatFormatter(
                input_types=_input_types(model),
            ),
        )
    elif model.provider_id == "openai":
        built = OpenAIChatModel(
            credential=OpenAICredential(**credential_kwargs),
            model=model.model_id,
            stream=True,
            parameters=OpenAIChatModel.Parameters(**parameters),
            context_size=context_size,
        )
    else:
        raise ValueError(
            f"unsupported provider_id={model.provider_id!r}; "
            "supported providers: dashscope, openai"
        )
    built = attach_model_usage(built, usage, provider_id=model.provider_id)
    return attach_generation_span(built, usage)


def _parameters(model: ActiveModel) -> dict[str, Any]:
    values: dict[str, Any] = {"parallel_tool_calls": False}
    if model.thinking_enabled is not None:
        values["thinking_enable"] = model.thinking_enabled
    return values


def _context_size(model: ActiveModel) -> int:
    if model.context_size is not None:
        return model.context_size
    spec = provider_registry.get(model.provider_id)
    builtin = None if spec is None else spec.find_model(model.model_id)
    if builtin is None:
        raise ValueError(
            f"context_size is required for {model.provider_id}/{model.model_id}"
        )
    return builtin.context_size


def _input_types(
    model: ActiveModel,
) -> list[str]:
    """Return the formatter ``input_types`` for ``model``.

    Multimodal builtin models get the full media set so image/audio DataBlocks
    are emitted to the API. Text-only models (and unrecognised custom models,
    for which we have no multimodal metadata) get ``["text/plain"]`` only, so
    media DataBlocks are dropped instead of being sent as ``image_url``/``input_audio``
    items the model rejects — e.g. a ``Read``-returned PNG persisted in a prior
    turn otherwise poisons every later ``qwen3.7-max`` request with a 400
    ``Unexpected item type in content``.
    """
    spec = provider_registry.get(model.provider_id)
    builtin_model = spec.find_model(model.model_id) if spec else None
    if builtin_model:
        return builtin_model.input_types
    return _DEFAULT_INPUT_TYPES

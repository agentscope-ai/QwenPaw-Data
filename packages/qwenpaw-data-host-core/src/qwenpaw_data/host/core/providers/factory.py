# -*- coding: utf-8 -*-
from __future__ import annotations

from agentscope.credential import DashScopeCredential, OpenAICredential
from agentscope.model import ChatModelBase, DashScopeChatModel, OpenAIChatModel

from qwenpaw_data.host.core.providers.registry import ActiveModel


def build_model(model: ActiveModel) -> ChatModelBase:
    """Build an AgentScope chat model from an active model selection."""
    if not model.api_key:
        raise ValueError("api_key is required")
    if not model.model_id:
        raise ValueError("model_id is required")
    credential_kwargs: dict[str, str] = {"api_key": model.api_key}
    if model.base_url:
        credential_kwargs["base_url"] = model.base_url

    if model.provider_id == "dashscope":
        return DashScopeChatModel(
            credential=DashScopeCredential(**credential_kwargs),
            model=model.model_id,
            stream=True,
            parameters=DashScopeChatModel.Parameters(parallel_tool_calls=False),
        )

    if model.provider_id == "openai":
        return OpenAIChatModel(
            credential=OpenAICredential(**credential_kwargs),
            model=model.model_id,
            stream=True,
            parameters=OpenAIChatModel.Parameters(parallel_tool_calls=False),
        )

    raise ValueError(
        f"unsupported provider_id={model.provider_id!r}; "
        "supported providers: dashscope, openai"
    )

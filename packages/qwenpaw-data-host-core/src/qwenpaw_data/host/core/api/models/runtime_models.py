"""Private per-turn bindings for authenticated console and submission APIs.

Credentials are excluded from request serialization, persisted chat records,
and durable submission digests. Replays retain the original run even when
Host settings have changed.
"""

from __future__ import annotations

import base64
import json
from typing import Literal

from pydantic import Field, SecretStr

from .common import ApiModel
from ...domain.preference import UserRuntimeConfig
from ...providers.registry import ActiveModel

MODEL_BINDING_HEADER = "x-qwenpaw-model-binding"


class RuntimeModel(ApiModel):
    provider_id: Literal["openai", "dashscope"]
    model_id: str = Field(min_length=1, max_length=256)
    api_key: SecretStr = Field(repr=False)
    base_url: str = Field(min_length=1, max_length=2048)

    def active(self) -> ActiveModel:
        return ActiveModel(
            provider_id=self.provider_id,
            model_id=self.model_id,
            api_key=self.api_key.get_secret_value(),
            base_url=self.base_url,
            chat_model=(
                "DashScopeChatModel"
                if self.provider_id == "dashscope"
                else "OpenAIChatModel"
            ),
            name=self.model_id,
        )

    def context_header(self) -> str:
        data = {
            "model": self.model_id,
            "base_url": self.base_url,
            "api_key": self.api_key.get_secret_value(),
        }
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


class RuntimeModels(ApiModel):
    inherit_analysis: list[Literal["lightweight", "databridge"]] = Field(
        default_factory=list
    )
    analysis: RuntimeModel | None = None
    lightweight: RuntimeModel | None = None
    databridge: RuntimeModel | None = None

    def runtime_config(self, local: UserRuntimeConfig) -> UserRuntimeConfig:
        default = self.analysis.active() if self.analysis else local.default
        if "lightweight" in self.inherit_analysis:
            light = default
        elif self.lightweight is not None:
            light = self.lightweight.active()
        else:
            light = local.light or local.default or default
        if default is None:
            raise ValueError("Analysis model is not configured")
        return UserRuntimeConfig(default=default, light=light)

    def resolved_context_header(self, runtime: UserRuntimeConfig) -> str | None:
        if "databridge" in self.inherit_analysis:
            active = runtime.default
            if active is None:
                raise ValueError("Analysis model is not configured")
            return RuntimeModel(
                provider_id=active.provider_id,
                model_id=active.model_id,
                api_key=active.api_key,
                base_url=active.base_url,
            ).context_header()
        return self.databridge.context_header() if self.databridge else None

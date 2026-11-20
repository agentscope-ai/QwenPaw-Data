"""Adapt CM storage and model services to profiling's existing interfaces."""

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

from context_manager.api.dependencies import get_context_authorization
from context_manager.model import ChatMessage, LlmCaller
from context_manager.settings import get_settings
from profiling.algo.handlers.llm import OpenAiCompatClient
from profiling.algo.stores import TextStore
from semantic_config.cm_runtime import current_runtime

_DIRECTORY_TOKEN = re.compile(r"[^A-Za-z0-9_.-]")


def resolve_profile_out_dir(
    tenant_id: str, workspace_id: str, datasource_id: str
) -> Path:
    """Resolve datasource output and caches under the workspace's .cm/profile directory."""
    directory = get_settings().paths.home
    for part in (tenant_id, workspace_id, ".cm", "profile", datasource_id):
        cleaned = _DIRECTORY_TOKEN.sub("_", (part or "").strip())[:64]
        directory /= "default" if not cleaned or cleaned in {".", ".."} else cleaned
        if directory.is_symlink():
            raise ValueError(f"symlinks are not allowed: {directory}")
    return directory


class RuntimeLlmClient(OpenAiCompatClient):
    """Translate profiling's two prompts to the runtime-owned LlmCaller."""

    def __init__(self, config, llm: LlmCaller, selection):
        if not isinstance(llm, LlmCaller):
            raise TypeError("llm must implement LlmCaller")
        self._llm = llm
        if not selection["model"]:
            raise ValueError("CM LLM model is not configured")
        super().__init__(
            model=selection["model"],
            api_key="",
            base_url=selection["base_url"] or "",
            store=TextStore(config.datasource_out_dir / "llm"),
            max_attempts=config.llm_max_attempts,
        )

    @classmethod
    @asynccontextmanager
    async def open(cls, config):
        """Keep one LLM configuration and client for the profiling run."""
        runtime, authorization = current_runtime(), get_context_authorization()
        clients = runtime.models(authorization)
        resolved = await asyncio.to_thread(clients.resolve, "llm")
        async with clients.llm(resolved) as llm:
            yield cls(
                config, llm, {"model": resolved.model, "base_url": resolved.base_url}
            )

    async def _call_async(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        return await self._llm.complete(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
            temperature=temperature,
        )

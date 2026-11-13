from __future__ import annotations

import asyncio
import json

import httpx
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from qwenpaw_data.host.core.api.models.ui import (
    TOOL_DEFINITIONS,
    DatasetRef,
    Nonempty,
    PositiveId,
    StrictModel,
)
from qwenpaw_data.host.core.domain.ui import ui_tools_enabled
from qwenpaw_data.host.core.integrations.cm.client import CMClientError, ContextManagerClient
from pydantic import Field


class UiTool(ToolBase):
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = False

    def __init__(self, definition):
        super().__init__()
        self.name = definition["name"]
        self.description = definition["description"]
        self.input_schema = definition["parameters"]

    async def check_permissions(self, tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW)

    async def call(self, **kwargs):
        raise RuntimeError("UI tools execute on the frontend")


class LookupArgs(StrictModel):
    dataset_id: PositiveId | None = None
    datasource_id: Nonempty | None = None
    domain_id: PositiveId | None = None
    name: str | None = None
    page: int = Field(default=1, ge=1, le=100)
    size: int = Field(default=20, ge=1, le=50)


class DatasetLookupTool(UiTool):
    is_external_tool = False

    def __init__(self, access_token):
        super().__init__(
            {
                "name": "cm_dataset_lookup",
                "description": "Read authorized dataset candidates or an exact dataset. Use the returned object_ref with ui_open_object. If several candidates match, ask the user; never guess. Bounded pagination only.",
                "parameters": LookupArgs.model_json_schema(),
            }
        )
        self._access_token = access_token

    async def call(self, **kwargs):
        try:
            params = LookupArgs.model_validate(kwargs)
            if not self._access_token:
                raise ValueError("FORBIDDEN: missing CM identity")
            payload = await asyncio.to_thread(
                ContextManagerClient(access_token=self._access_token).lookup_datasets,
                **params.model_dump(),
            )
            records = [payload] if params.dataset_id else payload["records"]
            items = []
            for item in records:
                if not isinstance(item, dict):
                    raise TypeError("Invalid dataset response")
                ref = DatasetRef.model_validate(
                    {
                        "kind": "cm.dataset",
                        "dataset_id": item.get("dataset_id"),
                        "domain_id": item.get("domain_id"),
                        "datasource_id": item.get("datasource_id"),
                    }
                )
                if params.dataset_id and ref.dataset_id != params.dataset_id:
                    raise ValueError("TARGET_NOT_FOUND")
                items.append(
                    {
                        "object_ref": ref.model_dump(),
                        "name": item.get("dataset_name"),
                        "description": item.get("description"),
                    }
                )
            value = {
                "items": items,
                "page": params.page,
                "total": 1 if params.dataset_id else payload["total"],
            }
            return ToolChunk(
                content=[TextBlock(text=json.dumps(value, ensure_ascii=False))],
                state=ToolResultState.SUCCESS,
            )
        except (httpx.HTTPError, CMClientError, ValueError, TypeError, KeyError):
            # HTTP URLs/headers and raw server errors must not enter the model context.
            return ToolChunk(
                content=[
                    TextBlock(
                        text=json.dumps(
                            {
                                "error": "Dataset lookup failed or access denied; do not guess a target."
                            }
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )


def build_ui_tools(context):
    from qwenpaw_data.host.core.utils.auth import resolve_context_access_token

    token = context.request_context.get("access_token")
    if token is not None and not isinstance(token, str):
        raise TypeError("request_context access_token must be a string")
    tools = [DatasetLookupTool(resolve_context_access_token(token))]
    if ui_tools_enabled():
        tools = [*(UiTool(definition) for definition in TOOL_DEFINITIONS), *tools]
    return tools
